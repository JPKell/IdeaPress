"""`ideapress plan` and `ideapress stage`: the same service the API reaches, through a terminal."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from ideapress.cli.main import app as cli_app

if TYPE_CHECKING:
    from collections.abc import Iterator

runner = CliRunner()

BRIEF = (
    "The article must state that inference runs entirely on the reader's own machine and that no "
    "document content is uploaded anywhere."
)
REQUIREMENTS = {
    "requirements": [
        {
            "text": "The article must be explicit about where inference happens.",
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
        },
        {"title": "Consequences", "goal_text": "Draw them out.", "requirement_keys": ["R-001"]},
    ]
}


@pytest.fixture(autouse=True)
def scripted_backend(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Make every runtime this process builds use a scripted backend."""
    import json as json_module

    from modelrack.testing import FakeGeneration, FakeScript

    from ideapress.infrastructure.backends import fake as fake_module
    from ideapress.services import backends as backends_module

    script = FakeScript(
        models=fake_module.default_fake_script().models,
        capabilities=fake_module.default_fake_script().capabilities,
        generations=(
            FakeGeneration(text=json_module.dumps(REQUIREMENTS)),
            FakeGeneration(text=json_module.dumps(PLAN)),
        ),
        repeat_final_generation=True,
    )

    def build(settings: Any, *, mode: str | None = None) -> Any:
        return fake_module.FakeBackend(script=script, seed=5)

    monkeypatch.setattr(backends_module, "build_backend", build)
    monkeypatch.setattr("ideapress.services.runtime.build_backend", build)
    yield


def _project() -> str:
    runner.invoke(cli_app, ["db", "upgrade"])
    created = runner.invoke(
        cli_app, ["project", "create", "Local inference for writers", "--brief", BRIEF, "--json"]
    )
    assert created.exit_code == 0, created.output
    identifier = json.loads(created.stdout)["id"]
    assert isinstance(identifier, str)
    return identifier


def test_plan_build_then_show() -> None:
    project_id = _project()
    built = runner.invoke(cli_app, ["plan", "build", project_id])
    assert built.exit_code == 0, built.output
    assert "Plan built." in built.stdout
    assert "requirements.compiled" in built.stdout

    shown = runner.invoke(cli_app, ["plan", "show", project_id])
    assert shown.exit_code == 0
    assert "R-001" in shown.stdout
    assert "BLOCKING" in shown.stdout
    assert "inference runs entirely on the reader's own machine" in shown.stdout, (
        "the quotation is printed beside the claim, in the terminal as much as in the UI"
    )
    assert "U-01" in shown.stdout


def test_plan_show_json_is_machine_readable() -> None:
    project_id = _project()
    runner.invoke(cli_app, ["plan", "build", project_id])
    payload = json.loads(runner.invoke(cli_app, ["plan", "show", project_id, "--json"]).stdout)
    assert [r["key"] for r in payload["requirements"]] == ["R-001"]
    assert payload["requirements"][0]["units"] == ["U-01", "U-02"]
    assert [u["key"] for u in payload["units"]] == ["U-01", "U-02"]


def test_plan_show_before_planning_reports_emptiness_plainly() -> None:
    project_id = _project()
    shown = runner.invoke(cli_app, ["plan", "show", project_id])
    assert shown.exit_code == 0
    assert "(none compiled)" in shown.stdout
    assert "(no plan)" in shown.stdout


def test_plan_build_exits_one_when_the_gate_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """A model that says "no requirements needed" fails the command, visibly."""
    import json as json_module

    from modelrack.testing import FakeGeneration, FakeScript

    from ideapress.infrastructure.backends import fake as fake_module

    script = FakeScript(
        models=fake_module.default_fake_script().models,
        capabilities=fake_module.default_fake_script().capabilities,
        generations=(
            FakeGeneration(text=json_module.dumps({"requirements": []})),
            FakeGeneration(text=json_module.dumps(PLAN)),
        ),
        repeat_final_generation=True,
    )
    monkeypatch.setattr(
        "ideapress.services.runtime.build_backend",
        lambda settings, mode=None: fake_module.FakeBackend(script=script, seed=5),
    )
    project_id = _project()
    built = runner.invoke(cli_app, ["plan", "build", project_id])
    assert built.exit_code == 1
    assert "failed" in built.output.lower()


def test_stage_list_reports_the_whole_table() -> None:
    listed = runner.invoke(cli_app, ["stage", "list"])
    assert listed.exit_code == 0
    assert "requirements" in listed.stdout
    assert "fact_check" in listed.stdout
    assert listed.stdout.count("\n") == 16
    rows = json.loads(runner.invoke(cli_app, ["stage", "list", "--json"]).stdout)
    assert len(rows) == 16
    assert sum(1 for row in rows if not row["uses_model"]) == 5


def test_stage_status_reports_a_finished_run() -> None:
    project_id = _project()
    built = runner.invoke(cli_app, ["plan", "build", project_id])
    task_id = built.stdout.split("task ", 1)[1].split("\n", 1)[0].strip()

    status = runner.invoke(cli_app, ["stage", "status", project_id, task_id])
    assert status.exit_code == 0
    assert "completed" in status.stdout
    assert "requirements" in status.stdout
    assert "outline" in status.stdout

    payload = json.loads(
        runner.invoke(cli_app, ["stage", "status", project_id, task_id, "--json"]).stdout
    )
    assert payload["state"] == "completed"
    assert len(payload["attempts"]) == 2


def test_stage_run_refuses_a_stage_with_no_implementation() -> None:
    project_id = _project()
    runner.invoke(cli_app, ["plan", "build", project_id])
    result = runner.invoke(cli_app, ["stage", "run", project_id, "critique"])
    assert result.exit_code != 0


def test_stage_cancel_on_a_finished_task_is_not_an_error() -> None:
    project_id = _project()
    built = runner.invoke(cli_app, ["plan", "build", project_id])
    task_id = built.stdout.split("task ", 1)[1].split("\n", 1)[0].strip()
    cancelled = runner.invoke(cli_app, ["stage", "cancel", project_id, task_id])
    assert cancelled.exit_code == 0
    assert "not running" in cancelled.stdout


GOOD_DRAFT = (
    "Everything happens on your own machine. The model reads what you wrote, and nothing you "
    "wrote is uploaded to anyone: no account, no sync, no telemetry carrying your sentences "
    "somewhere else. The trade-off is that the hardware is yours to provide."
)


def _drafted(monkeypatch: pytest.MonkeyPatch) -> str:
    """A project planned and then drafted, with the backend swapped between the two."""

    import json as json_module

    from modelrack.testing import FakeGeneration, FakeScript

    from ideapress.infrastructure.backends import fake as fake_module

    project_id = _project()
    assert runner.invoke(cli_app, ["plan", "build", project_id]).exit_code == 0

    script = FakeScript(
        models=fake_module.default_fake_script().models,
        capabilities=fake_module.default_fake_script().capabilities,
        generations=(
            FakeGeneration(text=GOOD_DRAFT),
            # The review stage runs after validation: a clean audit and an accepting critique.
            FakeGeneration(text=json_module.dumps({"findings": []})),
            FakeGeneration(text=json_module.dumps({"verdict": "acceptable", "rationale": "ok"})),
        ),
        repeat_final_generation=True,
    )
    monkeypatch.setattr(
        "ideapress.services.runtime.build_backend",
        lambda settings, mode=None: fake_module.FakeBackend(script=script, seed=5),
    )
    assert runner.invoke(cli_app, ["stage", "run", project_id, "draft"]).exit_code == 0
    return project_id


def test_unit_list_and_show(monkeypatch: pytest.MonkeyPatch) -> None:
    project_id = _drafted(monkeypatch)
    listed = runner.invoke(cli_app, ["unit", "list", project_id])
    assert listed.exit_code == 0
    assert "U-01" in listed.stdout
    assert "committed" in listed.stdout

    shown = runner.invoke(cli_app, ["unit", "show", project_id, "U-01"])
    assert shown.exit_code == 0
    assert "own machine" in shown.stdout


def test_unit_show_provenance_names_everything_workflows_8_asks_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = _drafted(monkeypatch)
    shown = runner.invoke(cli_app, ["unit", "show", project_id, "U-01", "--provenance"])
    assert shown.exit_code == 0
    for expected in ("COVERAGE", "VALIDATION", "ATTEMPTS", "stages.draft.write", "sha256:"):
        assert expected in shown.stdout, expected
    assert "deterministic_check" in shown.stdout


def test_unit_history_reports_each_version(monkeypatch: pytest.MonkeyPatch) -> None:
    project_id = _drafted(monkeypatch)
    shown = runner.invoke(cli_app, ["unit", "history", project_id, "U-01"])
    assert shown.exit_code == 0
    assert "v1" in shown.stdout
    assert "committed" in shown.stdout
