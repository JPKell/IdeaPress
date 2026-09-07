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

Since J2 (ADR-0104), the *fill decision* — which of the budgeted sections survive — is CutCtx's
``DropOldestPolicy``, not a hand-rolled loop. Workflows §7's order becomes the turn order a
``DropOldestPolicy`` reads as "oldest first": the least valuable section is built as the oldest
turn, the unit specification and requirements are pinned, and the budget's
``protected_recent_turns`` is fixed at ``0`` so ``pinned`` alone carries the whole untouchable set.
What stays local is the *rendering* — how a section becomes text — and the *presentation order* —
how the surviving sections and the dropped names are formatted back into workflows §7's shape, once
CutCtx has decided which sections survive.

Token counting is an **estimate**, and the module says so rather than implying a tokenizer it does
not have. The default is characters ÷ 4, the conventional English approximation, delegated to
CutCtx's own ``CharRatioEstimator`` so the suite has one such formula rather than two that happen to
agree; a caller with a real tokenizer injects one. The estimate is deliberately used for the
*budget* only — never to report usage, which comes from the backend and is measured.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from cutctx import (
    Action,
    BudgetUnsatisfiable,
    CharRatioEstimator,
    CompactionBudget,
    CompactionExecutor,
    DropOldestPolicy,
    Role,
    Transcript,
    TranscriptTurn,
)

from ideapress.errors import ContextLimitExceeded

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from cutctx import CompactionReport

    from ideapress.domain.plan import PlanUnit
    from ideapress.domain.requirements import Requirement

__all__ = [
    "REDUCTION_ORDER",
    "AssembledContext",
    "AssembledReview",
    "ContextSection",
    "Droppability",
    "assemble_context",
    "assemble_review_context",
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
sections either side of this one are the ones whose prose it must not contradict. Since J2, this is
also the turn-ordering key: a section's position in this tuple decides how old CutCtx's
``DropOldestPolicy`` sees it, so the reduction order is enforced by *position* rather than by a
loop that re-derives it."""

_CHARS_PER_TOKEN: Final = 4.0
_ESTIMATOR: Final = CharRatioEstimator(_CHARS_PER_TOKEN)


def estimate_tokens(text: str) -> int:
    """Estimate a string's token count.

    Args:
        text: Any text.

    Returns:
        ``ceil(len(text) / 4)`` — the conventional English approximation, and an **estimate**. It
        is used to decide what fits in a budget and never to report usage: usage comes from the
        backend, measured, and conflating the two would put a guess into a provenance record.

        A thin alias over :class:`cutctx.CharRatioEstimator`, which computes exactly this formula
        (spec §7; confirmed by ``test_estimate_tokens_agrees_with_cutctx`` in
        ``tests/unit/test_context_budget.py``) — one estimator, not two that happen to agree.

        Deterministic and locale-independent, so two runs on two machines assemble the same
        context — which the backend-parity and export-stability claims both rest on.
    """
    return _ESTIMATOR.estimate_tokens(text)


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
        report: The CutCtx ``context.compacted`` event body, when anything was dropped to fit;
            ``None`` when nothing was. A caller emits it on the existing event sink — ``domain/``
            performs no I/O (D4, ADR-0104).
    """

    sections: tuple[ContextSection, ...]
    dropped: tuple[str, ...] = ()
    budget_tokens: int = 0
    report: CompactionReport | None = None

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


@dataclass(frozen=True, slots=True)
class AssembledReview:
    """What ``project_review``'s whole-document context assembled to, and what had to go.

    Unlike :class:`AssembledContext`, nothing here is pinned. Workflows §2 stage 15's only
    documented input is "All units" — there is no per-review unit specification or requirement
    list the way per-unit assembly has, so every unit is budgeted and the least valuable is
    dropped first (see :func:`assemble_review_context` for which end that is, and why).

    Attributes:
        rendered_units: The surviving units, in reading order, already rendered in the stage's own
            ``"### {key} — {title}\\n{text}"`` block format — unchanged from before this row.
        dropped: Unit keys removed to fit, in the order they were removed.
        budget_tokens: The budget it was assembled against.
        report: The CutCtx ``context.compacted`` event body, when anything was dropped to fit;
            ``None`` when nothing was.
    """

    rendered_units: tuple[str, ...] = ()
    dropped: tuple[str, ...] = ()
    budget_tokens: int = 0
    report: CompactionReport | None = None

    def render(self) -> str:
        """The whole document: every surviving unit's block, joined exactly as before this row."""
        return "\n\n".join(self.rendered_units)


def _rank_of(name: str) -> int:
    """Where a droppable section sits in the reduction order; unknown names go first.

    Turn construction's ordering key, and the key the surviving and dropped sections are re-sorted
    by afterwards — not a decision (CutCtx's ``DropOldestPolicy`` makes the keep/drop call); a
    presentation key, used before the chain runs to place a section in the transcript, and after it
    runs to put workflows §7's order back onto whatever the chain kept.
    """
    return REDUCTION_ORDER.index(name) if name in REDUCTION_ORDER else -1


def _priority_key(section: ContextSection) -> tuple[int, int]:
    """Most valuable last to drop: highest name-rank, then highest rank, first."""
    return (_rank_of(section.name), section.rank)


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
            forbids, with extra steps. Raised here by translating CutCtx's
            :class:`~cutctx.BudgetUnsatisfiable` at this boundary (D6, ADR-0104): the package's
            error never leaks past ``domain/``.
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

    budgeted = _budgeted_sections(
        unit=unit,
        neighbouring_units=neighbouring_units or {},
        unit_ordinals=unit_ordinals or {},
        research_notes=research_notes,
    )
    # Most valuable first — the presentation order workflows §7 expects on screen, and the order
    # `DropOldestPolicy`'s "oldest first" must run in **reverse**: the least valuable section is
    # the oldest turn, so build the transcript from the tail of this list forward.
    ordered_budgeted = sorted(budgeted, key=_priority_key, reverse=True)

    mandatory_turns = tuple(
        TranscriptTurn(
            turn_id=f"mandatory:{index}",
            role=Role.USER,
            content=section.render(),
            token_estimate=estimator(section.render()),
            pinned=True,
        )
        for index, section in enumerate(always)
    )
    budgeted_ids = [f"budgeted:{index}" for index in range(len(ordered_budgeted))]
    budgeted_turns = tuple(
        TranscriptTurn(
            turn_id=turn_id,
            role=Role.USER,
            content=section.render(),
            token_estimate=estimator(section.render()),
            pinned=False,
        )
        for turn_id, section in reversed(list(zip(budgeted_ids, ordered_budgeted, strict=True)))
    )
    transcript = Transcript(turns=mandatory_turns + budgeted_turns)
    budget = CompactionBudget(max_tokens=budget_tokens, protected_recent_turns=0)

    try:
        plan = DropOldestPolicy().decide(transcript, budget)
    except BudgetUnsatisfiable as exc:
        mandatory_tokens = exc.details["untouchable_tokens"]
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
        ) from exc

    kept_ids = {action.turn_id for action in plan.actions if action.action is Action.KEEP}
    section_by_id = dict(zip(budgeted_ids, ordered_budgeted, strict=True))
    # `ordered_budgeted`'s order is preserved by filtering rather than re-sorting, which is what
    # keeps a tied pair's relative order identical to the pre-CutCtx algorithm's (both stable
    # sorts, same input order) instead of merely as-if-equivalent.
    kept_in_fill_order = [section_by_id[tid] for tid in budgeted_ids if tid in kept_ids]
    dropped_in_fill_order = [section_by_id[tid] for tid in budgeted_ids if tid not in kept_ids]

    # `DropOldestPolicy` keeps a suffix of "oldest first" — i.e. a prefix of `ordered_budgeted`
    # (most-valuable-first) — so `kept_in_fill_order` is already in that prefix's order; a stable
    # sort on the name alone regroups it ascending (research, then distant, then adjacent) without
    # disturbing the descending-rank order already established within one name.
    kept_final = sorted(kept_in_fill_order, key=lambda section: _rank_of(section.name))
    dropped_names = tuple(section.name for section in reversed(dropped_in_fill_order))

    report: CompactionReport | None = None
    if dropped_names:
        report = CompactionExecutor().apply(transcript, plan).report

    return AssembledContext(
        sections=(*always, *kept_final),
        dropped=dropped_names,
        budget_tokens=budget_tokens,
        report=report,
    )


def assemble_review_context(
    *,
    units: Mapping[str, str],
    titles: Mapping[str, str],
    budget_tokens: int,
    estimator: Callable[[str], int] = estimate_tokens,
) -> AssembledReview:
    """Assemble ``project_review``'s whole-document context within ``budget_tokens`` (row K3).

    Workflows §2 stage 15's documented input is "All units" — there is no unit specification or
    requirement list here for CutCtx's ``pinned`` to protect the way :func:`assemble_context`
    protects them, so nothing is pinned and every unit is dropped, least valuable first, until the
    budget fits.

    **Drop order.** Units are dropped from the *end* of reading order first — the latest unit is
    the least valuable and goes before the earliest. A cross-unit review is checking later material
    for drift *away from* something, and the earliest units are where the project's terms, facts
    and structure are first established; keeping that end intact for as long as the budget allows
    gives the reviewer the actual reference material a drift check needs, where dropping from the
    front would leave it comparing later units to each other with no anchor for what "consistent"
    means in this project.

    Args:
        units: Every committed unit's text by key, in reading order
            (``services.units.committed_units``'s contract).
        titles: Unit titles by key, for the heading each block already carries.
        budget_tokens: The ceiling, from ``workflow.project_review_context_budget_tokens``.
        estimator: Token estimator, injected so a caller with a real tokenizer can supply one.

    Returns:
        The assembly, naming everything that was dropped to fit.

    Raises:
        ContextLimitExceeded: The budget cannot hold even the single cheapest unit. Nothing here
            is pinned, so CutCtx's own ``BudgetUnsatisfiable`` (which fires only when a *pinned*
            turn overflows) never applies to this caller — a budget too small would otherwise have
            CutCtx silently return an empty view, which is the silent-degradation-with-extra-steps
            workflows §7's philosophy refuses everywhere else. This is that same refusal, made
            explicit at the one seam CutCtx's own contract cannot reach for a caller with no
            undroppable content (ADR-0104's "Revisit when"), carrying the smallest unit's size and
            the budget so an operator raises ``workflow.project_review_context_budget_tokens``
            rather than guessing.
    """
    if not units:
        return AssembledReview(budget_tokens=budget_tokens)

    ordered_keys = list(units)
    rendered_by_key = {
        key: f"### {key} — {titles.get(key, '')}\n{text.strip()}" for key, text in units.items()
    }
    tokens_by_key = {key: estimator(block) for key, block in rendered_by_key.items()}

    # Oldest-first is least-valuable-first: build the transcript from the tail of reading order
    # forward, so `DropOldestPolicy` drops the latest units before the earliest (see docstring).
    turns = tuple(
        TranscriptTurn(
            turn_id=key,
            role=Role.USER,
            content=rendered_by_key[key],
            token_estimate=tokens_by_key[key],
            pinned=False,
        )
        for key in reversed(ordered_keys)
    )
    transcript = Transcript(turns=turns)
    budget = CompactionBudget(max_tokens=budget_tokens, protected_recent_turns=0)
    plan = DropOldestPolicy().decide(transcript, budget)

    kept_ids = {action.turn_id for action in plan.actions if action.action is Action.KEEP}
    kept_keys = [key for key in ordered_keys if key in kept_ids]
    dropped_keys = tuple(key for key in reversed(ordered_keys) if key not in kept_ids)

    if not kept_keys:
        cheapest = min(tokens_by_key.values())
        message = (
            f"Even the cheapest committed unit needs {cheapest} tokens and the budget is "
            f"{budget_tokens}. Nothing in project_review's context is pinned, so raising "
            "workflow.project_review_context_budget_tokens is the only fix — the review cannot "
            "silently run over an empty document instead."
        )
        raise ContextLimitExceeded(
            message,
            details={"required_tokens": cheapest, "budget_tokens": budget_tokens},
        )

    report: CompactionReport | None = None
    if dropped_keys:
        report = CompactionExecutor().apply(transcript, plan).report

    return AssembledReview(
        rendered_units=tuple(rendered_by_key[key] for key in kept_keys),
        dropped=dropped_keys,
        budget_tokens=budget_tokens,
        report=report,
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
