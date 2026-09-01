"""Feedback reaches LoadCoach once per committed unit — P7 AC4 and its named failure mode.

The plan names "feedback posted more than once per committed unit" as a likely failure, and it is
not cosmetic: LoadCoach folds caller feedback into a model's *production evidence*, so one unit's
opinion counted twice is a measurement that did not happen being recorded as one that did.

Also asserted here: feedback never fails a commit. The unit is already written when feedback is
attempted, so a LoadCoach that stopped answering in between produces a recorded failure and a
committed unit — never a rolled-back one.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

import httpx
import pytest
from tests.contract.loadcoach_mock import MockLoadCoach

from ideapress.config import LoadCoachSettings, load_settings
from ideapress.infrastructure.backends.loadcoach import LoadCoachBackend
from ideapress.services.feedback import (
    FeedbackOutcome,
    feedback_summary,
    job_ids_for_unit,
    send_unit_feedback,
)
from ideapress.services.inference import InferenceGateway
from ideapress.services.runtime import build_runtime
from ideapress.services.stage_bodies import start_plan, start_stage
from ideapress.services.stages import StageRunner
from ideapress.services.unit_reports import unit_list

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ideapress.services.runtime import Runtime

BRIEF = (
    "The article must state that inference runs entirely on the reader's own machine and that no "
    "document content is uploaded anywhere."
)
DRAFT = (
    "Everything happens on your own machine. Nothing you write is uploaded anywhere at all, and "
    "no account is needed for any of it. The hardware is yours to provide."
)


def _answers() -> list[str]:
    requirements = {
        "requirements": [
            {
                "text": "The unit must be explicit about where inference happens.",
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
            }
        ]
    }
    return [json.dumps(requirements), json.dumps(plan)]


def _drafts() -> list[str]:
    return [
        DRAFT,
        json.dumps({"findings": [], "requirements_assessment": []}),
        json.dumps({"verdict": "acceptable", "rationale": "ok"}),
    ]


class _Harness:
    """A project run end to end through a mock LoadCoach, with the mock kept for inspection."""

    def __init__(self, runtime: Runtime, mock: MockLoadCoach, client: httpx.Client) -> None:
        self.runtime = runtime
        self.mock = mock
        self.client = client

    def wait(self, run_id: str) -> str:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.runtime.runner.is_finished(run_id):
                return self.runtime.runner.run_state(run_id) or "unknown"
            time.sleep(0.02)
        message = "the stage did not finish"
        raise AssertionError(message)

    def run_one_unit(self) -> str:
        project_id = self.runtime.projects.create(title="Local inference", brief=BRIEF).id
        assert self.wait(start_plan(self.runtime, project_id=project_id).run_id) == "completed"
        self.mock._answers = _drafts()  # noqa: SLF001 — scripting the drafts
        self.mock._answer_index = 0  # noqa: SLF001
        self.wait(start_stage(self.runtime, project_id=project_id, stage="draft").run_id)
        return project_id


@pytest.fixture
def harness() -> Iterator[_Harness]:
    settings = load_settings().settings.model_copy(deep=True)
    runtime = build_runtime(settings)
    mock = MockLoadCoach(answers=_answers())
    client = mock.client()
    backend = LoadCoachBackend(LoadCoachSettings(job_stages=()), client=client)
    gateway = InferenceGateway(
        backend=backend,
        bindings=runtime.settings.models.stages,
        execution=runtime.settings.execution,
    )
    runtime._gateway = gateway  # noqa: SLF001 — substituting the backend is the point
    runtime._backend = backend  # noqa: SLF001
    runtime._runner = StageRunner(runtime.storage, gateway=gateway, sink=runtime.events)  # noqa: SLF001
    try:
        yield _Harness(runtime, mock, client)
    finally:
        runtime.close()
        client.close()


def test_feedback_reaches_loadcoach_after_a_unit_commits(harness: _Harness) -> None:
    """P7 AC4's first half: it is sent at all, and it says the unit was accepted."""
    project_id = harness.run_one_unit()
    states = [unit["state"] for unit in unit_list(harness.runtime, project_id=project_id)]
    assert states == ["committed"], states

    posted = [r for r in harness.mock.requests if r.path.endswith("/feedback")]
    assert posted, "no feedback was posted for a committed unit"
    assert posted[0].body["accepted"] is True
    assert posted[0].body["validation"]["passed"] is True


def test_feedback_is_attributed_to_ideapress(harness: _Harness) -> None:
    """`source` is taken from `X-Client-Name` on LoadCoach's side, so one caller cannot overwrite
    another's — but the header has to actually be there."""
    harness.run_one_unit()
    posted = [r for r in harness.mock.requests if r.path.endswith("/feedback")]
    assert posted[0].headers["x-client-name"] == "ideapress"
    assert posted[0].body["source"] == "ideapress"


def test_each_job_is_told_about_exactly_once(harness: _Harness) -> None:
    """The named failure mode. One unit's opinion counted twice is a fabricated measurement."""
    harness.run_one_unit()
    posted = [r for r in harness.mock.requests if r.path.endswith("/feedback")]
    job_ids = [r.path.split("/")[-2] for r in posted]
    assert len(job_ids) == len(set(job_ids)), job_ids


def test_a_second_call_skips_what_it_already_sent(harness: _Harness) -> None:
    """A resumed project must not re-report work it already reported."""
    project_id = harness.run_one_unit()
    jobs = job_ids_for_unit(harness.runtime.storage, project_id=project_id, unit_key="U-01")
    assert jobs, "the attempts recorded no job ids"

    before = len([r for r in harness.mock.requests if r.path.endswith("/feedback")])
    backend = harness.runtime.backend
    assert backend is not None
    outcome = send_unit_feedback(
        harness.runtime.storage,
        backend,
        project_id=project_id,
        unit_key="U-01",
        accepted=True,
        already_sent=[job_id for job_id, _ in jobs],
    )
    after = len([r for r in harness.mock.requests if r.path.endswith("/feedback")])
    assert outcome.sent == ()
    assert set(outcome.skipped) == {job_id for job_id, _ in jobs}
    assert after == before, "a job that had already been reported was reported again"


def test_a_query_for_one_projects_jobs_will_not_return_anothers(harness: _Harness) -> None:
    """M7-29's general shape: a query that decides something about another process must know who
    owns what. Feedback attributed to the wrong project would corrupt the evidence it feeds."""
    project_id = harness.run_one_unit()
    other = harness.runtime.projects.create(title="Unrelated", brief=BRIEF).id
    assert job_ids_for_unit(harness.runtime.storage, project_id=project_id, unit_key="U-01")
    assert job_ids_for_unit(harness.runtime.storage, project_id=other, unit_key="U-01") == ()


def test_feedback_that_cannot_be_delivered_does_not_undo_a_commit() -> None:
    """The unit is already written. A report that could not be delivered is a degradation."""
    settings = load_settings().settings.model_copy(deep=True)
    runtime = build_runtime(settings)
    mock = MockLoadCoach(answers=_answers())
    refuse_feedback = {"yet": False}

    def transport(request: httpx.Request) -> httpx.Response:
        if refuse_feedback["yet"] and request.url.path.endswith("/feedback"):
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
        runtime._gateway = gateway  # noqa: SLF001
        runtime._backend = backend  # noqa: SLF001
        runtime._runner = StageRunner(runtime.storage, gateway=gateway, sink=runtime.events)  # noqa: SLF001
        harness = _Harness(runtime, mock, client)
        refuse_feedback["yet"] = True
        project_id = harness.run_one_unit()
        states = [unit["state"] for unit in unit_list(runtime, project_id=project_id)]
        assert states == ["committed"], "a failed feedback post rolled back a committed unit"
    finally:
        runtime.close()
        client.close()


def test_a_backend_with_nowhere_to_send_feedback_is_skipped_silently() -> None:
    """Standalone modes have no jobs and no feedback endpoint; that is not a failure."""
    from ideapress.infrastructure.backends.fake import FakeBackend

    settings = load_settings().settings.model_copy(deep=True)
    runtime = build_runtime(settings)
    try:
        outcome = send_unit_feedback(
            runtime.storage,
            FakeBackend(),
            project_id="01PROJECT",
            unit_key="U-01",
            accepted=True,
        )
        assert outcome == FeedbackOutcome()
    finally:
        runtime.close()


def test_the_summary_totals_across_units() -> None:
    outcomes = [
        FeedbackOutcome(sent=("a", "b")),
        FeedbackOutcome(skipped=("c",), failed=(("d", "refused"),)),
    ]
    assert feedback_summary(outcomes) == {"sent": 2, "skipped": 1, "failed": 1}


def test_the_commit_event_names_the_feedback_it_sent(harness: _Harness) -> None:
    """The stage event stream is where a person watches this happen, so it has to say so."""
    from sqlalchemy import select

    from ideapress.infrastructure.db.models import StageEvent, StageRun

    project_id = harness.run_one_unit()
    with harness.runtime.storage.read() as session:
        kinds = list(
            session.execute(
                select(StageEvent.event_type)
                .join(StageRun, StageRun.id == StageEvent.stage_run_id)
                .where(StageRun.project_id == project_id)
            ).scalars()
        )
    assert "unit.feedback_sent" in kinds, kinds


def test_no_feedback_is_posted_about_a_job_that_never_ran(harness: _Harness) -> None:
    """The worst consequence of reading a refused stage as a success, asserted closed.

    A `failed` job produced nothing, so telling LoadCoach it was *accepted* writes a false
    positive into a published application's reliability data — which then skews its routing for
    every future caller, not only IdeaPress. The live I7 run observed exactly that: feedback with
    `accepted: true` and `validation.passed: true` posted about a job whose state was `failed`
    with `NO_ELIGIBLE_MODEL`.

    It closes as a consequence of the fix rather than by a second check: a refused stage now
    raises, so no `StageResult` exists, so `Attempt.routing_json` stays NULL, and
    `job_ids_for_unit` filters the attempt out. This asserts that chain end to end.
    """
    project_id = harness.runtime.projects.create(title="Refused", brief=BRIEF).id
    assert harness.wait(start_plan(harness.runtime, project_id=project_id).run_id) == "completed"

    # The plan is in place; now LoadCoach declines the draft. Unguarded, the decline arrives as a
    # successful *empty* generation, the unit commits with no content, and feedback is then posted
    # about a job that never ran.
    harness.mock.fail_next("NO_ELIGIBLE_MODEL", "No model satisfied the profile's constraints.")
    state = harness.wait(start_stage(harness.runtime, project_id=project_id, stage="draft").run_id)

    assert state == "failed", f"a declined draft must fail the stage, not commit: {state}"
    states = [unit["state"] for unit in unit_list(harness.runtime, project_id=project_id)]
    assert "committed" not in states, f"a unit committed from a declined draft: {states}"
    posted = [r for r in harness.mock.requests if r.path.endswith("/feedback")]
    assert posted == [], f"feedback was posted about a job that never ran: {posted}"


def test_a_completed_job_that_carries_no_text_cannot_commit_an_empty_unit(
    harness: _Harness,
) -> None:
    """The residual of M8-16: a job that *completes* and carries no text.

    The adapter reports that faithfully — a model that genuinely stops having emitted nothing is
    `finish_reason='stop'` with empty text, and inventing a degradation for it would be the
    adapter guessing again. So the refusal has to come from the workflow, and this asserts it
    does: whatever else happens, a unit must not reach `committed` with no content.
    """
    project_id = harness.runtime.projects.create(title="Silent", brief=BRIEF).id
    assert harness.wait(start_plan(harness.runtime, project_id=project_id).run_id) == "completed"

    harness.mock._answers = [""] * 12  # noqa: SLF001 — the model says nothing, repeatedly
    harness.mock._answer_index = 0  # noqa: SLF001
    harness.wait(start_stage(harness.runtime, project_id=project_id, stage="draft").run_id)

    committed = [
        unit
        for unit in unit_list(harness.runtime, project_id=project_id)
        if unit["state"] == "committed"
    ]
    assert committed == [], f"a unit committed with no content: {committed}"
