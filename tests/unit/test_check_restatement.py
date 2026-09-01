"""ADR-0042: a deterministic check may not be a restatement of its requirement.

The gate's asymmetry is that Python settles a requirement with a check and a model cannot overturn
it. That is right, and this suite does not question it. What it holds is the precondition: a check
whose needle is lifted from the requirement's own sentence is satisfied by **quoting** the
requirement, so it reports `deterministic_check` — a stronger claim than the audit makes — while
guaranteeing nothing.

Observed on a real brief. R-006 read *"Every claim must be grounded in usage figures, named
programme types, and the specific services that have no other local provider"* and compiled to
`must_contain_any` over those phrases. A unit satisfied it by repeating them three times while its
own critique said *"fails the blocking requirement R-006"* — and committed.
"""

from __future__ import annotations

import pytest

from ideapress.domain.requirements import RequirementCheck
from ideapress.services.requirements import (  # noqa: PLC2701 — the units under test
    _build_checks,
    restates_its_requirement,
)

R006 = (
    "Every claim must be grounded in usage figures, named programme types, and the specific "
    "services that have no other local provider."
)


# ------------------------------------------------------------------ the predicate


@pytest.mark.parametrize(
    "needle",
    [
        "usage figures",
        "named programme types",
        "the specific services that have no other local provider",
        "USAGE FIGURES",  # case
        "usage   figures",  # collapsed whitespace
    ],
)
def test_a_needle_lifted_from_the_requirement_is_a_restatement(needle: str) -> None:
    check = RequirementCheck(kind="must_contain_any", values=(needle,))
    assert restates_its_requirement(check, R006) is True


@pytest.mark.parametrize(
    "needle",
    ["2023", "Toddler Storytime", "footfall", "Table 1"],
)
def test_a_needle_independent_of_the_requirement_survives(needle: str) -> None:
    """The other half. A check the requirement does not contain is a real check and is kept."""
    check = RequirementCheck(kind="must_contain_any", values=(needle,))
    assert restates_its_requirement(check, R006) is False


def test_a_negative_check_drawn_from_the_wording_is_the_same_error() -> None:
    """`must_not_contain: 'advocacy copy'` for *"must not write advocacy copy"* forbids a phrase,
    not a manner of writing, and passes for any advocacy that avoids naming itself."""
    check = RequirementCheck(kind="must_not_contain", values=("advocacy copy",))
    assert (
        restates_its_requirement(check, "The finished work must not write advocacy copy.") is True
    )


def test_a_word_count_check_is_never_a_restatement() -> None:
    """Numeric kinds carry no needle, so the rule cannot apply to them."""
    check = RequirementCheck(kind="min_words", threshold=200)
    assert restates_its_requirement(check, "The section must be at least 200 words.") is False


# ------------------------------------------------------------------ the compiler drops it


def test_the_compiler_drops_a_restating_check() -> None:
    raw = [{"kind": "must_contain_any", "values": ["usage figures", "named programme types"]}]
    assert _build_checks(raw, requirement_text=R006) == []


def test_the_compiler_keeps_an_independent_check_on_the_same_requirement() -> None:
    raw = [{"kind": "must_contain_any", "values": ["Toddler Storytime"]}]
    kept = _build_checks(raw, requirement_text=R006)
    assert [c.values for c in kept] == [("Toddler Storytime",)]


def test_one_lifted_needle_condemns_the_whole_check() -> None:
    """`must_contain_any` passes if *any* needle matches, so one quotable phrase is enough to let a
    unit satisfy the check by recitation. The check goes."""
    raw = [{"kind": "must_contain_any", "values": ["Toddler Storytime", "usage figures"]}]
    assert _build_checks(raw, requirement_text=R006) == []


def test_a_requirement_left_with_no_check_is_check_less_not_broken() -> None:
    """ADR-0042 §2: it routes to the audit under ADR-0039 rather than failing compilation."""
    raw = [{"kind": "must_contain_any", "values": ["usage figures"]}]
    assert _build_checks(raw, requirement_text=R006) == []  # no exception


def test_malformed_checks_are_still_dropped_the_way_they_were() -> None:
    """The pre-existing behaviour is unchanged: an unknown kind is dropped, not raised."""
    raw = [{"kind": "not_a_kind", "values": ["x"]}, {"kind": "min_words", "threshold": 10}]
    kept = _build_checks(raw, requirement_text="anything")
    assert [c.kind for c in kept] == ["min_words"]


def test_checks_are_unaffected_when_no_requirement_text_is_supplied() -> None:
    """The parameter defaults to empty so existing callers keep working; an empty requirement
    cannot contain any needle, so nothing is dropped by the new rule."""
    raw = [{"kind": "must_contain_any", "values": ["usage figures"]}]
    assert len(_build_checks(raw)) == 1
