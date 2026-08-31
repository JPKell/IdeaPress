"""ideapress.domain.revision_policy — when revision stops, and which stop applied.

Risk T2 is unbounded revision consuming hours without improving anything. Three stops, all decided
by Python, and **the reason is always recorded** (workflows §5):

* the critic said "leave it alone" or "acceptable";
* improvement fell below ``diminishing_returns_threshold``;
* the round limit was reached.

Improvement is the change in **deterministic finding counts** between rounds — validation failures
plus audit findings — never the critic's own assessment of how much better it got. A critic that
reports "much improved" every round would otherwise keep the loop alive forever, which is the
failure this module exists to make impossible.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = ["RevisionDecision", "RoundMeasurement", "StopReason", "decide_revision", "improvement"]

StopReason = Literal[
    "critique_satisfied", "diminishing_returns", "round_limit", "regression_rejected"
]


@dataclass(frozen=True, slots=True)
class RoundMeasurement:
    """What one revision round produced, in numbers Python computed.

    Attributes:
        round_number: 1-based.
        validation_failures: Blocking plus advisory failures after this round.
        audit_findings: Audit findings after this round.
    """

    round_number: int
    validation_failures: int
    audit_findings: int

    @property
    def total(self) -> int:
        """The single number improvement is measured on."""
        return self.validation_failures + self.audit_findings


def improvement(before: RoundMeasurement, after: RoundMeasurement) -> float:
    """The fractional improvement between two rounds.

    Args:
        before: The measurement before the round.
        after: The measurement after it.

    Returns:
        ``(before - after) / before``, in ``[-inf, 1]``. Zero when there was nothing to improve, so
        a unit that started clean does not read as "improving by 100%" and keep the loop alive.
        **Negative when the round made things worse**, which is what the regression rule reads.
    """
    if before.total == 0:
        return 0.0
    return (before.total - after.total) / before.total


@dataclass(frozen=True, slots=True)
class RevisionDecision:
    """Whether to revise again, and — when not — which stop applied.

    Attributes:
        should_revise: Whether another round runs.
        stop_reason: Which rule ended it. ``None`` only while revising.
        detail: The numbers behind the decision, for the record and the UI.
    """

    should_revise: bool
    stop_reason: StopReason | None = None
    detail: str = ""


def decide_revision(
    *,
    wants_revision: bool,
    round_number: int,
    max_rounds: int,
    before: RoundMeasurement | None,
    after: RoundMeasurement | None,
    threshold: float,
) -> RevisionDecision:
    """Decide whether to run another revision round.

    Args:
        wants_revision: Whether the critique asked for one. **Asking, not deciding.**
        round_number: How many rounds have completed.
        max_rounds: ``workflow.max_revision_rounds``.
        before: The measurement before the last round, or ``None`` on the first pass.
        after: The measurement after it, or ``None`` on the first pass.
        threshold: ``workflow.diminishing_returns_threshold``.

    Returns:
        The decision, naming the stop when it stops.

    The order is deliberate. The **round limit is checked first**, before the critic is consulted at
    all, so a critic that always answers "materially deficient" cannot extend the loop by one round
    — it cannot extend it by any. Then the critic's satisfaction, then the arithmetic.
    """
    if round_number >= max_rounds:
        return RevisionDecision(
            should_revise=False,
            stop_reason="round_limit",
            detail=f"{round_number} of {max_rounds} rounds used",
        )
    if not wants_revision:
        return RevisionDecision(
            should_revise=False,
            stop_reason="critique_satisfied",
            detail="the critique did not ask for a revision",
        )
    if before is not None and after is not None:
        gain = improvement(before, after)
        if gain < threshold:
            return RevisionDecision(
                should_revise=False,
                stop_reason="diminishing_returns",
                detail=(
                    f"round {after.round_number} changed the finding count from {before.total} to "
                    f"{after.total} ({gain:+.0%}), below the {threshold:.0%} threshold"
                ),
            )
    return RevisionDecision(
        should_revise=True,
        detail=f"round {round_number + 1} of {max_rounds}",
    )


def rejects_regression(before: RoundMeasurement, after: RoundMeasurement) -> bool:
    """Whether a round made the unit worse and must be discarded.

    Args:
        before: The measurement before the round.
        after: The measurement after it.

    Returns:
        ``True`` when **validation failures increased**. Only validation, deliberately: audit
        findings vary with what an auditor happened to notice, and discarding a genuine improvement
        because a deeper look found more to say would be the wrong lesson. Validation failures are
        the same checks run on different text, so an increase is unambiguous — the revision broke
        something the previous version had right.
    """
    return after.validation_failures > before.validation_failures
