"""`ideapress doctor` diagnoses every documented failure mode it can settle offline (P9 AC3).

The plan's acceptance is that doctor "diagnoses every documented failure mode". Not all of spec
§13's codes are diagnosable before a stage runs — `REVISION_LIMIT_REACHED` is a property of a
revision that happened — so what is asserted here is the set that *is* settleable in advance, plus
the rule that makes the command worth running at all: an unreachable backend is a warning, never a
failure, because a command that cries failure over a survivable state is a command people stop
reading.
"""

from __future__ import annotations

import pytest

from ideapress.config import load_settings
from ideapress.services.diagnostics import diagnose


def _by_name() -> dict[str, object]:
    return {finding.name: finding for finding in diagnose()}


def test_doctor_runs_with_no_configuration_at_all() -> None:
    """Spec §20 AC1's corollary: the diagnostic works on a machine that has configured nothing."""
    findings = diagnose()
    assert findings
    assert all(finding.level in {"ok", "warn", "fail"} for finding in findings)


@pytest.mark.parametrize(
    "name",
    [
        "configuration",
        "data directory",
        "database",
        "backend",
        "prompts",
        "stage model bindings",
        "output budget",
        "bind",
        "telemetry",
    ],
)
def test_every_check_is_present(name: str) -> None:
    assert name in _by_name(), f"doctor no longer checks {name!r}"


def test_an_unreachable_backend_is_a_warning_not_a_failure() -> None:
    """The application is designed to be useful without one (spec §20 AC7). Calling that a failure
    would teach users to ignore the command."""
    findings = _by_name()
    backend = findings["backend"]
    assert backend.level in {"ok", "warn"}, backend  # type: ignore[attr-defined]


def test_an_unbound_model_stage_is_a_failure_and_names_the_stage() -> None:
    """MODEL_NOT_CONFIGURED, caught before a stage runs rather than in the middle of one."""
    from ideapress.services.diagnostics import _configuration_findings  # noqa: PLC2701

    settings = load_settings().settings.model_copy(deep=True)
    settings.models.stages.draft = ""
    findings = {f.name: f for f in _configuration_findings(settings)}
    bindings = findings["stage model bindings"]
    assert bindings.level == "fail"
    assert "draft" in bindings.detail
    assert "models.stages" in (bindings.remedy or "")


def test_loadcoach_mode_needs_no_bindings_and_says_so() -> None:
    """ADR-0040: a LoadCoach user is never made to name eleven Ollama models they do not have —
    and doctor must not tell them otherwise."""
    from ideapress.services.diagnostics import _configuration_findings  # noqa: PLC2701

    settings = load_settings().settings.model_copy(deep=True)
    settings.inference.mode = "loadcoach"
    settings.models.stages.draft = ""
    findings = {f.name: f for f in _configuration_findings(settings)}
    assert findings["stage model bindings"].level == "ok"
    assert "LoadCoach routes by task profile" in findings["stage model bindings"].detail


def test_a_low_output_budget_is_warned_about_with_the_reason() -> None:
    """The most common cause of a paused unit, named before it happens."""
    from ideapress.services.diagnostics import _configuration_findings  # noqa: PLC2701

    settings = load_settings().settings.model_copy(deep=True)
    settings.workflow.structured_output_tokens = 2048
    findings = {f.name: f for f in _configuration_findings(settings)}
    budget = findings["output budget"]
    assert budget.level == "warn"
    assert "8192" in (budget.remedy or "")


def test_the_default_budget_is_not_warned_about() -> None:
    from ideapress.services.diagnostics import _configuration_findings  # noqa: PLC2701

    findings = {f.name: f for f in _configuration_findings(load_settings().settings)}
    assert findings["output budget"].level == "ok"


def test_a_lan_bind_is_reported_with_its_allowlist() -> None:
    """The operator should be able to see, without reading the config, what they exposed."""
    from ideapress.services.diagnostics import _configuration_findings  # noqa: PLC2701

    settings = load_settings().settings.model_copy(deep=True)
    settings.server.host = "192.168.1.5"
    settings.server.allowed_hosts = ("ideapress.local",)
    findings = {f.name: f for f in _configuration_findings(settings)}
    bind = findings["bind"]
    assert "192.168.1.5" in bind.detail
    assert "ideapress.local" in bind.detail
    assert "TLS" in (bind.remedy or "")


def test_telemetry_absence_explains_what_it_means_rather_than_complaining() -> None:
    """INSUFFICIENT_VRAM is only raised where telemetry is installed. Saying so stops a user
    looking for a preflight this installation never runs."""
    findings = _by_name()
    telemetry = findings["telemetry"]
    assert telemetry.level == "ok"  # type: ignore[attr-defined]
    assert "ADR-0038" in telemetry.detail or "invariant" in telemetry.detail  # type: ignore[attr-defined]


def test_every_finding_that_fails_carries_a_remedy() -> None:
    """A diagnosis a person cannot act on is a diagnosis that wasted their time."""
    for finding in diagnose():
        if finding.level == "fail":
            assert finding.remedy, f"{finding.name} fails with no remedy"
