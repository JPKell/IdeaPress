"""ideapress.services.backends — building the configured adapter, and reporting on it.

The factory is the only place a mode string becomes an adapter. `[inference] mode` selects; the
fallback is applied by the gateway's caller, never inside an adapter.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mirrorwall import ComponentHealth, ComponentStatus

from ideapress.errors import BackendUnavailable

if TYPE_CHECKING:
    from ideapress.config import InferenceSettings, Settings
    from ideapress.domain.inference import InferenceBackend

__all__ = ["backend_health_component", "build_backend", "describe_backends", "test_backend"]


def build_backend(settings: Settings, *, mode: str | None = None) -> InferenceBackend:
    """Build the adapter for a configured mode.

    Args:
        settings: The validated configuration.
        mode: Override `[inference] mode`, for building a fallback or testing another adapter.

    Returns:
        The adapter.

    Raises:
        ConfigurationError: ``mode`` names no adapter this build ships. Cannot normally happen —
            `Settings` validates the field — but a fallback mode read from elsewhere can.
    """
    from baseaicore import ConfigurationError

    selected = mode or settings.inference.mode
    if selected == "ollama":
        from ideapress.infrastructure.backends.ollama import OllamaBackend

        return OllamaBackend(settings.inference.ollama)
    if selected == "openai_compatible":
        from ideapress.infrastructure.backends.openai_compatible import OpenAICompatibleBackend

        return OpenAICompatibleBackend(settings.inference.openai_compatible)
    if selected == "fake":
        from ideapress.infrastructure.backends.fake import FakeBackend

        return FakeBackend()
    if selected == "loadcoach":
        message = (
            "The LoadCoach backend is not built yet; it arrives in Phase 7. Use "
            "inference.mode = 'ollama' or 'openai_compatible'."
        )
        raise ConfigurationError(message, details={"field": "inference.mode", "mode": selected})
    message = f"{selected!r} is not an inference mode this build ships."
    raise ConfigurationError(message, details={"field": "inference.mode", "mode": selected})


def backend_health_component(backend: InferenceBackend | None) -> ComponentHealth:
    """Report the ``backend`` health component, naming which one and whether it answers.

    An unreachable backend is **degraded**, never unavailable: opening projects and exporting
    committed content need no model, so calling it an outage would misreport a working
    application (spec §20 AC7).
    """
    if backend is None:
        return ComponentHealth(
            name="backend",
            status=ComponentStatus.NOT_CONFIGURED,
            detail="No inference backend is configured.",
        )
    health = backend.health()
    status = (
        ComponentStatus.OK
        if health.status == "ok"
        else ComponentStatus.NOT_CONFIGURED
        if health.status == "not_configured"
        else ComponentStatus.DEGRADED
    )
    detail = (
        f"{health.backend} at {health.base_url or 'no endpoint'}: {health.detail}"
        if health.detail
        else f"{health.backend} at {health.base_url or 'no endpoint'} is {health.status}"
    )
    return ComponentHealth(
        name="backend",
        status=status,
        detail=detail,
        data={
            "mode": health.backend,
            "reachable": health.status == "ok",
            "is_remote": health.is_remote,
            "model_count": health.model_count,
        },
    )


def describe_backends(settings: Settings) -> list[dict[str, object]]:
    """Describe every configured backend for ``GET /backends`` and ``ideapress backend list``.

    Returns:
        One entry per mode the configuration names — the selected one and the fallback — each with
        its reachability, its capabilities and an **egress flag** for a remote one. Risk S4: the
        user is told plainly, per backend, where their content would go.
    """
    inference: InferenceSettings = settings.inference
    modes: list[str] = [inference.mode]
    if inference.fallback_mode and inference.fallback_mode != inference.mode:
        modes.append(inference.fallback_mode)

    described: list[dict[str, object]] = []
    for mode in modes:
        entry: dict[str, object] = {
            "mode": mode,
            "selected": mode == inference.mode,
            "fallback": mode == inference.fallback_mode,
            "pinned": inference.pin_backend,
        }
        try:
            backend = build_backend(settings, mode=mode)
        except Exception as exc:  # noqa: BLE001 — a mode that will not build is a reported state
            entry |= {"available": False, "detail": str(exc), "is_remote": None}
            described.append(entry)
            continue
        health = backend.health()
        capabilities = backend.capabilities()
        entry |= {
            "available": health.status == "ok",
            "status": health.status,
            "detail": health.detail,
            "base_url": health.base_url,
            "is_remote": health.is_remote,
            "egress": health.is_remote,
            "model_count": health.model_count,
            "latency_ms": health.latency_ms,
            "capabilities": {
                "streaming": capabilities.streaming,
                "structured_output": capabilities.structured_output,
                "json_mode": capabilities.json_mode,
                "token_counts": capabilities.token_counts,
                "model_selection": capabilities.model_selection,
                "discloses_model": capabilities.discloses_model,
                "residency_control": capabilities.residency_control,
            },
        }
        described.append(entry)
    return described


def test_backend(settings: Settings, *, mode: str | None = None) -> dict[str, object]:
    """Round-trip one backend: health, latency and its model list (api.md §5).

    Returns:
        What was observed. A backend that does not answer produces a report saying so rather than
        an exception, because "is it up?" is the question being asked.
    """
    from time import perf_counter

    backend = build_backend(settings, mode=mode)
    started = perf_counter()
    health = backend.health()
    report: dict[str, object] = {
        "mode": backend.name,
        "status": health.status,
        "detail": health.detail,
        "base_url": health.base_url,
        "is_remote": health.is_remote,
        "version": health.version,
    }
    try:
        models = backend.list_models()
        report["models"] = [model.name for model in models]
        report["model_count"] = len(models)
    except BackendUnavailable as exc:
        report["models"] = []
        report["model_count"] = 0
        report["detail"] = str(exc)
    report["latency_ms"] = round((perf_counter() - started) * 1000.0, 2)
    return report
