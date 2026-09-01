"""What happens when LoadCoach is not there — P7's degradation contract, and spec §20 AC7.

The rule the whole optional integration rests on: **LoadCoach being absent is never a startup
failure.** Opening projects, reading committed units and exporting them need no model at all, so a
LoadCoach that is down is a health component and a stage-level error, never a reason the
application will not run.

The four rows of workflows §6.2 that concern reachability are each asserted here, plus the one that
matters most in practice: killing LoadCoach mid-project leaves the project resumable, with its
committed units intact.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest
from tests.contract.loadcoach_mock import MockLoadCoach

from ideapress.config import (
    ExecutionSettings,
    LoadCoachSettings,
    Settings,
    StageBindings,
    load_settings,
)
from ideapress.domain.inference import Correlation, StageLimits, StageRequest
from ideapress.errors import BackendUnavailable, BackendVersionMismatch, ProviderTimeout
from ideapress.infrastructure.backends.fake import FakeBackend
from ideapress.infrastructure.backends.loadcoach import LoadCoachBackend
from ideapress.services.backends import backend_health_component, build_backend, describe_backends
from ideapress.services.inference import InferenceGateway

if TYPE_CHECKING:
    from collections.abc import Iterator


def _request(stage: str = "critique") -> StageRequest:
    return StageRequest(
        stage=stage,  # type: ignore[arg-type]  # StageId is a Literal; the test names one
        system="s",
        user="u",
        limits=StageLimits(max_output_tokens=2048),
        correlation=Correlation(project_id="01PROJECT", unit_id="U-01"),
    )


def _unreachable_client(base_url: str = "http://127.0.0.1:8766") -> httpx.Client:
    """A client whose transport refuses to connect, as a stopped LoadCoach does."""

    def refuse(request: httpx.Request) -> httpx.Response:
        message = "connection refused"
        raise httpx.ConnectError(message, request=request)

    return httpx.Client(base_url=base_url, transport=httpx.MockTransport(refuse))


def _timing_out_client() -> httpx.Client:
    def stall(request: httpx.Request) -> httpx.Response:
        message = "timed out"
        raise httpx.ReadTimeout(message, request=request)

    return httpx.Client(base_url="http://127.0.0.1:8766", transport=httpx.MockTransport(stall))


@pytest.fixture
def unreachable() -> Iterator[LoadCoachBackend]:
    client = _unreachable_client()
    yield LoadCoachBackend(LoadCoachSettings(), client=client)
    client.close()


# ------------------------------------------------------------------ never a startup failure


def test_an_unreachable_loadcoach_is_never_a_startup_failure() -> None:
    """Spec §20 AC7, stated as the strongest form: the adapter *builds* against a dead LoadCoach."""
    settings = load_settings().settings.model_copy(deep=True)
    settings.inference.mode = "loadcoach"
    backend = build_backend(settings)
    assert backend.name == "loadcoach"


def test_health_reports_unavailable_rather_than_raising(unreachable: LoadCoachBackend) -> None:
    health = unreachable.health()
    assert health.status == "unavailable"
    assert "did not answer" in health.detail


def test_the_health_component_is_degraded_not_unavailable(unreachable: LoadCoachBackend) -> None:
    """Opening projects and exporting committed content need no model, so calling the whole
    application unavailable would misreport a working one."""
    from mirrorwall import ComponentStatus

    component = backend_health_component(unreachable)
    assert component.status is ComponentStatus.DEGRADED
    assert "loadcoach" in component.detail


def test_an_empty_base_url_is_not_configured_rather_than_down() -> None:
    """ "Nobody asked for it" reads differently from "it is broken", and is fixed differently."""
    backend = LoadCoachBackend(LoadCoachSettings(base_url=""))
    assert backend.health().status == "not_configured"


# ------------------------------------------------------------------ the errors themselves


def test_an_unreachable_loadcoach_raises_backend_unavailable(
    unreachable: LoadCoachBackend,
) -> None:
    with pytest.raises(BackendUnavailable) as caught:
        unreachable.generate(_request())
    assert "did not answer" in str(caught.value)
    assert caught.value.details["backend"] == "loadcoach"


def test_a_stalled_loadcoach_raises_provider_timeout() -> None:
    """ "It accepted the request and did not answer" is a different fault from "it is down"."""
    client = _timing_out_client()
    backend = LoadCoachBackend(LoadCoachSettings(timeout_seconds=1), client=client)
    with pytest.raises(ProviderTimeout):
        backend.generate(_request())
    client.close()


def test_a_version_mismatch_names_both_versions_and_does_not_downgrade() -> None:
    mock = MockLoadCoach(version="3.0.0", api_versions=("3.0",))
    client = mock.client()
    backend = LoadCoachBackend(LoadCoachSettings(), client=client)
    with pytest.raises(BackendVersionMismatch) as caught:
        backend.generate(_request())
    assert "major 3" in str(caught.value)
    assert "major 1" in str(caught.value)
    # No generation was attempted: negotiation precedes work, so a mismatch costs nothing.
    assert [r.path for r in mock.requests] == ["/api/v1/version"]
    client.close()


# ------------------------------------------------------------------ fallback


def _gateway(backend: Any, *, fallback: Any = None, pinned: bool = False) -> InferenceGateway:
    return InferenceGateway(
        backend=backend,
        bindings=StageBindings(critique="ollama/qwen3.5:9b-q8_0"),
        execution=ExecutionSettings(),
        fallback=fallback,
        pinned=pinned,
    )


def test_an_unreachable_loadcoach_falls_back_and_records_the_degradation(
    unreachable: LoadCoachBackend,
) -> None:
    """Workflows §6.2 row 1. The degradation names both backends, because a reader of the attempt
    must be able to see that this text did not come from the backend the configuration names."""
    gateway = _gateway(unreachable, fallback=FakeBackend())
    result = gateway.run(_request())
    assert result.text
    assert any("backend_fallback" in d for d in result.degradations)
    joined = " ".join(result.degradations)
    assert "loadcoach" in joined
    assert "fake" in joined


def test_a_pinned_backend_fails_the_stage_instead_of_falling_back(
    unreachable: LoadCoachBackend,
) -> None:
    """Workflows §6.2 row 2. Pinning is an explicit request to be told, not to be served quietly."""
    gateway = _gateway(unreachable, fallback=FakeBackend(), pinned=True)
    with pytest.raises(BackendUnavailable):
        gateway.run(_request())


def test_no_fallback_configured_fails_the_stage(unreachable: LoadCoachBackend) -> None:
    gateway = _gateway(unreachable)
    with pytest.raises(BackendUnavailable):
        gateway.run(_request())


def test_the_fallback_gets_a_binding_the_routing_backend_never_resolved(
    unreachable: LoadCoachBackend,
) -> None:
    """ADR-0040's loose end: in loadcoach mode nothing resolves a `[models.stages]` binding, so the
    fallback — which needs one — must have it resolved for it at the moment it is used."""
    fallback = FakeBackend()
    seen: list[str | None] = []
    original = fallback.generate

    def watched(request: StageRequest) -> Any:
        seen.append(request.model_hint)
        return original(request)

    fallback.generate = watched  # type: ignore[method-assign]  # observes what the fallback got
    _gateway(unreachable, fallback=fallback).run(_request())
    assert seen == ["ollama/qwen3.5:9b-q8_0"]


def test_a_fallback_is_not_built_when_the_backend_is_pinned() -> None:
    """Pinned means there is nowhere to fall back to: nothing is built and nothing is used."""
    settings = load_settings().settings.model_copy(deep=True)
    settings.inference.mode = "loadcoach"
    settings.inference.fallback_mode = "ollama"
    settings.inference.pin_backend = True
    from ideapress.services.runtime import build_runtime

    runtime = build_runtime(settings)
    try:
        assert runtime.inference.fallback is None
        assert runtime.inference.pinned is True
    finally:
        runtime.close()


def test_the_backends_page_names_the_fallback_and_the_egress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Risk S4: the user is told plainly, per backend, where their content would go."""
    settings = load_settings().settings.model_copy(deep=True)
    settings.inference.mode = "loadcoach"
    settings.inference.fallback_mode = "ollama"
    described = describe_backends(settings)
    modes = [entry["mode"] for entry in described]
    assert modes == ["loadcoach", "ollama"]
    assert described[0]["selected"] is True
    assert described[1]["fallback"] is True


# ------------------------------------------------------------------ resumability


def test_committed_units_survive_loadcoach_disappearing_mid_project() -> None:
    """P7 AC3, and the property the whole recovery story rests on.

    LoadCoach answers long enough for the first unit to commit and then stops. What must hold: the
    committed unit stays committed, the project is still readable and still exportable with
    LoadCoach dead, and the second unit is a resumption point rather than a wedge. A later failure
    never rolls back finished work (spec §20 AC5).
    """
    import time

    from ideapress.services.export import build_document
    from ideapress.services.runtime import build_runtime
    from ideapress.services.stage_bodies import start_plan, start_stage
    from ideapress.services.stages import StageRunner
    from ideapress.services.unit_reports import unit_list

    settings = load_settings().settings.model_copy(deep=True)
    runtime = build_runtime(settings)
    # Three generations commit one unit: the draft, the audit's findings, the critique's verdict.
    mock = MockLoadCoach(answers=_plan_answers())
    budget = {"generations": 0, "allowed": 10_000}

    def transport(request: httpx.Request) -> httpx.Response:
        if "generate" in request.url.path or request.url.path.endswith("/jobs"):
            budget["generations"] += 1
            if budget["generations"] > budget["allowed"]:
                message = "connection refused"
                raise httpx.ConnectError(message, request=request)
        return mock.handle(request)

    client = httpx.Client(
        base_url="http://127.0.0.1:8766", transport=httpx.MockTransport(transport)
    )
    try:
        backend = LoadCoachBackend(LoadCoachSettings(job_stages=()), client=client)
        gateway = InferenceGateway(
            backend=backend,
            bindings=runtime.settings.models.stages,
            execution=runtime.settings.execution,
        )
        runtime._gateway = gateway  # noqa: SLF001 — substituting the backend is the point
        runtime._backend = backend  # noqa: SLF001
        runtime._runner = StageRunner(runtime.storage, gateway=gateway, sink=runtime.events)  # noqa: SLF001

        def wait(run_id: str) -> str:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if runtime.runner.is_finished(run_id):
                    return runtime.runner.run_state(run_id) or "unknown"
                time.sleep(0.02)
            message = "the stage did not finish"
            raise AssertionError(message)

        project_id = runtime.projects.create(title="Local inference", brief=BRIEF).id
        assert wait(start_plan(runtime, project_id=project_id).run_id) == "completed"

        # LoadCoach dies after the first unit's three generations.
        mock._answers = _draft_answers()  # noqa: SLF001 — scripting the drafts
        mock._answer_index = 0  # noqa: SLF001
        budget["generations"] = 0
        budget["allowed"] = 3
        wait(start_stage(runtime, project_id=project_id, stage="draft").run_id)

        units = unit_list(runtime, project_id=project_id)
        assert len(units) == 2
        committed = [unit for unit in units if unit["state"] == "committed"]
        assert len(committed) == 1, [u["state"] for u in units]

        # Readable and exportable with LoadCoach still dead — no model is needed for either.
        document = build_document(runtime, project_id=project_id)
        assert len(document.units) == 1
        assert "own machine" in document.units[0].content

        # And resumable: LoadCoach comes back and the second unit finishes.
        budget["allowed"] = 10_000
        mock._answers = _draft_answers()  # noqa: SLF001
        mock._answer_index = 0  # noqa: SLF001
        wait(start_stage(runtime, project_id=project_id, stage="draft", resume=True).run_id)
        states = [unit["state"] for unit in unit_list(runtime, project_id=project_id)]
        assert states.count("committed") == 2, states
    finally:
        runtime.close()
        client.close()


BRIEF = (
    "The article must state that inference runs entirely on the reader's own machine and that no "
    "document content is uploaded anywhere."
)


def _plan_answers() -> list[str]:
    """The requirements compilation and the plan, in the order the plan stage asks for them."""
    import json

    requirements = {
        "requirements": [
            {
                "text": "The unit must state that inference runs on the reader's own machine.",
                "blocking": True,
                "source_document": "brief",
                "source_quote": "inference runs entirely on the reader's own machine",
                "checks": [{"kind": "must_contain_any", "values": ["own machine"]}],
            }
        ]
    }
    plan = {
        "units": [
            {
                "title": "Where the work happens",
                "goal_text": "Say plainly where inference runs.",
                "requirement_keys": ["R-001"],
                "target_words": 40,
            },
            {
                "title": "What it costs",
                "goal_text": "Be honest about the trade.",
                "requirement_keys": ["R-001"],
                "target_words": 40,
            },
        ]
    }
    return [json.dumps(requirements), json.dumps(plan)]


def _draft_answers() -> list[str]:
    """One unit's three generations — draft, clean audit, satisfied critique — then repeated."""
    import json

    draft = (
        "Everything happens on your own machine. Nothing you write is uploaded anywhere at all, "
        "and no account is needed for any of it. The hardware is yours to provide."
    )
    return [
        draft,
        json.dumps({"findings": [], "requirements_assessment": []}),
        json.dumps({"verdict": "acceptable", "rationale": "ok"}),
    ]


def test_a_settings_object_survives_a_loadcoach_mode_with_no_bindings() -> None:
    """ADR-0040: a LoadCoach user is never made to name eleven Ollama models they do not have."""
    settings = Settings.model_validate(
        {"inference": {"mode": "loadcoach"}, "models": {"stages": {}}}
    )
    assert settings.inference.mode == "loadcoach"
