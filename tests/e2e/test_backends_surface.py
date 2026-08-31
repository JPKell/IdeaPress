"""`GET /backends`, `POST /backends/test`, the backend page and `ideapress backend`.

The property that matters here is that a person can see where their content goes without reading
a configuration file (risk S4), and that a backend that is not running is a reported state rather
than an error.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from ideapress.cli.main import app as cli_app
from ideapress.config import Settings, load_settings
from ideapress.services.runtime import build_runtime
from ideapress.web.app import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator

LOOPBACK = "http://127.0.0.1:8767"
runner = CliRunner()


@pytest.fixture
def offline_settings() -> Settings:
    """Configuration pointing at a port nothing listens on: the AC7 state."""
    settings = load_settings().settings
    settings.inference.ollama.base_url = "http://127.0.0.1:1"
    settings.inference.ollama.timeout_seconds = 1
    return settings


@pytest.fixture
def client(offline_settings: Settings) -> Iterator[TestClient]:
    app = create_app(offline_settings, runtime_builder=build_runtime)
    with TestClient(app, base_url=LOOPBACK) as test_client:
        yield test_client


def test_backends_lists_the_selected_one_with_its_capabilities(client: TestClient) -> None:
    backends = client.get("/api/v1/backends").json()["backends"]
    assert len(backends) == 1
    entry = backends[0]
    assert entry["mode"] == "ollama"
    assert entry["selected"] is True
    assert entry["available"] is False
    assert set(entry["capabilities"]) >= {
        "streaming",
        "structured_output",
        "token_counts",
        "residency_control",
    }


def test_a_local_backend_is_not_flagged_as_egress(client: TestClient) -> None:
    entry = client.get("/api/v1/backends").json()["backends"][0]
    assert entry["egress"] is False
    assert entry["is_remote"] is False


def test_a_remote_backend_is_flagged_as_egress(offline_settings: Settings) -> None:
    """Risk S4: the UI states plainly, per backend, that content leaves the machine."""
    offline_settings.providers.allow_remote = True
    offline_settings.inference.mode = "openai_compatible"
    offline_settings.inference.openai_compatible.base_url = "https://api.example.com/v1"
    app = create_app(offline_settings, runtime_builder=build_runtime)
    with TestClient(app, base_url=LOOPBACK) as client:
        entry = client.get("/api/v1/backends").json()["backends"][0]
        assert entry["is_remote"] is True
        assert entry["egress"] is True
        page = client.get("/backends", headers={"accept": "text/html"}).text
        assert "sends your content off this machine" in page


def test_backend_test_reports_an_outage_rather_than_raising(client: TestClient) -> None:
    report = client.post("/api/v1/backends/test", json={}).json()
    assert report["mode"] == "ollama"
    assert report["status"] in {"unavailable", "degraded"}
    assert report["model_count"] == 0
    assert isinstance(report["latency_ms"], float)


def test_the_backend_page_renders_with_nothing_running(client: TestClient) -> None:
    page = client.get("/backends", headers={"accept": "text/html"})
    assert page.status_code == 200
    assert "ollama" in page.text
    assert "Residency control" in page.text


def test_the_openai_backend_reports_no_residency_control(offline_settings: Settings) -> None:
    """Honest capability reporting: the protocol has no unload, so it says so."""
    offline_settings.providers.allow_remote = True
    offline_settings.inference.mode = "openai_compatible"
    offline_settings.inference.openai_compatible.base_url = "https://api.example.com/v1"
    app = create_app(offline_settings, runtime_builder=build_runtime)
    with TestClient(app, base_url=LOOPBACK) as client:
        entry = client.get("/api/v1/backends").json()["backends"][0]
        assert entry["capabilities"]["residency_control"] is False


def test_cli_backend_list_marks_egress(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IDEAPRESS_INFERENCE__MODE", "openai_compatible")
    monkeypatch.setenv(
        "IDEAPRESS_INFERENCE__OPENAI_COMPATIBLE__BASE_URL", "https://api.example.com/v1"
    )
    monkeypatch.setenv("IDEAPRESS_PROVIDERS__ALLOW_REMOTE", "true")
    result = runner.invoke(cli_app, ["backend", "list"])
    assert result.exit_code == 0
    assert "EGRESS" in result.stdout


def test_cli_backend_test_exits_one_when_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IDEAPRESS_INFERENCE__OLLAMA__BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("IDEAPRESS_INFERENCE__OLLAMA__TIMEOUT_SECONDS", "1")
    result = runner.invoke(cli_app, ["backend", "test"])
    assert result.exit_code == 1
    assert "ollama" in result.stdout


def test_cli_backend_switch_refuses_an_unknown_mode() -> None:
    result = runner.invoke(cli_app, ["backend", "switch", "anthropic"])
    assert result.exit_code == 2


def test_cli_backend_switch_refuses_a_configuration_that_would_not_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A remote backend with allow_remote off is refused here, not at the next startup."""
    result = runner.invoke(cli_app, ["backend", "switch", "openai_compatible"])
    assert result.exit_code == 2
    # A refusal goes to stderr, which Click keeps separate from stdout.
    message = result.stderr
    assert "Refusing to switch" in message
    assert "allow_remote" in message or "base_url" in message


def test_cli_backend_switch_writes_a_file_config_validate_accepts(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pathlib import Path

    target = Path(str(tmp_path)) / "switch.toml"
    monkeypatch.setenv("IDEAPRESS_CONFIG", str(target))
    assert runner.invoke(cli_app, ["config", "init"]).exit_code == 0
    assert runner.invoke(cli_app, ["backend", "switch", "ollama"]).exit_code == 0
    assert 'mode = "ollama"' in target.read_text(encoding="utf-8")
    assert runner.invoke(cli_app, ["config", "validate"]).exit_code == 0
