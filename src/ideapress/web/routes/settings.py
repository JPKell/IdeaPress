"""ideapress.web.routes.settings — the settings a running process may change, and those it may not.

Api.md §6 draws the line: `inference.mode`, the stage model bindings and the workflow limits are
runtime-changeable; **bind address, exposure, `server.allowed_hosts`, tokens, the database URL and
`providers.allow_remote` are configuration-only and return 403 naming the key.**

The refusal is the point. Those six decide where the service listens and where content goes, and a
running process that could change them over HTTP would be a running process that could be talked
into exposing itself.
"""

from __future__ import annotations

from typing import Any, Final

from fastapi import APIRouter, Request
from mirrorwall import json_response
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse

from ideapress.errors import ValidationFailed
from ideapress.infrastructure.db.models import Setting as SettingRow

__all__ = ["CONFIG_ONLY_KEYS", "RUNTIME_KEYS", "router"]

router = APIRouter(tags=["settings"])

CONFIG_ONLY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "server.host",
        "server.port",
        "server.allow_lan_exposure",
        "server.allowed_hosts",
        "storage.database_url",
        "providers.allow_remote",
    }
)
"""Refused over HTTP, always, with the key named (api.md §6)."""

RUNTIME_KEYS: Final[frozenset[str]] = frozenset(
    {
        "inference.mode",
        "workflow.max_revision_rounds",
        "workflow.diminishing_returns_threshold",
        "workflow.max_attempts_per_stage",
        "workflow.audit_escalation_threshold",
        "workflow.require_clean_validation_to_commit",
        "workflow.context_budget_tokens",
        "logging.level",
    }
)
"""Changeable while the process runs. Stage model bindings are `models.stages.<stage>`, checked
against the stage vocabulary rather than listed here."""


class SettingsUpdate(BaseModel):
    """``PUT /settings`` body: dotted keys to new values."""

    model_config = ConfigDict(extra="forbid")

    values: dict[str, Any] = Field(default_factory=dict)


def _is_runtime_key(key: str) -> bool:
    from ideapress.domain.stages import MODEL_STAGES

    if key in RUNTIME_KEYS:
        return True
    return key.startswith("models.stages.") and key.split(".", 2)[2] in MODEL_STAGES


@router.get("/settings")
def get_settings(request: Request) -> JSONResponse:
    """Report the settings, marking which may be changed while the process runs."""
    from sqlalchemy import select

    settings = request.app.state.settings
    runtime = request.app.state.runtime
    overrides: dict[str, Any] = {}
    if runtime is not None and runtime.database is not None:
        with runtime.storage.read() as session:
            overrides = {
                row.key: row.value_json for row in session.scalars(select(SettingRow)).all()
            }

    effective: dict[str, Any] = {
        "inference.mode": settings.inference.mode,
        "logging.level": settings.logging.level,
    }
    for name in (
        "max_revision_rounds",
        "diminishing_returns_threshold",
        "max_attempts_per_stage",
        "audit_escalation_threshold",
        "require_clean_validation_to_commit",
        "context_budget_tokens",
    ):
        effective[f"workflow.{name}"] = getattr(settings.workflow, name)
    for stage, binding in settings.models.stages.model_dump().items():
        effective[f"models.stages.{stage}"] = binding

    return json_response(
        {
            "runtime_changeable": dict(sorted(effective.items())),
            "overrides": dict(sorted(overrides.items())),
            "config_only": sorted(CONFIG_ONLY_KEYS),
        }
    )


@router.put("/settings")
def put_settings(request: Request, body: SettingsUpdate) -> JSONResponse:
    """Change runtime settings.

    Raises:
        ValidationFailed: A key is configuration-only, or is not a setting at all. **Nothing is
            written when any key is refused**: a partial update would leave the caller unable to
            say what took effect, and a caller who mistyped one key of six should not have the
            other five applied.
    """
    from ideapress.domain.stages import MODEL_STAGES

    refused = sorted(key for key in body.values if key in CONFIG_ONLY_KEYS)
    if refused:
        message = (
            f"{', '.join(refused)} cannot be changed while the process is running. These decide "
            "where the service listens and where your content goes; changing them is a "
            "configuration edit and a restart."
        )
        raise ValidationFailed(message, details={"config_only": refused, "status": 403})
    unknown = sorted(key for key in body.values if not _is_runtime_key(key))
    if unknown:
        message = (
            f"{', '.join(unknown)} is not a runtime setting. Runtime settings are: "
            f"{', '.join(sorted(RUNTIME_KEYS))}, and models.stages.<stage> for "
            f"{', '.join(sorted(MODEL_STAGES))}."
        )
        raise ValidationFailed(message, details={"unknown": unknown})

    runtime = request.app.state.runtime
    from weightsdb import upsert

    with runtime.storage.write() as session:
        for key, value in sorted(body.values.items()):
            upsert(
                session,
                SettingRow,
                {"key": key, "value_json": value},
                index_elements=["key"],
            )
    return json_response({"updated": sorted(body.values), "count": len(body.values)})
