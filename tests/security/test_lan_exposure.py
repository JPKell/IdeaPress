"""ADR-0026 on a **non-loopback** bind — M7-31.

M7 proved the hardening on loopback, where `127.0.0.1` is in the allowlist by construction and the
`Host` check has an easy job. That is the configuration nobody is attacked in. This suite binds to
a LAN address, where the allowlist is the *only* thing standing between a DNS-rebinding page and
the user's private work, and asserts the same properties there.

The four ADR-0026 properties, on the bind that matters:

1. a non-loopback bind with no `allowed_hosts` **refuses to start** — a configuration refusal, not
   a runtime one, so it cannot be discovered in production;
2. `Host` is validated **before routing**, so a path that does not exist is still a 421;
3. CSRF is enforced on every form route;
4. remote-routed work is labelled as egress.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from mirrorwall import CSRF_COOKIE_NAME, CSRF_FIELD_NAME

from ideapress.config import InsecureBindingError, Settings, load_settings
from ideapress.services.runtime import build_runtime
from ideapress.web.app import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator

LAN_HOST = "192.168.1.5"
LAN_NAME = "ideapress.local"
LAN_PORT = 8767


def _lan_settings(**server: object) -> Settings:
    """Settings bound to a LAN address with a named allowlist."""
    settings = load_settings().settings.model_copy(deep=True)
    settings.server.host = LAN_HOST
    settings.server.allowed_hosts = (LAN_NAME,)
    for key, value in server.items():
        setattr(settings.server, key, value)
    return settings


@pytest.fixture
def lan_client() -> Iterator[TestClient]:
    app = create_app(_lan_settings(), runtime_builder=build_runtime)
    with TestClient(app, base_url=f"http://{LAN_NAME}:{LAN_PORT}") as client:
        yield client


# ------------------------------------------------------------------ 1. it refuses to start


def test_a_non_loopback_bind_with_no_allowed_hosts_refuses_to_start(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0026 §1. A configuration refusal, before anything opens a socket.

    This is the difference between a mistake caught at start-up and a service quietly reachable
    from any page the user visits. It is asserted through `load_settings` rather than through the
    model, because the refusal must survive the whole configuration pipeline.
    """
    from pathlib import Path

    config = Path(str(tmp_path)) / "config.toml"
    config.write_text('[server]\nhost = "192.168.1.5"\n', encoding="utf-8")
    with pytest.raises(InsecureBindingError, match="allowed_hosts"):
        load_settings(config_path=config)


def test_binding_to_every_interface_needs_an_acknowledgement_too(
    tmp_path: object,
) -> None:
    """`0.0.0.0` is the same exposure with a different spelling."""
    from pathlib import Path

    config = Path(str(tmp_path)) / "config.toml"
    config.write_text(
        '[server]\nhost = "0.0.0.0"\nallowed_hosts = ["box.local"]\n', encoding="utf-8"
    )
    with pytest.raises(InsecureBindingError):
        load_settings(config_path=config)


def test_a_lan_bind_with_an_allowlist_starts(tmp_path: object) -> None:
    """The other half: the refusal is about the *missing* allowlist, not about LAN binds."""
    from pathlib import Path

    config = Path(str(tmp_path)) / "config.toml"
    config.write_text(
        '[server]\nhost = "192.168.1.5"\nallowed_hosts = ["ideapress.local"]\n', encoding="utf-8"
    )
    loaded = load_settings(config_path=config)
    assert loaded.settings.server.allowed_hosts == ("ideapress.local",)


# ------------------------------------------------------------------ 2. Host before routing


def test_an_unexpected_host_is_refused_on_a_lan_bind(lan_client: TestClient) -> None:
    """The DNS-rebinding case, on the bind where it is reachable."""
    response = lan_client.get("/api/v1/version", headers={"host": "attacker.example.com"})
    assert response.status_code == 421
    assert response.json()["error"]["code"] == "MISDIRECTED_REQUEST"


@pytest.mark.parametrize(
    "path",
    ["/api/v1/version", "/api/v1/health", "/", "/projects", "/no-such-path-at-all"],
)
def test_host_validation_precedes_routing_on_a_lan_bind(path: str, lan_client: TestClient) -> None:
    """The no-route-path-421 property: a path that does not exist is still 421, never 404.

    That is the observable difference between validating the Host *before* a router is consulted
    and validating it inside a handler — and it is the whole reason ADR-0026 puts the check in
    middleware ordered ahead of everything.
    """
    response = lan_client.get(path, headers={"host": "attacker.example.com"})
    assert response.status_code == 421, path


def test_the_bind_address_itself_is_always_accepted(lan_client: TestClient) -> None:
    """The address the server is bound to is in its own allowlist; requiring it to be listed twice
    would be a configuration trap with no security value."""
    assert (
        lan_client.get("/api/v1/version", headers={"host": f"{LAN_HOST}:{LAN_PORT}"}).status_code
        == 200
    )


def test_a_configured_name_is_accepted(lan_client: TestClient) -> None:
    assert lan_client.get("/api/v1/version").status_code == 200


def test_a_host_with_the_right_name_and_a_different_port_is_still_accepted(
    lan_client: TestClient,
) -> None:
    """A reverse proxy in front of this rewrites the port; refusing on it would break the standard
    non-loopback deployment while stopping nothing."""
    assert (
        lan_client.get("/api/v1/version", headers={"host": f"{LAN_NAME}:9999"}).status_code == 200
    )


def test_a_subdomain_of_an_allowed_host_is_not_allowed(lan_client: TestClient) -> None:
    """`evil.ideapress.local` is a different host, and a prefix or suffix match would accept it."""
    response = lan_client.get("/api/v1/version", headers={"host": f"evil.{LAN_NAME}"})
    assert response.status_code == 421


# ------------------------------------------------------------------ 3. CSRF on every form route


def _form_routes() -> list[tuple[str, str]]:
    """Every POST the UI exposes, enumerated from the routers rather than listed by hand."""
    from ideapress.web.routes import backends, plan, projects, settings, system, units, workspace

    found: list[tuple[str, str]] = []
    for module in (projects, plan, workspace, units, backends, system, settings):
        router = getattr(module, "ui_router", None)
        if router is None:
            continue
        for route in router.routes:
            methods: set[str] = getattr(route, "methods", set())
            if "POST" in methods:
                found.append((module.__name__.rsplit(".", 1)[-1], str(getattr(route, "path", ""))))
    return found


def test_every_ui_form_route_is_csrf_protected(lan_client: TestClient) -> None:
    """ADR-0026 §2, over every form route the application exposes — enumerated, not remembered.

    A post with no token must be refused on **every** one of them. A route added later without a
    token is caught by this the day it appears, which is the difference between a checklist and a
    guard.
    """
    unprotected = []
    for _module, path in _form_routes():
        url = path.replace("{project_id}", "01PROJECT").replace("{unit_key}", "U-01")
        response = lan_client.post(url, data={"title": "x"}, follow_redirects=False)
        # 404/422 mean the route rejected the fake ids before CSRF; only a 2xx/3xx is a failure of
        # the guard, because that is a form post that was *acted on* without a token.
        if response.status_code < 400:
            unprotected.append(f"{path} -> {response.status_code}")
    assert unprotected == [], f"form routes acting on an untokened post: {unprotected}"


def test_there_is_at_least_one_form_route_to_protect() -> None:
    """The guard on the guard: an empty enumeration would make the test above vacuous."""
    assert len(_form_routes()) >= 3, _form_routes()


def test_a_valid_token_succeeds_on_a_lan_bind(lan_client: TestClient) -> None:
    """The other half of CSRF: the honest path still works behind the check."""
    page = lan_client.get("/")
    token = page.headers["set-cookie"].split(f"{CSRF_COOKIE_NAME}=", 1)[1].split(";", 1)[0]
    response = lan_client.post(
        "/projects",
        data={CSRF_FIELD_NAME: token, "title": "From the LAN", "brief": "A brief."},
        headers={"Cookie": f"{CSRF_COOKIE_NAME}={token}"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text


def test_the_csrf_cookie_keeps_its_flags_on_a_lan_bind(lan_client: TestClient) -> None:
    """`__Host-` requires Secure, and a LAN deployment terminates TLS in front (ADR-0026 §1). The
    flags do not relax because the bind changed."""
    cookie = lan_client.get("/").headers["set-cookie"]
    assert cookie.startswith("__Host-mw-csrf=")
    assert "Secure" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie.replace("SameSite=Strict", "SameSite=strict")


# ------------------------------------------------------------------ 4. egress labelling


def test_a_remote_backend_is_labelled_as_egress() -> None:
    """Risk S4. On a LAN bind especially, the user is told where their content goes."""
    from ideapress.services.backends import describe_backends

    settings = _lan_settings()
    settings.inference.mode = "openai_compatible"
    settings.inference.openai_compatible.base_url = "https://api.example.com/v1"
    described = describe_backends(settings)
    assert described[0]["is_remote"] is True
    assert described[0]["egress"] is True


def test_a_loopback_backend_is_not_labelled_as_egress() -> None:
    from ideapress.services.backends import describe_backends

    described = describe_backends(_lan_settings())
    assert described[0]["egress"] is False


def test_the_workspace_says_where_work_goes_on_a_lan_bind() -> None:
    """The badge is on the workspace, not only the backends page: the workspace is where somebody
    presses the button that sends their draft somewhere."""
    from ideapress.services.workspace import _backend_facts  # noqa: PLC2701 — the unit under test

    settings = _lan_settings()
    settings.inference.mode = "openai_compatible"
    settings.inference.openai_compatible.base_url = "https://api.example.com/v1"
    runtime = build_runtime(settings)
    try:
        facts = _backend_facts(runtime)
        assert facts["egress"] is True
        assert facts["mode"] == "openai_compatible"
    finally:
        runtime.close()
