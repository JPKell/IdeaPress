"""End-to-end: the whole project journey, Phase 9 acceptance criterion 3.

Development plan, Phase 9: "Clean-machine install: `pip install ideapress` → create → plan →
draft → export, with only Ollama." `install-check` (CI) proves the install; every other e2e file
exercises one page or one stage against a project some fixture already brought partway there. This
file is the single, unbroken path across all of them — create a project, plan it, draft it to a
committed unit, and export — and it proves the export a person reads back actually names the
content this journey produced, not merely that each step returns 200 in isolation.
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
    """A runtime that plans one unit and drafts it cleanly — no real model needed for this path."""
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


def test_create_plan_draft_and_export_is_one_unbroken_path(client: TestClient) -> None:
    """Development plan Phase 9, acceptance criterion 3, end to end and in order."""
    # `serve` reaches a healthy state with only the model source configured (P1 AC1, G3).
    components = {c["name"]: c["status"] for c in client.get("/api/v1/health").json()["components"]}
    assert components["database"] == "ok"

    # Create.
    created = client.post("/api/v1/projects", json={"title": "Local inference", "brief": BRIEF})
    assert created.status_code == 201, created.text
    project_id = created.json()["id"]

    # Plan.
    plan_task = client.post(f"/api/v1/projects/{project_id}/plan").json()["task_id"]
    assert _wait(client, project_id, plan_task) == "completed"

    # Draft — which chains validate, repair-if-needed and commit internally (P4).
    draft_task = client.post(f"/api/v1/projects/{project_id}/stages/draft/run", json={}).json()[
        "task_id"
    ]
    assert _wait(client, project_id, draft_task) == "completed"

    # Export, and prove the document names the content this journey just drafted — an export
    # that cannot be traced back to what was written would satisfy the letter of "export" and
    # none of its purpose.
    read_back = client.get(f"/api/v1/projects/{project_id}/export?format=markdown")
    assert read_back.status_code == 200
    assert "own machine" in read_back.text

    # And the write path lands somewhere a person can then read back, exactly as the dialog
    # promises (spec §20 AC9: exports are deterministic and reproducible).
    dialog = client.get(f"/projects/{project_id}/export")
    token = dialog.headers["set-cookie"].split(f"{CSRF_COOKIE_NAME}=", 1)[1].split(";", 1)[0]
    written = client.post(
        f"/projects/{project_id}/export",
        data={CSRF_FIELD_NAME: token, "format": "markdown"},
        headers={"Cookie": f"{CSRF_COOKIE_NAME}={token}"},
        follow_redirects=False,
    )
    assert written.status_code == 303
    location = written.headers["location"]
    assert "written=" in location
    assert client.get(location).status_code == 200
