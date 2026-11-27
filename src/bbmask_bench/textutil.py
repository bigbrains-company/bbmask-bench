"""Small text helpers shared by the detectors and the metrics."""

from __future__ import annotations

import re

# Python's str.lower() maps "I" to "i" and "İ" to "i̇" (two code points). Turkish needs
# I→ı and İ→i. Translating first keeps the string length unchanged, so offsets stay valid.
_TR_LOWER = str.maketrans({"I": "ı", "İ": "i"})
_WHITESPACE = re.compile(r"\s+")

# The service writes a type, an index and optional form suffixes. Suffixes mark a variant of
# the same entity so that restore can reproduce it exactly, e.g. another letter case:
#   <PERSON_0>   <TR_NATIONAL_ID_3>   <PERSON_2_U>   <LOCATION_14_L>   <PERSON_7_P1_U>
PLACEHOLDER_PATTERNS: dict[str, re.Pattern[str]] = {
    "angle": re.compile(r"<[A-Z][A-Z_]*_\d+(?:_[A-Z]\d*)*>"),
    # [KİŞİ_1], [TC_KİMLİK_2]
    "bracket_tr": re.compile(r"\[[A-ZÇĞİÖŞÜ][A-ZÇĞİÖŞÜ_]*_\d+(?:_[A-ZÇĞİÖŞÜ]\d*)*\]"),
}
_PLACEHOLDER_TYPE = re.compile(r"^[<\[]([^\d<>\[\]]+?)_\d+")


def tr_casefold(text: str) -> str:
    """Case-insensitive key that is correct for Turkish dotted/dotless i."""
    return text.translate(_TR_LOWER).lower()


def squash_ws(text: str) -> str:
    """Collapse every whitespace run to one space and trim."""
    return _WHITESPACE.sub(" ", text).strip()


def find_placeholders(text: str, style: str) -> list[str]:
    return PLACEHOLDER_PATTERNS[style].findall(text)


def strip_placeholders(text: str, style: str) -> str:
    """Replace placeholders with a neutral separator so they cannot produce false matches."""
    return PLACEHOLDER_PATTERNS[style].sub(" \u2022 ", text)


def placeholder_type(tag: str) -> str:
    """`<PERSON_0>` → `PERSON`, `<PERSON_2_U>` → `PERSON`, `[TC_KİMLİK_1]` → `TC_KİMLİK`."""
    match = _PLACEHOLDER_TYPE.match(tag)
    return match.group(1) if match else tag
