"""A cancel holds across the transport retry, and every model call is an attempt row (row WPF7).

WP6 cancelled a `project_review` three seconds into its first model call. IdeaPress answered
*honours it at the next model-call boundary*, then made a **second** 2 m 47 s call and ended the run
`failed`, with zero attempts recorded. Both halves were the same gap: the only cancel checks were in
the stage bodies, and the retry of an empty generation happens underneath them, inside the one door
to a model — which also recorded nothing about the calls it discarded, so a run that spent two full
output budgets could not say what it had cost.

Everything here runs against a scripted backend: the model's answers are the test's, and the
assertions are about what Python did between them.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import select

from ideapress.config import Settings, load_settings
from ideapress.infrastructure.backends.fake import FakeBackend, default_fake_script
from ideapress.infrastructure.db.models import Attempt as AttemptRow
from ideapress.services.runtime import Runtime, build_runtime

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ideapress.domain.inference import StageRequest, StageResult

BRIEF = """
# Local inference for writers

The article must state that inference runs entirely on the reader's own machine.
""".strip()

GOOD_REQUIREMENTS = {
    "requirements": [
        {
            "text": "The article must be explicit about where inference happens.",
            "blocking": True,
            "source_document": "brief",
            "source_quote": "inference runs entirely on the reader's own machine",
            "checks": [{"kind": "must_contain_any", "values": ["own machine", "locally"]}],
        }
    ]
}

GOOD_PLAN = {
    "units": [
        {
            "title": "What local inference means",
            "goal_text": "Explain running a model on your own machine.",
            "requirement_keys": ["R-001"],
            "target_words": 400,
        }
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


def _wait(runtime: Runtime, run_id: str, *, timeout: float = 15.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if runtime.runner.is_finished(run_id):
            return runtime.runner.run_state(run_id) or "unknown"
        time.sleep(0.02)
    message = f"stage {run_id} did not finish within {timeout}s"
    raise AssertionError(message)


def _attempts(runtime: Runtime, run_id: str) -> list[AttemptRow]:
    with runtime.storage.read() as session:
        return list(
            session.scalars(
                select(AttemptRow)
                .where(AttemptRow.stage_run_id == run_id)
                .order_by(AttemptRow.transport_call)
            ).all()
        )


def _empty(result: StageResult) -> StageResult:
    """What a reasoning model returns when its whole output budget went on thinking."""
    return replace(result, text="", finish_reason="length")


def _empty_first_calls(backend: FakeBackend, count: int) -> list[StageRequest]:
    """Make the backend's first ``count`` calls come back empty and truncated.

    Returns:
        The list every call appends its request to, so a test can count the calls that were made.
    """
    calls: list[StageRequest] = []
    original = backend.generate

    def generate(request: StageRequest) -> StageResult:
        calls.append(request)
        real = original(request)
        return _empty(real) if len(calls) <= count else real

    backend.generate = generate  # type: ignore[method-assign]  # simulates the thinking runaway
    return calls


def test_a_cancel_during_an_empty_generation_makes_no_second_call(runtime: Runtime) -> None:
    """The cancel WP6 pressed: it lands at the retry's boundary, and the run ends `cancelled`.

    The first call returns empty and truncated — the retry's trigger — but not before the test has
    asked the run to stop. One call was made, the run is `cancelled` rather than `failed`, and the
    call it made is on the record.
    """
    from ideapress.services.stage_bodies import start_plan

    backend = _scripted(GOOD_REQUIREMENTS, GOOD_PLAN)
    in_flight = threading.Event()
    cancelled = threading.Event()
    calls: list[StageRequest] = []
    original = backend.generate

    def generate(request: StageRequest) -> StageResult:
        calls.append(request)
        in_flight.set()
        assert cancelled.wait(10.0), "the test never cancelled the run"
        return _empty(original(request))

    backend.generate = generate  # type: ignore[method-assign]  # holds the call open
    _with_backend(runtime, backend)
    project_id = runtime.projects.create(title="Local inference", brief=BRIEF).id

    run_id = start_plan(runtime, project_id=project_id).run_id
    assert in_flight.wait(10.0), "the stage never reached its first model call"
    assert runtime.runner.cancel(run_id) is True
    cancelled.set()

    assert _wait(runtime, run_id) == "cancelled"
    assert len(calls) == 1, f"a cancel must make no second call; calls: {len(calls)}"
    rows = _attempts(runtime, run_id)
    assert [row.transport_call for row in rows] == [1]
    assert rows[0].outcome == "provider_error"
    assert rows[0].error_code == "EMPTY_GENERATION"


def test_a_discarded_call_is_recorded_beside_the_answer_that_was_kept(runtime: Runtime) -> None:
    """The retry still works, and now the run says it made two calls rather than one."""
    from ideapress.services.stage_bodies import start_plan

    # The script advances per call, and the discarded call consumes an answer: the requirements
    # answer is scripted twice so the retry gets it, and the outline follows.
    backend = _scripted(GOOD_REQUIREMENTS, GOOD_REQUIREMENTS, GOOD_PLAN)
    calls = _empty_first_calls(backend, 1)
    _with_backend(runtime, backend)
    project_id = runtime.projects.create(title="Local inference", brief=BRIEF).id

    run_id = start_plan(runtime, project_id=project_id).run_id
    assert _wait(runtime, run_id) == "completed"

    assert len(calls) == 3, "one discarded call, its retry, and the outline's call"
    rows = _attempts(runtime, run_id)
    discarded = [row for row in rows if row.transport_call > 0]
    kept = [row for row in rows if row.transport_call == 0]
    assert len(discarded) == 1, "the call whose answer was thrown away is a row of its own"
    assert discarded[0].stage == "requirements"
    same_stage = [row for row in kept if row.stage == "requirements"]
    assert len(same_stage) == 1, "the answer the retry produced is the attempt's kept row"
    assert discarded[0].attempt == same_stage[0].attempt, "a transport retry is no content attempt"
    assert discarded[0].outcome == "provider_error"
    assert discarded[0].output_tokens is not None, "a discarded call is still spend"
    assert sorted(row.stage for row in kept) == ["outline", "requirements"]
    assert {row.outcome for row in kept} == {"completed"}

    # A reader of the task must be able to tell the two rows of one attempt apart.
    from ideapress.services.stage_reports import task_report

    reported = task_report(runtime, project_id=project_id, run_id=run_id, stage=None)["attempts"]
    assert [a["transport_call"] for a in reported] == [1, 0, 0]


def test_both_calls_are_recorded_when_the_budget_is_genuinely_too_small(runtime: Runtime) -> None:
    """WP6's `project_review`, in miniature: it fails, and it says what the two calls cost.

    The stage still fails with the budget in its message — that behaviour is M7-16's and is not
    reopened here — but the run no longer ends with zero attempts.
    """
    from ideapress.services.stage_bodies import start_plan

    backend = _scripted(GOOD_REQUIREMENTS, GOOD_PLAN)
    calls = _empty_first_calls(backend, 2)
    _with_backend(runtime, backend)
    project_id = runtime.projects.create(title="Local inference", brief=BRIEF).id

    run_id = start_plan(runtime, project_id=project_id).run_id
    assert _wait(runtime, run_id) == "failed"

    assert len(calls) == 2, "one retry, and then it stops"
    rows = _attempts(runtime, run_id)
    assert [row.transport_call for row in rows] == [1, 2]
    assert [row.error_code for row in rows] == ["EMPTY_GENERATION", "CONTEXT_LIMIT_EXCEEDED"]
    assert all(row.outcome == "provider_error" for row in rows)


def test_a_stage_whose_budgets_cannot_fit_is_refused_before_it_starts(runtime: Runtime) -> None:
    """Row WPF7's other half: the operator is told which stage cannot fit, before any model call.

    WP6 met this the other way round — two model calls, each spending a whole window on reasoning,
    then *the model produced no text at all in 8192 output tokens*, advising a raise of the number
    the served context could not honour.
    """
    from ideapress.errors import StagePreconditionFailed
    from ideapress.services.stage_bodies import start_plan

    _with_backend(runtime, _scripted(GOOD_REQUIREMENTS, GOOD_PLAN))
    runtime.settings.inference.ollama.served_context_tokens = 8192
    project_id = runtime.projects.create(title="Local inference", brief=BRIEF).id

    with pytest.raises(StagePreconditionFailed) as refused:
        start_plan(runtime, project_id=project_id)

    assert "8192" in refused.value.message, "the window it would have run in"
    assert "requirements" in refused.value.message, "the stage that does not fit"
    assert refused.value.details["stage"] == "requirements"
    from ideapress.infrastructure.db.models import StageRun as StageRunRow

    with runtime.storage.read() as session:
        runs = session.scalars(
            select(StageRunRow).where(StageRunRow.project_id == project_id)
        ).all()
    assert list(runs) == [], "a refused stage starts no run at all"
