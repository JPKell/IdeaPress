"""The two exits, at the page layer (row J1 §6.1/§6.2): what a real project's page shows.

Gate C's demonstration for LoadLedger and Gate D's for Commissioner — one test each, through
:func:`~ideapress.services.unit_reports.unit_detail` and
:func:`~ideapress.services.workspace.workspace_view`, the same functions the workspace route
renders. Built through :func:`~ideapress.services.runtime.build_runtime`, so governance is attached
exactly as it is in a served process (no manual `attach_governance` call, unlike
``test_stage_governance.py``, which tests the funnel one layer down).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from ideapress.config import load_settings
from ideapress.domain.inference import ModelIdentity, StageResult, TokenUsage
from ideapress.infrastructure.db.models import Project as ProjectRow
from ideapress.infrastructure.db.models import StageRun as StageRunRow
from ideapress.infrastructure.db.models import Unit as UnitRow
from ideapress.services.runtime import build_runtime
from ideapress.services.stages import record_attempt
from ideapress.services.unit_reports import unit_detail
from ideapress.services.workspace import workspace_view

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from ideapress.services.runtime import Runtime

AT = datetime(2026, 9, 7, tzinfo=UTC)


@pytest.fixture
def project_with_a_drafted_unit(tmp_path: Path) -> Iterator[tuple[Runtime, str]]:
    """A runtime with governance attached, one project, one unit, one recorded attempt."""
    settings = load_settings(
        cli_overrides={"storage": {"database_url": f"sqlite:///{tmp_path / 't.sqlite3'}"}}
    ).settings
    runtime = build_runtime(settings)
    with runtime.storage.write() as session:
        project = ProjectRow(
            title="T",
            slug="t",
            content_type="article",
            content_type_version="1.0",
            workflow_id="default",
            workflow_version="1.0",
            status="active",
            brief_text="b",
            author_material_json=[],
            config_json={},
        )
        session.add(project)
        session.flush()
        run = StageRunRow(
            project_id=project.id,
            stage="draft",
            state="completed",
            units_total=1,
            started_at=AT,
            options_json={},
            backend="ollama",
            backend_mode="ollama",
        )
        session.add(run)
        unit = UnitRow(
            project_id=project.id, unit_key="U-01", ordinal=1, title="Unit", state="drafting"
        )
        session.add(unit)
        session.flush()
        project_id, run_id, unit_id = project.id, run.id, unit.id

    record_attempt(
        runtime.storage,
        stage_run_id=run_id,
        stage="draft",
        unit_id=unit_id,
        result=StageResult(
            text="Everything happens on your own machine.",
            model=ModelIdentity(provider_kind="ollama", provider_model_name="gemma4:12b"),
            backend="ollama",
            usage=TokenUsage(input_tokens=120, output_tokens=64),
        ),
    )
    yield runtime, project_id
    runtime.close()


def test_the_unit_page_answers_what_it_cost_honestly(
    project_with_a_drafted_unit: tuple[Runtime, str],
) -> None:
    """§6.1's exit: a project page answers "what did this cost?" — honestly, for a local run."""
    runtime, project_id = project_with_a_drafted_unit
    detail = unit_detail(runtime, project_id=project_id, unit_key="U-01")
    assert detail["cost"] is not None
    # Ollama is local and this installation names no pricing file: unpriced, never "$0.00".
    assert detail["cost"]["money_spent_display"] == "—"
    assert detail["cost"]["tokens_spent_display"] == "at least 184"
    (attempt,) = detail["attempts"]
    assert attempt["egress"] is not None
    assert attempt["egress"]["verdict"] == "approved"


def test_the_workspace_badge_reads_the_project_lifetime_balance(
    project_with_a_drafted_unit: tuple[Runtime, str],
) -> None:
    """§6.1's other half: the project-level figure, from the ``PER_TAG`` window, never resetting."""
    runtime, project_id = project_with_a_drafted_unit
    view = workspace_view(runtime, project_id=project_id)
    assert view["project_cost"] is not None
    assert view["project_cost"]["tokens_spent_display"] == "at least 184"
    assert view["backend"]["has_run"] is True
    assert view["backend"]["egress"] is False
    assert view["backend"]["verdict"] == "approved"
