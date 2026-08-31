"""Contract: every adapter satisfies the port, in the same way.

P2's named failure mode is a port shaped around Ollama that a later adapter cannot fit —
discovered at P6, with four phases built on top. This suite is written at P2 and every adapter
joins it as it arrives, including a **deliberately capability-poor** one from the first day, so a
port that only works for a rich backend fails here rather than in four phases' time.

Parametrised over adapters, not written per adapter: an assertion that holds for one and not the
others is the shape of the defect this exists to catch. Everything here runs offline; the Ollama
adapter is exercised against a provider that answers from a script rather than a socket, and the
`-m live` suite covers the real thing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from modelrack.testing import MINIMAL_CAPABILITIES, FakeProvider, FakeScript

from ideapress.config import OllamaSettings, OpenAICompatibleSettings
from ideapress.domain.inference import (
    BackendCapabilities,
    BackendHealth,
    Correlation,
    InferenceBackend,
    ResponseFormat,
    StageLimits,
    StageRequest,
    StageResult,
)
from ideapress.domain.stages import MODEL_STAGES
from ideapress.infrastructure.backends.fake import CAPABILITY_POOR, FakeBackend, default_fake_script
from ideapress.infrastructure.backends.ollama import OllamaBackend
from ideapress.infrastructure.backends.openai_compatible import OpenAICompatibleBackend

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.contract


def _ollama_over_a_script() -> InferenceBackend:
    """The real `OllamaBackend`, over a provider that answers from a script instead of a socket.

    This is the adapter's own translation code under test — `build_generation_request`,
    `to_stage_result`, the error mapping — with only the transport substituted.
    """
    return OllamaBackend(
        OllamaSettings(),
        provider=FakeProvider(default_fake_script(), seed=3),  # type: ignore[arg-type]  # structural
    )


def _openai_over_a_script() -> InferenceBackend:
    return OpenAICompatibleBackend(
        OpenAICompatibleSettings(base_url="http://127.0.0.1:9/v1", model="gemma4:12b"),
        provider=FakeProvider(default_fake_script(), seed=3),  # type: ignore[arg-type]  # structural
    )


def _capability_poor() -> InferenceBackend:
    """A backend that can do almost nothing, and says so."""
    return FakeBackend(
        name="poor",
        capabilities=CAPABILITY_POOR,
        script=FakeScript(models=default_fake_script().models, capabilities=MINIMAL_CAPABILITIES),
    )


ADAPTERS: dict[str, Callable[[], InferenceBackend]] = {
    "fake": lambda: FakeBackend(seed=3),
    "ollama": _ollama_over_a_script,
    "openai_compatible": _openai_over_a_script,
    "capability_poor": _capability_poor,
}


@pytest.fixture(params=sorted(ADAPTERS), ids=sorted(ADAPTERS))
def adapter(request: pytest.FixtureRequest) -> Callable[[], InferenceBackend]:
    """The *factory*, not an instance: a determinism test needs to build a second one."""
    return ADAPTERS[request.param]


@pytest.fixture
def backend(adapter: Callable[[], InferenceBackend]) -> InferenceBackend:
    return adapter()


def _request(stage: str = "draft", **overrides: object) -> StageRequest:
    base = {
        "stage": stage,
        "system": "You are terse.",
        "user": "Say hello.",
        "limits": StageLimits(seed=3, max_output_tokens=64),
        "correlation": Correlation(project_id="01PROJECT", unit_id="U-01", attempt=1),
        "model_hint": "gemma4:12b",
    }
    base.update(overrides)
    return StageRequest(**base)  # type: ignore[arg-type]  # a fixed keyword set built above


def test_every_adapter_satisfies_the_port(backend: InferenceBackend) -> None:
    assert isinstance(backend, InferenceBackend)


def test_name_is_a_non_empty_string(backend: InferenceBackend) -> None:
    assert isinstance(backend.name, str)
    assert backend.name


def test_capabilities_are_reported_without_contacting_anything(
    backend: InferenceBackend,
) -> None:
    capabilities = backend.capabilities()
    assert isinstance(capabilities, BackendCapabilities)


def test_health_never_raises(backend: InferenceBackend) -> None:
    """An outage is a returned status, never an exception: `/health` must always answer."""
    health = backend.health()
    assert isinstance(health, BackendHealth)
    assert health.status in {"ok", "degraded", "unavailable", "not_configured"}
    assert health.backend == backend.name


def test_generate_returns_a_complete_result(backend: InferenceBackend) -> None:
    result = backend.generate(_request())
    assert isinstance(result, StageResult)
    assert result.text
    assert result.backend == backend.name
    assert result.usage.input_tokens >= 0
    assert result.usage.output_tokens >= 0


def test_timings_are_none_rather_than_zero_when_unreported(backend: InferenceBackend) -> None:
    """ADR-0016's rule, applied at the port: a number nobody measured is not zero."""
    timing = backend.generate(_request()).timing
    for value in (timing.duration_ms, timing.ttft_ms, timing.queue_wait_ms):
        assert value is None or isinstance(value, float)


def test_a_model_is_disclosed_exactly_when_the_backend_says_it_can(
    backend: InferenceBackend,
) -> None:
    """Honest capability reporting: `discloses_model` and the result must agree."""
    result = backend.generate(_request())
    if backend.capabilities().discloses_model:
        assert result.model is not None
        assert result.model.canonical_id
    # A backend that does not disclose may still happen to; what it may not do is claim it can
    # and then return None.


def test_asking_for_a_schema_a_backend_cannot_enforce_records_a_degradation(
    backend: InferenceBackend,
) -> None:
    """Workflows §6.2: never pretend a schema was enforced."""
    request = _request(
        response_format=ResponseFormat(kind="json_schema", schema={"type": "object"})
    )
    result = backend.generate(request)
    if not backend.capabilities().structured_output:
        assert any("structured_output_unavailable" in d for d in result.degradations)
    else:
        assert not any("structured_output_unavailable" in d for d in result.degradations)


def test_streaming_ends_in_exactly_one_terminal_frame(backend: InferenceBackend) -> None:
    if not backend.capabilities().streaming:
        pytest.skip("this backend reports no streaming")
    frames = list(backend.stream(_request()))
    assert frames, "a stream produced no frames at all"
    terminal = [f for f in frames if f.kind in {"completed", "failed"}]
    assert len(terminal) == 1
    assert frames[-1].kind in {"completed", "failed"}
    if frames[-1].kind == "completed":
        assert frames[-1].result is not None
        assert frames[-1].result.text


def test_streamed_text_equals_the_completed_result(backend: InferenceBackend) -> None:
    if not backend.capabilities().streaming:
        pytest.skip("this backend reports no streaming")
    frames = list(backend.stream(_request()))
    streamed = "".join(f.text for f in frames if f.kind == "token")
    completed = frames[-1].result
    assert completed is not None
    assert streamed == completed.text


def test_resident_models_returns_a_sequence_of_strings(backend: InferenceBackend) -> None:
    residents = backend.resident_models()
    assert all(isinstance(name, str) for name in residents)


def test_a_backend_without_residency_control_reports_no_residents(
    backend: InferenceBackend,
) -> None:
    """Empty means "cannot observe" for such a backend, and `capabilities` is how you tell."""
    if not backend.capabilities().residency_control:
        assert list(backend.resident_models()) == []


def test_unload_returns_a_bool_and_never_raises(backend: InferenceBackend) -> None:
    """A backend that cannot evict says False; it does not claim the card was freed."""
    result = backend.unload("gemma4:12b")
    assert isinstance(result, bool)
    if not backend.capabilities().residency_control:
        assert result is False


def test_list_models_is_a_sequence(backend: InferenceBackend) -> None:
    models = backend.list_models()
    assert all(model.name for model in models)


def test_the_same_seed_produces_the_same_text(
    adapter: Callable[[], InferenceBackend],
) -> None:
    """Determinism is what the parity and export-stability tests are built on.

    Two *separately constructed* backends, not one called twice: a backend that memoised its own
    answer would pass the weaker check and still be non-deterministic across processes.
    """
    first = adapter().generate(_request()).text
    second = adapter().generate(_request()).text
    assert first == second


@pytest.mark.parametrize("stage", sorted(MODEL_STAGES))
def test_every_model_using_stage_can_be_requested(backend: InferenceBackend, stage: str) -> None:
    """The port carries IdeaPress vocabulary, so every stage in workflows §2 must go through it."""
    result = backend.generate(_request(stage))
    assert result.text


def test_the_port_refuses_a_schema_the_kind_cannot_enforce() -> None:
    """A contract nothing checks is worse than no contract: refuse it where it is written."""
    from baseaicore import ValidationError

    with pytest.raises(ValidationError):
        ResponseFormat(kind="json", schema={"type": "object"})
    with pytest.raises(ValidationError):
        ResponseFormat(kind="text", schema={"type": "object"})
    assert ResponseFormat(kind="json").schema is None
    assert ResponseFormat(kind="json_schema", schema={"type": "object"}).schema is not None
