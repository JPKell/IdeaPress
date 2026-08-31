"""ideapress.domain.critique — the quality verdict, with "leave it alone" as a first-class answer.

Workflows §5: *"Leave it alone" is an explicitly valid critique verdict*: a purely stylistic
preference does not trigger a revision, because endless polishing is how these systems burn hours
without improving anything (risk T2).

A critique, like an audit, **cannot change content**: :class:`Critique` holds a verdict and a
rationale and no text. The verdict is a report; whether a revision happens is
:mod:`ideapress.domain.revision_policy`'s decision, made by Python.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

__all__ = ["VERDICTS", "Critique", "Verdict"]

Verdict = Literal["acceptable", "leave_it_alone", "materially_deficient"]

VERDICTS: Final[frozenset[str]] = frozenset(
    {"acceptable", "leave_it_alone", "materially_deficient"}
)
"""The three, and only these.

``acceptable`` and ``leave_it_alone`` differ in what they say about the *critic*, not about the
work: ``acceptable`` is "this meets the bar", ``leave_it_alone`` is "I can see things I would
change and none of them is worth another round". Keeping them apart is what lets a report show that
a critic had opinions and declined to act on them, which is the honest version of a clean pass."""


@dataclass(frozen=True, slots=True)
class Critique:
    """A quality verdict on one version of a unit. Holds no content.

    Attributes:
        verdict: One of :data:`VERDICTS`.
        rationale_text: Why, in the critic's words. Recorded, shown, and never parsed for
            instructions — workflows §11's "a critique verdict parsed as a command rather than a
            report" is P5's named failure mode.
        improvement_delta: The deterministic change in finding count since the previous round, when
            there was one. Computed by Python; a critic does not get to report its own improvement.
    """

    verdict: Verdict
    rationale_text: str = ""
    improvement_delta: float | None = None

    @property
    def wants_revision(self) -> bool:
        """Whether this verdict *asks* for a revision.

        Asking is not deciding: :func:`~ideapress.domain.revision_policy.decide_revision` weighs
        this against the round limit and the diminishing-returns stop, and a critic that always
        answered ``materially_deficient`` would still stop.
        """
        return self.verdict == "materially_deficient"

    @property
    def is_clean_pass(self) -> bool:
        """Whether the critic is content, by either route."""
        return self.verdict in {"acceptable", "leave_it_alone"}
