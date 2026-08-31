"""ideapress.web.routes.export — rendering a committed project, and writing it to disk."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request
from mirrorwall import json_response

# Imported at runtime, not under TYPE_CHECKING: FastAPI reads a handler's return annotation
# when it builds the OpenAPI schema, and a forward reference it cannot resolve makes
# `app.openapi()` raise — which is a 500 on /api/v1/docs that no other test would notice.
from starlette.responses import JSONResponse, PlainTextResponse, Response

from ideapress.services.export import FORMATS, build_document, export_project, render

__all__ = ["router"]

router = APIRouter(tags=["export"])

_MEDIA_TYPES = {
    "markdown": "text/markdown; charset=utf-8",
    "html": "text/html; charset=utf-8",
    "json": "application/json",
}


@router.get("/projects/{project_id}/export")
def get_export(
    request: Request,
    project_id: str,
    fmt: Annotated[str, Query(alias="format")] = "markdown",
) -> Response:
    """Render the committed project and return it, without writing anything.

    Raises:
        ExportFailed: The format is not one of the three shipped at 1.0.
    """
    document = build_document(request.app.state.runtime, project_id=project_id)
    text = render(document, fmt)
    return PlainTextResponse(text, media_type=_MEDIA_TYPES.get(fmt, "text/plain; charset=utf-8"))


@router.post("/projects/{project_id}/export")
def post_export(
    request: Request,
    project_id: str,
    fmt: Annotated[str, Query(alias="format")] = "markdown",
) -> JSONResponse:
    """Write the export into the project's directory and return the artifact record."""
    return json_response(export_project(request.app.state.runtime, project_id=project_id, fmt=fmt))


@router.get("/export/formats")
def list_formats() -> JSONResponse:
    """The export formats this build ships, with their file extensions."""
    return json_response(
        {"formats": [{"format": k, "extension": v} for k, v in sorted(FORMATS.items())]}
    )
