"""ideapress.infrastructure.db.repositories.projects — rows in, value objects out.

The only module that holds a :class:`~ideapress.infrastructure.db.models.Project` row. Everything
above it receives :class:`ideapress.domain.project.Project`, which is frozen and has no session
attached — so a template cannot trigger a lazy load and a service cannot accidentally mutate a row
it only meant to read.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from sqlalchemy import func, select

from ideapress.domain.project import Project, ProjectStatus
from ideapress.infrastructure.db.models import Project as ProjectRow
from ideapress.infrastructure.db.models import Source as SourceRow

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

__all__ = [
    "count_sources",
    "delete",
    "get_by_id",
    "get_by_slug",
    "insert",
    "list_projects",
    "slug_exists",
    "to_domain",
    "update",
]


def to_domain(row: ProjectRow) -> Project:
    """Convert a row into the frozen value object the rest of the application uses."""
    return Project(
        id=row.id,
        title=row.title,
        slug=row.slug,
        content_type=row.content_type,
        content_type_version=row.content_type_version,
        workflow_id=row.workflow_id,
        workflow_version=row.workflow_version,
        status=cast("ProjectStatus", row.status),
        brief_text=row.brief_text,
        author_material=dict(row.author_material_json),
        created_at=row.created_at,
        updated_at=row.updated_at,
        completed_at=row.completed_at,
        archived_at=row.archived_at,
    )


def insert(session: Session, row: ProjectRow) -> Project:
    """Add ``row`` and return it as a value object, flushing so the identity is assigned."""
    session.add(row)
    session.flush()
    return to_domain(row)


def get_by_id(session: Session, project_id: str) -> Project | None:
    """Return the project with this identifier, or ``None``."""
    row = session.get(ProjectRow, project_id)
    return to_domain(row) if row is not None else None


def get_by_slug(session: Session, slug: str) -> Project | None:
    """Return the project with this slug, or ``None``."""
    row = session.scalars(select(ProjectRow).where(ProjectRow.slug == slug)).one_or_none()
    return to_domain(row) if row is not None else None


def slug_exists(session: Session, slug: str) -> bool:
    """Whether any project already holds this slug. Slugs are unique across the database."""
    return (
        session.scalar(select(func.count()).select_from(ProjectRow).where(ProjectRow.slug == slug))
        or 0
    ) > 0


def list_projects(
    session: Session,
    *,
    status: str | None = None,
    content_type: str | None = None,
    include_archived: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> Sequence[Project]:
    """List projects, newest activity first.

    Args:
        session: An open session.
        status: Restrict to one status.
        content_type: Restrict to one content type.
        include_archived: Whether archived projects appear. They are hidden by default: archiving
            exists so a finished project stops cluttering the list, and a list that shows them
            anyway makes the action pointless.
        limit: Page size.
        offset: How many to skip.

    Returns:
        Value objects, ordered by ``updated_at`` descending — the index data model §5 names.
    """
    query = select(ProjectRow).order_by(ProjectRow.updated_at.desc(), ProjectRow.id.desc())
    if status is not None:
        query = query.where(ProjectRow.status == status)
    elif not include_archived:
        query = query.where(ProjectRow.status != "archived")
    if content_type is not None:
        query = query.where(ProjectRow.content_type == content_type)
    return [to_domain(row) for row in session.scalars(query.limit(limit).offset(offset))]


def update(session: Session, project_id: str, **changes: object) -> Project | None:
    """Apply ``changes`` to one project and return the updated value object.

    Args:
        session: An open write session.
        project_id: Which project.
        **changes: Column names and their new values. Unknown names are refused rather than
            silently ignored, so a typo in a caller is a failure rather than a no-op.

    Returns:
        The updated project, or ``None`` when no such project exists.

    Raises:
        AttributeError: A name in ``changes`` is not a column of ``projects``.
    """
    row = session.get(ProjectRow, project_id)
    if row is None:
        return None
    for name, value in changes.items():
        if not hasattr(row, name):
            message = f"projects has no column {name!r}"
            raise AttributeError(message)
        setattr(row, name, value)
    session.flush()
    return to_domain(row)


def delete(session: Session, project_id: str) -> bool:
    """Delete one project and everything cascading from it. Returns whether it existed."""
    row = session.get(ProjectRow, project_id)
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True


def count_sources(session: Session, project_id: str) -> int:
    """How many source documents this project holds — part of the delete preview."""
    return (
        session.scalar(
            select(func.count()).select_from(SourceRow).where(SourceRow.project_id == project_id)
        )
        or 0
    )
