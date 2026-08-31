"""The plan gate, and the ways a model tries to satisfy it by saying nothing.

Workflows §2 stage 4 states one rule — every blocking requirement assigned to at least one unit —
and the interesting failures are all around its edges: empty sets that make it vacuously true, and
invented keys that look like coverage.
"""

from __future__ import annotations

import pytest
from baseaicore import ValidationError

from ideapress.domain.plan import Plan, PlanUnit, check_plan, unit_key
from ideapress.domain.requirements import CompiledBy, Requirement, SourceReference

COMPILED_BY = CompiledBy(prompt_id="stages.requirements.compile", version="1.0.0")


def _requirement(key: str, *, blocking: bool = True, text: str = "") -> Requirement:
    return Requirement(
        key=key,
        text=text or f"The work must satisfy {key}.",
        blocking=blocking,
        source=SourceReference(document="brief", quote="a quotation long enough to be evidence"),
        compiled_by=COMPILED_BY,
    )


def _plan(*assignments: tuple[str, ...]) -> Plan:
    units = tuple(
        PlanUnit(
            key=unit_key(index),
            ordinal=index,
            title=f"Unit {index}",
            goal_text="A goal.",
            requirement_keys=keys,
        )
        for index, keys in enumerate(assignments, start=1)
    )
    return Plan(units=units, requirement_keys=tuple(k for keys in assignments for k in keys))


def test_a_plan_covering_every_blocking_requirement_passes() -> None:
    requirements = [
        _requirement("R-001"),
        _requirement("R-002"),
        _requirement("R-003", blocking=False),
    ]
    check_plan(_plan(("R-001",), ("R-002",)), requirements)


def test_an_unassigned_blocking_requirement_fails_and_is_named() -> None:
    """A gate that fails without saying which requirement failed is one nobody can act on."""
    requirements = [
        _requirement("R-001"),
        _requirement("R-002", text="The article must state that inference runs locally."),
    ]
    with pytest.raises(ValidationError) as caught:
        check_plan(_plan(("R-001",)), requirements)
    assert "R-002" in caught.value.message
    assert "inference runs locally" in caught.value.message
    assert caught.value.details["unassigned_requirement_keys"] == ["R-002"]


def test_every_unassigned_requirement_is_named_not_just_the_first() -> None:
    requirements = [_requirement(f"R-{n:03d}") for n in range(1, 5)]
    with pytest.raises(ValidationError) as caught:
        check_plan(_plan(("R-001",)), requirements)
    assert caught.value.details["unassigned_requirement_keys"] == ["R-002", "R-003", "R-004"]


def test_an_advisory_requirement_need_not_be_assigned() -> None:
    """Risk T4: treating advisory findings as blocking 'because it is stricter' is the trap."""
    requirements = [_requirement("R-001"), _requirement("R-002", blocking=False)]
    check_plan(_plan(("R-001",)), requirements)


def test_no_requirements_at_all_does_not_satisfy_the_gate() -> None:
    """P3 AC2: a model that returns "looks good, no requirements needed" does not pass.

    An empty requirement set makes "every blocking requirement is assigned" vacuously true. This
    is the exact shape of a gate satisfied by saying nothing, and it is refused by name.
    """
    with pytest.raises(ValidationError) as caught:
        check_plan(_plan(()), [])
    assert "No requirements were compiled" in caught.value.message


def test_an_empty_plan_does_not_satisfy_the_gate() -> None:
    with pytest.raises(ValidationError) as caught:
        check_plan(Plan(units=(), requirement_keys=()), [_requirement("R-001")])
    assert "no units" in caught.value.message


def test_a_unit_citing_a_requirement_that_does_not_exist_is_refused() -> None:
    """An invented key looks like coverage and is not."""
    with pytest.raises(ValidationError) as caught:
        check_plan(_plan(("R-001", "R-042")), [_requirement("R-001")])
    assert "R-042" in caught.value.message


def test_one_requirement_may_be_carried_by_several_units() -> None:
    check_plan(_plan(("R-001",), ("R-001",)), [_requirement("R-001")])


def test_units_for_reports_every_carrier() -> None:
    plan = _plan(("R-001",), ("R-001", "R-002"))
    assert [unit.key for unit in plan.units_for("R-001")] == ["U-01", "U-02"]
    assert [unit.key for unit in plan.units_for("R-002")] == ["U-02"]


def test_unit_keys_are_generated_not_taken() -> None:
    assert unit_key(1) == "U-01"
    assert unit_key(12) == "U-12"


def test_plan_unit_lookup_by_key() -> None:
    plan = _plan(("R-001",), ("R-002",))
    assert plan.unit("U-02").title == "Unit 2"
    with pytest.raises(KeyError):
        plan.unit("U-99")
