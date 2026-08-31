"""The unit state machine (data model §3), as data and as a refusal.

Risk G1's mitigation is "the state machine as data", and the value of that is only realised if an
undocumented move actually fails. These tests enumerate the diagram in both directions: every arrow
that exists is permitted, and every pair that is not an arrow is refused.
"""

from __future__ import annotations

import itertools

import pytest
from baseaicore import ValidationError

from ideapress.domain.stage_state import (
    RUN_STATES,
    TERMINAL_RUN_STATES,
    TRANSITIONS,
    UNIT_STATES,
    assert_transition,
    can_transition,
    next_states,
)

# Data model §3's diagram, written out independently of TRANSITIONS so the table is compared
# against a second reading of the document rather than against itself.
DOCUMENTED_ARROWS = {
    ("planned", "drafting"),
    ("drafting", "validating"),
    ("drafting", "paused"),
    ("validating", "drafting"),
    ("validating", "paused"),
    ("validating", "auditing"),
    ("auditing", "revising"),
    ("auditing", "committed"),
    ("auditing", "paused"),
    ("revising", "validating"),
    ("revising", "paused"),
    ("paused", "drafting"),
    ("paused", "revising"),
    ("committed", "revising"),
}


def test_the_table_is_the_documented_diagram() -> None:
    encoded = {(source, target) for source, targets in TRANSITIONS.items() for target in targets}
    assert encoded == DOCUMENTED_ARROWS


def test_every_documented_arrow_is_permitted() -> None:
    for source, target in DOCUMENTED_ARROWS:
        assert can_transition(source, target), f"{source} -> {target}"
        assert_transition(source, target, unit_key="U-01")


def test_every_undocumented_pair_is_refused() -> None:
    for source, target in itertools.product(sorted(UNIT_STATES), repeat=2):
        if (source, target) in DOCUMENTED_ARROWS:
            continue
        assert not can_transition(source, target), f"{source} -> {target} should not be allowed"
        with pytest.raises(ValidationError):
            assert_transition(source, target, unit_key="U-01")


def test_a_refusal_names_the_unit_and_what_it_could_have_done() -> None:
    with pytest.raises(ValidationError) as caught:
        assert_transition("planned", "committed", unit_key="U-03")
    assert "U-03" in caught.value.message
    assert "drafting" in caught.value.message, "the message must say what *is* allowed"
    assert caught.value.details["from"] == "planned"
    assert caught.value.details["to"] == "committed"


def test_a_state_that_does_not_exist_is_refused_in_either_position() -> None:
    with pytest.raises(ValidationError) as caught:
        assert_transition("nonsense", "drafting", unit_key="U-01")
    assert "nonsense" in caught.value.message
    with pytest.raises(ValidationError):
        assert_transition("planned", "finished", unit_key="U-01")


def test_an_unknown_state_can_move_nowhere() -> None:
    assert next_states("nonsense") == frozenset()
    assert not can_transition("nonsense", "drafting")


def test_nothing_reaches_committed_except_through_auditing() -> None:
    """ "Nothing is ever committed to escape a loop" is this, mechanically."""
    into_committed = {source for source, target in DOCUMENTED_ARROWS if target == "committed"}
    assert into_committed == {"auditing"}


def test_paused_is_reachable_from_every_working_state() -> None:
    """`paused` is a first-class outcome, not a failure: every loop can land there."""
    for state in ("drafting", "validating", "auditing", "revising"):
        assert "paused" in next_states(state)


def test_a_paused_unit_moves_only_when_someone_resumes_it() -> None:
    assert next_states("paused") == {"drafting", "revising"}


def test_run_states_and_their_terminals() -> None:
    assert TERMINAL_RUN_STATES < RUN_STATES
    assert TERMINAL_RUN_STATES == {"completed", "failed", "cancelled", "interrupted"}
    assert RUN_STATES - TERMINAL_RUN_STATES == {"queued", "running"}
