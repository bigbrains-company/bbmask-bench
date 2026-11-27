"""Per-document quality metrics, computed offline from a run's artifacts.

Nothing here calls the service, and nothing here emits personal data: every metric is a count.

Four questions are asked of each document:

1. **Round trip.** Does ``restore(anonymize(text))`` return the original text exactly?
2. **Provable identifiers.** Do checksum-valid national IDs, IBANs, phone numbers and e-mail
   addresses survive in the masked text? (:mod:`bbmask_bench.detectors`)
3. **Known-entity residue.** The service reports every value it masked. Does any of those
   values still appear in the masked text, verbatim or in another letter case?
4. **Context retention.** How much of the legal context an LLM needs (statutes, articles,
   courts, dates, amounts) is still readable after masking?
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable

from bbmask_bench import detectors
from bbmask_bench.textutil import (
    find_placeholders,
    placeholder_type,
    squash_ws,
    strip_placeholders,
    tr_casefold,
)

# Types whose values are generic words ("Av.", "Dr."): a leftover occurrence identifies no one.
NON_IDENTIFYING_TYPES = frozenset({"JOB_TITLE"})
# Shorter values are too ambiguous to search for ("Ali" inside another word, initials).
MIN_VALUE_LENGTH = 4

# What a model needs in order to reason about a legal text. Patterns run on case-folded text.
CONTEXT_PATTERNS: dict[str, re.Pattern[str]] = {
    # "2918 sayılı", "6098 sayılı"
    "statute_reference": re.compile(r"(?<!\d)\d{3,5}\s+sayılı"),
    # "91. maddesi", "m. 49", "md. 17/2"
    "article_reference": re.compile(
        r"(?<![\d/.])\d{1,4}(?:/\d{1,2})?\.\s?madde|\bm(?:d)?\.\s?\d{1,4}"
    ),
    # "Yargıtay", "Danıştay", "Anayasa Mahkemesi"
    "high_court": re.compile(r"yargıtay|danıştay|anayasa\s+mahkemesi|sayıştay"),
    # "2019/54321 E.", "2018/6543 K."
    "decision_number": re.compile(r"(?<![\d/])(?:19|20)\d{2}/\d{1,6}\s?(?:e\.|k\.|esas|karar)"),
    # "11/08/2026", "01.10.2026"
    "date": re.compile(r"(?<![\d/.])\d{1,2}[./]\d{1,2}[./](?:19|20)\d{2}(?![\d/])"),
    # "400.000,00 TL", "1.500 ₺"
    "money_amount": re.compile(
        r"(?<![\d.,])\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?\s?(?:tl\b|₺|türk\s+lirası)"
    ),
}


def _value_pattern(value: str, *, casefold: bool) -> re.Pattern[str] | None:
    """Regex for one masked value: whole-token match, any whitespace between its words."""
    tokens = squash_ws(tr_casefold(value) if casefold else value).split(" ")
    if not tokens or not tokens[0]:
        return None
    body = r"\s+".join(re.escape(token) for token in tokens)
    return re.compile(rf"(?<!\w){body}(?!\w)")


def _count(pattern: re.Pattern[str], text: str) -> int:
    return sum(1 for _ in pattern.finditer(text))


def _is_multiword_or_numeric(value: str) -> bool:
    return " " in value.strip() or any(ch.isdigit() for ch in value)


def roundtrip(original: str, restored: str) -> dict:
    exact = restored == original
    return {
        "exact": exact,
        # Whitespace-only differences are reported separately from content differences.
        "exact_ignoring_whitespace": exact or squash_ws(restored) == squash_ws(original),
        "length_delta": len(restored) - len(original),
    }


def placeholders(masked: str, state: dict, style: str) -> dict:
    tags = find_placeholders(masked, style)
    known = {tag for mapping in state.values() for tag in mapping.values()}
    in_text = set(tags)
    return {
        "occurrences": len(tags),
        "unique": len(in_text),
        "by_type": dict(Counter(placeholder_type(tag) for tag in tags)),
        # A placeholder the mapping cannot resolve would survive restore as a raw tag.
        # (The mapping always carries angle-style tags, so this is checked for that style only.)
        "unresolvable": len(in_text - known) if style == "angle" else None,
        "mapped_values": sum(len(mapping) for mapping in state.values()),
    }


def structured_identifiers(original: str, masked_clean: str) -> dict:
    """Checksum-valid identifiers in the original, and how many survive masking."""
    before = detectors.detect_all(original)
    after = detectors.detect_all(masked_clean)
    result = {}
    for name, found in before.items():
        if not found:
            continue
        survivors = {value: min(n, after[name][value]) for value, n in found.items()}
        result[name] = {
            "occurrences": sum(found.values()),
            "residual": sum(survivors.values()),
            "unique": len(found),
            "unique_residual": sum(1 for n in survivors.values() if n),
        }
    return result


def known_entity_residue(original: str, masked_clean: str, state: dict) -> dict:
    """Occurrences of values the service itself masked that are still readable.

    ``exact`` searches the value as written. ``variant`` additionally ignores letter case
    (Turkish rules) and is limited to multi-word or numeric values: a single name such as
    "Güven" or "Deniz" is also an ordinary word, and matching it case-insensitively would
    count ordinary words as leaks.
    """
    original_folded = tr_casefold(original)
    masked_folded = tr_casefold(masked_clean)
    result = {}
    for entity_type, mapping in state.items():
        if entity_type in NON_IDENTIFYING_TYPES:
            continue
        stats = Counter()
        for value in mapping:
            value = squash_ws(value)
            if len(value) < MIN_VALUE_LENGTH:
                continue
            exact = _value_pattern(value, casefold=False)
            if exact is None:
                continue
            stats["values"] += 1
            stats["occurrences"] += _count(exact, original)
            residual = _count(exact, masked_clean)
            stats["residual"] += residual
            stats["values_with_residual"] += bool(residual)

            if _is_multiword_or_numeric(value):
                variant = _value_pattern(value, casefold=True)
                stats["variant_values"] += 1
                stats["variant_occurrences"] += _count(variant, original_folded)
                variant_residual = _count(variant, masked_folded)
                stats["variant_residual"] += variant_residual
                stats["variant_values_with_residual"] += bool(variant_residual)
        if stats["values"]:
            result[entity_type] = {key: int(stats[key]) for key in _RESIDUE_KEYS}
    return result


_RESIDUE_KEYS = (
    "values",
    "occurrences",
    "residual",
    "values_with_residual",
    "variant_values",
    "variant_occurrences",
    "variant_residual",
    "variant_values_with_residual",
)


def _matches(pattern: re.Pattern[str], folded_text: str) -> Counter[str]:
    return Counter(squash_ws(match.group()) for match in pattern.finditer(folded_text))


def context_retention(original: str, masked_clean: str) -> dict:
    """Share of legal-context expressions that are still present after masking."""
    original_folded = tr_casefold(original)
    masked_folded = tr_casefold(masked_clean)
    result = {}
    for name, pattern in CONTEXT_PATTERNS.items():
        before = _matches(pattern, original_folded)
        if not before:
            continue
        after = _matches(pattern, masked_folded)
        result[name] = {
            "original": sum(before.values()),
            "retained": sum(min(n, after[expr]) for expr, n in before.items()),
        }
    return result


def evaluate_document(original: str, artifact: dict) -> dict:
    """All metrics for one document. The result contains counts only."""
    style = artifact.get("placeholder_style") or "angle"
    masked = artifact["masked_text"]
    state = artifact.get("state") or {}
    masked_clean = strip_placeholders(masked, style)
    readable = len(squash_ws(masked_clean.replace("\u2022", " ")))
    original_readable = len(squash_ws(original))
    return {
        "chars_original": len(original),
        "chars_masked": len(masked),
        # Share of the original text that was replaced by placeholders.
        "masked_share": round(1 - readable / original_readable, 6) if original_readable else 0.0,
        "roundtrip": roundtrip(original, artifact.get("restored_text") or ""),
        "placeholders": placeholders(masked, state, style),
        "structured": structured_identifiers(original, masked_clean),
        "known_entities": known_entity_residue(original, masked_clean, state),
        "context": context_retention(original, masked_clean),
    }


def merge_counts(rows: Iterable[dict]) -> dict:
    """Sum nested ``{name: {metric: int}}`` dictionaries across documents."""
    total: dict[str, Counter] = {}
    documents: Counter = Counter()
    for row in rows:
        for name, values in row.items():
            total.setdefault(name, Counter()).update(values)
            documents[name] += 1
    return {name: {**dict(values), "documents": documents[name]} for name, values in total.items()}
