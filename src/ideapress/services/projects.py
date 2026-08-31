"""ideapress.services.projects — create, list, open, update, archive and delete.

The project directory is the one place a filesystem path is built from user input, so it is the one
place containment is checked. Risk S2 forbids ever building a path from model output; this module
builds it from a slug that :func:`ideapress.domain.project.slugify` produced and
:func:`~ideapress.domain.project.is_safe_slug` verified, and then resolves and re-checks the result
against the configured root before touching it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from baseaicore import ValidationError

from ideapress.domain.project import Project, is_safe_slug, slugify
from ideapress.errors import ProjectNotFound
from ideapress.infrastructure.db.models import Project as ProjectRow
from ideapress.infrastructure.db.repositories import projects as repository

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ideapress.services.database import Database

__all__ = [
    "DEFAULT_CONTENT_TYPE",
    "DEFAULT_WORKFLOW",
    "DeletePreview",
    "ProjectService",
]

DEFAULT_CONTENT_TYPE = ("article", "1.0")
DEFAULT_WORKFLOW = ("standard", "1.0")

_MAX_TITLE_LENGTH = 200


@dataclass(frozen=True, slots=True)
class DeletePreview:
    """Exactly what a delete would remove, shown before it is confirmed (api.md §2).

    Attributes:
        project: The project itself.
        source_count: How many attached source documents go with it.
        directory: The artifact directory that will be removed, or ``None`` if it was never
            created.
        directory_bytes: How much is on disk there.
    """

    project: Project
    source_count: int
    directory: Path | None
    directory_bytes: int


class ProjectService:
    """Project lifecycle, over one database handle and one artifact root.

    Both are injected: a test gets a temporary directory and an in-memory database, and nothing in
    this class reads configuration or a clock of its own.
    """

    def __init__(self, database: Database, *, project_dir: Path) -> None:
        """Bind the service to a database handle and the root every artifact directory sits in."""
        self._database = database
        self._root = Path(project_dir)

    def _directory_for(self, slug: str) -> Path:
        """Resolve this project's artifact directory, refusing anything outside the root.

        Args:
            slug: A stored slug.

        Returns:
            The absolute directory path.

        Raises:
            ValidationError: ``slug`` is not a safe name, or the resolved path escapes the
                configured root. The second check is not redundant: a symlinked root, or a slug
                that a future change lets through, both land here rather than on the filesystem.
        """
        if not is_safe_slug(slug):
            message = f"Project slug {slug!r} is not a safe directory name."
            raise ValidationError(message, details={"slug": slug})
        root = self._root.resolve()
        candidate = (root / slug).resolve()
        if candidate != root and root not in candidate.parents:
            message = f"Project directory for {slug!r} would fall outside {root}."
            raise ValidationError(message, details={"slug": slug, "root": str(root)})
        return candidate

    def _unique_slug(self, session: Any, title: str) -> str:
        """Derive a slug and resolve a collision by suffixing, never by overwriting."""
        base = slugify(title)
        if not repository.slug_exists(session, base):
            return base
        for suffix in range(2, 1000):
            candidate = f"{base[: 64 - len(str(suffix)) - 1]}-{suffix}"
            if not repository.slug_exists(session, candidate):
                return candidate
        message = f"Could not find a free slug for {title!r} after 998 attempts."
        raise ValidationError(message, details={"title": title})

    def create(
        self,
        *,
        title: str,
        brief: str = "",
        content_type: str = DEFAULT_CONTENT_TYPE[0],
        content_type_version: str = DEFAULT_CONTENT_TYPE[1],
        workflow_id: str = DEFAULT_WORKFLOW[0],
        workflow_version: str = DEFAULT_WORKFLOW[1],
        author_material: dict[str, Any] | None = None,
    ) -> Project:
        """Create a project and its artifact directory.

        Args:
            title: The human title. Any text; the slug is derived, never taken.
            brief: The one-page brief the requirement compiler will read.
            content_type: ``article`` or ``report`` at 1.0; the registry is open.
            content_type_version: Which version of that content type.
            workflow_id: Which workflow definition.
            workflow_version: Which version of it.
            author_material: Style guide, audience, constraints — structured, not prose.

        Returns:
            The created project.

        Raises:
            ValidationError: The title is empty or longer than 200 characters, or the derived slug
                would not be a safe directory name.
        """
        cleaned = title.strip()
        if not cleaned:
            message = "A project needs a title."
            raise ValidationError(message, details={"field": "title"})
        if len(cleaned) > _MAX_TITLE_LENGTH:
            message = f"Title is {len(cleaned)} characters; the limit is {_MAX_TITLE_LENGTH}."
            raise ValidationError(message, details={"field": "title"})

        with self._database.write() as session:
            slug = self._unique_slug(session, cleaned)
            directory = self._directory_for(slug)
            project = repository.insert(
                session,
                ProjectRow(
                    title=cleaned,
                    slug=slug,
                    content_type=content_type,
                    content_type_version=content_type_version,
                    workflow_id=workflow_id,
                    workflow_version=workflow_version,
                    status="draft",
                    brief_text=brief,
                    author_material_json=dict(author_material or {}),
                    config_json={},
                ),
            )
        # After the commit, so a failed insert never leaves an orphan directory behind. 0o700:
        # this is the user's private work and nothing else on the machine needs to read it.
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return project

    def list(
        self,
        *,
        status: str | None = None,
        content_type: str | None = None,
        include_archived: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[Project]:
        """List projects, most recently updated first. Archived ones are hidden by default."""
        with self._database.read() as session:
            return repository.list_projects(
                session,
                status=status,
                content_type=content_type,
                include_archived=include_archived,
                limit=limit,
                offset=offset,
            )

    def get(self, project_id: str) -> Project:
        """Open one project by identifier.

        Raises:
            ProjectNotFound: No project with that identifier.
        """
        with self._database.read() as session:
            project = repository.get_by_id(session, project_id)
        if project is None:
            message = f"No project with id {project_id!r}."
            raise ProjectNotFound(message, details={"project_id": project_id})
        return project

    def update(
        self,
        project_id: str,
        *,
        title: str | None = None,
        brief: str | None = None,
        author_material: dict[str, Any] | None = None,
        status: str | None = None,
    ) -> Project:
        """Update a project's brief, author material, title or status.

        The slug is **not** re-derived from a new title: it names a directory that already holds
        the user's exports, and renaming it would orphan them. Api.md §2 is explicit that a brief
        change never silently recompiles requirements either — that is a separate, explicit action.

        Raises:
            ProjectNotFound: No project with that identifier.
            ValidationError: The new title is empty or too long, or the status is not one of the
                six in the data model.
        """
        changes: dict[str, object] = {}
        if title is not None:
            cleaned = title.strip()
            if not cleaned or len(cleaned) > _MAX_TITLE_LENGTH:
                message = "A project title must be between 1 and 200 characters."
                raise ValidationError(message, details={"field": "title"})
            changes["title"] = cleaned
        if brief is not None:
            changes["brief_text"] = brief
        if author_material is not None:
            changes["author_material_json"] = dict(author_material)
        if status is not None:
            from ideapress.domain.project import PROJECT_STATUSES

            if status not in PROJECT_STATUSES:
                message = f"{status!r} is not a project status."
                raise ValidationError(
                    message, details={"field": "status", "allowed": sorted(PROJECT_STATUSES)}
                )
            changes["status"] = status

        with self._database.write() as session:
            project = repository.update(session, project_id, **changes)
        if project is None:
            message = f"No project with id {project_id!r}."
            raise ProjectNotFound(message, details={"project_id": project_id})
        return project

    def archive(self, project_id: str) -> Project:
        """Archive a project: hidden from the list, nothing removed, reversible."""
        from ideapress.infrastructure.db.models import utcnow

        with self._database.write() as session:
            project = repository.update(
                session, project_id, status="archived", archived_at=utcnow()
            )
        if project is None:
            message = f"No project with id {project_id!r}."
            raise ProjectNotFound(message, details={"project_id": project_id})
        return project

    def unarchive(self, project_id: str) -> Project:
        """Return an archived project to the list."""
        with self._database.write() as session:
            project = repository.update(session, project_id, status="draft", archived_at=None)
        if project is None:
            message = f"No project with id {project_id!r}."
            raise ProjectNotFound(message, details={"project_id": project_id})
        return project

    def preview_delete(self, project_id: str) -> DeletePreview:
        """Report exactly what a delete would remove, before anything is removed.

        Returns:
            The project, how many sources go with it, its artifact directory and how much is on
            disk there. Api.md §2 requires preview-then-confirm because this is irreversible and
            the thing being destroyed is the user's own writing.

        Raises:
            ProjectNotFound: No project with that identifier.
        """
        with self._database.read() as session:
            project = repository.get_by_id(session, project_id)
            if project is None:
                message = f"No project with id {project_id!r}."
                raise ProjectNotFound(message, details={"project_id": project_id})
            source_count = repository.count_sources(session, project_id)

        directory = self._directory_for(project.slug)
        if not directory.is_dir():
            return DeletePreview(
                project=project, source_count=source_count, directory=None, directory_bytes=0
            )
        total = sum(item.stat().st_size for item in directory.rglob("*") if item.is_file())
        return DeletePreview(
            project=project,
            source_count=source_count,
            directory=directory,
            directory_bytes=total,
        )

    def delete(self, project_id: str, *, confirm: bool = False) -> DeletePreview:
        """Delete a project, its rows and its artifact directory.

        Args:
            project_id: Which project.
            confirm: Must be ``True``. Without it nothing is removed and the preview is returned,
                so a caller that forgot to ask the user gets a description rather than a loss.

        Returns:
            The preview of what was (or would be) removed.

        Raises:
            ProjectNotFound: No project with that identifier.
        """
        preview = self.preview_delete(project_id)
        if not confirm:
            return preview
        with self._database.write() as session:
            repository.delete(session, project_id)
        if preview.directory is not None and preview.directory.is_dir():
            import shutil

            # The path came from `_directory_for`, which refuses an unsafe slug and re-checks
            # containment against the resolved root — this is not a path anything else supplied.
            shutil.rmtree(preview.directory)
        return preview

    def directory(self, project: Project) -> Path:
        """Return this project's artifact directory, creating it if it is missing."""
        path = self._directory_for(project.slug)
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path
