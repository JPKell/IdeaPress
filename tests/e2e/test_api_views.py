"""The views IdeaPress's pages had and its API lacked, over HTTP (row WP5).

WeightRoomGym, the host operator's console, drives IdeaPress over ``/api/v1`` only and never posts
to its HTML routes (arc index §2 item 4). Every view the pages assemble in-process, and the API did
not serve, is served here, and each is asserted against a project a scripted model has planned and
drafted, so what is checked is what IdeaPress recorded rather than what a model said.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

from ideapress.config import load_settings
from ideapress.services.runtime import build_runtime
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
            "text": "The unit should keep a plain register.",
            "blocking": False,
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
            "target_words": 50,
        },
        {
            "title": "What it costs",
            "goal_text": "Be honest about the trade.",
            "requirement_keys": ["R-001"],
            "target_words": 50,
        },
    ]
}
DRAFT = (
    "Everything happens on your own machine. The model reads what you wrote and answers there, "
    "with nothing uploaded and no account needed. The hardware is yours to provide, which is the "
    "trade you are making for keeping the work where you made it."
)
NO_FINDINGS: dict[str, Any] = {"findings": []}
ACCEPTABLE = {"verdict": "acceptable", "rationale": "it meets the bar"}


def _script(*answers: Any) -> Any:  # noqa: ANN401 — scripted answers in, a FakeBackend out
    from modelrack.testing import FakeGeneration, FakeScript

    from ideapress.infrastructure.backends.fake import FakeBackend, default_fake_script

    return FakeBackend(
        script=FakeScript(
            models=default_fake_script().models,
            capabilities=default_fake_script().capabilities,
            generations=tuple(
                FakeGeneration(text=a if isinstance(a, str) else json.dumps(a)) for a in answers
            ),
            repeat_final_generation=True,
        ),
        seed=5,
    )


def _with(runtime: Runtime, *answers: Any) -> None:  # noqa: ANN401 — scripted answers
    from ideapress.services.inference import InferenceGateway
    from ideapress.services.stages import StageRunner

    backend = _script(*answers)
    gateway = InferenceGateway(
        backend=backend,
        bindings=runtime.settings.models.stages,
        execution=runtime.settings.execution,
    )
    runtime._gateway = gateway  # noqa: SLF001 — substituting the backend is the point
    runtime._backend = backend  # noqa: SLF001
    runtime._runner = StageRunner(  # noqa: SLF001
        runtime.storage, gateway=gateway, sink=runtime.events
    )


def _wait(runtime: Runtime, task_id: str, *, timeout: float = 20.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if runtime.runner.is_finished(task_id):
            return runtime.runner.run_state(task_id) or "unknown"
        time.sleep(0.02)
    message = "the stage did not finish"
    raise AssertionError(message)


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app(load_settings().settings, runtime_builder=build_runtime)
    with TestClient(app, base_url=LOOPBACK) as test_client:
        yield test_client


def _runtime(client: TestClient) -> Runtime:
    runtime: Runtime = client.app.state.runtime  # type: ignore[attr-defined]
    return runtime


def _planned(client: TestClient) -> str:
    runtime = _runtime(client)
    project_id = client.post("/api/v1/projects", json={"title": "Local inference", "brief": BRIEF})
    identifier = str(project_id.json()["id"])
    _with(runtime, REQUIREMENTS, PLAN)
    task = client.post(f"/api/v1/projects/{identifier}/plan").json()
    assert _wait(runtime, task["task_id"]) == "completed"
    return identifier


def _drafted(client: TestClient) -> str:
    runtime = _runtime(client)
    project_id = _planned(client)
    _with(runtime, DRAFT, NO_FINDINGS, ACCEPTABLE)
    task = client.post(
        f"/api/v1/projects/{project_id}/stages/draft/run", json={"units": ["U-01"]}
    ).json()
    assert _wait(runtime, task["task_id"]) == "completed"
    return project_id


# --- Projects: the list's cursor, and the detail api.md §2 promised -------------------------------


def test_the_project_list_pages_by_cursor(client: TestClient) -> None:
    ids = {client.post("/api/v1/projects", json={"title": f"P{n}"}).json()["id"] for n in range(3)}

    first = client.get("/api/v1/projects", params={"limit": 2}).json()
    assert len(first["items"]) == 2
    assert first["page"]["has_more"] is True
    assert first["page"]["next_cursor"]
    second = client.get(
        "/api/v1/projects", params={"limit": 2, "cursor": first["page"]["next_cursor"]}
    ).json()
    assert second["page"] == {"limit": 2, "next_cursor": None, "has_more": False, "total": None}
    assert {item["id"] for item in first["items"] + second["items"]} == ids


def test_a_forged_project_cursor_is_refused_by_name(client: TestClient) -> None:
    response = client.get("/api/v1/projects", params={"cursor": "not-a-cursor"})
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"]["fields"][0]["path"] == "cursor"


def test_a_new_project_has_no_plan_no_units_and_no_stage_history(client: TestClient) -> None:
    project_id = client.post("/api/v1/projects", json={"title": "Fresh"}).json()["id"]
    body = client.get(f"/api/v1/projects/{project_id}").json()
    assert (body["plan"], body["units"], body["stages"], body["running_task_id"]) == (
        None,
        [],
        [],
        None,
    )


def test_project_detail_carries_the_plan_summary_unit_states_and_stage_history(
    client: TestClient,
) -> None:
    project_id = _drafted(client)
    body = client.get(f"/api/v1/projects/{project_id}").json()

    assert body["title"] == "Local inference"
    assert body["plan"] == {"units": 2, "requirements": 2, "blocking": 1}
    assert [(u["unit_key"], u["state"], u["version"]) for u in body["units"]] == [
        ("U-01", "committed", 1),
        ("U-02", "planned", None),
    ]
    assert [(s["stage"], s["state"]) for s in body["stages"]] == [
        ("draft", "completed"),
        ("outline", "completed"),
    ], "newest first; the plan runs as its outline stage"
    draft = body["stages"][0]
    assert draft["units_total"] == 1
    assert draft["units_completed"] == 1
    assert draft["task_id"]
    assert draft["stream_url"].endswith(f"/tasks/{draft['task_id']}/stream")
    assert body["running_task_id"] is None


# --- The plan: read it, and edit it under the gate the page's editor is under ---------------------


def test_the_plan_reads_every_requirement_with_its_source_and_the_unit_plan(
    client: TestClient,
) -> None:
    project_id = _planned(client)
    body = client.get(f"/api/v1/projects/{project_id}/plan").json()

    assert [(r["key"], r["blocking"], r["mechanical"]) for r in body["requirements"]] == [
        ("R-001", True, True),
        ("R-002", False, False),
    ]
    first = body["requirements"][0]
    assert first["quote"] == "inference runs entirely on the reader's own machine"
    assert first["units"] == ["U-01", "U-02"]
    assert [(u["key"], u["title"], u["state"]) for u in body["units"]] == [
        ("U-01", "Where the work happens", "planned"),
        ("U-02", "What it costs", "planned"),
    ]
    assert "planned" in body["editable_states"]
    assert "project" not in body


def test_the_plan_of_a_missing_project_is_a_named_404(client: TestClient) -> None:
    response = client.get("/api/v1/projects/01ZZZZZZZZZZZZZZZZZZZZZZZZ/plan")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PROJECT_NOT_FOUND"


def test_a_plan_edit_is_applied_and_answered_with_the_plan_as_stored(client: TestClient) -> None:
    project_id = _planned(client)
    edits = f"/api/v1/projects/{project_id}/plan/edits"

    goal = client.post(
        edits, json={"operation": "goal", "unit_keys": ["U-02"], "text": "Name the cost."}
    )
    assert goal.status_code == 200, goal.text
    assert goal.json()["units"][1]["goal"] == "Name the cost."
    assert client.get(f"/api/v1/projects/{project_id}/plan").json() == goal.json()

    moved = client.post(edits, json={"operation": "reorder", "unit_keys": ["U-02"], "position": 1})
    assert moved.status_code == 200, moved.text
    assert [u["title"] for u in moved.json()["units"]] == [
        "What it costs",
        "Where the work happens",
    ]


def test_an_edit_that_orphans_a_blocking_requirement_is_refused_and_changes_nothing(
    client: TestClient,
) -> None:
    project_id = _planned(client)
    edits = f"/api/v1/projects/{project_id}/plan/edits"
    narrowed = client.post(
        edits,
        json={"operation": "reassign", "unit_keys": ["U-02"], "requirement_keys": ["R-002"]},
    )
    assert narrowed.status_code == 200, narrowed.text
    before = client.get(f"/api/v1/projects/{project_id}/plan").json()

    refused = client.post(
        edits,
        json={"operation": "reassign", "unit_keys": [" U-01 "], "requirement_keys": ["R-002", ""]},
    )
    assert refused.status_code == 400
    error = refused.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"]["unassigned_requirement_keys"] == ["R-001"]
    assert client.get(f"/api/v1/projects/{project_id}/plan").json() == before


def test_a_structural_edit_over_committed_work_is_refused_naming_the_unit(
    client: TestClient,
) -> None:
    project_id = _drafted(client)
    refused = client.post(
        f"/api/v1/projects/{project_id}/plan/edits",
        json={"operation": "reorder", "unit_keys": ["U-02"], "position": 1},
    )
    assert refused.status_code == 400
    assert refused.json()["error"]["details"]["protected_unit_keys"] == ["U-01"]


# --- Research: where a fetch may go, and every call with its egress decision ----------------------


def test_research_lists_where_a_fetch_may_go_and_every_call_with_its_egress_decision(
    client: TestClient,
) -> None:
    import httpx

    from ideapress.config import ResearchSettings
    from ideapress.services.research import research_body

    runtime = _runtime(client)
    runtime.settings.research = ResearchSettings(
        allowed_hosts=("docs.example",), max_data_classification="public"
    )
    brief = "Background: https://docs.example/paper and https://blocked.example/x — read both."
    created = client.post("/api/v1/projects", json={"title": "Paper", "brief": brief})
    project_id = created.json()["id"]
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200, text="A fetched paragraph.", headers={"content-type": "text/plain"}
        )
    )
    task = runtime.runner.start(
        project_id=project_id,
        stage="research",
        body=research_body(
            runtime,
            project_id=project_id,
            resolver=lambda _host: ["93.184.216.34"],
            transport=transport,
        ),
    )
    assert _wait(runtime, task.run_id) == "completed"

    body = client.get(f"/api/v1/projects/{project_id}/research").json()
    assert body["allowed_hosts"] == ["docs.example"]
    assert [note["citation"] for note in body["notes"]] == ["https://docs.example/paper"]
    calls = body["tool_calls"]
    assert [(c["tool"], c["status"], c["reason"]) for c in calls] == [
        ("http_fetch", "ok", None),
        ("http_fetch", "refused", "host_not_allowed"),
    ]
    for call in calls:
        decision = call["egress_decision"]
        assert decision is not None, "every fetch was decided before it ran"
        assert decision["request"]["source_ref"] == call["invocation_id"]
    assert calls[0]["egress_decision"]["verdict"] == "approved"


def test_a_project_that_never_researched_has_an_empty_record(client: TestClient) -> None:
    project_id = client.post("/api/v1/projects", json={"title": "Plain"}).json()["id"]
    body = client.get(f"/api/v1/projects/{project_id}/research").json()
    assert (body["notes"], body["tool_calls"]) == ([], [])
    assert client.get("/api/v1/projects/01ZZZZZZZZZZZZZZZZZZZZZZZZ/research").status_code == 404


# --- The workspace: one unit's whole view, as IdeaPress assembles it ------------------------------

REVISED = (
    "Everything happens on your own machine. The model reads what you wrote and answers there, "
    "with nothing uploaded and no account needed, and no network involved at any point. The "
    "hardware is yours to provide: that is the trade for keeping the work where you made it."
)


def test_the_workspace_view_is_the_pages_own_data_for_one_unit(client: TestClient) -> None:
    project_id = _drafted(client)
    body = client.get(f"/api/v1/projects/{project_id}/workspace", params={"unit": "U-01"}).json()

    assert body["project"]["id"] == project_id
    assert [unit["unit_key"] for unit in body["units"]] == ["U-01", "U-02"]
    assert body["selected_unit_key"] == "U-01"
    assert body["unit"]["content"] == DRAFT
    assert body["pause"]["paused"] is False
    assert body["coverage_summary"]["total"] == 2
    assert body["backend"]["mode"] == "fake"
    assert (body["diff"], body["running_task_id"]) == (None, None)
    assert body["research"] == {"allowed_hosts": []}


def test_the_workspace_diffs_the_current_version_against_an_earlier_one(client: TestClient) -> None:
    runtime = _runtime(client)
    project_id = _drafted(client)
    _with(runtime, REVISED, NO_FINDINGS, ACCEPTABLE)
    task = client.post(
        f"/api/v1/projects/{project_id}/units/U-01/revise",
        json={"instructions": "Say that no network is involved."},
    ).json()
    assert _wait(runtime, task["task_id"]) == "completed"

    body = client.get(
        f"/api/v1/projects/{project_id}/workspace", params={"unit": "U-01", "compare": 1}
    ).json()
    diff = body["diff"]
    assert (diff["diff_old_version"], diff["diff_new_version"]) == (1, 2)
    assert diff["unavailable"] == ""
    assert diff["diff_added"] >= 1
    assert [entry["version"] for entry in body["unit"]["history"]] == [2, 1]


def test_the_workspace_of_a_missing_project_is_a_named_404(client: TestClient) -> None:
    response = client.get("/api/v1/projects/01ZZZZZZZZZZZZZZZZZZZZZZZZ/workspace")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PROJECT_NOT_FOUND"


# --- History: every version with the run that produced it -----------------------------------------


def test_history_carries_the_attempts_validations_and_verdicts_behind_each_version(
    client: TestClient,
) -> None:
    runtime = _runtime(client)
    project_id = _drafted(client)
    _with(runtime, REVISED, NO_FINDINGS, ACCEPTABLE)
    task = client.post(
        f"/api/v1/projects/{project_id}/units/U-01/revise",
        json={"instructions": "Say that no network is involved."},
    ).json()
    assert _wait(runtime, task["task_id"]) == "completed"

    versions = client.get(f"/api/v1/projects/{project_id}/units/U-01/history").json()["versions"]
    assert [version["version"] for version in versions] == [2, 1]
    newest, first = versions
    assert [a["stage"] for a in first["attempts"]] == ["draft", "audit_fast", "critique"]
    assert [a["stage"] for a in newest["attempts"]] == ["revise", "audit_fast", "critique"]
    assert newest["stage_run_id"] != first["stage_run_id"]
    produced = first["attempts"][0]["attempt_id"]
    assert first["validations"]
    assert {check["attempt_id"] for check in first["validations"]} == {produced}
    assert [critique["verdict"] for critique in newest["critiques"]] == ["acceptable"]
    assert newest["findings"] == []
    assert first["coverage"], "the coverage each version committed with is still there"


def test_the_history_of_an_unknown_unit_is_a_named_404(client: TestClient) -> None:
    project_id = _planned(client)
    response = client.get(f"/api/v1/projects/{project_id}/units/U-09/history")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "UNIT_NOT_FOUND"


# --- Export formats, and a delete that archives first ---------------------------------------------


def test_export_formats_say_what_each_contains(client: TestClient) -> None:
    formats = client.get("/api/v1/export/formats").json()["formats"]
    assert [entry["format"] for entry in formats] == ["html", "json", "markdown"]
    assert all(entry["description"] for entry in formats)
    html = next(entry for entry in formats if entry["format"] == "html")
    assert "no network" in html["description"]


def test_a_delete_previews_where_the_archive_goes_then_archives_before_deleting(
    client: TestClient,
) -> None:
    from pathlib import Path

    from ideapress.services.project_archive import inspect_archive

    project_id = _drafted(client)
    preview = client.delete(f"/api/v1/projects/{project_id}", params={"archive": "true"}).json()
    assert (preview["deleted"], preview["archive"]) == (False, None)
    directory = Path(preview["archive_directory"])
    assert not directory.exists(), "a preview writes nothing"

    done = client.delete(
        f"/api/v1/projects/{project_id}", params={"confirm": "true", "archive": "true"}
    ).json()
    assert done["deleted"] is True
    written = Path(done["archive"]["path"])
    assert written.parent == directory
    assert written.name.startswith("local-inference-")
    assert done["archive"]["size_bytes"] == written.stat().st_size
    report = inspect_archive(written)
    assert report.safe
    assert report.project_title == "Local inference"
    assert client.get(f"/api/v1/projects/{project_id}").status_code == 404


def test_an_archive_that_cannot_be_written_deletes_nothing(client: TestClient) -> None:
    from pathlib import Path

    project_id = _drafted(client)
    preview = client.delete(f"/api/v1/projects/{project_id}").json()
    blocked = Path(preview["archive_directory"])
    blocked.parent.mkdir(parents=True, exist_ok=True)
    blocked.write_text("a file where the archive directory would be", encoding="utf-8")

    response = client.delete(
        f"/api/v1/projects/{project_id}", params={"confirm": "true", "archive": "true"}
    )
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "EXPORT_FAILED"
    assert "Nothing was deleted" in response.json()["error"]["message"]
    assert client.get(f"/api/v1/projects/{project_id}").status_code == 200
