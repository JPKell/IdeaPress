"""ideapress.services.plan_editing — reordering, splitting, merging and reassigning units.

The plan is the one artefact a person edits directly. Everything else in IdeaPress is produced by a
stage and revised by a stage; the plan is where human judgement about *structure* goes, which is
why P8 gives it an editor and why the editor is here rather than in a route handler.

**Every edit re-validates the whole plan, and an edit that orphans a blocking requirement is
refused by name.** That is the plan's named failure mode and the one property the editor exists to
guarantee: reassigning R-014 away from the only unit carrying it would leave nothing in the
finished work answerable for it, and the gate that would have caught it (`check_plan`, workflows §2
stage 4) runs at plan time — long after the edit. So it runs again here, before the write, and the
refusal names the requirement rather than saying "invalid".

**Committed work is never destroyed by a structural edit.** A unit that has a committed version
cannot be merged away or have its text discarded: workflows §9 says committed units are never
rolled back by a later failure, and a person clicking "merge" is not a licence to do what a failure
may not. Such an edit is refused with the units named, and the person can revise or delete
deliberately instead.

Nothing here consults a model. Reordering, splitting and merging are structural operations Python
performs on data a person supplied; workflows §11 forbids a model modifying the plan at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from baseaicore import ValidationError

from ideapress.domain.plan import Plan, PlanUnit, check_plan

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ideapress.domain.requirements import Requirement
    from ideapress.services.runtime import Runtime

__all__ = [
    "PlanEdit",
    "apply_edit",
    "merge_units",
    "renumber",
    "reassign_requirements",
    "reorder_units",
    "set_unit_goal",
    "split_unit",
]

logger = logging.getLogger(__name__)

EDITABLE_STATES = frozenset({"planned", "paused"})
"""The unit states a structural edit may touch.

`committed` is excluded because a merge or a split would discard finished work, and the in-flight
states (`drafting`, `validating`, `auditing`, `revising`) because another process is writing to
them — the M7-29 shape again: an operation that decides something about work in flight must know
who owns it.
"""


@dataclass(frozen=True, slots=True)
class PlanEdit:
    """One structural edit, named so the UI and the CLI describe it the same way.

    Attributes:
        operation: ``reorder``, ``split``, ``merge``, ``reassign`` or ``goal``.
        unit_keys: The units the operation applies to, in the order it needs them.
        requirement_keys: The requirements, for ``reassign``.
        text: The new goal, for ``goal``; the split point's title, for ``split``.
        position: The target position, for ``reorder``.
    """

    operation: str
    unit_keys: tuple[str, ...] = ()
    requirement_keys: tuple[str, ...] = ()
    text: str = ""
    position: int | None = None


def renumber(units: Sequence[PlanUnit]) -> tuple[PlanUnit, ...]:
    """Return ``units`` with dense ordinals and keys matching their new positions.

    Args:
        units: The units in their intended order.

    Returns:
        The same units with ``ordinal`` running 1..n and ``key`` re-derived as ``U-01``, ``U-02``…

    Keys are positional by construction (data model §3), so a reorder that left them alone would
    produce a plan whose ``U-03`` sits second — readable to nobody. The cost is that a key is not a
    stable identity across an edit, which is why every caller here works from the *edited plan*
    rather than holding keys across the operation.
    """
    return tuple(
        replace(unit, key=f"U-{index:02d}", ordinal=index)
        for index, unit in enumerate(units, start=1)
    )


def reorder_units(plan: Plan, *, unit_key: str, position: int) -> Plan:
    """Move one unit to a new position.

    Args:
        plan: The current plan.
        unit_key: The unit to move.
        position: Its new 1-based position.

    Returns:
        The plan with the unit moved and every unit renumbered.

    Raises:
        ValidationError: ``unit_key`` names no unit, or ``position`` is outside 1..n. A silent
            clamp would move the unit somewhere the person did not ask for and report success.
    """
    units = list(plan.units)
    index = _index_of(units, unit_key)
    if not 1 <= position <= len(units):
        message = (
            f"Position {position} is outside the plan, which has {len(units)} unit(s). "
            f"Choose a position between 1 and {len(units)}."
        )
        raise ValidationError(message, details={"position": position, "unit_count": len(units)})
    moved = units.pop(index)
    units.insert(position - 1, moved)
    return replace(plan, units=renumber(units))


def split_unit(plan: Plan, *, unit_key: str, title: str, requirement_keys: Sequence[str]) -> Plan:
    """Split one unit into two, dividing its requirements between them.

    Args:
        plan: The current plan.
        unit_key: The unit to split.
        title: The title of the new second unit.
        requirement_keys: The requirements that move to the new unit. The rest stay.

    Returns:
        The plan with one more unit, renumbered.

    Raises:
        ValidationError: The unit does not exist; the title is blank; a named requirement is not
            on the unit being split; or **every** requirement would move, which is a rename wearing
            a split's clothes and leaves an empty unit behind.
    """
    units = list(plan.units)
    index = _index_of(units, unit_key)
    original = units[index]
    if not title.strip():
        message = "A new unit needs a title; a split that produces an unnamed unit is not usable."
        raise ValidationError(message, details={"unit_key": unit_key})

    moving = tuple(requirement_keys)
    stranger = sorted(set(moving) - set(original.requirement_keys))
    if stranger:
        message = (
            f"{', '.join(stranger)} is not assigned to {unit_key}, so a split cannot move it. "
            "Reassign it to this unit first, or split a different unit."
        )
        raise ValidationError(message, details={"unit_key": unit_key, "unknown": stranger})

    remaining = tuple(key for key in original.requirement_keys if key not in set(moving))
    if original.requirement_keys and not remaining:
        message = (
            f"Splitting {unit_key} this way moves every one of its requirements to the new unit, "
            "leaving the original responsible for nothing. Rename the unit instead, or move fewer "
            "requirements."
        )
        raise ValidationError(message, details={"unit_key": unit_key})

    words = original.target_words
    halved = max(1, words // 2) if words else None
    units[index] = replace(original, requirement_keys=remaining, target_words=halved)
    units.insert(
        index + 1,
        PlanUnit(
            key="U-00",  # replaced by renumber; never written with this value
            ordinal=0,
            title=title.strip(),
            goal_text=original.goal_text,
            requirement_keys=moving,
            target_words=halved,
        ),
    )
    return replace(plan, units=renumber(units))


def merge_units(plan: Plan, *, unit_keys: Sequence[str], title: str = "") -> Plan:
    """Merge two or more adjacent-or-not units into the first of them.

    Args:
        plan: The current plan.
        unit_keys: The units to merge; the first is kept and the others fold into it.
        title: A new title for the merged unit, or empty to keep the first one's.

    Returns:
        The plan with the units merged and renumbered. The merged unit carries the union of their
        requirements, in first-seen order, so nothing is orphaned by the merge itself.

    Raises:
        ValidationError: Fewer than two units were named, or one of them does not exist.
    """
    if len(set(unit_keys)) < 2:
        message = "A merge needs at least two distinct units."
        raise ValidationError(message, details={"unit_keys": list(unit_keys)})

    units = list(plan.units)
    keeping = _index_of(units, unit_keys[0])
    folding = [_index_of(units, key) for key in unit_keys[1:]]

    merged_requirements: list[str] = list(units[keeping].requirement_keys)
    merged_words = units[keeping].target_words or 0
    for index in folding:
        for key in units[index].requirement_keys:
            if key not in merged_requirements:
                merged_requirements.append(key)
        merged_words += units[index].target_words or 0

    units[keeping] = replace(
        units[keeping],
        title=title.strip() or units[keeping].title,
        requirement_keys=tuple(merged_requirements),
        target_words=merged_words or None,
    )
    for index in sorted(folding, reverse=True):
        units.pop(index)
    return replace(plan, units=renumber(units))


def reassign_requirements(plan: Plan, *, unit_key: str, requirement_keys: Sequence[str]) -> Plan:
    """Replace one unit's requirement assignment wholesale.

    Args:
        plan: The current plan.
        unit_key: The unit to reassign.
        requirement_keys: Exactly the requirements it should now carry.

    Returns:
        The plan with that unit's assignment replaced. Ordinals and keys are untouched: a
        reassignment changes responsibility, not structure.

    Raises:
        ValidationError: The unit does not exist. Whether the *result* is a legal plan is
            :func:`apply_edit`'s question, not this function's — this one performs the edit and the
            caller validates it, so a caller assembling several edits validates once at the end.
    """
    units = list(plan.units)
    index = _index_of(units, unit_key)
    units[index] = replace(units[index], requirement_keys=tuple(dict.fromkeys(requirement_keys)))
    return replace(plan, units=tuple(units))


def set_unit_goal(plan: Plan, *, unit_key: str, goal_text: str) -> Plan:
    """Rewrite one unit's goal.

    Args:
        plan: The current plan.
        unit_key: The unit whose goal to set.
        goal_text: The new goal.

    Returns:
        The plan with the goal replaced.

    Raises:
        ValidationError: The unit does not exist, or the goal is blank. A unit whose goal is empty
            gives the draft stage nothing to write toward.
    """
    if not goal_text.strip():
        message = (
            f"{unit_key} needs a goal; the draft stage has nothing to write toward without one."
        )
        raise ValidationError(message, details={"unit_key": unit_key})
    units = list(plan.units)
    index = _index_of(units, unit_key)
    units[index] = replace(units[index], goal_text=goal_text.strip())
    return replace(plan, units=tuple(units))


def apply_edit(
    runtime: Runtime,
    *,
    project_id: str,
    edit: PlanEdit,
) -> Plan:
    """Apply one edit to a project's stored plan, re-validate, and persist it.

    Args:
        runtime: The process's handles.
        project_id: The project whose plan to edit.
        edit: What to do.

    Returns:
        The new plan, as stored.

    Raises:
        ValidationError: The edit is malformed, it would orphan a blocking requirement, or it would
            destroy committed work. **The refusal names the requirement or the unit**, because a
            person told only "invalid" cannot act, and this is the one place a structural mistake
            can silently cost a guarantee.
        ProjectNotFound: No such project.

    The order is deliberate: build the edited plan in memory, refuse it if it is not a legal plan,
    and only then write. A plan that fails `check_plan` never reaches the database, so there is no
    window in which a project's stored plan is one nothing in the finished work is answerable for.
    """
    from ideapress.services.plan import load_plan, load_requirements

    with runtime.storage.read() as session:
        current = load_plan(session, project_id)
        requirements = load_requirements(session, project_id)

    edited = _edited(current, edit)

    if edit.operation in {"split", "merge", "reorder"}:
        _refuse_to_disturb_finished_work(runtime, project_id=project_id, plan=current, edit=edit)

    # The gate that would otherwise not run again until the next plan stage — long after this edit
    # took a guarantee away. It names every unassigned blocking requirement in its message.
    check_plan(edited, requirements)

    _persist(runtime, project_id=project_id, plan=edited, requirements=requirements)
    logger.info(
        "plan.edited",
        extra={
            "project_id": project_id,
            "operation": edit.operation,
            "unit_count": len(edited.units),
        },
    )
    return edited


def _edited(plan: Plan, edit: PlanEdit) -> Plan:
    """Dispatch one edit onto the plan.

    Raises:
        ValidationError: The operation is not one this editor performs.
    """
    if edit.operation == "reorder":
        return reorder_units(plan, unit_key=edit.unit_keys[0], position=edit.position or 1)
    if edit.operation == "split":
        return split_unit(
            plan,
            unit_key=edit.unit_keys[0],
            title=edit.text,
            requirement_keys=edit.requirement_keys,
        )
    if edit.operation == "merge":
        return merge_units(plan, unit_keys=edit.unit_keys, title=edit.text)
    if edit.operation == "reassign":
        return reassign_requirements(
            plan, unit_key=edit.unit_keys[0], requirement_keys=edit.requirement_keys
        )
    if edit.operation == "goal":
        return set_unit_goal(plan, unit_key=edit.unit_keys[0], goal_text=edit.text)
    message = (
        f"{edit.operation!r} is not a plan edit. Known operations: reorder, split, merge, "
        "reassign, goal."
    )
    raise ValidationError(message, details={"operation": edit.operation})


def _refuse_to_disturb_finished_work(
    runtime: Runtime, *, project_id: str, plan: Plan, edit: PlanEdit
) -> None:
    """Refuse a structural edit that would renumber or absorb a unit holding committed text.

    Raises:
        ValidationError: One of the affected units is committed or in flight. Workflows §9 says a
            later failure never rolls back a committed unit; a person pressing "merge" is not a
            licence to do what a failure may not, and a unit another process is writing to is not
            this operation's to move.
    """
    from sqlalchemy import select

    from ideapress.infrastructure.db.models import Unit as UnitRow

    # A reorder renumbers everything after the moved unit, so its blast radius is the whole plan;
    # a split or a merge touches the named units and everything after them.
    with runtime.storage.read() as session:
        protected = sorted(
            key
            for key, state in session.execute(
                select(UnitRow.unit_key, UnitRow.state).where(UnitRow.project_id == project_id)
            ).all()
            if state not in EDITABLE_STATES
        )
    if not protected:
        return
    message = (
        f"{', '.join(protected)} {'is' if len(protected) == 1 else 'are'} not in a state a "
        f"structural edit may touch: a {edit.operation} renumbers units, and renumbering a unit "
        "that already holds committed text would separate that text from the plan that describes "
        "it. Finished work is never rolled back by a later change (workflows §9). Reassign "
        "requirements or edit goals instead, which change no unit's identity."
    )
    raise ValidationError(
        message,
        details={
            "operation": edit.operation,
            "protected_unit_keys": protected,
            "editable_states": sorted(EDITABLE_STATES),
        },
    )


def _persist(
    runtime: Runtime, *, project_id: str, plan: Plan, requirements: Sequence[Requirement]
) -> None:
    """Write the edited plan back, updating unit rows in place and deleting the ones it removed.

    Args:
        runtime: The process's handles.
        project_id: The project being edited.
        plan: The validated plan to store.
        requirements: The compiled requirements, unchanged by any edit — they are compiled once and
            carried through (workflows §3), and an editor that rewrote them would be a model-free
            path to the thing workflows §11 forbids a model doing.

    Rows are updated in place rather than deleted and recreated, so a unit's identity, its state and
    anything referring to it survive a reorder. `store_plan` recreates because it is replacing a
    plan wholesale; this is amending one.
    """
    from sqlalchemy import select

    from ideapress.infrastructure.db.models import Unit as UnitRow

    with runtime.storage.write() as session:
        existing = {
            unit_row.unit_key: unit_row
            for unit_row in session.execute(
                select(UnitRow).where(UnitRow.project_id == project_id)
            ).scalars()
        }
        keeping = {unit.key for unit in plan.units}
        for key, unit_row in existing.items():
            if key not in keeping:
                session.delete(unit_row)

        for unit in plan.units:
            row = existing.get(unit.key)
            if row is None:
                row = UnitRow(
                    project_id=project_id,
                    unit_key=unit.key,
                    ordinal=unit.ordinal,
                    state="planned",
                )
                session.add(row)
            row.ordinal = unit.ordinal
            row.title = unit.title
            row.goal_text = unit.goal_text
            row.requirement_keys_json = list(unit.requirement_keys)
            row.target_words = unit.target_words


def _index_of(units: Sequence[PlanUnit], unit_key: str) -> int:
    """The position of ``unit_key`` in ``units``.

    Raises:
        ValidationError: No unit has that key. Names the keys that do exist, because the usual
            cause is a stale page whose keys were renumbered by somebody else's edit.
    """
    for index, unit in enumerate(units):
        if unit.key == unit_key:
            return index
    message = (
        f"{unit_key} is not a unit in this plan. The plan has: "
        f"{', '.join(unit.key for unit in units) or 'no units'}. If you were looking at an older "
        "version of this page, reload it — an edit renumbers units."
    )
    raise ValidationError(message, details={"unit_key": unit_key})
