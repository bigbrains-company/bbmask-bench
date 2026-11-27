"""The language-model audit: what the model says is checked against the text."""

from __future__ import annotations

import json
import random

from bbmask_bench import audit, report
from bbmask_bench.runner import MaskOptions, RunStore, run
from tests.test_runner import FakeClient, make_doc


class FakeModel:
    """Answers every request with the same scripted findings."""

    name = "fake-model"

    def __init__(self, findings: list[dict]):
        self.findings = findings
        self.requests: list[str] = []

    def complete(self, system: str, user: str) -> tuple[str, dict[str, int]]:
        self.requests.append(user)
        return json.dumps({"findings": self.findings}), {"input_tokens": 10, "output_tokens": 2}


def test_chunks_join_back_and_never_cut_a_placeholder():
    line = "Davacı <PERSON_12_U> ile davalı <ORGANIZATION_3> arasında. " * 40
    text = f"{line}\nkısa satır\n\n{line}\n\n\n"
    chunks = audit.split_chunks(text, max_chars=200)
    assert "".join(chunks) == text
    assert all(len(chunk) <= 200 or not chunk[200:].strip() for chunk in chunks)
    assert all(chunk.count("<") == chunk.count(">") for chunk in chunks)
    assert audit.split_chunks("", 100) == []


def test_answers_that_are_not_the_expected_json_have_no_findings():
    assert audit.parse_findings("sorry, I cannot") == []
    assert audit.parse_findings('```json\n{"findings": [{"type": "PERSON", "text": "A"}]}\n```')
    assert audit.parse_findings('{"findings": "none"}') == []


def test_only_findings_that_are_in_the_text_count():
    clean = "Davacı \u2022 ile tanık Mehmet Demir geldi. Mehmet Demir'in adresi: Gül Sok. No: 4."
    kept, rejected = audit.verify(
        [
            {"type": "PERSON", "text": "Mehmet Demir"},
            {"type": "PERSON", "text": "Demir"},  # part of the longer finding: not counted twice
            {"type": "PERSON", "text": "Ayşe Yılmaz"},  # not in the text
            {"type": "PERSON", "text": "<PERSON_0>"},  # a placeholder is not a finding
            {"type": "ADDRESS", "text": "Gül Sok. No: 4"},
            {"type": "HOBBY", "text": "geldi"},  # not a type that was asked for
        ],
        clean,
        state={"PERSON": {"Ayşe Yılmaz": "<PERSON_0>"}},
    )
    assert rejected == 3
    assert [(f["type"], f["text"], f["occurrences"]) for f in kept] == [
        ("ADDRESS", "Gül Sok. No: 4", 1),
        ("PERSON", "Mehmet Demir", 2),
    ]
    assert not any(f["masked_elsewhere"] for f in kept)


def test_a_value_the_service_masked_elsewhere_is_told_apart_from_a_miss():
    clean = "\u2022 geldi. Sonra AYŞE YILMAZ ve Kaya konuştu."
    kept, _ = audit.verify(
        [{"type": "PERSON", "text": "AYŞE YILMAZ"}, {"type": "PERSON", "text": "Kaya"}],
        clean,
        state={"PERSON": {"Ayşe Yılmaz": "<PERSON_0>"}},
    )
    assert {f["text"]: f["masked_elsewhere"] for f in kept} == {"AYŞE YILMAZ": True, "Kaya": False}


def build_run(tmp_path) -> RunStore:
    store = RunStore(tmp_path / "runs" / "t")
    docs = [
        make_doc(tmp_path, "a", "Davacı Ayşe ile tanık Mehmet Demir geldi."),
        make_doc(tmp_path, "b", "Davalı Ayşe gelmedi."),
    ]
    run(FakeClient(), docs, MaskOptions(), store, manifest_extra={"sdk_version": "x"})
    return store


def test_audit_is_stored_once_and_reported_as_counts(tmp_path):
    store = build_run(tmp_path)
    model = FakeModel([{"type": "PERSON", "text": "Mehmet Demir"}])

    first = audit.run_audit(store, model, concurrency=1)
    assert (first["audited"], first["failed"]) == (2, 0)
    assert all("<PERSON_0>" in request for request in model.requests)  # the masked text is sent
    again = audit.run_audit(store, model, concurrency=1)
    assert (again["audited"], again["skipped_already_done"]) == (0, 2)
    assert len(model.requests) == 2

    rows = report.evaluate(store)
    summary = report.summarize(
        store.read_records(), rows, store.read_manifest(), audit.read_counts(store)
    )
    person = summary["quality"]["llm_audit"]["by_type"]["PERSON"]
    # Two names were masked (one per document) and one occurrence of another name was left.
    assert (person["masked"], person["left"], person["never_masked"]) == (2, 1, 1)
    assert person["rate"] == report._rate(2, 3)
    assert summary["quality"]["llm_audit"]["documents_with_findings"] == 1

    report.publish(store, tmp_path / "results")
    published = "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "results").iterdir())
    assert "Mehmet" not in published and "Demir" not in published
    assert "Second opinion" in report.render_results(summary, "en")
    assert "Dil modeliyle" in report.render_results(summary, "tr")


def test_without_an_audit_the_report_has_no_audit_section(tmp_path):
    store = build_run(tmp_path)
    rows = report.evaluate(store)
    summary = report.summarize(store.read_records(), rows, store.read_manifest())
    assert summary["quality"]["llm_audit"] is None
    assert "Second opinion" not in report.render_results(summary, "en")


def test_a_rejected_key_stops_the_audit(tmp_path):
    class Refusing(FakeModel):
        def complete(self, system, user):
            raise audit.AuditError(401, "invalid key")

    summary = audit.run_audit(build_run(tmp_path), Refusing([]), concurrency=1)
    assert summary["audited"] == 0 and summary["stop_reason"].startswith("HTTP 401")


def test_court_file_numbers_are_neither_counted_nor_planted():
    artifact = {
        "masked_text": "Dosya <TR_REGISTRY_NO_0>, sicil <TR_REGISTRY_NO_1>, <PERSON_0> <PERSON_0>",
        "state": {
            "TR_REGISTRY_NO": {"2023/4321": "<TR_REGISTRY_NO_0>", "987654": "<TR_REGISTRY_NO_1>"},
            "PERSON": {"Ayşe Yılmaz": "<PERSON_0>"},
            "TR_CASE_NO": {"2024/7 E.": "<TR_CASE_NO_0>"},
        },
    }
    assert audit.masked_counts(artifact) == {"ID_NUMBER": 1, "PERSON": 2}
    text, planted = audit.plant_leaks(artifact, 5, random.Random(1))
    assert sorted(planted) == [("ID_NUMBER", "987654"), ("PERSON", "Ayşe Yılmaz")]
    assert "<TR_REGISTRY_NO_0>" in text and "Ayşe Yılmaz Ayşe Yılmaz" in text


def test_calibration_counts_the_planted_values_the_model_reports(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "MIN_PLANT_LENGTH", 4)  # the fake service masks a short name
    store = build_run(tmp_path)

    class Echo(FakeModel):
        """Reports the name whenever it is readable, as a good auditor would."""

        def complete(self, system, user):
            self.requests.append(user)
            found = [{"type": "PERSON", "text": "Ayşe"}] if "Ayşe" in user else []
            return json.dumps({"findings": found}), {"input_tokens": 5, "output_tokens": 1}

    model = Echo([])
    result = audit.calibrate(store, model, documents=5, per_document=3, concurrency=1)
    # Each document has one masked value; unmasking it puts the name back into the text.
    assert all("Ayşe" in request and "<PERSON_0>" not in request for request in model.requests)
    assert (result["documents"], result["planted"], result["found"]) == (2, 2, 2)
    assert result["by_type"] == {"PERSON": {"planted": 2, "found": 2}}

    blind = audit.calibrate(store, FakeModel([]), documents=5, concurrency=1)
    assert (blind["planted"], blind["found"]) == (2, 0)

    saved = audit.calibration_path(store).read_text()
    assert "Ayşe" not in saved
    assert audit.read_calibration(store)["found"] == 0
