"""ideapress.web.routes.system — `/health`, `/version` and `/system/status`.

Health reports three components (spec §17): ``database``, ``backend`` (naming which one and
whether it is reachable) and ``prompts``. An unreachable backend makes health *degraded*, never
unavailable and never a startup failure — spec §20 AC7 — because the application is fully useful
for opening projects and exporting existing content with no model anywhere.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from mirrorwall import ComponentHealth, ComponentStatus, health_payload, json_response

# Imported at runtime, not under TYPE_CHECKING: FastAPI reads a handler's return annotation
# when it builds the OpenAPI schema, and a forward reference it cannot resolve makes
# `app.openapi()` raise — which is a 500 on /api/v1/docs that no other test would notice.
from starlette.responses import HTMLResponse, JSONResponse

from ideapress.__about__ import __version__
from ideapress.web.rendering import render

__all__ = ["API_VERSION", "SCHEMA_VERSION", "router", "ui_router"]

router = APIRouter(tags=["system"])
ui_router = APIRouter(include_in_schema=False)

API_VERSION = "v1"
SCHEMA_VERSION = "1"


def _components(request: Request) -> list[ComponentHealth]:
    """Build the three health components from whatever the lifespan actually opened."""
    checkers = getattr(request.app.state, "health_checkers", None)
    if checkers is None:
        return [
            ComponentHealth(
                name="database",
                status=ComponentStatus.NOT_CONFIGURED,
                detail="The application is not serving; no handles are open.",
            )
        ]
    return [check() for check in checkers]


@router.get("/health")
def health(request: Request) -> JSONResponse:
    """Report component health.

    Returns:
        MirrorWall's standard health payload. ``200`` when every component is ``ok`` or
        ``degraded``, ``503`` when any is ``unavailable`` — a degraded backend is a working
        application with no model, which is a supported state, not an outage.
    """
    components = _components(request)
    payload = health_payload(application="ideapress", version=__version__, components=components)
    unavailable = any(c.status is ComponentStatus.UNAVAILABLE for c in components)
    return json_response(payload, status=503 if unavailable else 200)


@router.get("/version")
def version() -> JSONResponse:
    """Report application, API and schema versions.

    Never authenticated (ADR-0026 §5): a client has to be able to discover what it is talking to
    before it can present a credential.
    """
    payload: dict[str, Any] = {
        "application": "ideapress",
        "version": __version__,
        "api_version": API_VERSION,
        "schema_version": SCHEMA_VERSION,
    }
    return json_response(payload)


@router.get("/system/status")
def system_status(request: Request) -> JSONResponse:
    """Report active stage runs and the configured backend mode."""
    settings = request.app.state.settings
    payload: dict[str, Any] = {
        "backend_mode": settings.inference.mode,
        "fallback_mode": settings.inference.fallback_mode or None,
        "pinned": settings.inference.pin_backend,
        "max_concurrent_stages": settings.execution.max_concurrent_stages,
        "active_stage_runs": [],
    }
    return json_response(payload)


@ui_router.get("/system")
def system_page(request: Request) -> HTMLResponse:
    """Render the system page: versions, health components and the configured backend."""
    settings = request.app.state.settings
    components = _components(request)
    html = render(
        "system/index.html",
        page="system",
        page_title="System",
        health_rows=[(c.name, c.status.value, c.detail or "") for c in components],
        api_version=API_VERSION,
        schema_version=SCHEMA_VERSION,
        backend_mode=settings.inference.mode,
    )
    return HTMLResponse(html)
