"""ideapress.services.diagnostics — ``ideapress health`` and ``ideapress doctor``.

Both answer without a network and without a server. A backend that is not reachable is a finding,
never a failure: spec §20 AC7 and AC1 make "works with nothing running" a property of the product,
and a doctor that called it a failure would teach people to ignore the command.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal

from mirrorwall import ComponentStatus, health_payload

from ideapress.__about__ import __version__
from ideapress.config import data_dir, load_settings, resolve_config_path
from ideapress.services.runtime import build_runtime

if TYPE_CHECKING:
    from ideapress.config import Settings
    from ideapress.services.runtime import Runtime

__all__ = ["MEASURED_REASONING_TOKENS", "Diagnosis", "diagnose", "health_report"]

MEASURED_REASONING_TOKENS: Final = 16_384
"""What a thinking model needed for reasoning alone on the widest shipped prompt (row WPF7).

`workflow.structured_output_tokens`'s default, and the figure below which `doctor` warns: a
five-unit `project_review` on `qwen3.5:9b-q8_0` spent about 11 800 output tokens before its first
word of answer. The 8192 this warned about until now is the *draft* thinking floor, which is a
different and smaller measurement."""


@dataclass(frozen=True, slots=True)
class Diagnosis:
    """One ``doctor`` finding.

    Attributes:
        name: What was checked.
        level: ``ok``, ``warn`` or ``fail``. A backend that is not running is ``warn``: the
            application works without one.
        detail: What was observed.
        remedy: What a person should do, when there is something to do.
    """

    name: str
    level: Literal["ok", "warn", "fail"]
    detail: str
    remedy: str | None = None


def health_report() -> dict[str, Any]:
    """Return the health payload without starting a server, for ``ideapress health``."""
    loaded = load_settings()
    runtime = build_runtime(loaded.settings)
    try:
        components = [check() for check in runtime.health_checkers]
    finally:
        runtime.close()
    return health_payload(application="ideapress", version=__version__, components=components)


def diagnose() -> list[Diagnosis]:
    """Run every check ``ideapress doctor`` can make without a network.

    Returns:
        One :class:`Diagnosis` per check, in the order a person should read them. A backend that
        is not reachable is a ``warn``, never a ``fail``: the application is designed to be useful
        without one, and calling that a failure would teach users to ignore the command.
    """
    from baseaicore import ConfigurationError

    findings: list[Diagnosis] = []
    config_path = resolve_config_path()
    try:
        loaded = load_settings()
    except ConfigurationError as exc:
        findings.append(
            Diagnosis(
                name="configuration",
                level="fail",
                detail=exc.message,
                remedy="Fix the named key, or run `ideapress config init` for a valid example.",
            )
        )
        return findings
    findings.append(
        Diagnosis(
            name="configuration",
            level="ok",
            detail=f"Valid ({config_path}"
            + (", read)" if loaded.config_file_used else ", not present; using defaults)"),
        )
    )

    directory = data_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".ideapress-write-probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        findings.append(
            Diagnosis(name="data directory", level="ok", detail=f"Writable: {directory}")
        )
    except OSError as exc:
        findings.append(
            Diagnosis(
                name="data directory",
                level="fail",
                detail=f"{directory} is not writable: {exc}",
                remedy="Set IDEAPRESS_DATA_DIR to a directory you own.",
            )
        )

    runtime = build_runtime(loaded.settings)
    try:
        # The bindings and limits a stage would run on, stored runtime settings applied
        # (api.md §6) — not `config.toml` alone.
        runtime.refresh_settings()
        for component in runtime.health_checkers:
            health = component()
            level: Literal["ok", "warn", "fail"] = (
                "ok"
                if health.status is ComponentStatus.OK
                else "fail"
                if health.status is ComponentStatus.UNAVAILABLE
                else "warn"
            )
            findings.append(
                Diagnosis(
                    name=health.name, level=level, detail=health.detail or health.status.value
                )
            )
        findings.extend(_configuration_findings(runtime.settings))
        findings.extend(_backend_findings(runtime, runtime.settings))
    finally:
        runtime.close()
    return findings


def _configuration_findings(settings: Settings) -> list[Diagnosis]:
    """The failure modes a person hits before a model is ever reached.

    Args:
        settings: The validated configuration.

    Returns:
        One diagnosis per documented failure mode this can settle offline. Each names the setting
        that causes it, because spec §13's list is only useful to somebody who can find the key.
    """
    from ideapress.domain.stages import MODEL_STAGES

    findings: list[Diagnosis] = []

    # MODEL_NOT_CONFIGURED — the most common first failure, and one that only surfaces when a
    # stage runs unless something looks for it first.
    if settings.inference.mode == "loadcoach":
        findings.append(
            Diagnosis(
                name="stage model bindings",
                level="ok",
                detail=(
                    "Not required: LoadCoach routes by task profile, so `[models.stages]` is "
                    "ignored unless `honour_stage_bindings` is set (ADR-0040)."
                ),
            )
        )
    else:
        unbound = sorted(
            stage for stage in MODEL_STAGES if not getattr(settings.models.stages, stage, "")
        )
        findings.append(
            Diagnosis(
                name="stage model bindings",
                level="fail" if unbound else "ok",
                detail=(
                    f"{len(unbound)} model-using stage(s) have no binding: {', '.join(unbound)}"
                    if unbound
                    else f"All {len(MODEL_STAGES)} model-using stages are bound."
                ),
                remedy=(
                    "Set the named keys under `[models.stages]`, or run `ideapress config init`."
                    if unbound
                    else ""
                ),
            )
        )

    # CONTEXT_LIMIT_EXCEEDED's usual cause, checkable before it happens: a reasoning model spends
    # output tokens on thinking before its first word, and the default is the measured floor.
    budget = settings.workflow.structured_output_tokens
    findings.append(
        Diagnosis(
            name="output budget",
            level="warn" if budget < MEASURED_REASONING_TOKENS else "ok",
            detail=f"workflow.structured_output_tokens = {budget}",
            remedy=(
                f"Below {MEASURED_REASONING_TOKENS}, a reasoning model can exhaust the budget on "
                "its own thinking and return no text at all: measured on the reference machine, a "
                "five-unit project_review spent about 11 800 output tokens reasoning before its "
                "first word. Raise it if a stage fails or a unit pauses on an empty generation."
                if budget < MEASURED_REASONING_TOKENS
                else ""
            ),
        )
    )

    # Row WPF7: the budget above is spent from the same window as the prompt and the reasoning, so
    # the figure that decides whether a stage can answer at all is the served context.
    findings.append(_served_context_finding(settings))

    # The exposure refusals happen at load time, so reaching here means they passed — say so,
    # rather than staying silent about the check that did not fire.
    loopback = settings.server.host in {"127.0.0.1", "localhost", "::1"}
    findings.append(
        Diagnosis(
            name="bind",
            level="ok",
            detail=(
                f"Loopback ({settings.server.host}:{settings.server.port})"
                if loopback
                else f"{settings.server.host}:{settings.server.port} with allowed_hosts="
                f"{list(settings.server.allowed_hosts)}"
            ),
            remedy=(
                ""
                if loopback
                else "A non-loopback bind is reachable from your network. Terminate TLS in front "
                "of it (ADR-0026 §1)."
            ),
        )
    )

    # INSUFFICIENT_VRAM is only raised where telemetry is installed; saying so stops a user
    # looking for a preflight that this installation never runs.
    try:
        # `[telemetry]` is an optional extra, so the module genuinely may not exist here.
        import sweatmeter  # type: ignore[import-not-found]  # optional extra  # noqa: F401

        telemetry = True
    except ImportError:
        telemetry = False
    findings.append(
        Diagnosis(
            name="telemetry",
            level="ok",
            detail=(
                "Installed: the VRAM preflight runs and INSUFFICIENT_VRAM can be raised."
                if telemetry
                else "Not installed (optional). No VRAM preflight runs; the one-model-at-a-time "
                "invariant holds by serialising and unloading instead (ADR-0038 §3)."
            ),
        )
    )
    return findings


def _served_context_finding(settings: Settings) -> Diagnosis:
    """Report every stage whose budgets cannot fit the context the backend serves (row WPF7).

    Args:
        settings: The effective settings.

    Returns:
        One diagnosis for the whole stage list: ``ok`` when every stage fits, ``fail`` naming the
        stages that do not, and ``warn`` when the served context is not stated — in `loadcoach` and
        `openai_compatible` mode the window belongs to the service on the other side, and on
        `ollama` with the setting at 0 IdeaPress inherits `OLLAMA_CONTEXT_LENGTH`, which it cannot
        read. The check that cannot be made is reported as not made rather than passed.
    """
    from ideapress.domain.stages import MODEL_STAGES
    from ideapress.services.context_fit import context_shortfall, served_context_tokens

    served = served_context_tokens(settings)
    if served <= 0:
        return Diagnosis(
            name="served context",
            level="warn",
            detail=(
                "Not stated for this backend, so the pre-run budget check is off."
                if settings.inference.mode != "ollama"
                else "inference.ollama.served_context_tokens = 0, so the server's own "
                "OLLAMA_CONTEXT_LENGTH applies and the pre-run budget check is off."
            ),
            remedy=(
                ""
                if settings.inference.mode != "ollama"
                else "Set it to what this endpoint serves. A prompt, its reasoning and its answer "
                "come out of one window: a served context smaller than a stage's budgets produces "
                "an empty generation, not a short answer."
            ),
        )
    problems = {
        stage: problem
        for stage in sorted(MODEL_STAGES)
        if (problem := context_shortfall(settings, stage)) is not None
    }
    return Diagnosis(
        name="served context",
        level="fail" if problems else "ok",
        detail=(
            f"{len(problems)} stage(s) need more than the {served} tokens served: "
            f"{', '.join(problems)}"
            if problems
            else f"All {len(MODEL_STAGES)} model-using stages fit the {served} tokens served."
        ),
        remedy=next(iter(problems.values()), ""),
    )


def _backend_findings(runtime: Runtime, settings: Settings) -> list[Diagnosis]:
    """Backend-shaped failure modes: version mismatch, and a task map that has drifted.

    Args:
        runtime: The built runtime.
        settings: The validated configuration.

    Returns:
        Diagnoses for the failure modes that belong to the configured backend. Nothing here is a
        ``fail``: an unreachable backend is a documented, survivable state (spec §20 AC7), and
        doctor exists to explain it rather than to condemn it.
    """
    findings: list[Diagnosis] = []
    if settings.inference.mode != "loadcoach":
        return findings

    backend = runtime.backend
    if backend is None:  # pragma: no cover — build_backend never returns None for a valid mode
        return findings

    # BACKEND_VERSION_MISMATCH and TASK_PROFILE_NOT_FOUND, both checkable before a project starts.
    try:
        unmapped = backend.unmapped_task_profiles()  # type: ignore[attr-defined]  # LoadCoach only
    except Exception as exc:  # noqa: BLE001 — an unreachable LoadCoach is a reported state
        findings.append(
            Diagnosis(
                name="loadcoach task profiles",
                level="warn",
                detail=f"Could not be checked: {exc}",
                remedy="Start LoadCoach, or switch `inference.mode` to `ollama`.",
            )
        )
        return findings

    findings.append(
        Diagnosis(
            name="loadcoach task profiles",
            level="warn" if unmapped else "ok",
            detail=(
                f"LoadCoach does not serve: {', '.join(unmapped)}"
                if unmapped
                else "Every stage's task profile exists on the running LoadCoach."
            ),
            remedy=(
                "Those stages would fail with TASK_PROFILE_NOT_FOUND mid-project. Upgrade "
                "LoadCoach, or use `inference.mode = 'ollama'`."
                if unmapped
                else ""
            ),
        )
    )
    return findings
