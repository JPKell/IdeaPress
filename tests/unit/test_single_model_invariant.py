"""ADR-0038: one model runs at a time, never two.

The offline half of the proof. Two properties, and they are different properties:

* **The unload happened, and it happened before the load.** Read from the fake's own record of the
  calls it received, in order. This is what a `-m live` test cannot establish cheaply.
* **Never more than one model was resident.** Read from `resident_models()` after every stage. The
  fake models Ollama's behaviour — generating makes a model resident — precisely so this assertion
  is not vacuous; a fake whose residency list stayed empty would pass it while proving nothing.

The live half is `tests/live/test_single_model_live.py`, which polls a real Ollama across a real
switch. Neither substitutes for the other.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import pytest

from ideapress.config import ExecutionSettings, StageBindings, load_settings
from ideapress.domain.inference import Correlation, StageRequest, StageResult
from ideapress.domain.stages import StageId
from ideapress.errors import ModelNotConfigured
from ideapress.infrastructure.backends.fake import FakeBackend
from ideapress.services.inference import InferenceGateway, resolve_binding

if TYPE_CHECKING:
    pass


def _gateway(*, unload_before_model_switch: bool = True) -> tuple[InferenceGateway, FakeBackend]:
    settings = load_settings().settings
    backend = FakeBackend(seed=11)
    gateway = InferenceGateway(
        backend=backend,
        bindings=settings.models.stages,
        execution=ExecutionSettings(
            max_concurrent_stages=1, unload_before_model_switch=unload_before_model_switch
        ),
    )
    return gateway, backend


def _run(gateway: InferenceGateway, stage: StageId) -> None:
    gateway.run(
        StageRequest(
            stage=stage, system="s", user="u", correlation=Correlation(project_id="01PROJECT")
        )
    )


def test_the_unload_happens_before_the_load_on_a_model_switch() -> None:
    """Mutation-checked: removing the unload from the gateway makes this fail."""
    gateway, backend = _gateway()
    _run(gateway, "draft")  # binds gemma4:12b
    assert backend.unloaded == [], "nothing was resident, so nothing should have been unloaded"

    _run(gateway, "critique")  # binds qwen3.5:9b-q8_0
    assert backend.unloaded == ["ollama/gemma4:12b"], "the outgoing model must be unloaded"
    switch = gateway.switches[-1]
    assert switch.from_model == "ollama/gemma4:12b"
    assert switch.to_model == "ollama/qwen3.5:9b-q8_0"
    assert switch.unloaded is True


def test_never_more_than_one_model_is_resident_across_a_run() -> None:
    """The observation, not the assertion: read what the backend actually holds."""
    gateway, _ = _gateway()
    observed: list[int] = []
    stages: tuple[StageId, ...] = (
        "draft",
        "critique",
        "draft",
        "audit_fast",
        "draft",
        "requirements",
    )
    for stage in stages:
        _run(gateway, stage)
        observed.append(len(list(gateway.resident_models())))
    assert observed == [1] * 6, f"resident counts across the run: {observed}"


def test_a_repeated_stage_does_not_reload_the_same_model() -> None:
    """A switch per attempt would cost a 10 GB reload for nothing."""
    gateway, backend = _gateway()
    for _ in range(4):
        _run(gateway, "draft")
    assert backend.unloaded == []
    assert len(gateway.switches) == 1


def test_turning_the_unload_off_lets_two_models_contend() -> None:
    """The configuration key does what it says, and what it costs is visible in this test.

    This is not an endorsement of the setting: it demonstrates the failure ADR-0038 exists to
    prevent, so that a reader can see the difference the default makes.
    """
    gateway, backend = _gateway(unload_before_model_switch=False)
    _run(gateway, "draft")
    _run(gateway, "critique")
    assert backend.unloaded == [], "the setting was honoured"
    assert len(list(gateway.resident_models())) == 2, "which is exactly the 18 GB problem"


def test_a_model_hint_goes_through_the_same_switch() -> None:
    """There is no second door: a hint cannot load a model behind the invariant's back."""
    gateway, backend = _gateway()
    _run(gateway, "draft")
    gateway.run(
        StageRequest(
            stage="draft",
            system="s",
            user="u",
            model_hint="ollama/qwen3.5:9b-q8_0",
            correlation=Correlation(project_id="01PROJECT"),
        )
    )
    assert backend.unloaded == ["ollama/gemma4:12b"]
    assert len(list(gateway.resident_models())) == 1


def test_only_one_generation_is_in_flight_at_a_time() -> None:
    """Two real threads, not one worker: 'does exactly one thing' gets a two-caller test."""
    gateway, backend = _gateway()
    concurrent = 0
    peak = 0
    guard = threading.Lock()
    started = threading.Event()

    original = backend.generate

    def watched(request: StageRequest) -> StageResult:
        nonlocal concurrent, peak
        with guard:
            concurrent += 1
            peak = max(peak, concurrent)
        started.set()
        # Long enough that a second caller would overlap if nothing serialised it.
        threading.Event().wait(0.05)
        with guard:
            concurrent -= 1
        return original(request)

    backend.generate = watched  # type: ignore[method-assign]  # deliberate: observes the door

    threads = [
        threading.Thread(target=_run, args=(gateway, "draft"), daemon=True) for _ in range(6)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert peak == 1, f"{peak} generations were in flight at once"


def test_streaming_holds_the_lock_for_the_whole_iteration() -> None:
    """A generator that released at first yield would let a second stage start mid-stream."""
    gateway, _ = _gateway()
    stream = gateway.stream(
        StageRequest(
            stage="draft", system="s", user="u", correlation=Correlation(project_id="01PROJECT")
        )
    )
    next(iter(stream))  # start it, do not finish it

    blocked = threading.Event()

    def second() -> None:
        _run(gateway, "draft")
        blocked.set()

    thread = threading.Thread(target=second, daemon=True)
    thread.start()
    assert not blocked.wait(0.2), "a second generation started while a stream was open"
    list(stream)  # drain, releasing the lock
    assert blocked.wait(5), "the second generation never ran after the stream closed"


def test_a_stage_with_no_binding_names_the_stage_and_the_setting() -> None:
    class Partial(StageBindings):
        pass

    bindings = Partial()
    object.__setattr__(bindings, "draft", "")
    with pytest.raises(ModelNotConfigured) as caught:
        resolve_binding(bindings, "draft")
    assert "draft" in caught.value.message
    assert "models.stages.draft" in caught.value.message


def test_the_switch_log_records_what_it_cost() -> None:
    """Workflows §6.2: the user is entitled to see what the two-model default costs them."""
    gateway, _ = _gateway()
    _run(gateway, "draft")
    _run(gateway, "critique")
    switch = gateway.switches[-1]
    assert switch.unload_ms >= 0.0
    assert isinstance(switch.unload_ms, float)


def test_the_switch_is_recorded_as_a_degradation_on_the_attempt() -> None:
    gateway, _ = _gateway()
    _run(gateway, "draft")
    result = gateway.run(
        StageRequest(
            stage="critique", system="s", user="u", correlation=Correlation(project_id="01PROJECT")
        )
    )
    assert any(d.startswith("model_switch:") for d in result.degradations)
    assert "ollama/gemma4:12b" in result.degradations[0]


def test_an_empty_truncated_generation_is_retried_exactly_once() -> None:
    """The cold-load runaway `gemma4:12b` exhibits, handled at the choke point and bounded.

    Not a content retry: the provider returned an empty body, so there is nothing to validate and
    nothing to repair. One retry, in Python, recorded — never a loop.
    """
    from dataclasses import replace as dataclass_replace

    gateway, backend = _gateway()
    calls: list[StageRequest] = []
    original = backend.generate

    def first_is_empty(request: StageRequest) -> StageResult:
        calls.append(request)
        real = original(request)
        if len(calls) == 1:
            return dataclass_replace(real, text="", finish_reason="length")
        return real

    backend.generate = first_is_empty  # type: ignore[method-assign]  # simulates the cold load

    result = gateway.run(
        StageRequest(
            stage="draft", system="s", user="u", correlation=Correlation(project_id="01PROJECT")
        )
    )
    assert len(calls) == 2, "exactly one retry"
    assert result.text.strip()
    assert any(d.startswith("empty_generation_retried:") for d in result.degradations)


def test_a_second_empty_generation_is_not_retried_again() -> None:
    """Bounded means bounded: a backend that always returns nothing does not loop forever."""
    from dataclasses import replace as dataclass_replace

    gateway, backend = _gateway()
    calls = 0
    original = backend.generate

    def always_empty(request: StageRequest) -> StageResult:
        nonlocal calls
        calls += 1
        return dataclass_replace(original(request), text="", finish_reason="length")

    backend.generate = always_empty  # type: ignore[method-assign]  # simulates a stuck model

    result = gateway.run(
        StageRequest(
            stage="draft", system="s", user="u", correlation=Correlation(project_id="01PROJECT")
        )
    )
    assert calls == 2, "one retry, then the empty result is returned for validation to reject"
    assert result.text == ""
    assert result.truncated


def test_a_non_empty_truncation_is_not_retried() -> None:
    """A model that ran out of budget mid-sentence produced content; that is the loop's problem."""
    from dataclasses import replace as dataclass_replace

    gateway, backend = _gateway()
    calls = 0
    original = backend.generate

    def truncated_but_not_empty(request: StageRequest) -> StageResult:
        nonlocal calls
        calls += 1
        return dataclass_replace(original(request), finish_reason="length")

    backend.generate = truncated_but_not_empty  # type: ignore[method-assign]  # deliberate

    gateway.run(
        StageRequest(
            stage="draft", system="s", user="u", correlation=Correlation(project_id="01PROJECT")
        )
    )
    assert calls == 1
