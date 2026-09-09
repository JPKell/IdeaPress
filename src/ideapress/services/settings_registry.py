"""ideapress.services.settings_registry — which settings a running process may change.

Api.md §6 draws the line: `inference.mode`, the stage model bindings and the workflow limits are
runtime-changeable while bind address, exposure, `server.allowed_hosts`, tokens, the database URL
and `providers.allow_remote` are configuration-only. This module is the one place that line is
drawn in code — the web route (`PUT /settings`) and `config schema` both read it, so neither can
drift from the other (ADR-0127 rule 1, "no duplicated key lists").
"""

from __future__ import annotations

from typing import Final

from ideapress.domain.stages import MODEL_STAGES

__all__ = ["CONFIG_ONLY_KEYS", "RUNTIME_KEYS", "is_runtime_key"]

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


def is_runtime_key(key: str) -> bool:
    """Whether ``key`` may be changed on a running process.

    Args:
        key: A dotted settings path.

    Returns:
        ``True`` for a key in :data:`RUNTIME_KEYS`, or ``models.stages.<stage>`` for a stage in
        :data:`ideapress.domain.stages.MODEL_STAGES`.
    """
    if key in RUNTIME_KEYS:
        return True
    return key.startswith("models.stages.") and key.split(".", 2)[2] in MODEL_STAGES
