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
from ideapress.errors import ContextLimitExceeded, StagePreconditionFailed
from ideapress.infrastructure.db.models import Unit as UnitRow
from ideapress.services.plan import load_plan, load_requirements
from ideapress.services.prompts import render
from ideapress.services.research import project_notes as project_notes_for
from ideapress.services.review_loop import run_review_loop
from ideapress.services.stages import record_attempt
from ideapress.services.units import (
    commit_unit,
    committed_units,
    record_validation,
    reset_orphaned_units,
    set_unit_state,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from ideapress.domain.commit import CoverageReport
    from ideapress.domain.plan import PlanUnit
    from ideapress.domain.requirements import Requirement
    from ideapress.domain.validation import ValidationReport
    from ideapress.services.runtime import Runtime
    from ideapress.services.stages import StageTask

__all__ = [
    "UnitOutcome",
    "draft_body",
    "output_budget_tokens",
    "revise_body",
    "revise_unit",
    "run_unit",
]

logger = logging.getLogger(__name__)

DRAFT_THINKING_FLOOR_TOKENS = 8192
"""Output tokens reserved for a reasoning model's thinking, before the answer's own budget.

Measured, not guessed: both of spec §12's default models spent more than 4 096 output tokens
reasoning before emitting a word on a short task, and returned an empty string when the allowance
ran out first."""


def output_budget_tokens(*, target_words: int | None, structured_output_tokens: int) -> int:
    """The output budget for a stage that writes unit text — draft, repair, revise.

    Args:
        target_words: The unit's target length, when the plan states one; 400 is assumed when it
            does not, which errs long because a truncated draft costs a repair attempt.
        structured_output_tokens: ``workflow.structured_output_tokens``, the configured
            reasoning allowance.

    Returns:
        A thinking floor plus four tokens per target word. The floor is the larger of the
        measured default (:data:`DRAFT_THINKING_FLOOR_TOKENS`) and the configured allowance —
        the M7 demonstration paused a unit whose draft exhausted the floor twice, with a message
        telling the user to raise the stage's output budget while no setting reached this stage.
        Raising ``workflow.structured_output_tokens`` in ``config.toml`` is now that lever for
        every text-writing stage too, not only the structured ones (M7 finding 1c).
    """
    return max(DRAFT_THINKING_FLOOR_TOKENS, structured_output_tokens) + (target_words or 400) * 4


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
    research_notes: Sequence[tuple[str, str]],
    emit: Callable[[str, str, dict[str, Any]], None],
    max_rounds: int | None = None,
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
        research_notes: The project's research notes as ``(title, text)`` pairs — workflows §7's
            "relevant research notes … budgeted, ranked by explicit reference", and the first
            thing dropped when the budget binds. Empty for every project that never ran the
            `research` stage, which is every project before 1.4 (row M1, ADR-0116).
        emit: Event emitter, taking ``(event_type, message, data)``.
        max_rounds: The run's ``max_revision_rounds`` override; ``None`` reads the setting.

    Returns:
        What happened to the unit.

    Raises:
        ContextLimitExceeded: The requirements alone exceed the context budget. Not caught here:
            the stage fails with the numbers rather than drafting against a truncated contract.
            A ``ContextLimitExceeded`` from a **model call** — the output budget exhausted twice
            with no text at all, or a review round's context overflowing — is different: that is
            one unit being hard to draft or hard to critique, and it **pauses the unit** with the
            stage and the budget in the reason rather than aborting the stage, so the units after
            it still run (M7 finding 1).
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
            research_notes=research_notes,
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
        if context.report is not None:
            emit(
                "context.compacted",
                f"{unit.key}: {stage} attempt {attempt} dropped {', '.join(context.dropped)}",
                context.report.to_dict(),
            )
        emit(
            "attempt.started",
            f"{unit.key}: {stage} attempt {attempt} of {limit}",
            {"unit_key": unit.key, "stage": stage, "attempt": attempt},
        )
        try:
            result = gateway.run(
                StageRequest(
                    stage=stage,
                    system=prompt.system or "",
                    user=prompt.user,
                    limits=StageLimits(
                        temperature=0.4 if not is_repair else 0.2,
                        max_output_tokens=output_budget_tokens(
                            target_words=unit.target_words,
                            structured_output_tokens=settings.workflow.structured_output_tokens,
                        ),
                    ),
                    correlation=Correlation(
                        project_id=project_id, unit_id=unit.key, attempt=attempt
                    ),
                    prompt_id=prompt.prompt_id,
                    prompt_version=prompt.version,
                    prompt_sha256=prompt.sha256,
                )
            )
        except ContextLimitExceeded as exc:
            # One unit exhausting a model's output budget is that unit's problem, not the
            # stage's: pause it with the numbers and let the loop reach the units after it.
            reason = f"the {stage!r} stage exhausted its output budget: {exc.message}"
            _pause(runtime, project_id, unit.key, reason, emit, from_state="drafting")
            return UnitOutcome(unit.key, False, attempt, reason, report, None)
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
    return _review_and_commit(
        runtime,
        task,
        project_id=project_id,
        unit=unit,
        requirements=requirements,
        text=text,
        report=report,
        attempt_id=attempt_id,
        emit=emit,
        attempts=limit,
        edited=limit > 1,
        max_rounds=max_rounds,
    )


def _review_and_commit(  # noqa: PLR0913 — the unit, its text, and how it got here
    runtime: Runtime,
    task: StageTask,
    *,
    project_id: str,
    unit: PlanUnit,
    requirements: Sequence[Requirement],
    text: str,
    report: ValidationReport,
    attempt_id: str | None,
    emit: Callable[[str, str, dict[str, Any]], None],
    attempts: int,
    edited: bool,
    max_rounds: int | None,
    first_round: int = 0,
    unchanged_from: str | None = None,
) -> UnitOutcome:
    """Review a unit in ``validating``, then commit it, pause it, or leave its version current.

    The half of the loop a draft and a revision share (row WP5): ``validating → auditing``, the
    bounded review loop, coverage, the commit decision and the commit.

    Args:
        runtime: The process's handles.
        task: The running stage, for cancellation checkpoints.
        project_id: Which project.
        unit: The unit's plan entry.
        requirements: The requirements it carries.
        text: The text that passed validation.
        report: Its validation report.
        attempt_id: The attempt that produced it, for the committed version's provenance.
        emit: Event emitter.
        attempts: The model attempts a paused outcome reports.
        edited: Whether the text was edited after its first attempt, for the backend's feedback.
        max_rounds: The run's ``max_revision_rounds`` override, or ``None`` for the setting.
        first_round: Revision rounds already spent: ``1`` after a revision's instruction round,
            which counts against the limit like any other.
        unchanged_from: The committed text a revision started from. When the review hands back
            exactly that, the unit returns to ``committed`` with its version current, rather than
            committing an identical second version.

    Returns:
        What happened to the unit.

    Raises:
        StageCancelled: The user cancelled at a model-call boundary.
    """
    database = runtime.storage
    settings = runtime.settings
    set_unit_state(database, project_id=project_id, unit_key=unit.key, state="auditing")
    try:
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
            max_rounds=max_rounds,
            first_round=first_round,
        )
    except ContextLimitExceeded as exc:
        # The M7 blocker: a critique whose model returned nothing twice at the structured-output
        # budget aborted the whole stage and left the unit wedged mid-review. A review budget
        # exhausted on one unit pauses that unit — the reason names the stage and the budget
        # (both are in the message, with the numbers in the details) — and the loop moves on.
        reason = f"the review of this unit exhausted an output budget: {exc.message}"
        _pause(runtime, project_id, unit.key, reason, emit, from_state="auditing")
        return UnitOutcome(unit.key, False, attempts, reason, report, None)
    if unchanged_from is not None and review.text == unchanged_from:
        set_unit_state(database, project_id=project_id, unit_key=unit.key, state="committed")
        emit(
            "unit.unchanged",
            f"{unit.key}: the review changed nothing; its committed version stays current",
            {"unit_key": unit.key},
        )
        return UnitOutcome(unit.key, True, attempts, None, review.validation, None)
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
        audit_gating_allowed=settings.workflow.allow_audit_gated_requirements,
    )
    if not decision.allowed:
        _pause(runtime, project_id, unit.key, decision.refusal, emit, from_state="auditing")
        return UnitOutcome(unit.key, False, attempts, decision.refusal, report, coverage)

    committed = commit_unit(
        database,
        project_id=project_id,
        unit_key=unit.key,
        text=text,
        coverage=coverage,
        attempt_id=attempt_id,
    )
    # Requirements no deterministic check settled — the commit event says so out loud, because a
    # reader of the event stream must be able to tell a mechanical guarantee from a model's
    # review without opening the coverage report (M7-20 interim safeguard; ADR-0039 decides the
    # gate itself).
    model_guaranteed = sorted(entry.requirement.key for entry in coverage.model_assisted)
    blocking_guaranteed = sorted(
        entry.requirement.key for entry in coverage.model_assisted if entry.requirement.blocking
    )
    message = f"{unit.key}: version {committed.version} committed ({committed.word_count} words)"
    if blocking_guaranteed:
        message += (
            f" — {', '.join(blocking_guaranteed)} guaranteed by model review, "
            "not a deterministic check"
        )
    emit(
        "unit.committed",
        message,
        {
            "unit_key": unit.key,
            "version": committed.version,
            "content_hash": committed.content_hash,
            "word_count": committed.word_count,
            "model_guaranteed_requirements": model_guaranteed,
        },
    )
    _report_to_backend(
        runtime,
        project_id=project_id,
        unit_key=unit.key,
        validation_passed=report.passed if report is not None else None,
        edited=edited,
        emit=emit,
    )
    return UnitOutcome(unit.key, True, 1, None, report, coverage)


def _report_to_backend(
    runtime: Runtime,
    *,
    project_id: str,
    unit_key: str,
    validation_passed: bool | None,
    edited: bool,
    emit: Callable[[str, str, dict[str, Any]], None],
) -> None:
    """Tell the backend how its work turned out, once, after the unit is safely committed.

    Only LoadCoach has anywhere to send this; every other adapter is skipped without a call. It is
    deliberately after the commit and deliberately unable to fail it: the unit is already written,
    and a report that could not be delivered is a degradation to note, never a reason to undo
    finished work (P7 AC4).
    """
    from ideapress.services.feedback import send_unit_feedback

    backend = runtime.backend
    if backend is None:
        return
    outcome = send_unit_feedback(
        runtime.storage,
        backend,
        project_id=project_id,
        unit_key=unit_key,
        accepted=True,
        validation_passed=validation_passed,
        edited=edited,
    )
    if outcome.sent:
        emit(
            "unit.feedback_sent",
            f"{unit_key}: feedback posted to the backend for {outcome.posted_count} job(s)",
            {"unit_key": unit_key, "job_ids": list(outcome.sent)},
        )
    for job_id, reason in outcome.failed:
        emit(
            "unit.feedback_failed",
            f"{unit_key}: the backend would not accept feedback for job {job_id}: {reason}",
            {"unit_key": unit_key, "job_id": job_id},
        )


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
    runtime: Runtime,
    *,
    project_id: str,
    unit_keys: Sequence[str],
    resume: bool,
    overrides: Mapping[str, Any] | None = None,
) -> Callable[[StageTask], None]:
    """Build the stage body that runs the core loop over a selection of units.

    Args:
        runtime: The process's handles.
        project_id: Which project.
        unit_keys: The units to work on, in plan order.
        resume: Skip units that already have a committed version — workflows §9's
            ``--resume`` continuing from the first incomplete unit.
        overrides: The run's checked overrides. This body reads ``max_revision_rounds``; the
            gateway reads ``model_hint`` for every call of the run.

    Returns:
        The callable the runner executes. Every run it makes, resumed or not, first moves any unit
        an earlier run left mid-flight back to ``paused``
        (:func:`~ideapress.services.units.reset_orphaned_units`), so neither a crash mid-review
        nor a stage that failed before a unit's first attempt can wedge the project.
    """

    def body(task: StageTask) -> None:
        database = runtime.storage
        sink = runtime.events

        def emit(event_type: str, message: str, data: dict[str, Any]) -> None:
            sink.emit(database, task.run_id, event_type=event_type, message=message, data=data)

        # A crash — or a stage that failed before a unit's first attempt, such as a binding naming
        # a model the backend lacks (row WI1) — can leave a unit mid-flight ('drafting'…
        # 'revising'), from which no arrow leads back into the loop. Before reading states, move
        # each orphan to 'paused', resumed run or not — but only when the run that owned it is
        # demonstrably gone (M7 finding 1b).
        _reset_orphans(runtime, project_id, task.run_id, emit)

        with database.read() as session:
            plan = load_plan(session, project_id)
            requirements = load_requirements(session, project_id)
            neighbours = committed_units(session, project_id)
            # Read once for the whole stage: the `research` stage does not run concurrently with
            # this one (one stage task per project), so the notes cannot change under the loop.
            research_notes = project_notes_for(runtime, project_id)
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
                research_notes=research_notes,
                emit=emit,
                max_rounds=(overrides or {}).get("max_revision_rounds"),
            )
            if outcome.committed:
                committed_count += 1
                with database.read() as session:
                    neighbours = committed_units(session, project_id)
            else:
                paused_count += 1
            _record_progress(runtime, task.run_id, committed_count, paused_count)

    return body


def _reset_orphans(
    runtime: Runtime,
    project_id: str,
    run_id: str,
    emit: Callable[[str, str, dict[str, Any]], None],
) -> None:
    """Move every unit a gone run left mid-flight back to ``paused``, saying so (M7 finding 1b)."""
    for unit_key, previous in reset_orphaned_units(
        runtime.storage, project_id=project_id, active_run_id=run_id
    ):
        emit(
            "unit.reset",
            f"{unit_key}: an earlier run left it in '{previous}'; reset to paused",
            {"unit_key": unit_key, "previous_state": previous},
        )


def revise_body(
    runtime: Runtime,
    *,
    project_id: str,
    unit_keys: Sequence[str],
    overrides: Mapping[str, Any] | None = None,
) -> Callable[[StageTask], None]:
    """Build the stage body that revises units into new versions (row WP5).

    Data model §3's ``committed → revising`` ("explicit user revision, creates a new version") and
    ``paused → revising`` ("user resumes with instructions"). Before this body a revision started
    a draft run, whose first move, ``committed → drafting``, is no arrow at all, so every revision
    of a committed unit failed its stage.

    Args:
        runtime: The process's handles.
        project_id: Which project.
        unit_keys: The units to revise, in plan order. A unit with no committed version, or one
            neither ``committed`` nor ``paused``, is skipped with a ``unit.skipped`` event naming
            why: there is nothing to revise, and drafting it is the draft stage's work.
        overrides: The run's checked overrides: ``instructions`` (the author's words) and
            ``max_revision_rounds``.

    Returns:
        The callable the runner executes. Like the draft body, it first returns any unit an earlier
        run left mid-flight to ``paused``.
    """
    options = dict(overrides or {})

    def body(task: StageTask) -> None:
        database = runtime.storage

        def emit(event_type: str, message: str, data: dict[str, Any]) -> None:
            runtime.events.emit(
                database, task.run_id, event_type=event_type, message=message, data=data
            )

        _reset_orphans(runtime, project_id, task.run_id, emit)
        with database.read() as session:
            plan = load_plan(session, project_id)
            by_key = {
                requirement.key: requirement
                for requirement in load_requirements(session, project_id)
            }
            current = {
                row.unit_key: (row.state, row.current_version_id)
                for row in session.scalars(
                    select(UnitRow).where(UnitRow.project_id == project_id)
                ).all()
            }

        committed_count = 0
        paused_count = 0
        for unit in plan.units:
            if unit.key not in unit_keys:
                continue
            state, version_id = current.get(unit.key, ("planned", None))
            if version_id is None or state not in {"committed", "paused"}:
                emit(
                    "unit.skipped",
                    f"{unit.key}: no committed version to revise (it is {state!r}); draft it first",
                    {"unit_key": unit.key, "state": state},
                )
                continue
            outcome = revise_unit(
                runtime,
                task,
                project_id=project_id,
                unit=unit,
                requirements=[by_key[key] for key in unit.requirement_keys if key in by_key],
                instructions=str(options.get("instructions") or ""),
                max_rounds=options.get("max_revision_rounds"),
                emit=emit,
            )
            if outcome.committed:
                committed_count += 1
            else:
                paused_count += 1
            _record_progress(runtime, task.run_id, committed_count, paused_count)

    return body


def revise_unit(
    runtime: Runtime,
    task: StageTask,
    *,
    project_id: str,
    unit: PlanUnit,
    requirements: Sequence[Requirement],
    instructions: str,
    max_rounds: int | None,
    emit: Callable[[str, str, dict[str, Any]], None],
) -> UnitOutcome:
    """Revise one unit that has a committed version: the author's instructions, then review.

    Args:
        runtime: The process's handles.
        task: The running stage, for cancellation checkpoints.
        project_id: Which project.
        unit: The unit's plan entry; it must have a committed version.
        requirements: The requirements it carries.
        instructions: The author's words, or ``""``. When given, one ``revise`` attempt carries
            them to the reviser as a finding, against the committed text; that round counts
            against the round limit. Without them the committed text goes straight to review.
        max_rounds: The run's ``max_revision_rounds`` override, or ``None`` for the setting.
        emit: Event emitter.

    Returns:
        What happened. The unit **pauses with its committed version kept** when the instruction
        round raises validation failures above the version it revised (the review loop's own
        regression rule), when the reviser declines, or when its output budget runs out; the
        review loop's own outcomes follow otherwise. A review that changes nothing returns the
        unit to ``committed`` with no second version.

    Raises:
        StageCancelled: The user cancelled at a model-call boundary.
    """
    from ideapress.domain.audit import AuditFinding
    from ideapress.domain.revision_policy import RoundMeasurement, rejects_regression
    from ideapress.infrastructure.db.models import UnitVersion as UnitVersionRow
    from ideapress.services.review import run_revision
    from ideapress.services.review_loop import _validate
    from ideapress.services.units import load_unit

    settings = runtime.settings
    database = runtime.storage
    with database.read() as session:
        row = load_unit(session, project_id, unit.key)
        version = session.get(UnitVersionRow, row.current_version_id)
        if version is None:  # pragma: no cover — the body skips a unit with no version
            message = f"Unit {unit.key} has no committed version to revise."
            raise StagePreconditionFailed(message, details={"unit_key": unit.key})
        current_text, number, attempt_id = (
            version.content_text,
            version.version,
            version.created_from_attempt_id,
        )
    kept = f"version {number} is kept"
    emit("unit.started", f"{unit.key}: revising version {number}", {"unit_key": unit.key})
    set_unit_state(database, project_id=project_id, unit_key=unit.key, state="revising")
    text = current_text
    report = _validate(current_text, unit, requirements)
    first_round = 0

    if instructions:
        runtime.runner.checkpoint(task)
        context = assemble_context(
            unit=unit,
            requirements=requirements,
            budget_tokens=settings.workflow.context_budget_tokens,
            previous_findings=f"- {instructions}",
        )
        emit(
            "attempt.started",
            f"{unit.key}: revise attempt 1 of 1, on the author's instructions",
            {"unit_key": unit.key, "stage": "revise", "attempt": 1},
        )
        instruction = AuditFinding(
            key="A-001",
            category="author_instruction",
            severity="major",
            problem_text=instructions,
            source_stage="author",
        )
        try:
            revision = run_revision(
                runtime.runner.gateway,
                project_id=project_id,
                unit_key=unit.key,
                context=context.render(),
                content=current_text,
                findings=(instruction,),
                round_number=1,
                max_output_tokens=output_budget_tokens(
                    target_words=unit.target_words,
                    structured_output_tokens=settings.workflow.structured_output_tokens,
                ),
            )
        except ContextLimitExceeded as exc:
            reason = f"the revision exhausted its output budget: {exc.message}; {kept}"
            _pause(runtime, project_id, unit.key, reason, emit, from_state="revising")
            return UnitOutcome(unit.key, False, 1, reason, report, None)
        attempt_id = record_attempt(
            database,
            stage_run_id=task.run_id,
            stage="revise",
            result=revision.result,
            unit_id=_unit_id(runtime, project_id, unit.key),
            attempt=1,
            round_=1,
            prompt_id=revision.prompt_id,
            prompt_version=revision.prompt_version,
            prompt_sha256=revision.prompt_sha256,
            outcome="content_rejected" if revision.result.refused else "completed",
            store_content=runtime.runner.store_content,
        )
        if revision.result.refused:
            declined = revision.result.refusal_reason or "the model declined the task"
            reason = f"{declined}; {kept}"
            _pause(runtime, project_id, unit.key, reason, emit, from_state="revising")
            return UnitOutcome(unit.key, False, 1, reason, report, None)
        proposed = _validate(revision.text, unit, requirements)
        record_validation(database, attempt_id=attempt_id, report=proposed)
        emit(
            "validation.completed",
            f"{unit.key}: {proposed.summary()}",
            {"unit_key": unit.key, "attempt": 1, "passed": proposed.passed},
        )
        if rejects_regression(
            RoundMeasurement(
                round_number=0, validation_failures=report.failure_count, audit_findings=0
            ),
            RoundMeasurement(
                round_number=1, validation_failures=proposed.failure_count, audit_findings=0
            ),
        ):
            reason = (
                f"the revision raised validation failures from {report.failure_count} to "
                f"{proposed.failure_count}; {kept}"
            )
            _pause(runtime, project_id, unit.key, reason, emit, from_state="revising")
            return UnitOutcome(unit.key, False, 1, reason, proposed, None)
        text, report, first_round = revision.text, proposed, 1

    runtime.runner.checkpoint(task)
    set_unit_state(database, project_id=project_id, unit_key=unit.key, state="validating")
    return _review_and_commit(
        runtime,
        task,
        project_id=project_id,
        unit=unit,
        requirements=requirements,
        text=text,
        report=report,
        attempt_id=attempt_id,
        emit=emit,
        attempts=first_round,
        edited=True,
        max_rounds=max_rounds,
        first_round=first_round,
        unchanged_from=current_text,
    )


def _record_progress(runtime: Runtime, run_id: str, completed: int, paused: int) -> None:
    """Update the run's per-unit counters, so a task report is accurate while it runs."""
    from ideapress.infrastructure.db.models import StageRun as StageRunRow

    with runtime.storage.write() as session:
        run = session.get(StageRunRow, run_id)
        if run is not None:
            run.units_completed = completed
            run.units_paused = paused
