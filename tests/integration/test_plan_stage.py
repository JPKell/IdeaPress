"""The plan stage end to end: compile, gate, persist, and stream what happened.

Everything here runs against a scripted backend, so the model's answer is entirely under the test's
control and the assertions are about what **Python** did with it.
"""

from __future__ import annotations

import json
import threading
import time
from typing import TYPE_CHECKING, Any, cast

import pytest
from baseaicore import ValidationError

from ideapress.config import Settings, load_settings
from ideapress.infrastructure.backends.fake import FakeBackend, default_fake_script
from ideapress.services.runtime import Runtime, build_runtime

if TYPE_CHECKING:
    from collections.abc import Iterator

BRIEF = """
# Local inference for writers

Audience: working writers with no machine-learning background.

The article must state that inference runs entirely on the reader's own machine and that no
document content is uploaded anywhere. It must never promise that a local model is more accurate
than a hosted one. Keep it under 1200 words.
""".strip()

GOOD_REQUIREMENTS = {
    "requirements": [
        {
            "text": "The article must state that inference runs on the reader's own machine.",
            "blocking": True,
            "source_document": "brief",
            "source_quote": "inference runs entirely on the reader's own machine",
            "checks": [{"kind": "must_contain_any", "values": ["own machine", "locally"]}],
        },
        {
            "text": "The article must not claim a local model is more accurate than a hosted one.",
            "blocking": True,
            "source_document": "brief",
            "source_quote": "never promise that a local model is more accurate than a hosted one",
            "checks": [{"kind": "must_not_contain", "values": ["more accurate than a hosted"]}],
        },
    ]
}

GOOD_PLAN = {
    "units": [
        {
            "title": "What local inference means",
            "goal_text": "Explain running a model on your own machine.",
            "requirement_keys": ["R-001"],
            "target_words": 400,
        },
        {
            "title": "What it does and does not buy you",
            "goal_text": "Set expectations honestly.",
            "requirement_keys": ["R-002"],
            "target_words": 500,
        },
    ]
}


def _scripted(*answers: Any) -> FakeBackend:
    from modelrack.testing import FakeGeneration, FakeScript

    script = FakeScript(
        models=default_fake_script().models,
        capabilities=default_fake_script().capabilities,
        generations=tuple(
            FakeGeneration(text=json.dumps(answer) if not isinstance(answer, str) else answer)
            for answer in answers
        ),
        repeat_final_generation=True,
    )
    return FakeBackend(script=script, seed=5)


@pytest.fixture
def settings() -> Settings:
    return load_settings().settings


@pytest.fixture
def runtime(settings: Settings) -> Iterator[Runtime]:
    built = build_runtime(settings)
    yield built
    built.close()


def _with_backend(runtime: Runtime, backend: FakeBackend) -> Runtime:
    """Swap in a scripted backend, keeping one gateway so the invariant still holds."""
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


def _wait(runtime: Runtime, task_id: str, *, timeout: float = 15.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if runtime.runner.is_finished(task_id):
            return runtime.runner.run_state(task_id) or "unknown"
        time.sleep(0.02)
    message = f"stage {task_id} did not finish within {timeout}s"
    raise AssertionError(message)


def _project(runtime: Runtime, brief: str = BRIEF) -> str:
    return runtime.projects.create(title="Local inference for writers", brief=brief).id


def test_a_plan_stage_compiles_gates_and_persists(runtime: Runtime) -> None:
    """P3 AC1: an idea plus a brief yields identified requirements and an ordered unit plan."""
    from ideapress.services.stage_bodies import start_plan
    from ideapress.services.stage_reports import plan_report

    _with_backend(runtime, _scripted(GOOD_REQUIREMENTS, GOOD_PLAN))
    project_id = _project(runtime)
    task = start_plan(runtime, project_id=project_id)
    assert _wait(runtime, task.run_id) == "completed"

    report = plan_report(runtime, project_id=project_id)
    assert [r["key"] for r in report["requirements"]] == ["R-001", "R-002"]
    assert [u["key"] for u in report["units"]] == ["U-01", "U-02"]
    assert report["requirements"][0]["units"] == ["U-01"]
    assert report["requirements"][0]["quote"] in BRIEF
    assert all(unit["state"] == "planned" for unit in report["units"])


def test_a_model_that_says_no_requirements_are_needed_does_not_satisfy_the_gate(
    runtime: Runtime,
) -> None:
    """P3 AC2, end to end. The stage fails; nothing is written; the project is untouched."""
    from ideapress.services.stage_bodies import start_plan
    from ideapress.services.stage_reports import plan_report

    _with_backend(
        runtime,
        _scripted({"requirements": []}, GOOD_PLAN),
    )
    project_id = _project(runtime)
    task = start_plan(runtime, project_id=project_id)
    assert _wait(runtime, task.run_id) == "failed"

    report = plan_report(runtime, project_id=project_id)
    assert report["requirements"] == []
    assert report["units"] == []
    assert runtime.projects.get(project_id).status == "draft"


def test_a_plan_leaving_a_blocking_requirement_unassigned_fails_and_names_it(
    runtime: Runtime,
) -> None:
    from ideapress.services.stage_bodies import start_plan
    from ideapress.services.stage_reports import task_report

    partial_plan = {
        "units": [
            {
                "title": "One unit only",
                "goal_text": "Cover the first requirement.",
                "requirement_keys": ["R-001"],
            }
        ]
    }
    _with_backend(runtime, _scripted(GOOD_REQUIREMENTS, partial_plan))
    project_id = _project(runtime)
    task = start_plan(runtime, project_id=project_id)
    assert _wait(runtime, task.run_id) == "failed"

    report = task_report(runtime, project_id=project_id, run_id=task.run_id, stage=None)
    assert report["error_code"] == "VALIDATION_ERROR"
    assert "R-002" in (report["error_text"] or "")


def test_stage_events_are_gap_free(runtime: Runtime) -> None:
    """Replay-from-N is only correct if N+1 exists."""
    from ideapress.services.stage_bodies import start_plan

    _with_backend(runtime, _scripted(GOOD_REQUIREMENTS, GOOD_PLAN))
    project_id = _project(runtime)
    task = start_plan(runtime, project_id=project_id)
    _wait(runtime, task.run_id)

    records = runtime.events.source(runtime.storage, task.run_id).records(limit=1000)
    sequences = [record.sequence for record in records]
    assert sequences == list(range(1, len(sequences) + 1)), sequences
    assert records[0].event_type == "stage.started"
    assert records[-1].event_type in {"stage.completed", "stage.failed"}


def test_replay_after_a_disconnect_returns_exactly_what_was_missed(runtime: Runtime) -> None:
    from ideapress.services.stage_bodies import start_plan

    _with_backend(runtime, _scripted(GOOD_REQUIREMENTS, GOOD_PLAN))
    project_id = _project(runtime)
    task = start_plan(runtime, project_id=project_id)
    _wait(runtime, task.run_id)

    source = runtime.events.source(runtime.storage, task.run_id)
    everything = source.records(limit=1000)
    assert len(everything) >= 4

    # A client that saw the first two frames and dropped its connection.
    missed = source.records(after=2, limit=1000)
    assert [record.sequence for record in missed] == [record.sequence for record in everything[2:]]
    assert source.records(after=len(everything)) == []


def test_a_token_frame_is_bare_and_everything_else_carries_the_envelope(runtime: Runtime) -> None:
    """ADR-0025 §3."""
    _with_backend(runtime, _scripted(GOOD_REQUIREMENTS, GOOD_PLAN))
    project_id = _project(runtime)
    from ideapress.services.stage_bodies import start_plan

    task = start_plan(runtime, project_id=project_id)
    _wait(runtime, task.run_id)
    runtime.events.emit(runtime.storage, task.run_id, event_type="token", data={"text": "hello"})

    events = runtime.events.source(runtime.storage, task.run_id).replay(
        stream_id=task.run_id, after_sequence=0, limit=1000
    )
    token = next(event for event in events if event.type == "token")
    # `Event.payload` is typed as a mapping because most frames are; a token frame is the
    # documented exception (ADR-0025 §3), so this reads it as what it actually is.
    assert cast("object", token.payload) == "hello", "a token frame is bare"
    other = next(event for event in events if event.type == "stage.started")
    assert isinstance(other.payload, dict)
    assert set(other.payload) >= {"event", "occurred_at", "message"}


def test_only_one_stage_runs_per_project(runtime: Runtime) -> None:
    """api.md §3: a second stage for the same project is 409, not an interleaved writer."""
    from ideapress.errors import StageAlreadyRunning
    from ideapress.services.stages import StageTask

    _with_backend(runtime, _scripted(GOOD_REQUIREMENTS, GOOD_PLAN))
    project_id = _project(runtime)

    started = threading.Event()
    release = threading.Event()

    def slow(task: StageTask) -> None:
        started.set()
        release.wait(timeout=10)

    first = runtime.runner.start(project_id=project_id, stage="outline", body=slow)
    assert started.wait(timeout=5)
    try:
        with pytest.raises(StageAlreadyRunning) as caught:
            runtime.runner.start(project_id=project_id, stage="outline", body=slow)
        assert first.run_id in caught.value.message or first.run_id in str(caught.value.details)
    finally:
        release.set()
        if first.thread:
            first.thread.join(timeout=10)


def test_two_projects_may_run_stages_at_once(runtime: Runtime) -> None:
    """The per-project rule is not the per-process one; the gateway serialises generations."""
    from ideapress.services.stages import StageTask

    _with_backend(runtime, _scripted(GOOD_REQUIREMENTS, GOOD_PLAN))
    first_project = _project(runtime)
    second_project = runtime.projects.create(title="Another study", brief=BRIEF).id
    release = threading.Event()

    def slow(task: StageTask) -> None:
        release.wait(timeout=10)

    a = runtime.runner.start(project_id=first_project, stage="outline", body=slow)
    b = runtime.runner.start(project_id=second_project, stage="outline", body=slow)
    release.set()
    for task in (a, b):
        if task.thread:
            task.thread.join(timeout=10)
    assert _wait(runtime, a.run_id) == "completed"
    assert _wait(runtime, b.run_id) == "completed"


def test_a_cancelled_stage_stops_at_the_next_boundary_and_commits_nothing(
    runtime: Runtime,
) -> None:
    from ideapress.services.stage_reports import plan_report
    from ideapress.services.stages import StageTask

    _with_backend(runtime, _scripted(GOOD_REQUIREMENTS, GOOD_PLAN))
    project_id = _project(runtime)
    at_boundary = threading.Event()

    def cancellable(task: StageTask) -> None:
        at_boundary.set()
        time.sleep(0.15)
        runtime.runner.checkpoint(task)
        message = "the checkpoint should have raised"
        raise AssertionError(message)

    task = runtime.runner.start(project_id=project_id, stage="outline", body=cancellable)
    assert at_boundary.wait(timeout=5)
    assert runtime.runner.cancel(task.run_id) is True
    assert _wait(runtime, task.run_id) == "cancelled"
    assert plan_report(runtime, project_id=project_id)["units"] == []


def test_a_failed_stage_records_the_error_and_leaves_the_project_resumable(
    runtime: Runtime,
) -> None:
    from ideapress.services.stage_reports import task_report
    from ideapress.services.stages import StageTask

    _with_backend(runtime, _scripted(GOOD_REQUIREMENTS, GOOD_PLAN))
    project_id = _project(runtime)

    def explode(task: StageTask) -> None:
        message = "the backend fell over"
        raise ValidationError(message)

    task = runtime.runner.start(project_id=project_id, stage="outline", body=explode)
    assert _wait(runtime, task.run_id) == "failed"
    report = task_report(runtime, project_id=project_id, run_id=task.run_id, stage=None)
    assert report["error_code"] == "VALIDATION_ERROR"
    assert "fell over" in (report["error_text"] or "")
    # And a new stage may start: a failure pauses, it does not lock.
    assert runtime.runner.active_task(project_id) is None


def test_a_run_left_running_by_a_dead_process_is_marked_interrupted(runtime: Runtime) -> None:
    """Workflows §9: `interrupted` is not `failed`, which is what lets --resume pick it up."""
    from ideapress.infrastructure.db.models import StageRun as StageRunRow
    from ideapress.services.stages import boot_id

    _with_backend(runtime, _scripted(GOOD_REQUIREMENTS, GOOD_PLAN))
    project_id = _project(runtime)
    with runtime.storage.write() as session:
        run = StageRunRow(
            project_id=project_id,
            stage="draft",
            state="running",
            owner_pid=4_194_305,  # above the kernel maximum: nothing can be running there
            owner_boot_id=boot_id(),
        )
        session.add(run)
        session.flush()
        run_id = run.id

    assert runtime.runner.mark_interrupted() >= 1
    assert runtime.runner.run_state(run_id) == "interrupted"


def test_a_stage_with_no_implementation_is_refused_by_name(runtime: Runtime) -> None:
    """`draft` is implemented now; `critique` is not until P5, and the refusal names it."""
    from ideapress.errors import StagePreconditionFailed
    from ideapress.services.stage_bodies import start_plan, start_stage

    _with_backend(runtime, _scripted(GOOD_REQUIREMENTS, GOOD_PLAN))
    project_id = _project(runtime)
    task = start_plan(runtime, project_id=project_id)
    _wait(runtime, task.run_id)

    with pytest.raises(StagePreconditionFailed) as caught:
        start_stage(runtime, project_id=project_id, stage="critique")
    assert "critique" in caught.value.message
    assert "draft" in caught.value.message, "the message lists what *is* implemented"


def test_planning_a_project_with_no_brief_is_refused(runtime: Runtime) -> None:
    from ideapress.errors import StagePreconditionFailed
    from ideapress.services.stage_bodies import start_plan

    _with_backend(runtime, _scripted(GOOD_REQUIREMENTS, GOOD_PLAN))
    project_id = runtime.projects.create(title="Nothing to go on").id
    with pytest.raises(StagePreconditionFailed) as caught:
        start_plan(runtime, project_id=project_id)
    assert "no brief" in caught.value.message


def test_the_rejected_requirements_reach_the_event_stream(runtime: Runtime) -> None:
    """A rejection is the mechanism working; the user is entitled to see it happen."""
    from ideapress.services.stage_bodies import start_plan

    with_invention = {
        "requirements": [
            *GOOD_REQUIREMENTS["requirements"],
            {
                "text": "The article must include three customer testimonials.",
                "blocking": True,
                "source_document": "brief",
                "source_quote": "must include three customer testimonials from named users",
            },
        ]
    }
    _with_backend(runtime, _scripted(with_invention, GOOD_PLAN))
    project_id = _project(runtime)
    task = start_plan(runtime, project_id=project_id)
    assert _wait(runtime, task.run_id) == "completed"

    records = runtime.events.source(runtime.storage, task.run_id).records(limit=100)
    compiled = next(r for r in records if r.event_type == "requirements.compiled")
    assert compiled.data["compiled"] == 2
    assert len(compiled.data["rejected"]) == 1
    assert "testimonials" in compiled.data["rejected"][0]["text"]


def test_opening_a_second_runtime_does_not_kill_a_running_stage(runtime: Runtime) -> None:
    """The bug the M7 demonstration found, three units into a real drafting run.

    `mark_interrupted` runs whenever a Runtime is built, and the first version marked **every** row
    still `running`. So `ideapress unit list` in another terminal — or any second process opening
    the same database — marked a live stage as interrupted, after which the runner refused the next
    stage because the thread was still going. A run is only interrupted when its owner is gone.
    """
    from ideapress.services.stages import StageTask

    _with_backend(runtime, _scripted(GOOD_REQUIREMENTS, GOOD_PLAN))
    project_id = _project(runtime)

    started = threading.Event()
    release = threading.Event()

    def slow(task: StageTask) -> None:
        started.set()
        release.wait(timeout=15)

    task = runtime.runner.start(project_id=project_id, stage="outline", body=slow)
    assert started.wait(timeout=5)
    assert runtime.runner.run_state(task.run_id) == "running"

    # A second runtime over the same database — exactly what a CLI command in another terminal is.
    second = build_runtime(runtime.settings)
    try:
        assert runtime.runner.run_state(task.run_id) == "running", (
            "a second process marked a live run as interrupted"
        )
        assert second.runner.run_state(task.run_id) == "running"
    finally:
        second.close()
        release.set()
        if task.thread:
            task.thread.join(timeout=15)


def test_a_run_owned_by_a_dead_process_is_still_marked_interrupted(runtime: Runtime) -> None:
    """The other half: ownership must not make the check vacuous."""
    from ideapress.infrastructure.db.models import StageRun as StageRunRow
    from ideapress.services.stages import boot_id

    _with_backend(runtime, _scripted(GOOD_REQUIREMENTS, GOOD_PLAN))
    project_id = _project(runtime)
    with runtime.storage.write() as session:
        run = StageRunRow(
            project_id=project_id,
            stage="draft",
            state="running",
            # A PID that cannot be running: the kernel's maximum is well below this.
            owner_pid=4_194_305,
            owner_boot_id=boot_id(),
        )
        session.add(run)
        session.flush()
        dead_id = run.id

    assert runtime.runner.mark_interrupted() >= 1
    assert runtime.runner.run_state(dead_id) == "interrupted"


def test_a_run_from_an_earlier_boot_is_marked_interrupted(runtime: Runtime) -> None:
    """Nothing from a previous boot can still be running, whatever its PID says."""
    import os

    from ideapress.infrastructure.db.models import StageRun as StageRunRow

    _with_backend(runtime, _scripted(GOOD_REQUIREMENTS, GOOD_PLAN))
    project_id = _project(runtime)
    with runtime.storage.write() as session:
        run = StageRunRow(
            project_id=project_id,
            stage="draft",
            state="running",
            owner_pid=os.getpid(),  # alive — but from a boot that is not this one
            owner_boot_id="a-different-boot-entirely",
        )
        session.add(run)
        session.flush()
        stale_id = run.id

    runtime.runner.mark_interrupted()
    assert runtime.runner.run_state(stale_id) == "interrupted"


def test_a_run_with_no_recorded_owner_is_left_alone(runtime: Runtime) -> None:
    """Rows predating the ownership columns: refusing to guess is the safe direction."""
    from ideapress.infrastructure.db.models import StageRun as StageRunRow

    _with_backend(runtime, _scripted(GOOD_REQUIREMENTS, GOOD_PLAN))
    project_id = _project(runtime)
    with runtime.storage.write() as session:
        run = StageRunRow(project_id=project_id, stage="draft", state="running")
        session.add(run)
        session.flush()
        legacy_id = run.id

    runtime.runner.mark_interrupted()
    assert runtime.runner.run_state(legacy_id) == "running"
