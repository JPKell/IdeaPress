"""Migrations, on both dialects.

The PostgreSQL half is a P1 requirement the SQLite default never exercises: a migration that is a
syntax error on PostgreSQL passes every local gate and fails only on the runner, which is exactly
how LoadCoach's M5 closeout found M5C-15.

The server is reached through :func:`weightsdb.testing.temporary_postgres` rather than a URL this
repository invents. That helper reads ``WEIGHTSDB_POSTGRES_URL`` (whose default names the
``+psycopg`` driver this project actually installs), resets the schema between tests, and turns
its skip into a failure under ``WEIGHTSDB_REQUIRE_POSTGRES=1`` — because a silently skipped dialect
is an untested dialect.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import inspect
from weightsdb.testing import temporary_postgres

from ideapress.infrastructure.db.models import Base
from ideapress.services.database import Database, get_status, migration_runner, upgrade

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

EXPECTED_TABLES = {
    "projects",
    "sources",
    "settings",
    "api_tokens",
    "requirements",
    "units",
    "stage_runs",
    "attempts",
    "stage_events",
}


@pytest.fixture
def sqlite_database(tmp_path: Path) -> Iterator[Database]:
    database = Database.from_url(f"sqlite:///{tmp_path / 'ideapress.sqlite3'}")
    yield database
    database.close()


@pytest.fixture
def postgres_database() -> Iterator[Database]:
    """A handle on a freshly reset PostgreSQL schema, or a skip when no server is reachable."""
    with temporary_postgres() as engine:
        yield Database(engine)


def test_sqlite_migrates_from_empty_to_head(sqlite_database: Database) -> None:
    assert get_status(sqlite_database).at_head is False
    upgrade(sqlite_database)
    status = get_status(sqlite_database)
    assert status.at_head is True
    # The head moves every phase, so this asserts the runner's own head rather than a literal.
    assert status.current_revision == status.head_revision
    assert set(inspect(sqlite_database.engine).get_table_names()) >= EXPECTED_TABLES


def test_upgrade_is_idempotent(sqlite_database: Database) -> None:
    upgrade(sqlite_database)
    first = get_status(sqlite_database).current_revision
    upgrade(sqlite_database)
    assert get_status(sqlite_database).current_revision == first


def test_downgrade_removes_every_table(sqlite_database: Database) -> None:
    upgrade(sqlite_database)
    migration_runner(sqlite_database.engine).downgrade("base")
    remaining = set(inspect(sqlite_database.engine).get_table_names())
    assert not (remaining & EXPECTED_TABLES)


def test_models_and_migration_agree_on_sqlite(sqlite_database: Database) -> None:
    """Database standards §5.2: the migration and the declarative models cannot drift apart."""
    upgrade(sqlite_database)
    parity = migration_runner(sqlite_database.engine).check_parity(Base.metadata)
    assert parity.matches, parity.diff


def test_postgresql_migrates_from_empty_to_head(postgres_database: Database) -> None:
    upgrade(postgres_database)
    status = get_status(postgres_database)
    assert status.at_head is True
    assert status.dialect == "postgresql"
    assert set(inspect(postgres_database.engine).get_table_names()) >= EXPECTED_TABLES


def test_models_and_migration_agree_on_postgresql(postgres_database: Database) -> None:
    upgrade(postgres_database)
    parity = migration_runner(postgres_database.engine).check_parity(Base.metadata)
    assert parity.matches, parity.diff


def test_downgrade_removes_every_table_on_postgresql(postgres_database: Database) -> None:
    upgrade(postgres_database)
    migration_runner(postgres_database.engine).downgrade("base")
    remaining = set(inspect(postgres_database.engine).get_table_names())
    assert not (remaining & EXPECTED_TABLES)
