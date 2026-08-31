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
