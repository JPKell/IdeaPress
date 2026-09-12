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

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import inspect, text
from weightsdb import create_engine_for
from weightsdb.testing import temporary_postgres

from ideapress.infrastructure.db.models import Base
from ideapress.services.database import Database, get_status, migration_runner, upgrade

if TYPE_CHECKING:
    from collections.abc import Iterator

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


def test_0007_and_0008_mount_loadledger_and_commissioner_tables(sqlite_database: Database) -> None:
    """Row J1 (ADR-0050): the four LoadLedger tables and Commissioner's one, at head."""
    upgrade(sqlite_database)
    tables = set(inspect(sqlite_database.engine).get_table_names())
    assert tables >= {
        "ledger_entries",
        "ledger_balances",
        "ledger_balance_money",
        "ledger_runs",
        "egress_decisions",
    }


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


def test_0013_heals_a_postgresql_database_that_ran_the_original_0010(
    postgres_database: Database,
) -> None:
    """A 1.3.0–1.4.0 PostgreSQL database disagrees with the model on two timestamps.

    The original `0010` created `started_at` and `created_at` as `TIMESTAMP WITH TIME ZONE`, while
    `ToolCallRecordRow` maps both with `weightsdb.UtcDateTime` — a plain `DateTime` underneath. The
    columns are recreated here in their original shape, because `0010` itself has since been
    corrected, and `0013` must still convert the databases that ran the old one: the instant a row
    already holds survives the conversion, read back as the naive UTC the decorator writes.
    """
    runner = migration_runner(postgres_database.engine)
    runner.upgrade("0012")
    with postgres_database.engine.begin() as connection:
        for column in ("started_at", "created_at"):
            connection.execute(
                text(
                    f"ALTER TABLE tool_call_records ALTER COLUMN {column} "
                    f"TYPE TIMESTAMP WITH TIME ZONE USING {column} AT TIME ZONE 'UTC'"
                )
            )
        connection.execute(text("SET TimeZone='UTC'"))
        connection.execute(
            text(
                "INSERT INTO projects (id, title, slug, content_type, content_type_version,"
                " workflow_id, workflow_version, status, brief_text, author_material_json,"
                " config_json, created_at, updated_at)"
                " VALUES ('01PROJECT', 'T', 't', 'article', '1.0', 'default', '1.0', 'active',"
                " 'b', '[]', '{}', '2026-09-01T00:00:00', '2026-09-01T00:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO stage_runs (id, project_id, stage, state, units_total,"
                " units_completed, units_paused, started_at, options_json, backend, backend_mode)"
                " VALUES ('01RUN', '01PROJECT', 'research', 'completed', 1, 1, 0,"
                " '2026-09-01T00:00:00', '{}', 'ollama', 'ollama')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO attempts (id, stage_run_id, stage, attempt, round, backend,"
                " backend_mode, outcome, degradations_json, created_at, transport_call)"
                " VALUES ('01ATTEMPT', '01RUN', 'research', 1, 1, 'ollama', 'ollama',"
                " 'accepted', '[]', '2026-09-01T00:00:00', 0)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO tool_call_records (id, project_id, attempt_id, invocation_id,"
                " tool_name, args_sha256, status, result_summary, result_sha256, duration_ms,"
                " risk_class, egress, started_at, created_at)"
                " VALUES ('01RECORD', '01PROJECT', '01ATTEMPT', 'inv-1', 'fetch', 'sha256:a',"
                " 'ok', 's', 'sha256:b', 5, 'low', 'allowed', '2026-09-08 10:30:00+00',"
                " '2026-09-08 10:30:01+00')"
            )
        )

    runner.upgrade("head")

    parity = runner.check_parity(Base.metadata)
    assert parity.matches, parity.diff
    with postgres_database.engine.begin() as connection:
        stored = connection.execute(
            text("SELECT started_at, created_at FROM tool_call_records WHERE id = '01RECORD'")
        ).one()
    assert stored.started_at.isoformat() == "2026-09-08T10:30:00"
    assert stored.created_at.isoformat() == "2026-09-08T10:30:01"


def test_downgrade_removes_every_table_on_postgresql(postgres_database: Database) -> None:
    upgrade(postgres_database)
    migration_runner(postgres_database.engine).downgrade("base")
    remaining = set(inspect(postgres_database.engine).get_table_names())
    assert not (remaining & EXPECTED_TABLES)


def test_0007_and_0008_leave_an_existing_dev_database_untouched(sqlite_database: Database) -> None:
    """Row J1's two mounts, over data written before them (simulating an operator's own database).

    Migrated to `0006` first — where a real installation sits today — with a real project, run
    and attempt, then to `head`. The pre-existing rows must survive byte-for-byte and the two new
    mounts must appear, empty: a real upgrade adds tables, it never touches what was already there.
    """
    from sqlalchemy import text

    runner = migration_runner(sqlite_database.engine)
    runner.upgrade("0006")
    with sqlite_database.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO projects (id, title, slug, content_type, content_type_version,"
                " workflow_id, workflow_version, status, brief_text, author_material_json,"
                " config_json, created_at, updated_at)"
                " VALUES ('01PROJECT', 'T', 't', 'article', '1.0', 'default', '1.0', 'active',"
                " 'b', '[]', '{}', '2026-09-01T00:00:00', '2026-09-01T00:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO stage_runs (id, project_id, stage, state, units_total,"
                " units_completed, units_paused, started_at, options_json, backend, backend_mode)"
                " VALUES ('01RUN', '01PROJECT', 'draft', 'completed', 1, 1, 0,"
                " '2026-09-01T00:00:00', '{}', 'ollama', 'ollama')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO attempts (id, stage_run_id, stage, attempt, round, backend,"
                " backend_mode, outcome, model_canonical_id, degradations_json, created_at)"
                " VALUES ('01ATTEMPT', '01RUN', 'draft', 1, 0, 'ollama', 'ollama', 'completed',"
                " 'ollama/gemma4:12b@sha256:abcd', '[]', '2026-09-01T00:00:00')"
            )
        )

    runner.upgrade("head")

    with sqlite_database.engine.connect() as connection:
        row = connection.execute(
            text("SELECT id, title FROM projects WHERE id = '01PROJECT'")
        ).one()
        assert row.title == "T"
        attempt = connection.execute(
            text("SELECT model_canonical_id FROM attempts WHERE id = '01ATTEMPT'")
        ).one()
        assert attempt.model_canonical_id == "ollama/gemma4:12b@sha256:abcd"
    tables = set(inspect(sqlite_database.engine).get_table_names())
    assert tables >= {"ledger_entries", "ledger_runs", "egress_decisions"}
    with sqlite_database.engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM ledger_entries")).scalar() == 0
        assert connection.execute(text("SELECT COUNT(*) FROM egress_decisions")).scalar() == 0


def test_0006_leaves_existing_attempts_as_base_subjects(sqlite_database: Database) -> None:
    """Rows written before 1.1 were served by bare weights, and stay that way.

    There was no way to ask for an adapter before this release and no LoadCoach that could serve
    one, so `NULL` in all three columns is the true statement. `NULL` rather than `''` because "no
    adapter answered" and "an adapter whose name we do not know" are different facts, and a
    back-fill would collapse them.
    """
    from sqlalchemy import text

    runner = migration_runner(sqlite_database.engine)
    runner.upgrade("0005")
    with sqlite_database.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO projects (id, title, slug, content_type, content_type_version,"
                " workflow_id, workflow_version, status, brief_text, author_material_json,"
                " config_json, created_at, updated_at)"
                " VALUES ('01PROJECT', 'T', 't', 'article', '1.0', 'default', '1.0', 'active',"
                " 'b', '[]', '{}', '2026-09-01T00:00:00', '2026-09-01T00:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO stage_runs (id, project_id, stage, state, units_total,"
                " units_completed, units_paused, started_at, options_json, backend, backend_mode)"
                " VALUES ('01RUN', '01PROJECT', 'draft', 'completed', 1, 1, 0,"
                " '2026-09-01T00:00:00', '{}', 'ollama', 'ollama')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO attempts (id, stage_run_id, stage, attempt, round, backend,"
                " backend_mode, outcome, model_canonical_id, degradations_json, created_at)"
                " VALUES ('01ATTEMPT', '01RUN', 'draft', 1, 0, 'ollama', 'ollama', 'completed',"
                " 'ollama/gemma4:12b@sha256:abcd', '[]', '2026-09-01T00:00:00')"
            )
        )

    runner.upgrade("head")

    with sqlite_database.engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT model_canonical_id, adapter_name, adapter_digest, subject_canonical_id"
                " FROM attempts WHERE id = '01ATTEMPT'"
            )
        ).one()
    assert row.model_canonical_id == "ollama/gemma4:12b@sha256:abcd"
    assert row.adapter_name is None
    assert row.adapter_digest is None
    assert row.subject_canonical_id is None


def test_0009_leaves_existing_attempts_unreported_rather_than_zero(
    sqlite_database: Database,
) -> None:
    """Row K4: an attempt written before the cache columns existed knows nothing about them.

    `NULL`, never `0`. A build that never asked its backend for a cache figure observed nothing,
    and a back-fill of `0` would claim those calls were billed no cache tokens — the fabricated
    zero ADR-0016 forbids, and one that would silently turn old floors into totals nobody measured.
    """
    from sqlalchemy import text

    runner = migration_runner(sqlite_database.engine)
    runner.upgrade("0008")
    with sqlite_database.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO projects (id, title, slug, content_type, content_type_version,"
                " workflow_id, workflow_version, status, brief_text, author_material_json,"
                " config_json, created_at, updated_at)"
                " VALUES ('01PROJECT', 'T', 't', 'article', '1.0', 'default', '1.0', 'active',"
                " 'b', '[]', '{}', '2026-09-01T00:00:00', '2026-09-01T00:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO stage_runs (id, project_id, stage, state, units_total,"
                " units_completed, units_paused, started_at, options_json, backend, backend_mode)"
                " VALUES ('01RUN', '01PROJECT', 'draft', 'completed', 1, 1, 0,"
                " '2026-09-01T00:00:00', '{}', 'ollama', 'ollama')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO attempts (id, stage_run_id, stage, attempt, round, backend,"
                " backend_mode, outcome, input_tokens, output_tokens, degradations_json,"
                " created_at)"
                " VALUES ('01ATTEMPT', '01RUN', 'draft', 1, 0, 'ollama', 'ollama', 'completed',"
                " 812, 1104, '[]', '2026-09-01T00:00:00')"
            )
        )

    runner.upgrade("head")

    with sqlite_database.engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT input_tokens, cache_write_tokens, cache_read_tokens"
                " FROM attempts WHERE id = '01ATTEMPT'"
            )
        ).one()
    assert row.input_tokens == 812
    assert row.cache_write_tokens is None
    assert row.cache_read_tokens is None


def test_1_3_0_database_migrates_to_head_and_keeps_its_rows(tmp_path: Path) -> None:
    """A real released-version database, not one this test created (M9_AUDIT.md Group 3, O1).

    The fixture is a real ``ideapress==1.3.0`` install (from PyPI, in a scratch venv) migrated by
    its own ``ideapress db upgrade`` and seeded with two projects through ``ideapress project
    create`` — a real CLI write, not a repository call, since IdeaPress's CLI can do this one.
    1.3.0's head is this build's head too (``0009`` — no revision has landed since that release),
    so today this asserts the upgrade is the documented no-op and both rows survive; it starts
    asserting a real migration the day ``0010`` lands.
    """
    fixture = Path(__file__).parent.parent / "fixtures" / "databases" / "ideapress-1.3.0.sqlite3"
    working_copy = tmp_path / "ideapress-1.3.0.sqlite3"
    shutil.copyfile(fixture, working_copy)

    engine = create_engine_for(f"sqlite:///{working_copy}")
    try:
        runner = migration_runner(engine)
        current_before = runner.current()
        assert current_before is not None, "the 1.3.0 fixture must carry a recorded revision"

        database = Database(engine)
        upgrade(database)

        assert runner.is_at_head()
        with engine.connect() as connection:
            slugs = {
                row[0] for row in connection.execute(text("SELECT slug FROM projects")).fetchall()
            }
        assert slugs == {"fixture-project-one", "fixture-project-two"}
    finally:
        engine.dispose()


def test_1_1_0_database_migrates_to_head_and_keeps_its_rows(tmp_path: Path) -> None:
    """The earliest-published-release companion to the ``1.3.0`` fixture above (row L8, O1).

    ``1.3.0``'s own fixture test above is a documented no-op — its head and this build's head are
    the same revision, so it never actually drove a migration. ``1.1.0`` is IdeaPress's earliest
    PyPI release with a schema, and its head is ``0006``, three revisions behind this build's
    ``0009`` — real ledger and egress tables, and the cache/token-class columns, all apply on top
    of two projects an independent ``1.1.0`` install created through its own CLI.
    """
    fixture = Path(__file__).parent.parent / "fixtures" / "databases" / "ideapress-1.1.0.sqlite3"
    working_copy = tmp_path / "ideapress-1.1.0.sqlite3"
    shutil.copyfile(fixture, working_copy)

    engine = create_engine_for(f"sqlite:///{working_copy}")
    try:
        runner = migration_runner(engine)
        current_before = runner.current()
        assert current_before is not None, "the 1.1.0 fixture must carry a recorded revision"
        assert current_before == "0006", (
            f"the 1.1.0 fixture must sit at IdeaPress's earliest schema; found {current_before!r}"
        )

        database = Database(engine)
        upgrade(database)

        assert runner.is_at_head()
        with engine.connect() as connection:
            slugs = {
                row[0] for row in connection.execute(text("SELECT slug FROM projects")).fetchall()
            }
        assert slugs == {"fixture-project-one", "fixture-project-two"}
    finally:
        engine.dispose()
