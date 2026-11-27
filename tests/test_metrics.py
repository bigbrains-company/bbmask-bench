"""Metrics on hand-made documents where the right answer is known."""

from __future__ import annotations

from bbmask_bench import metrics
from bbmask_bench.textutil import (
    find_placeholders,
    placeholder_type,
    strip_placeholders,
    tr_casefold,
)

ORIGINAL = (
    "Davacı Ayşe Yılmaz (T.C. 10000000146), 34 XYZ 987 plakalı aracıyla geldi. "
    "AYŞE YILMAZ, 2918 sayılı Kanun'un 91. maddesi uyarınca 400.000,00 TL talep etti. "
    "(Yargıtay 17. HD, 2019/54321 E., 14.06.2017)"
)


def artifact(masked: str, state: dict, restored: str = ORIGINAL) -> dict:
    return {
        "placeholder_style": "angle",
        "masked_text": masked,
        "state": state,
        "restored_text": restored,
    }


def test_turkish_casefold():
    assert tr_casefold("IŞILDAK İNŞAAT") == "ışıldak inşaat"


def test_placeholder_forms_are_recognized():
    text = "<PERSON_0> <PERSON_2_U> <LOCATION_14_L> <PERSON_7_P1_U> <TR_NATIONAL_ID_3> <b> <A>"
    tags = find_placeholders(text, "angle")
    assert tags == [
        "<PERSON_0>",
        "<PERSON_2_U>",
        "<LOCATION_14_L>",
        "<PERSON_7_P1_U>",
        "<TR_NATIONAL_ID_3>",
    ]
    assert [placeholder_type(t) for t in tags] == [
        "PERSON",
        "PERSON",
        "LOCATION",
        "PERSON",
        "TR_NATIONAL_ID",
    ]
    assert "<" not in strip_placeholders(text, "angle").replace("<b>", "").replace("<A>", "")
    assert find_placeholders("[KİŞİ_1] ve [TC_KİMLİK_2]", "bracket_tr") == [
        "[KİŞİ_1]",
        "[TC_KİMLİK_2]",
    ]
    assert placeholder_type("[TC_KİMLİK_2]") == "TC_KİMLİK"


def test_fully_masked_document_has_no_residue():
    masked = (
        "Davacı <PERSON_0> (T.C. <TR_NATIONAL_ID_0>), <TR_PLATE_0> plakalı aracıyla geldi. "
        "<PERSON_0_U>, 2918 sayılı Kanun'un 91. maddesi uyarınca 400.000,00 TL talep etti. "
        "(Yargıtay 17. HD, <TR_CASE_NO_0> E., 14.06.2017)"
    )
    state = {
        "PERSON": {"Ayşe Yılmaz": "<PERSON_0>", "AYŞE YILMAZ": "<PERSON_0_U>"},
        "TR_NATIONAL_ID": {"10000000146": "<TR_NATIONAL_ID_0>"},
        "TR_PLATE": {"34 XYZ 987": "<TR_PLATE_0>"},
        "TR_CASE_NO": {"2019/54321": "<TR_CASE_NO_0>"},
    }
    result = metrics.evaluate_document(ORIGINAL, artifact(masked, state))

    assert result["roundtrip"]["exact"]
    assert result["placeholders"]["occurrences"] == 5
    assert result["placeholders"]["unresolvable"] == 0
    assert result["structured"]["TR_NATIONAL_ID"] == {
        "occurrences": 1,
        "residual": 0,
        "unique": 1,
        "unique_residual": 0,
    }
    assert result["structured"]["PLATE"]["residual"] == 0
    assert all(v["residual"] == 0 for v in result["known_entities"].values())
    assert all(v["variant_residual"] == 0 for v in result["known_entities"].values())
    # Statute, article, court, date and amount stay readable; the decision number is masked.
    context = result["context"]
    for name in ("statute_reference", "article_reference", "high_court", "date", "money_amount"):
        assert context[name]["retained"] == context[name]["original"] >= 1, name
    assert context["decision_number"] == {"original": 1, "retained": 0}
    assert 0 < result["masked_share"] < 0.5


def test_leaks_are_counted():
    # The national ID and the plate were left in; the upper-case name was missed.
    masked = (
        "Davacı <PERSON_0> (T.C. 10000000146), 34 XYZ 987 plakalı aracıyla geldi. "
        "AYŞE YILMAZ, 2918 sayılı Kanun'un 91. maddesi uyarınca 400.000,00 TL talep etti. "
        "(Yargıtay 17. HD, 2019/54321 E., 14.06.2017)"
    )
    state = {"PERSON": {"Ayşe Yılmaz": "<PERSON_0>"}}
    result = metrics.evaluate_document(ORIGINAL, artifact(masked, state))

    assert result["structured"]["TR_NATIONAL_ID"]["residual"] == 1
    assert result["structured"]["PLATE"]["residual"] == 1
    person = result["known_entities"]["PERSON"]
    assert (person["occurrences"], person["residual"]) == (1, 0)  # exact form: fully masked
    assert (person["variant_occurrences"], person["variant_residual"]) == (2, 1)  # AYŞE YILMAZ


def test_single_names_are_not_matched_case_insensitively():
    original = "Davacı Güven geldi. Tarafların güven ilişkisi zedelendi."
    masked = "Davacı <PERSON_0> geldi. Tarafların güven ilişkisi zedelendi."
    result = metrics.evaluate_document(
        original, artifact(masked, {"PERSON": {"Güven": "<PERSON_0>"}}, restored=original)
    )
    person = result["known_entities"]["PERSON"]
    assert (person["occurrences"], person["residual"]) == (1, 0)
    assert person["variant_values"] == 0  # "güven" the ordinary word is not a leak


def test_job_titles_and_very_short_values_are_skipped():
    original = "Av. Ali geldi. Av. bürosu kapalı."
    masked = "<JOB_TITLE_0>. <PERSON_0> geldi. Av. bürosu kapalı."
    state = {"JOB_TITLE": {"Av": "<JOB_TITLE_0>"}, "PERSON": {"Ali": "<PERSON_0>"}}
    result = metrics.evaluate_document(original, artifact(masked, state, restored=original))
    assert result["known_entities"] == {}


def test_roundtrip_reports_whitespace_only_differences_separately():
    assert metrics.roundtrip("a  b", "a b") == {
        "exact": False,
        "exact_ignoring_whitespace": True,
        "length_delta": -1,
    }
    assert not metrics.roundtrip("a b", "a c")["exact_ignoring_whitespace"]


def test_merge_counts():
    merged = metrics.merge_counts(
        [{"IBAN": {"occurrences": 2, "residual": 0}}, {"IBAN": {"occurrences": 1, "residual": 1}}]
    )
    assert merged == {"IBAN": {"occurrences": 3, "residual": 1, "documents": 2}}
