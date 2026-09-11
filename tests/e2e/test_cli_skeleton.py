"""The CLI skeleton: every command answers, and none of them needs a backend."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ideapress.cli.main import app

runner = CliRunner()


def test_version_flag_and_command_agree() -> None:
    from ideapress.__about__ import __version__

    assert __version__ in runner.invoke(app, ["--version"]).stdout
    assert __version__ in runner.invoke(app, ["version"]).stdout


def test_version_json_is_machine_readable() -> None:
    import json

    payload = json.loads(runner.invoke(app, ["version", "--json"]).stdout)
    assert payload["application"] == "ideapress"


def test_health_exits_zero_with_no_backend() -> None:
    """A backend that is not running is degraded, not an outage."""
    result = runner.invoke(app, ["health"])
    assert result.exit_code == 0
    assert "backend" in result.stdout


def test_doctor_exits_zero_and_names_every_component() -> None:
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    for name in ("configuration", "data directory", "database", "backend", "prompts"):
        assert name in result.stdout


def test_doctor_fails_loudly_on_invalid_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "bad.toml"
    config.write_text("[execution]\nmax_concurrent_stages = 4\n", encoding="utf-8")
    monkeypatch.setenv("IDEAPRESS_CONFIG", str(config))
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "max_concurrent_stages" in result.stdout


def test_config_show_names_the_layer_behind_every_value() -> None:
    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 0
    assert "[default]" in result.stdout
    assert "server.port" in result.stdout


def test_config_show_json_names_the_block_values_like_every_other_application() -> None:
    """ADR-0131: the effective configuration is `values`, as in the other four `config show`."""
    result = runner.invoke(app, ["config", "show", "--json"])
    assert result.exit_code == 0
    body = json.loads(result.stdout)
    assert "values" in body
    assert "settings" not in body
    assert "server" in body["values"]


def test_config_show_exits_two_cleanly_on_an_invalid_config(tmp_path: Path) -> None:
    """M7 finding 4: a mistyped config gets the refusal's message, never a traceback."""
    config = tmp_path / "bad.toml"
    config.write_text("[execution]\nmax_concurrent_stages = 2\n", encoding="utf-8")
    result = runner.invoke(app, ["config", "show", "--config", str(config)])
    assert result.exit_code == 2
    assert "max_concurrent_stages" in result.output
    assert "Traceback" not in result.output


def test_config_validate_reports_a_valid_default() -> None:
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 0
    assert "valid" in result.stdout.lower()


def test_config_validate_exits_two_on_a_bad_key(tmp_path: Path) -> None:
    config = tmp_path / "bad.toml"
    config.write_text("[server]\nprot = 1\n", encoding="utf-8")
    result = runner.invoke(app, ["config", "validate", "--config", str(config)])
    assert result.exit_code == 2


def test_config_validate_file_accepts_a_valid_candidate(tmp_path: Path) -> None:
    """ADR-0127 rule 2: WeightRoomGym's pre-write check."""
    candidate = tmp_path / "candidate.toml"
    candidate.write_text("[server]\nport = 9010\n", encoding="utf-8")
    result = runner.invoke(app, ["config", "validate", "--file", str(candidate)])
    assert result.exit_code == 0
    assert "candidate" in result.stdout.lower()


def test_config_validate_file_names_an_unknown_key(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.toml"
    candidate.write_text("[server]\nprot = 1\n", encoding="utf-8")
    result = runner.invoke(app, ["config", "validate", "--file", str(candidate)])
    assert result.exit_code == 2
    assert "server.prot" in result.output
    assert "Traceback" not in result.output


def test_config_validate_file_refuses_an_insecure_bind(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.toml"
    candidate.write_text('[server]\nhost = "0.0.0.0"\n', encoding="utf-8")
    result = runner.invoke(app, ["config", "validate", "--file", str(candidate)])
    assert result.exit_code == 2
    assert "allow_lan_exposure" in result.output


def test_config_validate_file_reports_a_missing_candidate_cleanly(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.toml"
    result = runner.invoke(app, ["config", "validate", "--file", str(missing)])
    assert result.exit_code == 2
    assert "does not exist" in result.output
    assert "Traceback" not in result.output


def test_config_validate_file_never_touches_the_applications_own_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    own = tmp_path / "own-config.toml"
    own.write_text("[server]\nport = 8767\n", encoding="utf-8")
    monkeypatch.setenv("IDEAPRESS_CONFIG", str(own))
    before = own.read_bytes()

    candidate = tmp_path / "candidate.toml"
    candidate.write_text("[server]\nprot = 1\n", encoding="utf-8")
    result = runner.invoke(app, ["config", "validate", "--file", str(candidate)])

    assert result.exit_code == 2
    assert own.read_bytes() == before


def test_config_schema_json_is_canonical() -> None:
    import json

    result = runner.invoke(app, ["config", "schema", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["application"] == "ideapress"
    assert payload["schema_version"] == "1.0"
    keys = {entry["key"] for entry in payload["runtime_changeable"]}
    assert "workflow.max_revision_rounds" in keys
    assert result.stdout == json.dumps(payload, indent=2, sort_keys=True) + "\n"


def test_config_schema_human_readable_by_default() -> None:
    result = runner.invoke(app, ["config", "schema"])
    assert result.exit_code == 0
    assert "schema_version" in result.stdout
    assert "runtime_changeable" in result.stdout


def test_config_schema_names_a_tolerated_unknown_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "ideapress.toml"
    config.write_text("[server]\nprot = 1\n", encoding="utf-8")
    monkeypatch.setenv("IDEAPRESS_CONFIG", str(config))
    result = runner.invoke(app, ["config", "schema"])
    assert result.exit_code == 0
    assert "server.prot" in result.stdout


def test_config_schema_refuses_the_same_insecure_bind_validate_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "ideapress.toml"
    config.write_text('[server]\nhost = "0.0.0.0"\n', encoding="utf-8")
    monkeypatch.setenv("IDEAPRESS_CONFIG", str(config))
    result = runner.invoke(app, ["config", "schema"])
    assert result.exit_code == 2
    assert "allow_lan_exposure" in result.output
    assert "Traceback" not in result.output


def test_config_init_writes_a_file_it_then_accepts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "written.toml"
    monkeypatch.setenv("IDEAPRESS_CONFIG", str(target))
    assert runner.invoke(app, ["config", "init"]).exit_code == 0
    assert target.is_file()
    assert runner.invoke(app, ["config", "validate"]).exit_code == 0


def test_config_init_refuses_to_overwrite_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "written.toml"
    target.write_text("# mine\n", encoding="utf-8")
    monkeypatch.setenv("IDEAPRESS_CONFIG", str(target))
    result = runner.invoke(app, ["config", "init"])
    assert result.exit_code == 1
    assert target.read_text(encoding="utf-8") == "# mine\n"
    assert runner.invoke(app, ["config", "init", "--force"]).exit_code == 0
    assert "# mine" not in target.read_text(encoding="utf-8")


def test_config_path_reports_four_locations() -> None:
    result = runner.invoke(app, ["config", "path"])
    for label in ("config file", "config dir", "data dir", "state dir"):
        assert label in result.stdout


def test_help_does_not_import_the_web_layer() -> None:
    """CLI standards §12: --help must not pull in FastAPI, uvicorn or a database driver."""
    import subprocess
    import sys

    probe = (
        "import sys; from ideapress.cli.main import app; "
        "print(','.join(m for m in ('fastapi','uvicorn','sqlalchemy','httpx') if m in sys.modules))"
    )
    # `cwd` is the repository root, not the test's temporary directory. pytest-cov starts
    # coverage inside a subprocess through a `.pth` hook, and that hook reads `pyproject.toml`
    # relative to the working directory: from anywhere else it measures without `branch = true`
    # and writes a data file the parent's cannot combine with, which fails the whole run inside
    # pytest-cov rather than as a test failure. Only the `--cov` invocation sees it, so the
    # default gate is green while the coverage job is red.
    output = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        cwd=Path(__file__).resolve().parents[2],
    )
    assert output.stdout.strip() == ""


def test_db_backup_writes_a_file_into_the_directory_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ideapress db backup` handed weightsdb the *directory* and failed with IsADirectoryError,
    with or without --output (WI1_HANDOFF.md §7). It writes a stamped file inside it now."""
    monkeypatch.setenv("IDEAPRESS_STORAGE__DATABASE_URL", f"sqlite:///{tmp_path}/ideapress.sqlite3")
    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
    named = tmp_path / "named"
    result = runner.invoke(app, ["db", "backup", "--output", str(named)])
    assert result.exit_code == 0, result.output
    (written,) = list(named.glob("ideapress-*.sqlite3"))
    assert written.stat().st_size > 0 and written.name in result.stdout
    default = runner.invoke(app, ["db", "backup"])
    assert default.exit_code == 0, default.output
    assert "/backups/ideapress-" in default.stdout
