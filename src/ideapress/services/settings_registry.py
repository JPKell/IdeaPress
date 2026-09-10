"""ideapress.services.settings_registry — which settings a running process may change.

Api.md §6 draws the line: the stage model bindings and the workflow limits are
runtime-changeable while bind address, exposure, `server.allowed_hosts`, tokens, the database URL
and `providers.allow_remote` are configuration-only. This module is the one place that line is
drawn in code — the web route (`PUT /settings`) and `config schema` both read it, so neither can
drift from the other (ADR-0127 rule 1, "no duplicated key lists").
"""

from __future__ import annotations

from typing import Final

from ideapress.domain.stages import MODEL_STAGES

__all__ = ["ALL_RUNTIME_KEYS", "CONFIG_ONLY_KEYS", "RUNTIME_KEYS", "is_runtime_key"]

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
        "workflow.max_revision_rounds",
        "workflow.diminishing_returns_threshold",
        "workflow.max_attempts_per_stage",
        "workflow.audit_escalation_threshold",
        "workflow.require_clean_validation_to_commit",
        "workflow.context_budget_tokens",
    }
)
"""Changeable while the process runs. Stage model bindings are `models.stages.<stage>`, derived
from the stage vocabulary rather than listed here.

Every key here is read by a stage, which is what lets a stored value take effect at the next stage
start. `inference.mode` and `logging.level` left the set at row WI1: the backend is built and
logging configured once per process, and a key nothing re-reads is not runtime-changeable
(ADR-0100 rule 1) — in the file, WeightRoomGym shows the restart it needs (ADR-0127 rule 5)."""

ALL_RUNTIME_KEYS: Final[frozenset[str]] = RUNTIME_KEYS | frozenset(
    f"models.stages.{stage}" for stage in MODEL_STAGES
)
""":data:`RUNTIME_KEYS` plus one `models.stages.<stage>` per model-using stage."""


def is_runtime_key(key: str) -> bool:
    """Whether ``key`` may be changed on a running process.

    Args:
        key: A dotted settings path.

    Returns:
        ``True`` for a key in :data:`RUNTIME_KEYS`, or ``models.stages.<stage>`` for a stage in
        :data:`ideapress.domain.stages.MODEL_STAGES`.
    """
    return key in ALL_RUNTIME_KEYS
