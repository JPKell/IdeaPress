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
from ideapress.services.review import (
    run_audit,
    run_critique,
    run_fact_check,
    run_revision,
)
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
        audit_satisfied: Requirement keys an audit **explicitly attested met** (ADR-0039) —
            never keys it was merely silent about. Consulted **only** for requirements with no
            deterministic check, and always empty when
            ``workflow.allow_audit_gated_requirements`` is off.
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


def _project_sources(runtime: Runtime, project_id: str) -> dict[str, str]:
    """The project's attached source documents, by title, for the fact checker.

    A source with no stored text — an opt-in URL never fetched, a file whose body was not kept —
    is omitted rather than passed as an empty document, because a checker told a source is empty
    reports every claim in the unit as unsupported and floods the round with noise.
    """
    from sqlalchemy import select

    from ideapress.infrastructure.db.models import Source

    with runtime.storage.read() as session:
        rows = session.execute(
            select(Source.title, Source.content_text)
            .where(Source.project_id == project_id)
            .order_by(Source.created_at, Source.id)
        ).all()
    return {str(title): str(body) for title, body in rows if body and str(body).strip()}


def _fact_check_applies(
    runtime: Runtime, project_id: str, *, requirements: Sequence[Requirement]
) -> bool:
    """Whether this unit's round runs `fact_check` (ADR-0043 §2).

    Args:
        runtime: The process's handles.
        project_id: The project.
        requirements: The unit's requirements.

    Returns:
        ``True`` when the project has at least one source carrying text **and** either the content
        type turns fact checking on by default (a report does; an article does not — workflows §2
        stage 10) or one of this unit's requirements demands grounding.

    Both halves are load-bearing. Without a source there is nothing to check against and the stage
    would report every claim unsupported; without a grounding-demanding requirement or a content
    type that asks for it, a model call per unit buys nothing the audit does not already cover.
    """
    if not _project_sources(runtime, project_id):
        return False
    if any(r.demands_grounding for r in requirements):
        return True
    from sqlalchemy import select

    from ideapress.content_types.registry import discover
    from ideapress.infrastructure.db.models import Project

    with runtime.storage.read() as session:
        name = session.execute(
            select(Project.content_type).where(Project.id == project_id)
        ).scalar_one_or_none()
    content_type = discover().get(str(name or ""))
    return bool(content_type and content_type.fact_check_by_default)


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
    # A module-level import would be circular: unit_loop imports this module for the loop.
    from ideapress.services.unit_loop import output_budget_tokens

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
        round_verdicts: dict[str, str] = dict(fast.report.requirement_verdicts)
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
            # The deep audit's verdicts override the fast one's where both spoke: it ran later,
            # with the fast findings in front of it.
            round_verdicts.update(dict(deep.report.requirement_verdicts))
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

        # Fact check (ADR-0043 §2): only where there is something to check against, and only for
        # a unit that carries a grounding-demanding requirement. It reports; it cannot pass
        # anything. Its findings join the round like any other, so the existing revise/re-audit
        # machinery handles them and no new control flow exists for a model to influence.
        if _fact_check_applies(runtime, project_id, requirements=requirements):
            runtime.runner.checkpoint(task)
            sources = _project_sources(runtime, project_id)
            checked = run_fact_check(
                gateway,
                project_id=project_id,
                unit_key=unit.key,
                content=text,
                sources=sources,
                round_number=rounds,
                structured_output_tokens=structured_output_tokens,
            )
            checked_attempt = record_attempt(
                database,
                stage_run_id=task.run_id,
                stage="fact_check",
                result=checked.result,
                unit_id=_unit_id(runtime, project_id, unit.key),
                attempt=1,
                round_=rounds,
                prompt_id=checked.prompt_id,
                prompt_version=checked.prompt_version,
                prompt_sha256=checked.prompt_sha256,
                store_content=runtime.runner.store_content,
            )
            _store_findings(runtime, checked_attempt, checked.report.findings, escalated=False)
            round_findings.extend(checked.report.findings)
            emit(
                "fact_check.completed",
                f"{unit.key}: {len(checked.report.findings)} unsupported claim(s)",
                {
                    "unit_key": unit.key,
                    "round": rounds,
                    "stage": "fact_check",
                    "sources": sorted(sources),
                    "findings": [
                        {"key": f.key, "problem": f.problem_text} for f in checked.report.findings
                    ],
                },
            )

        all_findings = round_findings
        # A requirement with no deterministic check is satisfied only by an audit's **explicit**
        # `met` attestation (ADR-0039). Silence, `cannot_judge` and an invented verdict all leave
        # it unsatisfied — the mechanism this replaced read the absence of a finding as
        # satisfaction, which let the model's default behaviour settle a blocking gate (M7-20).
        # Nothing here can overturn a check that ran — `evaluate_coverage` only consults this for
        # requirements that have none — and a key the model invented is discarded against the
        # requirement list Python rendered.
        checkless = {r.key for r in requirements if not r.checks}
        if settings.workflow.allow_audit_gated_requirements:
            audit_satisfied = {
                key
                for key, verdict in round_verdicts.items()
                if verdict == "met" and key in checkless
            }
        else:
            audit_satisfied = set()

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
            max_output_tokens=output_budget_tokens(
                target_words=unit.target_words,
                structured_output_tokens=structured_output_tokens,
            ),
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
