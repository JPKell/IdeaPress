"""ideapress.domain.validation — deterministic validation, and what a report of it says.

Workflows §2 stage 6: **no model involved**. This module and
:mod:`ideapress.domain.validators` are the reason "a gate passed" means something, and every
function here is a pure function of text plus a context Python assembled.

Failures are classed ``blocking`` or ``advisory`` (workflows §4). Blocking failures route to
repair; three failed repairs pause the unit and surface the problem rather than committing
something wrong. Advisory failures inform critique and never stop anything — treating them as
blocking "because it is stricter" is risk T4's named trap, and it is how a validator suite starts
refusing legitimate writing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from ideapress.domain.plan import PlanUnit
    from ideapress.domain.requirements import Requirement

__all__ = [
    "Severity",
    "ValidationContext",
    "ValidationOutcome",
    "ValidationReport",
    "Validator",
    "run_validators",
]

Severity = str


@dataclass(frozen=True, slots=True)
class ValidationContext:
    """Everything a validator may look at. Assembled by Python; a model contributes nothing.

    Attributes:
        text: The unit's content, as produced.
        unit: The unit's plan entry — its goal, its requirement keys, its length target.
        requirements: The compiled requirements this unit carries.
        glossary: Project terms and their required spelling, for the consistency family.
        forbidden_phrases: Text the project has said must not appear.
        source_titles: The titles of every attached source, for citation checks.
        committed_units: Previously committed unit text, by key, for cross-unit consistency.
        content_type: ``article`` or ``report``; selects the structural expectations.
    """

    text: str
    unit: PlanUnit | None = None
    requirements: tuple[Requirement, ...] = ()
    glossary: Mapping[str, str] = field(default_factory=dict)
    forbidden_phrases: tuple[str, ...] = ()
    source_titles: tuple[str, ...] = ()
    committed_units: Mapping[str, str] = field(default_factory=dict)
    content_type: str = "article"


@dataclass(frozen=True, slots=True)
class ValidationOutcome:
    """One check's verdict.

    Attributes:
        check_kind: The family — ``structural``, ``length``, ``format``, ``content``,
            ``reference``, ``consistency``, ``safety``.
        check_key: Which check within the family, stable enough to store and to filter on.
        passed: Whether it passed.
        blocking: Whether a failure stops the commit. Meaningless when ``passed``.
        detail: What was observed, in a person's words.
        evidence: The offending text, truncated for display.
    """

    check_kind: str
    check_key: str
    passed: bool
    blocking: bool
    detail: str
    evidence: tuple[str, ...] = ()

    @property
    def severity(self) -> Severity:
        """``ok``, ``blocking`` or ``advisory`` — what a report shows in one column."""
        if self.passed:
            return "ok"
        return "blocking" if self.blocking else "advisory"


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """Every check that ran, and what follows from them."""

    outcomes: tuple[ValidationOutcome, ...]

    @property
    def blocking_failures(self) -> tuple[ValidationOutcome, ...]:
        """The failures that stop a commit."""
        return tuple(o for o in self.outcomes if not o.passed and o.blocking)

    @property
    def advisory_failures(self) -> tuple[ValidationOutcome, ...]:
        """The failures that inform critique and stop nothing."""
        return tuple(o for o in self.outcomes if not o.passed and not o.blocking)

    @property
    def passed(self) -> bool:
        """Whether the unit may proceed. Advisory failures do not prevent it, by design."""
        return not self.blocking_failures

    @property
    def failure_count(self) -> int:
        """Every failure, blocking or not.

        This is the number the diminishing-returns stop compares between revision rounds
        (workflows §5): a deterministic count, never a critic's self-assessment.
        """
        return len(self.blocking_failures) + len(self.advisory_failures)

    def summary(self) -> str:
        """One line for a log or a CLI."""
        blocking = len(self.blocking_failures)
        advisory = len(self.advisory_failures)
        if not blocking and not advisory:
            return f"{len(self.outcomes)} checks passed"
        return f"{blocking} blocking, {advisory} advisory, of {len(self.outcomes)} checks"


class Validator(Protocol):
    """One family of deterministic checks."""

    @property
    def kind(self) -> str:
        """The family's name, used as ``check_kind`` on every outcome it produces."""
        ...

    def check(self, context: ValidationContext) -> Sequence[ValidationOutcome]:
        """Run every check in this family. Never raises for bad content — it reports."""
        ...


def run_validators(validators: Sequence[Validator], context: ValidationContext) -> ValidationReport:
    """Run every validator and collect one report.

    Args:
        validators: The families to run, in a fixed order so a report is stable.
        context: What they may look at.

    Returns:
        The report. A validator that raises is a defect in the validator, not in the content, so
        nothing is caught here: a swallowed exception would report "passed" for a check that never
        ran, which is the one outcome this module exists to prevent.
    """
    outcomes: list[ValidationOutcome] = []
    for validator in validators:
        outcomes.extend(validator.check(context))
    return ValidationReport(outcomes=tuple(outcomes))
