"""Risk T3: context reduction must never drop a requirement.

The reduction order is documented, so it is tested as documented — research notes, then distant
unit summaries, then adjacent ones — and the two undroppable sections are tested by squeezing the
budget until everything else is gone and then past that, where the stage must fail **with both
numbers** rather than truncate the contract.
"""

from __future__ import annotations

import pytest

from ideapress.domain.context_assembly import (
    REDUCTION_ORDER,
    assemble_context,
    estimate_tokens,
)
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

ORDINALS = {"U-01": 1, "U-02": 2, "U-03": 3, "U-07": 7}
NEIGHBOURS = {
    "U-01": "The previous section, " + ("adjacent " * 40),
    "U-03": "The next section, " + ("adjacent " * 40),
    "U-07": "A far-off section, " + ("distant " * 40),
}
NOTES = [
    ("General background", "A note nothing references. " * 40),
    ("Where the work happens", "A note the unit's own title names. " * 40),
]


def _requirement(key: str = "R-001", *, text: str | None = None) -> Requirement:
    return Requirement(
        key=key,
        text=text or "The unit must state that inference runs on the reader's own machine.",
        blocking=True,
        source=SourceReference(document="brief", quote="a quotation long enough to be evidence"),
        compiled_by=COMPILED_BY,
        checks=(RequirementCheck(kind="must_contain_any", values=("own machine",)),),
    )


def test_a_generous_budget_keeps_everything() -> None:
    assembled = assemble_context(
        unit=UNIT,
        requirements=[_requirement()],
        budget_tokens=100_000,
        neighbouring_units=NEIGHBOURS,
        unit_ordinals=ORDINALS,
        research_notes=NOTES,
    )
    assert assembled.dropped == ()
    names = {section.name for section in assembled.sections}
    assert names == {
        "unit_specification",
        "requirements",
        "research_notes",
        "adjacent_units",
        "distant_units",
    }


def test_the_requirements_and_the_unit_specification_are_always_present() -> None:
    assembled = assemble_context(unit=UNIT, requirements=[_requirement()], budget_tokens=100_000)
    assert assembled.section("unit_specification") is not None
    requirements = assembled.section("requirements")
    assert requirements is not None
    assert "R-001" in requirements.body
    assert "own machine" in requirements.body, "the checks are shown, not only the statement"


def test_the_reduction_order_is_exactly_the_documented_one() -> None:
    """Squeeze the budget step by step and record what leaves, in order."""
    order: list[str] = []
    full = assemble_context(
        unit=UNIT,
        requirements=[_requirement()],
        budget_tokens=100_000,
        neighbouring_units=NEIGHBOURS,
        unit_ordinals=ORDINALS,
        research_notes=NOTES,
    )
    budget = full.tokens
    while budget > 0:
        try:
            assembled = assemble_context(
                unit=UNIT,
                requirements=[_requirement()],
                budget_tokens=budget,
                neighbouring_units=NEIGHBOURS,
                unit_ordinals=ORDINALS,
                research_notes=NOTES,
            )
        except ContextLimitExceeded:
            break
        for name in assembled.dropped:
            if name not in order:
                order.append(name)
        budget -= 20
    assert order == list(REDUCTION_ORDER), f"dropped in this order: {order}"


def test_an_explicitly_referenced_research_note_survives_longer_than_an_unreferenced_one() -> None:
    """Workflows §7: research notes are "ranked by explicit reference"."""
    kept: list[str] = []
    for budget in range(400, 1200, 20):
        assembled = assemble_context(
            unit=UNIT,
            requirements=[_requirement()],
            budget_tokens=budget,
            research_notes=NOTES,
        )
        notes = [s.heading for s in assembled.sections if s.name == "research_notes"]
        if len(notes) == 1:
            kept = notes
            break
    assert kept == ["Research note: Where the work happens"], (
        "the note the unit's own title names must be the last one standing"
    )


def test_requirements_alone_exceeding_the_budget_fails_with_both_numbers() -> None:
    """The case risk T3 names, and the error api.md promises."""
    many = [
        _requirement(f"R-{n:03d}", text=f"Requirement {n}: " + ("a long statement " * 20))
        for n in range(1, 40)
    ]
    with pytest.raises(ContextLimitExceeded) as caught:
        assemble_context(unit=UNIT, requirements=many, budget_tokens=200)
    details = caught.value.details
    assert details["required_tokens"] > details["budget_tokens"] == 200
    assert str(details["required_tokens"]) in caught.value.message
    assert "200" in caught.value.message
    assert details["unit_key"] == "U-02"
    assert details["requirement_count"] == 39
    assert set(details["undroppable_sections"]) == {"unit_specification", "requirements"}


def test_the_overflow_message_says_what_to_do_about_it() -> None:
    with pytest.raises(ContextLimitExceeded) as caught:
        assemble_context(
            unit=UNIT,
            requirements=[_requirement(f"R-{n:03d}") for n in range(30)],
            budget_tokens=100,
        )
    message = caught.value.message
    assert "never" in message and "dropped" in message
    assert "context_budget_tokens" in message


def test_previous_findings_are_never_dropped() -> None:
    """A repair without the findings is a re-roll, so they are undroppable by construction."""
    assembled = assemble_context(
        unit=UNIT,
        requirements=[_requirement()],
        budget_tokens=400,
        neighbouring_units=NEIGHBOURS,
        unit_ordinals=ORDINALS,
        research_notes=NOTES,
        previous_findings="The unit never mentioned the reader's own machine.",
    )
    findings = assembled.section("previous_findings")
    assert findings is not None
    assert "own machine" in findings.body
    assert assembled.dropped, "the droppable sections went first"


def test_style_and_glossary_are_never_dropped() -> None:
    assembled = assemble_context(
        unit=UNIT,
        requirements=[_requirement()],
        budget_tokens=400,
        glossary={"AI suite": "the suite"},
        style_constraints="Plain English, second person.",
        research_notes=NOTES,
    )
    style = assembled.section("style")
    assert style is not None
    assert "Plain English" in style.body
    assert "'the suite'" in style.body


def test_an_adjacent_unit_outlives_a_distant_one() -> None:
    surviving: list[str] = []
    # Scanning **down** from a budget that holds everything: the interesting band is the one
    # just tight enough to lose the distant unit and no tighter.
    for budget in range(1400, 100, -20):
        assembled = assemble_context(
            unit=UNIT,
            requirements=[_requirement()],
            budget_tokens=budget,
            neighbouring_units=NEIGHBOURS,
            unit_ordinals=ORDINALS,
        )
        names = {s.name for s in assembled.sections}
        if "adjacent_units" in names and "distant_units" not in names:
            surviving = [s.heading for s in assembled.sections if s.name == "adjacent_units"]
            break
    assert surviving, "no budget keeps an adjacent unit while dropping a distant one"
    assert "U-07" not in " ".join(surviving), "U-07 is the distant one"
    assert {"U-01", "U-03"} == {head.split()[-1] for head in surviving}


def test_a_unit_with_no_known_ordinal_is_treated_as_distant() -> None:
    """ "Adjacent" must mean adjacent, not "we have no idea, assume it is nearby"."""
    assembled = assemble_context(
        unit=UNIT,
        requirements=[_requirement()],
        budget_tokens=100_000,
        neighbouring_units={"U-99": "An unplaced unit."},
        unit_ordinals=ORDINALS,
    )
    section = assembled.section("distant_units")
    assert section is not None
    assert "U-99" in section.heading


def test_a_unit_never_receives_its_own_text_as_a_neighbour() -> None:
    assembled = assemble_context(
        unit=UNIT,
        requirements=[_requirement()],
        budget_tokens=100_000,
        neighbouring_units={"U-02": "This unit's own previous text."},
        unit_ordinals=ORDINALS,
    )
    assert all("U-02" not in s.heading for s in assembled.sections if s.name.endswith("_units"))


def test_the_estimate_is_deterministic_and_locale_independent() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2
    assert estimate_tokens("héllo wörld") == estimate_tokens("héllo wörld")


def test_a_custom_estimator_is_honoured() -> None:
    """A caller with a real tokenizer supplies one; the budget arithmetic uses it throughout."""
    with pytest.raises(ContextLimitExceeded):
        assemble_context(
            unit=UNIT,
            requirements=[_requirement()],
            budget_tokens=10,
            estimator=lambda text: len(text),
        )


def test_the_assembled_context_renders_every_section_in_order() -> None:
    assembled = assemble_context(
        unit=UNIT,
        requirements=[_requirement()],
        budget_tokens=100_000,
        research_notes=NOTES,
    )
    rendered = assembled.render()
    assert rendered.index("The unit you are writing") < rendered.index("Requirements this unit")
    assert rendered.index("Requirements this unit") < rendered.index("Research note")
