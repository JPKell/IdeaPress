"""ideapress.services.runtime — the handles one process owns, and the health they report.

Separate from :mod:`ideapress.bootstrap` because the CLI needs these and may not reach the web
layer: the composition root imports ``web``, so anything the CLI took from it would drag ``cli``
into ``web`` and break both the layering and the web/CLI independence contract.

Opening a runtime never fails because a backend is unreachable (spec §20 AC7) and never fails
because the database is missing — it is created and migrated, which is what makes a first
``ideapress serve`` on a clean machine work with no setup step (AC1).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from mirrorwall import ComponentHealth, ComponentStatus
from weightsdb import DatabaseError

from ideapress.services.database import Database, database_health_component, ensure_ready
from ideapress.services.projects import ProjectService

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from ideapress.config import Settings

__all__ = ["Runtime", "build_runtime"]

logger = logging.getLogger(__name__)


class Runtime:
    """The database handle, the services built on it, and the health each reports."""

    __slots__ = ("_database", "_projects", "settings", "startup_error")

    def __init__(self, settings: Settings) -> None:
        """Open what this process needs, recording rather than raising on a storage failure."""
        self.settings = settings
        self._database: Database | None = None
        self._projects: ProjectService | None = None
        self.startup_error: str | None = None

        database_url = settings.storage.database_url
        project_dir = settings.storage.project_dir
        if database_url is None or project_dir is None:  # pragma: no cover — Settings fills both
            self.startup_error = "storage is not configured"
            return
        try:
            Path(project_dir).mkdir(parents=True, exist_ok=True, mode=0o700)
            database = Database.from_url(
                database_url, statement_timeout_ms=settings.storage.statement_timeout_ms
            )
            ensure_ready(database, auto_migrate=settings.storage.auto_migrate)
        except (DatabaseError, OSError) as exc:
            # A database that will not open is reported through /health and `doctor`, not raised
            # here: the process still answers, which is how the user finds out what is wrong.
            logger.error("storage.unavailable", exc_info=exc)
            self.startup_error = str(exc)
            return
        self._database = database
        self._projects = ProjectService(database, project_dir=Path(project_dir))

    @property
    def database(self) -> Database | None:
        """The handle, or ``None`` when storage could not be opened."""
        return self._database

    @property
    def projects(self) -> ProjectService:
        """The project service.

        Raises:
            RuntimeError: Storage could not be opened. A route reaching this has already been
                told by ``/health`` what is wrong; failing loudly beats returning empty lists that
                look like "you have no projects".
        """
        if self._projects is None:
            message = f"Storage is unavailable: {self.startup_error}"
            raise RuntimeError(message)
        return self._projects

    @property
    def health_checkers(self) -> Sequence[Callable[[], ComponentHealth]]:
        """The three components spec §17 names: ``database``, ``backend`` and ``prompts``."""
        return (self._database_health, self._backend_health, self._prompts_health)

    def _database_health(self) -> ComponentHealth:
        """Report the database component, from WeightsDB's own verdict."""
        if self._database is None:
            return ComponentHealth(
                name="database",
                status=ComponentStatus.UNAVAILABLE,
                detail=self.startup_error or "The database could not be opened.",
            )
        return database_health_component(self._database)

    def _backend_health(self) -> ComponentHealth:
        """Report the configured inference backend, naming which one and whether it answers."""
        mode = self.settings.inference.mode
        return ComponentHealth(
            name="backend",
            status=ComponentStatus.NOT_CONFIGURED,
            detail=f"Configured backend is {mode!r}; no adapter is wired yet.",
            data={"mode": mode},
        )

    def _prompts_health(self) -> ComponentHealth:
        """Report the prompt pack: present, parseable, and matching its manifest."""
        return ComponentHealth(
            name="prompts", status=ComponentStatus.NOT_CONFIGURED, detail="No prompt pack yet."
        )

    def close(self) -> None:
        """Dispose every handle. Safe to call more than once."""
        if self._database is not None:
            self._database.close()
            self._database = None
            self._projects = None


def build_runtime(settings: Settings) -> Runtime:
    """Open the handles a served process needs.

    Args:
        settings: The validated configuration.

    Returns:
        The runtime. Never raises because an inference backend is unreachable, and never raises
        because storage failed — both are reported through ``/health`` and ``ideapress doctor``.
    """
    return Runtime(settings)
