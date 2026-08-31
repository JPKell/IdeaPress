"""ideapress.domain.stage_state — the unit state machine, as data.

Data model §3's diagram, transcribed into a transition table. Risk G1 is workflow complexity
outgrowing comprehension — the prior project's orchestrator reached 2 103 lines — and the mitigation
named there is "the state machine as data". So it is a mapping, not a chain of ``if`` statements,
and an illegal transition raises rather than being silently permitted.

``paused`` is a **first-class outcome, not a failure**: the unit is intact, its findings are
visible, and the user decides what to do. Nothing is ever committed to escape a loop.
"""

from __future__ import annotations

from typing import Final, Literal, cast, get_args

from baseaicore import ValidationError

__all__ = [
    "RUN_STATES",
    "TERMINAL_RUN_STATES",
    "TRANSITIONS",
    "UNIT_STATES",
    "RunState",
    "UnitState",
    "assert_transition",
    "can_transition",
    "next_states",
]

UnitState = Literal[
    "planned", "drafting", "validating", "auditing", "revising", "paused", "committed"
]
UNIT_STATES: Final[frozenset[str]] = frozenset(get_args(UnitState))

RunState = Literal["queued", "running", "completed", "failed", "cancelled", "interrupted"]
RUN_STATES: Final[frozenset[str]] = frozenset(get_args(RunState))

TERMINAL_RUN_STATES: Final[frozenset[str]] = frozenset(
    {"completed", "failed", "cancelled", "interrupted"}
)
"""A run in one of these is over. An SSE stream closes when it sees one, and a `--resume` reads
`interrupted` as "pick this up" rather than "this failed"."""

TRANSITIONS: Final[dict[UnitState, frozenset[UnitState]]] = {
    "planned": frozenset({"drafting"}),
    # validating -> drafting is the repair loop; -> paused is its exhaustion.
    "drafting": frozenset({"validating", "paused"}),
    "validating": frozenset({"drafting", "auditing", "paused"}),
    # auditing -> revising covers both "materially deficient" and "blocking requirement uncovered".
    "auditing": frozenset({"revising", "committed", "paused"}),
    "revising": frozenset({"validating", "paused"}),
    # A paused unit moves only when a person resumes it.
    "paused": frozenset({"drafting", "revising"}),
    # A committed unit is immutable; an explicit user revision creates a new version.
    "committed": frozenset({"revising"}),
}
"""Data model §3, as data. Every arrow in the diagram and no others."""


def can_transition(current: str, target: str) -> bool:
    """Whether a unit may move from ``current`` to ``target``.

    Takes plain strings because the caller often holds a state read out of the database, where it
    is text. An unknown state has no arrows, so it can move nowhere — which is the safe answer.
    """
    return target in _arrows(current)


def _arrows(current: str) -> frozenset[str]:
    """Every legal target from ``current``; empty for a state that does not exist."""
    if current not in UNIT_STATES:
        return frozenset()
    return frozenset(TRANSITIONS[cast("UnitState", current)])


def next_states(current: str) -> frozenset[str]:
    """Every state a unit in ``current`` may legally move to."""
    return _arrows(current)


def assert_transition(current: str, target: str, *, unit_key: str) -> None:
    """Refuse an illegal transition, naming what was attempted.

    Args:
        current: The unit's state now.
        target: Where the caller wants it.
        unit_key: Which unit, for the message.

    Raises:
        ValidationError: ``current`` is not a state, ``target`` is not a state, or the arrow does
            not exist in data model §3. Raising rather than permitting is the point: a state
            machine that quietly allows an undocumented move is a state machine that documents
            nothing, and this is the mechanism that keeps "nothing is committed to escape a loop"
            true rather than aspirational.
    """
    if current not in UNIT_STATES:
        message = f"{current!r} is not a unit state. States: {', '.join(sorted(UNIT_STATES))}."
        raise ValidationError(message, details={"unit_key": unit_key, "state": current})
    if target not in UNIT_STATES:
        message = f"{target!r} is not a unit state. States: {', '.join(sorted(UNIT_STATES))}."
        raise ValidationError(message, details={"unit_key": unit_key, "state": target})
    if not can_transition(current, target):
        allowed = ", ".join(sorted(next_states(current))) or "nothing"
        message = (
            f"Unit {unit_key} cannot move from {current!r} to {target!r}. From {current!r} it may "
            f"move to: {allowed}."
        )
        raise ValidationError(
            message, details={"unit_key": unit_key, "from": current, "to": target}
        )
