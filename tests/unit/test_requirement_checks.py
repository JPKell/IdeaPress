"""Every deterministic check kind: pass, fail, boundary, malformed input, unicode.

These are what the coverage gate actually evaluates, so "the gate is deterministic" means exactly
as much as these tests do. No model is involved anywhere here — that is the point.
"""

from __future__ import annotations

import pytest
from baseaicore import ValidationError

from ideapress.domain.requirements import (
    CHECK_KINDS,
    CompiledBy,
    Requirement,
    RequirementCheck,
    SourceReference,
    evaluate_check,
    evaluate_requirement,
    normalise_for_matching,
)

COMPILED_BY = CompiledBy(prompt_id="stages.requirements.compile", version="1.0.0")


def _check(kind: str, **kwargs: object) -> RequirementCheck:
    return RequirementCheck(kind=kind, **kwargs)  # type: ignore[arg-type]  # kind under test


def test_every_declared_kind_is_evaluable() -> None:
    """A kind the constructor accepts and the evaluator cannot run is a gate that never fires."""
    samples = {
        "must_contain_any": _check("must_contain_any", values=("x",)),
        "must_contain_all": _check("must_contain_all", values=("x",)),
        "must_not_contain": _check("must_not_contain", values=("x",)),
        "min_words": _check("min_words", threshold=1),
        "max_words": _check("max_words", threshold=10),
        "heading_present": _check("heading_present", values=("x",)),
    }
    assert set(samples) == CHECK_KINDS
    for check in samples.values():
        outcome = evaluate_check(check, "# x\n\nx here")
        assert isinstance(outcome.passed, bool)
        assert outcome.detail


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Inference runs locally on your machine.", True),
        ("Inference runs ON-DEVICE.", True),
        ("Everything is uploaded to our servers.", False),
        ("", False),
    ],
)
def test_must_contain_any(text: str, expected: bool) -> None:
    check = _check("must_contain_any", values=("locally", "on-device"))
    assert evaluate_check(check, text).passed is expected


def test_must_contain_any_reports_what_it_found() -> None:
    outcome = evaluate_check(
        _check("must_contain_any", values=("locally", "on-device")), "runs locally"
    )
    assert outcome.evidence == ("locally",)
    assert "locally" in outcome.detail


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("It is local and private.", True),
        ("It is local.", False),
        ("It is private.", False),
    ],
)
def test_must_contain_all(text: str, expected: bool) -> None:
    assert (
        evaluate_check(_check("must_contain_all", values=("local", "private")), text).passed
        is expected
    )


def test_must_contain_all_names_what_is_missing() -> None:
    outcome = evaluate_check(_check("must_contain_all", values=("local", "private")), "local only")
    assert outcome.evidence == ("private",)
    assert "missing" in outcome.detail


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Nothing leaves your machine.", True),
        ("We upload your documents.", False),
        ("UPLOAD in capitals still counts.", False),
    ],
)
def test_must_not_contain(text: str, expected: bool) -> None:
    assert evaluate_check(_check("must_not_contain", values=("upload",)), text).passed is expected


@pytest.mark.parametrize(
    ("text", "threshold", "expected"),
    [
        ("one two three", 3, True),
        ("one two three", 4, False),
        ("one two three", 2, True),
        ("", 0, True),
        ("", 1, False),
        ("don't hyphen-word count", 3, True),
    ],
)
def test_min_words_including_its_boundary(text: str, threshold: int, expected: bool) -> None:
    assert evaluate_check(_check("min_words", threshold=threshold), text).passed is expected


@pytest.mark.parametrize(
    ("text", "threshold", "expected"),
    [("one two three", 3, True), ("one two three", 2, False), ("", 0, True)],
)
def test_max_words_including_its_boundary(text: str, threshold: int, expected: bool) -> None:
    assert evaluate_check(_check("max_words", threshold=threshold), text).passed is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("# Privacy\n\nbody", True),
        ("## What about privacy?\n\nbody", True),
        ("Privacy is mentioned but not as a heading.", False),
        ("#Privacy\n", True),
        ("", False),
    ],
)
def test_heading_present(text: str, expected: bool) -> None:
    assert evaluate_check(_check("heading_present", values=("privacy",)), text).passed is expected


def test_checks_are_case_and_whitespace_insensitive_without_being_loose() -> None:
    """Folding must let honest text match; it must never make absent text match."""
    check = _check("must_contain_any", values=("own machine",))
    assert evaluate_check(check, "your OWN    machine").passed
    assert evaluate_check(check, "your own\nmachine").passed
    assert not evaluate_check(check, "machine own").passed, "word order is never reordered"


def test_unicode_is_normalised_but_not_stripped() -> None:
    assert evaluate_check(_check("must_contain_any", values=("café",)), "a CAFÉ here").passed
    # NFKC folds the composed and decomposed forms together.
    assert evaluate_check(_check("must_contain_any", values=("café",)), "café").passed
    assert not evaluate_check(_check("must_contain_any", values=("café",)), "cafe").passed


def test_a_non_breaking_space_does_not_read_as_different_text() -> None:
    assert evaluate_check(_check("must_contain_any", values=("own machine",)), "own machine").passed


def test_normalise_never_reorders_or_removes_words() -> None:
    assert normalise_for_matching("  A  B \n C ") == "a b c"
    assert normalise_for_matching("a-b") == "a-b"


def test_a_zero_threshold_is_legal_a_negative_one_is_not() -> None:
    assert evaluate_check(_check("min_words", threshold=0), "").passed
    with pytest.raises(ValidationError):
        _check("min_words", threshold=-1)
    with pytest.raises(ValidationError):
        _check("min_words")


def test_an_overlong_or_blank_value_is_refused() -> None:
    with pytest.raises(ValidationError):
        _check("must_contain_any", values=("   ",))
    with pytest.raises(ValidationError):
        _check("must_contain_any", values=("x" * 201,))
    with pytest.raises(ValidationError) as caught:
        _check("must_contain_any", values=tuple(str(n) for n in range(33)))
    assert "at most" in caught.value.message


def test_describe_reads_as_a_line_a_person_can_check() -> None:
    assert "at least 300 words" in _check("min_words", threshold=300).describe()
    assert "contains any of" in _check("must_contain_any", values=("a",)).describe()
    assert "contains none of" in _check("must_not_contain", values=("a",)).describe()
    assert "heading" in _check("heading_present", values=("a",)).describe()


def test_evaluating_a_requirement_runs_all_its_checks() -> None:
    requirement = Requirement(
        key="R-001",
        text="Says where inference runs, briefly.",
        blocking=True,
        source=SourceReference(document="brief", quote="a quotation long enough to be evidence"),
        compiled_by=COMPILED_BY,
        checks=(
            _check("must_contain_any", values=("locally",)),
            _check("max_words", threshold=5),
        ),
    )
    outcomes = evaluate_requirement(requirement, "It runs locally on your machine, always.")
    assert [outcome.passed for outcome in outcomes] == [True, False]


def test_a_requirement_with_no_checks_is_flagged_as_model_assisted() -> None:
    requirement = Requirement(
        key="R-002",
        text="The article must be engaging.",
        blocking=False,
        source=SourceReference(document="brief", quote="a quotation long enough to be evidence"),
        compiled_by=COMPILED_BY,
    )
    assert not requirement.is_mechanically_checkable
    assert evaluate_requirement(requirement, "anything") == ()
    assert "no deterministic check" in requirement.describe_checks()


def test_a_check_value_at_the_length_limit_is_accepted() -> None:
    """The boundary itself, so the refusal is off-by-one in neither direction."""
    at_limit = _check("must_contain_any", values=("x" * 200,))
    assert at_limit.values == ("x" * 200,)
    assert len(_check("must_contain_any", values=tuple(str(n) for n in range(32))).values) == 32
