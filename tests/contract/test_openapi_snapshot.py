"""The committed OpenAPI snapshot matches the application (ADR-0108; packaging standards §6)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from ideapress.config import load_settings
from ideapress.web.app import create_app

SNAPSHOT = Path(__file__).resolve().parents[2] / "docs" / "openapi.json"

pytestmark = pytest.mark.contract


def current_openapi() -> dict[str, Any]:
    """The schema this build serves, with the volatile version field pinned by the build."""
    document = create_app(load_settings().settings).openapi()
    return cast("dict[str, Any]", json.loads(json.dumps(document, sort_keys=True)))


def test_the_committed_snapshot_matches_the_application() -> None:
    assert SNAPSHOT.is_file(), "docs/openapi.json is missing; regenerate it"
    committed = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert committed == current_openapi(), (
        "docs/openapi.json drifted; regenerate with "
        "python -c 'from tests.contract.test_openapi_snapshot import write; write()'"
    )


def test_every_documented_endpoint_is_in_the_snapshot() -> None:
    paths = set(current_openapi()["paths"])
    for path in (
        "/api/v1/health",
        "/api/v1/version",
        "/api/v1/system/status",
        "/api/v1/settings",
        "/api/v1/backends",
        "/api/v1/backends/test",
        "/api/v1/workflows",
        "/api/v1/workflows/{workflow_id}",
        "/api/v1/projects",
        "/api/v1/projects/{project_id}",
        "/api/v1/projects/{project_id}/plan",
        "/api/v1/projects/{project_id}/export",
        "/api/v1/projects/{project_id}/units",
        "/api/v1/projects/{project_id}/units/{unit_key}",
        "/api/v1/projects/{project_id}/units/{unit_key}/history",
        "/api/v1/projects/{project_id}/units/{unit_key}/revise",
        "/api/v1/projects/{project_id}/stages/{stage}/run",
        "/api/v1/projects/{project_id}/tasks/{task_id}",
        "/api/v1/projects/{project_id}/tasks/{task_id}/cancel",
        "/api/v1/projects/{project_id}/tasks/{task_id}/stream",
        "/api/v1/export/formats",
    ):
        assert path in paths, path


def write() -> None:
    """Regenerate the snapshot."""
    SNAPSHOT.write_text(json.dumps(current_openapi(), indent=2, sort_keys=True) + "\n")
