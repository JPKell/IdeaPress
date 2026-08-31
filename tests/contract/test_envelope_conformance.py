"""Contract: the shapes other components and MirrorWall agree on.

Spec §18 lists SetSpec envelopes under Contract. These are the cross-component surfaces that exist
at P1: the error envelope (API standards §4, ADR-0025 §4 — transported unwrapped), the health
payload MirrorWall builds, and `/version`. Backend-port conformance joins them in P2.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from ideapress.config import load_settings
from ideapress.errors import ERROR_CODES, SHARED_ERROR_CODES
from ideapress.services.runtime import build_runtime
from ideapress.web.app import create_app
from ideapress.web.routes.system import API_VERSION, SCHEMA_VERSION

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.contract

LOOPBACK = "http://127.0.0.1:8767"
_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")
_SCREAMING_SNAKE = re.compile(r"^[A-Z][A-Z0-9_]*$")


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app(load_settings().settings, runtime_builder=build_runtime)
    with TestClient(app, base_url=LOOPBACK) as test_client:
        yield test_client


def test_the_error_envelope_has_exactly_the_documented_fields(client: TestClient) -> None:
    body = client.get("/api/v1/projects/01ZZZZZZZZZZZZZZZZZZZZZZZZ").json()
    assert set(body) == {"error"}, "an error is never further wrapped (ADR-0025 §4)"
    error = body["error"]
    assert set(error) == {"code", "message", "details", "request_id", "timestamp"}
    assert _SCREAMING_SNAKE.match(error["code"])
    assert isinstance(error["details"], dict)
    assert _RFC3339.match(error["timestamp"]), error["timestamp"]


def test_the_request_id_in_the_body_matches_the_header(client: TestClient) -> None:
    response = client.get("/api/v1/projects/01ZZZZZZZZZZZZZZZZZZZZZZZZ")
    assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]


@pytest.mark.parametrize(
    ("path", "expected_code", "expected_status"),
    [
        ("/api/v1/projects/01ZZZZZZZZZZZZZZZZZZZZZZZZ", "PROJECT_NOT_FOUND", 404),
        ("/api/v1/no-such-endpoint", "NOT_FOUND", 404),
    ],
)
def test_documented_codes_come_back_with_their_documented_status(
    client: TestClient, path: str, expected_code: str, expected_status: int
) -> None:
    response = client.get(path)
    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == expected_code


def test_every_error_class_declares_a_screaming_snake_code() -> None:
    for code in ERROR_CODES | SHARED_ERROR_CODES:
        assert _SCREAMING_SNAKE.match(code), code


def test_the_error_vocabulary_is_exactly_the_specs_fifteen() -> None:
    """Spec §13 lists fifteen codes; a route raising a sixteenth is a contract change."""
    from pathlib import Path

    spec = (
        Path(__file__).resolve().parents[2] / "docs" / "apps" / "ideapress" / "spec.md"
    ).read_text(encoding="utf-8")
    block = spec.split("## 13. Error behaviour", 1)[1].split("```", 2)[1]
    documented = set(re.findall(r"\b[A-Z][A-Z0-9_]{4,}\b", block))
    assert documented == ERROR_CODES


def test_every_status_mapping_names_a_code_that_exists() -> None:
    from ideapress.web.app import _STATUS_BY_CODE

    unknown = (
        set(_STATUS_BY_CODE)
        - ERROR_CODES
        - SHARED_ERROR_CODES
        - {
            "NOT_FOUND",
            "METHOD_NOT_ALLOWED",
            "PAYLOAD_TOO_LARGE",
            "UNSUPPORTED_MEDIA_TYPE",
            "MISDIRECTED_REQUEST",
            "CSRF_FAILED",
            "CONFLICT",
            "DATABASE_ERROR",
            "DATABASE_UNAVAILABLE",
            "MIGRATION_REQUIRED",
            "MIGRATION_FAILED",
            "SCHEMA_AHEAD",
            "STORAGE_BUSY",
            "STORAGE_FULL",
            "INSECURE_BINDING",
        }
    )
    assert unknown == set()


def test_every_ideapress_error_code_has_an_http_status(client: TestClient) -> None:
    from ideapress.web.app import _STATUS_BY_CODE

    assert ERROR_CODES <= set(_STATUS_BY_CODE)


def test_the_health_payload_matches_mirrorwalls_shape(client: TestClient) -> None:
    body = client.get("/api/v1/health").json()
    assert set(body) >= {"status", "application", "version", "checked_at", "components"}
    assert body["application"] == "ideapress"
    for component in body["components"]:
        assert set(component) >= {"name", "status"}
        assert component["status"] in {"ok", "degraded", "unavailable", "not_configured"}


def test_version_reports_application_api_and_schema(client: TestClient) -> None:
    body = client.get("/api/v1/version").json()
    assert body == {
        "application": "ideapress",
        "version": body["version"],
        "api_version": API_VERSION,
        "schema_version": SCHEMA_VERSION,
    }


def test_the_paginated_list_shape_is_the_shared_one(client: TestClient) -> None:
    body = client.get("/api/v1/projects").json()
    assert set(body) == {"items", "page"}
    assert set(body["page"]) >= {"limit", "has_more"}


def test_the_openapi_schema_builds(client: TestClient) -> None:
    """Regression: a `TYPE_CHECKING`-only response annotation made `app.openapi()` raise.

    FastAPI reads a handler's return annotation at **runtime** to build the schema. Under
    `from __future__ import annotations` a `TYPE_CHECKING`-only import leaves a forward reference
    it cannot resolve, so `/api/v1/docs` and `/api/v1/openapi.json` were 500s while every test that
    never asked for them stayed green. Found by the closeout consistency review, not by the suite.
    """
    response = client.get("/api/v1/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert schema["info"]["title"] == "IdeaPress"
    assert len(schema["paths"]) >= 20
    assert client.get("/api/v1/docs").status_code == 200


def test_every_endpoint_the_specification_lists_exists(client: TestClient) -> None:
    """Roadmap §8's consistency review, as a test rather than a one-off reading.

    Spec §7.1 writes `{id}` and `{unit_id}` where the code writes `{project_id}` and `{unit_key}`;
    the comparison normalises those two and nothing else.
    """
    documented = {
        "DELETE /projects/{id}",
        "GET /backends",
        "GET /health",
        "GET /projects",
        "GET /projects/{id}",
        "GET /projects/{id}/export",
        "GET /projects/{id}/tasks/{task_id}",
        "GET /projects/{id}/tasks/{task_id}/stream",
        "GET /projects/{id}/units",
        "GET /projects/{id}/units/{unit_id}",
        "GET /projects/{id}/units/{unit_id}/history",
        "GET /settings",
        "GET /system/status",
        "GET /version",
        "GET /workflows",
        "GET /workflows/{id}",
        "POST /backends/test",
        "POST /projects",
        "POST /projects/{id}/export",
        "POST /projects/{id}/plan",
        "POST /projects/{id}/stages/{stage}/run",
        "POST /projects/{id}/tasks/{task_id}/cancel",
        "POST /projects/{id}/units/{unit_id}/revise",
        "PUT /projects/{id}",
        "PUT /settings",
    }
    schema = client.get("/api/v1/openapi.json").json()
    actual = {
        f"{method.upper()} {path.replace('/api/v1', '')}".replace("{project_id}", "{id}")
        .replace("{unit_key}", "{unit_id}")
        .replace("{workflow_id}", "{id}")
        for path, operations in schema["paths"].items()
        for method in operations
        if method.upper() in {"GET", "POST", "PUT", "DELETE"}
    }
    assert documented - actual == set(), f"specified but missing: {sorted(documented - actual)}"


def test_settings_refuses_a_configuration_only_key_by_name(client: TestClient) -> None:
    """Api.md §6: the six keys that decide where the service listens and where content goes."""
    for key in (
        "server.host",
        "server.allowed_hosts",
        "server.allow_lan_exposure",
        "storage.database_url",
        "providers.allow_remote",
    ):
        response = client.put("/api/v1/settings", json={"values": {key: "anything"}})
        assert response.status_code == 422, key
        assert key in response.json()["error"]["message"]


def test_settings_accepts_a_runtime_key(client: TestClient) -> None:
    response = client.put("/api/v1/settings", json={"values": {"workflow.max_revision_rounds": 2}})
    assert response.status_code == 200
    assert response.json()["updated"] == ["workflow.max_revision_rounds"]


def test_settings_refuses_the_whole_update_when_one_key_is_refused(client: TestClient) -> None:
    """A caller who mistyped one key of six should not have the other five applied."""
    from sqlalchemy import select

    from ideapress.infrastructure.db.models import Setting as SettingRow

    response = client.put(
        "/api/v1/settings",
        json={
            "values": {
                "workflow.max_revision_rounds": 2,
                "server.host": "0.0.0.0",  # noqa: S104 — the value under test is the refusal
            }
        },
    )
    assert response.status_code == 422
    runtime = client.app.state.runtime  # type: ignore[attr-defined]  # the served runtime
    with runtime.storage.read() as session:
        assert session.scalars(select(SettingRow)).all() == []


def test_settings_refuses_a_key_that_is_not_a_setting(client: TestClient) -> None:
    response = client.put("/api/v1/settings", json={"values": {"workflow.speed": 11}})
    assert response.status_code == 422
    assert "workflow.speed" in response.json()["error"]["message"]


def test_settings_reports_which_keys_are_config_only(client: TestClient) -> None:
    body = client.get("/api/v1/settings").json()
    assert "server.host" in body["config_only"]
    assert "inference.mode" in body["runtime_changeable"]
    assert "models.stages.draft" in body["runtime_changeable"]


def test_a_stage_binding_is_runtime_changeable_but_only_for_a_real_stage(
    client: TestClient,
) -> None:
    ok = client.put("/api/v1/settings", json={"values": {"models.stages.draft": "ollama/other:7b"}})
    assert ok.status_code == 200
    bad = client.put("/api/v1/settings", json={"values": {"models.stages.audit": "x"}})
    assert bad.status_code == 422
    assert "models.stages.audit" in bad.json()["error"]["message"]


def test_one_workflow_is_shipped_and_an_unknown_one_is_refused(client: TestClient) -> None:
    body = client.get("/api/v1/workflows/standard").json()
    assert body["id"] == "standard"
    assert len(body["stages"]) == 16
    missing = client.get("/api/v1/workflows/novel")
    assert missing.status_code == 409
    assert "novel" in missing.json()["error"]["message"]
