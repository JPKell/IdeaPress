"""Integration tests for backup, restore and integrity (database standards §7).

IdeaPress had no backup/restore test at all before this file (M9_AUDIT.md Group 3, item O4) — its
only "backup" hits were ``tests/security/test_project_archives.py``, which is about project
export, not the database. Written in the shape of FreeWeight's own
``tests/integration/test_backup_restore.py``: the WAL trap, corrupt-backup refusal and the
no-leftover-``.pre-restore``-file guarantee, plus one test built on ``weightsdb.testing`` directly
so both dialects are covered the way the ``db-matrix`` CI job expects.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from weightsdb import DatabaseError, MigrationRunner, create_engine_for
from weightsdb.backup import backup, checkpoint, integrity_check, restore, sqlite_path
from weightsdb.testing import temporary_postgres, temporary_sqlite

from ideapress.services.database import MIGRATIONS_LOCATION


@pytest.fixture
def sqlite_engine(tmp_path: Path) -> Engine:
    """A migrated SQLite database with one row in it, and its own engine."""
    engine = create_engine_for(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO settings (key, value_json, updated_at) "
                "VALUES ('original', '1', '2026-08-26 00:00:00')"
            )
        )
    return engine


def _keys(database: Path) -> set[str]:
    """Read the settings keys a *fresh* reader sees — WAL replay included."""
    connection = sqlite3.connect(database)
    try:
        return {row[0] for row in connection.execute("SELECT key FROM settings")}
    finally:
        connection.close()


def test_restore_undoes_writes_still_sitting_in_the_wal(
    sqlite_engine: Engine, tmp_path: Path
) -> None:
    """``journal_mode=WAL`` means a committed write can live only in the ``-wal`` sidecar until a
    checkpoint folds it in. A restore that copies the backup over the main file alone leaves that
    sidecar, and the next reader replays it right back on top — so the database still contains
    exactly the write the restore was called to undo, while reporting success.
    """
    good = tmp_path / "good.sqlite3"
    backup(sqlite_engine, good)

    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO settings (key, value_json, updated_at) "
                "VALUES ('written-after-the-backup', '1', '2026-08-26 00:00:00')"
            )
        )
    database = sqlite_path(sqlite_engine)
    assert Path(f"{database}-wal").exists(), "precondition: the write is still in the WAL"

    result = restore(sqlite_engine, good, confirm=True)

    assert result.revision == _head_revision()
    assert _keys(database) == {"original"}
    assert not Path(f"{database}-wal").exists()


def test_restore_leaves_no_pre_restore_file_behind(sqlite_engine: Engine, tmp_path: Path) -> None:
    good = tmp_path / "good.sqlite3"
    backup(sqlite_engine, good)

    restore(sqlite_engine, good, confirm=True)

    database = sqlite_path(sqlite_engine)
    assert not Path(f"{database}.pre-restore").exists()


def _corrupt(database: Path, *, offset: int) -> None:
    """Zero a page of ``database`` in place, to make it fail an integrity check."""
    with database.open("r+b") as handle:
        handle.seek(offset)
        handle.write(b"\x00" * 4096)


def test_restore_refuses_a_corrupt_backup_before_touching_anything(
    sqlite_engine: Engine, tmp_path: Path
) -> None:
    """Both corruptions are exercised because SQLite does not report them the same way: an
    interior page usually returns a row naming the bad pages, while a damaged header fails the
    pragma outright.
    """
    good = tmp_path / "good.sqlite3"
    backup(sqlite_engine, good)
    database = sqlite_path(sqlite_engine)
    checkpoint(sqlite_engine)
    original = database.read_bytes()

    for name, offset in (("interior-page", 4096), ("header", 0)):
        corrupt = tmp_path / f"corrupt-{name}.sqlite3"
        corrupt.write_bytes(good.read_bytes())
        _corrupt(corrupt, offset=offset)

        with pytest.raises(DatabaseError) as excinfo:
            restore(sqlite_engine, corrupt, confirm=True)

        assert "failed its integrity check" in str(excinfo.value), name
        assert database.read_bytes() == original, name
        assert not Path(f"{database}.pre-restore").exists(), name
        assert integrity_check(sqlite_engine).ok is True, name
        assert _keys(database) == {"original"}, name


def test_restore_refuses_without_confirmation(sqlite_engine: Engine, tmp_path: Path) -> None:
    good = tmp_path / "good.sqlite3"
    backup(sqlite_engine, good)

    with pytest.raises(DatabaseError, match="confirm=True"):
        restore(sqlite_engine, good, confirm=False)


def test_backup_refuses_when_there_is_no_database_file(tmp_path: Path) -> None:
    engine = create_engine_for(f"sqlite:///{tmp_path / 'absent.sqlite3'}")
    try:
        with pytest.raises(DatabaseError, match="no database at"):
            backup(engine, tmp_path / "out.sqlite3")
    finally:
        engine.dispose()


def test_backup_file_is_private_before_it_holds_data(sqlite_engine: Engine, tmp_path: Path) -> None:
    result = backup(sqlite_engine, tmp_path / "out.sqlite3")

    assert result.path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_backup_round_trips_or_refuses_restore_on_both_dialects(
    dialect: str, tmp_path: Path
) -> None:
    """Database standards §7 on ``weightsdb.testing`` directly, so both dialects are covered the
    way the ``db-matrix`` CI job expects — PostgreSQL skips honestly with no server configured.
    """
    context = temporary_sqlite() if dialect == "sqlite" else temporary_postgres()
    with context as engine:
        MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO settings (key, value_json, updated_at) "
                    "VALUES ('dialect-row', '1', '2026-08-26 00:00:00')"
                )
            )
        destination = tmp_path / ("backup.sqlite3" if dialect == "sqlite" else "backup.dump")
        if dialect == "postgresql" and shutil.which("pg_dump") is None:
            pytest.skip("pg_dump is not on PATH")
        result = backup(engine, destination)
        assert result.size_bytes > 0

        if dialect == "sqlite":
            with engine.begin() as connection:
                connection.execute(text("DELETE FROM settings WHERE key = 'dialect-row'"))
            restore(engine, destination, confirm=True)
            with engine.connect() as connection:
                assert (
                    connection.execute(
                        text("SELECT value_json FROM settings WHERE key = 'dialect-row'")
                    ).scalar_one()
                    == 1
                )
        else:
            with pytest.raises(DatabaseError, match="pg_restore"):
                restore(engine, destination, confirm=True)


def _head_revision() -> str:
    """The migration history's current head, read from the scripts rather than written down."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config()
    config.set_main_option("script_location", MIGRATIONS_LOCATION)
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head is not None
    return str(head)
