"""ideapress.domain.inference — the inference port.

The only place workflow code meets a model, declared exactly as
[Workflows §6](../../../docs/apps/ideapress/workflows.md) states it. Workflow code depends on this
module and nothing else: no adapter type, no `modelrack` type, no `httpx`. Risk T9 is that
provider specifics leak upward, and P2 AC2 makes it assertable — a test walks the AST of everything
outside `infrastructure/backends/` and finds no `modelrack` import.

Everything here is a frozen value object with no behaviour beyond validation. The port is a
`Protocol`, so an adapter satisfies it structurally without inheriting from anything, and a test
double is a real implementation rather than a subclass of the thing under test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from ideapress.domain.stages import StageId

__all__ = [
    "BackendCapabilities",
    "BackendHealth",
    "BackendModel",
    "BackendStatus",
    "Correlation",
    "InferenceBackend",
    "ModelIdentity",
    "ResponseFormat",
    "StageEvent",
    "StageEventKind",
    "StageLimits",
    "StageRequest",
    "StageResult",
    "Timing",
    "TokenUsage",
]

BackendStatus = Literal["ok", "degraded", "unavailable", "not_configured"]
ResponseFormatKind = Literal["text", "json", "json_schema"]
StageEventKind = Literal["token", "thinking", "completed", "failed"]


@dataclass(frozen=True, slots=True)
class ModelIdentity:
    """Which model produced something: ``provider/name@sha256:digest``.

    IdeaPress's own spelling of the suite's model identity (ADR-0008, ADR-0024) — minimal and
    immutable, with the descriptor and the runtime profile kept as separate objects. It is a value
    object here rather than a re-export so that workflow code, which records it on every attempt,
    does not import a provider library to name what it recorded.
    """

    provider_kind: str
    provider_model_name: str
    artifact_digest: str | None = None

    @property
    def canonical_id(self) -> str:
        """``provider/name@sha256:digest``, or ``provider/name`` when no digest was disclosed."""
        base = f"{self.provider_kind}/{self.provider_model_name}"
        return f"{base}@{self.artifact_digest}" if self.artifact_digest else base


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """What one attempt cost, in tokens.

    Cost in money is never stored: prices change on the provider's schedule, so the suite persists
    usage plus a pricing hash and re-derives money at read time (ADR-0030).
    """

    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int | None = None

    @property
    def total_tokens(self) -> int:
        """Input plus output. Thinking tokens are reported separately, never folded in."""
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class Timing:
    """How long one attempt took. Units are in the names, as they are suite-wide.

    Every field is ``float | None``, and ``None`` means **the backend did not report it** — never
    "zero". ModelRack reports timings as ``Measurement``, which may be the ``UNSUPPORTED`` sentinel
    (ADR-0016), and coercing that to ``0.0`` would put a fabricated number into a provenance record
    that a person is expected to trust. IdeaPress does not carry the sentinel itself — spec §3
    forbids it any measurement role — so the adapter translates ``UNSUPPORTED`` to ``None`` and the
    UI and the exports render it as "not reported".
    """

    duration_ms: float | None = None
    ttft_ms: float | None = None
    queue_wait_ms: float | None = None


@dataclass(frozen=True, slots=True)
class ResponseFormat:
    """What shape the answer must take.

    A backend that cannot enforce a schema must say so and record a degradation rather than
    pretending it did (workflows §6.2). ``schema`` is a JSON Schema document.
    """

    kind: ResponseFormatKind = "text"
    schema: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        """Refuse a schema the kind cannot enforce.

        Raises:
            ValidationError: ``schema`` is set on a ``text`` or ``json`` format. ``json`` asks for
                *valid JSON*, not for a particular shape, so a schema attached to it would be
                silently ignored — a caller believing a contract was enforced when nothing checked
                it is the failure mode this whole application exists to refuse. Use
                ``json_schema``.
        """
        if self.schema is not None and self.kind != "json_schema":
            from baseaicore import ValidationError

            message = (
                f"A response_format of kind {self.kind!r} cannot carry a schema; it would be "
                "silently ignored. Use kind='json_schema' to enforce a shape, or drop the schema."
            )
            raise ValidationError(message, details={"kind": self.kind})


@dataclass(frozen=True, slots=True)
class StageLimits:
    """The bounds one model task runs under. Every one of them is set by Python, never by a model.

    Workflows §11: a model may not set its own retry or revision budget, and these are the
    per-attempt half of that rule.
    """

    max_output_tokens: int = 2048
    """Output tokens, **including a thinking model's reasoning**. The default is deliberately
    generous for that reason: 64 tokens is enough for a one-sentence answer from a non-thinking
    model and produces empty text from a thinking one."""
    timeout_seconds: float = 300.0
    temperature: float = 0.2
    seed: int | None = None


@dataclass(frozen=True, slots=True)
class Correlation:
    """Which unit of work this request belongs to, for provenance and for logs."""

    project_id: str
    unit_id: str | None = None
    attempt: int = 1
    round: int = 0
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class StageRequest:
    """One bounded model task.

    ``stage`` uses **IdeaPress's** vocabulary — the identifiers in workflows §2 — never a
    provider's or LoadCoach's. Translating to a backend's own names is an adapter's job and
    happens in exactly one module per adapter.
    """

    stage: StageId
    system: str
    user: str
    response_format: ResponseFormat | None = None
    limits: StageLimits = field(default_factory=StageLimits)
    model_hint: str | None = None
    correlation: Correlation = field(default_factory=lambda: Correlation(project_id=""))
    prompt_id: str | None = None
    prompt_version: str | None = None
    prompt_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class StageResult:
    """What one bounded model task produced, with everything provenance needs.

    ``model`` is ``None`` when the backend does not disclose which model answered — an honest
    ``None`` rather than a guess, because a provenance record that names the wrong model is worse
    than one that says it does not know.
    """

    text: str
    structured: Any | None = None
    model: ModelIdentity | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)
    timing: Timing = field(default_factory=Timing)
    backend: str = ""
    routing: Mapping[str, Any] | None = None
    degradations: tuple[str, ...] = ()
    finish_reason: str = "stop"
    refusal_reason: str | None = None

    @property
    def truncated(self) -> bool:
        """Whether the model ran out of output budget before finishing.

        Distinguished from a refusal and from a failure because it is neither: the model was
        working and the budget stopped it. It matters more than it looks — a thinking model spends
        output tokens on reasoning before emitting a word, so a budget that seems generous can
        return **empty text** with this set. Measured on the reference machine: `gemma4:12b`
        answering a one-sentence prompt used 130 tokens of thinking before 12 tokens of answer, so
        a 64-token budget produced nothing at all while reporting success at the HTTP level.
        """
        return self.finish_reason == "length"

    @property
    def refused(self) -> bool:
        """Whether the model declined the task.

        A refusal is a distinct outcome from a failure (spec §13, risk M1): the workflow did not
        break, and the model's own words are surfaced so the user can rephrase or change model.
        """
        return self.refusal_reason is not None


@dataclass(frozen=True, slots=True)
class StageEvent:
    """One frame of a streamed stage.

    ``token`` frames are bare on the wire (ADR-0025 §3); every other frame carries the SetSpec
    event envelope. That asymmetry is deliberate and lives in the web layer, not here.
    """

    kind: StageEventKind
    text: str = ""
    result: StageResult | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class BackendCapabilities:
    """What this backend can actually do, reported honestly.

    A backend that cannot enforce structured output says ``structured_output=False`` and the stage
    runner adds a parsing step and records the degradation — it never claims a schema was enforced
    when it was not (workflows §6.2).
    """

    streaming: bool = False
    structured_output: bool = False
    json_mode: bool = False
    token_counts: bool = False
    model_selection: bool = False
    discloses_model: bool = False
    residency_control: bool = False


@dataclass(frozen=True, slots=True)
class BackendModel:
    """One model a backend offers."""

    name: str
    identity: ModelIdentity | None = None
    size_bytes: int | None = None
    max_context: int | None = None


@dataclass(frozen=True, slots=True)
class BackendHealth:
    """Whether the backend answers, and what it is.

    ``unavailable`` is never a startup failure (spec §20 AC7): it is a health component and a
    stage-level error, because opening projects and exporting committed content need no model.
    """

    backend: str
    status: BackendStatus
    detail: str = ""
    base_url: str | None = None
    is_remote: bool = False
    model_count: int | None = None
    latency_ms: float | None = None
    version: str | None = None


@runtime_checkable
class InferenceBackend(Protocol):
    """One bounded model task. Workflow code depends on this and nothing else.

    Every method is synchronous: async lives at the HTTP edge only (ADR-0003), and a stage runs in
    a worker thread.
    """

    @property
    def name(self) -> str:
        """The configured mode this adapter implements: ``ollama``, ``openai_compatible``, …"""
        ...

    def capabilities(self) -> BackendCapabilities:
        """Report what this backend can do, without contacting it."""
        ...

    def health(self) -> BackendHealth:
        """Report whether the backend answers. Never raises; an outage is a returned status."""
        ...

    def list_models(self) -> Sequence[BackendModel]:
        """List the models this backend offers.

        Raises:
            BackendUnavailable: The backend did not answer.
        """
        ...

    def generate(self, request: StageRequest) -> StageResult:
        """Run one bounded model task to completion.

        Raises:
            BackendUnavailable: The backend did not answer.
            ProviderTimeout: It accepted the request and did not answer in time.
            ModelNotConfigured: No model is bound to this stage.
            ContextLimitExceeded: The assembled request exceeds what the model serves.
        """
        ...

    def stream(self, request: StageRequest) -> Iterator[StageEvent]:
        """Run one bounded model task, yielding frames as they arrive.

        The final frame is ``completed`` carrying the same :class:`StageResult` that
        :meth:`generate` would have returned, or ``failed`` carrying an error code.
        """
        ...

    def resident_models(self) -> Sequence[str]:
        """Which models the backend currently holds in memory.

        Returns:
            Canonical identifiers, or an empty sequence when the backend cannot report residency.
            This is how the single-model invariant is *observed* rather than assumed (ADR-0038):
            a passing unit test proves the call was made, and only this proves what was resident.
        """
        ...

    def unload(self, model_reference: str) -> bool:
        """Evict one model from memory.

        Args:
            model_reference: The model to unload, in this backend's own reference form.

        Returns:
            Whether the backend reports it unloaded anything. ``False`` from a backend that cannot
            control residency is honest, not a failure.
        """
        ...
