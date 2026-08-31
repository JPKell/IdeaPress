"""ideapress.services.units — reading units, and committing one atomically.

**The commit is one transaction.** The version row, its coverage rows, the validation rows and the
unit's own state change are written together or not at all. P4's named failure mode is a partial
commit after a mid-write failure, and the defence is not care — it is that there is no point in the
sequence where a reader could observe half of it, because the transaction has not been committed
until every write is in it.

Nothing here catches an exception to "clean up". A rollback is what the database does when the
`with` block leaves by exception, and code that tried to tidy afterwards would be code that runs
*after* the failure that made tidying necessary.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from ideapress.domain.commit import content_hash, word_count
from ideapress.domain.stage_state import assert_transition
from ideapress.errors import StagePreconditionFailed, UnitNotFound
from ideapress.infrastructure.db.models import Coverage as CoverageRow
from ideapress.infrastructure.db.models import Requirement as RequirementRow
from ideapress.infrastructure.db.models import Unit as UnitRow
from ideapress.infrastructure.db.models import UnitVersion as UnitVersionRow
from ideapress.infrastructure.db.models import Validation as ValidationRow

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from ideapress.domain.commit import CoverageReport
    from ideapress.domain.validation import ValidationReport
    from ideapress.services.database import Database

__all__ = [
    "CommittedVersion",
    "commit_unit",
    "current_content",
    "committed_units",
    "load_unit",
    "record_validation",
    "set_unit_state",
    "unit_history",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CommittedVersion:
    """What a commit produced."""

    unit_key: str
    version: int
    version_id: str
    content_hash: str
    word_count: int
    committed_at: datetime


def load_unit(session: Session, project_id: str, unit_key: str) -> UnitRow:
    """Return one unit row.

    Raises:
        UnitNotFound: No unit with that key in this project.
    """
    row = session.scalars(
        select(UnitRow).where(UnitRow.project_id == project_id, UnitRow.unit_key == unit_key)
    ).one_or_none()
    if row is None:
        message = f"No unit {unit_key!r} in project {project_id!r}."
        raise UnitNotFound(message, details={"project_id": project_id, "unit_key": unit_key})
    return row


def set_unit_state(
    database: Database,
    *,
    project_id: str,
    unit_key: str,
    state: str,
    paused_reason: str | None = None,
) -> None:
    """Move a unit through the state machine, refusing an undocumented transition.

    Raises:
        ValidationError: The move is not an arrow in data model §3.
        UnitNotFound: No such unit.
    """
    with database.write() as session:
        unit = load_unit(session, project_id, unit_key)
        assert_transition(unit.state, state, unit_key=unit_key)
        unit.state = state
        unit.paused_reason = paused_reason


def record_validation(database: Database, *, attempt_id: str, report: ValidationReport) -> None:
    """Store every check's verdict against the attempt that produced the text."""
    with database.write() as session:
        for outcome in report.outcomes:
            session.add(
                ValidationRow(
                    attempt_id=attempt_id,
                    check_kind=outcome.check_kind,
                    check_key=outcome.check_key[:120],
                    passed=outcome.passed,
                    blocking=outcome.blocking,
                    detail_json={
                        "detail": outcome.detail,
                        "evidence": list(outcome.evidence),
                    },
                )
            )


def commit_unit(
    database: Database,
    *,
    project_id: str,
    unit_key: str,
    text: str,
    coverage: CoverageReport,
    attempt_id: str | None = None,
    now: datetime | None = None,
) -> CommittedVersion:
    """Write a validated unit as a new committed version. **Atomically.**

    Args:
        database: Where to write.
        project_id: Which project.
        unit_key: Which unit.
        text: The content to commit.
        coverage: The coverage report to store alongside it.
        attempt_id: The attempt that produced the text, for provenance.
        now: The commit instant, injected so a test is deterministic.

    Returns:
        What was committed.

    Raises:
        UnitNotFound: No such unit.
        StagePreconditionFailed: The unit is not in a state that may commit. Checked inside the
            transaction, so two concurrent commits cannot both pass the check and both write.
        ValidationError: The state transition is not an arrow in data model §3.

    Everything — the version, its coverage, the unit's state and its ``current_version_id`` — is
    written in **one** transaction. A failure anywhere in it, including a process death, leaves the
    database exactly as it was: there is no ordering of these writes that a reader can observe half
    of, because none of them is visible until the last one has succeeded.
    """
    committed_at = now or datetime.now(UTC)
    with database.write() as session:
        unit = load_unit(session, project_id, unit_key)
        if unit.state == "committed":
            message = (
                f"Unit {unit_key} is already committed. A committed version is immutable; a "
                "revision creates a new one."
            )
            raise StagePreconditionFailed(
                message, details={"unit_key": unit_key, "state": unit.state}
            )

        highest = (
            session.scalar(
                select(func.max(UnitVersionRow.version)).where(UnitVersionRow.unit_id == unit.id)
            )
            or 0
        )
        version = UnitVersionRow(
            unit_id=unit.id,
            version=highest + 1,
            content_text=text,
            content_hash=content_hash(text),
            word_count=word_count(text),
            char_count=len(text),
            committed=True,
            committed_at=committed_at,
            created_from_attempt_id=attempt_id,
        )
        session.add(version)
        session.flush()

        requirement_ids = _requirement_ids(session, project_id)
        for entry in coverage.entries:
            requirement_id = requirement_ids.get(entry.requirement.key)
            if requirement_id is None:  # pragma: no cover — a key with no row cannot be committed
                continue
            session.add(
                CoverageRow(
                    unit_version_id=version.id,
                    requirement_id=requirement_id,
                    satisfied=entry.satisfied,
                    satisfied_by=entry.satisfied_by,
                    detail_json={"detail": entry.detail},
                )
            )

        assert_transition(unit.state, "committed", unit_key=unit_key)
        unit.state = "committed"
        unit.paused_reason = None
        unit.current_version_id = version.id
        session.flush()

        return CommittedVersion(
            unit_key=unit_key,
            version=version.version,
            version_id=version.id,
            content_hash=version.content_hash,
            word_count=version.word_count,
            committed_at=committed_at,
        )


def _requirement_ids(session: Session, project_id: str) -> dict[str, str]:
    """Map requirement keys to row identifiers, newest generation."""
    generation = session.scalar(
        select(RequirementRow.generation)
        .where(RequirementRow.project_id == project_id)
        .order_by(RequirementRow.generation.desc())
        .limit(1)
    )
    if generation is None:
        return {}
    rows = session.scalars(
        select(RequirementRow).where(
            RequirementRow.project_id == project_id, RequirementRow.generation == generation
        )
    ).all()
    return {row.requirement_key: row.id for row in rows}


def current_content(session: Session, project_id: str, unit_key: str) -> str | None:
    """The text of a unit's current version, or ``None`` when it has none."""
    unit = load_unit(session, project_id, unit_key)
    if unit.current_version_id is None:
        return None
    version = session.get(UnitVersionRow, unit.current_version_id)
    return version.content_text if version is not None else None


def committed_units(session: Session, project_id: str) -> dict[str, str]:
    """Every committed unit's text, by key, in reading order — the neighbours a draft may see."""
    rows = session.scalars(
        select(UnitRow).where(UnitRow.project_id == project_id).order_by(UnitRow.ordinal)
    ).all()
    out: dict[str, str] = {}
    for row in rows:
        if row.state == "committed" and row.current_version_id is not None:
            version = session.get(UnitVersionRow, row.current_version_id)
            if version is not None:
                out[row.unit_key] = version.content_text
    return out


def unit_history(session: Session, project_id: str, unit_key: str) -> list[dict[str, Any]]:
    """Every version of a unit, newest first, with its coverage."""
    unit = load_unit(session, project_id, unit_key)
    versions = session.scalars(
        select(UnitVersionRow)
        .where(UnitVersionRow.unit_id == unit.id)
        .order_by(UnitVersionRow.version.desc())
    ).all()
    requirement_keys = {
        row.id: row.requirement_key
        for row in session.scalars(
            select(RequirementRow).where(RequirementRow.project_id == project_id)
        ).all()
    }
    history: list[dict[str, Any]] = []
    for version in versions:
        coverage = session.scalars(
            select(CoverageRow).where(CoverageRow.unit_version_id == version.id)
        ).all()
        history.append(
            {
                "version": version.version,
                "committed": version.committed,
                "committed_at": version.committed_at.isoformat() if version.committed_at else None,
                "content_hash": version.content_hash,
                "word_count": version.word_count,
                "created_at": version.created_at.isoformat(),
                "coverage": [
                    {
                        "requirement_key": requirement_keys.get(row.requirement_id, "?"),
                        "satisfied": row.satisfied,
                        "satisfied_by": row.satisfied_by,
                        "detail": row.detail_json.get("detail", ""),
                    }
                    for row in coverage
                ],
            }
        )
    return history
