"""Row WM2: the MirrorWall 0.3 surfaces this application opted into (design brief §6).

The tab strip only when ``[console] url`` is set, the status dot on the System page, the dense
project list, and the workspace's log pane fed by ``/api/v1/projects/{id}/tasks/{task}/log`` —
the same events as the API stream, as ``log`` frames closed with ``log.closed``.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from tests.e2e.test_stage_stream import _project, _scripted_runtime, _wait

from ideapress.config import load_settings
from ideapress.web.app import create_app

CONSOLE = "https://jordan-main.local:8769"


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app(load_settings().settings, runtime_builder=_scripted_runtime)
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        yield test_client


def test_no_tab_strip_without_a_console_url(client: TestClient) -> None:
    page = client.get("/system").text
    assert 'class="app-tabs"' not in page and 'class="app-tab"' not in page


def test_the_tab_strip_links_the_console_and_the_peers_through_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IDEAPRESS_CONSOLE__URL", CONSOLE + "/")
    app = create_app(load_settings().settings, runtime_builder=_scripted_runtime)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        page = client.get("/system").text
    assert f'<a href="{CONSOLE}" class="app-tab">' in page
    assert '<a href="/" class="app-tab" aria-current="page">' in page
    for peer in ("freeweight", "loadcoach", "promptcadence"):
        assert f'<a href="{CONSOLE}/apps/{peer}" class="app-tab">' in page


def test_the_system_page_shows_a_status_dot_per_health_component(client: TestClient) -> None:
    page = client.get("/system").text
    components = client.get("/api/v1/health").json()["components"]
    assert page.count('class="status-dot"') == len(components)


def test_the_project_list_is_dense(client: TestClient) -> None:
    _project(client)
    assert 'data-density="dense"' in client.get("/").text


def test_the_stage_log_is_the_task_stream_as_log_frames(client: TestClient) -> None:
    project_id = _project(client)
    task = client.post(f"/api/v1/projects/{project_id}/plan").json()
    _wait(client, project_id, task["task_id"])
    body = ""
    with client.stream(
        "GET", f"/api/v1/projects/{project_id}/tasks/{task['task_id']}/log"
    ) as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        for chunk in response.iter_text():
            body += chunk
            if "event: log.closed" in body:
                break
    assert 'event: log\ndata: <div class="log-pane-line" data-level=' in body
    assert "stage.started" in body and "stage.completed" in body
    assert body.rstrip().endswith("event: log.closed\ndata: {}")
    # The page itself carries the pane only while a stage runs; after it, no htmx either.
    page = client.get(f"/projects/{project_id}/workspace").text
    assert "vendor/htmx" not in page and "data-log-pane" not in page


def test_the_workspaces_own_assets_are_served(client: TestClient) -> None:
    """Found by the JS-per-page budget: workspace.css/js and diff.js were rendered under
    MirrorWall's static prefix, where nothing served them (fixed at row WM2)."""
    project_id = _project(client)
    page = client.get(f"/projects/{project_id}/workspace").text
    for name in ("css/workspace.css", "js/workspace.js", "js/diff.js"):
        assert f"/static/ideapress/{name}" in page, name
        assert client.get(f"/static/ideapress/{name}").status_code == 200, name
