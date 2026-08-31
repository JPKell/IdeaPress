"""ideapress.services.unit_loop — the core loop: draft, validate, repair, coverage, commit.

Workflows §2 stages 5–7 and 13–14, and the two bounded loops of §5. The shape is the whole point:

* the **model** drafts and repairs;
* **Python** validates, computes coverage and decides the commit;
* the loop is bounded by ``max_attempts_per_stage``, and exhausting it **pauses the unit** rather
  than committing something that failed.

A paused unit is a first-class outcome, not a failure (data model §3): its content and its findings
are kept, the reason is recorded on the unit, and a person decides what to do. Nothing is ever
committed to escape a loop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from ideapress.domain.commit import decide_commit, evaluate_coverage
from ideapress.domain.context_assembly import assemble_context
from ideapress.domain.inference import Correlation, StageLimits, StageRequest
from ideapress.domain.stages import StageId
from ideapress.domain.validation import ValidationContext, run_validators
from ideapress.domain.validators import DEFAULT_VALIDATORS
from ideapress.infrastructure.db.models import Unit as UnitRow
from ideapress.services.plan import load_plan, load_requirements
from ideapress.services.prompts import render
from ideapress.services.review_loop import run_review_loop
from ideapress.services.stages import record_attempt
from ideapress.services.units import (
    commit_unit,
    committed_units,
    record_validation,
    set_unit_state,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from ideapress.domain.commit import CoverageReport
    from ideapress.domain.plan import PlanUnit
    from ideapress.domain.requirements import Requirement
    from ideapress.domain.validation import ValidationReport
    from ideapress.services.runtime import Runtime
    from ideapress.services.stages import StageTask

__all__ = ["UnitOutcome", "draft_body", "run_unit"]

logger = logging.getLogger(__name__)

DRAFT_THINKING_FLOOR_TOKENS = 8192
"""Output tokens reserved for a reasoning model's thinking, before the answer's own budget.

Measured, not guessed: both of spec §12's default models spent more than 4 096 output tokens
reasoning before emitting a word on a short task, and returned an empty string when the allowance
ran out first."""


@dataclass(frozen=True, slots=True)
class UnitOutcome:
    """What the loop did with one unit.

    Attributes:
        unit_key: Which unit.
        committed: Whether it now has a committed version.
        attempts: How many model attempts it took.
        paused_reason: Why it stopped, when it did not commit.
        validation: The last validation report.
        coverage: The last coverage report.
    """

    unit_key: str
    committed: bool
    attempts: int
    paused_reason: str | None
    validation: ValidationReport | None
    coverage: CoverageReport | None


def _render_failures(report: ValidationReport) -> str:
    """One line per failing check, from the deterministic report rather than a summary of it."""
    lines = [
        f"- [{outcome.severity}] {outcome.check_key}: {outcome.detail}"
        for outcome in report.outcomes
        if not outcome.passed
    ]
    return "\n".join(lines) or "- (nothing failed)"


def run_unit(
    runtime: Runtime,
    task: StageTask,
    *,
    project_id: str,
    unit: PlanUnit,
    requirements: Sequence[Requirement],
    neighbours: dict[str, str],
    ordinals: dict[str, int],
    emit: Callable[[str, str, dict[str, Any]], None],
) -> UnitOutcome:
    """Draft, validate, repair and commit one unit.

    Args:
        runtime: The process's handles.
        task: The running stage, for cancellation checkpoints.
        project_id: Which project.
        unit: The unit's plan entry.
        requirements: The requirements it carries.
        neighbours: Committed unit text, for consistency context.
        ordinals: Unit positions, so "adjacent" means adjacent.
        emit: Event emitter, taking ``(event_type, message, data)``.

    Returns:
        What happened to the unit.

    Raises:
        ContextLimitExceeded: The requirements alone exceed the context budget. Not caught here:
            the stage fails with the numbers rather than drafting against a truncated contract.
        StageCancelled: The user cancelled at a model-call boundary.

    The repair bound is ``workflow.max_attempts_per_stage``. When it is exhausted, the unit is
    **paused** with the failing checks recorded — never committed. That is the difference between a
    workflow that stops and one that produces something wrong to avoid stopping.
    """
    settings = runtime.settings
    database = runtime.storage
    gateway = runtime.runner.gateway
    limit = settings.workflow.max_attempts_per_stage

    emit("unit.started", f"{unit.key}: {unit.title}", {"unit_key": unit.key})
    set_unit_state(database, project_id=project_id, unit_key=unit.key, state="drafting")

    text = ""
    report: ValidationReport | None = None
    findings = ""
    attempt_id: str | None = None

    for attempt in range(1, limit + 1):
        runtime.runner.checkpoint(task)
        context = assemble_context(
            unit=unit,
            requirements=requirements,
            budget_tokens=settings.workflow.context_budget_tokens,
            neighbouring_units=neighbours,
            unit_ordinals=ordinals,
            previous_findings=findings,
        )
        is_repair = attempt > 1
        prompt = (
            render(
                "stages.repair.fix",
                {
                    "context": context.render(),
                    "current_text": text,
                    "failures": findings,
                },
            )
            if is_repair
            else render("stages.draft.write", {"context": context.render()})
        )
        stage: StageId = "repair" if is_repair else "draft"
        emit(
            "attempt.started",
            f"{unit.key}: {stage} attempt {attempt} of {limit}",
            {"unit_key": unit.key, "stage": stage, "attempt": attempt},
        )
        result = gateway.run(
            StageRequest(
                stage=stage,
                system=prompt.system or "",
                user=prompt.user,
                limits=StageLimits(
                    temperature=0.4 if not is_repair else 0.2,
                    # The floor clears a reasoning model's thinking phase, which is spent from the
                    # same allowance as the answer: measured at over 4 096 tokens for a short
                    # structured task on this machine's models. Four tokens per target word on top
                    # is the answer's own room.
                    max_output_tokens=DRAFT_THINKING_FLOOR_TOKENS + (unit.target_words or 400) * 4,
                ),
                correlation=Correlation(project_id=project_id, unit_id=unit.key, attempt=attempt),
                prompt_id=prompt.prompt_id,
                prompt_version=prompt.version,
                prompt_sha256=prompt.sha256,
            )
        )
        text = result.text
        attempt_id = record_attempt(
            database,
            stage_run_id=task.run_id,
            stage=stage,
            result=result,
            unit_id=_unit_id(runtime, project_id, unit.key),
            attempt=attempt,
            prompt_id=prompt.prompt_id,
            prompt_version=prompt.version,
            prompt_sha256=prompt.sha256,
            outcome="content_rejected" if result.refused else "completed",
            store_content=runtime.runner.store_content,
        )

        if result.refused:
            reason = result.refusal_reason or "the model declined the task"
            _pause(runtime, project_id, unit.key, reason, emit, from_state="drafting")
            return UnitOutcome(unit.key, False, attempt, reason, None, None)

        with database.read() as session:
            source_titles: tuple[str, ...] = ()
            validation_context = ValidationContext(
                text=text,
                unit=unit,
                requirements=tuple(requirements),
                committed_units=committed_units(session, project_id),
                source_titles=source_titles,
            )
        report = run_validators(DEFAULT_VALIDATORS, validation_context)
        record_validation(database, attempt_id=attempt_id, report=report)
        emit(
            "validation.completed",
            f"{unit.key}: {report.summary()}",
            {
                "unit_key": unit.key,
                "attempt": attempt,
                "passed": report.passed,
                "failures": [
                    {"check": o.check_key, "detail": o.detail, "severity": o.severity}
                    for o in report.outcomes
                    if not o.passed
                ],
            },
        )
        if report.passed:
            break
        findings = _render_failures(report)
        if attempt < limit:
            set_unit_state(database, project_id=project_id, unit_key=unit.key, state="validating")
            set_unit_state(database, project_id=project_id, unit_key=unit.key, state="drafting")

    assert report is not None  # noqa: S101 — the loop runs at least once
    if not report.passed:
        reason = (
            f"{limit} repair attempts did not clear validation: "
            f"{', '.join(o.check_key for o in report.blocking_failures)}"
        )
        _pause(runtime, project_id, unit.key, reason, emit, from_state="drafting")
        return UnitOutcome(unit.key, False, limit, reason, report, None)

    runtime.runner.checkpoint(task)
    set_unit_state(database, project_id=project_id, unit_key=unit.key, state="validating")
    set_unit_state(database, project_id=project_id, unit_key=unit.key, state="auditing")
    review = run_review_loop(
        runtime,
        task,
        project_id=project_id,
        unit=unit,
        requirements=requirements,
        text=text,
        validation=report,
        attempt_id=attempt_id,
        emit=emit,
    )
    text = review.text
    report = review.validation
    audit_satisfied = review.audit_satisfied
    coverage = evaluate_coverage(text, requirements, audit_satisfied=audit_satisfied)
    emit(
        "coverage.completed",
        f"{unit.key}: {coverage.summary()}",
        {
            "unit_key": unit.key,
            "satisfied": coverage.satisfied,
            "entries": [
                {
                    "requirement_key": entry.requirement.key,
                    "satisfied": entry.satisfied,
                    "satisfied_by": entry.satisfied_by,
                    "detail": entry.detail,
                }
                for entry in coverage.entries
            ],
        },
    )

    decision = decide_commit(
        text=text,
        validation=report,
        coverage=coverage,
        require_clean_validation=settings.workflow.require_clean_validation_to_commit,
    )
    if not decision.allowed:
        _pause(runtime, project_id, unit.key, decision.refusal, emit, from_state="auditing")
        return UnitOutcome(unit.key, False, limit, decision.refusal, report, coverage)

    committed = commit_unit(
        database,
        project_id=project_id,
        unit_key=unit.key,
        text=text,
        coverage=coverage,
        attempt_id=attempt_id,
    )
    emit(
        "unit.committed",
        f"{unit.key}: version {committed.version} committed ({committed.word_count} words)",
        {
            "unit_key": unit.key,
            "version": committed.version,
            "content_hash": committed.content_hash,
            "word_count": committed.word_count,
        },
    )
    return UnitOutcome(unit.key, True, 1, None, report, coverage)


def _pause(
    runtime: Runtime,
    project_id: str,
    unit_key: str,
    reason: str,
    emit: Callable[[str, str, dict[str, Any]], None],
    *,
    from_state: str,
) -> None:
    """Pause a unit, recording why. Its content and findings are kept; nothing is committed."""
    if from_state == "validating":
        set_unit_state(runtime.storage, project_id=project_id, unit_key=unit_key, state="auditing")
    set_unit_state(
        runtime.storage,
        project_id=project_id,
        unit_key=unit_key,
        state="paused",
        paused_reason=reason,
    )
    emit("unit.paused", f"{unit_key}: {reason}", {"unit_key": unit_key, "reason": reason})


def _unit_id(runtime: Runtime, project_id: str, unit_key: str) -> str:
    """The unit's row identifier, for the attempt record."""
    from ideapress.services.units import load_unit

    with runtime.storage.read() as session:
        return load_unit(session, project_id, unit_key).id


def draft_body(
    runtime: Runtime, *, project_id: str, unit_keys: Sequence[str], resume: bool
) -> Callable[[StageTask], None]:
    """Build the stage body that runs the core loop over a selection of units.

    Args:
        runtime: The process's handles.
        project_id: Which project.
        unit_keys: The units to work on, in plan order.
        resume: Skip units that already have a committed version — workflows §9's
            ``--resume`` continuing from the first incomplete unit.

    Returns:
        The callable the runner executes.
    """

    def body(task: StageTask) -> None:
        database = runtime.storage
        sink = runtime.events

        def emit(event_type: str, message: str, data: dict[str, Any]) -> None:
            sink.emit(database, task.run_id, event_type=event_type, message=message, data=data)

        with database.read() as session:
            plan = load_plan(session, project_id)
            requirements = load_requirements(session, project_id)
            neighbours = committed_units(session, project_id)
            states = {
                row.unit_key: row.state
                for row in session.scalars(
                    select(UnitRow).where(UnitRow.project_id == project_id)
                ).all()
            }

        by_key = {requirement.key: requirement for requirement in requirements}
        ordinals = {unit.key: unit.ordinal for unit in plan.units}
        committed_count = 0
        paused_count = 0

        for unit in plan.units:
            if unit.key not in unit_keys:
                continue
            if resume and states.get(unit.key) == "committed":
                emit(
                    "unit.skipped",
                    f"{unit.key}: already committed",
                    {"unit_key": unit.key},
                )
                continue
            outcome = run_unit(
                runtime,
                task,
                project_id=project_id,
                unit=unit,
                requirements=[by_key[key] for key in unit.requirement_keys if key in by_key],
                neighbours=neighbours,
                ordinals=ordinals,
                emit=emit,
            )
            if outcome.committed:
                committed_count += 1
                with database.read() as session:
                    neighbours = committed_units(session, project_id)
            else:
                paused_count += 1
            _record_progress(runtime, task.run_id, committed_count, paused_count)

    return body


def _record_progress(runtime: Runtime, run_id: str, completed: int, paused: int) -> None:
    """Update the run's per-unit counters, so a task report is accurate while it runs."""
    from ideapress.infrastructure.db.models import StageRun as StageRunRow

    with runtime.storage.write() as session:
        run = session.get(StageRunRow, run_id)
        if run is not None:
            run.units_completed = completed
            run.units_paused = paused
