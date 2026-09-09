"""The workspace's "Run research" form (row N2, closing M1's D10), over HTTP.

Two properties. The click starts the stage through the same service the API and CLI use, so the
task appears and finishes; and the page states, before the click, which hosts a fetch may reach —
on a default installation, none. No model runs: `research` is the stage that touches no model.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from mirrorwall import CSRF_COOKIE_NAME, CSRF_FIELD_NAME

from ideapress.config import load_settings
from ideapress.services.runtime import build_runtime
from ideapress.web.app import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator

LOOPBACK = "http://127.0.0.1:8767"


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app(load_settings().settings, runtime_builder=build_runtime)
    with TestClient(app, base_url=LOOPBACK) as test_client:
        yield test_client


def test_the_workspace_form_starts_research_and_says_no_host_is_allowed(
    client: TestClient,
) -> None:
    project_id = client.post(
        "/api/v1/projects",
        json={"title": "Paper", "brief": "Read https://docs.example/paper first."},
    ).json()["id"]

    page = client.get(f"/projects/{project_id}/workspace")
    assert page.status_code == 200, page.text
    assert 'action="/projects/' + project_id + '/research"' in page.text
    assert "No host is allowed on this installation" in page.text
    # Header, not jar: the `__Host-` cookie is Secure and httpx will not store it over plain http.
    token = page.headers["set-cookie"].split(f"{CSRF_COOKIE_NAME}=", 1)[1].split(";", 1)[0]

    response = client.post(
        f"/projects/{project_id}/research",
        data={CSRF_FIELD_NAME: token},
        headers={"Cookie": f"{CSRF_COOKIE_NAME}={token}"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    assert response.headers["location"] == f"/projects/{project_id}/workspace"

    # The same runner the API and CLI start the stage on: it ran, and it finished.
    runtime = client.app.state.runtime  # type: ignore[attr-defined]  # set by the lifespan
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and runtime.runner.active_task(project_id) is not None:
        time.sleep(0.02)
    assert runtime.runner.active_task(project_id) is None, "the research stage did not finish"

    # Without the token the middleware refuses the form: the click is a person's, never a page's.
    refused = client.post(
        f"/projects/{project_id}/research", data={"forged": "1"}, follow_redirects=False
    )
    assert refused.status_code == 403, refused.text
