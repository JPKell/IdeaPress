"""ideapress.web.routes.backends — `GET /backends` and `POST /backends/test`.

The backend page states plainly where content goes: a remote endpoint carries an egress flag, per
backend, because risk S4 is the user's private work leaving the machine without their noticing.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from mirrorwall import json_response
from pydantic import BaseModel, ConfigDict, Field

# Imported at runtime, not under TYPE_CHECKING: FastAPI reads a handler's return annotation
# when it builds the OpenAPI schema, and a forward reference it cannot resolve makes
# `app.openapi()` raise — which is a 500 on /api/v1/docs that no other test would notice.
from starlette.responses import HTMLResponse, JSONResponse

from ideapress.services.backends import describe_backends, test_backend
from ideapress.web.rendering import render

__all__ = ["router", "ui_router"]

router = APIRouter(tags=["backends"])
ui_router = APIRouter(include_in_schema=False)


class BackendTestRequest(BaseModel):
    """``POST /backends/test`` body."""

    model_config = ConfigDict(extra="forbid")

    mode: str | None = Field(default=None, description="Which backend; the configured one if none.")


@router.get("/backends")
def list_backends(request: Request) -> JSONResponse:
    """List the configured backends with reachability, capabilities and an egress flag."""
    return json_response({"backends": describe_backends(request.app.state.settings)})


@router.post("/backends/test")
def run_backend_test(request: Request, body: BackendTestRequest) -> JSONResponse:
    """Round-trip a backend and report latency, its model list and any version mismatch."""
    return json_response(test_backend(request.app.state.settings, mode=body.mode))


@ui_router.get("/backends")
def backends_page(request: Request) -> HTMLResponse:
    """Render the backend page, stating where content goes for each configured backend."""
    return HTMLResponse(
        render(
            "backends/index.html",
            page="backends",
            page_title="Backends",
            backends=describe_backends(request.app.state.settings),
        )
    )
