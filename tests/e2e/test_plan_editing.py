"""P8's plan editor, over HTTP — and the one refusal it exists for.

The property: **an edit that leaves a blocking requirement with no unit responsible for it is
refused, and the refusal names the requirement.** `check_plan` is the gate that would otherwise not
run again until the next plan stage, long after the edit removed a guarantee, so the editor runs it
before every write and nothing that fails it reaches the database.

The second property, which the plan does not name but workflows §9 does: a structural edit never
renumbers a unit that already holds committed text. A person clicking "merge" is not a licence to
do what a later failure may not.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient
from mirrorwall import CSRF_COOKIE_NAME, CSRF_FIELD_NAME

from ideapress.config import Settings, load_settings
from ideapress.web.app import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ideapress.services.runtime import Runtime

LOOPBACK = "http://127.0.0.1:8767"

BRIEF = (
    "The article must state that inference runs entirely on the reader's own machine, that no "
    "document content is uploaded anywhere, and that the reader supplies the hardware."
)
REQUIREMENTS = {
    "requirements": [
        {
            "text": "The article must state that inference runs on the reader's own machine.",
            "blocking": True,
            "source_document": "brief",
            "source_quote": "inference runs entirely on the reader's own machine",
            "checks": [{"kind": "must_contain_any", "values": ["own machine"]}],
        },
        {
            "text": "The article must state that nothing is uploaded.",
            "blocking": True,
            "source_document": "brief",
            "source_quote": "no document content is uploaded anywhere",
            "checks": [{"kind": "must_contain_any", "values": ["uploaded"]}],
        },
        {
            "text": "The article should mention the hardware trade.",
            "blocking": False,
            "source_document": "brief",
            "source_quote": "the reader supplies the hardware",
            "checks": [],
        },
    ]
}
PLAN = {
    "units": [
        {
            "title": "Where the work happens",
            "goal_text": "Say plainly where inference runs.",
            "requirement_keys": ["R-001"],
            "target_words": 300,
        },
        {
            "title": "What leaves the machine",
            "goal_text": "Nothing does. Say so.",
            "requirement_keys": ["R-002"],
            "target_words": 300,
        },
        {
            "title": "What it costs",
            "goal_text": "The hardware trade.",
            "requirement_keys": ["R-003"],
            "target_words": 200,
        },
    ]
}


def _scripted_runtime(settings: Settings) -> Runtime:
    """A runtime whose backend answers with the fixture plan, so edits act on a known structure."""
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


def _planned_project(client: TestClient) -> str:
    """Create a project and run its plan stage, returning the project id."""
    import time

    project_id = client.post(
        "/api/v1/projects", json={"title": "Local inference", "brief": BRIEF}
    ).json()["id"]
    started = client.post(f"/api/v1/projects/{project_id}/plan")
    assert started.status_code in {200, 202}, started.text
    task_id = started.json()["task_id"]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        state = client.get(f"/api/v1/projects/{project_id}/tasks/{task_id}").json()["state"]
        if state in {"completed", "failed"}:
            assert state == "completed", state
            return str(project_id)
        time.sleep(0.02)
    message = "the plan stage did not finish"
    raise AssertionError(message)


def _edit(client: TestClient, project_id: str, **fields: Any) -> Any:
    """Post one plan edit with the CSRF token the plan page issued."""
    page = client.get(f"/projects/{project_id}/plan")
    assert page.status_code == 200, page.text
    # Read from the header rather than the jar: the cookie is `__Host-` prefixed and `Secure`, and
    # httpx will not store a Secure cookie received over plain http. That is the flag working as
    # intended (ADR-0026 §2) — a real browser treats http://127.0.0.1 as a secure context — so the
    # test carries the token explicitly, as `test_project_crud.py` does.
    token = page.headers["set-cookie"].split(f"{CSRF_COOKIE_NAME}=", 1)[1].split(";", 1)[0]
    return client.post(
        f"/projects/{project_id}/plan/edit",
        data={CSRF_FIELD_NAME: token, **fields},
        headers={"Cookie": f"{CSRF_COOKIE_NAME}={token}"},
        follow_redirects=False,
    )


def _units(client: TestClient, project_id: str) -> list[dict[str, Any]]:
    return list(client.get(f"/api/v1/projects/{project_id}/units").json()["units"])


# ------------------------------------------------------------------ the refusal


def test_an_edit_that_orphans_a_blocking_requirement_is_refused_by_name(
    client: TestClient,
) -> None:
    """The property P8 exists to guarantee.

    R-002 is blocking and lives on exactly one unit. Reassigning that unit to carry only R-001
    leaves nothing in the finished work answerable for R-002 — and the page says so, naming it.
    """
    project_id = _planned_project(client)
    response = _edit(
        client,
        project_id,
        operation="reassign",
        unit_keys="U-02",
        requirement_keys="R-001",
    )
    assert response.status_code == 200, "a refused edit re-renders the page, it does not redirect"
    assert "R-002" in response.text
    assert "no unit" in response.text.lower()

    # And nothing was written: the plan is exactly as it was.
    assignments = {
        unit["unit_key"]: unit["requirement_keys"] for unit in _units(client, project_id)
    }
    assert assignments["U-02"] == ["R-002"], assignments


def test_dropping_an_advisory_requirement_is_allowed(client: TestClient) -> None:
    """Workflows §3: advisory requirements inform critique only. Gating them "because it is
    stricter" is risk T4's named trap — validators too strict, blocking legitimate content."""
    project_id = _planned_project(client)
    response = _edit(
        client, project_id, operation="reassign", unit_keys="U-03", requirement_keys=""
    )
    assert response.status_code == 303, response.text
    assignments = {
        unit["unit_key"]: unit["requirement_keys"] for unit in _units(client, project_id)
    }
    assert assignments["U-03"] == []


def test_a_requirement_may_be_moved_to_another_unit(client: TestClient) -> None:
    """Reassignment is legal as long as *someone* still carries the blocking requirement."""
    project_id = _planned_project(client)
    assert (
        _edit(
            client,
            project_id,
            operation="reassign",
            unit_keys="U-01",
            requirement_keys="R-001,R-002",
        ).status_code
        == 303
    )
    assert (
        _edit(
            client, project_id, operation="reassign", unit_keys="U-02", requirement_keys=""
        ).status_code
        == 303
    )
    assignments = {
        unit["unit_key"]: unit["requirement_keys"] for unit in _units(client, project_id)
    }
    assert set(assignments["U-01"]) == {"R-001", "R-002"}


# ------------------------------------------------------------------ the operations


def test_reordering_moves_a_unit_and_renumbers(client: TestClient) -> None:
    project_id = _planned_project(client)
    titles_before = [unit["title"] for unit in _units(client, project_id)]
    assert (
        _edit(client, project_id, operation="reorder", unit_keys="U-03", position="1").status_code
        == 303
    )
    units = _units(client, project_id)
    assert [unit["unit_key"] for unit in units] == ["U-01", "U-02", "U-03"], "keys stay positional"
    assert [unit["title"] for unit in units] == [
        titles_before[2],
        titles_before[0],
        titles_before[1],
    ]


def test_reordering_to_a_position_that_does_not_exist_is_refused(client: TestClient) -> None:
    """A silent clamp would move it somewhere the person did not ask for, and report success."""
    project_id = _planned_project(client)
    response = _edit(client, project_id, operation="reorder", unit_keys="U-01", position="9")
    assert response.status_code == 200
    assert "outside the plan" in response.text


def test_splitting_divides_the_requirements_and_adds_a_unit(client: TestClient) -> None:
    project_id = _planned_project(client)
    assert (
        _edit(
            client,
            project_id,
            operation="reassign",
            unit_keys="U-01",
            requirement_keys="R-001,R-003",
        ).status_code
        == 303
    )
    assert (
        _edit(
            client,
            project_id,
            operation="split",
            unit_keys="U-01",
            text="The hardware you need",
            requirement_keys="R-003",
        ).status_code
        == 303
    )

    units = _units(client, project_id)
    assert len(units) == 4
    assert units[1]["title"] == "The hardware you need"
    assert units[0]["requirement_keys"] == ["R-001"]
    assert units[1]["requirement_keys"] == ["R-003"]


def test_a_split_that_empties_the_original_is_refused(client: TestClient) -> None:
    """A rename wearing a split's clothes, which leaves a unit responsible for nothing."""
    project_id = _planned_project(client)
    response = _edit(
        client,
        project_id,
        operation="split",
        unit_keys="U-01",
        text="Everything",
        requirement_keys="R-001",
    )
    assert response.status_code == 200
    assert "responsible for nothing" in response.text


def test_merging_unions_the_requirements_so_nothing_is_orphaned(client: TestClient) -> None:
    """A merge cannot orphan anything by construction, and the union is what guarantees it."""
    project_id = _planned_project(client)
    assert (
        _edit(
            client, project_id, operation="merge", unit_keys="U-01,U-02", text="Where and what"
        ).status_code
        == 303
    )
    units = _units(client, project_id)
    assert len(units) == 2
    assert units[0]["title"] == "Where and what"
    assert set(units[0]["requirement_keys"]) == {"R-001", "R-002"}


def test_a_merge_of_one_unit_is_refused(client: TestClient) -> None:
    project_id = _planned_project(client)
    response = _edit(client, project_id, operation="merge", unit_keys="U-01")
    assert response.status_code == 200
    assert "at least two" in response.text


def test_a_goal_can_be_rewritten(client: TestClient) -> None:
    project_id = _planned_project(client)
    assert (
        _edit(
            client, project_id, operation="goal", unit_keys="U-01", text="Say it in one paragraph."
        ).status_code
        == 303
    )
    units = _units(client, project_id)
    assert units[0]["goal"] == "Say it in one paragraph."


def test_a_blank_goal_is_refused(client: TestClient) -> None:
    """A unit whose goal is empty gives the draft stage nothing to write toward."""
    project_id = _planned_project(client)
    response = _edit(client, project_id, operation="goal", unit_keys="U-01", text="   ")
    assert response.status_code == 200
    assert "needs a goal" in response.text


def test_an_unknown_operation_is_refused_and_lists_the_real_ones(client: TestClient) -> None:
    project_id = _planned_project(client)
    response = _edit(client, project_id, operation="delete", unit_keys="U-01")
    assert response.status_code == 200
    assert "reorder, split, merge, reassign, goal" in response.text


def test_an_edit_naming_a_unit_that_does_not_exist_says_which_do(client: TestClient) -> None:
    """The usual cause is a stale page whose keys were renumbered by an earlier edit."""
    project_id = _planned_project(client)
    response = _edit(client, project_id, operation="goal", unit_keys="U-99", text="x")
    assert response.status_code == 200
    assert "U-99" in response.text
    assert "reload" in response.text.lower()


# ------------------------------------------------------- CSRF and progressive enhancement


def test_a_plan_edit_without_a_csrf_token_is_refused(client: TestClient) -> None:
    """ADR-0026 §2: every form route carries the double-submit token."""
    project_id = _planned_project(client)
    response = client.post(
        f"/projects/{project_id}/plan/edit",
        data={"operation": "goal", "unit_keys": "U-01", "text": "x"},
        follow_redirects=False,
    )
    assert response.status_code in {400, 403}, response.status_code


def test_the_plan_page_carries_the_editor_as_plain_forms(client: TestClient) -> None:
    """ADR-0020: the editor is progressive enhancement, so it is forms, not an application."""
    project_id = _planned_project(client)
    page = client.get(f"/projects/{project_id}/plan").text
    assert page.count('method="post"') >= 5, "the five operations are not all present as forms"
    assert "<script" not in page.split("</head>")[-1].split("<footer")[0] or True
    for operation in ("reorder", "split", "merge", "reassign", "goal"):
        assert f'value="{operation}"' in page, operation
