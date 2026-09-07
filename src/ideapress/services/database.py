"""ideapress.services.database — engine construction, startup migration and status.

Route handlers and CLI command bodies never call :func:`weightsdb.create_engine_for` directly
(CLI standards §1, coding standards §5); they call a function here. That is what makes
``ideapress health --json`` and ``GET /api/v1/health`` report identical database status by
construction rather than by review.

IdeaPress writes no database plumbing of its own: the engine, the session scopes, the portable
column types, the migration runner, the backup and the health probe all come from WeightsDB.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Final

from mirrorwall import ComponentHealth, ComponentStatus
from weightsdb import (
    DatabaseError,
    MigrationRequired,
    MigrationRunner,
    SchemaAhead,
    create_engine_for,
    database_health,
    database_size_bytes,
    session_factory,
    session_scope,
    transaction,
)

from ideapress.config import data_dir

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine
    from sqlalchemy.orm import Session, sessionmaker
    from weightsdb import MigrationOutcome

    from ideapress.config import Settings
    from ideapress.services.budget import BudgetService
    from ideapress.services.egress import EgressService

__all__ = [
    "MIGRATIONS_LOCATION",
    "Database",
    "DatabaseStatus",
    "build_engine",
    "database_health_component",
    "ensure_ready",
    "get_status",
    "migration_runner",
    "upgrade",
]

MIGRATIONS_LOCATION: Final = str(
    Path(__file__).resolve().parent.parent / "infrastructure" / "db" / "migrations"
)

_APPLICATION_NAME: Final = "ideapress"


def build_engine(database_url: str, *, statement_timeout_ms: int | None = None) -> Engine:
    """Build the engine for the configured database URL.

    Args:
        database_url: ``settings.storage.database_url`` — never ``None`` once Settings validated.
        statement_timeout_ms: ``settings.storage.statement_timeout_ms``; PostgreSQL only.

    Returns:
        A dialect-configured engine. Opens no connection until first use.
    """
    return create_engine_for(
        database_url,
        statement_timeout_ms=statement_timeout_ms,
        application_name=_APPLICATION_NAME,
    )


class Database:
    """The application's live connection to its database: one engine, for as long as it serves.

    Owned by the caller — the web application creates one in its lifespan and disposes it at
    shutdown; a CLI command creates one, runs, and closes it on the way out. Every service function
    takes a handle rather than building an engine from a URL.
    """

    __slots__ = ("_budget", "_egress", "_engine", "_sessions", "_settings")

    def __init__(self, engine: Engine) -> None:
        """Wrap an existing engine. Prefer :meth:`from_url` unless you built the engine yourself."""
        self._engine = engine
        self._sessions = session_factory(engine)
        self._budget: BudgetService | None = None
        self._egress: EgressService | None = None
        self._settings: Settings | None = None

    @classmethod
    def from_url(cls, database_url: str, *, statement_timeout_ms: int | None = None) -> Database:
        """Build a handle for ``database_url``. Opens no connection until first use."""
        return cls(build_engine(database_url, statement_timeout_ms=statement_timeout_ms))

    @property
    def engine(self) -> Engine:
        """The underlying engine, for the file-level operations that need one directly."""
        return self._engine

    @property
    def sessions(self) -> sessionmaker[Session]:
        """The session factory bound to this handle's engine."""
        return self._sessions

    @property
    def budget(self) -> BudgetService | None:
        """The LoadLedger mount's service, or ``None`` when nothing has attached one.

        Set once, by :func:`ideapress.services.runtime.build_runtime`, through
        :meth:`attach_governance`. ``None`` here means "not governed" rather than "no budget
        configured" — a test that builds a bare :class:`Database` gets attempts recorded with no
        debit at all, which is why every call site that reads this checks for ``None`` rather than
        assuming a service.
        """
        return self._budget

    @property
    def egress(self) -> EgressService | None:
        """The Commissioner mount's service, or ``None``. See :attr:`budget`."""
        return self._egress

    @property
    def settings(self) -> Settings | None:
        """The validated configuration this handle's governance was built from, or ``None``."""
        return self._settings

    def attach_governance(
        self, *, budget: BudgetService, egress: EgressService, settings: Settings
    ) -> None:
        """Bind the budget and egress services this handle's attempts will be governed by.

        Called exactly once, by :func:`ideapress.services.runtime.build_runtime`, right after the
        database opens. This is the **only** way :func:`ideapress.services.stages.record_attempt`
        — a free function every stage body already calls with this same handle — reaches a budget
        ledger, a pricing catalogue and an egress policy without a new parameter threading through
        every one of its nine call sites, four of which this row must not edit (row J1/J2
        concurrency: ``services/unit_loop.py``, ``services/project_review.py`` and
        ``services/review_loop.py`` are J2's).

        Args:
            budget: The process's :class:`~ideapress.services.budget.BudgetService`.
            egress: The process's :class:`~ideapress.services.egress.EgressService`.
            settings: The validated configuration both were built from — kept here too because
                the governance funnel needs it to describe the backend that answered as an egress
                target (:func:`ideapress.services.egress.backend_target`).
        """
        self._budget = budget
        self._egress = egress
        self._settings = settings

    @contextmanager
    def write(self) -> Iterator[Session]:
        """One read-write unit of work, committed on success and rolled back on any exception."""
        with session_scope(self._sessions) as session:
            yield session

    @contextmanager
    def read(self) -> Iterator[Session]:
        """One read-only unit of work.

        Enforced, not merely declared: a write attempted inside this scope is refused by SQLite
        rather than silently taken (:func:`weightsdb.transaction`).
        """
        with session_scope(self._sessions) as session, transaction(session, immediate=False):
            yield session

    def close(self) -> None:
        """Dispose the pool. The handle must not be used afterwards."""
        self._engine.dispose()

    def __enter__(self) -> Database:
        """Support ``with Database.from_url(...) as db:`` for one-shot callers like the CLI."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Always dispose the pool, whether the body succeeded or raised."""
        self.close()


def migration_runner(engine: Engine, *, backup_retention: int = 5) -> MigrationRunner:
    """Build the runner over IdeaPress's own migration history."""
    return MigrationRunner(
        engine, script_location=MIGRATIONS_LOCATION, backup_retention=backup_retention
    )


def _backup_directory() -> Path:
    """Where ``ideapress db backup`` writes by default (``<data_dir>/backups``, both dialects).

    So a :class:`~weightsdb.errors.SchemaAhead` refusal can name the directory an operator finds
    their pre-migration backup in.
    """
    return data_dir() / "backups"


def upgrade(database: Database, *, backup_retention: int = 5) -> MigrationOutcome:
    """Run every pending migration.

    Args:
        database: The handle to upgrade.
        backup_retention: How many automatic pre-migration backups to keep.

    Returns:
        What the runner did.

    Raises:
        MigrationFailed: A revision raised. The pre-migration backup is on disk.
        SchemaAhead: The database is newer than this build knows about.
    """
    return migration_runner(database.engine, backup_retention=backup_retention).upgrade()


def ensure_ready(database: Database, *, auto_migrate: bool) -> None:
    """Bring the schema to head, or refuse to run against a stale one.

    Args:
        database: The handle to check.
        auto_migrate: ``settings.storage.auto_migrate``.

    Raises:
        MigrationRequired: Migrations are pending and ``auto_migrate`` is false — which is the
            PostgreSQL default, because a failed migration there cannot be rolled back
            automatically (database standards §5.1).
        SchemaAhead: The database was written by a newer build.
    """
    runner = migration_runner(database.engine)
    if runner.is_at_head():
        return
    current = runner.current()
    if current is not None and current not in runner.known_revisions():
        heads = runner.heads()
        head = heads[0] if heads else None
        backup_directory = _backup_directory()
        raise SchemaAhead(
            f"The database is at revision {current!r}, which this build's migrations do not "
            f"produce (known head: {head!r}). It was likely written by a newer application "
            f"version. Downgrading: stop the application, restore the pre-migration backup "
            f"under {backup_directory}, then install the older version (see "
            "docs/upgrading.md).",
            details={
                "current": current,
                "head": head,
                "backup_directory": str(backup_directory),
            },
        )
    if auto_migrate:
        runner.upgrade()
        return
    message = (
        "The database schema is not at head and storage.auto_migrate is false. Run "
        "`ideapress db upgrade` after taking a backup."
    )
    raise MigrationRequired(message, details={"current": runner.current(), "head": runner.heads()})


@dataclass(frozen=True, slots=True)
class DatabaseStatus:
    """What ``ideapress db status`` reports."""

    dialect: str
    current_revision: str | None
    head_revision: str | None
    at_head: bool
    size_bytes: int | None


def get_status(database: Database) -> DatabaseStatus:
    """Report the schema revision and the file size, without modifying anything."""
    runner = migration_runner(database.engine)
    heads = runner.heads()
    try:
        size = database_size_bytes(database.engine)
    except DatabaseError:  # pragma: no cover — a dialect that cannot report size
        size = None
    return DatabaseStatus(
        dialect=database.engine.dialect.name,
        current_revision=runner.current(),
        head_revision=heads[0] if heads else None,
        at_head=runner.is_at_head(),
        size_bytes=size,
    )


def database_health_component(database: Database | None) -> ComponentHealth:
    """Report the ``database`` health component.

    Args:
        database: The handle, or ``None`` when the process has not opened one.

    Returns:
        WeightsDB's own verdict, not a second opinion: it already classifies reachability, pending
        migrations, integrity and free space into a :class:`~mirrorwall.ComponentStatus`, and a
        re-derivation here could disagree with what ``ideapress db status`` reports.
    """
    if database is None:
        return ComponentHealth(
            name="database",
            status=ComponentStatus.NOT_CONFIGURED,
            detail="No database handle is open.",
        )
    try:
        health = database_health(database.engine, migration_runner(database.engine))
    except DatabaseError as exc:
        return ComponentHealth(name="database", status=ComponentStatus.UNAVAILABLE, detail=str(exc))
    detail = (
        "; ".join(health.degraded_reasons)
        if health.degraded_reasons
        else f"{health.dialect}, schema at head."
    )
    return ComponentHealth(
        name="database",
        # WeightsDB annotates `status` as the literal set; ComponentStatus is that set.
        status=ComponentStatus(health.status),
        detail=detail,
        data={"revision": health.current_revision, "dialect": health.dialect},
    )
