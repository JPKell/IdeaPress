"""Configuration precedence and refusals.

The precedence chain is defaults → file → ``IDEAPRESS_*`` → CLI overrides, merged per leaf field.
The refusals are the ones that would expose the user's private work, or that would let a
configuration mean something other than it says.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ideapress.config import (
    ConfigurationError,
    InsecureBindingError,
    data_dir,
    load_settings,
    resolve_config_path,
)


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_zero_configuration_loads(isolated_environment: Path) -> None:
    loaded = load_settings()
    assert loaded.config_file_used is False
    assert loaded.settings.server.host == "127.0.0.1"
    assert loaded.settings.server.port == 8767
    assert loaded.settings.inference.mode == "ollama"
    assert loaded.settings.execution.max_concurrent_stages == 1


def test_precedence_defaults_file_env_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _write(tmp_path / "ideapress.toml", "[server]\nport = 9001\n")

    from_file = load_settings(config_path=config)
    assert from_file.settings.server.port == 9001
    assert from_file.sources["server.port"] == "file"

    monkeypatch.setenv("IDEAPRESS_SERVER__PORT", "9002")
    from_env = load_settings(config_path=config)
    assert from_env.settings.server.port == 9002
    assert from_env.sources["server.port"] == "env IDEAPRESS_SERVER__PORT"

    from_cli = load_settings(config_path=config, cli_overrides={"server": {"port": 9003}})
    assert from_cli.settings.server.port == 9003
    assert from_cli.sources["server.port"] == "cli"


def test_override_is_per_leaf_not_per_section(tmp_path: Path) -> None:
    config = _write(tmp_path / "ideapress.toml", "[server]\nport = 9001\nmax_body_bytes = 4096\n")
    loaded = load_settings(config_path=config, cli_overrides={"server": {"port": 9003}})
    assert loaded.settings.server.port == 9003
    assert loaded.settings.server.max_body_bytes == 4096, "siblings must survive an override"


def test_unknown_key_is_refused_with_a_suggestion(tmp_path: Path) -> None:
    config = _write(tmp_path / "ideapress.toml", "[server]\nprot = 9001\n")
    with pytest.raises(ConfigurationError) as caught:
        load_settings(config_path=config)
    assert "server.prot" in caught.value.message
    assert "server.port" in caught.value.message


def test_malformed_toml_names_the_file(tmp_path: Path) -> None:
    config = _write(tmp_path / "ideapress.toml", "[server\nport = 1\n")
    with pytest.raises(ConfigurationError) as caught:
        load_settings(config_path=config)
    assert str(config) in caught.value.message


@pytest.mark.parametrize(
    ("body", "expected_field"),
    [
        ('[server]\nhost = "0.0.0.0"\n', "allow_lan_exposure"),
        ('[server]\nhost = "192.168.1.5"\n', "allowed_hosts"),
    ],
)
def test_unsafe_bind_is_refused(tmp_path: Path, body: str, expected_field: str) -> None:
    config = _write(tmp_path / "ideapress.toml", body)
    with pytest.raises(InsecureBindingError) as caught:
        load_settings(config_path=config)
    assert expected_field in caught.value.message


def test_lan_bind_is_accepted_once_acknowledged(tmp_path: Path) -> None:
    config = _write(
        tmp_path / "ideapress.toml",
        '[server]\nhost = "0.0.0.0"\nallow_lan_exposure = true\nallowed_hosts = ["box.local"]\n',
    )
    loaded = load_settings(config_path=config)
    assert loaded.settings.server.allowed_hosts == ("box.local",)


def test_concurrency_above_one_is_refused_with_the_reason(tmp_path: Path) -> None:
    """ADR-0038 §2: a value above 1 is refused at startup, never silently clamped."""
    config = _write(tmp_path / "ideapress.toml", "[execution]\nmax_concurrent_stages = 2\n")
    with pytest.raises(ConfigurationError) as caught:
        load_settings(config_path=config)
    assert "max_concurrent_stages" in caught.value.message


def test_concurrency_of_one_is_accepted(tmp_path: Path) -> None:
    config = _write(tmp_path / "ideapress.toml", "[execution]\nmax_concurrent_stages = 1\n")
    assert load_settings(config_path=config).settings.execution.max_concurrent_stages == 1


def test_unload_before_model_switch_defaults_on() -> None:
    assert load_settings().settings.execution.unload_before_model_switch is True


def test_fallback_to_itself_is_refused(tmp_path: Path) -> None:
    config = _write(tmp_path / "ideapress.toml", '[inference]\nfallback_mode = "ollama"\n')
    with pytest.raises(ConfigurationError) as caught:
        load_settings(config_path=config)
    assert "fallback_mode" in caught.value.message


def test_fallback_naming_an_unknown_mode_is_refused(tmp_path: Path) -> None:
    config = _write(tmp_path / "ideapress.toml", '[inference]\nfallback_mode = "lodcoach"\n')
    with pytest.raises(ConfigurationError) as caught:
        load_settings(config_path=config)
    assert "lodcoach" in caught.value.message


def test_remote_backend_needs_allow_remote(tmp_path: Path) -> None:
    """Risk S4: the user's briefs and drafts do not leave the machine by default."""
    config = _write(
        tmp_path / "ideapress.toml",
        '[inference]\nmode = "openai_compatible"\n\n'
        '[inference.openai_compatible]\nbase_url = "https://api.example.com/v1"\n',
    )
    with pytest.raises(InsecureBindingError) as caught:
        load_settings(config_path=config)
    assert "allow_remote" in caught.value.message


def test_remote_backend_accepted_once_allowed(tmp_path: Path) -> None:
    config = _write(
        tmp_path / "ideapress.toml",
        '[inference]\nmode = "openai_compatible"\n\n'
        '[inference.openai_compatible]\nbase_url = "https://api.example.com/v1"\n\n'
        "[providers]\nallow_remote = true\n",
    )
    assert load_settings(config_path=config).settings.inference.mode == "openai_compatible"


def test_local_openai_compatible_needs_no_egress_opt_in(tmp_path: Path) -> None:
    config = _write(
        tmp_path / "ideapress.toml",
        '[inference]\nmode = "openai_compatible"\n\n'
        '[inference.openai_compatible]\nbase_url = "http://127.0.0.1:8080/v1"\n',
    )
    assert load_settings(config_path=config).settings.providers.allow_remote is False


def test_openai_compatible_without_a_url_is_refused(tmp_path: Path) -> None:
    config = _write(tmp_path / "ideapress.toml", '[inference]\nmode = "openai_compatible"\n')
    with pytest.raises(ConfigurationError) as caught:
        load_settings(config_path=config)
    assert "base_url" in caught.value.message


def test_postgres_url_turns_auto_migrate_off_unless_stated(tmp_path: Path) -> None:
    """Database standards §5.1: a failed PostgreSQL migration has no automatic rollback."""
    config = _write(
        tmp_path / "ideapress.toml",
        '[storage]\ndatabase_url = "postgresql://localhost/ideapress"\n',
    )
    assert load_settings(config_path=config).settings.storage.auto_migrate is False

    explicit = _write(
        tmp_path / "explicit.toml",
        '[storage]\ndatabase_url = "postgresql://localhost/ideapress"\nauto_migrate = true\n',
    )
    assert load_settings(config_path=explicit).settings.storage.auto_migrate is True


def test_paths_default_under_the_data_directory(isolated_environment: Path) -> None:
    settings = load_settings().settings
    assert (
        settings.storage.database_url == f"sqlite:///{isolated_environment / 'ideapress.sqlite3'}"
    )
    assert settings.storage.project_dir == str(isolated_environment / "projects")
    assert data_dir() == isolated_environment


def test_config_path_resolution_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert resolve_config_path() == Path(tmp_path / "xdg-config") / "ideapress" / "config.toml"
    local = _write(tmp_path / "ideapress.toml", "")
    assert resolve_config_path() == local
    monkeypatch.setenv("IDEAPRESS_CONFIG", str(tmp_path / "named.toml"))
    assert resolve_config_path() == tmp_path / "named.toml"
    assert resolve_config_path(tmp_path / "explicit.toml") == tmp_path / "explicit.toml"


def test_example_config_is_valid(tmp_path: Path) -> None:
    """`config init` must write something `config validate` accepts."""
    from ideapress.config import EXAMPLE_CONFIG_TOML

    config = _write(tmp_path / "example.toml", EXAMPLE_CONFIG_TOML)
    loaded = load_settings(config_path=config)
    assert loaded.config_file_used is True
    assert loaded.settings.models.stages.draft == "ollama/gemma4:12b"


def test_structured_output_tokens_defaults_and_loads_from_file(tmp_path: Path) -> None:
    """M7 finding 1c: the budget is a `config.toml` lever, defaulting to the measured floor."""
    assert load_settings().settings.workflow.structured_output_tokens == 8192

    config = _write(tmp_path / "ideapress.toml", "[workflow]\nstructured_output_tokens = 16384\n")
    loaded = load_settings(config_path=config)
    assert loaded.settings.workflow.structured_output_tokens == 16384
    assert loaded.sources["workflow.structured_output_tokens"] == "file"


@pytest.mark.parametrize("value", [512, 1_000_000])
def test_structured_output_tokens_out_of_range_is_refused(tmp_path: Path, value: int) -> None:
    """The documented range is 1024-131072; outside it the refusal names the field."""
    config = _write(
        tmp_path / "ideapress.toml", f"[workflow]\nstructured_output_tokens = {value}\n"
    )
    with pytest.raises(ConfigurationError, match="structured_output_tokens"):
        load_settings(config_path=config)
