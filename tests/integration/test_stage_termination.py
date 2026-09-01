"""A run's end is one fact, not two: its terminal state and its terminal event land together.

CI found this and a fast machine could not: ``test_review_loop.py`` asserted a failing stage emits
``stage.failed``, and on a slower runner it did not — not because the event was never written, but
because the run was already *readable as finished* while the event was still in flight.

The window mattered outside the tests. ``StageRunner._finish`` committed the run's terminal state
and then emitted its terminal event in a second transaction, and every poller in the product reads
those two in the order that loses: the CLI's ``plan build`` drains events, then checks
``is_finished``, then breaks — so a run whose state committed first ends with no terminal line
printed at all. A run that ends without saying it ended is the silence ADR-0039 exists to forbid.

Closing it by swapping the order only moves the hazard to the other observer: an SSE client that
sees ``stage.completed`` and then asks for the run would find it still running. The fix is that
neither order exists, because both writes are one transaction.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import pytest

from ideapress.config import Settings, load_settings
from ideapress.infrastructure.backends.fake import FakeBackend, default_fake_script
from ideapress.services.events import TERMINAL_STAGE_EVENTS
from ideapress.services.runtime import Runtime, build_runtime

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ideapress.services.database import Database

BRIEF = "The article must state that inference runs on the reader's own machine."

TERMINAL_EMIT_DELAY_SECONDS = 0.5
"""Long enough that a poller on a 20 ms tick is certain to look inside the window."""


def _script(*answers: str) -> FakeBackend:
    from modelrack.testing import FakeGeneration, FakeScript

    return FakeBackend(
        script=FakeScript(
            models=default_fake_script().models,
            capabilities=default_fake_script().capabilities,
            generations=tuple(FakeGeneration(text=a) for a in answers),
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


def _widen_the_window(runtime: Runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hold every terminal emit open for half a second, before it writes anything.

    The delay goes *before* the sink's own work rather than inside it, so it widens the gap
    without deciding where the gap is: whatever ``_finish`` does before calling the sink is what
    an observer gets to see early.
    """
    sink = runtime.events
    real_emit = sink.emit

    def slow_terminal_emit(
        database: Database, stage_run_id: str, *, event_type: str, **kwargs: Any
    ) -> int:
        if event_type in TERMINAL_STAGE_EVENTS:
            time.sleep(TERMINAL_EMIT_DELAY_SECONDS)
        return real_emit(database, stage_run_id, event_type=event_type, **kwargs)

    monkeypatch.setattr(sink, "emit", slow_terminal_emit)


def _poll_until_finished(runtime: Runtime, run_id: str, *, timeout: float = 20.0) -> list[Any]:
    """Read as the CLI reads: ask whether it is over, then drain, then stop if it was."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        finished = runtime.runner.is_finished(run_id)
        records = runtime.events.source(runtime.storage, run_id).records(limit=500)
        if finished:
            return records
        time.sleep(0.02)
    message = "the stage did not finish"
    raise AssertionError(message)


def _failing_plan(runtime: Runtime) -> str:
    """Start a plan stage whose first answer cannot be parsed. Returns the run id."""
    from ideapress.services.stage_bodies import start_plan

    _with(runtime, _script("not json, not anything"))
    project_id = runtime.projects.create(title="Local inference", brief=BRIEF).id
    return start_plan(runtime, project_id=project_id).run_id


def test_a_finished_run_always_has_its_terminal_event(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact shape CI failed on: finished, not committed, and no terminal event."""
    _widen_the_window(runtime, monkeypatch)
    run_id = _failing_plan(runtime)

    records = _poll_until_finished(runtime, run_id)

    assert runtime.runner.run_state(run_id) == "failed"
    assert any(r.event_type in TERMINAL_STAGE_EVENTS for r in records), (
        "a run readable as finished must already carry the event that says so"
    )


def test_a_terminal_event_is_never_visible_before_the_state_it_reports(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half, so the fix cannot be a swap: the SSE reader's order must hold too."""
    _widen_the_window(runtime, monkeypatch)
    run_id = _failing_plan(runtime)

    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        records = runtime.events.source(runtime.storage, run_id).records(limit=500)
        if any(r.event_type in TERMINAL_STAGE_EVENTS for r in records):
            assert runtime.runner.is_finished(run_id), (
                "a client told the stage ended must not find the run still running"
            )
            return
        time.sleep(0.02)
    message = "no terminal event was ever emitted"
    raise AssertionError(message)
