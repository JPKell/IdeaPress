"""ideapress.web.routes.units — a unit's content, its provenance and its history.

Model-produced text reaches the templates as a plain string and is escaped once by the shared
macros. There is no ``| safe`` in this package on anything a model wrote (risk S1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from mirrorwall import json_response
from starlette.responses import HTMLResponse

from ideapress.web.rendering import render

if TYPE_CHECKING:
    from starlette.responses import JSONResponse

__all__ = ["router", "ui_router"]

router = APIRouter(tags=["units"])
ui_router = APIRouter(include_in_schema=False)


def _reports(request: Request) -> Any:
    from ideapress.services import unit_reports

    return unit_reports


@router.get("/projects/{project_id}/units")
def list_units(request: Request, project_id: str) -> JSONResponse:
    """Unit list with state, version, requirement coverage and the last validation."""
    return json_response(
        {"units": _reports(request).unit_list(request.app.state.runtime, project_id=project_id)}
    )


@router.get("/projects/{project_id}/units/{unit_key}")
def get_unit(request: Request, project_id: str, unit_key: str) -> JSONResponse:
    """One unit's current content plus its full provenance."""
    return json_response(
        _reports(request).unit_detail(
            request.app.state.runtime, project_id=project_id, unit_key=unit_key
        )
    )


@router.get("/projects/{project_id}/units/{unit_key}/history")
def get_unit_history(request: Request, project_id: str, unit_key: str) -> JSONResponse:
    """Every version with its coverage."""
    from ideapress.services.units import unit_history

    with request.app.state.runtime.storage.read() as session:
        return json_response({"versions": unit_history(session, project_id, unit_key)})


@ui_router.get("/projects/{project_id}/units/{unit_key}")
def unit_page(request: Request, project_id: str, unit_key: str) -> HTMLResponse:
    """Render a unit: its content, validation report, coverage and history."""
    detail = _reports(request).unit_detail(
        request.app.state.runtime, project_id=project_id, unit_key=unit_key
    )
    return HTMLResponse(render("units/detail.html", page="projects", page_title=unit_key, **detail))
