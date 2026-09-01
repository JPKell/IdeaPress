"""P5 end to end: audit, escalation, critique, bounded revision, and the regression rule.

Every model answer is scripted, so what is asserted is what Python did with it.
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
    "The article must state that inference runs entirely on the reader's own machine and that no "
    "document content is uploaded anywhere."
)
REQUIREMENTS = {
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
PLAN = {
    "units": [
        {
            "title": "Where the work happens",
            "goal_text": "Say plainly where inference runs.",
            "requirement_keys": ["R-001"],
            "target_words": 50,
        }
    ]
}
DRAFT = (
    "Everything happens on your own machine. The model reads what you wrote and answers there, "
    "with nothing uploaded and no account needed. The hardware is yours to provide, which is the "
    "trade you are making for keeping the work where you made it."
)
BETTER = (
    "Everything happens on your own machine. The model reads what you wrote and answers there, "
    "with nothing uploaded and no account needed, and no network involved at any point. The "
    "hardware is yours to provide: that is the trade for keeping the work where you made it."
)
WORSE = "Everything happens on your own machine but this sentence stops mid"

NO_FINDINGS: dict[str, Any] = {"findings": []}
# `minor` scores 0.85, which is above the 0.6 escalation threshold — so these fixtures exercise
# the loop without also triggering a deep audit. ONE_CRITICAL scores 0.0 and is what escalates.
ONE_MINOR = {
    "findings": [
        {
            "category": "clarity",
            "severity": "minor",
            "problem_text": "the second sentence does two things at once",
            "evidence_text": "The model reads what you wrote and answers there",
            "required_fix_text": "split it",
        }
    ]
}
ONE_CRITICAL = {
    "findings": [
        {
            "category": "accuracy",
            "severity": "critical",
            "problem_text": "an unsupported claim about accuracy",
            "evidence_text": "answers there",
        }
    ]
}
ACCEPTABLE = {"verdict": "acceptable", "rationale": "it meets the bar"}
LEAVE_IT = {"verdict": "leave_it_alone", "rationale": "only preferences remain"}
DEFICIENT = {"verdict": "materially_deficient", "rationale": "the clarity finding matters"}


def _script(*answers: Any) -> FakeBackend:
    from modelrack.testing import FakeGeneration, FakeScript

    return FakeBackend(
        script=FakeScript(
            models=default_fake_script().models,
            capabilities=default_fake_script().capabilities,
            generations=tuple(
                FakeGeneration(text=a if isinstance(a, str) else json.dumps(a)) for a in answers
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
    message = "the stage did not finish"
    raise AssertionError(message)


def _planned(runtime: Runtime) -> str:
    _with(runtime, _script(REQUIREMENTS, PLAN))
    project_id = runtime.projects.create(title="Local inference", brief=BRIEF).id
    from ideapress.services.stage_bodies import start_plan

    task = start_plan(runtime, project_id=project_id)
    assert _wait(runtime, task.run_id) == "completed"
    return project_id


def _draft_with(runtime: Runtime, project_id: str, *answers: Any) -> list[Any]:
    from ideapress.services.stage_bodies import start_stage

    _with(runtime, _script(*answers))
    task = start_stage(runtime, project_id=project_id, stage="draft")
    _wait(runtime, task.run_id)
    return runtime.events.source(runtime.storage, task.run_id).records(limit=500)


def _event(events: list[Any], kind: str) -> Any:
    return next(e for e in events if e.event_type == kind)


def test_a_clean_audit_and_an_acceptable_critique_commits_in_one_round(
    runtime: Runtime,
) -> None:
    project_id = _planned(runtime)
    events = _draft_with(runtime, project_id, DRAFT, NO_FINDINGS, ACCEPTABLE)

    assert _event(events, "audit.completed").data["score"] == 1.0
    assert _event(events, "critique.completed").data["verdict"] == "acceptable"
    stop = _event(events, "review.stopped")
    assert stop.data["stop_reason"] == "critique_satisfied"
    assert stop.data["rounds"] == 0
    assert _event(events, "unit.committed")


def test_leave_it_alone_ends_the_loop_without_a_change(runtime: Runtime) -> None:
    """Workflows §5: a purely stylistic preference does not trigger a revision."""
    project_id = _planned(runtime)
    events = _draft_with(runtime, project_id, DRAFT, ONE_MINOR, LEAVE_IT)

    assert _event(events, "critique.completed").data["verdict"] == "leave_it_alone"
    assert _event(events, "review.stopped").data["stop_reason"] == "critique_satisfied"
    assert not [e for e in events if e.event_type == "revision.completed"]

    from ideapress.services.unit_reports import unit_detail

    detail = unit_detail(runtime, project_id=project_id, unit_key="U-01")
    assert detail["state"] == "committed"
    assert detail["content"] == DRAFT, "nothing was rewritten"


def test_a_low_score_escalates_to_a_deep_audit_exactly_once_per_round(
    runtime: Runtime,
) -> None:
    """Workflows §5: one deep audit per unit per round."""
    project_id = _planned(runtime)
    events = _draft_with(runtime, project_id, DRAFT, ONE_CRITICAL, ONE_CRITICAL, ACCEPTABLE)

    audits = [e for e in events if e.event_type == "audit.completed"]
    stages = [e.data["stage"] for e in audits]
    assert stages == ["audit_fast", "audit_deep"], stages
    assert audits[1].data["escalated"] is True
    assert _event(events, "review.stopped").data["escalations"] == 1


def test_a_clean_audit_does_not_escalate(runtime: Runtime) -> None:
    project_id = _planned(runtime)
    events = _draft_with(runtime, project_id, DRAFT, NO_FINDINGS, ACCEPTABLE)
    assert [e.data["stage"] for e in events if e.event_type == "audit.completed"] == ["audit_fast"]
    assert _event(events, "review.stopped").data["escalations"] == 0


def test_a_revision_that_makes_the_unit_worse_is_rejected_and_the_prior_text_kept(
    runtime: Runtime,
) -> None:
    """P5's named failure mode. The revision raises validation failures and is discarded."""
    project_id = _planned(runtime)
    events = _draft_with(runtime, project_id, DRAFT, ONE_MINOR, DEFICIENT, WORSE)

    rejected = _event(events, "revision.rejected")
    assert "raised validation failures" in rejected.message
    stop = _event(events, "review.stopped")
    assert stop.data["stop_reason"] == "regression_rejected"
    assert stop.data["rejected_revisions"] == 1

    from ideapress.services.unit_reports import unit_detail

    detail = unit_detail(runtime, project_id=project_id, unit_key="U-01")
    assert detail["content"] == DRAFT, "the prior version was kept"
    assert WORSE not in detail["content"]


def test_a_revision_that_improves_is_accepted_and_the_loop_continues(
    runtime: Runtime,
) -> None:
    project_id = _planned(runtime)
    events = _draft_with(
        runtime, project_id, DRAFT, ONE_MINOR, DEFICIENT, BETTER, NO_FINDINGS, ACCEPTABLE
    )

    completed = _event(events, "revision.completed")
    assert completed.data["round"] == 1
    from ideapress.services.unit_reports import unit_detail

    detail = unit_detail(runtime, project_id=project_id, unit_key="U-01")
    assert detail["content"] == BETTER


def test_a_critic_that_never_converges_is_stopped_by_the_arithmetic(runtime: Runtime) -> None:
    """Risk T2. The critic says "materially deficient" every time and the loop stops anyway.

    It stops on **diminishing returns** rather than the round limit, and that is the stronger
    result: the finding count did not move between rounds, so Python ended it two rounds early
    without consulting the critic's opinion of its own progress.
    """
    project_id = _planned(runtime)
    events = _draft_with(
        runtime, project_id, DRAFT, ONE_MINOR, DEFICIENT, BETTER, ONE_MINOR, DEFICIENT
    )

    stop = _event(events, "review.stopped")
    assert stop.data["stop_reason"] == "diminishing_returns"
    assert stop.data["rounds"] == 1
    assert stop.data["rounds"] < runtime.settings.workflow.max_revision_rounds
    assert [e.data["verdict"] for e in events if e.event_type == "critique.completed"] == [
        "materially_deficient",
        "materially_deficient",
    ], "the critic asked twice and got one round"
    assert "below the 5% threshold" in stop.data["detail"]


def test_the_stop_reason_is_always_recorded_on_the_critique_row(runtime: Runtime) -> None:
    from sqlalchemy import select

    from ideapress.infrastructure.db.models import Critique as CritiqueRow

    project_id = _planned(runtime)
    _draft_with(runtime, project_id, DRAFT, NO_FINDINGS, ACCEPTABLE)
    with runtime.storage.read() as session:
        rows = session.scalars(select(CritiqueRow)).all()
    assert rows
    assert rows[-1].verdict == "acceptable"
    assert rows[-1].stop_reason == "critique_satisfied"


def test_findings_are_stored_with_severity_evidence_and_their_source_stage(
    runtime: Runtime,
) -> None:
    from sqlalchemy import select

    from ideapress.infrastructure.db.models import AuditFinding as AuditFindingRow

    project_id = _planned(runtime)
    _draft_with(runtime, project_id, DRAFT, ONE_CRITICAL, ONE_MINOR, ACCEPTABLE)
    with runtime.storage.read() as session:
        rows = session.scalars(select(AuditFindingRow)).all()
    by_stage = {row.source_stage for row in rows}
    assert by_stage == {"audit_fast", "audit_deep"}
    critical = next(row for row in rows if row.severity == "critical")
    assert critical.evidence_text == "answers there"
    assert critical.escalated is False
    deep = next(row for row in rows if row.source_stage == "audit_deep")
    assert deep.escalated is True


def test_an_invented_verdict_is_refused_rather_than_read_as_acceptable(
    runtime: Runtime,
) -> None:
    """A model must not be able to end the loop with a word nobody defined."""
    project_id = _planned(runtime)
    events = _draft_with(
        runtime, project_id, DRAFT, NO_FINDINGS, {"verdict": "looks_great", "rationale": "yes"}
    )
    assert not [e for e in events if e.event_type == "unit.committed"]
    assert any(e.event_type == "stage.failed" for e in events)


def test_an_invented_severity_becomes_minor_rather_than_inflating_the_score(
    runtime: Runtime,
) -> None:
    """A model inventing a severity name must not be able to weight its own finding."""
    from ideapress.services.review import parse_findings

    report = parse_findings(
        json.dumps(
            {"findings": [{"category": "x", "severity": "catastrophic", "problem_text": "p"}]}
        ),
        stage="audit_fast",
    )
    assert report.findings[0].severity == "minor"
    assert report.score == pytest.approx(0.85)


def test_finding_keys_are_generated_here_not_taken_from_the_model(runtime: Runtime) -> None:
    from ideapress.services.review import parse_findings

    report = parse_findings(
        json.dumps(
            {
                "findings": [
                    {"category": "x", "severity": "minor", "problem_text": "p", "key": "INJECTED"}
                ]
            }
        ),
        stage="audit_fast",
        key_prefix="A",
    )
    assert report.findings[0].key == "A-001"


def test_the_unit_report_shows_findings_and_what_stopped_the_loop(runtime: Runtime) -> None:
    """The findings UI's data: severity, evidence, and what changed between rounds."""
    from ideapress.services.unit_reports import unit_detail

    project_id = _planned(runtime)
    _draft_with(runtime, project_id, DRAFT, ONE_MINOR, DEFICIENT, BETTER, ONE_MINOR, DEFICIENT)
    detail = unit_detail(runtime, project_id=project_id, unit_key="U-01")

    assert detail["findings"], "the findings reach the report"
    first = detail["findings"][0]
    assert first["severity"] == "minor"
    assert first["evidence"], "with the passage it is about"
    assert first["fix"], "and a description of what would resolve it"
    assert first["stage"] == "audit_fast"

    verdicts = [c["verdict"] for c in detail["critiques"]]
    assert verdicts == ["materially_deficient", "materially_deficient"]
    assert detail["critiques"][-1]["stop_reason"] == "diminishing_returns"
    assert detail["critiques"][-1]["improvement_delta"] == pytest.approx(0.0)
    rounds = {c["round"] for c in detail["critiques"]}
    assert rounds == {0, 1}, "what changed between rounds is visible as rounds"
