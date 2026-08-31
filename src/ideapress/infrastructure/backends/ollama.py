"""ideapress.infrastructure.backends.ollama — direct Ollama, the default backend.

One of three modules permitted to import `modelrack`. Everything above this package sees only the
port; a test walks the AST and proves it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from modelrack import ProviderError
from modelrack.providers.ollama import OllamaProvider

from ideapress.domain.inference import (
    BackendCapabilities,
    BackendHealth,
)
from ideapress.infrastructure.backends._modelrack import (
    build_generation_request,
    resident_ids,
    to_backend_health,
    to_backend_models,
    to_stage_events,
    to_stage_result,
    translate_errors,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from ideapress.config import OllamaSettings
    from ideapress.domain.inference import BackendModel, StageEvent, StageRequest

from ideapress.domain.inference import StageResult

__all__ = ["OllamaBackend"]

logger = logging.getLogger(__name__)


class OllamaBackend:
    """The inference port over ModelRack's Ollama provider.

    Full control over sampling, no queue, and — importantly for ADR-0038 — real residency control:
    ModelRack's Ollama adapter implements ``unload`` as ``keep_alive: 0`` and ``list_resident`` over
    ``/api/ps``, which is how the single-model invariant is both enforced and observed.
    """

    name = "ollama"

    def __init__(self, settings: OllamaSettings, *, provider: OllamaProvider | None = None) -> None:
        """Bind to a configured Ollama endpoint.

        Args:
            settings: The `[inference.ollama]` section.
            provider: An already-built provider, injected by tests. Building one opens no socket.
        """
        self._settings = settings
        self._provider = provider or OllamaProvider(
            base_url=settings.base_url, timeout=float(settings.timeout_seconds)
        )

    def capabilities(self) -> BackendCapabilities:
        """What Ollama can do, read from the provider rather than assumed."""
        provider_capabilities = self._provider.capabilities()
        return BackendCapabilities(
            streaming=provider_capabilities.streaming,
            structured_output=provider_capabilities.structured_output,
            json_mode=provider_capabilities.json_mode,
            token_counts=provider_capabilities.token_counts,
            model_selection=True,
            discloses_model=True,
            residency_control=provider_capabilities.force_unload
            and provider_capabilities.residency_query,
        )

    def health(self) -> BackendHealth:
        """Whether Ollama answers. Never raises: an outage is a status, not an exception."""
        try:
            return to_backend_health(self._provider.health(), backend=self.name)
        except ProviderError as exc:
            return BackendHealth(
                backend=self.name,
                status="unavailable",
                detail=str(exc),
                base_url=self._settings.base_url,
            )

    def list_models(self) -> Sequence[BackendModel]:
        """List the models Ollama has pulled.

        Raises:
            BackendUnavailable: Ollama did not answer.
        """
        try:
            return to_backend_models(self._provider.list_models())
        except ProviderError as exc:
            raise translate_errors(
                exc, backend=self.name, base_url=self._settings.base_url
            ) from exc

    def generate(self, request: StageRequest) -> StageResult:
        """Run one bounded model task to completion.

        Raises:
            BackendUnavailable: Ollama did not answer.
            ProviderTimeout: It accepted the request and did not answer in time.
            ContextLimitExceeded: The assembled request exceeds what the model serves.
            ModelNotConfigured: The bound model is not available from this endpoint.
        """
        generation, degradations = build_generation_request(
            request,
            model_reference=request.model_hint or "",
            provider_kind=self.name,
            supports_structured_output=self.capabilities().structured_output,
        )
        try:
            result = self._provider.generate(generation)
        except ProviderError as exc:
            raise translate_errors(
                exc, backend=self.name, base_url=self._settings.base_url
            ) from exc
        return self._with_digest(
            to_stage_result(result, backend=self.name, degradations=degradations)
        )

    def _with_digest(self, result: StageResult) -> StageResult:
        """Fill in the artifact digest Ollama's generate response omits.

        Model identity is ``provider/name@sha256:digest`` (ADR-0008, ADR-0024), and Ollama's
        ``/api/generate`` reply names the model but not its digest — so a provenance record built
        from the reply alone identifies "whatever `gemma4:12b` pointed at", which is a moving
        target. ``resolve`` reads it from the metadata ModelRack already caches with a TTL, so this
        costs one request per model per five minutes, not one per attempt.

        A failure to resolve leaves the identity as it was: an honest `provider/name` beats a
        fabricated digest, and the unit's provenance shows plainly which it got.
        """
        from dataclasses import replace

        if result.model is None or result.model.artifact_digest:
            return result
        try:
            resolved = self._provider.resolve(result.model.provider_model_name)
        except ProviderError as exc:
            logger.warning(
                "backend.digest_unresolved",
                extra={"model_canonical_id": result.model.canonical_id, "detail": str(exc)},
            )
            return result
        if not resolved.artifact_digest:
            return result
        return replace(
            result, model=replace(result.model, artifact_digest=resolved.artifact_digest)
        )

    def stream(self, request: StageRequest) -> Iterator[StageEvent]:
        """Run one bounded model task, yielding frames as they arrive."""
        generation, degradations = build_generation_request(
            request,
            model_reference=request.model_hint or "",
            provider_kind=self.name,
            supports_structured_output=self.capabilities().structured_output,
        )
        try:
            yield from to_stage_events(
                iter(self._provider.stream(generation)),
                backend=self.name,
                degradations=degradations,
            )
        except ProviderError as exc:
            raise translate_errors(
                exc, backend=self.name, base_url=self._settings.base_url
            ) from exc

    def resident_models(self) -> Sequence[str]:
        """What Ollama currently holds in VRAM, from ``/api/ps``.

        Returns an empty sequence when the endpoint cannot be reached: a failure to observe is not
        evidence that nothing is resident, and the caller treats it as unknown rather than empty.
        """
        try:
            return resident_ids(self._provider.list_resident())
        except ProviderError as exc:
            logger.warning("backend.residency_unavailable", extra={"detail": str(exc)})
            return ()

    def unload(self, model_reference: str) -> bool:
        """Evict one model, as ``keep_alive: 0``.

        Args:
            model_reference: ``provider/name`` or a bare name.

        Returns:
            Whether Ollama reports it unloaded anything.
        """
        from ideapress.infrastructure.backends._modelrack import reference_to_identity

        try:
            return self._provider.unload(reference_to_identity(model_reference, default="ollama"))
        except ProviderError as exc:
            logger.warning(
                "backend.unload_failed",
                extra={"model_canonical_id": model_reference, "detail": str(exc)},
            )
            return False
