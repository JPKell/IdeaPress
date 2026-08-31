"""`-m live`: the real Ollama, the real card.

Two claims no offline test can make:

* **P2 AC1** — a "hello" stage produces real text through Ollama, and the *same* stage produces
  identical text through the fake at the same seed. The second half is the substitutability claim
  the whole test suite rests on.
* **ADR-0038** — across a real stage switch, `list_resident()` never reports two models. Polled
  from a background thread throughout, not sampled once at the end: the dangerous window is
  *during* the switch, and a single sample after it would miss exactly the failure being tested.

Never run in default CI. Needs Ollama at the configured URL with both default models pulled.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

import pytest

from ideapress.config import load_settings
from ideapress.domain.inference import Correlation, StageLimits, StageRequest
from ideapress.domain.stages import StageId
from ideapress.infrastructure.backends.fake import FakeBackend
from ideapress.infrastructure.backends.ollama import OllamaBackend
from ideapress.services.inference import InferenceGateway
from ideapress.services.prompts import render

if TYPE_CHECKING:
    from ideapress.domain.inference import InferenceBackend

pytestmark = pytest.mark.live


@pytest.fixture
def ollama() -> InferenceBackend:
    settings = load_settings().settings
    backend = OllamaBackend(settings.inference.ollama)
    if backend.health().status != "ok":
        pytest.skip("no Ollama at the configured URL")
    return backend


def _hello(*, model_hint: str | None = None, seed: int = 7) -> StageRequest:
    prompt = render("stages.hello", {"title": "Local inference for writers"})
    return StageRequest(
        stage="draft",
        system=prompt.system or "",
        user=prompt.user,
        # The default budget, deliberately: `gemma4:12b` spends its output allowance thinking
        # before its first word, and **how much varies run to run even at temperature 0** — the
        # same call measured 184 tokens once and over 512 the next. A test with a tight budget
        # tests the budget, not the backend.
        limits=StageLimits(seed=seed, max_output_tokens=2048, temperature=0.0),
        correlation=Correlation(project_id="01LIVE"),
        model_hint=model_hint,
        prompt_id=prompt.prompt_id,
        prompt_version=prompt.version,
        prompt_sha256=prompt.sha256,
    )


def test_a_hello_stage_produces_real_text_through_ollama(ollama: InferenceBackend) -> None:
    """P2 AC1, first half — through the gateway, because that is how every stage reaches a model.

    Not `ollama.generate` directly: the choke point is where serialisation, the model switch and
    the bounded empty-generation retry live, so a test that went round it would be testing a path
    the product does not have.
    """
    settings = load_settings().settings
    gateway = InferenceGateway(
        backend=ollama, bindings=settings.models.stages, execution=settings.execution
    )
    result = gateway.run(_hello())
    assert result.text.strip()
    assert result.model is not None
    assert result.model.provider_kind == "ollama"
    assert result.model.artifact_digest, "Ollama discloses a digest; provenance records it"
    assert result.usage.output_tokens > 0
    assert result.timing.duration_ms is not None
    assert not result.truncated, "the budget cut the answer off; raise it rather than accept it"


def test_the_same_stage_runs_against_the_fake_at_the_same_seed() -> None:
    """P2 AC1, second half: identical text through the fake, twice, from the same seed."""
    request = _hello(model_hint="gemma4:12b")
    first = FakeBackend(seed=7).generate(request)
    second = FakeBackend(seed=7).generate(request)
    assert first.text == second.text
    assert first.text.strip()


def test_one_model_at_a_time_across_a_real_switch(ollama: InferenceBackend) -> None:
    """ADR-0038's live proof: polled throughout, not sampled once at the end."""
    settings = load_settings().settings
    gateway = InferenceGateway(
        backend=ollama, bindings=settings.models.stages, execution=settings.execution
    )

    samples: list[tuple[float, tuple[str, ...]]] = []
    stop = threading.Event()

    def poll() -> None:
        while not stop.is_set():
            samples.append((time.monotonic(), tuple(gateway.resident_models())))
            time.sleep(0.5)

    watcher = threading.Thread(target=poll, daemon=True)
    watcher.start()
    try:
        stages: tuple[StageId, ...] = ("draft", "critique", "draft")
        for stage in stages:
            gateway.run(
                StageRequest(
                    stage=stage,
                    system="You are terse.",
                    user="Reply with one short sentence about local inference.",
                    limits=StageLimits(max_output_tokens=256, temperature=0.0),
                    correlation=Correlation(project_id="01LIVE"),
                )
            )
    finally:
        stop.set()
        watcher.join(timeout=5)

    over_one = [(at, models) for at, models in samples if len(models) > 1]
    assert over_one == [], f"more than one model was resident: {over_one}"
    assert len(samples) >= 3, "the poller did not sample often enough to prove anything"
    assert any(models for _, models in samples), "nothing was ever resident; nothing was tested"
    assert len(gateway.switches) == 3, "three stages with alternating bindings means three switches"
    assert all(switch.unloaded for switch in gateway.switches[1:])


def test_a_budget_too_small_for_a_thinking_model_is_reported_as_truncation(
    ollama: InferenceBackend,
) -> None:
    """The finding that made this test exist, pinned so a later change cannot hide it.

    A thinking model spends output tokens on reasoning before it emits a word. With a small budget
    it returns **empty text** and `finish_reason="length"` — an HTTP success carrying nothing. The
    workflow must be able to tell that from a model that answered briefly, or the repair loop will
    spend its attempts rewriting a prompt that was never the problem.

    Measured on the reference machine: at a 64-token budget, always empty; at 512, empty on one
    run and 184 tokens of thinking plus a full answer on another. The variance is the reason this
    asserts on the *reported* outcome rather than on a token count.
    """
    tiny = ollama.generate(
        StageRequest(
            stage="draft",
            system="You are terse.",
            user="Say hello.",
            limits=StageLimits(seed=7, max_output_tokens=32, temperature=0.0),
            correlation=Correlation(project_id="01LIVE"),
            model_hint="gemma4:12b",
        )
    )
    assert tiny.truncated
    assert tiny.text.strip() == ""
