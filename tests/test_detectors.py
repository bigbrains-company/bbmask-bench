"""Ground-truth detectors: they must accept real identifiers and reject look-alikes."""

from __future__ import annotations

from bbmask_bench import detectors

# 10000000146 is the well-known valid test number; the IBAN is the example from bbmask.co.
VALID_TCKN = "10000000146"
VALID_IBAN = "TR33 0006 1005 1978 6457 8413 26"


def test_tckn_checksum():
    assert detectors.is_valid_tckn(VALID_TCKN)
    assert not detectors.is_valid_tckn("10000000147")  # last check digit wrong
    assert not detectors.is_valid_tckn("00000000000")  # may not start with zero
    assert not detectors.is_valid_tckn("1000000014")  # too short


def test_tckn_is_found_only_as_a_whole_number():
    text = f"T.C. Kimlik No: {VALID_TCKN}, barkod 9{VALID_TCKN}9, sıra 12345678901"
    assert detectors.find_tckn(text) == [VALID_TCKN]


def test_iban_checksum_and_normalization():
    assert detectors.is_valid_iban(VALID_IBAN)
    assert not detectors.is_valid_iban("TR33 0006 1005 1978 6457 8413 27")
    solid = VALID_IBAN.replace(" ", "")
    assert detectors.find_iban(f"hesap {VALID_IBAN} ve {solid}") == [solid, solid]
    assert detectors.find_iban("TR33 0006 1005 1978 6457 8413 27") == []


def test_email():
    assert detectors.find_email("İletişim: Av.Ayse_Yilmaz@Example.com.tr.") == [
        "av.ayse_yilmaz@example.com.tr"
    ]
    assert detectors.find_email("kullanıcı@ ve @alan") == []


def test_phone_formats_normalize_to_ten_digits():
    text = "0532 123 45 67, +90 (532) 123 45 67, 05321234567"
    assert detectors.find_phone(text) == ["5321234567"] * 3


def test_phone_ignores_landlines_and_long_numbers():
    assert detectors.find_phone("0312 123 45 67") == []
    assert detectors.find_phone("905321234567890") == []


def test_plate_needs_the_word_plaka():
    assert detectors.find_plate("34 XYZ 987 plakalı araç ile 34ABC12 plakalı araç") == [
        "34XYZ987",
        "34ABC12",
    ]
    assert detectors.find_plate("ücret 10 TL 500 kuruş, 34 XYZ 987 numaralı dosya") == []
    assert detectors.find_plate("99 ABC 123 plakalı") == []  # 99 is not a province code


def test_identifiers_broken_by_a_line_wrap_are_still_found():
    # Text extracted from scans wraps in the middle of a value; the value is still readable.
    assert detectors.find_plate("idaresindeki 34 \nXY 987 plakalı aracı") == ["34XY987"]
    wrapped = VALID_IBAN.replace(" 1978", "\n1978")
    assert detectors.find_iban(wrapped) == [VALID_IBAN.replace(" ", "")]


def test_detect_all_counts_occurrences():
    found = detectors.detect_all(f"{VALID_TCKN} ... {VALID_TCKN}")
    assert found["TR_NATIONAL_ID"][VALID_TCKN] == 2
    assert not found["IBAN"]
