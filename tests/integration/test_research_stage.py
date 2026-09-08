"""The `research` stage end to end: targets, egress, records, notes (row M1, ADR-0116).

No model runs here — the stage reaches none — and no socket opens: the fetch path is driven
through `httpx.MockTransport` with an injected resolver, which is what keeps spec §20 AC11 true
now that a network tool ships.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import httpx
import pytest
from sqlalchemy import select

from ideapress.config import ResearchSettings, Settings, load_settings
from ideapress.infrastructure.db.models import Attempt as AttemptRow
from ideapress.infrastructure.db.models import Source as SourceRow
from ideapress.infrastructure.db.models import ToolCallRecord as ToolCallRow
from ideapress.services.research import SOURCES_DIRECTORY_NAME, project_notes, research_body
from ideapress.services.runtime import Runtime, build_runtime

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

BRIEF_WITH_URL = """
# Local inference for writers

Background reading: https://docs.example/local-inference — use it for the numbers.
""".strip()

BRIEF_WITHOUT_URL = "# Local inference for writers\n\nNo sources; write from the outline."


def _resolver(_host: str) -> Sequence[str]:
    return ["93.184.216.34"]


def _transport(body: str = "Local inference runs on your own machine.") -> httpx.MockTransport:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/plain"})

    return httpx.MockTransport(handler)


@pytest.fixture
def settings() -> Settings:
    return load_settings().settings


@pytest.fixture
def runtime(settings: Settings) -> Iterator[Runtime]:
    built = build_runtime(settings)
    yield built
    built.close()


def _configure(runtime: Runtime, **overrides: object) -> None:
    runtime.settings.research = ResearchSettings(**overrides)  # type: ignore[arg-type]


def _wait(runtime: Runtime, task_id: str, *, timeout: float = 15.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if runtime.runner.is_finished(task_id):
            return runtime.runner.run_state(task_id) or "unknown"
        time.sleep(0.02)
    message = f"stage {task_id} did not finish within {timeout}s"
    raise AssertionError(message)


def _run_research(
    runtime: Runtime,
    project_id: str,
    *,
    transport: httpx.MockTransport | None = None,
) -> str:
    body = research_body(
        runtime,
        project_id=project_id,
        resolver=_resolver,
        transport=transport if transport is not None else _transport(),
    )
    task = runtime.runner.start(project_id=project_id, stage="research", body=body)
    return _wait(runtime, task.run_id)


def _project(runtime: Runtime, brief: str) -> str:
    return runtime.projects.create(title="Local inference for writers", brief=brief).id


def _sources_dir(runtime: Runtime, project_id: str) -> Path:
    project = runtime.projects.get(project_id)
    return runtime.projects.directory(project) / SOURCES_DIRECTORY_NAME


# ---------------------------------------------------------------------------------------------
# Exit condition 1 — a URL in the brief becomes a note, and the call is recorded
# ---------------------------------------------------------------------------------------------


def test_a_url_in_the_brief_produces_a_note_with_its_citation(runtime: Runtime) -> None:
    _configure(runtime, allowed_hosts=("docs.example",), max_data_classification="public")
    project_id = _project(runtime, BRIEF_WITH_URL)

    assert _run_research(runtime, project_id) == "completed"

    with runtime.storage.read() as session:
        note = session.scalars(select(SourceRow)).one()
    assert note.kind == "url"
    assert note.path == "https://docs.example/local-inference"
    assert note.title == "https://docs.example/local-inference"
    assert note.content_text is not None
    assert "own machine" in note.content_text
    assert note.sha256.startswith("sha256:")


def test_the_call_is_recorded_on_the_provenance_table(runtime: Runtime) -> None:
    _configure(runtime, allowed_hosts=("docs.example",), max_data_classification="public")
    project_id = _project(runtime, BRIEF_WITH_URL)

    _run_research(runtime, project_id)

    with runtime.storage.read() as session:
        call = session.scalars(select(ToolCallRow)).one()
        attempt = session.get(AttemptRow, call.attempt_id)
    assert call.tool_name == "http_fetch"
    assert call.status == "ok"
    assert call.reason is None
    assert call.egress == "network"
    assert attempt is not None
    assert attempt.stage == "research"
    assert attempt.outcome == "completed"
    assert attempt.unit_id is None


def test_the_egress_decision_joins_the_call_by_invocation_id(runtime: Runtime) -> None:
    """The decision is rendered before the fetch, so it cannot carry an attempt id (ADR-0073)."""
    _configure(runtime, allowed_hosts=("docs.example",), max_data_classification="public")
    project_id = _project(runtime, BRIEF_WITH_URL)

    _run_research(runtime, project_id)

    egress = runtime.storage.egress
    assert egress is not None
    decisions = egress.decisions(target="research:docs.example")
    assert [d.verdict.value for d in decisions] == ["approved"]
    with runtime.storage.read() as session:
        call = session.scalars(select(ToolCallRow)).one()
    assert decisions[0].request.source_ref == call.invocation_id


# ---------------------------------------------------------------------------------------------
# Exit condition 2 — a disallowed host is refused as a result, visible, with a decision row
# ---------------------------------------------------------------------------------------------


def test_a_host_off_the_allowlist_is_refused_as_a_result(runtime: Runtime) -> None:
    _configure(runtime, allowed_hosts=("other.example",), max_data_classification="public")
    project_id = _project(runtime, BRIEF_WITH_URL)

    assert _run_research(runtime, project_id) == "completed"

    with runtime.storage.read() as session:
        call = session.scalars(select(ToolCallRow)).one()
        attempt = session.get(AttemptRow, call.attempt_id)
        assert session.scalars(select(SourceRow)).all() == []
    assert call.status == "refused"
    assert call.reason == "host_not_allowed"
    assert attempt is not None
    assert attempt.outcome == "refused"
    assert attempt.error_code == "host_not_allowed"


def test_a_remote_host_with_no_declared_ceiling_is_denied_and_never_fetched(
    runtime: Runtime,
) -> None:
    """Fail closed, ADR-0103 decision 2 moved to the fetch target."""
    _configure(runtime, allowed_hosts=("docs.example",))
    project_id = _project(runtime, BRIEF_WITH_URL)

    assert _run_research(runtime, project_id) == "completed"

    egress = runtime.storage.egress
    assert egress is not None
    decisions = egress.decisions(target="research:docs.example")
    assert [d.verdict.value for d in decisions] == ["denied"]

    with runtime.storage.read() as session:
        call = session.scalars(select(ToolCallRow)).one()
        assert session.scalars(select(SourceRow)).all() == []
    assert call.status == "refused"
    assert call.reason == "egress_not_permitted"


def test_a_denial_leaves_both_rows_and_raises_nothing(runtime: Runtime) -> None:
    """Exit condition 2, stated as the two rows and the absence of an exception."""
    _configure(runtime, allowed_hosts=("docs.example",))
    project_id = _project(runtime, BRIEF_WITH_URL)

    assert _run_research(runtime, project_id) == "completed"

    with runtime.storage.read() as session:
        calls = session.scalars(select(ToolCallRow)).all()
        attempts = session.scalars(select(AttemptRow)).all()
    egress = runtime.storage.egress
    assert egress is not None
    assert len(egress.decisions(run_id=f"project:{project_id}")) == 1
    assert len(calls) == 1
    # The stage ran to completion: nothing raised, and the run's own state says so above.
    assert [attempt.outcome for attempt in attempts] == ["refused"]


def test_an_unconfigured_installation_refuses_a_url_with_unknown_tool(runtime: Runtime) -> None:
    """No host named, so the tool is not registered — and nothing loopback is reachable either."""
    _configure(runtime)
    project_id = _project(runtime, BRIEF_WITH_URL)

    assert _run_research(runtime, project_id) == "completed"

    with runtime.storage.read() as session:
        call = session.scalars(select(ToolCallRow)).one()
        assert session.scalars(select(SourceRow)).all() == []
    assert call.reason == "unknown_tool"
    egress = runtime.storage.egress
    assert egress is not None
    # The decision is still rendered and recorded: a call refused after a verdict and a call that
    # was never allowed to have one are different audits.
    assert len(egress.decisions(target="research:docs.example")) == 1


# ---------------------------------------------------------------------------------------------
# Exit condition 3 — nothing to research is not an error, and changes nothing
# ---------------------------------------------------------------------------------------------


def test_a_brief_with_no_url_and_no_files_completes_and_writes_nothing(runtime: Runtime) -> None:
    _configure(runtime, allowed_hosts=("docs.example",), max_data_classification="public")
    project_id = _project(runtime, BRIEF_WITHOUT_URL)

    assert _run_research(runtime, project_id) == "completed"

    with runtime.storage.read() as session:
        assert session.scalars(select(SourceRow)).all() == []
        assert session.scalars(select(ToolCallRow)).all() == []
        assert session.scalars(select(AttemptRow)).all() == []
    assert project_notes(runtime, project_id) == []


# ---------------------------------------------------------------------------------------------
# read_file: the local half, which needs no network at all
# ---------------------------------------------------------------------------------------------


def test_a_file_in_the_sources_directory_becomes_a_note(runtime: Runtime) -> None:
    _configure(runtime)
    project_id = _project(runtime, BRIEF_WITHOUT_URL)
    sources = _sources_dir(runtime, project_id)
    sources.mkdir(parents=True, exist_ok=True)
    (sources / "field-notes.md").write_text("Measured on the reference machine.", encoding="utf-8")

    assert _run_research(runtime, project_id) == "completed"

    with runtime.storage.read() as session:
        note = session.scalars(select(SourceRow)).one()
    assert note.kind == "file"
    assert note.path == "field-notes.md"
    assert note.content_text == "Measured on the reference machine."


def test_an_export_beside_the_sources_directory_is_never_ingested(runtime: Runtime) -> None:
    """The read root is `sources/`, not the project directory an export is written into."""
    _configure(runtime)
    project_id = _project(runtime, BRIEF_WITHOUT_URL)
    project = runtime.projects.get(project_id)
    directory = runtime.projects.directory(project)
    (directory / f"{project.slug}.md").write_text("the exported document", encoding="utf-8")

    assert _run_research(runtime, project_id) == "completed"

    with runtime.storage.read() as session:
        assert session.scalars(select(SourceRow)).all() == []


def test_a_subdirectory_is_not_a_target(runtime: Runtime) -> None:
    _configure(runtime)
    project_id = _project(runtime, BRIEF_WITHOUT_URL)
    sources = _sources_dir(runtime, project_id)
    (sources / "nested").mkdir(parents=True, exist_ok=True)
    (sources / "nested" / "deep.md").write_text("not a target", encoding="utf-8")

    assert _run_research(runtime, project_id) == "completed"

    with runtime.storage.read() as session:
        assert session.scalars(select(SourceRow)).all() == []


# ---------------------------------------------------------------------------------------------
# Re-running, and the notes reaching the workflow
# ---------------------------------------------------------------------------------------------


def test_running_twice_replaces_the_notes_rather_than_doubling_them(runtime: Runtime) -> None:
    """Two runs of the same brief must not give one source twice the weight in a budget."""
    _configure(runtime, allowed_hosts=("docs.example",), max_data_classification="public")
    project_id = _project(runtime, BRIEF_WITH_URL)

    _run_research(runtime, project_id)
    _run_research(runtime, project_id, transport=_transport("The source has since changed."))

    with runtime.storage.read() as session:
        notes = session.scalars(select(SourceRow)).all()
        calls = session.scalars(select(ToolCallRow)).all()
    assert len(notes) == 1
    assert notes[0].content_text == "The source has since changed."
    # Every call is still recorded: the notes are replaced, the audit trail is not.
    assert len(calls) == 2


def test_the_notes_reach_assemble_context_as_title_and_text(runtime: Runtime) -> None:
    """J2's dead `research_notes` path, reopened (ADR-0116 decision 5)."""
    _configure(runtime, allowed_hosts=("docs.example",), max_data_classification="public")
    project_id = _project(runtime, BRIEF_WITH_URL)

    _run_research(runtime, project_id)

    assert project_notes(runtime, project_id) == [
        ("https://docs.example/local-inference", "Local inference runs on your own machine.")
    ]


def test_a_note_with_no_text_is_omitted_from_the_context(runtime: Runtime) -> None:
    """An empty note ranks and budgets like a real one and tells a model nothing."""
    _configure(runtime, allowed_hosts=("docs.example",), max_data_classification="public")
    project_id = _project(runtime, BRIEF_WITH_URL)

    _run_research(runtime, project_id, transport=_transport("   "))

    assert project_notes(runtime, project_id) == []
