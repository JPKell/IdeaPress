"""ideapress.services.export — assembling a committed project and writing it to disk.

The assembly is the half that has to be deterministic: every collection is ordered explicitly,
nothing is read from a clock, and the only timestamps are the units' own ``committed_at``.

Writes are UTF-8 with ``\\n`` endings on every platform, because a file that differs by line ending
between Linux and Windows is a file that fails spec §11's contract 4 on the second machine
(risk P2).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import select

from ideapress.domain.exporters.html import render_html
from ideapress.domain.exporters.json import render_json
from ideapress.domain.exporters.markdown import render_markdown
from ideapress.domain.exporters.model import (
    EXPORT_FORMAT_VERSION,
    ExportDocument,
    ExportUnit,
    IncompleteUnit,
    RequirementCoverage,
    UnitProvenance,
)
from ideapress.errors import ExportFailed
from ideapress.infrastructure.db.models import Attempt as AttemptRow
from ideapress.infrastructure.db.models import AuditFinding as AuditFindingRow
from ideapress.infrastructure.db.models import Coverage as CoverageRow
from ideapress.infrastructure.db.models import Critique as CritiqueRow
from ideapress.infrastructure.db.models import Export as ExportRow
from ideapress.infrastructure.db.models import Requirement as RequirementRow
from ideapress.infrastructure.db.models import Unit as UnitRow
from ideapress.infrastructure.db.models import UnitVersion as UnitVersionRow
from ideapress.services.plan import load_requirements

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sqlalchemy.orm import Session

    from ideapress.domain.requirements import Requirement
    from ideapress.services.runtime import Runtime

__all__ = [
    "FORMATS",
    "build_document",
    "export_project",
    "refuse_partial_export",
    "render",
]

logger = logging.getLogger(__name__)

FORMATS: Final[dict[str, str]] = {"markdown": "md", "html": "html", "json": "json"}
"""The three shipped at 1.0, and their file extensions."""

_RENDERERS: Final[dict[str, Callable[[ExportDocument], str]]] = {
    "markdown": render_markdown,
    "html": render_html,
    "json": render_json,
}


def _coverage_entry(
    entry: CoverageRow, requirement_keys: dict[str, str], requirements: dict[str, Requirement]
) -> RequirementCoverage:
    """Resolve one stored coverage row against its requirement, source and quote included.

    The source travels because it is the fabrication-detection evidence (risk T6): the exported
    coverage section shows the claim and the verbatim quote that grounds it side by side, exactly
    as the live views do (M7 finding 2). A row whose requirement cannot be resolved — a dangling
    identifier, which cannot be produced by a commit — renders with empty text and source rather
    than being dropped, because a missing row would hide that something is wrong.
    """
    key = requirement_keys.get(entry.requirement_id, "?")
    requirement = requirements.get(key)
    return RequirementCoverage(
        key=key,
        text=requirement.text if requirement else "",
        blocking=requirement.blocking if requirement else True,
        satisfied=entry.satisfied,
        satisfied_by=entry.satisfied_by,
        detail=entry.detail_json.get("detail", ""),
        checks=requirement.describe_checks() if requirement else "",
        source_document=requirement.source.document if requirement else "",
        source_quote=requirement.source.quote if requirement else "",
        source_anchor=requirement.source.anchor if requirement else None,
    )


def _findings_for(session: Session, attempt_ids: Sequence[str]) -> tuple[dict[str, Any], ...]:
    """Every audit finding recorded against a unit's attempts, newest last.

    `ExportUnit.findings` has existed since the exporters were written and nothing ever filled it,
    so a unit that committed carrying unresolved `major` findings — because the review loop stopped
    on `diminishing_returns` rather than because they were fixed — exported as though it had none.
    """
    if not attempt_ids:
        return ()
    rows = session.scalars(
        select(AuditFindingRow)
        .where(AuditFindingRow.attempt_id.in_(list(attempt_ids)))
        .order_by(AuditFindingRow.created_at, AuditFindingRow.id)
    ).all()
    return tuple(
        {
            "key": row.finding_key,
            "category": row.category,
            "severity": row.severity,
            "problem": row.problem_text,
            "evidence": row.evidence_text,
            "required_fix": row.required_fix_text,
            "stage": row.source_stage,
            "escalated": bool(row.escalated),
            "uncertain": bool(row.uncertain),
        }
        for row in rows
    )


def _critiques_for(session: Session, attempt_ids: Sequence[str]) -> tuple[dict[str, Any], ...]:
    """Every critique verdict on a unit's attempts, newest last.

    The last one is the verdict the unit committed under. M8 observed a unit commit with a final
    verdict of `materially_deficient` and a stop reason of `diminishing_returns` — the review gave
    up rather than succeeded — and no export said so.
    """
    if not attempt_ids:
        return ()
    rows = session.scalars(
        select(CritiqueRow)
        .where(CritiqueRow.attempt_id.in_(list(attempt_ids)))
        .order_by(CritiqueRow.created_at, CritiqueRow.id)
    ).all()
    return tuple(
        {
            "verdict": row.verdict,
            "rationale": row.rationale_text,
            "round": row.round,
            "stop_reason": row.stop_reason,
        }
        for row in rows
    )


def build_document(runtime: Runtime, *, project_id: str) -> ExportDocument:
    """Assemble a committed project into the rendering model.

    Args:
        runtime: The process's handles.
        project_id: Which project.

    Returns:
        The document. **Only committed units appear**: an export is of the work that passed its
        gates, and including a paused draft would put unvalidated text into a file the user is
        entitled to trust.

    Raises:
        ProjectNotFound: No such project.
    """
    project = runtime.projects.get(project_id)
    with runtime.storage.read() as session:
        requirements = {r.key: r for r in load_requirements(session, project_id)}
        requirement_keys = {
            row.id: row.requirement_key
            for row in session.scalars(
                select(RequirementRow).where(RequirementRow.project_id == project_id)
            ).all()
        }
        units: list[ExportUnit] = []
        incomplete: list[IncompleteUnit] = []
        rows = session.scalars(
            select(UnitRow).where(UnitRow.project_id == project_id).order_by(UnitRow.ordinal)
        ).all()
        for row in rows:
            if row.state != "committed" or row.current_version_id is None:
                # Its content stays out — an export is of work that passed its gates. Its
                # existence does not: a planned unit that never committed, and the requirements
                # it owed, are what a reader needs to know the document is partial (M8-21).
                incomplete.append(
                    IncompleteUnit(
                        key=row.unit_key,
                        ordinal=row.ordinal,
                        title=row.title,
                        goal=row.goal_text,
                        state=row.state,
                        reason=str(row.paused_reason or ""),
                        requirement_keys=tuple(row.requirement_keys_json or ()),
                    )
                )
                continue
            version = session.get(UnitVersionRow, row.current_version_id)
            if version is None:  # pragma: no cover — a dangling current_version_id
                continue
            coverage_rows = session.scalars(
                select(CoverageRow).where(CoverageRow.unit_version_id == version.id)
            ).all()
            attempts = session.scalars(
                select(AttemptRow)
                .where(AttemptRow.unit_id == row.id)
                .order_by(AttemptRow.created_at)
            ).all()
            units.append(
                ExportUnit(
                    key=row.unit_key,
                    ordinal=row.ordinal,
                    title=row.title,
                    goal=row.goal_text,
                    content=version.content_text,
                    version=version.version,
                    content_hash=version.content_hash,
                    word_count=version.word_count,
                    committed_at=(version.committed_at.isoformat() if version.committed_at else ""),
                    coverage=tuple(
                        sorted(
                            (
                                _coverage_entry(entry, requirement_keys, requirements)
                                for entry in coverage_rows
                            ),
                            key=lambda entry: entry.key,
                        )
                    ),
                    findings=_findings_for(session, [a.id for a in attempts]),
                    critiques=_critiques_for(session, [a.id for a in attempts]),
                    provenance=tuple(
                        UnitProvenance(
                            stage=attempt.stage,
                            attempt=attempt.attempt,
                            round=attempt.round,
                            outcome=attempt.outcome,
                            backend=attempt.backend,
                            model_canonical_id=attempt.model_canonical_id,
                            prompt_id=attempt.prompt_id,
                            prompt_version=attempt.prompt_version,
                            prompt_sha256=attempt.prompt_sha256,
                            response_hash=attempt.response_hash,
                            input_tokens=attempt.input_tokens,
                            output_tokens=attempt.output_tokens,
                            provider_ms=attempt.provider_ms,
                            degradations=tuple(sorted(attempt.degradations_json)),
                        )
                        for attempt in attempts
                    ),
                )
            )

    return ExportDocument(
        project_id=project.id,
        title=project.title,
        slug=project.slug,
        content_type=project.content_type,
        content_type_version=project.content_type_version,
        workflow_id=project.workflow_id,
        workflow_version=project.workflow_version,
        brief=project.brief_text,
        units=tuple(units),
        format_version=EXPORT_FORMAT_VERSION,
        planned_units=len(rows),
        incomplete_units=tuple(incomplete),
    )


def render(document: ExportDocument, fmt: str) -> str:
    """Render a document in one format.

    Raises:
        ExportFailed: ``fmt`` is not a shipped format.
    """
    renderer = _RENDERERS.get(fmt)
    if renderer is None:
        message = f"{fmt!r} is not an export format. Available: {', '.join(sorted(FORMATS))}."
        raise ExportFailed(message, details={"format": fmt, "available": sorted(FORMATS)})
    return renderer(document)


def refuse_partial_export(document: ExportDocument, *, allow_partial: bool) -> None:
    """Refuse an export of a plan that is not fully committed, unless it was asked for.

    Args:
        document: The assembled document.
        allow_partial: Whether the caller opted in.

    Raises:
        ExportFailed: The plan has uncommitted units and ``allow_partial`` is not set.

    A project with **nothing** committed has always refused. A project with *some* of its plan
    committed silently succeeded, dropping the uncommitted units and the requirements they owed —
    so the honest case was the one nobody hits and the quiet one was the common one. Both refuse
    now, and the opt-in produces a file that says on its face what it is missing.
    """
    if not document.units or document.is_complete or allow_partial:
        return
    missing = ", ".join(
        f"{u.key} ({u.state})" for u in sorted(document.incomplete_units, key=lambda u: u.ordinal)
    )
    several = len(document.incomplete_units) != 1
    message = (
        f"This project's plan is not fully committed: {len(document.units)} of "
        f"{document.planned_units} units are committed, and {missing} "
        f"{'are' if several else 'is'} not. "
        f"Finish {'them' if several else 'it'} "
        "(`ideapress stage run <id> draft --resume`), or pass --allow-partial to export what "
        "there is — the file will say it is incomplete and list the requirements nothing answers."
    )
    raise ExportFailed(
        message,
        details={
            "project_id": document.project_id,
            "committed_units": len(document.units),
            "planned_units": document.planned_units,
            "incomplete": [u.key for u in document.incomplete_units],
            "remedy": "finish the units, or pass --allow-partial",
        },
    )


def export_project(
    runtime: Runtime, *, project_id: str, fmt: str, allow_partial: bool = False
) -> dict[str, object]:
    """Render a committed project and write it into the project's own directory.

    Args:
        runtime: The process's handles.
        project_id: Which project.
        fmt: ``markdown``, ``html`` or ``json``.
        allow_partial: Export a project whose plan is not fully committed. Off by default: a
            project with **nothing** committed has always been refused, and a project with *some*
            of its plan committed silently succeeded — so the empty case was honest and the
            partial one was not. Both now refuse; this is the opt-in, and what it produces says
            on its face that it is incomplete.

    Returns:
        What was written: the path, its hash, its size and the versions it covers.

    Raises:
        ExportFailed: The format is unknown, the project has no committed unit, the plan is not
            fully committed and ``allow_partial`` is not set, or the file could not be written.

    The path comes from the project's **slug**, which IdeaPress generated and validated — never
    from a title and never from anything a model produced (risk S2).
    """
    import hashlib

    document = build_document(runtime, project_id=project_id)
    refuse_partial_export(document, allow_partial=allow_partial)
    if not document.units:
        message = (
            "This project has no committed units, so there is nothing to export. Run the draft "
            "stage first; a paused unit is deliberately not exported."
        )
        raise ExportFailed(message, details={"project_id": project_id})

    text = render(document, fmt)
    project = runtime.projects.get(project_id)
    directory = runtime.projects.directory(project)
    path = directory / f"{project.slug}.{FORMATS[fmt]}"
    try:
        # newline="\n" on every platform: a file that differs by line ending between two machines
        # fails the byte-stability contract on the second one (risk P2).
        with Path(path).open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    except OSError as exc:
        message = f"Could not write {path}: {exc}"
        raise ExportFailed(message, details={"path": str(path), "format": fmt}) from exc

    # The **file's own** digest, over the bytes on disk, so `sha256sum <file>` reproduces it.
    # BaseAiCore's `sha256_of` hashes a value's canonical JSON, which is the right thing for a
    # structure and the wrong thing here: a reader who runs `sha256sum` to check an export against
    # its record must get the same number, and a hash they cannot reproduce is a hash they cannot
    # use.
    digest = f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"
    size = path.stat().st_size
    with runtime.storage.write() as session:
        session.add(
            ExportRow(
                project_id=project_id,
                format=fmt,
                path=str(path),
                sha256=digest,
                size_bytes=size,
                unit_version_ids_json=list(document.unit_version_ids),
                export_format_version=document.format_version,
            )
        )
    return {
        "format": fmt,
        "path": str(path),
        "sha256": digest,
        "size_bytes": size,
        "units": len(document.units),
        "export_format_version": document.format_version,
    }
