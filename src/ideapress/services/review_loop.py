"""ideapress.services.review_loop — the bounded review loop, and what stops it.

`audit_fast` → (escalate to `audit_deep` once per round if the score is low) → `critique` →
Python decides → `revise` → back to validation. Three stops, all Python's, and the reason is always
recorded (workflows §5).

The two properties this exists to hold, both tested:

* **A revision that increases validation failures is discarded** and the previous text kept. A
  round that makes the unit worse is not an improvement with a silver lining.
* **The round limit is checked before the critic is consulted at all**, so a critic that always
  answers "materially deficient" cannot extend the loop by even one round.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ideapress.domain.audit import AuditFinding
from ideapress.domain.context_assembly import assemble_context
from ideapress.domain.revision_policy import (
    RoundMeasurement,
    decide_revision,
    improvement,
    rejects_regression,
)
from ideapress.domain.validation import ValidationContext, run_validators
from ideapress.domain.validators import DEFAULT_VALIDATORS
from ideapress.infrastructure.db.models import AuditFinding as AuditFindingRow
from ideapress.infrastructure.db.models import Critique as CritiqueRow
from ideapress.services.review import run_audit, run_critique, run_revision
from ideapress.services.stages import record_attempt
from ideapress.services.units import record_validation

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from ideapress.domain.plan import PlanUnit
    from ideapress.domain.requirements import Requirement
    from ideapress.domain.validation import ValidationReport
    from ideapress.services.runtime import Runtime
    from ideapress.services.stages import StageTask

__all__ = ["ReviewOutcome", "run_review_loop"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReviewOutcome:
    """What the review loop settled on.

    Attributes:
        text: The text that survived. The original when no revision improved on it.
        validation: Its validation report.
        findings: Every audit finding across every round.
        rounds: How many revision rounds ran.
        stop_reason: Which stop applied — always recorded (workflows §5).
        stop_detail: The numbers behind it.
        audit_satisfied: Requirement keys an audit reported satisfied. Consulted **only** for
            requirements with no deterministic check.
        escalations: How many deep audits ran.
        rejected_revisions: How many rounds were discarded for making the unit worse.
    """

    text: str
    validation: ValidationReport
    findings: tuple[AuditFinding, ...]
    rounds: int
    stop_reason: str
    stop_detail: str
    audit_satisfied: tuple[str, ...] = ()
    escalations: int = 0
    rejected_revisions: int = 0
    critique_verdicts: tuple[str, ...] = field(default_factory=tuple)


def _validate(text: str, unit: PlanUnit, requirements: Sequence[Requirement]) -> ValidationReport:
    return run_validators(
        DEFAULT_VALIDATORS,
        ValidationContext(text=text, unit=unit, requirements=tuple(requirements)),
    )


def _store_findings(
    runtime: Runtime, attempt_id: str, findings: Sequence[AuditFinding], *, escalated: bool
) -> None:
    with runtime.storage.write() as session:
        for finding in findings:
            session.add(
                AuditFindingRow(
                    attempt_id=attempt_id,
                    finding_key=finding.key,
                    category=finding.category,
                    severity=finding.severity,
                    confidence=finding.confidence,
                    problem_text=finding.problem_text,
                    evidence_text=finding.evidence_text or None,
                    required_fix_text=finding.required_fix_text or None,
                    uncertain=finding.uncertain,
                    escalated=escalated,
                    source_stage=finding.source_stage,
                )
            )


def run_review_loop(
    runtime: Runtime,
    task: StageTask,
    *,
    project_id: str,
    unit: PlanUnit,
    requirements: Sequence[Requirement],
    text: str,
    validation: ValidationReport,
    attempt_id: str | None,
    emit: Callable[[str, str, dict[str, Any]], None],
) -> ReviewOutcome:
    """Audit, critique and revise a validated unit, within its bounds.

    Args:
        runtime: The process's handles.
        task: The running stage, for cancellation checkpoints.
        project_id, unit, requirements: What is being reviewed.
        text: The validated text to review.
        validation: Its validation report.
        attempt_id: The attempt that produced it.
        emit: Event emitter.

    Returns:
        The text that survived, with the stop reason recorded.

    Raises:
        StageCancelled: The user cancelled at a model-call boundary.
    """
    settings = runtime.settings
    database = runtime.storage
    gateway = runtime.runner.gateway
    max_rounds = settings.workflow.max_revision_rounds
    threshold = settings.workflow.diminishing_returns_threshold
    escalation_threshold = settings.workflow.audit_escalation_threshold
    structured_output_tokens = settings.workflow.structured_output_tokens

    all_findings: list[AuditFinding] = []
    verdicts: list[str] = []
    escalations = 0
    rejected = 0
    rounds = 0
    before: RoundMeasurement | None = None
    after: RoundMeasurement | None = None
    stop_reason = "critique_satisfied"
    stop_detail = "no review round ran"
    audit_satisfied: set[str] = set()

    while True:
        runtime.runner.checkpoint(task)
        fast = run_audit(
            gateway,
            stage="audit_fast",
            project_id=project_id,
            unit_key=unit.key,
            content=text,
            requirements=requirements,
            round_number=rounds,
            structured_output_tokens=structured_output_tokens,
        )
        fast_attempt = record_attempt(
            database,
            stage_run_id=task.run_id,
            stage="audit_fast",
            result=fast.result,
            unit_id=_unit_id(runtime, project_id, unit.key),
            attempt=1,
            round_=rounds,
            prompt_id=fast.prompt_id,
            prompt_version=fast.prompt_version,
            prompt_sha256=fast.prompt_sha256,
            store_content=runtime.runner.store_content,
        )
        _store_findings(runtime, fast_attempt, fast.report.findings, escalated=False)
        round_findings = list(fast.report.findings)
        emit(
            "audit.completed",
            f"{unit.key}: {fast.report.summary()}",
            {
                "unit_key": unit.key,
                "round": rounds,
                "stage": "audit_fast",
                "score": round(fast.report.score, 3),
                "findings": [
                    {"key": f.key, "severity": f.severity, "problem": f.problem_text}
                    for f in fast.report.findings
                ],
            },
        )

        # Escalation: at most once per unit per round, and only on a low computed score.
        if fast.report.score < escalation_threshold:
            runtime.runner.checkpoint(task)
            deep = run_audit(
                gateway,
                stage="audit_deep",
                project_id=project_id,
                unit_key=unit.key,
                content=text,
                requirements=requirements,
                prior_findings=fast.report.findings,
                round_number=rounds,
                structured_output_tokens=structured_output_tokens,
            )
            deep_attempt = record_attempt(
                database,
                stage_run_id=task.run_id,
                stage="audit_deep",
                result=deep.result,
                unit_id=_unit_id(runtime, project_id, unit.key),
                attempt=1,
                round_=rounds,
                prompt_id=deep.prompt_id,
                prompt_version=deep.prompt_version,
                prompt_sha256=deep.prompt_sha256,
                store_content=runtime.runner.store_content,
            )
            _store_findings(runtime, deep_attempt, deep.report.findings, escalated=True)
            round_findings.extend(deep.report.findings)
            escalations += 1
            emit(
                "audit.completed",
                f"{unit.key}: escalated — {deep.report.summary()}",
                {
                    "unit_key": unit.key,
                    "round": rounds,
                    "stage": "audit_deep",
                    "escalated": True,
                    "score": round(deep.report.score, 3),
                },
            )

        all_findings = round_findings
        # A requirement with no deterministic check is satisfied when no finding is about it
        # (workflows §3). Nothing here can overturn a check that ran — `evaluate_coverage` only
        # consults this for requirements that have none.
        mentioned = " ".join(f"{f.problem_text} {f.evidence_text}" for f in round_findings).lower()
        audit_satisfied = {
            requirement.key
            for requirement in requirements
            if not requirement.checks and requirement.key.lower() not in mentioned
        }

        current = RoundMeasurement(
            round_number=rounds,
            validation_failures=validation.failure_count,
            audit_findings=len(round_findings),
        )
        before, after = after, current
        delta = improvement(before, after) if before is not None else None

        runtime.runner.checkpoint(task)
        critique = run_critique(
            gateway,
            project_id=project_id,
            unit_key=unit.key,
            content=text,
            requirements=requirements,
            findings=round_findings,
            rounds_used=rounds,
            max_rounds=max_rounds,
            improvement_delta=delta,
            structured_output_tokens=structured_output_tokens,
        )
        critique_attempt = record_attempt(
            database,
            stage_run_id=task.run_id,
            stage="critique",
            result=critique.result,
            unit_id=_unit_id(runtime, project_id, unit.key),
            attempt=1,
            round_=rounds,
            prompt_id=critique.prompt_id,
            prompt_version=critique.prompt_version,
            prompt_sha256=critique.prompt_sha256,
            store_content=runtime.runner.store_content,
        )
        verdicts.append(critique.critique.verdict)
        emit(
            "critique.completed",
            f"{unit.key}: {critique.critique.verdict}",
            {
                "unit_key": unit.key,
                "round": rounds,
                "verdict": critique.critique.verdict,
                "rationale": critique.critique.rationale_text,
            },
        )

        decision = decide_revision(
            wants_revision=critique.critique.wants_revision,
            round_number=rounds,
            max_rounds=max_rounds,
            before=before,
            after=after,
            threshold=threshold,
        )
        with database.write() as session:
            session.add(
                CritiqueRow(
                    attempt_id=critique_attempt,
                    verdict=critique.critique.verdict,
                    rationale_text=critique.critique.rationale_text,
                    improvement_delta=delta,
                    round=rounds,
                    stop_reason=decision.stop_reason,
                )
            )
        if not decision.should_revise:
            stop_reason = decision.stop_reason or "critique_satisfied"
            stop_detail = decision.detail
            break

        runtime.runner.checkpoint(task)
        context = assemble_context(
            unit=unit,
            requirements=requirements,
            budget_tokens=settings.workflow.context_budget_tokens,
            previous_findings="\n".join(f"- {f.problem_text}" for f in round_findings),
        )
        revision = run_revision(
            gateway,
            project_id=project_id,
            unit_key=unit.key,
            context=context.render(),
            content=text,
            findings=round_findings,
            round_number=rounds + 1,
            max_output_tokens=max(8192, (unit.target_words or 400) * 4 + 8192),
        )
        record_attempt(
            database,
            stage_run_id=task.run_id,
            stage="revise",
            result=revision.result,
            unit_id=_unit_id(runtime, project_id, unit.key),
            attempt=1,
            round_=rounds + 1,
            prompt_id=revision.prompt_id,
            prompt_version=revision.prompt_version,
            prompt_sha256=revision.prompt_sha256,
            store_content=runtime.runner.store_content,
        )
        revised_validation = _validate(revision.text, unit, requirements)
        proposed = RoundMeasurement(
            round_number=rounds + 1,
            validation_failures=revised_validation.failure_count,
            audit_findings=len(round_findings),
        )
        if rejects_regression(after, proposed):
            rejected += 1
            stop_reason = "regression_rejected"
            stop_detail = (
                f"round {rounds + 1} raised validation failures from "
                f"{after.validation_failures} to {proposed.validation_failures}; "
                "the previous version was kept"
            )
            emit(
                "revision.rejected",
                f"{unit.key}: {stop_detail}",
                {"unit_key": unit.key, "round": rounds + 1, "reason": stop_reason},
            )
            break

        rounds += 1
        text = revision.text
        validation = revised_validation
        if attempt_id is not None:
            record_validation(database, attempt_id=attempt_id, report=validation)
        emit(
            "revision.completed",
            f"{unit.key}: round {rounds} — {validation.summary()}",
            {"unit_key": unit.key, "round": rounds, "failures": validation.failure_count},
        )

    emit(
        "review.stopped",
        f"{unit.key}: {stop_reason} — {stop_detail}",
        {
            "unit_key": unit.key,
            "stop_reason": stop_reason,
            "detail": stop_detail,
            "rounds": rounds,
            "escalations": escalations,
            "rejected_revisions": rejected,
        },
    )
    return ReviewOutcome(
        text=text,
        validation=validation,
        findings=tuple(all_findings),
        rounds=rounds,
        stop_reason=stop_reason,
        stop_detail=stop_detail,
        audit_satisfied=tuple(sorted(audit_satisfied)),
        escalations=escalations,
        rejected_revisions=rejected,
        critique_verdicts=tuple(verdicts),
    )


def _unit_id(runtime: Runtime, project_id: str, unit_key: str) -> str:
    from ideapress.services.units import load_unit

    with runtime.storage.read() as session:
        return load_unit(session, project_id, unit_key).id
