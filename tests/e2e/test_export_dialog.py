"""The export dialog: format, scope and content-inclusion choices stated plainly (P8).

The plan's wording is "stated plainly", and the reason is that choosing an export format is
choosing what leaves this application and in what shape. Three unexplained buttons would satisfy
the letter of "an export dialog" and none of the intent, so what is asserted here is that the page
says what each format contains and what it will leave out.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from mirrorwall import CSRF_COOKIE_NAME, CSRF_FIELD_NAME

from ideapress.config import Settings, load_settings
from ideapress.web.app import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ideapress.services.runtime import Runtime

LOOPBACK = "http://127.0.0.1:8767"

BRIEF = "The article must state that inference runs entirely on the reader's own machine."
REQUIREMENTS = {
    "requirements": [
        {
            "text": "The unit must be explicit about where inference happens.",
            "blocking": True,
            "source_document": "brief",
            "source_quote": "inference runs entirely on the reader's own machine",
            "checks": [{"kind": "must_contain_any", "values": ["own machine"]}],
        }
    ]
}
PLAN = {
    "units": [
        {
            "title": "Where the work happens",
            "goal_text": "Say plainly where inference runs.",
            "requirement_keys": ["R-001"],
            "target_words": 40,
        }
    ]
}
DRAFT = (
    "Everything happens on your own machine. Nothing you write is uploaded anywhere at all, and "
    "no account is needed for any of it."
)


def _scripted_runtime(settings: Settings) -> Runtime:
    from modelrack.testing import FakeGeneration, FakeScript

    from ideapress.infrastructure.backends.fake import FakeBackend, default_fake_script
    from ideapress.services.inference import InferenceGateway
    from ideapress.services.runtime import build_runtime
    from ideapress.services.stages import StageRunner

    runtime = build_runtime(settings)
    script = FakeScript(
        models=default_fake_script().models,
        capabilities=default_fake_script().capabilities,
        generations=(
            FakeGeneration(text=json.dumps(REQUIREMENTS)),
            FakeGeneration(text=json.dumps(PLAN)),
            FakeGeneration(text=DRAFT),
            FakeGeneration(text=json.dumps({"findings": [], "requirements_assessment": []})),
            FakeGeneration(text=json.dumps({"verdict": "acceptable", "rationale": "ok"})),
        ),
        repeat_final_generation=True,
    )
    backend = FakeBackend(script=script, seed=5)
    gateway = InferenceGateway(
        backend=backend, bindings=settings.models.stages, execution=settings.execution
    )
    runtime._gateway = gateway  # noqa: SLF001 — substituting the backend is the point
    runtime._backend = backend  # noqa: SLF001
    runtime._runner = StageRunner(runtime.storage, gateway=gateway, sink=runtime.events)  # noqa: SLF001
    return runtime


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app(load_settings().settings, runtime_builder=_scripted_runtime)
    with TestClient(app, base_url=LOOPBACK) as test_client:
        yield test_client


def _wait(client: TestClient, project_id: str, task_id: str) -> str:
    for _ in range(600):
        state = client.get(f"/api/v1/projects/{project_id}/tasks/{task_id}").json()["state"]
        if state in {"completed", "failed", "cancelled", "interrupted"}:
            return str(state)
        time.sleep(0.02)
    message = "the stage never finished"
    raise AssertionError(message)


@pytest.fixture
def committed(client: TestClient) -> str:
    project_id = client.post(
        "/api/v1/projects", json={"title": "Local inference", "brief": BRIEF}
    ).json()["id"]
    _wait(client, project_id, client.post(f"/api/v1/projects/{project_id}/plan").json()["task_id"])
    _wait(
        client,
        project_id,
        client.post(f"/api/v1/projects/{project_id}/stages/draft/run", json={}).json()["task_id"],
    )
    return str(project_id)


def _token(client: TestClient, project_id: str) -> str:
    page = client.get(f"/projects/{project_id}/export")
    return str(page.headers["set-cookie"].split(f"{CSRF_COOKIE_NAME}=", 1)[1].split(";", 1)[0])


# ------------------------------------------------------------------ format


def test_every_shipped_format_is_offered_with_its_extension(
    client: TestClient, committed: str
) -> None:
    page = client.get(f"/projects/{committed}/export").text
    for fmt, extension in (("markdown", ".md"), ("html", ".html"), ("json", ".json")):
        assert fmt in page, fmt
        assert extension in page, extension


def test_each_format_says_what_it_contains(client: TestClient, committed: str) -> None:
    """The difference that actually matters: Markdown drops provenance, JSON keeps everything."""
    page = client.get(f"/projects/{committed}/export").text
    assert "No provenance, no coverage" in page
    assert "self-contained" in page
    assert "provenance of every committed version" in page


def test_the_html_format_promises_no_network(client: TestClient, committed: str) -> None:
    """P6's property, restated where a person chooses it: the file opens from a USB stick."""
    page = client.get(f"/projects/{committed}/export").text
    assert "opens with no network at all" in page


# ------------------------------------------------------------------ scope


def test_the_dialog_says_how_many_units_will_be_included(
    client: TestClient, committed: str
) -> None:
    page = client.get(f"/projects/{committed}/export").text
    assert "1 of 1 unit(s) are committed" in page
    assert "Only committed units are exported" in page


def test_an_empty_project_says_an_export_would_be_empty(client: TestClient) -> None:
    """UI/UX Standards §13: an empty state, and one that explains rather than showing zero."""
    project_id = client.post("/api/v1/projects", json={"title": "Nothing", "brief": BRIEF}).json()[
        "id"
    ]
    page = client.get(f"/projects/{project_id}/export").text
    assert "an export would be empty" in page
    assert "disabled" in page, "the write button is offered for a project with nothing in it"


# ------------------------------------------------------------------ writing


def test_writing_an_export_reports_where_it_landed(client: TestClient, committed: str) -> None:
    token = _token(client, committed)
    response = client.post(
        f"/projects/{committed}/export",
        data={CSRF_FIELD_NAME: token, "format": "markdown"},
        headers={"Cookie": f"{CSRF_COOKIE_NAME}={token}"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert "written=" in location
    assert client.get(location).status_code == 200


def test_an_export_write_without_a_csrf_token_is_refused(
    client: TestClient, committed: str
) -> None:
    response = client.post(
        f"/projects/{committed}/export",
        data={"format": "markdown"},
        follow_redirects=False,
    )
    assert response.status_code in {400, 403}


def test_the_dialog_offers_reading_a_format_without_writing_anything(
    client: TestClient, committed: str
) -> None:
    """Reading is a GET, writing a POST — a distinction a person should not have to guess."""
    page = client.get(f"/projects/{committed}/export").text
    assert f"/api/v1/projects/{committed}/export?format=markdown" in page
    body = client.get(f"/api/v1/projects/{committed}/export?format=markdown")
    assert body.status_code == 200
    assert "own machine" in body.text


def test_the_dialog_states_that_exports_are_deterministic(
    client: TestClient, committed: str
) -> None:
    """Spec §20 AC9, said where it is useful: two exports can be compared with sha256sum."""
    page = client.get(f"/projects/{committed}/export").text
    assert "byte-identically" in page
    assert "sha256sum" in page
