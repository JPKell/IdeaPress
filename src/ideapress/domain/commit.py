"""ideapress.domain.commit — what a commit is, and what must be true before one happens.

Workflows §2 stage 14: **no model involved**. A commit is an atomic write of a validated unit with
full provenance, and every condition on it is arithmetic over things Python computed.

The domain half is here — the decision — and the write is in
:mod:`ideapress.services.units`, because atomicity is a property of a transaction and a transaction
belongs to the service layer. Keeping the *decision* pure means the reason a commit was refused can
be reported without a database anywhere near it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ideapress.domain.requirements import evaluate_requirement

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ideapress.domain.requirements import Requirement
    from ideapress.domain.validation import ValidationReport

__all__ = [
    "CommitDecision",
    "CoverageEntry",
    "CoverageReport",
    "SatisfiedBy",
    "content_hash",
    "decide_commit",
    "evaluate_coverage",
    "word_count",
]

SatisfiedBy = Literal["deterministic_check", "audit", "manual", "unsatisfied"]


def content_hash(text: str) -> str:
    """The hash a committed version is identified by.

    Args:
        text: The unit's content.

    Returns:
        ``sha256:<hex>``. The digest is BaseAiCore's, over the string's canonical JSON, so two runs
        on two machines and two Python versions agree — which the export byte-identity claim
        depends on. The prefix is the suite's convention and is carried explicitly, so a bare hex
        string can never be mistaken for one of these.
    """
    from baseaicore import sha256_of

    return f"sha256:{sha256_of(text)}"


def word_count(text: str) -> int:
    """Count words the way the length validator does, so a report and a gate never disagree."""
    import re

    return len(re.findall(r"\b[\w'-]+\b", text, flags=re.UNICODE))


@dataclass(frozen=True, slots=True)
class CoverageEntry:
    """Whether one requirement is satisfied by this text, and by what.

    Attributes:
        requirement: The requirement.
        satisfied: Whether it is met.
        satisfied_by: ``deterministic_check`` when a check decided it; ``audit`` when nothing
            mechanical could and a model-assisted stage must; ``manual`` when a person said so;
            ``unsatisfied`` when it is not met.
        detail: What was observed.
    """

    requirement: Requirement
    satisfied: bool
    satisfied_by: SatisfiedBy
    detail: str

    @property
    def is_mechanical(self) -> bool:
        """Whether a deterministic check decided this, rather than a model.

        Workflows §3: the coverage report shows which guarantees are mechanical and which are
        model-assisted, so the user can tell what the product actually verified.
        """
        return self.satisfied_by == "deterministic_check"


@dataclass(frozen=True, slots=True)
class CoverageReport:
    """Coverage over every requirement a unit carries."""

    entries: tuple[CoverageEntry, ...]

    @property
    def unmet_blocking(self) -> tuple[CoverageEntry, ...]:
        """Blocking requirements that are not satisfied — what stops a commit."""
        return tuple(e for e in self.entries if e.requirement.blocking and not e.satisfied)

    @property
    def model_assisted(self) -> tuple[CoverageEntry, ...]:
        """Requirements with no deterministic check, which only an audit can speak to."""
        return tuple(e for e in self.entries if e.satisfied and not e.is_mechanical)

    @property
    def satisfied(self) -> bool:
        """Whether every blocking requirement is met."""
        return not self.unmet_blocking

    def summary(self) -> str:
        """One line for a log or a CLI."""
        met = sum(1 for entry in self.entries if entry.satisfied)
        mechanical = sum(1 for entry in self.entries if entry.is_mechanical)
        return (
            f"{met}/{len(self.entries)} requirements satisfied, "
            f"{mechanical} by a deterministic check"
        )


def evaluate_coverage(
    text: str, requirements: Sequence[Requirement], *, audit_satisfied: Sequence[str] = ()
) -> CoverageReport:
    """Decide, per requirement, whether this text satisfies it.

    Args:
        text: The unit's content.
        requirements: The requirements this unit carries.
        audit_satisfied: Requirement keys an audit stage **explicitly attested met**
            (ADR-0039) — never keys it was silent about; the caller builds this set from
            per-requirement verdicts, not from the absence of findings. **Only consulted for
            requirements with no deterministic check**: where a check exists, the check decides and
            a model's opinion cannot overturn it. That asymmetry is the whole of T1 in one place —
            a model may fill a gap Python cannot reach, and may never overrule Python where it can.

    Returns:
        The report, naming for every requirement what decided it.
    """
    entries: list[CoverageEntry] = []
    for requirement in requirements:
        if requirement.checks:
            outcomes = evaluate_requirement(requirement, text)
            passed = all(outcome.passed for outcome in outcomes)
            failures = "; ".join(o.detail for o in outcomes if not o.passed)
            entries.append(
                CoverageEntry(
                    requirement=requirement,
                    satisfied=passed,
                    satisfied_by="deterministic_check" if passed else "unsatisfied",
                    detail=(
                        "; ".join(o.detail for o in outcomes) if passed else f"failed: {failures}"
                    ),
                )
            )
            continue
        by_audit = requirement.key in audit_satisfied
        entries.append(
            CoverageEntry(
                requirement=requirement,
                satisfied=by_audit,
                satisfied_by="audit" if by_audit else "unsatisfied",
                detail=(
                    "no deterministic check; the audit explicitly attested this met — a "
                    "model-review guarantee, not a mechanical one"
                    if by_audit
                    else "no deterministic check, and no audit has attested it met"
                ),
            )
        )
    return CoverageReport(entries=tuple(entries))


@dataclass(frozen=True, slots=True)
class CommitDecision:
    """Whether a unit may be committed, and if not, why not."""

    allowed: bool
    reasons: tuple[str, ...] = ()

    @property
    def refusal(self) -> str:
        """Every reason, joined — what the user is told and what the unit's pause records."""
        return "; ".join(self.reasons)


def decide_commit(
    *,
    text: str,
    validation: ValidationReport,
    coverage: CoverageReport,
    require_clean_validation: bool = True,
    audit_gating_allowed: bool = True,
) -> CommitDecision:
    """Decide whether this text may become a committed version.

    Args:
        text: The unit's content.
        validation: The deterministic validation report.
        coverage: The requirement-coverage report.
        require_clean_validation: `workflow.require_clean_validation_to_commit`. When ``False``, a
            blocking validation failure no longer stops the commit — coverage still does. This is
            configuration, so a project can lower a bar it set itself; it cannot lower the
            requirement bar, because that one came from the author's own material.
        audit_gating_allowed: `workflow.allow_audit_gated_requirements` (ADR-0039). Configuration,
            not a model channel: it only changes what the refusal *says* about an unmet check-less
            blocking requirement — with it off, "run the review stage" would be false advice,
            because no audit verdict can satisfy the requirement and the user's real options are a
            deterministic check, a demotion, or turning the setting back on.

    Returns:
        The decision, with every reason it was refused. Every reason traces to a deterministic
        check, an exhausted bound, or — for a check-less requirement — an audit's explicit
        attestation carried in ``coverage``; never to a model's unprompted opinion (risk T1).
    """
    reasons: list[str] = []
    if not text.strip():
        reasons.append("the unit is empty")
    if require_clean_validation and validation.blocking_failures:
        failing = ", ".join(outcome.check_key for outcome in validation.blocking_failures)
        reasons.append(
            f"{len(validation.blocking_failures)} blocking validation failure(s): {failing}"
        )
    if coverage.unmet_blocking:
        mechanical = [e for e in coverage.unmet_blocking if e.requirement.checks]
        awaiting_audit = [e for e in coverage.unmet_blocking if not e.requirement.checks]
        if mechanical:
            unmet = ", ".join(entry.requirement.key for entry in mechanical)
            reasons.append(f"{len(mechanical)} blocking requirement(s) unmet: {unmet}")
        if awaiting_audit:
            # A blocking requirement the compiler could not express as a literal check is not a
            # defect in the text: nothing mechanical can settle it, so only a review stage's
            # explicit attestation can (workflows §3, ADR-0039). Saying "unmet" would send the
            # reader looking for missing content that is very likely already there.
            pending = ", ".join(entry.requirement.key for entry in awaiting_audit)
            if audit_gating_allowed:
                reasons.append(
                    f"{len(awaiting_audit)} blocking requirement(s) have no deterministic check "
                    f"and no audit has attested them met: {pending}. Run the review stage; a "
                    "requirement nothing mechanical can settle needs the audit's explicit "
                    "attestation, and the coverage report labels that as a model-review "
                    "guarantee rather than implying it is mechanical."
                )
            else:
                reasons.append(
                    f"{len(awaiting_audit)} blocking requirement(s) have no deterministic check "
                    f"and workflow.allow_audit_gated_requirements is false: {pending}. Give them "
                    "a deterministic check, demote them to advisory, or allow audit gating."
                )
    return CommitDecision(allowed=not reasons, reasons=tuple(reasons))
