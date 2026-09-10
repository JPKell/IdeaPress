"""Prompt standards §6 in IdeaPress (row W9): an override replaces the shipped record, and every
attempt that rendered one says so.

The conftest points ``XDG_CONFIG_HOME`` at the test's own tree, so each override here is written
where :func:`~ideapress.config.prompt_override_dir` looks, and nowhere a real IdeaPress would
read it.
The pack is cached per process, so every test clears the cache before and after.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from mirrorwall import ComponentStatus
from setspec.prompts import PromptPackInvalid
from sqlalchemy import select, text
from typer.testing import CliRunner

from ideapress.cli.main import app
from ideapress.config import prompt_override_dir
from ideapress.domain.inference import StageResult
from ideapress.infrastructure.db.models import Attempt as AttemptRow
from ideapress.infrastructure.db.models import Project, StageRun
from ideapress.services.database import Database, migration_runner, upgrade
from ideapress.services.prompts import (
    PACK_ROOT,
    library,
    prompt_source,
    prompts_health_component,
    render,
    shipped_library,
)
from ideapress.services.stages import record_attempt

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def fresh_pack() -> Iterator[None]:
    """Each test reads the pack after writing its own override, never a cached earlier one."""
    library.cache_clear()
    shipped_library.cache_clear()
    yield
    library.cache_clear()
    shipped_library.cache_clear()


@pytest.fixture
def sqlite_database(tmp_path: Path) -> Iterator[Database]:
    """A migrated, empty database of IdeaPress's own schema."""
    database = Database.from_url(f"sqlite:///{tmp_path / 'ideapress.sqlite3'}")
    upgrade(database)
    yield database
    database.close()


@pytest.fixture
def stage_run(sqlite_database: Database) -> str:
    """One project and one stage run, so an attempt has parents to belong to."""
    with sqlite_database.write() as session:
        project = Project(
            title="T",
            slug="t",
            content_type="article",
            content_type_version="1.0",
            workflow_id="default",
            workflow_version="1.0",
            status="active",
            brief_text="b",
            author_material_json=[],
            config_json={},
        )
        session.add(project)
        session.flush()
        run = StageRun(
            project_id=project.id,
            stage="draft",
            state="completed",
            units_total=1,
            units_completed=1,
            units_paused=0,
            started_at=datetime.now(UTC),
            options_json={},
            backend="ollama",
            backend_mode="ollama",
        )
        session.add(run)
        session.flush()
        return str(run.id)


def _write_override(version: str = "1.1.0") -> Path:
    body = json.loads((PACK_ROOT / "stages" / "hello.v1.json").read_text(encoding="utf-8"))
    body["version"] = version
    body["template"] = "Welcome the reader of a document titled {{ title }} in one sentence."
    body["metadata"]["change_reason"] = "The operator's own greeting, for the override tests."
    directory = prompt_override_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "stages.hello.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def _sources(database: Database) -> dict[int, str | None]:
    with database.read() as session:
        rows = session.execute(select(AttemptRow)).scalars().all()
        return {row.attempt: row.prompt_source for row in rows}


def test_the_override_directory_is_under_the_configuration_root(tmp_path: Path) -> None:
    assert prompt_override_dir() == tmp_path / "xdg-config" / "ideapress" / "prompts"


def test_an_override_replaces_the_shipped_record_and_the_shipped_pack_stays_readable() -> None:
    _write_override()

    effective = library().get("stages.hello")

    assert (effective.version, effective.source) == ("1.1.0", "user_override")
    assert "Welcome the reader" in render("stages.hello", {"title": "A study"}).user
    shipped = shipped_library().get("stages.hello")
    assert (shipped.version, shipped.source) == ("1.0.0", "pack")
    assert prompt_source("stages.hello", "1.1.0") == "user_override"
    assert prompt_source("stages.draft.write", None) == "pack"
    assert prompt_source(None, None) is None
    assert prompt_source("stages.not_in_the_pack", None) is None
    assert prompts_health_component().data["overridden"] == ["stages.hello"]


def test_an_attempt_records_whether_its_prompt_was_overridden(
    sqlite_database: Database, stage_run: str
) -> None:
    shipped = render("stages.draft.write", {"context": "c"})
    _write_override()
    library.cache_clear()
    overridden = render("stages.hello", {"title": "T"})
    for number, prompt in ((1, overridden), (2, shipped)):
        record_attempt(
            sqlite_database,
            stage_run_id=stage_run,
            stage="draft",
            result=StageResult(text="hello.", backend="ollama"),
            attempt=number,
            prompt_id=prompt.prompt_id,
            prompt_version=prompt.version,
            prompt_sha256=prompt.sha256,
        )
    record_attempt(sqlite_database, stage_run_id=stage_run, stage="draft", result=None, attempt=3)

    assert _sources(sqlite_database) == {1: "user_override", 2: "pack", 3: None}


def test_an_override_that_does_not_load_stops_the_pack_but_not_the_shipped_one() -> None:
    directory = prompt_override_dir()
    directory.mkdir(parents=True)
    (directory / "stages.hello.json").write_text('{"prompt_id": "stages.hello"}', encoding="utf-8")

    with pytest.raises(PromptPackInvalid):
        library()
    assert prompts_health_component().status is ComponentStatus.UNAVAILABLE
    assert shipped_library().get("stages.hello").source == "pack"


def test_the_cli_lists_what_a_stage_renders_and_with_shipped_what_was_installed() -> None:
    _write_override()
    runner = CliRunner()

    effective = json.loads(runner.invoke(app, ["prompts", "list", "--json"]).output)
    hello = next(one for one in effective if one["prompt_id"] == "stages.hello")
    assert (hello["source"], hello["version"]) == ("user_override", "1.1.0")
    assert hello["sha256"].startswith("sha256:")
    shipped = json.loads(runner.invoke(app, ["prompts", "list", "--shipped", "--json"]).output)
    assert next(one for one in shipped if one["prompt_id"] == "stages.hello")["source"] == "pack"
    record = json.loads(
        runner.invoke(app, ["prompts", "show", "stages.hello", "--shipped", "--json"]).output
    )
    assert (record["version"], record["schema_version"]) == ("1.0.0", "1.0")
    text_listing = runner.invoke(app, ["prompts", "list"]).output
    assert "(user_override)" in text_listing


def test_0011_marks_every_earlier_attempt_with_a_prompt_as_pack(tmp_path: Path) -> None:
    """No build before 0011 could load an override, so `pack` is a fact about those attempts."""
    database = Database.from_url(f"sqlite:///{tmp_path / 'at-0010.sqlite3'}")
    try:
        runner = migration_runner(database.engine)
        runner.upgrade("0010")
        with database.engine.begin() as connection:
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
                    " units_completed, units_paused, started_at, options_json, backend,"
                    " backend_mode) VALUES ('01RUN', '01PROJECT', 'draft', 'completed', 1, 1, 0,"
                    " '2026-09-01T00:00:00', '{}', 'ollama', 'ollama')"
                )
            )
            for attempt_id, number, prompt_id in (
                ("01PROMPTED", 1, "stages.draft.write"),
                ("01BARE", 2, None),
            ):
                connection.execute(
                    text(
                        "INSERT INTO attempts (id, stage_run_id, stage, attempt, round, backend,"
                        " backend_mode, outcome, prompt_id, degradations_json, created_at)"
                        " VALUES (:id, '01RUN', 'draft', :number, 0, 'ollama', 'ollama',"
                        " 'completed', :prompt_id, '[]', '2026-09-01T00:00:00')"
                    ),
                    {"id": attempt_id, "number": number, "prompt_id": prompt_id},
                )

        runner.upgrade("head")

        assert _sources(database) == {1: "pack", 2: None}
    finally:
        database.close()
