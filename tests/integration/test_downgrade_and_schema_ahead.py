"""Integration test for the downgrade drill packaging standards §6.1 promises.

"Downgrading the application without downgrading the database is refused, not attempted: a
database ahead of the code raises ``SchemaAhead`` at startup and names both revisions and the
backup directory. A supported downgrade path is: stop the application, restore the automatic
pre-migration backup, install the older version." Until this test, nothing in any of the four
applications' suites drove that drill end to end (M9_AUDIT.md Group 3, item O2) — this is the
one that does, on IdeaPress's own bootstrap path.

Writing this test surfaced that IdeaPress's ``ensure_ready`` had never actually implemented the
``SchemaAhead`` check FreeWeight and LoadCoach already had: a database ahead of head fell straight
through to an attempted ``runner.upgrade()`` (when ``auto_migrate`` is true, the SQLite default),
which alembic would fail on its own terms rather than refusing cleanly. It now checks
``known_revisions()`` first, the same as its siblings.

IdeaPress also does not raise this at all in the way the other three do: spec §20 AC7 makes an
unreachable/broken database a health condition, never a startup crash, so
:class:`~ideapress.services.runtime.Runtime` catches every ``DatabaseError`` and records
``startup_error`` instead of propagating it. This test drives both: ``ensure_ready`` directly
(the function every other application's bootstrap calls straight through), and
:func:`~ideapress.services.runtime.build_runtime` (IdeaPress's own translation of the same
refusal into a health condition rather than a raised exception).
"""

from __future__ import annotations

from pathlib import Path
from tempfile import mkdtemp

from sqlalchemy import select, text
from weightsdb import SchemaAhead
from weightsdb import backup as weightsdb_backup
from weightsdb import restore as weightsdb_restore

from ideapress.config import load_settings
from ideapress.infrastructure.db.models import Project as ProjectRow
from ideapress.infrastructure.db.repositories.projects import insert as insert_project
from ideapress.services.database import (
    Database,
    _backup_directory,
    ensure_ready,
    migration_runner,
)
from ideapress.services.runtime import build_runtime


def _seed_one_project(database: Database) -> None:
    """Write one row through the repository layer, not raw SQL."""
    with database.write() as session:
        insert_project(
            session,
            ProjectRow(
                title="Fixture project",
                slug="fixture-project",
                content_type="article",
                content_type_version="1.0",
                workflow_id="standard",
                workflow_version="1.0",
                status="drafting",
                brief_text="a brief",
            ),
        )


def test_schema_ahead_is_refused_and_the_pre_migration_backup_restores_it() -> None:
    """Upgrade, write a row, back up, jump the version ahead, refuse, restore, start again."""
    settings = load_settings().settings
    assert settings.storage.database_url is not None
    database_url = settings.storage.database_url

    database = Database.from_url(database_url)
    # 1. Bootstrap's own path to a fresh database: migrate to head.
    ensure_ready(database, auto_migrate=True)
    head = migration_runner(database.engine).heads()[0]

    # 2. Write one row through the repository layer.
    _seed_one_project(database)

    # 3. `ideapress db backup` — the real path an operator runs before anything risky.
    backup_dir = Path(mkdtemp())
    backup_result = weightsdb_backup(database.engine, backup_dir / "pre-jump.sqlite3")
    assert backup_result.path.is_file()

    # 4. Simulate a newer application version having migrated this database further: hand-set
    # `alembic_version` to a revision this build's history does not contain. `database` is closed
    # here (not reopened at the end) — the file-level jump below must not run underneath its pool.
    fake_future_revision = "9999_from_the_future"
    with database.engine.begin() as connection:
        connection.execute(
            text("UPDATE alembic_version SET version_num = :revision"),
            {"revision": fake_future_revision},
        )
    database.close()

    # 5a. `ensure_ready` — the function IdeaPress's own bootstrap-adjacent Runtime calls —
    # refuses directly, the exact drill packaging standards §6.1 promises.
    reopened = Database.from_url(database_url)
    try:
        try:
            ensure_ready(reopened, auto_migrate=True)
        except SchemaAhead as exc:
            assert fake_future_revision in str(exc)
            assert head in str(exc)
            assert exc.details["current"] == fake_future_revision
            assert exc.details["head"] == head
            expected_backup_directory = str(_backup_directory())
            assert expected_backup_directory in str(exc)
            assert exc.details["backup_directory"] == expected_backup_directory
        else:  # pragma: no cover — defensive: the test proves nothing if this branch runs
            raise AssertionError("SchemaAhead was not raised for a database ahead of head")
    finally:
        reopened.close()

    # 5b. IdeaPress's own translation (spec §20 AC7): the same refusal reaches a real `Runtime`
    # as a recorded `startup_error`, never a crash.
    runtime = build_runtime(settings)
    try:
        assert runtime.startup_error is not None
        assert fake_future_revision in runtime.startup_error
        assert head in runtime.startup_error
    finally:
        runtime.close()

    # 6. The supported downgrade path: restore the pre-migration backup.
    restored = Database.from_url(database_url)
    try:
        weightsdb_restore(restored.engine, backup_result.path, confirm=True)

        # 7. The application starts again, at the revision it knew about all along, and the row
        # written before the jump is intact.
        ensure_ready(restored, auto_migrate=True)
        assert migration_runner(restored.engine).current() == head
        with restored.read() as session:
            found = session.scalar(select(ProjectRow).where(ProjectRow.slug == "fixture-project"))
            assert found is not None
    finally:
        restored.close()
