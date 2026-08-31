"""The CLI skeleton: every command answers, and none of them needs a backend."""

from __future__ import annotations

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


def test_config_validate_reports_a_valid_default() -> None:
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 0
    assert "valid" in result.stdout.lower()


def test_config_validate_exits_two_on_a_bad_key(tmp_path: Path) -> None:
    config = tmp_path / "bad.toml"
    config.write_text("[server]\nprot = 1\n", encoding="utf-8")
    result = runner.invoke(app, ["config", "validate", "--config", str(config)])
    assert result.exit_code == 2


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
