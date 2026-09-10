"""Row WI1 Gate B: a stored runtime setting is resolved with ADR-0100 rule 5's precedence.

Configuration standards §7: ``defaults → file → database → env → CLI``. A row beats the file, the
environment beats a row and says so, and ``null`` through ``PUT /settings`` removes a row and hands
the key back to configuration. When a resolved value reaches a running stage is
``test_stage_recovery.py``'s half, beside the stages that read it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import select

from ideapress.config import load_settings
from ideapress.errors import ValidationFailed
from ideapress.infrastructure.db.models import Setting as SettingRow
from ideapress.services.runtime import Runtime, build_runtime
from ideapress.services.settings import (
    SettingConfigOnly,
    read_runtime_settings,
    runtime_settings_document,
    write_runtime_settings,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
KEY = "workflow.max_revision_rounds"
FILE = "[workflow]\nmax_revision_rounds = 5\n"


def _runtime(tmp_path: Path, toml: str = FILE) -> Runtime:
    config = tmp_path / "ideapress.toml"
    config.write_text(toml, encoding="utf-8")
    return build_runtime(load_settings(config_path=config).settings)


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[Runtime]:
    built = _runtime(tmp_path)
    yield built
    built.close()


def _write(runtime: Runtime, changes: dict[str, Any]) -> dict[str, Any]:
    return write_runtime_settings(runtime.storage, changes, settings=runtime.configured, now=NOW)


def _document(runtime: Runtime) -> dict[str, Any]:
    return runtime_settings_document(runtime.storage, settings=runtime.configured)


def test_with_no_row_the_file_decides(runtime: Runtime) -> None:
    definition = _document(runtime)["definitions"][KEY]

    assert (definition["configured"], definition["stored"]) == (5, None)
    assert definition["source"] == "configuration"
    assert read_runtime_settings(runtime.storage, settings=runtime.configured)[KEY] == 5


def test_a_row_beats_the_file(runtime: Runtime) -> None:
    assert _write(runtime, {KEY: 2})[KEY] == 2

    definition = _document(runtime)["definitions"][KEY]
    assert (definition["configured"], definition["stored"]) == (5, 2)
    assert definition["source"] == "database"
    assert definition["shadowed_by"] is None


def test_the_environment_beats_a_row_and_the_document_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IDEAPRESS_WORKFLOW__MAX_REVISION_ROUNDS", "7")
    runtime = _runtime(tmp_path)
    try:
        assert _write(runtime, {KEY: 2})[KEY] == 7, "the row is kept, and does nothing"

        definition = _document(runtime)["definitions"][KEY]
        assert (definition["configured"], definition["stored"]) == (7, 2)
        assert definition["source"] == "configuration"
        assert definition["shadowed_by"] == "env IDEAPRESS_WORKFLOW__MAX_REVISION_ROUNDS"
    finally:
        runtime.close()


def test_a_stage_binding_row_is_shadowed_by_its_nested_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IDEAPRESS_MODELS__STAGES__DRAFT", "ollama/from-env:1b")
    runtime = _runtime(tmp_path, "")
    try:
        _write(runtime, {"models.stages.draft": "ollama/from-row:1b"})

        document = _document(runtime)
        assert document["settings"]["models.stages.draft"] == "ollama/from-env:1b"
        definition = document["definitions"]["models.stages.draft"]
        assert definition["stored"] == "ollama/from-row:1b"
        assert definition["shadowed_by"] == "env IDEAPRESS_MODELS__STAGES__DRAFT"
    finally:
        runtime.close()


def test_null_removes_the_row_and_hands_the_key_back_to_configuration(runtime: Runtime) -> None:
    _write(runtime, {KEY: 2})

    assert _write(runtime, {KEY: None})[KEY] == 5
    definition = _document(runtime)["definitions"][KEY]
    assert (definition["stored"], definition["source"]) == (None, "configuration")
    with runtime.storage.read() as session:
        assert session.scalars(select(SettingRow)).all() == []


def test_null_for_a_key_with_no_row_changes_nothing(runtime: Runtime) -> None:
    assert _write(runtime, {KEY: None})[KEY] == 5


def test_null_is_refused_like_any_value_for_a_refused_key(runtime: Runtime) -> None:
    _write(runtime, {KEY: 2})

    with pytest.raises(SettingConfigOnly):
        _write(runtime, {KEY: None, "server.host": None})
    with pytest.raises(ValidationFailed, match="workflow.speed"):
        _write(runtime, {KEY: None, "workflow.speed": None})
    assert _document(runtime)["definitions"][KEY]["stored"] == 2, "nothing was cleared"


def test_a_row_this_build_cannot_read_falls_back_to_configuration(runtime: Runtime) -> None:
    """A row written by another version must not stop this one working — nor claim to decide."""
    with runtime.storage.write() as session:
        session.add(SettingRow(key=KEY, value_json="many", updated_at=NOW))

    definition = _document(runtime)["definitions"][KEY]
    assert (definition["stored"], definition["source"]) == ("many", "configuration")
    assert read_runtime_settings(runtime.storage, settings=runtime.configured)[KEY] == 5


@pytest.mark.parametrize(
    ("key", "value"), [("inference.mode", "loadcoach"), ("logging.level", "DEBUG")]
)
def test_a_key_only_a_restart_reads_is_not_runtime_changeable(
    runtime: Runtime, key: str, value: str
) -> None:
    """ADR-0100 rule 1: a key nothing re-reads is not runtime-changeable.

    The backend is built, and logging configured, once per process. A stored row for either would
    be a change the running process ignores until a restart nobody was told to perform; in the
    file, WeightRoomGym shows the restart it needs (ADR-0127 rule 5).
    """
    with pytest.raises(ValidationFailed, match=key):
        _write(runtime, {key: value})
    assert key not in _document(runtime)["settings"]


def test_every_key_says_a_stored_value_applies_from_the_next_stage_start(runtime: Runtime) -> None:
    definitions = _document(runtime)["definitions"]

    assert {definition["applies"] for definition in definitions.values()} == {"next_stage"}
