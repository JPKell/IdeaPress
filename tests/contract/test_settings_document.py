"""Api.md §6: `GET`/`PUT /settings` answer the suite's runtime-settings document (row WI1).

LoadCoach and PromptCadence answer ``{settings, definitions, config_only}`` and take a flat
``{key: value}`` body, and WeightRoomGym's Settings page sends and reads exactly that for every
application (ADR-0127 rule 4). IdeaPress conforms rather than teaching the console a second shape.
Its own refusal rules are unchanged: a configuration-only key is ``403`` naming it, an unknown key
is refused naming it, and a request that names either writes nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from ideapress.config import load_settings
from ideapress.infrastructure.db.models import Setting as SettingRow
from ideapress.services.runtime import build_runtime
from ideapress.web.app import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.contract

LOOPBACK = "http://127.0.0.1:8767"
DEFINITION_FIELDS = {
    "type",
    "description",
    "minimum",
    "maximum",
    "configured",
    "stored",
    "source",
    "shadowed_by",
}
"""LoadCoach's and PromptCadence's per-key fields — the ones the console may read."""


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app(load_settings().settings, runtime_builder=build_runtime)
    with TestClient(app, base_url=LOOPBACK) as test_client:
        yield test_client


def _rows(client: TestClient) -> list[tuple[str, Any]]:
    runtime = client.app.state.runtime  # type: ignore[attr-defined]  # the served runtime
    with runtime.storage.read() as session:
        return [(row.key, row.value_json) for row in session.scalars(select(SettingRow)).all()]


def test_get_answers_the_suites_document(client: TestClient) -> None:
    body = client.get("/api/v1/settings").json()

    assert set(body) == {"settings", "definitions", "config_only"}
    assert set(body["settings"]) == set(body["definitions"])
    assert "workflow.max_revision_rounds" in body["settings"]
    assert "models.stages.draft" in body["settings"]
    for key, definition in body["definitions"].items():
        assert DEFINITION_FIELDS <= set(definition), key
    assert "server.host" in body["config_only"]


def test_a_key_with_no_row_is_its_configured_value(client: TestClient) -> None:
    body = client.get("/api/v1/settings").json()

    definition = body["definitions"]["workflow.max_revision_rounds"]
    assert body["settings"]["workflow.max_revision_rounds"] == 3
    assert definition["configured"] == 3
    assert definition["stored"] is None
    assert definition["source"] == "configuration"
    assert definition["shadowed_by"] is None
    assert definition["type"] == "integer"
    assert (definition["minimum"], definition["maximum"]) == (0, 100)


def test_a_flat_put_is_stored_and_answers_the_document(client: TestClient) -> None:
    response = client.put("/api/v1/settings", json={"workflow.max_revision_rounds": 2})

    assert response.status_code == 200
    body = response.json()
    assert body["settings"]["workflow.max_revision_rounds"] == 2
    definition = body["definitions"]["workflow.max_revision_rounds"]
    assert (definition["configured"], definition["stored"]) == (3, 2)
    assert definition["source"] == "database"
    assert _rows(client) == [("workflow.max_revision_rounds", 2)]
    assert client.get("/api/v1/settings").json() == body, "both verbs answer one document"


def test_the_old_values_wrapper_is_refused_as_an_unknown_key(client: TestClient) -> None:
    response = client.put("/api/v1/settings", json={"values": {"workflow.max_revision_rounds": 2}})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "values" in response.json()["error"]["message"]
    assert _rows(client) == []


@pytest.mark.parametrize(
    "key",
    [
        "server.host",
        "server.port",
        "server.allowed_hosts",
        "server.allow_lan_exposure",
        "storage.database_url",
        "providers.allow_remote",
    ],
)
def test_a_configuration_only_key_is_403_naming_it(client: TestClient, key: str) -> None:
    """Api.md §6: the six keys that decide where the service listens and where content goes."""
    response = client.put("/api/v1/settings", json={key: "anything"})

    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "FORBIDDEN"
    assert key in error["message"]


@pytest.mark.parametrize(
    "body",
    [
        {"workflow.max_revision_rounds": 2, "server.host": "0.0.0.0"},  # noqa: S104 — the refusal
        {"workflow.max_revision_rounds": 2, "workflow.speed": 11},
        {"workflow.max_revision_rounds": 2, "workflow.max_attempts_per_stage": 0},
    ],
)
def test_a_request_naming_one_refused_key_writes_nothing(
    client: TestClient, body: dict[str, Any]
) -> None:
    """A caller who mistyped one key of six should not have the other five applied."""
    assert client.put("/api/v1/settings", json=body).status_code in {403, 400}
    assert _rows(client) == []


def test_an_unknown_key_is_refused_naming_it(client: TestClient) -> None:
    response = client.put("/api/v1/settings", json={"workflow.speed": 11})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "workflow.speed" in response.json()["error"]["message"]


@pytest.mark.parametrize("value", [-1, 101, "three"])
def test_a_value_the_field_refuses_is_refused_naming_the_key(
    client: TestClient, value: Any
) -> None:
    response = client.put("/api/v1/settings", json={"workflow.max_revision_rounds": value})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "workflow.max_revision_rounds" in response.json()["error"]["message"]
    assert _rows(client) == []


def test_a_stage_binding_is_runtime_changeable_only_for_a_real_stage(client: TestClient) -> None:
    ok = client.put("/api/v1/settings", json={"models.stages.draft": "ollama/other:7b"})
    assert ok.status_code == 200
    assert ok.json()["settings"]["models.stages.draft"] == "ollama/other:7b"

    bad = client.put("/api/v1/settings", json={"models.stages.audit": "x"})
    assert bad.status_code == 400
    assert "models.stages.audit" in bad.json()["error"]["message"]
