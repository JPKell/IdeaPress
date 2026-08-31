"""ideapress.domain.stages — the stage vocabulary, spelled once.

[Workflows §2](../../../docs/apps/ideapress/workflows.md) is the **only** list of stage identifiers
in the suite. `[models.stages]` configuration keys, the port's ``StageId``, the API's ``stage`` path
values and (from P7) the LoadCoach task map all draw from this module, and
:func:`check_stage_vocabulary` asserts the configuration and this table are one set at startup.

`fact_check` is why that check exists: it was bound in configuration and mapped to a LoadCoach task
profile while appearing in no stage list at all, and nothing noticed until the final architecture
audit read all three documents at once. A table in one module, with a test that reads the
documentation back, is the mechanical version of that audit.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Final, Literal, cast, get_args

__all__ = [
    "GATE_STAGES",
    "MODEL_STAGES",
    "NO_MODEL_STAGES",
    "STAGES",
    "StageDefinition",
    "check_table_matches_type",
    "StageId",
    "is_stage",
    "stage_definition",
]

StageId = Literal[
    "requirements",
    "research",
    "research_synthesis",
    "outline",
    "draft",
    "validate",
    "repair",
    "audit_fast",
    "audit_deep",
    "fact_check",
    "critique",
    "revise",
    "coverage",
    "commit",
    "project_review",
    "export",
]
"""Every stage identifier, in workflows §2's order. The type and the table below never diverge:
:data:`STAGES` is keyed by exactly these values and a test asserts both against the document."""


@dataclass(frozen=True, slots=True)
class StageDefinition:
    """One row of workflows §2.

    Attributes:
        stage: The identifier, as spelled in the documentation and in configuration.
        ordinal: The row's number in workflows §2, which is the pipeline order.
        uses_model: Whether this stage reaches a model. The five stages for which this is ``False``
            are the ones that decide whether work proceeds — that is the point of the split, not an
            incidental property (workflows §1, rule 1).
        gate: The gate the stage must pass, in the document's own words.
    """

    stage: StageId
    ordinal: int
    uses_model: bool
    gate: str


def _row(stage: StageId, ordinal: int, *, uses_model: bool, gate: str) -> StageDefinition:
    return StageDefinition(stage=stage, ordinal=ordinal, uses_model=uses_model, gate=gate)


STAGES: Final[dict[StageId, StageDefinition]] = {
    definition.stage: definition
    for definition in (
        _row(
            "requirements",
            1,
            uses_model=True,
            gate="Every requirement has an ID and a checkable statement",
        ),
        # "Optional" in workflows §2's Model? column is the *stage*, not the model: no research
        # backend ships at 1.0, so this stage reaches no model and takes no `[models.stages]`
        # binding. It is the fifth of the five no-model stages the section counts.
        _row("research", 2, uses_model=False, gate="Every note cites an available source"),
        _row("research_synthesis", 3, uses_model=True, gate="Structure valid; no uncited claim"),
        _row(
            "outline", 4, uses_model=True, gate="Every blocking requirement assigned to >= 1 unit"
        ),
        _row("draft", 5, uses_model=True, gate="Non-empty; length band; structure"),
        _row("validate", 6, uses_model=False, gate="Deterministic checks pass"),
        _row("repair", 7, uses_model=True, gate="Re-validated"),
        _row("audit_fast", 8, uses_model=True, gate="Runs to completion"),
        _row("audit_deep", 9, uses_model=True, gate="Runs to completion"),
        _row("fact_check", 10, uses_model=True, gate="Every unsupported claim becomes a finding"),
        _row("critique", 11, uses_model=True, gate="Runs to completion"),
        _row("revise", 12, uses_model=True, gate="Re-validated and re-audited"),
        _row("coverage", 13, uses_model=False, gate="Every blocking requirement satisfied"),
        _row("commit", 14, uses_model=False, gate="Atomic write"),
        _row("project_review", 15, uses_model=True, gate="Runs to completion"),
        _row("export", 16, uses_model=False, gate="Deterministic render"),
    )
}
"""Workflows §2, transcribed. Ordered by ordinal because dicts preserve insertion order and the
pipeline order is meaningful."""

_STAGE_NAMES: Final[frozenset[str]] = frozenset(STAGES)

MODEL_STAGES: Final[frozenset[str]] = frozenset(
    stage for stage, definition in STAGES.items() if definition.uses_model
)
"""The stages that need a `[models.stages]` binding: eleven of sixteen, exactly spec §12's keys."""

NO_MODEL_STAGES: Final[frozenset[str]] = frozenset(
    stage for stage, definition in STAGES.items() if not definition.uses_model
)
"""The five stages that involve no model at all. Four of them — ``validate``, ``coverage``,
``commit`` and ``export`` — are the gates that decide whether work proceeds; ``research`` is the
fifth, and reaches no model because no research backend ships at 1.0."""

GATE_STAGES: Final[frozenset[str]] = frozenset({"validate", "coverage", "commit", "export"})
"""The four deterministic gates. A model never runs in one, which is the whole mechanism behind
"Python owns the control flow" — not a property of the prompts, a property of the stage list."""


def is_stage(value: str) -> bool:
    """Whether ``value`` is a stage identifier.

    Args:
        value: A candidate identifier, typically from configuration or a URL path.

    Returns:
        ``True`` when ``value`` appears in workflows §2. Refuses anything else, including a
        near-miss like ``"audit"`` or ``"edit"`` — both of which a previous version of the
        configuration used and neither of which is a stage.
    """
    return value in STAGES


def stage_definition(stage: str) -> StageDefinition:
    """Return the definition for ``stage``.

    Args:
        stage: A stage identifier.

    Returns:
        Its row from workflows §2.

    Raises:
        KeyError: ``stage`` is not a stage identifier. Callers that hold a validated
            :data:`StageId` never see this; it exists so an unvalidated string cannot silently
            produce a default.
    """
    if stage not in _STAGE_NAMES:
        raise KeyError(stage)
    return STAGES[cast("StageId", stage)]


def check_table_matches_type(
    declared: Collection[str], table: Mapping[StageId, StageDefinition]
) -> None:
    """Fail if the stage type and the stage table have drifted apart.

    Args:
        declared: The identifiers :data:`StageId` names.
        table: The transcribed rows of workflows §2.

    Raises:
        RuntimeError: The two name different stages, or the ordinals are not ``1..n`` in order.
            Called at import with this module's own values, so an edit to either spelling that
            forgets the other fails immediately rather than at whichever call site happened to
            touch the missing stage — and it raises rather than asserting, because ``python -O``
            removes an assertion and this invariant is load-bearing.
    """
    if set(declared) != set(table):
        drifted = ", ".join(sorted(set(declared) ^ set(table)))
        message = f"StageId and STAGES disagree about: {drifted}"
        raise RuntimeError(message)
    ordinals = [definition.ordinal for definition in table.values()]
    if ordinals != list(range(1, len(table) + 1)):
        message = f"STAGES ordinals are not workflows §2's 1..{len(table)}: {ordinals}"
        raise RuntimeError(message)


check_table_matches_type(get_args(StageId), STAGES)
