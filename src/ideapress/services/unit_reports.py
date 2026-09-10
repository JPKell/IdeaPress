"""ideapress.services.unit_reports — what the API and the unit page read.

Read-only shaping, kept out of the routes so the JSON and the page report the same thing by
construction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from ideapress.infrastructure.db.models import Attempt as AttemptRow
from ideapress.infrastructure.db.models import AuditFinding as AuditFindingRow
from ideapress.infrastructure.db.models import Critique as CritiqueRow
from ideapress.infrastructure.db.models import Source as SourceRow
from ideapress.infrastructure.db.models import ToolCallRecord as ToolCallRow
from ideapress.infrastructure.db.models import Unit as UnitRow
from ideapress.infrastructure.db.models import UnitVersion as UnitVersionRow
from ideapress.infrastructure.db.models import Validation as ValidationRow
from ideapress.services.egress import decision_view
from ideapress.services.plan import load_requirements
from ideapress.services.units import load_unit, unit_history

if TYPE_CHECKING:
    from ideapress.services.runtime import Runtime

__all__ = ["research_report", "unit_detail", "unit_list"]


def research_report(runtime: Runtime, *, project_id: str) -> dict[str, Any]:
    """The project's research notes and every tool call that produced or failed to produce one.

    Project-scoped rather than unit-scoped, and shown on the unit page anyway (row M1, ADR-0116):
    the `research` stage runs before any unit exists — workflows §2 puts it at position 2 and the
    plan at position 4 — so a research call has no unit to belong to. What a person reading a unit
    needs is which sources fed it and which fetches were refused, and both of those are facts
    about the project.

    Args:
        runtime: The process's handles.
        project_id: Which project.

    Returns:
        ``{"notes": [...], "tool_calls": [...]}``. Notes carry their citation, their digest and
        their length rather than their text — a unit page that inlined every fetched document
        would be unreadable, and the text is what the draft context already carried. Tool calls
        carry ToolYard's status, reason and detail, so a refusal is diagnosable from the page the
        way it is from the row (toolyard §11.2). Both lists are empty for every project that never
        ran the stage, which renders as the existing empty state.
    """
    with runtime.storage.read() as session:
        notes = session.scalars(
            select(SourceRow)
            .where(SourceRow.project_id == project_id)
            .order_by(SourceRow.created_at, SourceRow.id)
        ).all()
        calls = session.scalars(
            select(ToolCallRow)
            .where(ToolCallRow.project_id == project_id)
            .order_by(ToolCallRow.started_at, ToolCallRow.id)
        ).all()
    return {
        "notes": [
            {
                "kind": note.kind,
                "title": note.title,
                "citation": note.path or note.title,
                "sha256": note.sha256,
                "characters": len(note.content_text or ""),
            }
            for note in notes
        ],
        "tool_calls": [
            {
                "tool": call.tool_name,
                "status": call.status,
                "reason": call.reason,
                "detail": call.reason_detail,
                "duration_ms": call.duration_ms,
                "egress": call.egress,
                "started_at": call.started_at.isoformat(),
            }
            for call in calls
        ],
    }


def unit_list(runtime: Runtime, *, project_id: str) -> list[dict[str, Any]]:
    """Every unit with its state, current version and coverage summary."""
    with runtime.storage.read() as session:
        rows = session.scalars(
            select(UnitRow).where(UnitRow.project_id == project_id).order_by(UnitRow.ordinal)
        ).all()
        out: list[dict[str, Any]] = []
        for row in rows:
            version = (
                session.get(UnitVersionRow, row.current_version_id)
                if row.current_version_id
                else None
            )
            out.append(
                {
                    "unit_key": row.unit_key,
                    "ordinal": row.ordinal,
                    "title": row.title,
                    "goal": row.goal_text,
                    "state": row.state,
                    "paused_reason": row.paused_reason,
                    "requirement_keys": list(row.requirement_keys_json),
                    "version": version.version if version else None,
                    "word_count": version.word_count if version else None,
                    "content_hash": version.content_hash if version else None,
                }
            )
        return out


def unit_detail(runtime: Runtime, *, project_id: str, unit_key: str) -> dict[str, Any]:
    """One unit's content and complete provenance.

    Raises:
        UnitNotFound: No such unit.

    "Complete" is workflows §8's list: backend, model identity, prompt id, version and hash, usage,
    timing, outcome and degradations, per attempt — plus the validation report and the coverage,
    each naming what decided it.
    """
    with runtime.storage.read() as session:
        unit = load_unit(session, project_id, unit_key)
        version = (
            session.get(UnitVersionRow, unit.current_version_id)
            if unit.current_version_id
            else None
        )
        requirements = {r.key: r for r in load_requirements(session, project_id)}
        attempts = session.scalars(
            select(AttemptRow).where(AttemptRow.unit_id == unit.id).order_by(AttemptRow.created_at)
        ).all()
        validations = (
            session.scalars(
                select(ValidationRow).where(
                    ValidationRow.attempt_id == version.created_from_attempt_id
                )
            ).all()
            if version is not None and version.created_from_attempt_id
            else []
        )
        history = unit_history(session, project_id, unit_key)
        attempt_ids = [attempt.id for attempt in attempts]
        findings = (
            session.scalars(
                select(AuditFindingRow)
                .where(AuditFindingRow.attempt_id.in_(attempt_ids))
                .order_by(AuditFindingRow.created_at)
            ).all()
            if attempt_ids
            else []
        )
        critiques = (
            session.scalars(
                select(CritiqueRow)
                .where(CritiqueRow.attempt_id.in_(attempt_ids))
                .order_by(CritiqueRow.round)
            ).all()
            if attempt_ids
            else []
        )
        rounds_by_attempt = {attempt.id: attempt.round for attempt in attempts}
        unit_id = unit.id

    budget = runtime.storage.budget
    cost = budget.unit_cost(project_id=project_id, unit_id=unit_id) if budget is not None else None

    egress = runtime.storage.egress
    egress_by_attempt: dict[str, dict[str, Any]] = {}
    if egress is not None:
        for decision in egress.decisions(run_id=unit_id):
            egress_by_attempt[decision.request.source_ref] = decision_view(decision)

    return {
        "project_id": project_id,
        "unit_key": unit_key,
        "cost": cost,
        # Project-scoped, on the unit page on purpose: see `research_report` (row M1).
        "research": research_report(runtime, project_id=project_id),
        "title": unit.title,
        "goal": unit.goal_text,
        "state": unit.state,
        "paused_reason": unit.paused_reason,
        "content": version.content_text if version else "",
        "version": version.version if version else None,
        "content_hash": version.content_hash if version else None,
        "word_count": version.word_count if version else None,
        "committed_at": (
            version.committed_at.isoformat() if version and version.committed_at else None
        ),
        "requirements": [
            {
                "key": key,
                "text": requirements[key].text if key in requirements else "(unknown)",
                "blocking": requirements[key].blocking if key in requirements else True,
                "checks": (
                    requirements[key].describe_checks() if key in requirements else "unknown"
                ),
            }
            for key in unit.requirement_keys_json
        ],
        "validation": [
            {
                "kind": row.check_kind,
                "key": row.check_key,
                "passed": row.passed,
                "blocking": row.blocking,
                "detail": row.detail_json.get("detail", ""),
            }
            for row in validations
        ],
        "attempts": [
            {
                "stage": attempt.stage,
                "attempt": attempt.attempt,
                "round": attempt.round,
                "outcome": attempt.outcome,
                "backend": attempt.backend,
                "model_canonical_id": attempt.model_canonical_id,
                "prompt_id": attempt.prompt_id,
                "prompt_version": attempt.prompt_version,
                "prompt_sha256": attempt.prompt_sha256,
                "prompt_source": attempt.prompt_source,
                "response_hash": attempt.response_hash,
                "input_tokens": attempt.input_tokens,
                "output_tokens": attempt.output_tokens,
                "provider_ms": attempt.provider_ms,
                "degradations": list(attempt.degradations_json),
                "rejection_reason": attempt.rejection_reason,
                # LoadCoach only, and `None` everywhere else: the routing decision that chose this
                # model, so a person asking "why this model?" can reach the answer from the
                # attempt rather than from another application's logs (P7 AC2).
                "routing": dict(attempt.routing_json) if attempt.routing_json else None,
                "idempotency_key": attempt.idempotency_key,
                # The row J1 egress decision this attempt's own model call was evaluated under,
                # joined by reference (D7, D8) — `None` on data written before this row, or on an
                # installation with nothing attached.
                "egress": egress_by_attempt.get(attempt.id),
            }
            for attempt in attempts
        ],
        "history": history,
        "coverage": history[0]["coverage"] if history else [],
        "findings": [
            {
                "key": row.finding_key,
                "category": row.category,
                "severity": row.severity,
                "problem": row.problem_text,
                "evidence": row.evidence_text or "",
                "fix": row.required_fix_text or "",
                "uncertain": row.uncertain,
                "escalated": row.escalated,
                "stage": row.source_stage,
                "round": rounds_by_attempt.get(row.attempt_id, 0),
            }
            for row in findings
        ],
        "critiques": [
            {
                "round": row.round,
                "verdict": row.verdict,
                "rationale": row.rationale_text,
                "improvement_delta": row.improvement_delta,
                "stop_reason": row.stop_reason,
            }
            for row in critiques
        ],
    }
