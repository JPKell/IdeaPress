"""The stage API over HTTP, including SSE replay after a disconnect.

Risk T7's mitigation is that a long stage survives a browser refresh. Asserting that means
reconnecting with ``Last-Event-ID`` and reading what a client would have missed — not asserting
that a store method exists.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

from ideapress.config import Settings, load_settings
from ideapress.infrastructure.backends.fake import FakeBackend, default_fake_script
from ideapress.services.runtime import Runtime
from ideapress.web.app import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator

LOOPBACK = "http://127.0.0.1:8767"

BRIEF = (
    "The article must state that inference runs entirely on the reader's own machine and that no "
    "document content is uploaded anywhere."
)
REQUIREMENTS = {
    "requirements": [
        {
            "text": "The article must state that inference runs on the reader's own machine.",
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
        },
        {
            "title": "What that means for you",
            "goal_text": "Draw the consequence.",
            "requirement_keys": ["R-001"],
        },
    ]
}


def _scripted_runtime(settings: Settings) -> Runtime:
    from modelrack.testing import FakeGeneration, FakeScript

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
    settings = load_settings().settings
    app = create_app(settings, runtime_builder=_scripted_runtime)
    with TestClient(app, base_url=LOOPBACK) as test_client:
        yield test_client


def _project(client: TestClient) -> str:
    identifier = client.post(
        "/api/v1/projects", json={"title": "Local inference for writers", "brief": BRIEF}
    ).json()["id"]
    assert isinstance(identifier, str)
    return identifier


def _wait(client: TestClient, project_id: str, task_id: str) -> dict[str, Any]:
    for _ in range(400):
        report: dict[str, Any] = client.get(f"/api/v1/projects/{project_id}/tasks/{task_id}").json()
        if report["state"] in {"completed", "failed", "cancelled", "interrupted"}:
            return report
        time.sleep(0.02)
    message = "the stage never finished"
    raise AssertionError(message)


def test_post_plan_returns_a_task_with_a_stream_url(client: TestClient) -> None:
    project_id = _project(client)
    response = client.post(f"/api/v1/projects/{project_id}/plan")
    assert response.status_code == 202
    task = response.json()
    assert task["stage"] == "outline"
    assert task["stream_url"].endswith("/stream")
    report = _wait(client, project_id, task["task_id"])
    assert report["state"] == "completed"
    assert report["units_total"] == 2


def test_the_task_report_carries_every_attempt_with_its_prompt_provenance(
    client: TestClient,
) -> None:
    project_id = _project(client)
    task = client.post(f"/api/v1/projects/{project_id}/plan").json()
    report = _wait(client, project_id, task["task_id"])
    stages = {attempt["stage"] for attempt in report["attempts"]}
    assert stages == {"requirements", "outline"}
    for attempt in report["attempts"]:
        assert attempt["prompt_id"].startswith("stages.")
        assert attempt["prompt_version"] == "1.0.0"


def test_the_stream_replays_everything_from_the_beginning(client: TestClient) -> None:
    project_id = _project(client)
    task = client.post(f"/api/v1/projects/{project_id}/plan").json()
    _wait(client, project_id, task["task_id"])

    with client.stream("GET", task["stream_url"]) as response:
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
        body = "".join(response.iter_text())
    assert "stage.started" in body
    assert "stage.completed" in body
    assert "requirements.compiled" in body


def test_a_reconnect_with_last_event_id_replays_only_what_was_missed(
    client: TestClient,
) -> None:
    """Risk T7: a long stage survives a browser refresh."""
    project_id = _project(client)
    task = client.post(f"/api/v1/projects/{project_id}/plan").json()
    _wait(client, project_id, task["task_id"])

    with client.stream("GET", task["stream_url"]) as response:
        everything = "".join(response.iter_text())
    ids = [line.split(": ", 1)[1] for line in everything.splitlines() if line.startswith("id: ")]
    assert ids == [str(n) for n in range(1, len(ids) + 1)], "gap-free over the wire, too"

    with client.stream("GET", task["stream_url"], headers={"Last-Event-ID": ids[1]}) as response:
        resumed = "".join(response.iter_text())
    resumed_ids = [
        line.split(": ", 1)[1] for line in resumed.splitlines() if line.startswith("id: ")
    ]
    assert resumed_ids == ids[2:], "a reconnect replays exactly what was missed and no more"
    assert "stage.started" not in resumed, "and does not repeat what was already seen"


def test_a_second_stage_for_one_project_is_a_409(client: TestClient) -> None:
    project_id = _project(client)
    first = client.post(f"/api/v1/projects/{project_id}/plan")
    assert first.status_code == 202
    second = client.post(f"/api/v1/projects/{project_id}/plan")
    if second.status_code == 202:
        pytest.skip("the first stage finished before the second was posted")
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "STAGE_ALREADY_RUNNING"


def test_a_stage_that_is_not_a_stage_is_refused_by_name(client: TestClient) -> None:
    project_id = _project(client)
    response = client.post(f"/api/v1/projects/{project_id}/stages/audit/run", json={})
    assert response.status_code == 409
    assert "audit" in response.json()["error"]["message"]


def test_the_workflow_list_reports_the_stage_table(client: TestClient) -> None:
    workflows = client.get("/api/v1/workflows").json()["workflows"]
    stages = workflows[0]["stages"]
    assert len(stages) == 16
    assert [s["stage"] for s in stages][:2] == ["requirements", "research"]
    assert sum(1 for s in stages if not s["uses_model"]) == 5


def test_the_plan_page_shows_each_requirement_with_its_quotation(client: TestClient) -> None:
    project_id = _project(client)
    task = client.post(f"/api/v1/projects/{project_id}/plan").json()
    _wait(client, project_id, task["task_id"])

    page = client.get(f"/projects/{project_id}/plan", headers={"accept": "text/html"})
    assert page.status_code == 200
    assert "R-001" in page.text
    assert "inference runs entirely on the reader&#39;s own machine" in page.text
    assert "U-01" in page.text
    assert "blocking" in page.text


def test_a_task_belonging_to_another_project_is_not_disclosed(client: TestClient) -> None:
    first = _project(client)
    second = _project(client)
    task = client.post(f"/api/v1/projects/{first}/plan").json()
    _wait(client, first, task["task_id"])
    response = client.get(f"/api/v1/projects/{second}/tasks/{task['task_id']}")
    assert response.status_code == 404


def test_the_unit_page_renders_model_output_inert(client: TestClient) -> None:
    """Risk S1 at the rendering boundary: hostile text is shown as text, in every view."""

    from modelrack.testing import FakeGeneration, FakeScript

    from ideapress.infrastructure.backends import fake as fake_module
    from ideapress.services.inference import InferenceGateway
    from ideapress.services.stages import StageRunner

    project_id = _project(client)
    task = client.post(f"/api/v1/projects/{project_id}/plan").json()
    _wait(client, project_id, task["task_id"])

    # Script tags and template syntax are *advisory*: an article about web security quotes them
    # legitimately, and escaping is the control. A path traversal would be blocking and the unit
    # would pause instead of committing — which is the subject of the next test, not this one.
    hostile = (
        "Everything runs on your own machine. <script>alert(1)</script> {{ 7*7 }} "
        "and the rest reads normally enough for a section. " * 4
    )
    runtime = client.app.state.runtime  # type: ignore[attr-defined]  # the served runtime
    backend = fake_module.FakeBackend(
        script=FakeScript(
            models=fake_module.default_fake_script().models,
            capabilities=fake_module.default_fake_script().capabilities,
            generations=(
                FakeGeneration(text=hostile),
                FakeGeneration(text=json.dumps({"findings": []})),
                FakeGeneration(text=json.dumps({"verdict": "acceptable", "rationale": "ok"})),
            ),
            repeat_final_generation=True,
        ),
        seed=5,
    )
    gateway = InferenceGateway(
        backend=backend,
        bindings=runtime.settings.models.stages,
        execution=runtime.settings.execution,
    )
    runtime._gateway = gateway  # noqa: SLF001 — substituting the backend is the point
    runtime._runner = StageRunner(  # noqa: SLF001
        runtime.storage, gateway=gateway, sink=runtime.events
    )

    run = client.post(f"/api/v1/projects/{project_id}/stages/draft/run", json={}).json()
    _wait(client, project_id, run["task_id"])

    page = client.get(f"/projects/{project_id}/units/U-01", headers={"accept": "text/html"})
    assert page.status_code == 200
    assert "<script>alert(1)</script>" not in page.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text
    assert "{{ 7*7 }}" in page.text, "template syntax survives as text, never evaluated"


def test_the_unit_api_reports_the_full_provenance(client: TestClient) -> None:
    project_id = _project(client)
    task = client.post(f"/api/v1/projects/{project_id}/plan").json()
    _wait(client, project_id, task["task_id"])
    listed = client.get(f"/api/v1/projects/{project_id}/units").json()["units"]
    assert [u["unit_key"] for u in listed] == ["U-01", "U-02"]
    assert all(u["state"] == "planned" for u in listed)

    detail = client.get(f"/api/v1/projects/{project_id}/units/U-01").json()
    assert detail["unit_key"] == "U-01"
    assert detail["requirements"][0]["key"] == "R-001"
    assert detail["content"] == ""
    history = client.get(f"/api/v1/projects/{project_id}/units/U-01/history").json()
    assert history["versions"] == []


def test_a_path_traversal_in_model_output_blocks_the_commit(client: TestClient) -> None:
    """Risk S2: the one safety finding that is blocking, because no prose needs it."""
    from modelrack.testing import FakeGeneration, FakeScript

    from ideapress.infrastructure.backends import fake as fake_module
    from ideapress.services.inference import InferenceGateway
    from ideapress.services.stages import StageRunner

    project_id = _project(client)
    task = client.post(f"/api/v1/projects/{project_id}/plan").json()
    _wait(client, project_id, task["task_id"])

    # The traversal fails a *blocking* validation check, so the repair loop runs three times and
    # the unit pauses — the review stage is never reached, which is why this script has no audit.
    traversal = (
        "Everything runs on your own machine and nothing is uploaded. "
        "Read ../../etc/passwd for the details. " * 4
    )
    runtime = client.app.state.runtime  # type: ignore[attr-defined]  # the served runtime
    backend = fake_module.FakeBackend(
        script=FakeScript(
            models=fake_module.default_fake_script().models,
            capabilities=fake_module.default_fake_script().capabilities,
            generations=(FakeGeneration(text=traversal),),
            repeat_final_generation=True,
        ),
        seed=5,
    )
    gateway = InferenceGateway(
        backend=backend,
        bindings=runtime.settings.models.stages,
        execution=runtime.settings.execution,
    )
    runtime._gateway = gateway  # noqa: SLF001 — substituting the backend is the point
    runtime._runner = StageRunner(  # noqa: SLF001
        runtime.storage, gateway=gateway, sink=runtime.events
    )

    run = client.post(f"/api/v1/projects/{project_id}/stages/draft/run", json={}).json()
    _wait(client, project_id, run["task_id"])

    detail = client.get(f"/api/v1/projects/{project_id}/units/U-01").json()
    assert detail["state"] == "paused"
    assert detail["version"] is None, "nothing was committed"
    assert "no_path_traversal" in (detail["paused_reason"] or "")
