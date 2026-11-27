"""Runner behaviour that protects credits: locking, resume, retry policy, failure records."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import pytest

from bbmask_bench.corpus import Document
from bbmask_bench.runner import (
    MaskOptions,
    RunLocked,
    RunStore,
    estimate_cost,
    process_document,
    run,
)


class FakeServiceError(RuntimeError):
    """Stands in for bb_mask.BBError: the runner only looks at ``status_code``."""

    def __init__(self, status_code: int, detail: str = "boom"):
        super().__init__(detail)
        self.status_code = status_code
        self.request_id = None


@dataclass
class FakeTagged:
    text: str
    state: dict | None
    doc_id: str | None = None
    entities_found: dict = field(default_factory=lambda: {"PERSON": 1})
    balance_credits: str | None = "10.00"
    request_id: str | None = "req_test"
    placeholder_style: str = "angle"
    llm_review: dict | None = None


class FakeClient:
    """Masks the word 'Ayşe'. Failures are scripted per method as a list of status codes."""

    def __init__(self, fail: dict[str, list[int]] | None = None):
        self.fail = fail or {}
        self.calls: dict[str, int] = {"anonymize": 0, "restore": 0, "get_map": 0, "delete": 0}

    def _maybe_fail(self, name: str) -> None:
        self.calls[name] += 1
        queue = self.fail.get(name)
        if queue:
            status = queue.pop(0)
            if status:
                try:
                    raise TimeoutError("socket")  # gives the error a __cause__, like httpx does
                except TimeoutError as cause:
                    raise FakeServiceError(status) from cause

    def anonymize(self, text, persist=True, **options):
        self._maybe_fail("anonymize")
        state = {"PERSON": {"Ayşe": "<PERSON_0>"}}
        return FakeTagged(
            text=text.replace("Ayşe", "<PERSON_0>"),
            state=None if persist else state,
            doc_id="svc-1" if persist else None,
        )

    def restore(self, doc_id_or_state, model_text):
        self._maybe_fail("restore")
        return model_text.replace("<PERSON_0>", "Ayşe")

    def get_map(self, doc_id, once=False):
        self._maybe_fail("get_map")
        return {"PERSON": {"Ayşe": "<PERSON_0>"}}

    def delete(self, doc_id):
        self._maybe_fail("delete")
        return True


def make_doc(tmp_path: Path, name: str = "a", text: str = "Davacı Ayşe geldi.") -> Document:
    path = tmp_path / f"{name}.txt"
    path.write_text(text, encoding="utf-8")
    return Document(
        doc_id=name, case_id="c", kind="document", path=path, n_bytes=path.stat().st_size
    )


def no_sleep(_seconds: float) -> None:
    return None


def test_success_writes_record_and_artifact(tmp_path):
    store = RunStore(tmp_path / "run")
    store.prepare()
    client = FakeClient()

    record = process_document(client, make_doc(tmp_path), MaskOptions(), store)

    assert record.status == "ok"
    assert record.balance_after == "10.00"
    artifact = store.read_artifact("a")
    assert artifact["masked_text"] == "Davacı <PERSON_0> geldi."
    assert artifact["restored_text"] == "Davacı Ayşe geldi."
    assert store.read_records()[0]["status"] == "ok"


def test_504_is_not_retried_and_failure_is_described(tmp_path):
    store = RunStore(tmp_path / "run")
    store.prepare()
    client = FakeClient(fail={"anonymize": [504]})

    record = process_document(client, make_doc(tmp_path), MaskOptions(), store, sleep=no_sleep)

    assert client.calls["anonymize"] == 1  # a 504 may have been charged: never resend blindly
    assert record.status == "error"
    assert (record.error_status, record.error_stage) == (504, "anonymize")
    assert record.error_type == "TimeoutError"
    assert record.error_elapsed_ms is not None


def test_503_is_retried(tmp_path):
    store = RunStore(tmp_path / "run")
    store.prepare()
    client = FakeClient(fail={"anonymize": [503, 0]})

    record = process_document(client, make_doc(tmp_path), MaskOptions(), store, sleep=no_sleep)

    assert record.status == "ok"
    assert record.attempts == 2


def test_failed_restore_does_not_pay_for_anonymize_twice(tmp_path):
    store = RunStore(tmp_path / "run")
    store.prepare()
    client = FakeClient(fail={"restore": [504]})
    doc = make_doc(tmp_path)

    first = process_document(client, doc, MaskOptions(), store, sleep=no_sleep)
    assert (first.status, first.error_stage) == ("error", "restore")
    assert store.read_artifact("a")["masked_text"]  # the paid response is already on disk

    second = process_document(client, doc, MaskOptions(), store, sleep=no_sleep)
    assert second.status == "ok"
    assert second.request_id == "req_test"
    assert client.calls["anonymize"] == 1
    assert client.calls["restore"] == 2


def test_persist_mode_fetches_map_and_deletes_it(tmp_path):
    store = RunStore(tmp_path / "run")
    store.prepare()
    client = FakeClient()

    record = process_document(client, make_doc(tmp_path), MaskOptions(persist=True), store)

    assert record.status == "ok"
    assert client.calls["get_map"] == 1 and client.calls["delete"] == 1
    artifact = store.read_artifact("a")
    assert artifact["state"] == {"PERSON": {"Ayşe": "<PERSON_0>"}}
    assert artifact["service_doc_id"] is None


def test_run_resumes_without_resending_finished_documents(tmp_path):
    store = RunStore(tmp_path / "run")
    docs = [make_doc(tmp_path, "a"), make_doc(tmp_path, "b")]
    client = FakeClient(fail={"anonymize": [0, 504]})

    first = run(client, docs, MaskOptions(), store)
    assert (first["ok"], first["error"]) == (1, 1)

    second = run(client, docs, MaskOptions(), store)
    assert second["skipped_already_done"] == 1
    assert (second["ok"], second["error"]) == (1, 0)
    assert client.calls["anonymize"] == 3


def test_run_directory_is_exclusive(tmp_path):
    store = RunStore(tmp_path / "run")
    other = RunStore(tmp_path / "run")
    client = FakeClient()

    with store.lock(), pytest.raises(RunLocked):
        run(client, [make_doc(tmp_path)], MaskOptions(), other)
    assert client.calls["anonymize"] == 0

    assert run(client, [make_doc(tmp_path)], MaskOptions(), other)["ok"] == 1  # lock released


def test_run_stops_after_consecutive_connection_failures(tmp_path):
    store = RunStore(tmp_path / "run")
    docs = [make_doc(tmp_path, name) for name in "abcdef"]
    client = FakeClient(fail={"anonymize": [0, 504, 504, 504, 0, 0]})

    summary = run(client, docs, MaskOptions(), store, max_consecutive_failures=3)

    assert summary["stopped_early"] and "connection failures" in summary["stop_reason"]
    assert client.calls["anonymize"] == 4  # the last two documents were never sent
    assert (summary["ok"], summary["error"]) == (1, 3)


def test_a_success_resets_the_failure_streak(tmp_path):
    store = RunStore(tmp_path / "run")
    docs = [make_doc(tmp_path, name) for name in "abcde"]
    client = FakeClient(fail={"anonymize": [504, 504, 0, 504, 504]})

    summary = run(client, docs, MaskOptions(), store, max_consecutive_failures=3)

    assert not summary["stopped_early"]
    assert client.calls["anonymize"] == 5


def test_run_refuses_changed_options(tmp_path):
    store = RunStore(tmp_path / "run")
    docs = [make_doc(tmp_path)]
    run(FakeClient(), docs, MaskOptions(), store)
    with pytest.raises(ValueError):
        run(FakeClient(), docs, MaskOptions(health_data=True), store)


def test_estimate_cost_rounds_each_document_up_to_a_thousand_characters():
    # Values taken from the usage page of the service.
    assert estimate_cost([2244]) == Decimal("0.03")
    assert estimate_cost([10003]) == Decimal("0.11")
    assert estimate_cost([43993, 4339]) == Decimal("0.49")


def test_interrupt_stops_sending_and_keeps_what_was_paid_for(tmp_path):
    client = FakeClient()
    store = RunStore(tmp_path / "run")
    docs = [make_doc(tmp_path, name) for name in "abcde"]
    events = []

    def interrupt_after_first(record, index, total):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run(
            client,
            docs,
            MaskOptions(),
            store,
            on_record=interrupt_after_first,
            on_interrupt=lambda: events.append("stopping"),
        )

    assert events == ["stopping"]
    # The pool had five documents queued. Only what was already in flight may complete.
    assert client.calls["anonymize"] <= 2
    assert len(store.read_records()) == client.calls["anonymize"]


def test_pause_rests_after_each_document(tmp_path, monkeypatch):
    import threading

    rests = []
    monkeypatch.setattr(threading.Event, "wait", lambda self, timeout=None: rests.append(timeout))
    docs = [make_doc(tmp_path, name) for name in "abc"]
    run(FakeClient(), docs, MaskOptions(), RunStore(tmp_path / "run"), pause=2.5)
    # Other code waits on events too; only the runner waits for exactly this long.
    assert rests.count(2.5) == 3
