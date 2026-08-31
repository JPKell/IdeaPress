"""The core loop end to end: draft, validate, repair, coverage, commit.

Everything runs against a scripted backend, so the model's answer is under the test's control and
every assertion is about what Python did with it.
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
        }
    ]
}

GOOD_DRAFT = (
    "Everything happens on your own machine. The model reads what you wrote, and nothing you "
    "wrote is uploaded to anyone: no account, no sync, no telemetry carrying your sentences "
    "somewhere else. The trade-off is that the hardware is yours to provide, and a laptop that "
    "runs warm will run warmer. What you get back is that the work stays where you made it."
)
MISSING_UPLOAD = (
    "Everything happens on your own machine. The model reads what you wrote and answers there, "
    "with no network involved at any point in the process, which is the entire point of the "
    "exercise and the reason people put up with the hardware bill in the first instance."
)


# The review stage runs after validation (P5), so every drafting script ends with an audit that
# finds nothing and a critique that accepts. These tests are about the *core loop*; the review
# loop has its own file.
CLEAN_REVIEW = (
    json.dumps({"findings": []}),
    json.dumps({"verdict": "acceptable", "rationale": "ok"}),
)


def _script(*texts: str) -> FakeBackend:
    from modelrack.testing import FakeGeneration, FakeScript

    return FakeBackend(
        script=FakeScript(
            models=default_fake_script().models,
            capabilities=default_fake_script().capabilities,
            generations=tuple(FakeGeneration(text=text) for text in texts),
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


def _planned(runtime: Runtime, *drafts: str) -> str:
    """Create a project, plan it, then swap in the drafting script."""
    _with(runtime, _script(json.dumps(REQUIREMENTS), json.dumps(PLAN)))
    project_id = runtime.projects.create(title="Local inference", brief=BRIEF).id
    from ideapress.services.stage_bodies import start_plan

    task = start_plan(runtime, project_id=project_id)
    assert _wait(runtime, task.run_id) == "completed"
    _with(runtime, _script(*drafts, *CLEAN_REVIEW))
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


def test_a_unit_is_drafted_validated_and_committed(runtime: Runtime) -> None:
    """P4 AC1's shape, against a scripted model rather than a real one."""
    from ideapress.services.units import unit_history

    project_id = _planned(runtime, GOOD_DRAFT)
    assert _draft(runtime, project_id) == "completed"

    with runtime.storage.read() as session:
        history = unit_history(session, project_id, "U-01")
    assert len(history) == 1
    assert history[0]["committed"] is True
    assert history[0]["version"] == 1
    assert history[0]["content_hash"].startswith("sha256:")
    assert {c["requirement_key"] for c in history[0]["coverage"]} == {"R-001", "R-002"}
    assert all(c["satisfied"] for c in history[0]["coverage"])
    assert all(c["satisfied_by"] == "deterministic_check" for c in history[0]["coverage"])


def test_a_non_compliant_draft_is_repaired(runtime: Runtime) -> None:
    """P4 AC2, first half: the loop repairs rather than committing something wrong."""
    project_id = _planned(runtime, MISSING_UPLOAD, GOOD_DRAFT)
    assert _draft(runtime, project_id) == "completed"

    events = _events(runtime, project_id)
    stages = [e.data.get("stage") for e in events if e.event_type == "attempt.started"]
    assert stages == ["draft", "repair"]
    committed = [e for e in events if e.event_type == "unit.committed"]
    assert len(committed) == 1


def test_an_unrepairable_unit_pauses_and_commits_nothing(runtime: Runtime) -> None:
    """P4 AC2, second half. Three attempts, then the unit pauses with its findings kept."""
    from ideapress.services.units import load_unit, unit_history

    project_id = _planned(runtime, MISSING_UPLOAD)
    assert _draft(runtime, project_id) == "completed", "the stage completes; the unit pauses"

    with runtime.storage.read() as session:
        unit = load_unit(session, project_id, "U-01")
        assert unit.state == "paused"
        assert unit.paused_reason is not None
        assert "R-002" in unit.paused_reason or "repair attempts" in unit.paused_reason
        assert unit.current_version_id is None
        assert unit_history(session, project_id, "U-01") == []

    attempts = [e for e in _events(runtime, project_id) if e.event_type == "attempt.started"]
    assert len(attempts) == 3, "max_attempts_per_stage, and not one more"
    assert any(e.event_type == "unit.paused" for e in _events(runtime, project_id))


def test_the_commit_records_provenance_field_by_field(runtime: Runtime) -> None:
    """P4 AC3: every committed unit names the backend, model, prompts and validations."""
    from sqlalchemy import select

    from ideapress.infrastructure.db.models import Attempt as AttemptRow
    from ideapress.infrastructure.db.models import UnitVersion as UnitVersionRow
    from ideapress.infrastructure.db.models import Validation as ValidationRow

    project_id = _planned(runtime, GOOD_DRAFT)
    _draft(runtime, project_id)

    with runtime.storage.read() as session:
        version = session.scalars(select(UnitVersionRow)).one()
        assert version.committed is True
        assert version.committed_at is not None
        assert version.word_count > 0
        assert version.char_count == len(version.content_text)
        assert version.created_from_attempt_id is not None

        attempt = session.get(AttemptRow, version.created_from_attempt_id)
        assert attempt is not None
        assert attempt.stage == "draft"
        assert attempt.backend == "fake"
        assert attempt.model_canonical_id
        assert attempt.model_provider_kind == "fake"
        assert attempt.model_provider_name == "gemma4:12b"
        assert attempt.model_digest
        assert attempt.prompt_id == "stages.draft.write"
        assert attempt.prompt_version == "1.0.0"
        assert attempt.prompt_sha256 and attempt.prompt_sha256.startswith("sha256:")
        assert attempt.response_hash and attempt.response_hash.startswith("sha256:")
        assert attempt.input_tokens is not None
        assert attempt.output_tokens is not None
        assert attempt.outcome == "completed"

        validations = session.scalars(
            select(ValidationRow).where(ValidationRow.attempt_id == attempt.id)
        ).all()
        assert len(validations) >= 20, "every check that ran is recorded, not only the failures"
        assert {v.check_kind for v in validations} >= {
            "structural",
            "length",
            "format",
            "content",
            "reference",
            "consistency",
            "safety",
        }


def test_prompt_and_response_text_are_not_stored_by_default(runtime: Runtime) -> None:
    """Data model §4: the user's work is not stored twice without being asked."""
    from sqlalchemy import select

    from ideapress.infrastructure.db.models import Attempt as AttemptRow

    project_id = _planned(runtime, GOOD_DRAFT)
    _draft(runtime, project_id)
    with runtime.storage.read() as session:
        for attempt in session.scalars(select(AttemptRow)).all():
            assert attempt.response_text is None
            assert attempt.prompt_text is None
            assert attempt.response_hash is not None, "the hash is always kept"


def test_a_refusal_pauses_the_unit_as_a_distinct_outcome(runtime: Runtime) -> None:
    """Risk M1: a refusal is not a workflow failure, and the model's words are kept."""
    from sqlalchemy import select

    from ideapress.infrastructure.db.models import Attempt as AttemptRow
    from ideapress.services.units import load_unit

    project_id = _planned(runtime, "I cannot assist with that request.")
    assert _draft(runtime, project_id) == "completed"

    with runtime.storage.read() as session:
        unit = load_unit(session, project_id, "U-01")
        assert unit.state == "paused"
        attempt = session.scalars(select(AttemptRow).where(AttemptRow.stage == "draft")).first()
        assert attempt is not None
        assert attempt.outcome == "content_rejected"
        assert attempt.rejection_reason == "I cannot assist with that request."


def test_hostile_model_output_is_stored_and_rendered_inert(runtime: Runtime) -> None:
    """Risk S1, at the storage boundary: it is kept as text and never executed."""
    hostile = (
        "Everything runs on your own machine and nothing is uploaded. "
        "<script>alert(1)</script> {{ 7*7 }} " + "and the rest reads normally enough. " * 6
    )
    project_id = _planned(runtime, hostile)
    assert _draft(runtime, project_id) == "completed"

    from sqlalchemy import select

    from ideapress.infrastructure.db.models import UnitVersion as UnitVersionRow

    with runtime.storage.read() as session:
        version = session.scalars(select(UnitVersionRow)).one()
        assert "<script>alert(1)</script>" in version.content_text, "stored verbatim"
        assert "{{ 7*7 }}" in version.content_text
        assert "49" not in version.content_text, "never evaluated"


def test_resume_skips_a_committed_unit(runtime: Runtime) -> None:
    """Workflows §9: `--resume` continues from the first incomplete unit."""
    project_id = _planned(runtime, GOOD_DRAFT)
    _draft(runtime, project_id)
    assert _draft(runtime, project_id, resume=True) == "completed"

    events = _events(runtime, project_id)
    assert any(e.event_type == "unit.skipped" for e in events)
    assert not any(e.event_type == "attempt.started" for e in events)


def test_without_resume_a_committed_unit_is_refused_rather_than_overwritten(
    runtime: Runtime,
) -> None:
    """A committed version is immutable; re-running must not silently replace one."""
    project_id = _planned(runtime, GOOD_DRAFT)
    _draft(runtime, project_id)
    assert _draft(runtime, project_id) == "failed", "a second pass cannot redraft a committed unit"
