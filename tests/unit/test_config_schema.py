"""The ADR-0127 rule 1 settings schema document — WeightRoomGym's form generator reads this.

The document is built from objects that already exist and already have tests
(`Settings.model_json_schema()`, the runtime/security registry, `config_reference.sections()`,
`load_settings`'s `sources`). These tests guard the assembly, not the sources.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ideapress.domain.stages import MODEL_STAGES
from ideapress.services.config_schema import (
    _all_leaf_keys,
    _resolve_in_json_schema,
    build_schema_document,
)
from ideapress.services.settings_registry import CONFIG_ONLY_KEYS, RUNTIME_KEYS

GOLDEN = Path(__file__).parent / "goldens" / "config_schema_1_0.json"


@pytest.fixture
def _pinned_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the XDG lookups the golden was generated under, so the document stays machine-free."""
    monkeypatch.setenv("XDG_CONFIG_HOME", "/golden/config")
    monkeypatch.setenv("IDEAPRESS_DATA_DIR", "/golden/data")


@pytest.mark.usefixtures("_pinned_paths")
def test_schema_document_matches_golden() -> None:
    """Regenerate from `docs/history/handoffs/WS3_HANDOFF.md`'s snippet if this fails on purpose."""
    document = build_schema_document()
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert document == expected


def test_every_runtime_and_security_key_exists_in_the_json_schema() -> None:
    """Gate A: a key the document names but the schema cannot resolve is a defect, not a typo."""
    document = build_schema_document()
    json_schema = document["json_schema"]
    for entry in document["runtime_changeable"]:
        _resolve_in_json_schema(json_schema, entry["key"])  # raises KeyError if absent
    for key in document["security_keys"]:
        _resolve_in_json_schema(json_schema, key)


def test_runtime_changeable_has_the_registry_plus_every_model_stage() -> None:
    document = build_schema_document()
    keys = {entry["key"] for entry in document["runtime_changeable"]}
    assert keys == RUNTIME_KEYS | {f"models.stages.{stage}" for stage in MODEL_STAGES}


def test_security_keys_is_the_config_only_registry_verbatim() -> None:
    document = build_schema_document()
    assert set(document["security_keys"]) == CONFIG_ONLY_KEYS


def test_config_only_covers_every_other_leaf_exactly_once() -> None:
    """No leaf is in two buckets, and none is missing from all three."""
    document = build_schema_document()
    runtime = {entry["key"] for entry in document["runtime_changeable"]}
    security = set(document["security_keys"])
    config_only = set(document["config_only"])
    assert runtime & security == set()
    assert runtime & config_only == set()
    assert security & config_only == set()
    assert runtime | security | config_only == set(_all_leaf_keys())


def test_runtime_entries_always_carry_the_five_agreed_fields() -> None:
    """ADR-0127 rule 1: the one shape every application's document emits."""
    document = build_schema_document()
    for entry in document["runtime_changeable"]:
        assert set(entry) == {"key", "kind", "minimum", "maximum", "description"}


def test_bounded_runtime_keys_carry_their_pydantic_bounds() -> None:
    document = build_schema_document()
    by_key = {entry["key"]: entry for entry in document["runtime_changeable"]}
    assert by_key["workflow.max_revision_rounds"]["minimum"] == 0
    assert by_key["workflow.max_revision_rounds"]["maximum"] == 100
    assert by_key["workflow.context_budget_tokens"]["minimum"] == 256
    assert by_key["workflow.context_budget_tokens"]["maximum"] is None
    assert by_key["logging.level"]["kind"] == "string"
    assert by_key["logging.level"]["minimum"] is None
    assert by_key["workflow.require_clean_validation_to_commit"]["kind"] == "boolean"


def test_an_unknown_key_in_the_current_file_is_reported_under_problems(tmp_path: Path) -> None:
    """ADR-0127 rule 1: never dropped, and the document still builds around it."""
    config = tmp_path / "ideapress.toml"
    config.write_text("[server]\nprot = 1\n", encoding="utf-8")

    document = build_schema_document(config_path=config)

    assert document["problems"], "an unknown key must be reported, not dropped"
    assert any("server.prot" in problem for problem in document["problems"])
    assert document["sources"]["server.port"] == "default"
    assert document["schema_version"] == "1.0", "the document still builds despite the bad key"


def test_sources_survive_around_a_stripped_unknown_key(tmp_path: Path) -> None:
    config = tmp_path / "ideapress.toml"
    config.write_text("[server]\nport = 9001\nprot = 1\n", encoding="utf-8")

    document = build_schema_document(config_path=config)

    assert any("server.prot" in problem for problem in document["problems"])
    assert document["sources"]["server.port"] == "file"


def test_a_clean_file_has_no_problems_and_real_sources(tmp_path: Path) -> None:
    config = tmp_path / "ideapress.toml"
    config.write_text("[server]\nport = 9001\n", encoding="utf-8")

    document = build_schema_document(config_path=config)

    assert document["problems"] == []
    assert document["sources"]["server.port"] == "file"


def test_a_genuine_validation_error_is_not_swallowed(tmp_path: Path) -> None:
    """Only an unknown key is tolerated; an unsafe bind is still this command's job to refuse."""
    from ideapress.config import ConfigurationError

    config = tmp_path / "ideapress.toml"
    config.write_text('[server]\nhost = "0.0.0.0"\n', encoding="utf-8")

    with pytest.raises(ConfigurationError):
        build_schema_document(config_path=config)


def test_the_document_never_carries_a_live_secret_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Configuration standards §6: a config file names a secret's *location*, never the secret.

    `inference.loadcoach.api_key_env` holds the name of an environment variable, not a token. The
    document carries no configured *values* at all — `json_schema` is class-level (defaults only),
    `sources` names layers, `problems` names keys — but this proves it directly: even with the
    named variable genuinely set to a secret in the process environment, the secret's value is
    nowhere in the serialized document.
    """
    monkeypatch.setenv("LOADCOACH_TEST_API_KEY", "sk-do-not-leak-this-value")
    config = tmp_path / "ideapress.toml"
    config.write_text(
        '[inference]\nmode = "loadcoach"\n'
        "[inference.loadcoach]\n"
        'api_key_env = "LOADCOACH_TEST_API_KEY"\n',
        encoding="utf-8",
    )

    document = build_schema_document(config_path=config)

    serialized = json.dumps(document)
    assert "sk-do-not-leak-this-value" not in serialized
