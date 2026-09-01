"""ADR-0039 option (b), end to end: attestation gates, silence pauses, and the opt-out holds.

The M7-20 defect was that a check-less blocking requirement was satisfied whenever the audit's
findings did not *mention* it — the model's default behaviour settled exactly the gates nothing
mechanical backstops. These tests script the three answers an audit can now give and assert what
Python does with each: an explicit ``met`` commits (labelled as a model-review guarantee),
silence pauses, and ``workflow.allow_audit_gated_requirements = false`` pauses even an attested
unit with the config named in the reason.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

import pytest

from ideapress.config import Settings, load_settings
from ideapress.infrastructure.backends.fake import FakeBackend, default_fake_script
from ideapress.services.runtime import Runtime, build_runtime

if TYPE_CHECKING:
    from collections.abc import Iterator

BRIEF = (
    "The article must state that inference runs entirely on the reader's own machine, and it "
    "must not use a marketing register anywhere in the finished text."
)
REQUIREMENTS = {
    "requirements": [
        {
            "text": "The unit must be explicit about where inference happens.",
            "blocking": True,
            "source_document": "brief",
            "source_quote": "inference runs entirely on the reader's own machine",
            "checks": [{"kind": "must_contain_any", "values": ["own machine"]}],
        },
        {
            # The M7-20 shape: blocking, qualitative, and nothing literal can settle it.
            "text": "The unit must not use a marketing register.",
            "blocking": True,
            "source_document": "brief",
            "source_quote": "must not use a marketing register anywhere",
            "checks": [],
        },
    ]
}
PLAN = {
    "units": [
        {
            "title": "Where the work happens",
            "goal_text": "Say plainly where inference runs.",
            "requirement_keys": ["R-001", "R-002"],
            "target_words": 60,
        }
    ]
}
DRAFT = (
    "Everything happens on your own machine. The model reads what you wrote, and nothing you "
    "wrote is uploaded to anyone: no account, no sync, no telemetry carrying your sentences "
    "somewhere else. The trade-off is that the hardware is yours to provide, and a laptop that "
    "runs warm will run warmer. What you get back is that the work stays where you made it."
)
ATTESTED = json.dumps(
    {
        "findings": [],
        "requirements_assessment": [{"key": "R-002", "verdict": "met"}],
    }
)
SILENT = json.dumps({"findings": []})
ACCEPTABLE = json.dumps({"verdict": "acceptable", "rationale": "meets the bar"})


def _script(*answers: str) -> FakeBackend:
    from modelrack.testing import FakeGeneration, FakeScript

    return FakeBackend(
        script=FakeScript(
            models=default_fake_script().models,
            capabilities=default_fake_script().capabilities,
            generations=tuple(FakeGeneration(text=answer) for answer in answers),
            repeat_final_generation=True,
        ),
        seed=5,
    )


@pytest.fixture
def settings() -> Settings:
    return load_settings().settings


@pytest.fixture
def runtime(settings: Settings) -> Iterator[Runtime]:
    built = build_runtime(settings)
    yield built
    built.close()


def _with(runtime: Runtime, backend: FakeBackend) -> Runtime:
    from ideapress.services.inference import InferenceGateway
    from ideapress.services.stages import StageRunner

    gateway = InferenceGateway(
        backend=backend,
        bindings=runtime.settings.models.stages,
        execution=runtime.settings.execution,
    )
    runtime._gateway = gateway  # noqa: SLF001 — substituting the backend is the point
    runtime._backend = backend  # noqa: SLF001
    runtime._runner = StageRunner(  # noqa: SLF001
        runtime.storage, gateway=gateway, sink=runtime.events
    )
    return runtime


def _wait(runtime: Runtime, task_id: str, *, timeout: float = 20.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if runtime.runner.is_finished(task_id):
            return runtime.runner.run_state(task_id) or "unknown"
        time.sleep(0.02)
    message = f"stage {task_id} did not finish"
    raise AssertionError(message)


def _planned(runtime: Runtime, *answers: str) -> str:
    _with(runtime, _script(json.dumps(REQUIREMENTS), json.dumps(PLAN)))
    project_id = runtime.projects.create(title="Local inference", brief=BRIEF).id
    from ideapress.services.stage_bodies import start_plan

    task = start_plan(runtime, project_id=project_id)
    assert _wait(runtime, task.run_id) == "completed"
    _with(runtime, _script(*answers))
    return project_id


def _draft(runtime: Runtime, project_id: str, **kwargs: Any) -> str:
    from ideapress.services.stage_bodies import start_stage

    task = start_stage(runtime, project_id=project_id, stage="draft", **kwargs)
    return _wait(runtime, task.run_id)


def _events(runtime: Runtime, project_id: str) -> list[Any]:
    from sqlalchemy import select

    from ideapress.infrastructure.db.models import StageRun as StageRunRow

    with runtime.storage.read() as session:
        run_id = session.scalars(
            select(StageRunRow.id)
            .where(StageRunRow.project_id == project_id)
            .order_by(StageRunRow.started_at.desc())
            .limit(1)
        ).one()
    return runtime.events.source(runtime.storage, run_id).records(limit=1000)


def test_an_explicit_met_attestation_commits_and_is_labelled(runtime: Runtime) -> None:
    from ideapress.services.units import load_unit, unit_history

    project_id = _planned(runtime, DRAFT, ATTESTED, ACCEPTABLE)
    assert _draft(runtime, project_id) == "completed"

    with runtime.storage.read() as session:
        assert load_unit(session, project_id, "U-01").state == "committed"
        history = unit_history(session, project_id, "U-01")
    coverage = {c["requirement_key"]: c for c in history[0]["coverage"]}
    assert coverage["R-002"]["satisfied"] is True
    assert coverage["R-002"]["satisfied_by"] == "audit"

    committed = [e for e in _events(runtime, project_id) if e.event_type == "unit.committed"]
    assert committed[0].data["model_guaranteed_requirements"] == ["R-002"]
    assert "guaranteed by model review" in committed[0].message


def test_audit_silence_pauses_instead_of_satisfying(runtime: Runtime) -> None:
    """The M7-20 defect, inverted: the model's default behaviour now settles nothing."""
    from ideapress.services.units import load_unit

    project_id = _planned(runtime, DRAFT, SILENT, ACCEPTABLE)
    assert _draft(runtime, project_id) == "completed", "the stage completes; the unit pauses"

    with runtime.storage.read() as session:
        unit = load_unit(session, project_id, "U-01")
    assert unit.state == "paused"
    assert unit.current_version_id is None, "nothing was committed on silence"
    assert unit.paused_reason is not None
    assert "R-002" in unit.paused_reason
    assert "no audit has attested" in unit.paused_reason


def test_a_not_met_or_cannot_judge_verdict_also_pauses(runtime: Runtime) -> None:
    from ideapress.services.units import load_unit

    refused = json.dumps(
        {
            "findings": [],
            "requirements_assessment": [{"key": "R-002", "verdict": "cannot_judge"}],
        }
    )
    project_id = _planned(runtime, DRAFT, refused, ACCEPTABLE)
    assert _draft(runtime, project_id) == "completed"
    with runtime.storage.read() as session:
        assert load_unit(session, project_id, "U-01").state == "paused"


def test_the_opt_out_forces_a_wholly_mechanical_gate() -> None:
    """With allow_audit_gated_requirements=false even an attested requirement cannot commit,
    and the pause names the setting instead of sending the user to a review that cannot help."""
    from ideapress.services.units import load_unit

    settings = load_settings(
        cli_overrides={"workflow": {"allow_audit_gated_requirements": False}}
    ).settings
    runtime = build_runtime(settings)
    try:
        project_id = _planned(runtime, DRAFT, ATTESTED, ACCEPTABLE)
        assert _draft(runtime, project_id) == "completed"
        with runtime.storage.read() as session:
            unit = load_unit(session, project_id, "U-01")
        assert unit.state == "paused"
        assert unit.paused_reason is not None
        assert "allow_audit_gated_requirements" in unit.paused_reason
    finally:
        runtime.close()
