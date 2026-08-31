"""ideapress.services.unit_reports — what the API and the unit page read.

Read-only shaping, kept out of the routes so the JSON and the page report the same thing by
construction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from ideapress.infrastructure.db.models import Attempt as AttemptRow
from ideapress.infrastructure.db.models import Unit as UnitRow
from ideapress.infrastructure.db.models import UnitVersion as UnitVersionRow
from ideapress.infrastructure.db.models import Validation as ValidationRow
from ideapress.services.plan import load_requirements
from ideapress.services.units import load_unit, unit_history

if TYPE_CHECKING:
    from ideapress.services.runtime import Runtime

__all__ = ["unit_detail", "unit_list"]


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

    return {
        "project_id": project_id,
        "unit_key": unit_key,
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
                "response_hash": attempt.response_hash,
                "input_tokens": attempt.input_tokens,
                "output_tokens": attempt.output_tokens,
                "provider_ms": attempt.provider_ms,
                "degradations": list(attempt.degradations_json),
                "rejection_reason": attempt.rejection_reason,
            }
            for attempt in attempts
        ],
        "history": history,
        "coverage": history[0]["coverage"] if history else [],
    }
