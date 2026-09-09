"""ideapress.services.config_schema — the settings schema document (ADR-0127 rule 1).

WeightRoomGym renders a settings form for every application from one versioned JSON document
rather than hardcoding each application's key set — the drift the generated configuration
reference already exists to prevent (row WS3, ADR-0127). Every field of the document is read out
of objects that already exist: `Settings.model_json_schema()`, the runtime/security registry in
:mod:`ideapress.services.settings_registry`, `config_reference.sections()` (the same walk
`docs/configuration.md` is generated from) and `load_settings`'s per-leaf `sources` map. Nothing
here is a second copy of a key list.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ideapress.config import ENV_PREFIX, Settings, load_settings_tolerant
from ideapress.config_reference import sections
from ideapress.domain.stages import MODEL_STAGES
from ideapress.services.settings_registry import CONFIG_ONLY_KEYS, RUNTIME_KEYS

__all__ = ["build_schema_document"]


def _all_leaf_keys() -> list[str]:
    """Every dotted `section.field` path that is a scalar leaf, not a nested section.

    The same walk :func:`ideapress.config_reference.sections` drives the generated reference
    with, so a leaf cannot appear in one document and not the other.
    """
    leaves: list[str] = []
    for path, model in sections():
        for name, field in model.model_fields.items():
            annotation = field.annotation
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                continue
            leaves.append(f"{path}.{name}")
    return leaves


def _resolve_in_json_schema(json_schema: dict[str, Any], dotted: str) -> dict[str, Any]:
    """Walk ``dotted`` into ``json_schema``, following `$ref`/`$defs`, to the leaf's own schema."""
    node = json_schema
    for part in dotted.split("."):
        if "$ref" in node:
            node = json_schema["$defs"][node["$ref"].rsplit("/", 1)[-1]]
        node = node["properties"][part]
    if "$ref" in node:
        node = json_schema["$defs"][node["$ref"].rsplit("/", 1)[-1]]
    return node


def _runtime_entry(
    key: str, json_schema: dict[str, Any], *, fallback_description: str
) -> dict[str, Any]:
    """One `runtime_changeable` entry: `key`, `kind`, `minimum`, `maximum`, `description`.

    The one shape every application's document emits (ADR-0127 rule 1); `minimum`/`maximum` are
    always present, `None` where the field has no such bound. A choice field's permitted values
    live in `json_schema`'s own `enum`, not repeated here.
    """
    leaf = _resolve_in_json_schema(json_schema, key)
    return {
        "key": key,
        "kind": leaf.get("type", "string"),
        "minimum": leaf.get("minimum"),
        "maximum": leaf.get("maximum"),
        "description": leaf.get("description") or fallback_description,
    }


def build_schema_document(*, config_path: str | Path | None = None) -> dict[str, Any]:
    """Build the ADR-0127 rule 1 settings schema document.

    Args:
        config_path: An explicit configuration file to resolve `sources` and `problems` against.
            Defaults to the normal resolution order (see `resolve_config_path`).

    Returns:
        The document: `schema_version`, `application`, `version`, `env_prefix`, `config_path`,
        `json_schema`, `runtime_changeable`, `security_keys`, `config_only`, `sources` and
        `problems`. `problems` is never omitted; it is empty when the application's own file
        loads cleanly and otherwise names each unknown key, never dropping it (ADR-0127 rule 1).

    Raises:
        ConfigurationError: The application's own configuration has a problem other than an
            unknown key — the same refusals `config show` and `config validate` give, since a
            schema document over a genuinely unsafe configuration is not this command's job.
    """
    from ideapress.__about__ import __version__

    json_schema = Settings.model_json_schema()
    stage_keys = {stage: f"models.stages.{stage}" for stage in MODEL_STAGES}
    runtime_changeable = [
        _runtime_entry(key, json_schema, fallback_description="") for key in sorted(RUNTIME_KEYS)
    ] + [
        _runtime_entry(key, json_schema, fallback_description=f"Model bound to the {stage} stage.")
        for stage, key in sorted(stage_keys.items())
    ]
    security_keys = sorted(CONFIG_ONLY_KEYS)
    runtime_key_set = RUNTIME_KEYS | set(stage_keys.values())
    config_only = sorted(set(_all_leaf_keys()) - runtime_key_set - CONFIG_ONLY_KEYS)

    loaded, problems = load_settings_tolerant(config_path=config_path)

    return {
        "schema_version": "1.0",
        "application": "ideapress",
        "version": __version__,
        "env_prefix": ENV_PREFIX,
        "config_path": str(loaded.config_path),
        "json_schema": json_schema,
        "runtime_changeable": runtime_changeable,
        "security_keys": security_keys,
        "config_only": config_only,
        "sources": loaded.sources,
        "problems": problems,
    }
