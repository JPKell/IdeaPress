"""ideapress.infrastructure.backends.openai_compatible — any OpenAI-compatible endpoint.

The third adapter, and the one that proves the port is not shaped around Ollama. It reports its
capabilities honestly — reduced, and named as such — and records a degradation when it was asked
for a schema it cannot enforce, rather than pretending one was applied (workflows §6.2).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from modelrack import ProviderError
from modelrack.providers.openai_compatible import OpenAICompatibleProvider

from ideapress.domain.inference import BackendCapabilities, BackendHealth
from ideapress.infrastructure.backends._modelrack import (
    build_generation_request,
    to_backend_health,
    to_backend_models,
    to_stage_events,
    to_stage_result,
    translate_errors,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from ideapress.config import OpenAICompatibleSettings
    from ideapress.domain.inference import BackendModel, StageEvent, StageRequest, StageResult

__all__ = ["OpenAICompatibleBackend"]

logger = logging.getLogger(__name__)


class OpenAICompatibleBackend:
    """The inference port over an OpenAI-compatible endpoint.

    Honest about three things it cannot do: it does not control residency (there is no unload in
    the protocol), it may not disclose which model actually answered, and structured output depends
    entirely on the server behind the URL.
    """

    name = "openai_compatible"

    def __init__(
        self,
        settings: OpenAICompatibleSettings,
        *,
        provider: OpenAICompatibleProvider | None = None,
    ) -> None:
        """Bind to a configured endpoint.

        Args:
            settings: The `[inference.openai_compatible]` section.
            provider: An already-built provider, injected by tests.
        """
        import os

        self._settings = settings
        api_key = os.environ.get(settings.api_key_env, "") if settings.api_key_env else ""
        self._provider = provider or OpenAICompatibleProvider(
            base_url=settings.base_url,
            api_key=api_key or None,
            timeout=float(settings.timeout_seconds),
        )

    def capabilities(self) -> BackendCapabilities:
        """What this endpoint can do, read from the provider and reduced where it cannot."""
        provider_capabilities = self._provider.capabilities()
        return BackendCapabilities(
            streaming=provider_capabilities.streaming,
            structured_output=provider_capabilities.structured_output,
            json_mode=provider_capabilities.json_mode,
            token_counts=provider_capabilities.token_counts,
            model_selection=True,
            discloses_model=True,
            # No unload in the protocol: this backend cannot make the single-model invariant hold
            # by itself, which is exactly the honest report ADR-0038 needs from it.
            residency_control=False,
        )

    def health(self) -> BackendHealth:
        """Whether the endpoint answers. Never raises."""
        if not self._settings.base_url:
            return BackendHealth(
                backend=self.name,
                status="not_configured",
                detail="inference.openai_compatible.base_url is empty.",
            )
        try:
            return to_backend_health(self._provider.health(), backend=self.name)
        except ProviderError as exc:
            return BackendHealth(
                backend=self.name,
                status="unavailable",
                detail=str(exc),
                base_url=self._settings.base_url,
                is_remote=True,
            )

    def list_models(self) -> Sequence[BackendModel]:
        """List the models this endpoint serves.

        Raises:
            BackendUnavailable: The endpoint did not answer.
        """
        try:
            return to_backend_models(self._provider.list_models())
        except ProviderError as exc:
            raise translate_errors(
                exc, backend=self.name, base_url=self._settings.base_url
            ) from exc

    def _request(self, request: StageRequest) -> tuple[object, tuple[str, ...]]:
        # An OpenAI-compatible server exposes one namespace with no provider prefix, so a
        # `[models.stages]` binding of `ollama/gemma4:12b` names a model this endpoint has never
        # heard of. `[inference.openai_compatible] model` is what it actually serves.
        reference = self._settings.model or (request.model_hint or "").rpartition("/")[2]
        return build_generation_request(
            request,
            model_reference=reference,
            provider_kind=self.name,
            supports_structured_output=self.capabilities().structured_output,
        )

    def generate(self, request: StageRequest) -> StageResult:
        """Run one bounded model task to completion.

        Raises:
            BackendUnavailable: The endpoint did not answer.
            ProviderTimeout: It accepted the request and did not answer in time.
        """
        generation, degradations = self._request(request)
        try:
            result = self._provider.generate(generation)  # type: ignore[arg-type]  # built above
        except ProviderError as exc:
            raise translate_errors(
                exc, backend=self.name, base_url=self._settings.base_url
            ) from exc
        return to_stage_result(result, backend=self.name, degradations=degradations)

    def stream(self, request: StageRequest) -> Iterator[StageEvent]:
        """Run one bounded model task, yielding frames as they arrive."""
        generation, degradations = self._request(request)
        try:
            yield from to_stage_events(
                iter(self._provider.stream(generation)),  # type: ignore[arg-type]  # built above
                backend=self.name,
                degradations=degradations,
            )
        except ProviderError as exc:
            raise translate_errors(
                exc, backend=self.name, base_url=self._settings.base_url
            ) from exc

    def resident_models(self) -> Sequence[str]:
        """Empty, always: the protocol has no residency query.

        An empty sequence here means "cannot observe", not "nothing is resident", and
        :meth:`capabilities` says so with ``residency_control=False`` so a caller can tell the
        two apart.
        """
        return ()

    def unload(self, model_reference: str) -> bool:
        """Refuse honestly: the protocol has no unload.

        Returns:
            ``False``, always. A backend that cannot evict must say so rather than returning
            ``True`` and leaving the caller believing the card was freed.
        """
        logger.debug("backend.unload_unsupported", extra={"model_canonical_id": model_reference})
        return False
