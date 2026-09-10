"""Row WI1: `config schema` and `config show` name the layer behind every runtime key.

WeightRoomGym labels each Settings field with the schema document's `sources` (ADR-0127 rule 1),
and configuration standards §7 asks `config show` to mark a database-sourced value. IdeaPress
tracked sources only to `section.field`, so a stage binding set in `config.toml` read `default` on
the console, and a stored row deciding the value was not mentioned at all: row WI1's demonstration
found the console showing the row's value labelled `default`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from ideapress.cli.main import app
from ideapress.config import load_settings
from ideapress.services.config_schema import build_schema_document
from ideapress.services.runtime import build_runtime
from ideapress.services.settings import write_runtime_settings

if TYPE_CHECKING:
    from pathlib import Path

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
DRAFT = "models.stages.draft"


def _config(tmp_path: Path) -> Path:
    config = tmp_path / "ideapress.toml"
    config.write_text('[models.stages]\ndraft = "ollama/qwen3.5:9b-q8_0"\n', encoding="utf-8")
    return config


def _store(config: Path, changes: dict[str, object]) -> None:
    runtime = build_runtime(load_settings(config_path=config).settings)
    try:
        write_runtime_settings(runtime.storage, changes, settings=runtime.configured, now=NOW)
    finally:
        runtime.close()


def test_a_nested_leaf_names_its_own_layer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)

    sources = load_settings(config_path=config).sources
    assert sources[DRAFT] == "file"
    assert sources["models.stages.outline"] == "default"
    assert sources["inference.ollama.base_url"] == "default"

    monkeypatch.setenv("IDEAPRESS_MODELS__STAGES__OUTLINE", "ollama/from-env:1b")
    assert load_settings(config_path=config).sources["models.stages.outline"] == (
        "env IDEAPRESS_MODELS__STAGES__OUTLINE"
    )


def test_the_schema_document_marks_a_value_a_stored_row_decides(tmp_path: Path) -> None:
    config = _config(tmp_path)

    _store(config, {DRAFT: "ollama/gemma4:12b"})
    assert build_schema_document(config_path=config)["sources"][DRAFT] == "database"

    _store(config, {DRAFT: None})
    assert build_schema_document(config_path=config)["sources"][DRAFT] == "file"


def test_a_shadowed_row_is_named_beside_the_variable_that_beats_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    _store(config, {DRAFT: "ollama/gemma4:12b"})
    monkeypatch.setenv("IDEAPRESS_MODELS__STAGES__DRAFT", "ollama/from-env:1b")

    assert build_schema_document(config_path=config)["sources"][DRAFT] == (
        "env IDEAPRESS_MODELS__STAGES__DRAFT; database row 'ollama/gemma4:12b' shadowed"
    )


def test_config_show_prints_the_stored_value_marked_database(tmp_path: Path) -> None:
    """Configuration standards §7: `config show` marks database-sourced values `(database)`."""
    config = _config(tmp_path)
    _store(config, {"workflow.max_revision_rounds": 2})
    runner = CliRunner()

    text = runner.invoke(app, ["config", "show", "--config", str(config)])
    assert text.exit_code == 0, text.output
    line = next(
        row for row in text.output.splitlines() if row.startswith("workflow.max_revision_rounds ")
    )
    assert line.split()[1:] == ["2", "[database]"]

    shown = runner.invoke(app, ["config", "show", "--json", "--config", str(config)])
    body = json.loads(shown.output)
    assert body["values"]["workflow"]["max_revision_rounds"] == 2
    assert body["sources"]["workflow.max_revision_rounds"] == "database"


def test_describing_the_configuration_creates_no_database(
    tmp_path: Path, isolated_environment: Path
) -> None:
    """An inspection command must not leave a database behind for `db status` to find."""
    config = _config(tmp_path)

    build_schema_document(config_path=config)
    CliRunner().invoke(app, ["config", "show", "--config", str(config)])

    assert list(isolated_environment.rglob("*.sqlite3")) == []
