"""ideapress.infrastructure.backends._modelrack — the one translation between two vocabularies.

Every adapter built on ModelRack shares this: turning a :class:`~ideapress.domain.inference.
StageRequest` into a ``modelrack.GenerationRequest`` and a ``GenerationResult`` back into a
:class:`~ideapress.domain.inference.StageResult`. Written once so that `OllamaBackend` and
`OpenAICompatibleBackend` cannot drift into two dialects of the same translation.

This module and its siblings in this package are the **only** ones in IdeaPress permitted to import
`modelrack` (risk T9, P2 AC2, asserted by an AST test).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final

from baseaicore import ModelIdentity as SuiteModelIdentity
from baseaicore import ProviderKind, RuntimeProfile, is_supported
from modelrack import (
    ContextLimitExceeded as MrContextLimitExceeded,
)
from modelrack import (
    GenerationRequest,
    Message,
    ProviderStatus,
    ResponseFormat,
    ResponseFormatKind,
    Role,
    SamplingParameters,
)
from modelrack import (
    ModelNotFound as MrModelNotFound,
)
from modelrack import (
    ProviderError as MrProviderError,
)
from modelrack import (
    ProviderRejected as MrProviderRejected,
)
from modelrack import (
    ProviderTimeout as MrProviderTimeout,
)
from modelrack import (
    ProviderUnavailable as MrProviderUnavailable,
)

from ideapress.domain.inference import (
    BackendHealth,
    BackendModel,
    BackendStatus,
    ModelIdentity,
    StageEvent,
    StageResult,
    Timing,
    TokenUsage,
)
from ideapress.errors import (
    BackendUnavailable,
    ContentRejected,
    ContextLimitExceeded,
    ModelNotConfigured,
    ProviderTimeout,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from modelrack import GenerationResult, ResidentModel, StreamEvent

    from ideapress.domain.inference import StageRequest

__all__ = [
    "build_generation_request",
    "reference_to_identity",
    "resident_ids",
    "to_backend_health",
    "to_backend_models",
    "to_stage_events",
    "to_stage_result",
    "translate_errors",
]

logger = logging.getLogger(__name__)

_STATUS_MAP: Final[dict[ProviderStatus, BackendStatus]] = {
    ProviderStatus.OK: "ok",
    ProviderStatus.DEGRADED: "degraded",
    ProviderStatus.UNAVAILABLE: "unavailable",
}

# Phrases a model uses when it declines the task rather than failing at it. A refusal is a distinct
# outcome (spec §13, risk M1) and must not be scored as a workflow failure — but it is a heuristic
# over free text, so it only fires on a *short* answer: a long essay that happens to contain
# "I cannot" is an essay, not a refusal.
_REFUSAL_MARKERS: Final[tuple[str, ...]] = (
    "i cannot assist",
    "i can't assist",
    "i cannot help with",
    "i can't help with",
    "i'm unable to help",
    "i am unable to help",
    "i cannot comply",
    "i won't be able to",
    "i must decline",
)
_REFUSAL_MAX_CHARS: Final = 400


def _provider_kind(value: str) -> ProviderKind:
    """Read a configured provider kind, refusing one the suite does not define.

    Args:
        value: The prefix of a `[models.stages]` binding, e.g. ``ollama``.

    Returns:
        The enum member.

    Raises:
        ModelNotConfigured: ``value`` names no provider kind BaseAiCore defines. Names what was
            written and what is available, because "ollam/gemma4:12b" is a typo a user can fix and
            a silent fallback to some default provider is one they cannot even see.
    """
    try:
        return ProviderKind(value)
    except ValueError as exc:
        message = (
            f"{value!r} is not a provider kind. The suite defines: "
            f"{', '.join(kind.value for kind in ProviderKind)}."
        )
        raise ModelNotConfigured(message, details={"provider_kind": value}) from exc


def reference_to_identity(reference: str, *, default: str) -> SuiteModelIdentity:
    """Public form of :func:`_reference_to_identity`, for adapters that need an identity alone."""
    return _reference_to_identity(reference, default_kind=_provider_kind(default))


def _reference_to_identity(reference: str, *, default_kind: ProviderKind) -> SuiteModelIdentity:
    """Turn a ``provider/name`` binding into the suite's model identity.

    Args:
        reference: A `[models.stages]` value, e.g. ``ollama/gemma4:12b``. A bare name with no
            provider belongs to the adapter's own provider kind.
        default_kind: The adapter's own kind, used when the reference names none.

    Returns:
        The identity, with no digest — the provider fills that in when it resolves the model.

    Raises:
        ModelNotConfigured: The reference names a provider kind that does not exist.
    """
    prefix, separator, name = reference.partition("/")
    if not separator:
        return SuiteModelIdentity(provider_kind=default_kind, provider_model_name=reference)
    return SuiteModelIdentity(provider_kind=_provider_kind(prefix), provider_model_name=name)


def build_generation_request(
    request: StageRequest,
    *,
    model_reference: str,
    provider_kind: str,
    supports_structured_output: bool,
    context_size: int | None = None,
) -> tuple[GenerationRequest, tuple[str, ...]]:
    """Translate one :class:`StageRequest` into ModelRack's request.

    Args:
        request: The stage request, in IdeaPress vocabulary.
        model_reference: The resolved binding, ``provider/name`` or a bare name.
        provider_kind: This adapter's provider kind, used when the reference names none.
        supports_structured_output: Whether the backend can enforce a schema. When it cannot and
            the stage asked for one, the format is downgraded to text and a degradation is
            returned — never a silent claim that a schema was enforced (workflows §6.2).
        context_size: A runtime-profile context override, when one is configured.

    Returns:
        The ModelRack request, and the degradations the translation itself introduced.
    """
    identity = _reference_to_identity(model_reference, default_kind=_provider_kind(provider_kind))

    degradations: list[str] = []
    response_format: ResponseFormat | None = None
    if request.response_format is not None and request.response_format.kind != "text":
        if supports_structured_output:
            # ModelRack refuses a schema on `json`, and it is right to: `json` asks for valid JSON,
            # `json_schema` asks for a shape. The port refuses the same contradiction, so by here
            # a schema implies the kind that can enforce it.
            kind = ResponseFormatKind(request.response_format.kind)
            schema = (
                dict(request.response_format.schema)
                if request.response_format.schema and kind is ResponseFormatKind.JSON_SCHEMA
                else None
            )
            response_format = ResponseFormat(kind=kind, schema=schema)
        else:
            degradations.append(
                "structured_output_unavailable: the backend cannot enforce a schema, so the "
                "answer was requested as text and parsed here; no schema was enforced"
            )

    generation = GenerationRequest(
        identity=identity,
        messages=(
            Message(role=Role.SYSTEM, content=request.system),
            Message(role=Role.USER, content=request.user),
        ),
        runtime_profile=RuntimeProfile(context_size=context_size),
        sampling=SamplingParameters(
            temperature=request.limits.temperature,
            seed=request.limits.seed,
            max_output_tokens=request.limits.max_output_tokens,
        ),
        response_format=response_format,
        timeout_seconds=request.limits.timeout_seconds,
    )
    return generation, tuple(degradations)


def _ms(value: object) -> float | None:
    """Read a ModelRack ``Measurement`` as milliseconds, or ``None`` when it is UNSUPPORTED.

    ADR-0016: an unsupported measurement is a sentinel that raises rather than coercing, and it is
    never zero. A provenance record saying a stage took 0 ms when the backend reported nothing is
    a fabricated number in the one place the product asks a person to trust it.
    """
    return float(value) if is_supported(value) and isinstance(value, (int, float)) else None


def _count(value: object) -> int:
    """Read a token count, treating an unsupported one as zero *for arithmetic only*.

    Token counts feed budget arithmetic that must produce a number. A backend that reports none
    also reports ``token_counts=False`` in its capabilities, which is where the UI learns not to
    present the total as measured.
    """
    return int(value) if is_supported(value) and isinstance(value, (int, float)) else 0


def _optional_count(value: object) -> int | None:
    """Read a token count that is genuinely optional, keeping "not reported" distinct from zero."""
    return int(value) if is_supported(value) and isinstance(value, (int, float)) else None


def _detect_refusal(text: str) -> str | None:
    """Return the model's refusal, if this short answer is one.

    Args:
        text: The model's answer.

    Returns:
        The answer verbatim when it reads as a refusal, else ``None``. Refuses to classify a long
        answer as a refusal at all: a 3 000-word draft that quotes "I cannot help with" is a draft.
    """
    if len(text) > _REFUSAL_MAX_CHARS:
        return None
    lowered = text.lower()
    return text.strip() if any(marker in lowered for marker in _REFUSAL_MARKERS) else None


def to_stage_result(
    result: GenerationResult, *, backend: str, degradations: tuple[str, ...] = ()
) -> StageResult:
    """Translate ModelRack's result into the port's, keeping everything provenance needs."""
    identity = result.identity
    model = (
        ModelIdentity(
            provider_kind=identity.provider_kind.value,
            provider_model_name=identity.provider_model_name,
            artifact_digest=identity.artifact_digest,
        )
        if identity is not None
        else None
    )
    tokens = result.usage.tokens if result.usage is not None else None
    timing = result.timing
    return StageResult(
        text=result.text,
        structured=None,
        model=model,
        usage=TokenUsage(
            input_tokens=_count(tokens.input_tokens) if tokens else 0,
            output_tokens=_count(tokens.output_tokens) if tokens else 0,
            thinking_tokens=(
                _optional_count(result.usage.thinking_tokens) if result.usage is not None else None
            ),
        ),
        timing=Timing(
            duration_ms=_ms(timing.client_wall_ms) if timing else None,
            ttft_ms=_ms(timing.client_ttft_ms) if timing else None,
        ),
        backend=backend,
        degradations=degradations,
        finish_reason=result.finish_reason.value if result.finish_reason else "unknown",
        refusal_reason=_detect_refusal(result.text),
    )


def to_stage_events(
    events: Iterator[StreamEvent], *, backend: str, degradations: tuple[str, ...] = ()
) -> Iterator[StageEvent]:
    """Translate ModelRack's stream into the port's frames."""
    from modelrack import StreamCompleted, StreamFailed, ThinkingDelta, TokenDelta

    for event in events:
        if isinstance(event, TokenDelta):
            yield StageEvent(kind="token", text=event.text)
        elif isinstance(event, ThinkingDelta):
            yield StageEvent(kind="thinking", text=event.text)
        elif isinstance(event, StreamCompleted):
            yield StageEvent(
                kind="completed",
                result=to_stage_result(event.result, backend=backend, degradations=degradations),
            )
        elif isinstance(event, StreamFailed):
            yield StageEvent(
                kind="failed",
                error_code=type(event.error).__name__,
                error_message=str(event.error),
            )


def to_backend_health(health: Any, *, backend: str) -> BackendHealth:
    """Translate ModelRack's provider health into the port's.

    ``model_count`` and ``latency_ms`` are ``Measurement``: when the provider did not answer, they
    are the ``UNSUPPORTED`` sentinel, which raises rather than coercing (ADR-0016). They are
    sanitised to ``None`` here, at the boundary, because everything above this reaches ``/health``
    — and an unreachable backend is exactly the state spec §20 AC7 requires to keep working, so a
    sentinel escaping into a JSON body turns a supported condition into a 500.
    """
    return BackendHealth(
        backend=backend,
        status=_STATUS_MAP.get(health.status, "unavailable"),
        detail=health.detail or "",
        base_url=health.base_url,
        is_remote=bool(health.is_remote),
        model_count=_optional_count(health.model_count),
        latency_ms=_ms(health.latency_ms),
        version=health.provider_version,
    )


def to_backend_models(descriptors: Sequence[Any]) -> list[BackendModel]:
    """Translate ModelRack's descriptors into the port's model list."""
    models: list[BackendModel] = []
    for descriptor in descriptors:
        identity = descriptor.identity
        models.append(
            BackendModel(
                name=identity.provider_model_name,
                identity=ModelIdentity(
                    provider_kind=identity.provider_kind.value,
                    provider_model_name=identity.provider_model_name,
                    artifact_digest=identity.artifact_digest,
                ),
                size_bytes=_optional_count(getattr(descriptor, "size_bytes", None)),
                max_context=_optional_count(getattr(descriptor, "max_context", None)),
            )
        )
    return models


def resident_ids(residents: Sequence[ResidentModel]) -> list[str]:
    """Canonical identifiers of what a provider reports resident."""
    return [resident.identity.canonical_id for resident in residents]


def translate_errors(exc: Exception, *, backend: str, base_url: str | None) -> Exception:
    """Turn a ModelRack error into IdeaPress's own vocabulary.

    Args:
        exc: What the provider raised.
        backend: The adapter's name, for the error's details.
        base_url: Where it was pointed, for the error's details.

    Returns:
        The matching :mod:`ideapress.errors` exception, or ``exc`` unchanged when it is not a
        provider error at all — swallowing an unrelated exception into a backend error is how a
        bug becomes an outage report.
    """
    details = {"backend": backend, "base_url": base_url}
    if isinstance(exc, MrProviderUnavailable):
        return BackendUnavailable(
            f"The {backend} backend at {base_url} did not answer: {exc}", details=details
        )
    if isinstance(exc, MrProviderTimeout):
        return ProviderTimeout(
            f"The {backend} backend accepted the request and did not answer in time: {exc}",
            details=details,
        )
    if isinstance(exc, MrContextLimitExceeded):
        return ContextLimitExceeded(str(exc), details=details)
    if isinstance(exc, MrModelNotFound):
        return ModelNotConfigured(
            f"The {backend} backend has no such model: {exc}", details=details
        )
    if isinstance(exc, MrProviderRejected):
        return ContentRejected(str(exc), details=details)
    if isinstance(exc, MrProviderError):
        return BackendUnavailable(f"The {backend} backend failed: {exc}", details=details)
    return exc
