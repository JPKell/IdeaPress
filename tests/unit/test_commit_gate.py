"""The commit gate: every reason it refuses, and the one thing it will not accept as a reason.

Risk T1 is a model's opinion ending a gate. `decide_commit` takes a validation report and a
coverage report — both computed by Python — and takes **no** argument through which a model could
speak. That is the mechanism, and it is asserted here on the signature as well as on the behaviour.
"""

from __future__ import annotations

import inspect

from ideapress.domain.commit import (
    CoverageEntry,
    CoverageReport,
    content_hash,
    decide_commit,
    evaluate_coverage,
    word_count,
)
from ideapress.domain.requirements import (
    CompiledBy,
    Requirement,
    RequirementCheck,
    SourceReference,
)
from ideapress.domain.validation import ValidationOutcome, ValidationReport

COMPILED_BY = CompiledBy(prompt_id="stages.requirements.compile", version="1.0.0")
TEXT = "Everything runs on your own machine and nothing is uploaded."


def _requirement(key: str = "R-001", *, blocking: bool = True, checked: bool = True) -> Requirement:
    return Requirement(
        key=key,
        text="The unit must say inference runs on the reader's own machine.",
        blocking=blocking,
        source=SourceReference(document="brief", quote="a quotation long enough to be evidence"),
        compiled_by=COMPILED_BY,
        checks=(
            (RequirementCheck(kind="must_contain_any", values=("own machine",)),) if checked else ()
        ),
    )


def _report(*outcomes: ValidationOutcome) -> ValidationReport:
    return ValidationReport(outcomes=outcomes)


def _failure(*, blocking: bool = True) -> ValidationOutcome:
    return ValidationOutcome(
        check_kind="structural",
        check_key="complete_ending",
        passed=False,
        blocking=blocking,
        detail="the last line stops mid-sentence",
    )


def _clean_coverage() -> CoverageReport:
    return evaluate_coverage(TEXT, [_requirement()])


def test_a_clean_unit_commits() -> None:
    decision = decide_commit(text=TEXT, validation=_report(), coverage=_clean_coverage())
    assert decision.allowed
    assert decision.reasons == ()


def test_an_empty_unit_never_commits() -> None:
    decision = decide_commit(text="   ", validation=_report(), coverage=_clean_coverage())
    assert not decision.allowed
    assert "empty" in decision.refusal


def test_a_blocking_validation_failure_refuses_and_names_the_check() -> None:
    decision = decide_commit(text=TEXT, validation=_report(_failure()), coverage=_clean_coverage())
    assert not decision.allowed
    assert "complete_ending" in decision.refusal


def test_an_advisory_validation_failure_does_not_refuse() -> None:
    decision = decide_commit(
        text=TEXT, validation=_report(_failure(blocking=False)), coverage=_clean_coverage()
    )
    assert decision.allowed


def test_an_unmet_blocking_requirement_refuses_and_names_it() -> None:
    coverage = evaluate_coverage("Nothing about where it runs.", [_requirement()])
    decision = decide_commit(
        text="Nothing about where it runs.", validation=_report(), coverage=coverage
    )
    assert not decision.allowed
    assert "R-001" in decision.refusal


def test_an_unmet_advisory_requirement_does_not_refuse() -> None:
    coverage = evaluate_coverage("Nothing about it.", [_requirement(blocking=False)])
    decision = decide_commit(text="Nothing about it.", validation=_report(), coverage=coverage)
    assert decision.allowed


def test_relaxing_validation_cannot_relax_the_requirement_bar() -> None:
    """A project may lower a bar it set itself; it cannot lower one the author's material set."""
    coverage = evaluate_coverage("Nothing about where it runs.", [_requirement()])
    decision = decide_commit(
        text="Nothing about where it runs.",
        validation=_report(_failure()),
        coverage=coverage,
        require_clean_validation=False,
    )
    assert not decision.allowed
    assert "R-001" in decision.refusal
    assert "complete_ending" not in decision.refusal, "validation was waived, coverage was not"


def test_every_refusal_is_reported_not_just_the_first() -> None:
    coverage = evaluate_coverage("", [_requirement(), _requirement("R-002")])
    decision = decide_commit(text="", validation=_report(_failure()), coverage=coverage)
    assert len(decision.reasons) == 3
    assert "R-001" in decision.refusal
    assert "R-002" in decision.refusal


def test_the_gate_has_no_parameter_a_model_could_speak_through() -> None:
    """T1, asserted on the signature.

    A `critique_verdict`, an `audit_says_ok` or a `model_confidence` parameter is how a gate that
    Python owns becomes one a model can satisfy. There is none, and adding one would fail here
    before it failed in a review. The two extras are **configuration**, set by the user in
    `config.toml` and threaded by Python — `audit_gating_allowed` (ADR-0039) only changes what
    the refusal says about an unmet check-less blocking requirement, never whether a model's
    output can satisfy one; growing this set with anything model-derived is still the failure
    this test exists to catch.
    """
    parameters = set(inspect.signature(decide_commit).parameters)
    assert parameters == {
        "text",
        "validation",
        "coverage",
        "require_clean_validation",
        "audit_gating_allowed",
    }


def test_a_model_assisted_requirement_needs_an_audit_and_never_a_claim() -> None:
    """A requirement with no deterministic check is satisfied by audit — and only by audit."""
    unchecked = _requirement("R-003", checked=False)
    without = evaluate_coverage(TEXT, [unchecked])
    assert not without.satisfied
    assert without.entries[0].satisfied_by == "unsatisfied"

    with_audit = evaluate_coverage(TEXT, [unchecked], audit_satisfied=("R-003",))
    assert with_audit.satisfied
    assert with_audit.entries[0].satisfied_by == "audit"
    assert not with_audit.entries[0].is_mechanical
    assert with_audit.model_assisted


def test_an_audit_cannot_overturn_a_deterministic_check() -> None:
    """The asymmetry that is the whole of T1: a model fills a gap, and never overrules Python."""
    coverage = evaluate_coverage(
        "Nothing about where it runs.", [_requirement()], audit_satisfied=("R-001",)
    )
    assert not coverage.satisfied, "the check said no; a model saying yes changes nothing"
    assert coverage.entries[0].satisfied_by == "unsatisfied"


def test_the_coverage_summary_reports_how_much_is_mechanical() -> None:
    coverage = evaluate_coverage(
        TEXT, [_requirement(), _requirement("R-002", checked=False)], audit_satisfied=("R-002",)
    )
    assert "2/2 requirements satisfied" in coverage.summary()
    assert "1 by a deterministic check" in coverage.summary()


def test_the_content_hash_is_prefixed_and_stable() -> None:
    assert content_hash("abc").startswith("sha256:")
    assert content_hash("abc") == content_hash("abc")
    assert content_hash("abc") != content_hash("abd")


def test_word_count_matches_the_length_validator() -> None:
    from ideapress.domain.validation import ValidationContext
    from ideapress.domain.validators.length import LengthValidator

    text = "don't split hyphen-words when counting"
    outcome = next(
        o
        for o in LengthValidator().check(ValidationContext(text=text))
        if o.check_key == "minimum_words"
    )
    assert outcome.evidence == (str(word_count(text)),)


def test_a_coverage_entry_reports_what_decided_it() -> None:
    entry = CoverageEntry(
        requirement=_requirement(), satisfied=True, satisfied_by="manual", detail="a person said so"
    )
    assert not entry.is_mechanical
    assert CoverageReport(entries=(entry,)).satisfied
