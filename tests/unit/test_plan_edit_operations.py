"""The plan editor's pure operations, tested without a database.

`apply_edit` is the transactional wrapper and `tests/e2e/test_plan_editing.py` exercises it over
HTTP; these are the structural functions underneath, where the arithmetic of renumbering and the
edge cases of splitting live.
"""

from __future__ import annotations

import pytest
from baseaicore import ValidationError

from ideapress.domain.plan import Plan, PlanUnit
from ideapress.services.plan_editing import (
    merge_units,
    reassign_requirements,
    renumber,
    reorder_units,
    set_unit_goal,
    split_unit,
)


def _plan() -> Plan:
    return Plan(
        units=(
            PlanUnit(
                key="U-01",
                ordinal=1,
                title="First",
                goal_text="a",
                requirement_keys=("R-001",),
                target_words=100,
            ),
            PlanUnit(
                key="U-02",
                ordinal=2,
                title="Second",
                goal_text="b",
                requirement_keys=("R-002",),
                target_words=200,
            ),
            PlanUnit(
                key="U-03",
                ordinal=3,
                title="Third",
                goal_text="c",
                requirement_keys=("R-003",),
                target_words=300,
            ),
        ),
        requirement_keys=("R-001", "R-002", "R-003"),
        workflow_id="article",
        workflow_version="1.0.0",
    )


def test_renumber_makes_keys_match_positions() -> None:
    """Keys are positional by construction, so a reorder that left them alone would produce a plan
    whose `U-03` sits second — readable to nobody."""
    shuffled = (_plan().units[2], _plan().units[0], _plan().units[1])
    renumbered = renumber(shuffled)
    assert [unit.key for unit in renumbered] == ["U-01", "U-02", "U-03"]
    assert [unit.ordinal for unit in renumbered] == [1, 2, 3]
    assert [unit.title for unit in renumbered] == ["Third", "First", "Second"]


def test_reorder_moves_a_unit_to_the_front() -> None:
    moved = reorder_units(_plan(), unit_key="U-03", position=1)
    assert [unit.title for unit in moved.units] == ["Third", "First", "Second"]


def test_reorder_to_the_same_position_is_a_no_op() -> None:
    moved = reorder_units(_plan(), unit_key="U-01", position=1)
    assert [unit.title for unit in moved.units] == ["First", "Second", "Third"]


@pytest.mark.parametrize("position", [0, 4, -1])
def test_reorder_refuses_a_position_outside_the_plan(position: int) -> None:
    with pytest.raises(ValidationError, match="outside the plan"):
        reorder_units(_plan(), unit_key="U-01", position=position)


def test_reorder_refuses_a_unit_that_does_not_exist_and_lists_the_ones_that_do() -> None:
    with pytest.raises(ValidationError, match="U-01, U-02, U-03"):
        reorder_units(_plan(), unit_key="U-99", position=1)


def test_split_divides_the_requirements_and_halves_the_target() -> None:
    plan = reassign_requirements(_plan(), unit_key="U-01", requirement_keys=["R-001", "R-002"])
    split = split_unit(plan, unit_key="U-01", title="New", requirement_keys=["R-002"])
    assert len(split.units) == 4
    assert split.units[0].requirement_keys == ("R-001",)
    assert split.units[1].requirement_keys == ("R-002",)
    assert split.units[1].title == "New"
    assert split.units[0].target_words == 50


def test_split_refuses_a_blank_title() -> None:
    with pytest.raises(ValidationError, match="needs a title"):
        split_unit(_plan(), unit_key="U-01", title="   ", requirement_keys=[])


def test_split_refuses_moving_a_requirement_the_unit_does_not_carry() -> None:
    with pytest.raises(ValidationError, match="not assigned to U-01"):
        split_unit(_plan(), unit_key="U-01", title="New", requirement_keys=["R-002"])


def test_split_refuses_to_empty_the_original() -> None:
    """A rename wearing a split's clothes, which leaves a unit responsible for nothing."""
    with pytest.raises(ValidationError, match="responsible for nothing"):
        split_unit(_plan(), unit_key="U-01", title="New", requirement_keys=["R-001"])


def test_merge_unions_the_requirements_and_sums_the_targets() -> None:
    merged = merge_units(_plan(), unit_keys=["U-01", "U-03"])
    assert len(merged.units) == 2
    assert merged.units[0].requirement_keys == ("R-001", "R-003")
    assert merged.units[0].target_words == 400


def test_merge_keeps_the_first_units_title_unless_given_one() -> None:
    assert merge_units(_plan(), unit_keys=["U-01", "U-02"]).units[0].title == "First"
    assert merge_units(_plan(), unit_keys=["U-01", "U-02"], title="Both").units[0].title == "Both"


def test_merge_does_not_duplicate_a_shared_requirement() -> None:
    plan = reassign_requirements(_plan(), unit_key="U-02", requirement_keys=["R-001"])
    merged = merge_units(plan, unit_keys=["U-01", "U-02"])
    assert merged.units[0].requirement_keys == ("R-001",)


@pytest.mark.parametrize("keys", [["U-01"], [], ["U-01", "U-01"]])
def test_merge_refuses_fewer_than_two_distinct_units(keys: list[str]) -> None:
    with pytest.raises(ValidationError, match="at least two"):
        merge_units(_plan(), unit_keys=keys)


def test_reassign_replaces_the_assignment_and_keeps_the_structure() -> None:
    reassigned = reassign_requirements(
        _plan(), unit_key="U-02", requirement_keys=["R-001", "R-003"]
    )
    assert reassigned.units[1].requirement_keys == ("R-001", "R-003")
    assert [unit.key for unit in reassigned.units] == ["U-01", "U-02", "U-03"]


def test_reassign_removes_duplicates_while_keeping_order() -> None:
    reassigned = reassign_requirements(
        _plan(), unit_key="U-01", requirement_keys=["R-002", "R-001", "R-002"]
    )
    assert reassigned.units[0].requirement_keys == ("R-002", "R-001")


def test_setting_a_goal_trims_it() -> None:
    assert set_unit_goal(_plan(), unit_key="U-01", goal_text="  new  ").units[0].goal_text == "new"


def test_a_blank_goal_is_refused() -> None:
    with pytest.raises(ValidationError, match="needs a goal"):
        set_unit_goal(_plan(), unit_key="U-01", goal_text="\n\t ")


def test_no_operation_mutates_the_plan_it_was_given() -> None:
    """Value objects, and a plan a caller still holds must not change under them."""
    original = _plan()
    reorder_units(original, unit_key="U-03", position=1)
    merge_units(original, unit_keys=["U-01", "U-02"])
    set_unit_goal(original, unit_key="U-01", goal_text="changed")
    assert [unit.key for unit in original.units] == ["U-01", "U-02", "U-03"]
    assert original.units[0].goal_text == "a"
