"""Command line interface: ``bbmask-bench corpus | run | audit | evaluate | report``."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from bbmask_bench import __version__, corpus
from bbmask_bench.config import (
    DEFAULT_RUNS_DIR,
    ConfigError,
    default_corpus_dir,
    load_env,
    load_settings,
)
from bbmask_bench.runner import MaskOptions, Record, RunLocked, RunStore, estimate_cost, run

DEFAULT_MIN_BYTES = 200


def _add_selection_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("document selection")
    group.add_argument("--corpus", type=Path, default=None, help="corpus directory")
    group.add_argument("--limit", type=int, default=None, help="number of documents (default: all)")
    group.add_argument("--seed", type=int, default=42, help="sampling seed (default: 42)")
    group.add_argument(
        "--kinds",
        nargs="+",
        choices=[corpus.KIND_PETITION, corpus.KIND_ATTACHMENT, corpus.KIND_DOCUMENT],
        help="restrict to these document kinds",
    )
    group.add_argument(
        "--min-bytes",
        type=int,
        default=DEFAULT_MIN_BYTES,
        help=f"skip documents smaller than this (default: {DEFAULT_MIN_BYTES})",
    )
    group.add_argument(
        "--max-bytes", type=int, default=None, help="skip documents larger than this"
    )


def _select(args: argparse.Namespace) -> tuple[list[corpus.Document], list[corpus.Document]]:
    root = args.corpus or default_corpus_dir()
    everything = corpus.discover(root)
    eligible = corpus.filter_documents(
        everything, kinds=args.kinds, min_bytes=args.min_bytes, max_bytes=args.max_bytes
    )
    return everything, corpus.sample(eligible, args.limit, args.seed)


def _kb(n_bytes: float) -> str:
    return f"{n_bytes / 1000:.1f} kB"


# --- corpus ---------------------------------------------------------------------------------


def cmd_corpus(args: argparse.Namespace) -> int:
    load_env()
    everything, selected = _select(args)
    stats = corpus.describe(selected)
    stats["excluded_by_filters"] = len(everything) - len(
        corpus.filter_documents(
            everything, kinds=args.kinds, min_bytes=args.min_bytes, max_bytes=args.max_bytes
        )
    )
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return 0

    print(f"Corpus: {args.corpus or default_corpus_dir()}")
    print(f"  documents : {stats['documents']}  (in {stats['cases']} cases)")
    print(f"  excluded  : {stats['excluded_by_filters']} by filters")
    print(f"  total size: {stats['bytes']['total'] / 1e6:.1f} MB")
    b = stats["bytes"]
    print(f"  size      : p50 {_kb(b['p50'])} | p90 {_kb(b['p90'])} | max {_kb(b['max'])}")
    for title, key in (
        ("by kind", "by_kind"),
        ("by size bucket", "by_size_bucket"),
        ("petitions by case type", "petitions_by_case_type"),
        ("attachments by source format", "attachments_by_source_format"),
    ):
        if stats[key]:
            print(f"  {title}:")
            for name, count in stats[key].items():
                print(f"    {name:<12} {count}")
    return 0


# --- run ------------------------------------------------------------------------------------


def _progress(record: Record, index: int, total: int) -> None:
    width = len(str(total))
    head = f"[{index:>{width}}/{total}] {record.doc_id} {record.kind:<10} {_kb(record.n_chars):>9}"
    if record.status == "ok":
        entities = sum(record.entities_found.values())
        print(
            f"{head}  ok   anonymize {record.anonymize_ms / 1000:6.2f}s"
            f"  restore {record.restore_ms / 1000:5.2f}s"
            f"  entities {entities:>4}  balance {record.balance_after}",
            flush=True,
        )
    else:
        took = f"{record.error_elapsed_ms / 1000:.1f}s" if record.error_elapsed_ms else "?"
        print(
            f"{head}  FAIL HTTP {record.error_status} at {record.error_stage} "
            f"after {took} ({record.error_type}): {record.error_detail}",
            flush=True,
        )


def _stopping() -> None:
    print(
        "\nStopping. Waiting for the document in progress: it will be charged either way.",
        flush=True,
    )


def _confirm(documents: list[corpus.Document], assume_yes: bool) -> bool:
    estimate = estimate_cost([len(doc.read_text()) for doc in documents])
    print(
        f"\nThis will make up to {len(documents)} anonymize calls, about {estimate} in credits "
        "(0.01 per started 1,000 characters)."
    )
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print("Refusing to spend credits without confirmation. Re-run with --yes.")
        return False
    return input("Continue? [y/N] ").strip().lower() in {"y", "yes", "e", "evet"}


def cmd_run(args: argparse.Namespace) -> int:
    load_env()
    _, selected = _select(args)
    if not selected:
        print("No documents match the selection.")
        return 1

    options = MaskOptions(
        placeholder_style=args.placeholder_style,
        keep_legal_form=args.keep_legal_form,
        health_data=args.health_data,
        llm_review=args.llm_review,
        persist=args.persist,
    )
    run_id = args.run_id or datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    store = RunStore(Path(args.runs_dir) / run_id)
    done = {r["doc_id"] for r in store.read_records() if r["status"] == "ok"}
    pending = [d for d in selected if d.doc_id not in done]
    calls = len(pending) if args.max_credits is None else min(len(pending), args.max_credits)

    print(f"Run      : {store.run_dir}")
    print(f"Selected : {len(selected)} documents ({len(done & {d.doc_id for d in selected})} done)")
    print(f"Options  : {options}")
    if args.dry_run:
        for doc in pending[:calls]:
            print(
                f"  would send {doc.doc_id} {doc.kind:<10} {doc.size_bucket:<6} {_kb(doc.n_bytes)}"
            )
        print(f"\nDry run: {calls} calls planned, nothing sent, no credits spent.")
        return 0
    if calls == 0:
        print("Nothing to do: every selected document already has a successful record.")
        return 0

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not _confirm(pending[:calls], args.yes):
        return 1

    import bb_mask  # imported late so `corpus` and `--dry-run` work without the SDK installed

    timeout = args.timeout or settings.timeout
    print(f"Service  : {settings.base_url} (key from ${settings.token_source})")
    print(f"Timeout  : {timeout:g}s\n")
    try:
        with bb_mask.BBAnonym(settings.base_url, settings.token, timeout=timeout) as client:
            summary = run(
                client,
                selected,
                options,
                store,
                concurrency=args.concurrency,
                max_credits=args.max_credits,
                retries=args.retries,
                max_consecutive_failures=args.max_consecutive_failures or None,
                pause=args.pause,
                on_record=_progress,
                on_interrupt=_stopping,
                manifest_extra={
                    "run_id": run_id,
                    "base_url": settings.base_url,
                    "sdk_version": getattr(bb_mask.client, "VERSION", "unknown"),
                    "timeout_s": timeout,
                    "seed": args.seed,
                    "limit": args.limit,
                    "min_bytes": args.min_bytes,
                    "max_bytes": args.max_bytes,
                    "kinds": args.kinds,
                },
            )
    except RunLocked as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"\nDone: {summary['ok']} ok, {summary['error']} failed.")
    if summary["stopped_early"]:
        print(f"Stopped early: {summary['stop_reason']}.")
    print(f"Next: bbmask-bench evaluate {run_id}")
    return 0 if summary["error"] == 0 else 3


# --- audit ----------------------------------------------------------------------------------


def cmd_audit(args: argparse.Namespace) -> int:
    from bbmask_bench import audit

    load_env()
    store = _open_run(args)
    if store is None:
        return 2
    try:
        model = audit.openai_from_env(args.model, reasoning_effort=args.reasoning_effort)
    except audit.AuditError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    def progress(result: dict, index: int, total: int) -> None:
        # Counts only: the findings themselves are personal data and stay in the run directory.
        used = result["usage"]
        print(
            f"[{index:>{len(str(total))}}/{total}] {result['doc_id']}"
            f"  requests {result['chunks']:>3}"
            f"  still readable {sum(f['occurrences'] for f in result['findings']):>4}"
            f"  rejected {result['rejected']:>3}"
            f"  tokens {used.get('input_tokens', 0) + used.get('output_tokens', 0):>7}",
            flush=True,
        )

    print(f"Run   : {store.run_dir}")
    print(f"Model : {model.name} (the masked text of each document is sent to it)\n", flush=True)
    if args.calibrate:
        try:
            result = audit.calibrate(
                store,
                model,
                documents=args.calibrate,
                chunk_chars=args.chunk_chars,
                concurrency=args.concurrency,
            )
        except audit.AuditError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        finally:
            model.close()
        print(
            f"Planted {result['planted']} known values in {result['documents']} documents; "
            f"the model reported {result['found']}."
        )
        for name, stats in result["by_type"].items():
            print(f"  {name:<14} {stats['found']:>4} of {stats['planted']:>4}")
        print(f"Wrote {audit.calibration_path(store)}")
        return 0
    try:
        summary = audit.run_audit(
            store,
            model,
            limit=args.limit,
            concurrency=args.concurrency,
            chunk_chars=args.chunk_chars,
            on_document=progress,
        )
    finally:
        model.close()

    used = summary["usage"]
    print(
        f"\nAudited {summary['audited']} documents "
        f"({summary['skipped_already_done']} were already done), {summary['failed']} failed."
    )
    print(
        f"Tokens: {used.get('input_tokens', 0):,} in "
        f"({used.get('cached_input_tokens', 0):,} cached), {used.get('output_tokens', 0):,} out "
        f"({used.get('reasoning_tokens', 0):,} reasoning)."
    )
    if summary["stop_reason"]:
        print(f"Stopped: {summary['stop_reason']}")
        return 2
    return 0 if not summary["failed"] else 3


# --- evaluate / report ----------------------------------------------------------------------

# Which language each README's results block is written in. The Turkish README lives with
# the other documents, next to the full report.
README_NAMES = {"en": "README.md", "tr": "README.tr.md"}


def _open_run(args: argparse.Namespace) -> RunStore | None:
    store = RunStore(Path(args.runs_dir) / args.run_id)
    if not store.records_path.is_file():
        print(f"error: no run found at {store.run_dir}", file=sys.stderr)
        return None
    return store


def _print_summary(summary: dict) -> None:
    from bbmask_bench import report

    sections = ("headline", "identifiers", "roundtrip", "audit")
    print(report.render_results(summary, "en", sections=sections))


def cmd_evaluate(args: argparse.Namespace) -> int:
    from bbmask_bench import audit, report

    store = _open_run(args)
    if store is None:
        return 2
    rows = report.evaluate(store)
    print(f"Evaluated {len(rows)} documents -> {store.run_dir / 'metrics.jsonl'}\n")
    _print_summary(
        report.summarize(
            store.read_records(),
            rows,
            store.read_manifest(),
            audit.read_counts(store),
            audit.read_calibration(store),
        )
    )
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from bbmask_bench import report

    store = _open_run(args)
    if store is None:
        return 2
    rows = report.evaluate(store)
    out_dir = Path(args.out or Path("results") / args.run_id)
    summary = report.publish(store, out_dir)
    print(f"Evaluated {len(rows)} documents")
    print(f"Wrote {out_dir}/summary.json, documents.jsonl, latency.svg, entities.svg")

    if not args.publish:
        print()
        print(report.render_results(summary, args.lang, out_dir.as_posix()))
        return 0

    docs_dir = Path(args.docs_dir)
    docs_dir.mkdir(parents=True, exist_ok=True)
    from_docs = Path(os.path.relpath(out_dir, docs_dir)).as_posix()
    for lang, name in (("en", "RESULTS.md"), ("tr", "RESULTS.tr.md")):
        title = "# Benchmark results" if lang == "en" else "# Benchmark sonuçları"
        body = report.render_results(summary, lang, from_docs)
        (docs_dir / name).write_text(f"{title}\n\n{body}", encoding="utf-8")
        print(f"Wrote {docs_dir / name}")

    readmes = (
        (Path(README_NAMES["en"]), "en", out_dir.as_posix()),
        (docs_dir / README_NAMES["tr"], "tr", from_docs),
    )
    for readme, lang, results_path in readmes:
        block = report.render_results(summary, lang, results_path, sections=report.README_SECTIONS)
        if report.inject_results(readme, block):
            print(f"Updated results in {readme}")
    return 0


# --- parser ---------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bbmask-bench",
        description="Benchmark the bb-mask (Data Mask) anonymization service on your documents.",
    )
    parser.add_argument("--version", action="version", version=f"bbmask-bench {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_corpus = sub.add_parser("corpus", help="describe the corpus (no API calls)")
    _add_selection_args(p_corpus)
    p_corpus.add_argument("--json", action="store_true", help="print statistics as JSON")
    p_corpus.set_defaults(func=cmd_corpus)

    p_run = sub.add_parser("run", help="send documents to the service (spends credits)")
    _add_selection_args(p_run)
    p_run.add_argument("--run-id", help="name of the run directory; reuse it to resume")
    p_run.add_argument("--runs-dir", default=DEFAULT_RUNS_DIR, help="where runs are stored")
    p_run.add_argument("--concurrency", type=int, default=1, help="parallel requests (default: 1)")
    p_run.add_argument("--retries", type=int, default=2, help="retries on 429/502/503 (default: 2)")
    p_run.add_argument(
        "--timeout", type=float, help="HTTP timeout in seconds (default: BBMASK_TIMEOUT or 150)"
    )
    p_run.add_argument(
        "--max-consecutive-failures",
        type=int,
        default=3,
        help="stop after this many connection failures in a row; 0 disables (default: 3)",
    )
    p_run.add_argument(
        "--pause",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="rest after each document, to go easy on a small server (default: 0)",
    )
    p_run.add_argument(
        "--max-credits", type=int, help="hard cap on anonymize calls in this invocation"
    )
    p_run.add_argument("--dry-run", action="store_true", help="show the plan, send nothing")
    p_run.add_argument(
        "--yes", "-y", action="store_true", help="do not ask before spending credits"
    )
    service = p_run.add_argument_group("service options (same names as bb_mask.anonymize)")
    service.add_argument("--placeholder-style", choices=["angle", "bracket_tr"], default="angle")
    service.add_argument("--keep-legal-form", action="store_true")
    service.add_argument("--health-data", action="store_true")
    service.add_argument("--llm-review", action="store_true")
    service.add_argument(
        "--persist",
        action="store_true",
        help="keep the mapping on the service during the call (deleted afterwards). "
        "Default: the service stores nothing.",
    )
    p_run.set_defaults(func=cmd_run)

    p_audit = sub.add_parser(
        "audit",
        help="ask a language model what is still readable in the masked text (spends tokens)",
    )
    p_audit.add_argument("run_id")
    p_audit.add_argument("--runs-dir", default=DEFAULT_RUNS_DIR)
    p_audit.add_argument(
        "--model", required=True, help="model name at the OpenAI-compatible endpoint"
    )
    p_audit.add_argument("--limit", type=int, help="audit at most this many documents")
    p_audit.add_argument("--concurrency", type=int, default=4, help="parallel requests")
    p_audit.add_argument(
        "--chunk-chars",
        type=int,
        default=8000,
        help="longer documents are sent in pieces of this size (default: 8000)",
    )
    p_audit.add_argument(
        "--reasoning-effort", help="passed to the model as reasoning_effort, if given"
    )
    p_audit.add_argument(
        "--calibrate",
        type=int,
        metavar="N",
        help="instead of auditing, test the model itself: unmask a few known values in N "
        "documents and count how many it reports",
    )
    p_audit.set_defaults(func=cmd_audit)

    p_evaluate = sub.add_parser("evaluate", help="compute metrics for a run (no API calls)")
    p_evaluate.add_argument("run_id")
    p_evaluate.add_argument("--runs-dir", default=DEFAULT_RUNS_DIR)
    p_evaluate.set_defaults(func=cmd_evaluate)

    p_report = sub.add_parser(
        "report", help="evaluate a run, write results and charts, print the report"
    )
    p_report.add_argument("run_id")
    p_report.add_argument("--runs-dir", default=DEFAULT_RUNS_DIR)
    p_report.add_argument("--out", help="output directory (default: results/<run_id>)")
    p_report.add_argument(
        "--lang", choices=["en", "tr"], default="en", help="language of the printed report"
    )
    p_report.add_argument(
        "--publish",
        action="store_true",
        help="instead of printing, write RESULTS.md and RESULTS.tr.md to --docs-dir and "
        "update the results block of the READMEs of this repository",
    )
    p_report.add_argument(
        "--docs-dir",
        default=f"docs/v{__version__}",
        help="where --publish writes the Markdown report (default: %(default)s)",
    )
    p_report.set_defaults(func=cmd_report)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted. Re-run with the same --run-id to resume.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
