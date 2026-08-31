"""Projects over HTTP and over the CLI: the same lifecycle through both doors."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from ideapress.cli.main import app as cli_app
from ideapress.config import load_settings
from ideapress.services.runtime import build_runtime
from ideapress.web.app import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator

LOOPBACK = "http://127.0.0.1:8767"
runner = CliRunner()


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app(load_settings().settings, runtime_builder=build_runtime)
    with TestClient(app, base_url=LOOPBACK) as test_client:
        yield test_client


def test_health_is_ok_once_storage_is_migrated(client: TestClient) -> None:
    """P1 AC1: `serve` starts with zero configuration; the schema is created on the way up."""
    components = {c["name"]: c["status"] for c in client.get("/api/v1/health").json()["components"]}
    assert components["database"] == "ok"


def test_create_list_get_over_http(client: TestClient) -> None:
    created = client.post(
        "/api/v1/projects", json={"title": "Local inference for writers", "brief": "A brief."}
    )
    assert created.status_code == 201
    body = created.json()
    assert body["slug"] == "local-inference-for-writers"
    assert body["status"] == "draft"

    listed = client.get("/api/v1/projects").json()
    assert [item["id"] for item in listed["items"]] == [body["id"]]

    fetched = client.get(f"/api/v1/projects/{body['id']}").json()
    assert fetched["brief"] == "A brief."


def test_a_missing_project_is_a_named_404(client: TestClient) -> None:
    response = client.get("/api/v1/projects/01ZZZZZZZZZZZZZZZZZZZZZZZZ")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PROJECT_NOT_FOUND"


def test_an_unknown_body_field_is_refused(client: TestClient) -> None:
    response = client.post("/api/v1/projects", json={"title": "x", "slug": "chosen-by-caller"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_delete_previews_then_deletes(client: TestClient) -> None:
    project_id = client.post("/api/v1/projects", json={"title": "Temporary"}).json()["id"]

    preview = client.delete(f"/api/v1/projects/{project_id}").json()
    assert preview["deleted"] is False
    assert preview["project"]["id"] == project_id
    assert client.get(f"/api/v1/projects/{project_id}").status_code == 200

    confirmed = client.delete(f"/api/v1/projects/{project_id}?confirm=true").json()
    assert confirmed["deleted"] is True
    assert client.get(f"/api/v1/projects/{project_id}").status_code == 404


def test_update_changes_the_brief(client: TestClient) -> None:
    project_id = client.post("/api/v1/projects", json={"title": "Draft"}).json()["id"]
    updated = client.put(f"/api/v1/projects/{project_id}", json={"brief": "rewritten"}).json()
    assert updated["brief"] == "rewritten"


def test_the_list_page_links_each_project_by_href(client: TestClient) -> None:
    """Assert links by `href=`, not by link text: the text is the user's own title."""
    project_id = client.post("/api/v1/projects", json={"title": "Findable"}).json()["id"]
    page = client.get("/", headers={"accept": "text/html"}).text
    assert f'href="/projects/{project_id}"' in page


def test_a_hostile_title_is_rendered_inert_on_both_pages(client: TestClient) -> None:
    """Risk S1: the most dangerous input this suite handles is text it did not write."""
    hostile = "<script>alert(1)</script> {{ 7*7 }} ../../etc/passwd"
    project_id = client.post("/api/v1/projects", json={"title": hostile}).json()["id"]

    for path in ("/", f"/projects/{project_id}"):
        page = client.get(path, headers={"accept": "text/html"}).text
        assert "<script>alert(1)</script>" not in page
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
        # The template expression survives verbatim, escaped: proof it was rendered as text
        # rather than evaluated. Asserting its *result* is absent would be weaker and flaky —
        # "49" occurs by chance in a random CSRF token roughly one page in twenty.
        assert "{{ 7*7 }}" in page
        assert "../../etc/passwd" in page, "stored as text; only the slug touches the filesystem"


def test_the_form_post_creates_and_redirects(client: TestClient) -> None:
    """The token is echoed back explicitly, because httpx will not replay a `Secure` cookie.

    Browsers treat ``http://127.0.0.1`` as a secure context and do send ``__Host-`` cookies there,
    which is the loopback bind IdeaPress defaults to; httpx goes by scheme alone. Sending the
    header is the correct fix — dropping ``Secure`` to make a test client happy would remove the
    control on every real deployment (the M5 lesson).
    """
    from mirrorwall import CSRF_COOKIE_NAME, CSRF_FIELD_NAME

    page = client.get("/", headers={"accept": "text/html"})
    token = page.headers["set-cookie"].split(f"{CSRF_COOKIE_NAME}=", 1)[1].split(";", 1)[0]
    assert f'value="{token}"' in page.text

    response = client.post(
        "/projects",
        data={"title": "From the form", "brief": "b", CSRF_FIELD_NAME: token},
        headers={"Cookie": f"{CSRF_COOKIE_NAME}={token}"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/projects/")


def test_a_form_post_without_the_token_is_refused(client: TestClient) -> None:
    client.get("/", headers={"accept": "text/html"})
    response = client.post("/projects", data={"title": "Forged"}, follow_redirects=False)
    assert response.status_code == 403


def test_the_csrf_cookie_carries_the_hardening_flags(client: TestClient) -> None:
    """ADR-0026 §2: `__Host-` prefix, Secure, HttpOnly, SameSite=strict, path=/."""
    cookie = client.get("/", headers={"accept": "text/html"}).headers["set-cookie"]
    assert cookie.startswith("__Host-mw-csrf=")
    lowered = cookie.lower()
    assert "secure" in lowered
    assert "httponly" in lowered
    assert "samesite=strict" in lowered
    assert "path=/" in lowered


def test_the_same_lifecycle_through_the_cli() -> None:
    """The CLI and the API reach one service; neither has a private path to the database."""
    import json

    assert runner.invoke(cli_app, ["db", "upgrade"]).exit_code == 0
    created = runner.invoke(
        cli_app, ["project", "create", "Through the CLI", "--brief", "b", "--json"]
    )
    assert created.exit_code == 0
    project_id = json.loads(created.stdout)["id"]

    listed = json.loads(runner.invoke(cli_app, ["project", "list", "--json"]).stdout)
    assert [item["id"] for item in listed] == [project_id]

    shown = runner.invoke(cli_app, ["project", "show", project_id])
    assert "Through the CLI" in shown.stdout

    assert runner.invoke(cli_app, ["project", "archive", project_id]).exit_code == 0
    assert json.loads(runner.invoke(cli_app, ["project", "list", "--json"]).stdout) == []

    deleted = runner.invoke(cli_app, ["project", "delete", project_id, "--yes"])
    assert deleted.exit_code == 0
    assert "sources" in deleted.stdout, "the preview is printed even with --yes"


def test_cli_delete_answered_no_removes_nothing() -> None:
    import json

    runner.invoke(cli_app, ["db", "upgrade"])
    project_id = json.loads(runner.invoke(cli_app, ["project", "create", "Kept", "--json"]).stdout)[
        "id"
    ]
    result = runner.invoke(cli_app, ["project", "delete", project_id], input="n\n")
    assert result.exit_code == 0
    assert "Nothing was removed." in result.stdout
    assert runner.invoke(cli_app, ["project", "show", project_id]).exit_code == 0


def test_cli_show_of_a_missing_project_exits_one() -> None:
    runner.invoke(cli_app, ["db", "upgrade"])
    result = runner.invoke(cli_app, ["project", "show", "01ZZZZZZZZZZZZZZZZZZZZZZZZ"])
    assert result.exit_code == 1


def test_db_status_reports_head() -> None:
    import json

    runner.invoke(cli_app, ["db", "upgrade"])
    status = json.loads(runner.invoke(cli_app, ["db", "status", "--json"]).stdout)
    assert status["at_head"] is True
    assert status["current_revision"] == "0001"
