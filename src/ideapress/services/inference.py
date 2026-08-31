"""ideapress.services.inference — the one door to a model.

**There is no second door.** The CLI, the web route and the stage runner all reach a model through
:meth:`InferenceGateway.run`, and that method serialises. This is ADR-0038's first obligation, and
it is written as one class with one lock rather than as a convention, because M5's lesson was
precise: LoadCoach's synchronous path bypassed the circuit breaker its queue honoured, and nothing
noticed until a verification looked for a second entry point.

Two things happen here and nowhere else:

1. **Serialisation.** One generation is in flight at a time. `execution.max_concurrent_stages`
   above 1 is refused at startup (see :mod:`ideapress.config`), so the semaphore is always 1 — it
   is written as a semaphore anyway so that the invariant is visible in the code rather than
   implied by a default.
2. **Unload before switching.** When the incoming stage's binding names a different model from the
   resident one, the outgoing model is unloaded *before* the incoming one loads. On a 16 GB card
   holding a 10.7 GB model, loading a 7.6 GB second one without unloading first is 18 GB of demand
   that Ollama satisfies by degrading to CPU or by failing — silently, with no error IdeaPress
   could raise.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from ideapress.domain.inference import StageRequest, StageResult
from ideapress.errors import ContextLimitExceeded, ModelNotConfigured
from ideapress.observability.logging import correlation

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from ideapress.config import ExecutionSettings, StageBindings
    from ideapress.domain.inference import InferenceBackend, StageEvent
    from ideapress.domain.stages import StageId

__all__ = ["InferenceGateway", "ModelSwitch", "resolve_binding"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ModelSwitch:
    """One model change: what was evicted, what was loaded, and what it cost.

    Recorded on the attempt as a degradation, because on a single-GPU machine a switch is a full
    reload and the user is entitled to see what the two-model default is costing them
    (workflows §6.2).
    """

    from_model: str | None
    to_model: str
    unloaded: bool
    unload_ms: float = 0.0


def resolve_binding(bindings: StageBindings, stage: StageId) -> str:
    """Return the model reference bound to ``stage``.

    Args:
        bindings: The resolved `[models.stages]` section.
        stage: A model-using stage identifier.

    Returns:
        The binding, as ``provider/name``.

    Raises:
        ModelNotConfigured: This stage has no binding. Names the stage and the setting, because a
            configuration error the user cannot locate is a configuration error twice.
    """
    reference = getattr(bindings, stage, None)
    if not reference:
        message = (
            f"No model is bound to the {stage!r} stage. Set models.stages.{stage} in your "
            "configuration, or run `ideapress config init` for an example."
        )
        raise ModelNotConfigured(
            message, details={"stage": stage, "setting": f"models.stages.{stage}"}
        )
    return str(reference)


@dataclass
class InferenceGateway:
    """The single choke point every stage reaches a model through.

    Attributes:
        backend: The adapter in use.
        bindings: The `[models.stages]` section, for resolving a stage to a model.
        execution: The `[execution]` policy — concurrency and unload-on-switch.
        switches: Every model switch this process performed, newest last. Read by the
            demonstration and by the UI; never used to make a decision.
    """

    backend: InferenceBackend
    bindings: StageBindings
    execution: ExecutionSettings
    switches: list[ModelSwitch] = field(default_factory=list)
    _lock: threading.Semaphore = field(init=False, repr=False)
    _resident: str | None = field(default=None, init=False, repr=False)
    _switch_lock: threading.Lock = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Build the semaphore that makes one-at-a-time a fact rather than a default."""
        self._lock = threading.Semaphore(self.execution.max_concurrent_stages)
        self._switch_lock = threading.Lock()

    @property
    def resident_model(self) -> str | None:
        """Which model this gateway last loaded, as far as it knows."""
        return self._resident

    def model_for(self, stage: StageId) -> str:
        """The model bound to ``stage``, honouring an explicit hint is *not* this function's job.

        Raises:
            ModelNotConfigured: The stage has no binding.
        """
        return resolve_binding(self.bindings, stage)

    def _prepare(self, request: StageRequest) -> str:
        """Resolve the model for this request and make it the only resident one.

        Args:
            request: The stage request about to run.

        Returns:
            The model reference the request will use.

        Raises:
            ModelNotConfigured: The stage has no binding and no hint was given.

        A hint from the caller wins over the binding — that is what `model_hint` is for in
        standalone mode (workflows §6.1) — but it goes through the same switch, so there is no way
        to load a second model by supplying a hint.
        """
        target = request.model_hint or self.model_for(request.stage)
        if not self.execution.unload_before_model_switch:
            self._resident = target
            return target
        with self._switch_lock:
            previous = self._resident
            if previous == target:
                return target
            unloaded = False
            unload_ms = 0.0
            if previous is not None:
                from time import perf_counter

                started = perf_counter()
                unloaded = self.backend.unload(previous)
                unload_ms = (perf_counter() - started) * 1000.0
                logger.info(
                    "inference.model_unloaded",
                    extra={"model_canonical_id": previous, "duration_ms": round(unload_ms, 1)},
                )
            self.switches.append(
                ModelSwitch(
                    from_model=previous, to_model=target, unloaded=unloaded, unload_ms=unload_ms
                )
            )
            self._resident = target
            return target

    def run(self, request: StageRequest) -> StageResult:
        """Run one bounded model task, serialised, with the model switched if it must be.

        Args:
            request: The stage request. Its ``stage`` is IdeaPress vocabulary; the adapter
                translates.

        Returns:
            The result, carrying everything provenance needs and any degradations that applied.
            A `model_switch` degradation is added when this call evicted a model to make room.

        Raises:
            ModelNotConfigured: The stage has no binding.
            BackendUnavailable: The backend did not answer.
            ProviderTimeout: It accepted the request and did not answer in time.

        **This is the only function in IdeaPress that calls a backend's `generate`.** A test walks
        the source and asserts that; adding a second call site is what the M5 lesson forbids.
        """
        with self._lock:
            target = self._prepare(request)
            switch = self.switches[-1] if self.switches else None
            # The adapter is told which model to use; it never reads `[models.stages]` itself.
            # One resolver means one place a binding can be wrong, and it is this one.
            request = replace(request, model_hint=target)
            with correlation(
                project_id=request.correlation.project_id,
                unit_id=request.correlation.unit_id,
                stage=request.stage,
                attempt=request.correlation.attempt,
                backend=self.backend.name,
                model_canonical_id=target,
            ):
                result = self.backend.generate(request)
                result = self._retry_empty_truncation(request, result)
        return self._annotate(result, switch=switch, target=target)

    def _retry_empty_truncation(self, request: StageRequest, result: StageResult) -> StageResult:
        """Retry **once** when the model returned nothing at all after exhausting its budget.

        Args:
            request: The request that produced ``result``.
            result: What came back.

        Returns:
            ``result`` unchanged, or the retry's result carrying an
            ``empty_generation_retried`` degradation.

        This is a **transport-level** retry, not a content one, and the distinction is the whole
        justification: the provider returned an empty body, so there is nothing to validate, nothing
        to repair and nothing a model decided. It is bounded at exactly one attempt, performed by
        Python, and recorded — so it cannot become the unbounded loop workflows §11 forbids, and it
        does not consume the stage's `max_attempts_per_stage`, which exist for defects in content
        that exists.

        It is here because the measured behaviour of a shipped default makes it necessary.
        `gemma4:12b` — spec §12's binding for `draft`, the most important stage in the product —
        enters a runaway reasoning loop on the **first generation after a cold load** and returns
        empty text with ``finish_reason="length"``; the next call is clean. Reproduced three times
        in three on the reference machine, and not observed at all for `qwen3.5:9b-q8_0`. ADR-0038
        makes IdeaPress unload before every model switch, so a cold load is guaranteed on every
        alternation between the two default models — which means that without this, the documented
        default configuration cannot draft a single unit.
        """
        if result.text.strip() or not result.truncated:
            return result
        logger.warning(
            "inference.empty_generation_retried",
            extra={"model_canonical_id": request.model_hint, "stage": request.stage},
        )
        retried = self.backend.generate(request)
        if not retried.text.strip() and retried.truncated:
            # The retry produced nothing either, so this is not a cold load: the budget is genuinely
            # too small for this model's reasoning on this task. Say that, with the number. Letting
            # it through would surface as "Expecting value: line 1 column 1" from a JSON parser,
            # which sends the reader looking for a malformed answer when there was no answer at all.
            message = (
                f"The model produced no text at all in {request.limits.max_output_tokens} output "
                f"tokens, twice, for the {request.stage!r} stage. A reasoning model spends output "
                "tokens on thinking before its first word; this budget was exhausted before it "
                "reached one. Raise the stage's output budget."
            )
            raise ContextLimitExceeded(
                message,
                details={
                    "stage": request.stage,
                    "max_output_tokens": request.limits.max_output_tokens,
                    "output_tokens": retried.usage.output_tokens,
                    "model_canonical_id": request.model_hint,
                },
            )
        return replace(
            retried,
            degradations=(
                *retried.degradations,
                "empty_generation_retried: the model exhausted its output budget without emitting "
                "any text, which a cold load of some models does; retried once",
            ),
        )

    def stream(self, request: StageRequest) -> Iterator[StageEvent]:
        """Stream one bounded model task, serialised the same way.

        Holds the semaphore for the whole iteration: a generator that released it at first yield
        would let a second stage start while the first is still producing tokens, which is the
        same failure with extra steps.
        """
        with self._lock:
            target = self._prepare(request)
            request = replace(request, model_hint=target)
            with correlation(
                project_id=request.correlation.project_id,
                unit_id=request.correlation.unit_id,
                stage=request.stage,
                attempt=request.correlation.attempt,
                backend=self.backend.name,
                model_canonical_id=target,
            ):
                yield from self.backend.stream(request)

    def _annotate(
        self, result: StageResult, *, switch: ModelSwitch | None, target: str
    ) -> StageResult:
        """Add the backend name and any model-switch degradation to a result."""
        degradations = result.degradations
        if switch is not None and switch.to_model == target and switch.from_model is not None:
            degradations = (
                *degradations,
                f"model_switch: unloaded {switch.from_model} to load {target} "
                f"({switch.unload_ms:.0f} ms)",
            )
        return replace(
            result, backend=result.backend or self.backend.name, degradations=degradations
        )

    def resident_models(self) -> Sequence[str]:
        """Ask the backend what it is actually holding.

        This is the observation the invariant rests on. ADR-0038's proof is a live test that polls
        this across a real stage switch and asserts it never returns more than one entry — never
        "the unload call returned, so it must have been fine".
        """
        return self.backend.resident_models()
