"""Revising a committed unit, and the overrides a stage run carries (row WP5).

Before this row, ``POST …/units/{key}/revise`` started a *draft* run over the unit. The draft loop's
first move, ``committed → drafting``, is not an arrow in data model §3, so every revision of a
committed unit ended ``stage.failed``. Its instructions, and every key of a stage run's
``overrides``, were recorded on the run and read by nothing. Every model answer here is scripted,
so what is asserted is what Python did with it.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

import pytest
from baseaicore import ValidationError
from fastapi.testclient import TestClient

from ideapress.config import Settings, load_settings
from ideapress.infrastructure.backends.fake import FakeBackend, default_fake_script
from ideapress.services.runtime import Runtime, build_runtime
from ideapress.web.app import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ideapress.domain.inference import StageRequest

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
INSTRUCTION = "Say that no network is involved at any point."

NO_FINDINGS: dict[str, Any] = {"findings": []}
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
ACCEPTABLE = {"verdict": "acceptable", "rationale": "it meets the bar"}
DEFICIENT = {"verdict": "materially_deficient", "rationale": "the clarity finding matters"}
HINT = "ollama/qwen3.5:9b-q8_0"


def _script(*answers: Any) -> FakeBackend:  # noqa: ANN401 — a scripted answer, text or JSON
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


def _with(runtime: Runtime, backend: FakeBackend) -> list[StageRequest]:
    """Put ``backend`` behind ``runtime``, and return the list every request it serves lands in."""
    from ideapress.services.inference import InferenceGateway
    from ideapress.services.stages import StageRunner

    seen: list[StageRequest] = []
    original = backend.generate

    def recording(request: StageRequest) -> Any:  # noqa: ANN401 — the backend's own result
        seen.append(request)
        return original(request)

    backend.generate = recording  # type: ignore[method-assign]  # recording is the point
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
    return seen


def _wait(runtime: Runtime, task_id: str, *, timeout: float = 20.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if runtime.runner.is_finished(task_id):
            return runtime.runner.run_state(task_id) or "unknown"
        time.sleep(0.02)
    message = "the stage did not finish"
    raise AssertionError(message)


def _planned(runtime: Runtime, title: str = "Local inference") -> str:
    from ideapress.services.stage_bodies import start_plan

    _with(runtime, _script(REQUIREMENTS, PLAN))
    project_id = runtime.projects.create(title=title, brief=BRIEF).id
    task = start_plan(runtime, project_id=project_id)
    assert _wait(runtime, task.run_id) == "completed"
    return project_id


def _run(
    runtime: Runtime,
    project_id: str,
    stage: str,
    *answers: Any,  # noqa: ANN401 — scripted answers
    **kwargs: Any,  # noqa: ANN401 — start_stage's own keywords
) -> tuple[str, list[Any], list[StageRequest]]:
    from ideapress.services.stage_bodies import start_stage

    seen = _with(runtime, _script(*answers))
    task = start_stage(runtime, project_id=project_id, stage=stage, **kwargs)  # type: ignore[arg-type]
    state = _wait(runtime, task.run_id)
    return state, runtime.events.source(runtime.storage, task.run_id).records(limit=500), seen


def _drafted(runtime: Runtime, title: str = "Local inference") -> str:
    project_id = _planned(runtime, title)
    state, _events, _seen = _run(runtime, project_id, "draft", DRAFT, NO_FINDINGS, ACCEPTABLE)
    assert state == "completed"
    return project_id


def _event(events: list[Any], kind: str) -> Any:  # noqa: ANN401 — a stage event record
    return next(e for e in events if e.event_type == kind)


def _unit(runtime: Runtime, project_id: str) -> Any:  # noqa: ANN401 — the unit's row
    from ideapress.services.units import load_unit

    with runtime.storage.read() as session:
        unit = load_unit(session, project_id, "U-01")
        session.expunge(unit)
        return unit


def _versions(runtime: Runtime, project_id: str) -> list[tuple[int, bool]]:
    from ideapress.services.units import unit_history

    with runtime.storage.read() as session:
        return [(v["version"], v["committed"]) for v in unit_history(session, project_id, "U-01")]


# --- Revise ---------------------------------------------------------------------------------------


def test_revising_a_committed_unit_carries_the_instructions_and_commits_a_new_version(
    runtime: Runtime,
) -> None:
    from sqlalchemy import select

    from ideapress.infrastructure.db.models import Attempt as AttemptRow
    from ideapress.infrastructure.db.models import UnitVersion as UnitVersionRow

    project_id = _drafted(runtime)
    state, events, seen = _run(
        runtime, project_id, "revise", BETTER, NO_FINDINGS, ACCEPTABLE,
        units=["U-01"], overrides={"instructions": INSTRUCTION},
    )  # fmt: skip

    assert state == "completed"
    revisions = [request for request in seen if request.stage == "revise"]
    assert len(revisions) == 1
    assert INSTRUCTION in revisions[0].user, "the author's words reach the revise prompt"
    assert DRAFT in revisions[0].user, "the committed text is what is revised"
    assert _event(events, "unit.committed").data["version"] == 2
    assert _versions(runtime, project_id) == [(2, True), (1, True)], "version 1 is kept"
    unit = _unit(runtime, project_id)
    assert unit.state == "committed"
    with runtime.storage.read() as session:
        version = session.get(UnitVersionRow, unit.current_version_id)
        assert version is not None
        assert version.content_text == BETTER
        attempt = session.get(AttemptRow, version.created_from_attempt_id)
        assert attempt is not None
        assert (attempt.stage, attempt.round) == ("revise", 1)
        stages = session.scalars(
            select(AttemptRow.stage).where(AttemptRow.stage_run_id == attempt.stage_run_id)
        ).all()
    assert "draft" not in stages, "a revision never redrafts"


def test_the_instruction_round_counts_against_the_round_limit(runtime: Runtime) -> None:
    project_id = _drafted(runtime)
    state, events, seen = _run(
        runtime, project_id, "revise", BETTER, ONE_MINOR, DEFICIENT, BETTER,
        units=["U-01"], overrides={"instructions": INSTRUCTION, "max_revision_rounds": 1},
    )  # fmt: skip

    assert state == "completed"
    assert [request.stage for request in seen].count("revise") == 1
    stopped = _event(events, "review.stopped")
    assert (stopped.data["stop_reason"], stopped.data["rounds"]) == ("round_limit", 1)
    assert _event(events, "unit.committed").data["version"] == 2


def test_a_revision_that_raises_validation_failures_pauses_the_unit_and_keeps_its_version(
    runtime: Runtime,
) -> None:
    project_id = _drafted(runtime)
    state, events, seen = _run(
        runtime, project_id, "revise", WORSE,
        units=["U-01"], overrides={"instructions": INSTRUCTION},
    )  # fmt: skip

    assert state == "completed", "the stage completes; the unit pauses"
    assert [request.stage for request in seen] == ["revise"], (
        "nothing is audited after a regression"
    )
    paused = _event(events, "unit.paused")
    assert "version 1" in paused.message
    unit = _unit(runtime, project_id)
    assert unit.state == "paused"
    assert unit.current_version_id is not None, "the committed version is still the unit's"
    assert _versions(runtime, project_id) == [(1, True)]

    # A paused unit with a version is revised again: data model §3's "user resumes with
    # instructions" arrow.
    state, events, _seen = _run(
        runtime, project_id, "revise", BETTER, NO_FINDINGS, ACCEPTABLE,
        units=["U-01"], overrides={"instructions": INSTRUCTION},
    )  # fmt: skip
    assert state == "completed"
    assert _unit(runtime, project_id).state == "committed"
    assert _versions(runtime, project_id) == [(2, True), (1, True)]


def test_a_review_that_changes_nothing_returns_the_unit_to_its_committed_version(
    runtime: Runtime,
) -> None:
    project_id = _drafted(runtime)
    state, events, seen = _run(
        runtime, project_id, "revise", NO_FINDINGS, ACCEPTABLE, units=["U-01"]
    )

    assert state == "completed"
    assert "revise" not in [request.stage for request in seen]
    assert _event(events, "unit.unchanged")
    assert not [e for e in events if e.event_type == "unit.committed"]
    assert _unit(runtime, project_id).state == "committed"
    assert _versions(runtime, project_id) == [(1, True)], "no identical second version"


def test_a_unit_with_no_committed_version_is_skipped_with_its_reason(runtime: Runtime) -> None:
    project_id = _planned(runtime)
    state, events, seen = _run(
        runtime, project_id, "revise", BETTER,
        units=["U-01"], overrides={"instructions": INSTRUCTION},
    )  # fmt: skip

    assert state == "completed"
    assert seen == []
    assert "no committed version" in _event(events, "unit.skipped").message
    assert _unit(runtime, project_id).state == "planned"


# --- Overrides ------------------------------------------------------------------------------------


def test_max_revision_rounds_on_a_draft_run_bounds_that_run_only(runtime: Runtime) -> None:
    project_id = _planned(runtime)
    state, events, seen = _run(
        runtime, project_id, "draft", DRAFT, ONE_MINOR, DEFICIENT, BETTER,
        overrides={"max_revision_rounds": 0},
    )  # fmt: skip

    assert state == "completed"
    assert "revise" not in [request.stage for request in seen]
    stopped = _event(events, "review.stopped")
    assert (stopped.data["stop_reason"], stopped.data["rounds"]) == ("round_limit", 0)
    assert runtime.settings.workflow.max_revision_rounds == 3, "the settings are not rewritten"


def test_model_hint_names_the_model_for_every_call_of_that_run_only(runtime: Runtime) -> None:
    from sqlalchemy import select

    from ideapress.infrastructure.db.models import Attempt as AttemptRow

    project_id = _planned(runtime)
    state, _events, seen = _run(
        runtime, project_id, "draft", DRAFT, NO_FINDINGS, ACCEPTABLE,
        overrides={"model_hint": HINT},
    )  # fmt: skip
    assert state == "completed"
    assert {request.stage for request in seen} >= {"draft", "audit_fast", "critique"}
    assert {request.model_hint for request in seen} == {HINT}
    with runtime.storage.read() as session:
        names = set(
            session.scalars(
                select(AttemptRow.model_provider_name).where(AttemptRow.unit_id.is_not(None))
            ).all()
        )
    assert names == {"qwen3.5:9b-q8_0"}

    other = _planned(runtime, "Another project")
    state, _events, seen = _run(runtime, other, "draft", DRAFT, NO_FINDINGS, ACCEPTABLE)
    assert state == "completed"
    drafts = [request for request in seen if request.stage == "draft"]
    assert drafts[0].model_hint == "ollama/gemma4:12b", "the next run is back on its binding"


@pytest.mark.parametrize(
    ("stage", "overrides", "path"),
    [
        ("draft", {"temperature": 0.9}, "overrides.temperature"),
        ("draft", {"instructions": "x"}, "overrides.instructions"),
        ("draft", {"max_revision_rounds": -1}, "overrides.max_revision_rounds"),
        ("draft", {"max_revision_rounds": "three"}, "overrides.max_revision_rounds"),
        ("draft", {"model_hint": ""}, "overrides.model_hint"),
        ("research", {"model_hint": HINT}, "overrides.model_hint"),
        ("project_review", {"max_revision_rounds": 1}, "overrides.max_revision_rounds"),
    ],
)
def test_an_override_the_stage_does_not_read_is_refused_by_name_before_anything_starts(
    runtime: Runtime, stage: str, overrides: dict[str, Any], path: str
) -> None:
    from sqlalchemy import func, select

    from ideapress.infrastructure.db.models import StageRun as StageRunRow
    from ideapress.services.stage_bodies import start_stage

    project_id = runtime.projects.create(title="Refused", brief=BRIEF).id
    with pytest.raises(ValidationError) as caught:
        start_stage(runtime, project_id=project_id, stage=stage, overrides=overrides)  # type: ignore[arg-type]
    assert [field["path"] for field in caught.value.details["fields"]] == [path]
    with runtime.storage.read() as session:
        assert session.scalar(select(func.count()).select_from(StageRunRow)) == 0


# --- Over HTTP ------------------------------------------------------------------------------------


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app(load_settings().settings, runtime_builder=build_runtime)
    with TestClient(app, base_url="http://127.0.0.1:8767") as test_client:
        yield test_client


def test_the_revise_route_starts_a_revise_run(client: TestClient) -> None:
    runtime: Runtime = client.app.state.runtime  # type: ignore[attr-defined]
    project_id = _drafted(runtime)
    _with(runtime, _script(BETTER, NO_FINDINGS, ACCEPTABLE))

    response = client.post(
        f"/api/v1/projects/{project_id}/units/U-01/revise", json={"instructions": INSTRUCTION}
    )
    assert response.status_code == 202, response.text
    assert response.json()["stage"] == "revise"
    assert _wait(runtime, response.json()["task_id"]) == "completed"
    assert _versions(runtime, project_id) == [(2, True), (1, True)]


def test_a_refused_override_is_400_naming_it(client: TestClient) -> None:
    project_id = client.post("/api/v1/projects", json={"title": "Refused"}).json()["id"]
    response = client.post(
        f"/api/v1/projects/{project_id}/stages/draft/run", json={"overrides": {"seed": 1}}
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"]["fields"][0]["path"] == "overrides.seed"
