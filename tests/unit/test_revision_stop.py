"""Risk T2: revision must stop, and must say which rule stopped it.

Three stops, all Python's, plus the regression rule. The ordering matters and is asserted: the
round limit is checked **before** the critic is consulted, so a critic that always answers
"materially deficient" cannot extend the loop by even one round.
"""

from __future__ import annotations

from typing import Any

import pytest

from ideapress.domain.audit import (
    AuditFinding,
    AuditReport,
    Severity,
    finding_delta,
    weighted_score,
)
from ideapress.domain.critique import VERDICTS, Critique
from ideapress.domain.revision_policy import (
    RevisionDecision,
    RoundMeasurement,
    decide_revision,
    improvement,
    rejects_regression,
)


def _measure(round_number: int, validation: int, audit: int) -> RoundMeasurement:
    return RoundMeasurement(
        round_number=round_number, validation_failures=validation, audit_findings=audit
    )


def _decide(**kwargs: Any) -> RevisionDecision:
    base: dict[str, Any] = {
        "wants_revision": True,
        "round_number": 0,
        "max_rounds": 3,
        "before": None,
        "after": None,
        "threshold": 0.05,
    }
    base.update(kwargs)
    return decide_revision(**base)


def test_a_first_pass_that_wants_revision_revises() -> None:
    decision = _decide()
    assert decision.should_revise is True
    assert decision.stop_reason is None


def test_a_satisfied_critique_stops_and_says_so() -> None:
    decision = _decide(wants_revision=False)
    assert decision.should_revise is False
    assert decision.stop_reason == "critique_satisfied"


def test_the_round_limit_stops_it() -> None:
    decision = _decide(round_number=3, max_rounds=3)
    assert decision.should_revise is False
    assert decision.stop_reason == "round_limit"
    assert "3 of 3" in decision.detail


def test_a_critic_that_never_converges_cannot_extend_the_loop() -> None:
    """The whole of risk T2 in one test: an insistent critic runs exactly max_rounds and stops."""
    rounds = 0
    max_rounds = 3
    while True:
        decision = _decide(wants_revision=True, round_number=rounds, max_rounds=max_rounds)
        if not decision.should_revise:
            break
        rounds += 1
        assert rounds <= max_rounds + 1, "the loop did not terminate"
    assert rounds == max_rounds
    assert decision.stop_reason == "round_limit"


def test_the_round_limit_is_checked_before_the_critic() -> None:
    """Order matters: at the limit, an insistent critic gets `round_limit`, not another round."""
    decision = _decide(wants_revision=True, round_number=3, max_rounds=3)
    assert decision.stop_reason == "round_limit"


def test_diminishing_returns_stops_it_and_reports_the_numbers() -> None:
    decision = _decide(
        round_number=1, before=_measure(0, 10, 10), after=_measure(1, 10, 10), threshold=0.05
    )
    assert decision.should_revise is False
    assert decision.stop_reason == "diminishing_returns"
    assert "20 to 20" in decision.detail
    assert "+0%" in decision.detail


def test_real_improvement_keeps_going() -> None:
    decision = _decide(
        round_number=1, before=_measure(0, 10, 10), after=_measure(1, 4, 4), threshold=0.05
    )
    assert decision.should_revise is True


def test_improvement_at_exactly_the_threshold_continues() -> None:
    """The boundary: `< threshold` stops, so equal to it does not."""
    decision = _decide(
        round_number=1, before=_measure(0, 20, 0), after=_measure(1, 19, 0), threshold=0.05
    )
    assert decision.should_revise is True


def test_a_round_that_made_things_worse_reads_as_negative_improvement() -> None:
    assert improvement(_measure(0, 4, 4), _measure(1, 8, 8)) == -1.0
    decision = _decide(
        round_number=1, before=_measure(0, 4, 4), after=_measure(1, 8, 8), threshold=0.05
    )
    assert decision.stop_reason == "diminishing_returns"


def test_a_unit_that_started_clean_does_not_read_as_improving() -> None:
    """Zero of zero is not 100%: a clean unit must not keep the loop alive."""
    assert improvement(_measure(0, 0, 0), _measure(1, 0, 0)) == 0.0


def test_a_revision_that_raises_validation_failures_is_rejected() -> None:
    """P5's named failure mode: a critique that makes a unit worse being accepted."""
    assert rejects_regression(_measure(0, 2, 5), _measure(1, 3, 5)) is True
    assert rejects_regression(_measure(0, 2, 5), _measure(1, 2, 5)) is False
    assert rejects_regression(_measure(0, 2, 5), _measure(1, 1, 5)) is False


def test_more_audit_findings_alone_is_not_a_regression() -> None:
    """A deeper look finding more to say is not the unit getting worse."""
    assert rejects_regression(_measure(0, 2, 1), _measure(1, 2, 9)) is False


# ------------------------------------------------------------------ audit


def _finding(severity: Severity, key: str = "A-001") -> AuditFinding:
    return AuditFinding(key=key, category="clarity", severity=severity, problem_text="a problem")


def test_the_audit_score_is_computed_from_severities_not_taken_from_a_model() -> None:
    assert AuditReport(findings=()).score == 1.0
    assert AuditReport(findings=(_finding("critical"),)).score == 0.0
    assert AuditReport(findings=(_finding("nit"),)).score == pytest.approx(0.95)
    assert weighted_score([_finding("major"), _finding("minor", "A-002")]) == pytest.approx(0.35)


def test_one_critical_finding_alone_triggers_escalation_at_the_default_threshold() -> None:
    """The weights are chosen so that a critical finding buys a deeper look, and nits do not."""
    default_threshold = 0.6
    assert AuditReport(findings=(_finding("critical"),)).score < default_threshold
    nits = tuple(_finding("nit", f"A-{n:03d}") for n in range(4))
    assert AuditReport(findings=nits).score >= default_threshold


def test_an_audit_report_has_no_content_field() -> None:
    """Workflows §1 rule 2, enforced by the return type.

    A future field called `suggested_text` or `revised_content` is exactly how "auditors report,
    writers repair" would quietly stop being true, so the shape is asserted rather than reviewed.
    `requirement_verdicts` (ADR-0039) is keys plus a three-value enum — a verdict, not a channel
    revised content could travel through — which is why admitting it does not weaken the rule.
    """
    fields = set(AuditReport.__dataclass_fields__)
    assert fields == {"findings", "stage", "requirement_verdicts"}
    for forbidden in ("text", "content", "revised_text", "suggested_text", "replacement"):
        assert forbidden not in fields


def test_a_critique_has_no_content_field_either() -> None:
    fields = set(Critique.__dataclass_fields__)
    assert fields == {"verdict", "rationale_text", "improvement_delta"}


def test_a_finding_carries_a_description_of_a_fix_never_replacement_text() -> None:
    """`required_fix_text` is prose about what would resolve it, and never becomes the unit."""
    finding = AuditFinding(
        key="A-001",
        category="accuracy",
        severity="major",
        problem_text="the claim is unsupported",
        required_fix_text="say which measurement supports it, or drop the claim",
    )
    assert "say which" in finding.required_fix_text


def test_finding_delta_is_deterministic() -> None:
    assert finding_delta([_finding("major")], []) == 1
    assert finding_delta([], [_finding("major")]) == -1


def test_leave_it_alone_is_a_clean_pass_that_does_not_ask_for_a_revision() -> None:
    leave = Critique(verdict="leave_it_alone", rationale_text="stylistic only")
    assert leave.is_clean_pass
    assert not leave.wants_revision
    assert _decide(wants_revision=leave.wants_revision).stop_reason == "critique_satisfied"


def test_acceptable_and_leave_it_alone_are_distinct_verdicts() -> None:
    """They say different things about the critic, and a report should be able to show which."""
    assert VERDICTS == {"acceptable", "leave_it_alone", "materially_deficient"}
    assert Critique(verdict="acceptable").is_clean_pass
    assert Critique(verdict="leave_it_alone").is_clean_pass
    assert not Critique(verdict="materially_deficient").is_clean_pass
