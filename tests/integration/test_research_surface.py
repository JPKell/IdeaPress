"""What a person sees after the `research` stage ran: the unit page and the CLI (row M1, gate E).

The notes and the refusals are project-scoped facts shown on a unit's page, because a research
call runs before any unit exists and "which sources fed this unit, and which fetches were refused"
is still the question a person reading a unit's provenance is asking.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import httpx
import pytest
from sqlalchemy import select

from ideapress.config import ResearchSettings, load_settings
from ideapress.infrastructure.db.models import Unit as UnitRow
from ideapress.services.research import research_body
from ideapress.services.runtime import Runtime, build_runtime
from ideapress.services.unit_reports import unit_detail
from ideapress.web.rendering import render

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

BRIEF = "Background: https://docs.example/paper and https://blocked.example/x — read both."


def _resolver(_host: str) -> Sequence[str]:
    return ["93.184.216.34"]


def _transport() -> httpx.MockTransport:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text="A fetched paragraph.", headers={"content-type": "text/plain"}
        )

    return httpx.MockTransport(handler)


@pytest.fixture
def researched_project() -> Iterator[tuple[Runtime, str]]:
    """A project with one fetched note and one refused fetch, plus a unit to look at it from."""
    runtime = build_runtime(load_settings().settings)
    runtime.settings.research = ResearchSettings(
        allowed_hosts=("docs.example",), max_data_classification="public"
    )
    project_id = runtime.projects.create(title="Paper", brief=BRIEF).id
    with runtime.storage.write() as session:
        session.add(
            UnitRow(
                project_id=project_id, unit_key="U-01", ordinal=1, title="Unit", state="planned"
            )
        )
    task = runtime.runner.start(
        project_id=project_id,
        stage="research",
        body=research_body(
            runtime, project_id=project_id, resolver=_resolver, transport=_transport()
        ),
    )
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline and not runtime.runner.is_finished(task.run_id):
        time.sleep(0.02)
    yield runtime, project_id
    runtime.close()


def test_the_unit_detail_carries_the_notes_and_every_call(
    researched_project: tuple[Runtime, str],
) -> None:
    runtime, project_id = researched_project
    detail = unit_detail(runtime, project_id=project_id, unit_key="U-01")

    research = detail["research"]
    assert [note["citation"] for note in research["notes"]] == ["https://docs.example/paper"]
    assert [(call["tool"], call["status"], call["reason"]) for call in research["tool_calls"]] == [
        ("http_fetch", "ok", None),
        ("http_fetch", "refused", "host_not_allowed"),
    ]


def test_the_unit_page_shows_the_note_and_the_refusal(
    researched_project: tuple[Runtime, str],
) -> None:
    """Server-rendered, no new JS, no new route — the existing unit page grew one section."""
    runtime, project_id = researched_project
    detail = unit_detail(runtime, project_id=project_id, unit_key="U-01")

    page = render("units/detail.html", page="projects", page_title="U-01", **detail)

    assert "<h3>Research</h3>" in page
    assert "https://docs.example/paper" in page
    assert "host_not_allowed" in page
    assert "<script" not in page.split("<h3>Research</h3>", 1)[1].split("<h3>Provenance", 1)[0]


def test_a_project_that_never_researched_renders_the_empty_state() -> None:
    runtime = build_runtime(load_settings().settings)
    try:
        project_id = runtime.projects.create(title="Plain", brief="No sources.").id
        with runtime.storage.write() as session:
            session.add(
                UnitRow(
                    project_id=project_id,
                    unit_key="U-01",
                    ordinal=1,
                    title="Unit",
                    state="planned",
                )
            )
        detail = unit_detail(runtime, project_id=project_id, unit_key="U-01")
        assert detail["research"] == {"notes": [], "tool_calls": []}
        page = render("units/detail.html", page="projects", page_title="U-01", **detail)
        assert "The research stage has not run for this project." in page
    finally:
        runtime.close()


def test_the_cli_provenance_view_prints_the_notes_and_the_calls(
    researched_project: tuple[Runtime, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`ideapress unit show --provenance` is the verb that shows a unit's provenance."""
    from ideapress.cli.commands import plan as plan_commands
    from ideapress.cli.commands import unit as unit_commands

    runtime, project_id = researched_project

    def _runtime_for() -> Iterator[Runtime]:
        yield runtime

    monkeypatch.setattr(plan_commands, "runtime_for", _runtime_for)
    unit_commands.show(project_id, "U-01", json_output=False, provenance=True)

    printed = capsys.readouterr().out
    assert "RESEARCH" in printed
    assert "https://docs.example/paper" in printed
    assert "host_not_allowed" in printed


def test_only_the_research_stages_units_are_untouched(
    researched_project: tuple[Runtime, str],
) -> None:
    """The stage writes notes and records; it moves no unit and commits no version."""
    runtime, project_id = researched_project
    with runtime.storage.read() as session:
        states = session.scalars(
            select(UnitRow.state).where(UnitRow.project_id == project_id)
        ).all()
    assert list(states) == ["planned"]
