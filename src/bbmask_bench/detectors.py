"""Independent detectors used as ground truth.

The benchmark has no hand-labelled gold set. Instead it relies on identifiers that can be
recognized from the text alone with very little room for doubt: a Turkish national ID carries
two check digits, an IBAN carries a mod-97 checksum, a licence plate is named as one by the
sentence around it. Finding such a value in the masked output is a leak, with no annotator
judgement involved.

Detectors return normalized values so that ``TR67 0001 ...`` and ``TR670001...`` compare equal.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable

# --- Turkish national ID (T.C. Kimlik No) ---------------------------------------------------

_TCKN = re.compile(r"(?<!\d)[1-9]\d{10}(?!\d)")


def is_valid_tckn(value: str) -> bool:
    if len(value) != 11 or not value.isdigit() or value[0] == "0":
        return False
    digits = [int(c) for c in value]
    odd = sum(digits[0:9:2])
    even = sum(digits[1:8:2])
    if (odd * 7 - even) % 10 != digits[9]:
        return False
    return sum(digits[:10]) % 10 == digits[10]


def find_tckn(text: str) -> list[str]:
    return [m.group() for m in _TCKN.finditer(text) if is_valid_tckn(m.group())]


# --- Turkish IBAN ---------------------------------------------------------------------------

# TR + 2 check digits + 22 digits, written solid or in groups separated by whitespace
# (a space or, in wrapped text, a line break).
_IBAN_TR = re.compile(r"(?<![A-Za-z0-9])TR\s?\d{2}(?:\s?\d){22}(?!\d)", re.IGNORECASE)


def is_valid_iban(value: str) -> bool:
    iban = re.sub(r"\s+", "", value).upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", iban):
        return False
    rearranged = iban[4:] + iban[:4]
    number = "".join(str(int(ch, 36)) for ch in rearranged)
    return int(number) % 97 == 1


def find_iban(text: str, *, validate: bool = True) -> list[str]:
    found = []
    for match in _IBAN_TR.finditer(text):
        iban = re.sub(r"\s+", "", match.group()).upper()
        if not validate or is_valid_iban(iban):
            found.append(iban)
    return found


# --- E-mail ---------------------------------------------------------------------------------

_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}(?![\w-])")


def find_email(text: str) -> list[str]:
    return [m.group().lower() for m in _EMAIL.finditer(text)]


# --- Turkish mobile phone -------------------------------------------------------------------

# 05xx xxx xx xx, +90 5xx ..., 0 (5xx) ... Mobile numbers only: they identify a person, and the
# 5xx prefix keeps the pattern from matching arbitrary ten-digit numbers.
_PHONE = re.compile(
    r"(?<![\d+])(?:\+?90[\s.-]?|0[\s.-]?)\(?5\d{2}\)?[\s.-]?\d{3}[\s.-]?\d{2}[\s.-]?\d{2}(?!\d)"
)


def find_phone(text: str) -> list[str]:
    found = []
    for match in _PHONE.finditer(text):
        digits = re.sub(r"\D", "", match.group())
        found.append(digits[-10:])
    return found


# --- Turkish licence plate ------------------------------------------------------------------

# Province code (01-81), 1-3 letters, 2-4 digits. The format alone is too easy to match by
# accident ("10 TL 500"), so a plate only counts when the text itself calls it one:
# "34 XYZ 987 plakalı araç".
# Whitespace between the parts may be a line break: text extracted from scans wraps anywhere.
_PLATE = re.compile(
    r"(?<![\w/.-])(?:0[1-9]|[1-7]\d|8[01])\s{0,2}[A-Z]{1,3}\s{0,2}\d{2,4}"
    r"(?=\s+(?:sayılı\s+)?plaka)"
)


def find_plate(text: str) -> list[str]:
    return [re.sub(r"\s+", "", m.group()) for m in _PLATE.finditer(text)]


# --- Registry -------------------------------------------------------------------------------

Detector = Callable[[str], list[str]]

# Order is the order used in reports.
STRUCTURED_DETECTORS: dict[str, Detector] = {
    "TR_NATIONAL_ID": find_tckn,
    "IBAN": find_iban,
    "EMAIL": find_email,
    "PHONE": find_phone,
    "PLATE": find_plate,
}

# How each type is validated; shown in the report so readers can judge the evidence.
EVIDENCE = {
    "TR_NATIONAL_ID": "11 digits, both check digits valid",
    "IBAN": "TR IBAN, ISO 13616 mod-97 valid",
    "EMAIL": "address syntax",
    "PHONE": "Turkish mobile number format (5xx)",
    "PLATE": "plate format directly followed by the word “plaka”",
}


def detect_all(text: str) -> dict[str, Counter[str]]:
    """Occurrence count per normalized value, for every structured type."""
    return {name: Counter(detector(text)) for name, detector in STRUCTURED_DETECTORS.items()}
