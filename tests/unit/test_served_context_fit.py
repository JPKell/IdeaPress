"""A stage's budgets are checked against the window the backend serves, before the run (row WPF7).

WP6's IdeaPress failures all had one shape: the prompt, the model's reasoning and its answer come
out of **one** context window, and nothing compared the two budgets IdeaPress configures against the
size of that window. On the reference machine Ollama served 8 192 tokens while `project_review`
assembled 12 777 under a 24 000-token budget and asked for 8 192 more — so the model spent what was
left of the window thinking and returned nothing, twice, and the failure told the operator to raise
the number that could not be honoured.
"""

from __future__ import annotations

from ideapress.config import Settings, load_settings
from ideapress.services.context_fit import (
    PROMPT_OVERHEAD_TOKENS,
    context_shortfall,
    needed_context_tokens,
    served_context_tokens,
)


def _settings(**workflow: object) -> Settings:
    settings = load_settings().settings
    if workflow:
        return settings.model_copy(
            update={"workflow": settings.workflow.model_copy(update=workflow)}
        )
    return settings


def test_the_shipped_defaults_fit_the_context_they_ask_to_be_served() -> None:
    """The set has to be self-consistent, or a fresh installation refuses its own first stage."""
    settings = _settings()
    served = served_context_tokens(settings)
    assert served > 0, "the default is a stated number, not the server's unknown one"
    from ideapress.domain.stages import MODEL_STAGES

    unfitting = {
        stage: context_shortfall(settings, stage, target_words=1000) for stage in MODEL_STAGES
    }
    assert {stage: problem for stage, problem in unfitting.items() if problem} == {}


def test_a_stage_that_cannot_fit_is_named_with_every_number() -> None:
    """WP6's own arithmetic: 24 000 of context and 8 192 of output into an 8 192-token window."""
    settings = _settings(project_review_context_budget_tokens=24_000)
    settings = settings.model_copy(
        update={
            "inference": settings.inference.model_copy(
                update={
                    "ollama": settings.inference.ollama.model_copy(
                        update={"served_context_tokens": 8192}
                    )
                }
            )
        }
    )

    problem = context_shortfall(settings, "project_review")

    assert problem is not None
    assert "24000" in problem, "the context budget it would have used"
    assert "8192" in problem, "the served window, and the output budget"
    assert "no text at all" in problem, "what the operator would otherwise have seen"
    assert "served_context_tokens" in problem, "one of the two things they can change"


def test_the_needed_figure_is_context_plus_output_plus_the_prompt() -> None:
    settings = _settings(context_budget_tokens=1000, structured_output_tokens=2000)
    assert needed_context_tokens(settings, "critique") == 1000 + 2000 + PROMPT_OVERHEAD_TOKENS


def test_a_text_stage_budgets_four_tokens_per_target_word_above_the_floor() -> None:
    """`draft` writes: its output budget grows with the unit, so the check must grow with it."""
    settings = _settings(context_budget_tokens=1000, structured_output_tokens=2000)
    short = needed_context_tokens(settings, "draft", target_words=100)
    long = needed_context_tokens(settings, "draft", target_words=1100)
    assert long - short == 4000


def test_an_unstated_window_is_not_checked() -> None:
    """A check with no figure to check against would be a guess dressed as a refusal."""
    settings = _settings(project_review_context_budget_tokens=999_000)
    settings = settings.model_copy(
        update={
            "inference": settings.inference.model_copy(
                update={
                    "ollama": settings.inference.ollama.model_copy(
                        update={"served_context_tokens": 0}
                    )
                }
            )
        }
    )
    assert served_context_tokens(settings) == 0
    assert context_shortfall(settings, "project_review") is None


def test_another_backend_owns_its_own_window() -> None:
    """In `loadcoach` mode the window belongs to the service on the other side (ADR-0040)."""
    settings = _settings(project_review_context_budget_tokens=999_000)
    settings = settings.model_copy(
        update={"inference": settings.inference.model_copy(update={"mode": "loadcoach"})}
    )
    assert served_context_tokens(settings) == 0
    assert context_shortfall(settings, "project_review") is None


def test_the_setting_reaches_ollama_as_num_ctx() -> None:
    """What IdeaPress checks against must be what it asks for, or the check is about nothing."""
    from modelrack.testing import FakeProvider

    from ideapress.config import OllamaSettings
    from ideapress.domain.inference import Correlation, StageLimits, StageRequest
    from ideapress.infrastructure.backends.fake import default_fake_script
    from ideapress.infrastructure.backends.ollama import OllamaBackend

    provider = FakeProvider(default_fake_script(), seed=3)
    backend = OllamaBackend(
        OllamaSettings(served_context_tokens=12_345),
        provider=provider,  # type: ignore[arg-type]  # structural, as the parity tests do
    )
    seen: list[int | None] = []
    original = provider.generate

    def watched(request):  # type: ignore[no-untyped-def]  # a probe, not an implementation
        profile = request.runtime_profile
        seen.append(profile.context_size if profile is not None else None)
        return original(request)

    provider.generate = watched  # type: ignore[method-assign]  # observes what was asked for

    backend.generate(
        StageRequest(
            stage="critique",
            system="s",
            user="u",
            limits=StageLimits(temperature=0.0, max_output_tokens=64),
            correlation=Correlation(project_id="01PROJECT"),
            model_hint="ollama/qwen3.5:9b-q8_0",
        )
    )

    assert seen == [12_345]


def test_zero_asks_for_nothing_and_leaves_the_server_its_default() -> None:
    """The served context is the operator's memory decision (ADR-0119); 0 keeps it theirs."""
    from modelrack.testing import FakeProvider

    from ideapress.config import OllamaSettings
    from ideapress.domain.inference import Correlation, StageLimits, StageRequest
    from ideapress.infrastructure.backends.fake import default_fake_script
    from ideapress.infrastructure.backends.ollama import OllamaBackend

    provider = FakeProvider(default_fake_script(), seed=3)
    backend = OllamaBackend(
        OllamaSettings(served_context_tokens=0),
        provider=provider,  # type: ignore[arg-type]  # structural
    )
    seen: list[int | None] = []
    original = provider.generate

    def watched(request):  # type: ignore[no-untyped-def]  # a probe
        profile = request.runtime_profile
        seen.append(profile.context_size if profile is not None else None)
        return original(request)

    provider.generate = watched  # type: ignore[method-assign]  # observes what was asked for

    backend.generate(
        StageRequest(
            stage="critique",
            system="s",
            user="u",
            limits=StageLimits(temperature=0.0, max_output_tokens=64),
            correlation=Correlation(project_id="01PROJECT"),
            model_hint="ollama/qwen3.5:9b-q8_0",
        )
    )

    assert seen == [None]


def test_a_stage_request_gets_the_configured_timeout_not_a_domain_default() -> None:
    """Row WPF7, found live: `inference.ollama.timeout_seconds` was dead configuration.

    `StageLimits.timeout_seconds` was 300.0 and no stage set it, so every request carried a
    300-second deadline whatever the file said — a revision under the raised output budget timed out
    at 300 s and failed the whole draft stage twice before this was found.
    """
    from modelrack.testing import FakeProvider

    from ideapress.config import OllamaSettings
    from ideapress.domain.inference import Correlation, StageLimits, StageRequest
    from ideapress.infrastructure.backends.fake import default_fake_script
    from ideapress.infrastructure.backends.ollama import OllamaBackend

    provider = FakeProvider(default_fake_script(), seed=3)
    backend = OllamaBackend(
        OllamaSettings(timeout_seconds=900),
        provider=provider,  # type: ignore[arg-type]  # structural
    )
    seen: list[float | None] = []
    original = provider.generate

    def watched(request):  # type: ignore[no-untyped-def]  # a probe
        seen.append(request.timeout_seconds)
        return original(request)

    provider.generate = watched  # type: ignore[method-assign]  # observes the deadline asked for

    def run(limits: StageLimits) -> None:
        backend.generate(
            StageRequest(
                stage="revise",
                system="s",
                user="u",
                limits=limits,
                correlation=Correlation(project_id="01PROJECT"),
                model_hint="ollama/qwen3.5:9b-q8_0",
            )
        )

    run(StageLimits(temperature=0.2, max_output_tokens=16_384))
    run(StageLimits(temperature=0.2, max_output_tokens=16_384, timeout_seconds=42.0))

    assert seen == [900.0, 42.0], "the configured timeout, and a caller's own bound winning"
