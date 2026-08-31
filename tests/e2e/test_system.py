"""The skeleton over HTTP: health, version, the system page, and the security controls.

Everything here runs with no backend reachable and no network (spec §20 AC11), which is the point:
AC1 says the application starts with zero configuration and nothing running, and AC7 says an
unavailable backend is *never* a startup failure.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from ideapress.config import Settings, load_settings
from ideapress.services.runtime import build_runtime
from ideapress.web.app import create_app

LOOPBACK = "http://127.0.0.1:8767"


@pytest.fixture
def settings() -> Settings:
    return load_settings().settings


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings, runtime_builder=build_runtime)
    with TestClient(app, base_url=LOOPBACK) as test_client:
        yield test_client


def test_version_is_unauthenticated_and_names_all_three_versions(client: TestClient) -> None:
    body = client.get("/api/v1/version").json()
    assert body["application"] == "ideapress"
    assert body["api_version"] == "v1"
    assert body["schema_version"] == "1"


def test_health_reports_the_three_components_spec_17_names(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    names = {component["name"] for component in response.json()["components"]}
    assert names == {"database", "backend", "prompts"}


def test_health_names_which_backend_is_configured(client: TestClient) -> None:
    components = client.get("/api/v1/health").json()["components"]
    backend = next(c for c in components if c["name"] == "backend")
    assert "ollama" in backend["detail"]


def test_starting_with_no_backend_is_not_a_failure(client: TestClient) -> None:
    """Spec §20 AC7: never a startup failure. The app answers; health says what is missing."""
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/api/v1/system/status").status_code == 200


def test_host_validation_runs_before_routing_on_every_request(client: TestClient) -> None:
    """ADR-0026 §1. A path that does not exist is still refused at 421, not 404 — proof that the
    check happens before a router is consulted."""
    for path in ("/api/v1/version", "/api/v1/health", "/", "/no-such-path"):
        response = client.get(path, headers={"host": "attacker.example.com"})
        assert response.status_code == 421, path
        assert response.json()["error"]["code"] == "MISDIRECTED_REQUEST"


def test_loopback_names_are_all_accepted(settings: Settings) -> None:
    app = create_app(settings, runtime_builder=build_runtime)
    for host in ("127.0.0.1:8767", "localhost:8767"):
        with TestClient(app, base_url=f"http://{host}") as client:
            assert client.get("/api/v1/version").status_code == 200


def test_every_response_carries_a_request_id(client: TestClient) -> None:
    response = client.get("/api/v1/version")
    assert response.headers["X-Request-ID"]


def test_error_bodies_use_the_shared_envelope(client: TestClient) -> None:
    body = client.get("/api/v1/no-such-endpoint").json()
    assert set(body) == {"error"}
    assert set(body["error"]) >= {"code", "message", "request_id", "timestamp"}


def test_a_page_request_gets_an_html_error_not_json(client: TestClient) -> None:
    response = client.get("/no-such-page", headers={"accept": "text/html"})
    assert response.status_code == 404
    assert "text/html" in response.headers["content-type"]
    assert "NOT_FOUND" in response.text


def test_oversize_body_is_refused_before_it_is_buffered(settings: Settings) -> None:
    settings.server.max_body_bytes = 512
    app = create_app(settings, runtime_builder=build_runtime)
    with TestClient(app, base_url=LOOPBACK) as client:
        response = client.post("/api/v1/system/status", content=b"x" * 1024)
        assert response.status_code == 413


def test_cross_origin_write_is_refused(client: TestClient) -> None:
    response = client.post(
        "/api/v1/system/status", headers={"origin": "http://evil.example.com"}, json={}
    )
    assert response.status_code == 403


def test_system_page_renders_without_a_backend(client: TestClient) -> None:
    response = client.get("/system", headers={"accept": "text/html"})
    assert response.status_code == 200
    assert "IdeaPress" in response.text
    assert "Health" in response.text


def test_static_assets_are_served_from_the_package_not_a_cdn(client: TestClient) -> None:
    """UI standards: no external request at page load.

    Checked on the attributes that *fetch* — `src` and `href` — rather than on the page text. A
    URL appearing as prose is not a request: the backend's own endpoint is displayed on this very
    page, and a test that forbade the characters would forbid showing the user where their content
    goes.
    """
    import re

    page = client.get("/system", headers={"accept": "text/html"}).text
    fetched = re.findall(r'(?:src|href)="([^"]+)"', page)
    assert fetched, "the page references no assets at all, so this proves nothing"
    external = [url for url in fetched if url.startswith(("http://", "https://", "//"))]
    assert external == [], f"external resource(s) referenced: {external}"


def test_docs_are_loopback_only(settings: Settings) -> None:
    settings.server.host = "0.0.0.0"  # noqa: S104 — asserting the docs are withheld off loopback
    app = create_app(settings, runtime_builder=build_runtime)
    assert app.openapi_url is None


def test_health_serialises_when_the_backend_is_unreachable(settings: Settings) -> None:
    """Regression: an UNSUPPORTED measurement reaching a JSON body made `/health` a 500.

    Found by running the suite under `unshare -rn`, and only there. ModelRack reports
    `model_count` and `latency_ms` as `Measurement`, which is the UNSUPPORTED sentinel when the
    provider did not answer; the sentinel raises rather than coercing (ADR-0016), so it must be
    sanitised at the adapter boundary. An unreachable backend is the exact state spec §20 AC7
    requires to keep working, so this was a 500 in the supported case.
    """
    settings.inference.ollama.base_url = "http://127.0.0.1:1"  # nothing listens here
    settings.inference.ollama.timeout_seconds = 1
    app = create_app(settings, runtime_builder=build_runtime)
    with TestClient(app, base_url=LOOPBACK) as client:
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        components = {c["name"]: c for c in response.json()["components"]}
        backend = components["backend"]
        assert backend["status"] == "degraded", "unreachable is degraded, never an outage"
        assert backend["data"]["reachable"] is False
        assert client.get("/api/v1/backends").status_code == 200
        assert client.get("/backends", headers={"accept": "text/html"}).status_code == 200
