"""Golden fixtures for domain.context_assembly, captured against the implementation
before J2 (docs/history/prompts/j2-ideapress-cutctx.prompt.md) replaced its reduction with a
CutCtx `DropOldestPolicy` chain behind the unchanged `assemble_context` seam.

Generated once by running the *old* implementation and hardcoding its exact output —
`render()` byte-for-byte, `dropped`, and the raised error's `details` — before any
behaviour moved (Gate A). These values are never regenerated: a case that needs editing
here is a stop, not a rebase (the row's whole claim is golden parity), and the git history
of this file is the proof they were not written to fit the new implementation.
"""

from __future__ import annotations

import pytest

from ideapress.domain.context_assembly import assemble_context
from ideapress.domain.plan import PlanUnit
from ideapress.domain.requirements import (
    CompiledBy,
    Requirement,
    RequirementCheck,
    SourceReference,
)
from ideapress.errors import ContextLimitExceeded

COMPILED_BY = CompiledBy(prompt_id="stages.requirements.compile", version="1.0.0")

UNIT = PlanUnit(
    key="U-02",
    ordinal=2,
    title="Where the work happens",
    goal_text="Say plainly where inference runs.",
    requirement_keys=("R-001",),
    target_words=400,
)
REQUIREMENT = Requirement(
    key="R-001",
    text="The unit must be explicit about where inference happens.",
    blocking=True,
    source=SourceReference(document="brief", quote="a quotation long enough to be evidence"),
    compiled_by=COMPILED_BY,
    checks=(RequirementCheck(kind="must_contain_any", values=("own machine",)),),
)

ORDINALS = {"U-01": 1, "U-02": 2, "U-03": 3, "U-07": 7}
NEIGHBOURS = {
    "U-01": "The previous section. The previous section. The previous section. The previous section. The previous section. The previous section. The previous section. The previous section. The previous section. The previous section. ",
    "U-03": "The next section. The next section. The next section. The next section. The next section. The next section. The next section. The next section. The next section. The next section. ",
    "U-07": "A far-off section. A far-off section. A far-off section. A far-off section. A far-off section. A far-off section. A far-off section. A far-off section. A far-off section. A far-off section. ",
}
NOTES = [
    (
        "General background",
        "A note nothing references. A note nothing references. A note nothing references. A note nothing references. A note nothing references. A note nothing references. A note nothing references. A note nothing references. A note nothing references. A note nothing references. ",
    ),
    (
        "Where the work happens",
        "A note the unit's own title names. A note the unit's own title names. A note the unit's own title names. A note the unit's own title names. A note the unit's own title names. A note the unit's own title names. A note the unit's own title names. A note the unit's own title names. A note the unit's own title names. A note the unit's own title names. ",
    ),
]
NOTE_LARGE = (
    "Where the work happens",
    "A large referenced note. A large referenced note. A large referenced note. A large referenced note. A large referenced note. A large referenced note. A large referenced note. A large referenced note. A large referenced note. A large referenced note. A large referenced note. A large referenced note. A large referenced note. A large referenced note. A large referenced note. A large referenced note. A large referenced note. A large referenced note. A large referenced note. A large referenced note. A large referenced note. A large referenced note. A large referenced note. A large referenced note. A large referenced note. A large referenced note. A large referenced note. A large referenced note. A large referenced note. A large referenced note. ",
)
NOTE_SMALL = ("General background", "A small unreferenced note. A small unreferenced note. ")


def test_generous() -> None:
    """Golden fixture, captured pre-CutCtx: generous."""
    context = assemble_context(
        unit=UNIT,
        requirements=[REQUIREMENT],
        budget_tokens=100000,
        neighbouring_units=NEIGHBOURS,
        unit_ordinals=ORDINALS,
        research_notes=NOTES,
    )
    assert context.dropped == ()
    assert context.budget_tokens == 100000
    assert [s.name for s in context.sections] == [
        "unit_specification",
        "requirements",
        "research_notes",
        "research_notes",
        "distant_units",
        "adjacent_units",
        "adjacent_units",
    ]
    assert (
        context.render()
        == "## The unit you are writing: U-02 — Where the work happens\nGoal: Say plainly where inference runs.\nTarget length: about 400 words.\nRequirements assigned to this unit: R-001\n\n## Requirements this unit must satisfy\n- R-001 (MUST): The unit must be explicit about where inference happens.\n  Checked by: contains any of: 'own machine'\n\n## Research note: Where the work happens\nA note the unit's own title names. A note the unit's own title names. A note the unit's own title names. A note the unit's own title names. A note the unit's own title names. A note the unit's own title names. A note the unit's own title names. A note the unit's own title names. A note the unit's own title names. A note the unit's own title names.\n\n## Research note: General background\nA note nothing references. A note nothing references. A note nothing references. A note nothing references. A note nothing references. A note nothing references. A note nothing references. A note nothing references. A note nothing references. A note nothing references.\n\n## Committed unit U-07\nA far-off section. A far-off section. A far-off section. A far-off section. A far-off section. A far-off section. A far-off section. A far-off section. A far-off section. A far-off section.\n\n## Committed unit U-01\nThe previous section. The previous section. The previous section. The previous section. The previous section. The previous section. The previous section. The previous section. The previous section. The previous section.\n\n## Committed unit U-03\nThe next section. The next section. The next section. The next section. The next section. The next section. The next section. The next section. The next section. The next section.\n"
    )


def test_drops_research_notes_only() -> None:
    """Golden fixture, captured pre-CutCtx: drops research notes only."""
    context = assemble_context(
        unit=UNIT,
        requirements=[REQUIREMENT],
        budget_tokens=284,
        neighbouring_units=NEIGHBOURS,
        unit_ordinals=ORDINALS,
        research_notes=NOTES,
    )
    assert context.dropped == ("research_notes", "research_notes")
    assert context.budget_tokens == 284
    assert [s.name for s in context.sections] == [
        "unit_specification",
        "requirements",
        "distant_units",
        "adjacent_units",
        "adjacent_units",
    ]
    assert (
        context.render()
        == "## The unit you are writing: U-02 — Where the work happens\nGoal: Say plainly where inference runs.\nTarget length: about 400 words.\nRequirements assigned to this unit: R-001\n\n## Requirements this unit must satisfy\n- R-001 (MUST): The unit must be explicit about where inference happens.\n  Checked by: contains any of: 'own machine'\n\n## Committed unit U-07\nA far-off section. A far-off section. A far-off section. A far-off section. A far-off section. A far-off section. A far-off section. A far-off section. A far-off section. A far-off section.\n\n## Committed unit U-01\nThe previous section. The previous section. The previous section. The previous section. The previous section. The previous section. The previous section. The previous section. The previous section. The previous section.\n\n## Committed unit U-03\nThe next section. The next section. The next section. The next section. The next section. The next section. The next section. The next section. The next section. The next section.\n"
    )


def test_drops_distant_and_research() -> None:
    """Golden fixture, captured pre-CutCtx: drops distant and research."""
    context = assemble_context(
        unit=UNIT,
        requirements=[REQUIREMENT],
        budget_tokens=224,
        neighbouring_units=NEIGHBOURS,
        unit_ordinals=ORDINALS,
        research_notes=NOTES,
    )
    assert context.dropped == ("research_notes", "research_notes", "distant_units")
    assert context.budget_tokens == 224
    assert [s.name for s in context.sections] == [
        "unit_specification",
        "requirements",
        "adjacent_units",
        "adjacent_units",
    ]
    assert (
        context.render()
        == "## The unit you are writing: U-02 — Where the work happens\nGoal: Say plainly where inference runs.\nTarget length: about 400 words.\nRequirements assigned to this unit: R-001\n\n## Requirements this unit must satisfy\n- R-001 (MUST): The unit must be explicit about where inference happens.\n  Checked by: contains any of: 'own machine'\n\n## Committed unit U-01\nThe previous section. The previous section. The previous section. The previous section. The previous section. The previous section. The previous section. The previous section. The previous section. The previous section.\n\n## Committed unit U-03\nThe next section. The next section. The next section. The next section. The next section. The next section. The next section. The next section. The next section. The next section.\n"
    )


def test_drops_one_adjacent_and_more() -> None:
    """Golden fixture, captured pre-CutCtx: drops one adjacent and more."""
    context = assemble_context(
        unit=UNIT,
        requirements=[REQUIREMENT],
        budget_tokens=184,
        neighbouring_units=NEIGHBOURS,
        unit_ordinals=ORDINALS,
        research_notes=NOTES,
    )
    assert context.dropped == (
        "research_notes",
        "research_notes",
        "distant_units",
        "adjacent_units",
    )
    assert context.budget_tokens == 184
    assert [s.name for s in context.sections] == [
        "unit_specification",
        "requirements",
        "adjacent_units",
    ]
    assert (
        context.render()
        == "## The unit you are writing: U-02 — Where the work happens\nGoal: Say plainly where inference runs.\nTarget length: about 400 words.\nRequirements assigned to this unit: R-001\n\n## Requirements this unit must satisfy\n- R-001 (MUST): The unit must be explicit about where inference happens.\n  Checked by: contains any of: 'own machine'\n\n## Committed unit U-01\nThe previous section. The previous section. The previous section. The previous section. The previous section. The previous section. The previous section. The previous section. The previous section. The previous section.\n"
    )


def test_strict_not_greedy_large_then_small() -> None:
    """Golden fixture, captured pre-CutCtx: strict not greedy large then small."""
    context = assemble_context(
        unit=UNIT,
        requirements=[REQUIREMENT],
        budget_tokens=184,
        research_notes=[NOTE_SMALL, NOTE_LARGE],
    )
    assert context.dropped == ("research_notes", "research_notes")
    assert context.budget_tokens == 184
    assert [s.name for s in context.sections] == ["unit_specification", "requirements"]
    assert (
        context.render()
        == "## The unit you are writing: U-02 — Where the work happens\nGoal: Say plainly where inference runs.\nTarget length: about 400 words.\nRequirements assigned to this unit: R-001\n\n## Requirements this unit must satisfy\n- R-001 (MUST): The unit must be explicit about where inference happens.\n  Checked by: contains any of: 'own machine'\n"
    )


def test_overflow_raises() -> None:
    """Golden: the overflow raises with both numbers, captured pre-CutCtx."""
    with pytest.raises(ContextLimitExceeded) as caught:
        assemble_context(
            unit=UNIT,
            requirements=[REQUIREMENT],
            budget_tokens=50,
        )
    assert (
        caught.value.message
        == "The context this unit cannot do without needs 84 tokens and the budget is 50. Requirements and the unit specification are never dropped, so nothing here can be reduced: raise workflow.context_budget_tokens, split the unit, or assign it fewer requirements."
    )
    assert caught.value.details == {
        "required_tokens": 84,
        "budget_tokens": 50,
        "unit_key": "U-02",
        "requirement_count": 1,
        "undroppable_sections": ["unit_specification", "requirements"],
    }
