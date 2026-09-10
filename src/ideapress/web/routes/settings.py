"""ideapress.web.routes.settings — ``GET``/``PUT /settings`` (api.md §6).

Runtime-changeable keys only, in the document LoadCoach and PromptCadence answer and WeightRoomGym's
Settings page reads (ADR-0127 rule 4): ``PUT`` takes a flat ``{key: value}`` object, and both verbs
answer ``settings``, ``definitions`` and ``config_only``. A configuration-only key — the bind
address, exposure, ``server.allowed_hosts``, the database URL, ``providers.allow_remote`` — is
``403 FORBIDDEN`` naming it; an unknown key or a refused value is ``422`` naming it; a request
naming either writes nothing. The rules live in :mod:`ideapress.services.settings`.

The refusal is the point. Those keys decide where the service listens and where content goes, and a
running process that could change them over HTTP would be a running process that could be talked
into exposing itself.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Request
from mirrorwall import json_response
from starlette.responses import JSONResponse

from ideapress.services.settings import runtime_settings_document, write_runtime_settings

__all__ = ["router"]

router = APIRouter(tags=["settings"])


@router.get("/settings")
def get_settings(request: Request) -> JSONResponse:
    """Every runtime-changeable key's effective value, its definition, and the config-only keys.

    Answered over the **configured** settings, so ``configured`` stays what the operator configured
    and ``source`` says whether a stored row or configuration decides each value.
    """
    runtime = request.app.state.runtime
    return json_response(
        runtime_settings_document(runtime.storage, settings=request.app.state.settings)
    )


@router.put("/settings")
def put_settings(request: Request, body: Annotated[dict[str, Any], Body()]) -> JSONResponse:
    """Store one or more runtime-changeable keys, and answer with the whole document.

    Raises:
        SettingConfigOnly: A configuration-only key, named (``403``). Nothing is written.
        ValidationFailed: An unknown key, or a value its field refuses, named (``422``). Nothing is
            written.
    """
    runtime = request.app.state.runtime
    settings = request.app.state.settings
    write_runtime_settings(runtime.storage, body, settings=settings, now=datetime.now(UTC))
    return json_response(runtime_settings_document(runtime.storage, settings=settings))
