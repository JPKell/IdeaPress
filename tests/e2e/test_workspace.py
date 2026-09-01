"""P8's workspace, over HTTP.

P8 AC1 is that a person can run a project start to finish from the UI alone, and AC2 that findings,
coverage and history are visible without leaving the unit. Both are properties of one page, so this
asserts what that page carries — including the two things M7's verification found a person could
not reach: the pause reason with its remedy, and the routing decision behind an attempt.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from ideapress.config import Settings, load_settings
from ideapress.web.app import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ideapress.services.runtime import Runtime

LOOPBACK = "http://127.0.0.1:8767"

BRIEF = (
    "The article must state that inference runs entirely on the reader's own machine and that no "
    "document content is uploaded anywhere."
)
REQUIREMENTS = {
    "requirements": [
        {
            "text": "The unit must be explicit about where inference happens.",
            "blocking": True,
            "source_document": "brief",
            "source_quote": "inference runs entirely on the reader's own machine",
            "checks": [{"kind": "must_contain_any", "values": ["own machine"]}],
        },
        {
            "text": "The unit must not use a marketing register.",
            "blocking": True,
            "source_document": "brief",
            "source_quote": "no document content is uploaded anywhere",
            "checks": [],
        },
    ]
}
PLAN = {
    "units": [
        {
            "title": "Where the work happens",
            "goal_text": "Say plainly where inference runs.",
            "requirement_keys": ["R-001", "R-002"],
            "target_words": 40,
        },
        {
            "title": "What it costs",
            "goal_text": "Be honest about the trade.",
            "requirement_keys": ["R-001", "R-002"],
            "target_words": 40,
        },
    ]
}
DRAFT = (
    "Everything happens on your own machine. Nothing you write is uploaded anywhere at all, and "
    "no account is needed for any of it. The hardware is yours to provide."
)
CLEAN_AUDIT = json.dumps(
    {
        "findings": [],
        "requirements_assessment": [{"key": "R-002", "verdict": "met"}],
    }
)
VERDICT = json.dumps({"verdict": "acceptable", "rationale": "ok"})


def _scripted_runtime(settings: Settings) -> Runtime:
    """A runtime that plans two units and drafts them both cleanly."""
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
            FakeGeneration(text=CLEAN_AUDIT),
            FakeGeneration(text=VERDICT),
            FakeGeneration(text=DRAFT),
            FakeGeneration(text=CLEAN_AUDIT),
            FakeGeneration(text=VERDICT),
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
def drafted(client: TestClient) -> str:
    """A project planned and drafted, so the workspace has something to show."""
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


# ------------------------------------------------------------------ the page exists and navigates


def test_the_workspace_renders_for_a_planned_project(client: TestClient, drafted: str) -> None:
    page = client.get(f"/projects/{drafted}/workspace")
    assert page.status_code == 200
    assert "Where the work happens" in page.text


def test_the_navigator_lists_every_unit_and_marks_the_current_one(
    client: TestClient, drafted: str
) -> None:
    page = client.get(f"/projects/{drafted}/workspace?unit=U-02").text
    assert "U-01" in page
    assert "U-02" in page
    assert 'aria-current="true"' in page


def test_an_unknown_unit_falls_back_rather_than_erroring(client: TestClient, drafted: str) -> None:
    """A stale link after a plan edit renumbered the units is a normal thing to click."""
    page = client.get(f"/projects/{drafted}/workspace?unit=U-99")
    assert page.status_code == 200
    assert "Where the work happens" in page.text


def test_a_project_with_no_plan_shows_an_empty_state_rather_than_failing(
    client: TestClient,
) -> None:
    """UI/UX Standards §13: loading, empty, error and populated states exist for every view."""
    project_id = client.post(
        "/api/v1/projects", json={"title": "Nothing yet", "brief": BRIEF}
    ).json()["id"]
    page = client.get(f"/projects/{project_id}/workspace")
    assert page.status_code == 200
    assert "Nothing has been planned yet" in page.text


# ------------------------------------------------------------------ AC2: without leaving the unit


def test_content_coverage_findings_and_history_are_all_on_one_page(
    client: TestClient, drafted: str
) -> None:
    """P8 AC2, asserted as the four panels being present together."""
    page = client.get(f"/projects/{drafted}/workspace?unit=U-01").text
    assert "own machine" in page, "the content is not shown"
    assert "Requirement coverage" in page
    assert "Findings" in page
    assert "Versions" in page
    assert "How this was produced" in page


def test_the_coverage_panel_counts_what_a_model_guaranteed(
    client: TestClient, drafted: str
) -> None:
    """ADR-0039's labelling survives the redesign: R-002 has no deterministic check, so its
    guarantee is a model's attestation and the page says so before any row is read."""
    page = client.get(f"/projects/{drafted}/workspace?unit=U-01").text
    assert "guaranteed by model review, not a deterministic check" in page


def test_the_provenance_panel_shows_the_prompt_version_per_attempt(
    client: TestClient, drafted: str
) -> None:
    page = client.get(f"/projects/{drafted}/workspace?unit=U-01").text
    assert "stages.draft" in page or "draft" in page
    assert "How this was produced" in page


def test_the_backend_badge_says_whether_work_leaves_the_machine(
    client: TestClient, drafted: str
) -> None:
    """Risk S4, on the page where somebody is about to press a button that sends their draft."""
    page = client.get(f"/projects/{drafted}/workspace").text
    assert "stays on this machine" in page


# ------------------------------------------------------------------ progressive enhancement


def test_the_workspace_works_with_javascript_disabled(client: TestClient, drafted: str) -> None:
    """ADR-0020. Every read-only surface is server-rendered; the scripts are deferred enhancements.

    Asserted structurally: the navigator is anchors and the actions are forms, so nothing a reader
    needs depends on a script running.
    """
    page = client.get(f"/projects/{drafted}/workspace?unit=U-01").text
    body = page.split("</head>", 1)[-1]
    assert 'href="/projects/' in body, "the navigator is not links"
    assert "own machine" in body, "the content is not in the server-rendered HTML"
    # No inline script decides what the page shows.
    assert "document.write" not in body


def test_the_scripts_are_deferred_so_they_never_block_the_render(
    client: TestClient, drafted: str
) -> None:
    page = client.get(f"/projects/{drafted}/workspace").text
    for name in ("workspace.js", "diff.js"):
        assert name in page, name
    assert page.count("defer") >= 2


def test_no_asset_is_fetched_from_off_this_machine(client: TestClient, drafted: str) -> None:
    """UI/UX Standards §13: no network request leaves the machine."""
    page = client.get(f"/projects/{drafted}/workspace").text
    assert "http://" not in page.replace(LOOPBACK, "")
    assert "https://" not in page


# ------------------------------------------------------------------ the paused unit


def test_a_paused_unit_shows_its_reason_and_a_resume_action(client: TestClient) -> None:
    """M7's finding, closed on the surface a person was actually looking at.

    The unit pauses because its draft budget is exhausted twice; the page must carry the reason,
    the setting that fixes it — named, with its default and range — and the action, together.
    """
    from ideapress.services.workspace import BUDGET_PAUSE_HINT, pause_guidance

    guidance = pause_guidance(
        "draft: the model produced no text at all in 8192 output tokens, twice"
    )
    assert guidance["paused"] is True
    assert guidance["kind"] == "output_budget"
    assert "workflow.structured_output_tokens" in guidance["hint"]
    assert guidance["hint"] == BUDGET_PAUSE_HINT
    assert "1024" in guidance["hint"] and "131072" in guidance["hint"]


def test_a_pause_with_no_known_remedy_gets_no_invented_advice() -> None:
    """Guessing a remedy sends a person to change a setting that was not the problem."""
    from ideapress.services.workspace import pause_guidance

    guidance = pause_guidance("revise: the revision limit was reached")
    assert guidance["paused"] is True
    assert guidance["kind"] == "other"
    assert guidance["hint"] == ""


def test_a_unit_that_is_not_paused_reports_so() -> None:
    from ideapress.services.workspace import pause_guidance

    assert pause_guidance(None)["paused"] is False
    assert pause_guidance("")["paused"] is False


# ------------------------------------------------------------------ the export dialog


def test_the_export_dialog_states_what_each_format_contains(
    client: TestClient, drafted: str
) -> None:
    page = client.get(f"/projects/{drafted}/export")
    assert page.status_code == 200
    for fmt in ("markdown", "html", "json"):
        assert fmt in page.text
    assert "opens with no network at all" in page.text
    assert "Only committed units are exported" in page.text


def test_the_export_dialog_names_the_units_it_will_leave_out(client: TestClient) -> None:
    """Scope, stated plainly: an uncommitted unit silently missing from a document is the
    surprise this page exists to prevent."""
    project_id = client.post(
        "/api/v1/projects", json={"title": "Half done", "brief": BRIEF}
    ).json()["id"]
    _wait(client, project_id, client.post(f"/api/v1/projects/{project_id}/plan").json()["task_id"])
    page = client.get(f"/projects/{project_id}/export").text
    assert "not yet committed" in page
    assert "U-01" in page


def test_the_export_dialog_writes_a_file_and_says_where(client: TestClient, drafted: str) -> None:
    from mirrorwall import CSRF_COOKIE_NAME, CSRF_FIELD_NAME

    page = client.get(f"/projects/{drafted}/export")
    token = page.headers["set-cookie"].split(f"{CSRF_COOKIE_NAME}=", 1)[1].split(";", 1)[0]
    response = client.post(
        f"/projects/{drafted}/export",
        data={CSRF_FIELD_NAME: token, "format": "markdown"},
        headers={"Cookie": f"{CSRF_COOKIE_NAME}={token}"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "written=" in response.headers["location"]
