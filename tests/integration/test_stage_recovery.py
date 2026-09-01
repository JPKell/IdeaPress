"""M7 finding 1: one hard unit must never abandon the units after it, or wedge the project.

Three properties, each of which the M7 verification found false on the default configuration:

* a review-stage output budget exhausted on one unit **pauses that unit** and the loop continues
  (1a) — before, the ``ContextLimitExceeded`` aborted the whole stage with the remaining units
  never started;
* ``--resume`` recovers a unit that a failure left mid-review (1b) — before, a unit left in
  ``auditing`` had no arrow back into the loop and the project was unrecoverable from the CLI;
* the reset behind 1b touches nothing while a run that may own the units is alive.

Every model answer is scripted, so the assertions are about what Python did with the failure.
"""

from __future__ import annotations

import json
import os
import time
from typing import TYPE_CHECKING, Any

import pytest
from modelrack import FinishReason
from modelrack.testing import FakeGeneration, FakeScript

from ideapress.config import Settings, load_settings
from ideapress.infrastructure.backends.fake import FakeBackend, default_fake_script
from ideapress.services.runtime import Runtime, build_runtime

if TYPE_CHECKING:
    from collections.abc import Iterator

BRIEF = (
    "The article must state that inference runs entirely on the reader's own machine and that no "
    "document content is uploaded anywhere. Keep each section short."
)
REQUIREMENTS = {
    "requirements": [
        {
            "text": "The unit must state that inference runs on the reader's own machine.",
            "blocking": True,
            "source_document": "brief",
            "source_quote": "inference runs entirely on the reader's own machine",
            "checks": [{"kind": "must_contain_any", "values": ["own machine"]}],
        },
        {
            "text": "The unit must state that nothing is uploaded.",
            "blocking": True,
            "source_document": "brief",
            "source_quote": "no document content is uploaded anywhere",
            "checks": [{"kind": "must_contain_any", "values": ["uploaded"]}],
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
        },
        {
            "title": "What never leaves",
            "goal_text": "Say plainly that nothing is uploaded.",
            "requirement_keys": ["R-001", "R-002"],
            "target_words": 60,
        },
    ]
}
DRAFT_ONE = (
    "Everything happens on your own machine. The model reads what you wrote, and nothing you "
    "wrote is uploaded to anyone: no account, no sync, no telemetry carrying your sentences "
    "somewhere else. The trade-off is that the hardware is yours to provide, and a laptop that "
    "runs warm will run warmer. What you get back is that the work stays where you made it."
)
DRAFT_TWO = (
    "Nothing you write is uploaded anywhere, at any point, for any reason. The inference happens "
    "on your own machine, and when it finishes the result is already where it will stay. There "
    "is no queue on someone else's server and no copy you cannot see, which is the entire promise "
    "this arrangement makes and the only one it needs to keep."
)
NO_FINDINGS = json.dumps({"findings": []})
ACCEPTABLE = json.dumps({"verdict": "acceptable", "rationale": "meets the bar"})
EMPTY_AT_BUDGET = FakeGeneration(text="", finish_reason=FinishReason.LENGTH)
"""What the M7 blocker looked like on the wire: no text at all, budget exhausted."""


def _script(*answers: str | FakeGeneration) -> FakeBackend:
    return FakeBackend(
        script=FakeScript(
            models=default_fake_script().models,
            capabilities=default_fake_script().capabilities,
            generations=tuple(
                answer if isinstance(answer, FakeGeneration) else FakeGeneration(text=answer)
                for answer in answers
            ),
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


def _planned(runtime: Runtime, *answers: str | FakeGeneration) -> str:
    """Create a two-unit project, plan it, then swap in the drafting script."""
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


def _states(runtime: Runtime, project_id: str) -> dict[str, str]:
    from sqlalchemy import select

    from ideapress.infrastructure.db.models import Unit as UnitRow

    with runtime.storage.read() as session:
        return {
            row.unit_key: row.state
            for row in session.scalars(
                select(UnitRow).where(UnitRow.project_id == project_id)
            ).all()
        }


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


def _force_state(runtime: Runtime, project_id: str, unit_key: str, state: str) -> None:
    """Write a unit's state directly, as a crash would leave it — no transition check."""
    from sqlalchemy import select

    from ideapress.infrastructure.db.models import Unit as UnitRow

    with runtime.storage.write() as session:
        row = session.scalars(
            select(UnitRow).where(UnitRow.project_id == project_id, UnitRow.unit_key == unit_key)
        ).one()
        row.state = state


def test_a_review_budget_exhaustion_pauses_the_unit_and_the_loop_continues(
    runtime: Runtime,
) -> None:
    """M7 finding 1a. U-01's critique returns nothing twice at the budget; U-02 still commits."""
    project_id = _planned(
        runtime,
        # U-01: drafts fine, audit finds nothing, then the critique model returns empty text at
        # the budget twice — the retry cannot help, so the gateway raises. Before the fix this
        # aborted the stage; now it pauses U-01 and the loop reaches U-02.
        DRAFT_ONE,
        NO_FINDINGS,
        EMPTY_AT_BUDGET,
        EMPTY_AT_BUDGET,
        # U-02: a clean run all the way to commit.
        DRAFT_TWO,
        NO_FINDINGS,
        ACCEPTABLE,
    )
    assert _draft(runtime, project_id) == "completed", "the stage completes; only the unit pauses"

    states = _states(runtime, project_id)
    assert states["U-01"] == "paused"
    assert states["U-02"] == "committed", "the unit after the hard one must still be drafted"

    from ideapress.services.units import load_unit

    with runtime.storage.read() as session:
        unit = load_unit(session, project_id, "U-01")
        assert unit.paused_reason is not None
        assert "critique" in unit.paused_reason, "the reason names the stage"
        assert str(runtime.settings.workflow.structured_output_tokens) in unit.paused_reason, (
            "the reason names the budget"
        )

    events = _events(runtime, project_id)
    assert any(e.event_type == "unit.paused" for e in events)
    assert any(e.event_type == "unit.committed" for e in events)

    from sqlalchemy import select

    from ideapress.infrastructure.db.models import StageRun as StageRunRow

    with runtime.storage.read() as session:
        run = session.scalars(
            select(StageRunRow)
            .where(StageRunRow.project_id == project_id, StageRunRow.stage == "draft")
            .order_by(StageRunRow.started_at.desc())
        ).first()
        assert run is not None
        assert run.units_completed == 1
        assert run.units_paused == 1


def test_resume_after_a_budget_pause_finishes_the_project(runtime: Runtime) -> None:
    """Fix 1's acceptance: after the pause, ``--resume`` re-enters the unit and completes it."""
    project_id = _planned(
        runtime,
        DRAFT_ONE,
        NO_FINDINGS,
        EMPTY_AT_BUDGET,
        EMPTY_AT_BUDGET,
        DRAFT_TWO,
        NO_FINDINGS,
        ACCEPTABLE,
    )
    assert _draft(runtime, project_id) == "completed"
    assert _states(runtime, project_id) == {"U-01": "paused", "U-02": "committed"}

    _with(runtime, _script(DRAFT_ONE, NO_FINDINGS, ACCEPTABLE))
    assert _draft(runtime, project_id, resume=True) == "completed"
    assert _states(runtime, project_id) == {"U-01": "committed", "U-02": "committed"}


def test_resume_recovers_a_unit_a_failure_left_mid_review(runtime: Runtime) -> None:
    """M7 finding 1b. A stage failure strands U-01 in ``auditing``; ``--resume`` completes both.

    The stranding failure is real, not simulated: a critique that answers prose instead of JSON
    raises ``ValidationError`` past the per-unit loop and fails the stage, which is exactly the
    class of crash 1a's pause does not cover.
    """
    project_id = _planned(runtime, DRAFT_ONE, NO_FINDINGS, "not json at all")
    assert _draft(runtime, project_id) == "failed"
    assert _states(runtime, project_id) == {"U-01": "auditing", "U-02": "planned"}, (
        "the precondition: a unit stranded mid-review, the rest never started"
    )

    _with(
        runtime,
        _script(DRAFT_ONE, NO_FINDINGS, ACCEPTABLE, DRAFT_TWO, NO_FINDINGS, ACCEPTABLE),
    )
    assert _draft(runtime, project_id, resume=True) == "completed"
    assert _states(runtime, project_id) == {"U-01": "committed", "U-02": "committed"}, (
        "nothing is left in a non-terminal state"
    )

    # The reset is visible on the resuming run's event stream.
    from sqlalchemy import select

    from ideapress.infrastructure.db.models import StageRun as StageRunRow

    with runtime.storage.read() as session:
        run_ids = session.scalars(
            select(StageRunRow.id)
            .where(StageRunRow.project_id == project_id, StageRunRow.stage == "draft")
            .order_by(StageRunRow.started_at)
        ).all()
    resumed_events = runtime.events.source(runtime.storage, run_ids[-1]).records(limit=1000)
    reset = [e for e in resumed_events if e.event_type == "unit.reset"]
    assert len(reset) == 1
    assert reset[0].data["unit_key"] == "U-01"
    assert reset[0].data["previous_state"] == "auditing"


def test_the_reset_refuses_while_a_live_run_may_own_the_units(runtime: Runtime) -> None:
    """The reset never yanks a unit out from under a writer, and never guesses."""
    from ideapress.errors import StagePreconditionFailed
    from ideapress.infrastructure.db.models import StageRun as StageRunRow
    from ideapress.services.stages import boot_id
    from ideapress.services.units import reset_orphaned_units

    project_id = _planned(runtime, DRAFT_ONE)
    _force_state(runtime, project_id, "U-01", "auditing")

    with runtime.storage.write() as session:
        session.add(
            StageRunRow(
                project_id=project_id,
                stage="draft",
                state="running",
                owner_pid=os.getpid(),
                owner_boot_id=boot_id(),
            )
        )
    with pytest.raises(StagePreconditionFailed, match="alive"):
        reset_orphaned_units(runtime.storage, project_id=project_id)

    # A run with no recorded owner cannot be proven dead, so it also refuses.
    from sqlalchemy import select

    with runtime.storage.write() as session:
        run = session.scalars(select(StageRunRow).where(StageRunRow.state == "running")).one()
        run.owner_pid = None
        run.owner_boot_id = None
    with pytest.raises(StagePreconditionFailed, match="no recorded owner"):
        reset_orphaned_units(runtime.storage, project_id=project_id)

    # A provably dead owner (this boot, a PID nothing runs under) unblocks the reset.
    with runtime.storage.write() as session:
        run = session.scalars(select(StageRunRow).where(StageRunRow.state == "running")).one()
        run.owner_pid = 2**22 + 1  # above any default pid_max
        run.owner_boot_id = boot_id()
    reset = reset_orphaned_units(runtime.storage, project_id=project_id)
    assert reset == (("U-01", "auditing"),)
    assert _states(runtime, project_id)["U-01"] == "paused"


def test_the_reset_touches_nothing_that_is_not_mid_flight(runtime: Runtime) -> None:
    """Committed, paused and planned units are exactly as the reset found them."""
    from ideapress.services.units import reset_orphaned_units

    project_id = _planned(runtime, DRAFT_ONE, NO_FINDINGS, ACCEPTABLE, DRAFT_TWO)
    assert reset_orphaned_units(runtime.storage, project_id=project_id) == ()
    assert _states(runtime, project_id) == {"U-01": "planned", "U-02": "planned"}
