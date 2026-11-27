"""Turns a run into publishable results.

``evaluate``   artifacts → ``runs/<id>/metrics.jsonl`` (counts only)
``summarize``  records + metrics → one summary dictionary
``publish``    summary → ``results/<name>/`` (JSON, per-document table, SVG charts) and the
               results block of the READMEs

Every number shown in a README comes from ``summary.json``; nothing is typed by hand.
Published files contain no text from the documents, no file names, no request ids and no
account balance.
"""

from __future__ import annotations

import itertools
import json
import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from bbmask_bench import __version__, audit, charts, metrics
from bbmask_bench.detectors import EVIDENCE
from bbmask_bench.i18n import AUDIT_LABELS, CONTEXT_LABELS, EVIDENCE_TR, PRICING_RULE, TEXT
from bbmask_bench.runner import RunStore, estimate_cost, parse_balance

SIZE_BANDS: tuple[tuple[int, str], ...] = (
    (5_000, "< 5k"),
    (20_000, "5k – 20k"),
    (60_000, "20k – 60k"),
    (10**12, "≥ 60k"),
)
RESULTS_START = "<!-- BENCH:RESULTS:START -->"
RESULTS_END = "<!-- BENCH:RESULTS:END -->"


# --- evaluate -------------------------------------------------------------------------------


def evaluate(store: RunStore) -> list[dict]:
    """Compute metrics for every successful document and write ``metrics.jsonl``."""
    rows = []
    for record in sorted(store.read_records(), key=lambda r: r["doc_id"]):
        if record["status"] != "ok":
            continue
        artifact = store.read_artifact(record["doc_id"])
        if not artifact or artifact.get("restored_text") is None:
            continue
        original = Path(artifact["source_path"]).read_text(encoding="utf-8", errors="replace")
        rows.append({"doc_id": record["doc_id"], **metrics.evaluate_document(original, artifact)})
    path = store.run_dir / "metrics.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return rows


def read_metrics(store: RunStore) -> list[dict]:
    path = store.run_dir / "metrics.jsonl"
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


# --- summarize ------------------------------------------------------------------------------


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile of an unsorted sequence."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[rank - 1]


def _distribution(values: Sequence[float], digits: int = 2) -> dict:
    if not values:
        return {}
    return {
        "mean": round(statistics.fmean(values), digits),
        "p50": round(percentile(values, 0.50), digits),
        "p90": round(percentile(values, 0.90), digits),
        "p95": round(percentile(values, 0.95), digits),
        "p99": round(percentile(values, 0.99), digits),
        "max": round(max(values), digits),
    }


def _band(n_chars: int) -> str:
    for limit, name in SIZE_BANDS:
        if n_chars < limit:
            return name
    return SIZE_BANDS[-1][1]


def _rate(part: int, whole: int) -> float | None:
    """Share masked/retained. Rounded down so a single leak never shows as 100%."""
    if not whole:
        return None
    return math.floor(part / whole * 1_000_000) / 1_000_000


def _cost(ok_records: list[dict], sequential: bool) -> dict:
    by_rule = estimate_cost([r["n_chars"] for r in ok_records])
    chars = sum(r["n_chars"] for r in ok_records)
    cost: dict = {
        "pricing_rule": "0.01 credits per started 1,000 characters (published at /health)",
        "total_by_rule": str(by_rule),
        "per_document_mean": round(float(by_rule) / len(ok_records), 4) if ok_records else None,
        "per_million_characters": round(float(by_rule) / chars * 1e6, 2) if chars else None,
    }
    if sequential:
        # In a sequential run the balance returned by call i minus the balance returned by
        # call i+1 is what call i+1 cost. Pairs separated by a pause are skipped.
        ordered = sorted(ok_records, key=lambda r: r["finished_at"])
        checked = matched = 0
        for previous, current in itertools.pairwise(ordered):
            before, after = (
                parse_balance(previous["balance_after"]),
                parse_balance(current["balance_after"]),
            )
            if before is None or after is None:
                continue
            gap = (current.get("anonymize_ms") or 0) / 1000 + 30
            if _seconds_between(previous["finished_at"], current["finished_at"]) > gap:
                continue
            checked += 1
            matched += (before - after) == estimate_cost([current["n_chars"]])
        cost["rule_checked_calls"] = checked
        cost["rule_matched_calls"] = matched
    return cost


def _seconds_between(start: str, end: str) -> float:
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()


def _sum_nested(rows: Iterable[dict], key: str) -> dict:
    return metrics.merge_counts(row[key] for row in rows)


def _audit_summary(
    audit_rows: list[dict], by_doc: dict[str, dict], calibration: dict | None
) -> dict | None:
    """Findings of the language model next to what the service masked, per kind of data."""
    rows = [row for row in audit_rows if row["doc_id"] in by_doc]
    if not rows:
        return None
    usage: Counter = Counter()
    by_type = {name: Counter() for name in audit.AUDIT_TYPES}
    for row in rows:
        usage.update(row["usage"])
        for name in audit.AUDIT_TYPES:
            stats = by_type[name]
            stats["masked"] += row["masked"].get(name, 0)
            found = row["by_type"].get(name)
            if found:
                stats["left"] += found["occurrences"]
                stats["left_values"] += found["values"]
                stats["never_masked"] += found["never_masked_occurrences"]
                stats["never_masked_values"] += found["never_masked_values"]
                stats["documents"] += 1
    keys = ("masked", "left", "left_values", "never_masked", "never_masked_values", "documents")
    return {
        "model": Counter(row["model"] for row in rows).most_common(1)[0][0],
        "prompt_version": rows[0]["prompt_version"],
        "documents": len(rows),
        "documents_with_findings": sum(1 for row in rows if row["by_type"]),
        "requests": sum(row["chunks"] for row in rows),
        "reported": sum(row["reported"] for row in rows),
        "rejected": sum(row["rejected"] for row in rows),
        "usage": dict(usage),
        # How many leaks planted on purpose the model reported: the auditor's own recall.
        "calibration": calibration,
        "by_type": {
            name: {
                **{key: int(stats[key]) for key in keys},
                "service_types": list(audit.AUDIT_TYPES[name].service_types),
                # An estimate: share of the occurrences known to either side that were masked.
                # Kinds the service has no type for are listed without a rate.
                "rate": _rate(stats["masked"], stats["masked"] + stats["left"])
                if audit.AUDIT_TYPES[name].service_types
                else None,
            }
            for name, stats in by_type.items()
        },
    }


def summarize(
    records: list[dict],
    metric_rows: list[dict],
    manifest: dict,
    audit_rows: list[dict] | None = None,
    calibration: dict | None = None,
) -> dict:
    ok = [r for r in records if r["status"] == "ok"]
    failed = [r for r in records if r["status"] != "ok"]
    by_doc = {row["doc_id"]: row for row in metric_rows}
    evaluated = [r for r in ok if r["doc_id"] in by_doc]
    sequential = manifest.get("concurrency", 1) == 1

    anonymize_s = [r["anonymize_ms"] / 1000 for r in ok if r.get("anonymize_ms") is not None]
    restore_ms = [r["restore_ms"] for r in ok if r.get("restore_ms") is not None]
    chars = sum(r["n_chars"] for r in ok)

    bands = defaultdict(list)
    for r in ok:
        if r.get("anonymize_ms") is not None:
            bands[_band(r["n_chars"])].append(r["anonymize_ms"] / 1000)

    structured = _sum_nested(metric_rows, "structured")
    for name, values in structured.items():
        values["masked_rate"] = _rate(
            values["occurrences"] - values["residual"], values["occurrences"]
        )
        values["evidence"] = EVIDENCE.get(name, "")

    known = _sum_nested(metric_rows, "known_entities")
    for values in known.values():
        values["consistency"] = _rate(
            values["occurrences"] - values["residual"], values["occurrences"]
        )
        values["variant_consistency"] = _rate(
            values["variant_occurrences"] - values["variant_residual"],
            values["variant_occurrences"],
        )

    context = _sum_nested(metric_rows, "context")
    for values in context.values():
        values["retention"] = _rate(values["retained"], values["original"])

    placeholder_types: Counter = Counter()
    documents_with_type: Counter = Counter()
    for row in metric_rows:
        placeholder_types.update(row["placeholders"]["by_type"])
        documents_with_type.update(row["placeholders"]["by_type"].keys())

    exact = sum(row["roundtrip"]["exact"] for row in metric_rows)
    shares = [row["masked_share"] for row in metric_rows]
    leaking_docs = sum(
        1 for row in metric_rows if any(v["residual"] for v in row["structured"].values())
    )

    by_kind = {}
    for kind in sorted({r["kind"] for r in evaluated}):
        members = [r for r in evaluated if r["kind"] == kind]
        rows = [by_doc[r["doc_id"]] for r in members]
        kind_chars = sum(r["n_chars"] for r in members)
        placeholders_total = sum(row["placeholders"]["occurrences"] for row in rows)
        by_kind[kind] = {
            "documents": len(members),
            "characters": kind_chars,
            "roundtrip_exact": sum(row["roundtrip"]["exact"] for row in rows),
            "placeholders_per_1k_chars": round(placeholders_total / kind_chars * 1000, 2)
            if kind_chars
            else None,
            "anonymize_p50_s": round(
                percentile(
                    [
                        r["anonymize_ms"] / 1000
                        for r in members
                        if r.get("anonymize_ms") is not None
                    ],
                    0.5,
                ),
                2,
            ),
        }

    return {
        "meta": {
            "bench_version": __version__,
            "sdk_version": manifest.get("sdk_version"),
            "service": manifest.get("base_url"),
            "options": manifest.get("options"),
            "concurrency": manifest.get("concurrency"),
            "seed": manifest.get("seed"),
            "max_bytes": manifest.get("max_bytes"),
            "started_at": min((r["finished_at"] for r in records), default=None),
            "finished_at": max((r["finished_at"] for r in records), default=None),
        },
        "corpus": {
            "documents": len(records),
            "cases": len({r["case_id"] for r in records}),
            "characters": sum(r["n_chars"] for r in records),
            "by_kind": dict(Counter(r["kind"] for r in records)),
            "petitions_by_case_type": dict(
                Counter(
                    r.get("case_type") or "unknown" for r in records if r["kind"] == "petition"
                ).most_common()
            ),
            "attachments_by_source_format": dict(
                Counter(
                    r.get("source_format") or "unknown"
                    for r in records
                    if r["kind"] == "attachment"
                ).most_common()
            ),
        },
        "reliability": {
            "documents": len(records),
            "ok": len(ok),
            "failed": len(failed),
            "success_rate": _rate(len(ok), len(records)),
            "failed_by_status": dict(Counter(str(r.get("error_status")) for r in failed)),
            "failed_characters": _distribution([r["n_chars"] for r in failed], 0),
            "retried_calls": sum(1 for r in ok if (r.get("attempts") or 1) > 1),
        },
        "latency": {
            "anonymize_seconds": _distribution(anonymize_s),
            "restore_milliseconds": _distribution(restore_ms, 0),
            "characters_per_second": round(chars / sum(anonymize_s))
            if sum(anonymize_s) > 0
            else None,
            "anonymize_seconds_by_size": {
                name: {"documents": len(bands[name]), **_distribution(bands[name])}
                for _, name in SIZE_BANDS
                if bands[name]
            },
        },
        "cost": _cost(ok, sequential),
        "quality": {
            "documents_evaluated": len(metric_rows),
            "roundtrip": {
                "exact": exact,
                "exact_ignoring_whitespace": sum(
                    row["roundtrip"]["exact_ignoring_whitespace"] for row in metric_rows
                ),
                "rate": _rate(exact, len(metric_rows)),
            },
            "structured_identifiers": structured,
            "documents_with_identifier_leak": leaking_docs,
            "known_entities": known,
            "context_retention": context,
            "placeholders": {
                "occurrences": sum(placeholder_types.values()),
                "unresolvable": sum(
                    row["placeholders"]["unresolvable"] or 0 for row in metric_rows
                ),
                "by_type": dict(placeholder_types.most_common()),
                "documents_with_type": dict(documents_with_type.most_common()),
                "per_document_p50": percentile(
                    [row["placeholders"]["occurrences"] for row in metric_rows], 0.5
                ),
            },
            "masked_share": {
                "mean": round(statistics.fmean(shares), 4) if shares else None,
                "p50": round(percentile(shares, 0.5), 4) if shares else None,
            },
            "llm_audit": _audit_summary(audit_rows or [], by_doc, calibration),
        },
        "by_kind": by_kind,
    }


# --- publish --------------------------------------------------------------------------------

_PUBLIC_RECORD_FIELDS = (
    "doc_id",
    "case_id",
    "kind",
    "case_type",
    "petition_type",
    "source_format",
    "n_chars",
    "status",
    "error_status",
    "error_stage",
    "attempts",
    "anonymize_ms",
    "restore_ms",
    "entities_found",
)


def public_documents(
    records: list[dict], metric_rows: list[dict], audit_rows: list[dict] | None = None
) -> list[dict]:
    """Per-document table without request ids, balances, paths or error text."""
    by_doc = {row["doc_id"]: row for row in metric_rows}
    audits = {row["doc_id"]: row["by_type"] for row in audit_rows or []}
    table = []
    for record in sorted(records, key=lambda r: r["doc_id"]):
        row = {name: record.get(name) for name in _PUBLIC_RECORD_FIELDS}
        metric = by_doc.get(record["doc_id"])
        if metric:
            row["metrics"] = {k: v for k, v in metric.items() if k != "doc_id"}
        if record["doc_id"] in audits:
            row["llm_audit"] = audits[record["doc_id"]]
        table.append(row)
    return table


def publish(store: RunStore, out_dir: str | Path) -> dict:
    """Write ``summary.json``, ``documents.jsonl`` and the charts. Returns the summary."""
    records = store.read_records()
    metric_rows = read_metrics(store)
    audit_rows = audit.read_counts(store)
    summary = summarize(
        records, metric_rows, store.read_manifest(), audit_rows, audit.read_calibration(store)
    )

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with (out / "documents.jsonl").open("w", encoding="utf-8") as handle:
        for row in public_documents(records, metric_rows, audit_rows):
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    ok = [r for r in records if r["status"] == "ok" and r.get("anonymize_ms") is not None]
    (out / "latency.svg").write_text(
        charts.latency_scatter(
            [(r["n_chars"], r["anonymize_ms"] / 1000) for r in ok],
            summary["latency"]["characters_per_second"],
        ),
        encoding="utf-8",
    )
    (out / "entities.svg").write_text(
        charts.horizontal_bars(
            "Masked entities by type",
            list(summary["quality"]["placeholders"]["by_type"].items())[:14],
        ),
        encoding="utf-8",
    )
    return summary


# --- markdown -------------------------------------------------------------------------------


def _num(value: float, lang: str, digits: int = 0) -> str:
    text = f"{value:,.{digits}f}"
    if lang == "tr":
        text = text.replace(",", "\u00a0").replace(".", ",").replace("\u00a0", ".")
    return text


def _pct(rate: float | None, lang: str) -> str:
    if rate is None:
        return "–"
    body = "100" if rate == 1 else _num(math.floor(rate * 10_000) / 100, lang, 2)
    return f"%{body}" if lang == "tr" else f"{body}%"


ALL_SECTIONS = (
    "headline",
    "identifiers",
    "roundtrip",
    "consistency",
    "audit",
    "context",
    "latency",
    "reliability",
    "entities",
    "cost",
)
# The README leaves out the two longest tables; the full report lives in docs/.
README_SECTIONS = (
    "headline",
    "identifiers",
    "roundtrip",
    "consistency",
    "audit",
    "context",
    "latency",
    "reliability",
)


def render_results(
    summary: dict,
    lang: str = "en",
    results_path: str = "results/full",
    sections: Sequence[str] = ALL_SECTIONS,
) -> str:
    """The results as Markdown. ``results_path`` is relative to the file that embeds them."""
    t = TEXT[lang]
    quality = summary["quality"]
    latency = summary["latency"]
    reliability = summary["reliability"]
    meta = summary["meta"]

    def n(value: float, digits: int = 0) -> str:
        return _num(value, lang, digits)

    def evidence(name: str, values: dict) -> str:
        return EVIDENCE_TR.get(name, values["evidence"]) if lang == "tr" else values["evidence"]

    structured = quality["structured_identifiers"]
    occurrences = sum(v["occurrences"] for v in structured.values())
    residual = sum(v["residual"] for v in structured.values())
    options = ", ".join(f"{k}={v}" for k, v in (meta.get("options") or {}).items())
    concurrency = (
        t["sequential"]
        if (meta.get("concurrency") or 1) == 1
        else t["parallel"].format(n=meta["concurrency"])
    )

    out: dict[str, list[str]] = {}
    out["headline"] = lines = [
        f"<sub>{t['generated'].format(path=results_path)}</sub>",
        "",
        t["headline"],
        "|---:|---:|---:|---:|---:|",
        "| **{docs}** | **{chars}** | **{rt}** | **{ident}** | **{p50}** |".format(
            docs=n(quality["documents_evaluated"]),
            chars=n(sum(v["characters"] for v in summary["by_kind"].values())),
            rt=_pct(quality["roundtrip"]["rate"], lang),
            ident=_pct(_rate(occurrences - residual, occurrences), lang),
            p50=f"{n(latency['anonymize_seconds']['p50'], 1)} s",
        ),
        "",
        t["run"].format(
            docs=n(summary["corpus"]["documents"]),
            cases=n(summary["corpus"]["cases"]),
            chars=n(summary["corpus"]["characters"]),
            sdk=meta.get("sdk_version"),
            options=options,
            concurrency=concurrency,
        )
        + (t["size_cap"].format(kb=n(meta["max_bytes"] / 1000)) if meta.get("max_bytes") else ""),
        "",
    ]
    out["identifiers"] = lines = [
        f"#### {t['identifiers']}",
        "",
        t["identifiers_note"],
        "",
        t["identifiers_head"],
        "|---|---|---:|---:|---:|---:|",
    ]
    for name, v in structured.items():
        lines.append(
            f"| `{name}` | {evidence(name, v)} | {n(v['documents'])} | {n(v['occurrences'])} "
            f"| {n(v['residual'])} | **{_pct(v['masked_rate'], lang)}** |"
        )

    lines.append("")
    out["roundtrip"] = [
        f"#### {t['roundtrip']}",
        "",
        t["roundtrip_text"].format(
            exact=n(quality["roundtrip"]["exact"]),
            total=n(quality["documents_evaluated"]),
            rate=_pct(quality["roundtrip"]["rate"], lang),
            unresolvable=n(quality["placeholders"]["unresolvable"]),
        ),
        "",
    ]
    out["consistency"] = lines = [
        f"#### {t['consistency']}",
        "",
        t["consistency_note"],
        "",
        t["consistency_head"],
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, v in sorted(quality["known_entities"].items(), key=lambda kv: -kv[1]["occurrences"]):
        lines.append(
            f"| `{name}` | {n(v['values'])} | {n(v['occurrences'])} | {n(v['residual'])} "
            f"| **{_pct(v['consistency'], lang)}** | {n(v['variant_residual'])} |"
        )

    lines.append("")
    out["audit"] = lines = []
    llm = quality.get("llm_audit")
    if llm:
        lines += [
            f"#### {t['audit']}",
            "",
            t["audit_note"].format(
                model=llm["model"],
                docs=n(llm["documents"]),
                reported=n(llm["reported"]),
                rejected=n(llm["rejected"]),
            ),
            "",
            t["audit_head"],
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        for name, v in llm["by_type"].items():
            if not v["service_types"]:
                continue
            label_en, label_tr = AUDIT_LABELS[name]
            lines.append(
                f"| {label_tr if lang == 'tr' else label_en} | {n(v['masked'])} | {n(v['left'])} "
                f"| {n(v['never_masked'])} | {n(v['left'] - v['never_masked'])} "
                f"| {n(v['documents'])} | **{_pct(v['rate'], lang)}** |"
            )
        lines.append("")
        authors = llm["by_type"].get("CITED_AUTHOR")
        if authors and authors["left"]:
            lines += [
                t["audit_authors"].format(n=n(authors["left"]), docs=n(authors["documents"])),
                "",
            ]
        calibration = llm.get("calibration")
        if calibration and calibration["planted"]:
            lines += [
                t["audit_calibration"].format(
                    docs=n(calibration["documents"]),
                    planted=n(calibration["planted"]),
                    found=n(calibration["found"]),
                    rate=_pct(_rate(calibration["found"], calibration["planted"]), lang),
                ),
                "",
            ]
    out["context"] = lines = [
        f"#### {t['context']}",
        "",
        t["context_note"],
        "",
        t["context_head"],
        "|---|---|---:|---:|---:|",
    ]
    for name, (label_en, label_tr, example) in CONTEXT_LABELS.items():
        v = quality["context_retention"].get(name)
        if v:
            lines.append(
                f"| {label_tr if lang == 'tr' else label_en} | `{example}` | {n(v['original'])} "
                f"| {n(v['retained'])} | **{_pct(v['retention'], lang)}** |"
            )
    lines += [
        "",
        t["masked_share"].format(
            share=_pct(quality["masked_share"]["mean"], lang),
            median=_pct(quality["masked_share"]["p50"], lang),
        ),
        "",
    ]
    out["latency"] = lines = [
        f"#### {t['latency']}",
        "",
        t["latency_text"].format(
            p50=n(latency["anonymize_seconds"]["p50"], 1),
            p90=n(latency["anonymize_seconds"]["p90"], 1),
            p99=n(latency["anonymize_seconds"]["p99"], 1),
            cps=n(latency["characters_per_second"] or 0),
            r50=n(latency["restore_milliseconds"]["p50"]),
            r99=n(latency["restore_milliseconds"]["p99"]),
        ),
        "",
        t["latency_head"],
        "|---|---:|---:|---:|---:|---:|",
    ]
    for band, v in latency["anonymize_seconds_by_size"].items():
        lines.append(
            f"| {band} | {n(v['documents'])} | {n(v['p50'], 1)} s | {n(v['p90'], 1)} s "
            f"| {n(v['p99'], 1)} s | {n(v['max'], 1)} s |"
        )
    lines += ["", f"![Latency]({results_path}/latency.svg)", ""]

    detail = ""
    if reliability["failed"]:
        codes = ", ".join(
            f"HTTP {code}: {count}" for code, count in reliability["failed_by_status"].items()
        )
        detail = t["failed_detail"].format(
            codes=codes, size=n(reliability["failed_characters"].get("p50", 0))
        )
    out["reliability"] = [
        f"#### {t['reliability']}",
        "",
        t["reliability_text"].format(
            ok=n(reliability["ok"]),
            total=n(reliability["documents"]),
            rate=_pct(reliability["success_rate"], lang),
            failed=n(reliability["failed"]),
            detail=detail,
        ),
        "",
    ]
    out["entities"] = lines = [
        f"#### {t['entities']}",
        "",
        t["entities_text"].format(
            total=n(quality["placeholders"]["occurrences"]),
            docs=n(quality["documents_evaluated"]),
            median=n(quality["placeholders"]["per_document_p50"]),
        ),
        "",
        t["entities_head"],
        "|---|---:|---:|",
    ]
    documents_with_type = quality["placeholders"]["documents_with_type"]
    for name, count in quality["placeholders"]["by_type"].items():
        lines.append(f"| `{name}` | {n(count)} | {n(documents_with_type.get(name, 0))} |")
    lines += ["", f"![Entities]({results_path}/entities.svg)"]

    cost = summary["cost"]
    check = ""
    if cost.get("rule_checked_calls"):
        check = t["cost_check"].format(
            matched=n(cost["rule_matched_calls"]), checked=n(cost["rule_checked_calls"])
        )
    lines.append("")
    out["cost"] = [
        f"#### {t['cost']}",
        "",
        t["cost_text"].format(
            total=n(float(Decimal(cost["total_by_rule"])), 2),
            docs=n(reliability["ok"]),
            per_doc=n(cost["per_document_mean"], 3),
            per_m=n(cost["per_million_characters"], 2),
            rule=PRICING_RULE[lang],
            check=check,
        ),
        "",
    ]
    return "\n".join(line for name in sections for line in out[name]).rstrip("\n") + "\n"


def inject_results(readme: str | Path, block: str) -> bool:
    """Replace the text between the result markers. Returns False if the markers are missing."""
    path = Path(readme)
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    if RESULTS_START not in text or RESULTS_END not in text:
        return False
    head, rest = text.split(RESULTS_START, 1)
    _, tail = rest.split(RESULTS_END, 1)
    path.write_text(f"{head}{RESULTS_START}\n{block}{RESULTS_END}{tail}", encoding="utf-8")
    return True
