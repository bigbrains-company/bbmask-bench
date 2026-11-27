"""Report pipeline end to end on a tiny synthetic run: no network, no real data."""

from __future__ import annotations

import json

from bbmask_bench import charts, report
from bbmask_bench.runner import MaskOptions, RunStore, run
from tests.test_runner import FakeClient, make_doc

TCKN = "10000000146"


class LeakyClient(FakeClient):
    """Masks the name but leaves national IDs in documents whose text contains 'SIZINTI'."""

    def anonymize(self, text, persist=True, **options):
        tagged = super().anonymize(text, persist, **options)
        if "SIZINTI" not in text:
            tagged.text = tagged.text.replace(TCKN, "<TR_NATIONAL_ID_0>")
            tagged.state["TR_NATIONAL_ID"] = {TCKN: "<TR_NATIONAL_ID_0>"}
        return tagged

    def restore(self, doc_id_or_state, model_text):
        return super().restore(doc_id_or_state, model_text).replace("<TR_NATIONAL_ID_0>", TCKN)


def build_run(tmp_path, texts: dict[str, str], fail: dict | None = None) -> RunStore:
    store = RunStore(tmp_path / "runs" / "t")
    docs = [make_doc(tmp_path, name, text) for name, text in texts.items()]
    run(LeakyClient(fail=fail), docs, MaskOptions(), store, manifest_extra={"sdk_version": "x"})
    return store


def test_percentile_is_nearest_rank():
    values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert report.percentile(values, 0.5) == 5
    assert report.percentile(values, 0.9) == 9
    assert report.percentile(values, 0.99) == 10
    assert report.percentile([], 0.5) == 0.0


def test_a_single_leak_never_rounds_up_to_100_percent():
    rate = report._rate(999_999, 1_000_000)
    assert rate < 1
    assert report._pct(rate, "en") == "99.99%"
    assert report._pct(1.0, "en") == "100%"
    assert report._pct(0.98765, "tr") == "%98,76"
    assert report._num(1234567.5, "tr", 1) == "1.234.567,5"


def test_summary_counts_leaks_and_failures(tmp_path):
    store = build_run(
        tmp_path,
        {
            "a": f"Davacı Ayşe, T.C. {TCKN}. 2918 sayılı Kanun.",
            "b": f"SIZINTI Davacı Ayşe, T.C. {TCKN}.",
            "c": "Davacı Ayşe geldi.",
        },
        fail={"anonymize": [0, 0, 504]},
    )
    rows = report.evaluate(store)
    summary = report.summarize(store.read_records(), rows, store.read_manifest())

    assert summary["reliability"]["ok"] == 2
    assert summary["reliability"]["failed_by_status"] == {"504": 1}
    assert summary["quality"]["roundtrip"]["exact"] == 2
    national_id = summary["quality"]["structured_identifiers"]["TR_NATIONAL_ID"]
    assert (national_id["occurrences"], national_id["residual"]) == (2, 1)
    assert national_id["masked_rate"] == 0.5
    assert summary["quality"]["documents_with_identifier_leak"] == 1
    assert summary["quality"]["context_retention"]["statute_reference"]["retention"] == 1


def test_publish_writes_pii_free_files_and_updates_the_readme(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = build_run(tmp_path, {"a": f"Davacı Ayşe, T.C. {TCKN}.", "b": "Davalı Ayşe geldi."})
    report.evaluate(store)
    summary = report.publish(store, tmp_path / "results")

    published = "".join(
        path.read_text(encoding="utf-8") for path in (tmp_path / "results").iterdir()
    )
    assert "Ayşe" not in published and TCKN not in published
    assert "req_test" not in published and "balance" not in published
    assert len((tmp_path / "results" / "documents.jsonl").read_text().splitlines()) == 2
    assert (
        json.loads((tmp_path / "results" / "summary.json").read_text())["corpus"]["documents"] == 2
    )

    readme = tmp_path / "README.md"
    readme.write_text(f"intro\n{report.RESULTS_START}\nold\n{report.RESULTS_END}\noutro\n")
    block = report.render_results(summary, "en", "results", sections=report.README_SECTIONS)
    assert report.inject_results(readme, block)
    text = readme.read_text()
    assert text.startswith("intro\n") and text.endswith("outro\n") and "\nold\n" not in text
    assert "| **2** |" in text
    assert not report.inject_results(tmp_path / "missing.md", block)

    turkish = report.render_results(summary, "tr", "results")
    assert "Gidiş-dönüş" in turkish and "%100" in turkish


def test_charts_are_valid_svg_even_when_empty():
    assert charts.latency_scatter([], None).startswith("<svg")
    assert charts.latency_scatter([(1000, 1.0)], 1000).count("<circle") == 1
    bars = charts.horizontal_bars("Types", [("PERSON", 10), ("A & B", 0)])
    assert "A &amp; B" in bars and bars.count("<rect") == 3


def test_report_command_prints_by_default_and_publishes_on_request(tmp_path, monkeypatch, capsys):
    from bbmask_bench import cli

    monkeypatch.chdir(tmp_path)
    build_run(tmp_path, {"a": f"Davacı Ayşe, T.C. {TCKN}.", "b": "Davalı Ayşe geldi."})
    markers = f"{report.RESULTS_START}\n{report.RESULTS_END}\n"
    (tmp_path / "README.md").write_text(markers)
    docs = tmp_path / "docs" / "v0"
    docs.mkdir(parents=True)
    (docs / "README.tr.md").write_text(markers)

    # Without --publish the report goes to the terminal and the repository files stay as they are.
    assert cli.main(["report", "t"]) == 0
    assert "Round trip" in capsys.readouterr().out
    assert (tmp_path / "results" / "t" / "summary.json").is_file()
    assert (tmp_path / "README.md").read_text() == markers
    assert not (docs / "RESULTS.md").exists()

    assert cli.main(["report", "t", "--publish", "--docs-dir", "docs/v0"]) == 0
    assert "results/t/latency.svg" in (tmp_path / "README.md").read_text()
    # Links in files under docs/ are relative to that directory.
    assert "../../results/t/latency.svg" in (docs / "README.tr.md").read_text()
    assert "../../results/t/summary.json" in (docs / "RESULTS.md").read_text()
    assert "Gidiş-dönüş" in (docs / "RESULTS.tr.md").read_text()
