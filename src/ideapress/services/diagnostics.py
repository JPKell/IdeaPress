"""ideapress.services.diagnostics — ``ideapress health`` and ``ideapress doctor``.

Both answer without a network and without a server. A backend that is not reachable is a finding,
never a failure: spec §20 AC7 and AC1 make "works with nothing running" a property of the product,
and a doctor that called it a failure would teach people to ignore the command.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from mirrorwall import ComponentStatus, health_payload

from ideapress.__about__ import __version__
from ideapress.config import data_dir, load_settings, resolve_config_path
from ideapress.services.runtime import build_runtime

__all__ = ["Diagnosis", "diagnose", "health_report"]


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
    finally:
        runtime.close()
    return findings
