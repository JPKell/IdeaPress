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
from typing import TYPE_CHECKING, Final

from sqlalchemy import select

from ideapress.domain.exporters.html import render_html
from ideapress.domain.exporters.json import render_json
from ideapress.domain.exporters.markdown import render_markdown
from ideapress.domain.exporters.model import (
    EXPORT_FORMAT_VERSION,
    ExportDocument,
    ExportUnit,
    RequirementCoverage,
    UnitProvenance,
)
from ideapress.errors import ExportFailed
from ideapress.infrastructure.db.models import Attempt as AttemptRow
from ideapress.infrastructure.db.models import Coverage as CoverageRow
from ideapress.infrastructure.db.models import Export as ExportRow
from ideapress.infrastructure.db.models import Requirement as RequirementRow
from ideapress.infrastructure.db.models import Unit as UnitRow
from ideapress.infrastructure.db.models import UnitVersion as UnitVersionRow
from ideapress.services.plan import load_requirements

if TYPE_CHECKING:
    from collections.abc import Callable

    from ideapress.services.runtime import Runtime

__all__ = ["FORMATS", "build_document", "export_project", "render"]

logger = logging.getLogger(__name__)

FORMATS: Final[dict[str, str]] = {"markdown": "md", "html": "html", "json": "json"}
"""The three shipped at 1.0, and their file extensions."""

_RENDERERS: Final[dict[str, Callable[[ExportDocument], str]]] = {
    "markdown": render_markdown,
    "html": render_html,
    "json": render_json,
}


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
        rows = session.scalars(
            select(UnitRow).where(UnitRow.project_id == project_id).order_by(UnitRow.ordinal)
        ).all()
        for row in rows:
            if row.state != "committed" or row.current_version_id is None:
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
                                RequirementCoverage(
                                    key=requirement_keys.get(entry.requirement_id, "?"),
                                    text=(
                                        requirements[requirement_keys[entry.requirement_id]].text
                                        if entry.requirement_id in requirement_keys
                                        and requirement_keys[entry.requirement_id] in requirements
                                        else ""
                                    ),
                                    blocking=(
                                        requirements[
                                            requirement_keys[entry.requirement_id]
                                        ].blocking
                                        if entry.requirement_id in requirement_keys
                                        and requirement_keys[entry.requirement_id] in requirements
                                        else True
                                    ),
                                    satisfied=entry.satisfied,
                                    satisfied_by=entry.satisfied_by,
                                    detail=entry.detail_json.get("detail", ""),
                                    checks=(
                                        requirements[
                                            requirement_keys[entry.requirement_id]
                                        ].describe_checks()
                                        if entry.requirement_id in requirement_keys
                                        and requirement_keys[entry.requirement_id] in requirements
                                        else ""
                                    ),
                                )
                                for entry in coverage_rows
                            ),
                            key=lambda entry: entry.key,
                        )
                    ),
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


def export_project(runtime: Runtime, *, project_id: str, fmt: str) -> dict[str, object]:
    """Render a committed project and write it into the project's own directory.

    Args:
        runtime: The process's handles.
        project_id: Which project.
        fmt: ``markdown``, ``html`` or ``json``.

    Returns:
        What was written: the path, its hash, its size and the versions it covers.

    Raises:
        ExportFailed: The format is unknown, the project has no committed unit, or the file could
            not be written.

    The path comes from the project's **slug**, which IdeaPress generated and validated — never
    from a title and never from anything a model produced (risk S2).
    """
    import hashlib

    document = build_document(runtime, project_id=project_id)
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
