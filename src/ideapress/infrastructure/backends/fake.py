"""ideapress.infrastructure.backends.fake — the port over ModelRack's `FakeProvider`.

This is what makes spec §20 AC11 true: the whole default suite passes with no backend reachable and
no network. `FakeProvider` runs no model and opens no socket, and everything it produces is derived
from a script and a seed by SHA-256 — so the same pair yields byte-identical text in another
process, on another platform and under another ``PYTHONHASHSEED``. That determinism is what the
backend-parity test and the export byte-identity tests stand on.

It also carries the **capability-poor** variant the conformance suite needs from day one. The named
failure mode of P2 is a port shaped around Ollama that the OpenAI-compatible adapter cannot fit
(discovered at P6 with four phases built on top); a stub that reports no structured output, no
residency control and no model disclosure is how that is found in P2 instead.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from baseaicore import RuntimeProfile
from modelrack import ProviderError
from modelrack.testing import DEFAULT_MODEL, FULL_CAPABILITIES, FakeProvider, FakeScript

from ideapress.domain.inference import BackendCapabilities, BackendHealth
from ideapress.infrastructure.backends._modelrack import (
    build_generation_request,
    reference_to_identity,
    resident_ids,
    to_backend_health,
    to_backend_models,
    to_stage_events,
    to_stage_result,
    translate_errors,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from ideapress.domain.inference import BackendModel, StageEvent, StageRequest, StageResult

__all__ = ["CAPABILITY_POOR", "DEFAULT_FAKE_SCRIPT", "FakeBackend", "default_fake_script"]


def default_fake_script() -> FakeScript:
    """A script serving the models IdeaPress's default `[models.stages]` binds.

    Without this the fake serves ModelRack's own ``fake-model:8b-q8_0`` and a stage bound to
    ``ollama/gemma4:12b`` fails with `MODEL_NOT_CONFIGURED` — which is correct behaviour and
    useless as a substitute. The parity test's whole claim is that the *same configuration* runs
    against every backend, so the fake has to answer to the same names.

    The two models' sizes are the real ones (10.7 GB and 7.6 GB against a 16 GB card), because a
    test about the single-model invariant that used toy sizes would not be about anything.
    """
    return FakeScript(
        models=(
            replace(
                DEFAULT_MODEL,
                name="qwen3.5:9b-q8_0",
                digest="sha256:" + "9b" * 32,
                aliases=(),
                family="qwen3.5",
                size_bytes=10_700_000_000,
                vram_bytes=10_700_000_000,
            ),
            replace(
                DEFAULT_MODEL,
                name="gemma4:12b",
                digest="sha256:" + "12" * 32,
                aliases=(),
                family="gemma4",
                size_bytes=7_600_000_000,
                vram_bytes=7_600_000_000,
            ),
        ),
        # Ollama controls residency (`keep_alive: 0` and `/api/ps`), so the fake must too, or the
        # offline half of ADR-0038's proof would be testing a backend that cannot do the thing.
        capabilities=FULL_CAPABILITIES,
    )


DEFAULT_FAKE_SCRIPT = default_fake_script()
"""Module-level default, so every fake in the suite serves one set of models."""

CAPABILITY_POOR = BackendCapabilities(
    streaming=False,
    structured_output=False,
    json_mode=False,
    token_counts=False,
    model_selection=True,
    discloses_model=False,
    residency_control=False,
)
"""A deliberately capability-poor backend, for the conformance suite.

P2's named failure mode is a port shaped around Ollama that the OpenAI-compatible adapter cannot
fit — discovered at P6, with four phases built on top. Running the conformance suite against this
from P2 onward is how that is found now instead."""


class FakeBackend:
    """The inference port over `FakeProvider`: deterministic, offline, and honest about it."""

    def __init__(
        self,
        *,
        script: FakeScript | None = None,
        seed: int = 0,
        name: str = "fake",
        capabilities: BackendCapabilities | None = None,
        provider: FakeProvider | None = None,
    ) -> None:
        """Build a fake backend.

        Args:
            script: What the provider should do. ``None`` gives ModelRack's default script.
            seed: The determinism seed. The same script and seed produce byte-identical text.
            name: What this adapter calls itself in provenance records. A parity test runs the same
                workflow under several names.
            capabilities: Override the reported capabilities, to stand in for a capability-poor
                backend. ``None`` reports what the provider actually supports.
            provider: An already-built provider, when a test needs to hold the same instance.
        """
        self._name = name
        self._provider = provider or FakeProvider(
            script if script is not None else default_fake_script(), seed=seed
        )
        self._capabilities_override = capabilities
        self.unloaded: list[str] = []
        """Every unload this backend was asked to perform, in order.

        The default-path half of ADR-0038's proof reads this and asserts the unload happened
        *before* the load — a fact no assertion about `list_resident` can establish offline.
        """

    @property
    def name(self) -> str:
        """What this adapter calls itself."""
        return self._name

    def capabilities(self) -> BackendCapabilities:
        """What this fake claims it can do."""
        if self._capabilities_override is not None:
            return self._capabilities_override
        provider_capabilities = self._provider.capabilities()
        return BackendCapabilities(
            streaming=provider_capabilities.streaming,
            structured_output=provider_capabilities.structured_output,
            json_mode=provider_capabilities.json_mode,
            token_counts=provider_capabilities.token_counts,
            model_selection=True,
            discloses_model=True,
            residency_control=provider_capabilities.force_unload,
        )

    def health(self) -> BackendHealth:
        """Whether the fake answers — which it does, unless its script says otherwise."""
        try:
            return to_backend_health(self._provider.health(), backend=self._name)
        except ProviderError as exc:
            return BackendHealth(backend=self._name, status="unavailable", detail=str(exc))

    def list_models(self) -> Sequence[BackendModel]:
        """List the scripted models."""
        try:
            return to_backend_models(self._provider.list_models())
        except ProviderError as exc:
            raise translate_errors(exc, backend=self._name, base_url=None) from exc

    def _make_resident(self, model_reference: str) -> None:
        """Mirror the real backend: generating makes the model resident.

        Ollama loads a model on the first generation and keeps it until ``keep_alive`` expires, so
        a fake whose ``list_resident`` stayed empty would make the offline half of ADR-0038's proof
        vacuous — a test asserting "never more than one resident" passes trivially against a
        backend that never reports one. `FakeProvider` tracks residency only for explicit loads, so
        this performs it.
        """
        if not self.capabilities().residency_control:
            return
        try:
            self._provider.load(
                reference_to_identity(model_reference, default="fake"), RuntimeProfile()
            )
        except ProviderError:  # pragma: no cover — a script with no such model fails earlier
            pass

    def generate(self, request: StageRequest) -> StageResult:
        """Run one scripted task to completion."""
        self._make_resident(request.model_hint or "")
        generation, degradations = build_generation_request(
            request,
            model_reference=request.model_hint or "",
            provider_kind="fake",
            supports_structured_output=self.capabilities().structured_output,
        )
        try:
            result = self._provider.generate(generation)
        except ProviderError as exc:
            raise translate_errors(exc, backend=self._name, base_url=None) from exc
        return to_stage_result(result, backend=self._name, degradations=degradations)

    def stream(self, request: StageRequest) -> Iterator[StageEvent]:
        """Run one scripted task, yielding frames."""
        self._make_resident(request.model_hint or "")
        generation, degradations = build_generation_request(
            request,
            model_reference=request.model_hint or "",
            provider_kind="fake",
            supports_structured_output=self.capabilities().structured_output,
        )
        try:
            yield from to_stage_events(
                iter(self._provider.stream(generation)),
                backend=self._name,
                degradations=degradations,
            )
        except ProviderError as exc:
            raise translate_errors(exc, backend=self._name, base_url=None) from exc

    def resident_models(self) -> Sequence[str]:
        """What the fake reports resident.

        A capability-poor variant reports nothing, which is the honest answer for a backend with no
        residency query — and is why the offline half of the invariant's proof reads
        :attr:`unloaded` instead.
        """
        if not self.capabilities().residency_control:
            return ()
        try:
            return resident_ids(self._provider.list_resident())
        except ProviderError:
            return ()

    def unload(self, model_reference: str) -> bool:
        """Record and perform an unload.

        Returns:
            Whether the provider reports it unloaded anything. A capability-poor backend records
            the request and returns ``False``, which is honest rather than a failure.
        """
        self.unloaded.append(model_reference)
        if not self.capabilities().residency_control:
            return False
        try:
            return self._provider.unload(reference_to_identity(model_reference, default="fake"))
        except ProviderError:
            return False
