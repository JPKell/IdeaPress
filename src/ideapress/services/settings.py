"""ideapress.services.settings — the runtime-changeable settings: resolved, stored and documented.

Api.md §6. Configuration is loaded once at startup through the precedence chain (configuration
standards §1). The keys :mod:`ideapress.services.settings_registry` names may also be changed
through ``PUT /settings``, and those live in the ``settings`` table. ``GET`` and ``PUT`` answer the
document LoadCoach and PromptCadence answer — ``settings``, ``definitions`` and ``config_only``
(ADR-0100) — because WeightRoomGym's Settings page reads that one shape from every application
(ADR-0127 rule 4), and IdeaPress conforms rather than teaching it a second one (row WI1).

**Precedence follows configuration standards §7**: ``defaults → file → database → env → CLI``. A
stored row is ignored while the environment pins its key, and the document says so per key —
``stored``, ``source`` and ``shadowed_by`` — so a row that does nothing is visible rather than
applied or dropped in silence. ``ideapress serve`` applies its flags as environment variables
before the loader runs, so the environment check covers the CLI layer too.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, ClassVar

from baseaicore import SuiteError
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select
from weightsdb import upsert

from ideapress.config import ENV_PREFIX
from ideapress.domain.stages import MODEL_STAGES
from ideapress.errors import ValidationFailed
from ideapress.infrastructure.db.models import Setting
from ideapress.services.config_schema import runtime_changeable_entries
from ideapress.services.settings_registry import (
    ALL_RUNTIME_KEYS,
    CONFIG_ONLY_KEYS,
    RUNTIME_KEYS,
    is_runtime_key,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from pydantic import BaseModel

    from ideapress.config import Settings
    from ideapress.services.database import Database

__all__ = [
    "SettingConfigOnly",
    "configured_value",
    "read_runtime_settings",
    "runtime_settings_document",
    "shadowing_source",
    "write_runtime_settings",
]


class SettingConfigOnly(SuiteError):
    """A configuration-only key was sent to ``PUT /settings``: ``403 FORBIDDEN``, naming it."""

    code: ClassVar[str] = "FORBIDDEN"


def _leaf(settings: Settings, key: str) -> tuple[BaseModel, str]:
    """The model holding ``key``'s field, and the field's name."""
    *sections, field = key.split(".")
    parent: BaseModel = settings
    for name in sections:
        parent = getattr(parent, name)
    return parent, field


def configured_value(settings: Settings, key: str) -> Any:
    """``key``'s value in ``settings``.

    Args:
        settings: The configured settings — the file/environment/CLI layers as loaded.
        key: A dotted runtime-changeable path, ``models.stages.<stage>`` included.

    Returns:
        The value, as the model holds it.
    """
    parent, field = _leaf(settings, key)
    return getattr(parent, field)


def _coerce(settings: Settings, key: str, value: object) -> Any:
    """Validate ``value`` for ``key`` through the field's own model, and return it as held there.

    The field's pydantic validation is the bound — the one a value in ``config.toml`` meets — so
    ``PUT /settings`` cannot store a value the file could not hold.

    Raises:
        ValidationFailed: The field refuses ``value``, naming the key and pydantic's reason.
    """
    parent, field = _leaf(settings, key)
    try:
        checked = type(parent).model_validate({**parent.model_dump(), field: value})
    except PydanticValidationError as exc:
        problem = "; ".join(str(error["msg"]) for error in exc.errors())
        message = f"{key} cannot be {value!r}: {problem}."
        raise ValidationFailed(
            message, details={"fields": [{"path": key, "problem": problem}]}
        ) from exc
    return getattr(checked, field)


def shadowing_source(key: str) -> str | None:
    """The environment variable pinning ``key``, or ``None`` when nothing shadows a stored row.

    Configuration standards §7 puts the database *between* file and environment, so a key set in
    the environment beats a stored row. The variable is the one the loader reads —
    ``IDEAPRESS_WORKFLOW__MAX_REVISION_ROUNDS``, ``IDEAPRESS_MODELS__STAGES__DRAFT``. There is no
    separate CLI layer to consult: ``ideapress serve`` sets its flags as environment variables
    before the loader runs.

    Args:
        key: A dotted runtime-changeable path.

    Returns:
        ``"env IDEAPRESS_…"``, naming the variable, or ``None``.
    """
    name = ENV_PREFIX + "__".join(part.upper() for part in key.split("."))
    return f"env {name}" if name in os.environ else None


def _stored(database: Database) -> dict[str, Any]:
    """Every stored row belonging to the registry, keyed by dotted path.

    A row for a key this build does not register is left in the table and ignored.
    """
    with database.read() as session:
        rows = session.execute(
            select(Setting.key, Setting.value_json).where(Setting.key.in_(sorted(ALL_RUNTIME_KEYS)))
        ).all()
    return {str(key): value for key, value in rows}


def _resolve(
    database: Database, settings: Settings
) -> tuple[dict[str, Any], dict[str, Any], frozenset[str]]:
    """``(effective, stored, decided_by_row)`` for every runtime-changeable key, sorted by key."""
    stored = _stored(database)
    effective: dict[str, Any] = {}
    decided_by_row: set[str] = set()
    for key in sorted(ALL_RUNTIME_KEYS):
        if key in stored and shadowing_source(key) is None:
            try:
                effective[key] = _coerce(settings, key, stored[key])
            except ValidationFailed:
                pass  # a row this build cannot read falls back to configuration
            else:
                decided_by_row.add(key)
                continue
        effective[key] = configured_value(settings, key)
    return effective, stored, frozenset(decided_by_row)


def read_runtime_settings(database: Database, *, settings: Settings) -> dict[str, Any]:
    """Every runtime-changeable key's effective value.

    The stored row wins unless the environment pins the key (:func:`shadowing_source`) or the row
    is one this build cannot read — a value the field now refuses falls back to configuration
    rather than raising, because a row written by another version must not stop this one working.

    Args:
        database: The application's database handle.
        settings: The **configured** settings — the file/environment/CLI layers as loaded, never a
            copy stored values were already applied to.

    Returns:
        ``key -> value`` for every runtime-changeable key, sorted by key.
    """
    return _resolve(database, settings)[0]


def write_runtime_settings(
    database: Database,
    changes: Mapping[str, Any],
    *,
    settings: Settings,
    now: datetime,
) -> dict[str, Any]:
    """Validate and store ``changes``, returning every effective value afterwards.

    Args:
        database: The application's database handle.
        changes: ``key -> value``, flat, as ``PUT /settings`` receives it.
        settings: The configured settings.
        now: The instant recorded on each row.

    Returns:
        What :func:`read_runtime_settings` returns — which differs from what was written when the
        environment shadows a key the caller stored. The row is kept either way: unsetting the
        variable makes it effective.

    Raises:
        SettingConfigOnly: A configuration-only key (``403``), naming every one sent.
        ValidationFailed: A key that is not a runtime setting, or a value its field refuses, each
            named. **Nothing is written when any key is refused**: a partial update would leave
            the caller unable to say what took effect, and a caller who mistyped one key of six
            should not have the other five applied.
    """
    refused = sorted(key for key in changes if key in CONFIG_ONLY_KEYS)
    if refused:
        message = (
            f"{', '.join(refused)} cannot be changed while the process is running. These decide "
            "where the service listens and where your content goes; changing them is a "
            "configuration edit and a restart."
        )
        raise SettingConfigOnly(message, details={"config_only": refused})
    unknown = sorted(key for key in changes if not is_runtime_key(key))
    if unknown:
        message = (
            f"{', '.join(unknown)} is not a runtime setting. Runtime settings are: "
            f"{', '.join(sorted(RUNTIME_KEYS))}, and models.stages.<stage> for "
            f"{', '.join(sorted(MODEL_STAGES))}."
        )
        raise ValidationFailed(message, details={"unknown": unknown})
    validated = {key: _coerce(settings, key, value) for key, value in sorted(changes.items())}
    if validated:
        with database.write() as session:
            for key, value in validated.items():
                upsert(
                    session,
                    Setting,
                    {"key": key, "value_json": value, "updated_at": now},
                    index_elements=["key"],
                )
    return read_runtime_settings(database, settings=settings)


def runtime_settings_document(database: Database, *, settings: Settings) -> dict[str, Any]:
    """The body ``GET`` and ``PUT /settings`` answer: what is effective, why, and what is refused.

    Every key carries its stored row *and* whether that row is what decides the value: a row the
    environment shadows does nothing, and a document that showed it as the value would be the lie
    configuration standards §7's precedence exists to prevent.

    Args:
        database: The application's database handle.
        settings: The configured settings.

    Returns:
        ``settings`` (effective values), ``definitions`` (per key: ``type``, ``description``,
        ``minimum`` and ``maximum`` — the entry ``config schema`` publishes — ``configured``, the
        ``stored`` row or ``None``, ``source`` — ``"database"`` when a row decides the value, else
        ``"configuration"`` — and ``shadowed_by``, the variable beating a stored row), and
        ``config_only`` (the keys refused by name).
    """
    effective, stored, decided_by_row = _resolve(database, settings)
    entries = {entry["key"]: entry for entry in runtime_changeable_entries()}
    definitions: dict[str, Any] = {}
    for key in effective:
        entry = entries[key]
        definitions[key] = {
            "type": entry["kind"],
            "description": entry["description"],
            "minimum": entry["minimum"],
            "maximum": entry["maximum"],
            "configured": configured_value(settings, key),
            "stored": stored.get(key),
            "source": "database" if key in decided_by_row else "configuration",
            "shadowed_by": shadowing_source(key) if key in stored else None,
        }
    return {
        "settings": effective,
        "definitions": definitions,
        "config_only": sorted(CONFIG_ONLY_KEYS),
    }
