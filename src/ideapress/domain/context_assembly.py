"""ideapress.domain.context_assembly — building a model's context, deterministically, to a budget.

Workflows §7. Python assembles the context; a model never chooses what it sees. Two rules carry
the whole risk:

* **Requirements and the unit specification are never dropped.** They are the contract the unit is
  judged against. Dropping one to make room would produce a unit that fails a gate it was never
  told about — risk T3, whose early signal is "units missing requirements they were assigned".
* **If the undroppable sections alone exceed the budget, the stage fails with numbers.** Not a
  truncation, not a best effort: an error carrying both the required figure and the budget, so the
  user can raise one or split the unit rather than guess.

The reduction order is **data**, not a sequence of ``if`` statements, so it can be read, tested and
compared against the document: research notes → distant unit summaries → adjacent unit summaries.

Token counting is an **estimate**, and the module says so rather than implying a tokenizer it does
not have. The default is characters ÷ 4, the conventional English approximation; a caller with a
real tokenizer injects one. The estimate is deliberately used for the *budget* only — never to
report usage, which comes from the backend and is measured.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from ideapress.errors import ContextLimitExceeded

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from ideapress.domain.plan import PlanUnit
    from ideapress.domain.requirements import Requirement

__all__ = [
    "REDUCTION_ORDER",
    "AssembledContext",
    "ContextSection",
    "Droppability",
    "assemble_context",
    "estimate_tokens",
]

Droppability = Literal["always", "budgeted"]

REDUCTION_ORDER: Final[tuple[str, ...]] = (
    "research_notes",
    "distant_units",
    "adjacent_units",
)
"""Workflows §7's reduction order, as data.

Least valuable first. Research notes go before unit summaries because a note the unit never
references is the cheapest thing in the window; distant units go before adjacent ones because the
sections either side of this one are the ones whose prose it must not contradict."""

_CHARS_PER_TOKEN: Final = 4.0


def estimate_tokens(text: str) -> int:
    """Estimate a string's token count.

    Args:
        text: Any text.

    Returns:
        ``ceil(len(text) / 4)`` — the conventional English approximation, and an **estimate**. It
        is used to decide what fits in a budget and never to report usage: usage comes from the
        backend, measured, and conflating the two would put a guess into a provenance record.

        Deterministic and locale-independent, so two runs on two machines assemble the same
        context — which the backend-parity and export-stability claims both rest on.
    """
    return -(-len(text) // int(_CHARS_PER_TOKEN))


@dataclass(frozen=True, slots=True)
class ContextSection:
    """One labelled block of the assembled context.

    Attributes:
        name: The section's identity, matching :data:`REDUCTION_ORDER` where it is droppable.
        heading: What the model sees above the block.
        body: The block's text.
        droppability: ``always`` sections are never removed to fit a budget.
        rank: Within a droppable section, higher survives longer. Research notes the unit
            explicitly references rank above ones it does not (workflows §7's "ranked by explicit
            reference").
    """

    name: str
    heading: str
    body: str
    droppability: Droppability = "budgeted"
    rank: int = 0

    @property
    def tokens(self) -> int:
        """The estimated size of this section as it will be rendered."""
        return estimate_tokens(self.render())

    def render(self) -> str:
        """The block as the model sees it."""
        return f"## {self.heading}\n{self.body.strip()}\n"


@dataclass(frozen=True, slots=True)
class AssembledContext:
    """What was assembled, and what had to go.

    Attributes:
        sections: What survived, in assembly order.
        dropped: The names of sections removed to fit, in the order they were removed.
        budget_tokens: The budget it was assembled against.
    """

    sections: tuple[ContextSection, ...]
    dropped: tuple[str, ...] = ()
    budget_tokens: int = 0

    @property
    def tokens(self) -> int:
        """The estimated size of the whole assembly."""
        return estimate_tokens(self.render())

    def render(self) -> str:
        """The complete context, in order, with a trailing newline per section."""
        return "\n".join(section.render() for section in self.sections)

    def section(self, name: str) -> ContextSection | None:
        """The section with this name, if it survived."""
        for candidate in self.sections:
            if candidate.name == name:
                return candidate
        return None


def _rank_of(name: str) -> int:
    """Where a droppable section sits in the reduction order; unknown names go first."""
    return REDUCTION_ORDER.index(name) if name in REDUCTION_ORDER else -1


def assemble_context(
    *,
    unit: PlanUnit,
    requirements: Sequence[Requirement],
    budget_tokens: int,
    glossary: Mapping[str, str] | None = None,
    style_constraints: str = "",
    neighbouring_units: Mapping[str, str] | None = None,
    unit_ordinals: Mapping[str, int] | None = None,
    research_notes: Sequence[tuple[str, str]] = (),
    previous_findings: str = "",
    estimator: Callable[[str], int] = estimate_tokens,
) -> AssembledContext:
    """Assemble one unit's context within ``budget_tokens``, dropping in the documented order.

    Args:
        unit: The unit being worked on. Its specification is never dropped.
        requirements: The requirements it carries. Never dropped.
        budget_tokens: The ceiling, from `workflow.context_budget_tokens`.
        glossary: Project terms, always included.
        style_constraints: The author's style guidance, always included.
        neighbouring_units: Committed unit text by key, for consistency.
        unit_ordinals: Each unit's position, so "adjacent" means adjacent rather than "mentioned
            nearby". A unit with no known ordinal is treated as distant.
        research_notes: ``(title, text)`` pairs. A note whose title the unit's goal mentions ranks
            above one it does not — workflows §7's "ranked by explicit reference".
        previous_findings: What the last attempt got wrong. Always included on repair and revision,
            because a repair without the findings is a re-roll.
        estimator: Token estimator, injected so a caller with a real tokenizer can supply one.

    Returns:
        The assembly, naming everything that was dropped to fit.

    Raises:
        ContextLimitExceeded: The undroppable sections alone exceed the budget. Carries
            ``required_tokens`` and ``budget_tokens`` **both**, always — workflows §7 says the
            stage fails "with numbers", and a message without them is the silent truncation it
            forbids, with extra steps.
    """
    always: list[ContextSection] = [
        ContextSection(
            name="unit_specification",
            heading=f"The unit you are writing: {unit.key} — {unit.title}",
            body=_render_unit(unit),
            droppability="always",
        ),
        ContextSection(
            name="requirements",
            heading="Requirements this unit must satisfy",
            body=_render_requirements(requirements),
            droppability="always",
        ),
    ]
    if glossary or style_constraints:
        always.append(
            ContextSection(
                name="style",
                heading="Project glossary and style",
                body=_render_style(glossary or {}, style_constraints),
                droppability="always",
            )
        )
    if previous_findings.strip():
        always.append(
            ContextSection(
                name="previous_findings",
                heading="What the previous attempt got wrong",
                body=previous_findings.strip(),
                droppability="always",
            )
        )

    mandatory_tokens = sum(estimator(section.render()) for section in always)
    if mandatory_tokens > budget_tokens:
        message = (
            f"The context this unit cannot do without needs {mandatory_tokens} tokens and the "
            f"budget is {budget_tokens}. Requirements and the unit specification are never "
            "dropped, so nothing here can be reduced: raise workflow.context_budget_tokens, split "
            "the unit, or assign it fewer requirements."
        )
        raise ContextLimitExceeded(
            message,
            details={
                "required_tokens": mandatory_tokens,
                "budget_tokens": budget_tokens,
                "unit_key": unit.key,
                "requirement_count": len(requirements),
                "undroppable_sections": [section.name for section in always],
            },
        )

    budgeted = _budgeted_sections(
        unit=unit,
        neighbouring_units=neighbouring_units or {},
        unit_ordinals=unit_ordinals or {},
        research_notes=research_notes,
    )

    remaining = budget_tokens - mandatory_tokens
    kept: list[ContextSection] = []
    dropped: list[str] = []
    # Most valuable first: `adjacent_units`, then `distant_units`, then `research_notes`, and
    # within one name the higher `rank` first. That is REDUCTION_ORDER read backwards, which is
    # what "dropped in this order" means when you are filling rather than emptying.
    ordered = sorted(budgeted, key=lambda s: (_rank_of(s.name), s.rank), reverse=True)
    # **Strictly ordered, not greedy.** Once something does not fit, everything less valuable is
    # dropped too, even if it happened to be small enough. A greedy fill would keep an unreferenced
    # research note because it was short while dropping the referenced one because it was long —
    # utilising the budget better and violating the ranking the document states. Workflows §7
    # describes an order of preference, not a packing problem, and a reduction whose outcome
    # depends on the relative sizes of what it is reducing is one nobody can predict or test.
    exhausted = False
    for section in ordered:
        cost = estimator(section.render())
        if exhausted or cost > remaining:
            exhausted = True
            dropped.append(section.name)
            continue
        remaining -= cost
        kept.append(section)

    kept.sort(key=lambda s: (_rank_of(s.name), -s.rank))
    return AssembledContext(
        sections=(*always, *kept),
        dropped=tuple(reversed(dropped)),
        budget_tokens=budget_tokens,
    )


def _budgeted_sections(
    *,
    unit: PlanUnit,
    neighbouring_units: Mapping[str, str],
    unit_ordinals: Mapping[str, int],
    research_notes: Sequence[tuple[str, str]],
) -> list[ContextSection]:
    """Build the droppable sections, one per neighbour and one per note."""
    sections: list[ContextSection] = []
    goal = f"{unit.title} {unit.goal_text}".lower()
    for index, (title, body) in enumerate(research_notes):
        referenced = title.lower() in goal
        sections.append(
            ContextSection(
                name="research_notes",
                heading=f"Research note: {title}",
                body=body,
                rank=(1000 if referenced else 0) - index,
            )
        )

    ordinal = unit_ordinals.get(unit.key)
    for key, body in neighbouring_units.items():
        if key == unit.key:
            continue
        other = unit_ordinals.get(key)
        adjacent = ordinal is not None and other is not None and abs(other - ordinal) == 1
        sections.append(
            ContextSection(
                name="adjacent_units" if adjacent else "distant_units",
                heading=f"Committed unit {key}",
                body=body,
                rank=-abs((other or 999) - (ordinal or 0)),
            )
        )
    return sections


def _render_unit(unit: PlanUnit) -> str:
    """The unit specification block."""
    lines = [f"Goal: {unit.goal_text}"]
    if unit.target_words:
        lines.append(f"Target length: about {unit.target_words} words.")
    if unit.requirement_keys:
        lines.append(f"Requirements assigned to this unit: {', '.join(unit.requirement_keys)}")
    return "\n".join(lines)


def _render_requirements(requirements: Sequence[Requirement]) -> str:
    """The requirements block, with each one's deterministic checks stated.

    The checks are shown because they are what will actually be run: a model told "must mention
    running locally" and then judged on `must_contain_any: ["locally", "on-device"]` is being
    marked against a rubric it was not given.
    """
    if not requirements:
        return "(none assigned to this unit)"
    lines: list[str] = []
    for requirement in requirements:
        marker = "MUST" if requirement.blocking else "should"
        lines.append(f"- {requirement.key} ({marker}): {requirement.text}")
        lines.append(f"  Checked by: {requirement.describe_checks()}")
    return "\n".join(lines)


def _render_style(glossary: Mapping[str, str], style_constraints: str) -> str:
    """The glossary and style block."""
    lines: list[str] = []
    if style_constraints.strip():
        lines.append(style_constraints.strip())
    if glossary:
        lines.append("Preferred terms:")
        lines.extend(
            f"- write {canonical!r}, not {variant!r}"
            for variant, canonical in sorted(glossary.items())
        )
    return "\n".join(lines)
