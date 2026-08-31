"""P4's named failure mode: a partial commit after a mid-write failure.

Two tests, and they are different evidence:

* **An exception at a real seam.** `commit_unit` writes the version, its coverage and the unit's
  state change in one transaction; a fault injected between them must leave *nothing*. Injected by
  patching a function the commit actually calls, not by a test hook in production code.
* **A killed process.** The strongest evidence, and the one the run's prompt asks for: a child
  process is `SIGKILL`ed while its commit transaction is open, and the database is then reopened
  from scratch. SQLite rolls the journal back on open, so what a reader sees afterwards is what a
  reader would have seen if the machine had lost power.

Both then assert the unit is **resumable**: still planned or paused, with no version, so the loop
picks it up rather than skipping it as done.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from ideapress.domain.commit import CoverageEntry, CoverageReport
from ideapress.domain.requirements import CompiledBy, Requirement, SourceReference
from ideapress.infrastructure.db.models import Coverage as CoverageRow
from ideapress.infrastructure.db.models import Project as ProjectRow
from ideapress.infrastructure.db.models import Requirement as RequirementRow
from ideapress.infrastructure.db.models import Unit as UnitRow
from ideapress.infrastructure.db.models import UnitVersion as UnitVersionRow
from ideapress.services.database import Database, upgrade
from ideapress.services.units import commit_unit, load_unit

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

COMPILED_BY = CompiledBy(prompt_id="stages.requirements.compile", version="1.0.0")
TEXT = "Everything runs on your own machine and nothing is uploaded anywhere at all."


def _requirement() -> Requirement:
    return Requirement(
        key="R-001",
        text="The unit must say where inference runs.",
        blocking=True,
        source=SourceReference(document="brief", quote="a quotation long enough to be evidence"),
        compiled_by=COMPILED_BY,
    )


def _coverage() -> CoverageReport:
    return CoverageReport(
        entries=(
            CoverageEntry(
                requirement=_requirement(),
                satisfied=True,
                satisfied_by="deterministic_check",
                detail="found",
            ),
        )
    )


def _seed(database: Database) -> tuple[str, str]:
    """A project with one requirement and one planned unit, ready to commit."""
    with database.write() as session:
        project = ProjectRow(
            title="Local inference",
            slug="local-inference",
            content_type="article",
            content_type_version="1.0",
            workflow_id="standard",
            workflow_version="1.0",
            status="generating",
            brief_text="a brief",
        )
        session.add(project)
        session.flush()
        session.add(
            RequirementRow(
                project_id=project.id,
                requirement_key="R-001",
                generation=1,
                text="The unit must say where inference runs.",
                blocking=True,
                source_document="brief",
                source_quote="a quotation long enough to be evidence",
                compiled_by_prompt_id="stages.requirements.compile",
                compiled_by_prompt_version="1.0.0",
            )
        )
        session.add(
            UnitRow(
                project_id=project.id,
                unit_key="U-01",
                ordinal=1,
                title="Where the work happens",
                goal_text="Say where it runs.",
                requirement_keys_json=["R-001"],
                state="auditing",
            )
        )
        return project.id, "U-01"


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    handle = Database.from_url(f"sqlite:///{tmp_path / 'ideapress.sqlite3'}")
    upgrade(handle)
    yield handle
    handle.close()


def test_a_healthy_commit_writes_the_version_its_coverage_and_the_state(
    database: Database,
) -> None:
    """The baseline the failure cases are measured against."""
    project_id, unit_key = _seed(database)
    committed = commit_unit(
        database,
        project_id=project_id,
        unit_key=unit_key,
        text=TEXT,
        coverage=_coverage(),
    )
    assert committed.version == 1
    with database.read() as session:
        assert len(session.scalars(select(UnitVersionRow)).all()) == 1
        assert len(session.scalars(select(CoverageRow)).all()) == 1
        unit = load_unit(session, project_id, unit_key)
        assert unit.state == "committed"
        assert unit.current_version_id is not None


def test_a_fault_between_the_writes_leaves_no_partial_version(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The version row is written first. A failure after it must leave none of it behind."""
    project_id, unit_key = _seed(database)

    def explode(*args: object, **kwargs: object) -> dict[str, str]:
        message = "the disk went away mid-commit"
        raise OSError(message)

    monkeypatch.setattr("ideapress.services.units._requirement_ids", explode)

    with pytest.raises(OSError, match="mid-commit"):
        commit_unit(
            database,
            project_id=project_id,
            unit_key=unit_key,
            text=TEXT,
            coverage=_coverage(),
        )

    with database.read() as session:
        assert session.scalars(select(UnitVersionRow)).all() == [], "no partial version"
        assert session.scalars(select(CoverageRow)).all() == []
        unit = load_unit(session, project_id, unit_key)
        assert unit.state == "auditing", "the unit did not move"
        assert unit.current_version_id is None
        assert unit.state != "committed", "and is resumable"


def test_a_fault_after_the_coverage_rows_also_leaves_nothing(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later seam: the version and its coverage exist in the transaction and still vanish."""
    project_id, unit_key = _seed(database)

    def explode(*args: object, **kwargs: object) -> None:
        message = "interrupted after coverage"
        raise RuntimeError(message)

    monkeypatch.setattr("ideapress.services.units.assert_transition", explode)

    with pytest.raises(RuntimeError, match="after coverage"):
        commit_unit(
            database,
            project_id=project_id,
            unit_key=unit_key,
            text=TEXT,
            coverage=_coverage(),
        )

    with database.read() as session:
        assert session.scalars(select(UnitVersionRow)).all() == []
        assert session.scalars(select(CoverageRow)).all() == []
        assert load_unit(session, project_id, unit_key).state == "auditing"


def test_committing_an_already_committed_unit_is_refused(database: Database) -> None:
    """A committed version is immutable; a revision makes a new one."""
    from ideapress.errors import StagePreconditionFailed

    project_id, unit_key = _seed(database)
    commit_unit(database, project_id=project_id, unit_key=unit_key, text=TEXT, coverage=_coverage())
    with pytest.raises(StagePreconditionFailed) as caught:
        commit_unit(
            database, project_id=project_id, unit_key=unit_key, text=TEXT, coverage=_coverage()
        )
    assert "immutable" in caught.value.message


def test_a_killed_process_mid_commit_leaves_the_database_untouched(tmp_path: Path) -> None:
    """The strongest evidence: SIGKILL with the transaction open, then reopen from scratch.

    Not an exception — a *death*. The child holds the commit transaction open and signals the
    parent; the parent kills it with SIGKILL, which no handler can intercept and no `finally` can
    tidy after. What the reopened database contains is what a reader would see after a power cut.
    """
    database_path = tmp_path / "ideapress.sqlite3"
    marker = tmp_path / "inside-the-transaction"

    setup = Database.from_url(f"sqlite:///{database_path}")
    upgrade(setup)
    project_id, unit_key = _seed(setup)
    setup.close()

    # A plain template with placeholders, not an f-string: an f-string containing the word
    # "select" reads to the linter as SQL construction, and suppressing that would suppress the
    # rule everywhere in this block rather than at the one place it misfires.
    program = (
        textwrap.dedent("""
        import pathlib, time
        from sqlalchemy import select
        from ideapress.services.database import Database
        from ideapress.infrastructure.db.models import Unit, UnitVersion
        from ideapress.domain.commit import content_hash, word_count

        text = __TEXT__
        database = Database.from_url("sqlite:///__DATABASE__")
        with database.write() as session:
            unit = session.scalars(select(Unit).where(Unit.unit_key == "U-01")).one()
            session.add(
                UnitVersion(
                    unit_id=unit.id,
                    version=1,
                    content_text=text,
                    content_hash=content_hash(text),
                    word_count=word_count(text),
                    char_count=len(text),
                    committed=True,
                )
            )
            session.flush()
            # The row exists inside the open transaction. Announce it, then wait to be killed.
            pathlib.Path(__MARKER__).write_text("in", encoding="utf-8")
            time.sleep(30)
        """)
        .replace("__TEXT__", repr(TEXT))
        .replace("__DATABASE__", str(database_path))
        .replace("__MARKER__", repr(str(marker)))
    )

    child = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
        [sys.executable, "-c", program], cwd=str(tmp_path)
    )
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not marker.exists():
            time.sleep(0.02)
        assert marker.exists(), "the child never reached the open transaction"
        os.kill(child.pid, signal.SIGKILL)
    finally:
        child.wait(timeout=20)
    assert child.returncode == -signal.SIGKILL

    reopened = Database.from_url(f"sqlite:///{database_path}")
    try:
        with reopened.read() as session:
            assert session.scalars(select(UnitVersionRow)).all() == [], (
                "a killed process left a partial version behind"
            )
            assert session.scalars(select(CoverageRow)).all() == []
            unit = load_unit(session, project_id, unit_key)
            assert unit.state == "auditing", "the unit is where it was"
            assert unit.current_version_id is None
        # And the unit can still be committed afterwards: resumable, not wedged.
        committed = commit_unit(
            reopened,
            project_id=project_id,
            unit_key=unit_key,
            text=TEXT,
            coverage=_coverage(),
        )
        assert committed.version == 1, "version 1 is still free; nothing was consumed"
    finally:
        reopened.close()
