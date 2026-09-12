"""ideapress.services.context_fit — do this stage's budgets fit the context the backend serves?

Row WPF7. IdeaPress's two budgets — the assembled context and the output allowance — are spent
from **one** window: a local server serves a fixed context length, and prompt, reasoning and answer
all come out of it. WP6 found what that costs when nobody checks. On the reference machine Ollama
served 8 192 tokens (`OLLAMA_CONTEXT_LENGTH`, the operator's memory cap, ADR-0119), while
`project_review` assembled a 12 777-token context under its 24 000-token budget and asked for
8 192 output tokens on top. The prompt did not fit, the model spent what was left of the window
reasoning, and the stage failed with *the model produced no text at all in 8192 output tokens* —
advising the operator to raise the very number that could not be honoured.

So the arithmetic is done **before the run**, once, here, and the answer is a sentence naming the
stage, the two budgets, the served context and what to change. The refusal and `doctor` read the
same function, so the operator cannot be told two different things.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from ideapress.services.unit_loop import output_budget_tokens

if TYPE_CHECKING:
    from ideapress.config import Settings

__all__ = ["PROMPT_OVERHEAD_TOKENS", "TEXT_STAGES", "context_shortfall", "needed_context_tokens"]

PROMPT_OVERHEAD_TOKENS: Final = 512
"""What a rendered prompt costs beyond the assembled context it carries.

The system prompt, the instructions and the JSON schema a structured stage sends are not part of
the context budget, and they are not free. 512 is the measured order of the shipped prompt records
on the reference machine — the largest, `project_review`'s, renders 190 tokens of instruction — kept
generous because the consequence of under-counting is the empty generation this module exists to
prevent."""

TEXT_STAGES: Final[frozenset[str]] = frozenset({"draft", "repair", "revise"})
"""The stages that write unit text, and so budget four tokens per target word above the thinking
floor (:func:`~ideapress.services.unit_loop.output_budget_tokens`)."""


def needed_context_tokens(
    settings: Settings, stage: str, *, target_words: int | None = None
) -> int:
    """How much served context this stage needs in the worst case its budgets allow.

    Args:
        settings: The effective settings.
        stage: The stage about to run.
        target_words: The longest target length among the units the run will write, for a text
            stage. ``None`` uses the same 400-word assumption the output budget does.

    Returns:
        The assembled-context budget, plus the stage's output budget, plus
        :data:`PROMPT_OVERHEAD_TOKENS`. It is an upper bound, not a prediction: a stage whose real
        context comes in under its budget needs less, and nothing here refuses a stage for using
        the budget it was given.
    """
    workflow = settings.workflow
    if stage == "project_review":
        context = workflow.project_review_context_budget_tokens
        output = workflow.structured_output_tokens
    elif stage in TEXT_STAGES:
        context = workflow.context_budget_tokens
        output = output_budget_tokens(
            target_words=target_words,
            structured_output_tokens=workflow.structured_output_tokens,
        )
    else:
        context = workflow.context_budget_tokens
        output = workflow.structured_output_tokens
    return context + output + PROMPT_OVERHEAD_TOKENS


def served_context_tokens(settings: Settings) -> int:
    """The context the configured backend serves, as far as configuration states it.

    Args:
        settings: The effective settings.

    Returns:
        `inference.ollama.served_context_tokens` in `ollama` mode, and ``0`` otherwise — for
        `loadcoach` and `openai_compatible` the window belongs to the service on the other side and
        IdeaPress is not entitled to state it (ADR-0040). ``0`` means "not stated here", and every
        check below is skipped rather than guessed.
    """
    if settings.inference.mode != "ollama":
        return 0
    return settings.inference.ollama.served_context_tokens


def context_shortfall(
    settings: Settings, stage: str, *, target_words: int | None = None
) -> str | None:
    """Say, before the run, that this stage's budgets cannot fit the served context.

    Args:
        settings: The effective settings.
        stage: The stage about to run.
        target_words: The longest target length among the units a text stage will write.

    Returns:
        A sentence naming the stage, what it needs, what is served and the three numbers that
        produce it — or ``None`` when it fits, and when the served context is not stated
        (:func:`served_context_tokens`), because a check with no figure to check against would be
        a guess dressed as a refusal.

    The remedy is deliberately not a single instruction: lowering a budget and serving a larger
    window are both valid, and which one is right is the operator's memory decision, not
    IdeaPress's (ADR-0119 decision 3).
    """
    served = served_context_tokens(settings)
    if served <= 0:
        return None
    needed = needed_context_tokens(settings, stage, target_words=target_words)
    if needed <= served:
        return None
    workflow = settings.workflow
    context_budget = (
        workflow.project_review_context_budget_tokens
        if stage == "project_review"
        else workflow.context_budget_tokens
    )
    output = needed - context_budget - PROMPT_OVERHEAD_TOKENS
    return (
        f"The {stage!r} stage needs {needed} tokens of served context — {context_budget} for its "
        f"assembled context, {output} for its output and {PROMPT_OVERHEAD_TOKENS} for the prompt "
        f"itself — and this backend serves {served}. A model spends its reasoning from the same "
        "window as its answer, so the stage would return no text at all rather than a short "
        "answer. Lower the stage's context or output budget, or raise "
        "`inference.ollama.served_context_tokens` to what the card can hold."
    )
