"""``tool_call_records``: ToolYard's record shape in IdeaPress's own table (row M1, ADR-0116).

The store collects, ``record_attempt`` writes, and the row commits with the attempt that owns it.
What is proved here is the ownership half — the round trip, the refusal, and the foreign key —
rather than the stage that produces the records, which is ``test_research_stage.py``'s.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select
from toolyard import EgressClass, Reason, RiskClass, ToolCallRecord, ToolStatus

from ideapress.infrastructure.db.models import Project as ProjectRow
from ideapress.infrastructure.db.models import StageRun as StageRunRow
from ideapress.infrastructure.db.models import ToolCallRecord as ToolCallRow
from ideapress.infrastructure.tool_calls import CollectingToolCallStore
from ideapress.services.database import Database, upgrade
from ideapress.services.stages import record_attempt

if TYPE_CHECKING:
    from pathlib import Path

AT = datetime(2026, 9, 8, tzinfo=UTC)


@pytest.fixture
def database(tmp_path: Path) -> Database:
    db = Database.from_url(f"sqlite:///{tmp_path / 't.sqlite3'}")
    upgrade(db)
    return db


def _project_and_run(database: Database) -> tuple[str, str]:
    with database.write() as session:
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
        run = StageRunRow(project_id=project.id, stage="research", state="running")
        session.add(run)
        session.flush()
        return project.id, run.id


def _record(
    *,
    status: ToolStatus = ToolStatus.OK,
    reason: str | None = None,
    reason_detail: str | None = None,
    tool_name: str = "http_fetch",
) -> ToolCallRecord:
    return ToolCallRecord(
        invocation_id="inv-1",
        tool_name=tool_name,
        args_json='{"url": "http://127.0.0.1/a"}',
        args_sha256="sha256:" + "0" * 64,
        status=status,
        reason=reason,
        reason_detail=reason_detail,
        result_summary="hello",
        result_sha256="sha256:" + "1" * 64,
        duration_ms=7,
        risk_class=RiskClass.READ_ONLY,
        egress=EgressClass.NETWORK,
        started_at=AT,
    )


def test_a_record_round_trips_field_for_field(database: Database) -> None:
    """Every field ToolYard produces survives the write, unreshaped."""
    project_id, run_id = _project_and_run(database)
    store = CollectingToolCallStore()
    store.append(_record())

    attempt_id = record_attempt(
        database,
        stage_run_id=run_id,
        stage="research",
        result=None,
        project_id=project_id,
        tool_calls=store.records,
    )

    with database.read() as session:
        row = session.scalars(select(ToolCallRow)).one()
    assert row.attempt_id == attempt_id
    assert row.project_id == project_id
    assert row.invocation_id == "inv-1"
    assert row.tool_name == "http_fetch"
    assert row.args_json == '{"url": "http://127.0.0.1/a"}'
    assert row.args_sha256 == "sha256:" + "0" * 64
    assert row.status == "ok"
    assert row.reason is None
    assert row.result_summary == "hello"
    assert row.result_sha256 == "sha256:" + "1" * 64
    assert row.duration_ms == 7
    assert row.risk_class == "read_only"
    assert row.egress == "network"
    assert row.started_at == AT


def test_a_refusal_is_recorded_with_the_check_that_said_no(database: Database) -> None:
    """A refused call is a row naming the reason: toolyard §11.2's "from the record alone"."""
    project_id, run_id = _project_and_run(database)
    store = CollectingToolCallStore()
    store.append(
        _record(
            status=ToolStatus.REFUSED,
            reason=Reason.HOST_NOT_ALLOWED.value,
            reason_detail="the host is not on this stage's allowlist",
        )
    )

    record_attempt(
        database,
        stage_run_id=run_id,
        stage="research",
        result=None,
        outcome="refused",
        project_id=project_id,
        tool_calls=store.records,
    )

    with database.read() as session:
        row = session.scalars(select(ToolCallRow)).one()
    assert row.status == "refused"
    assert row.reason == "host_not_allowed"
    assert row.reason_detail == "the host is not on this stage's allowlist"


def test_a_name_no_tool_has_is_recorded_as_asked_for(database: Database) -> None:
    """`unknown_tool` records the name given: a refusal that hides it cannot be diagnosed."""
    project_id, run_id = _project_and_run(database)
    store = CollectingToolCallStore()
    store.append(
        _record(
            tool_name="http_fetch",
            status=ToolStatus.REFUSED,
            reason=Reason.UNKNOWN_TOOL.value,
        )
    )

    record_attempt(
        database,
        stage_run_id=run_id,
        stage="research",
        result=None,
        outcome="refused",
        project_id=project_id,
        tool_calls=store.records,
    )

    with database.read() as session:
        row = session.scalars(select(ToolCallRow)).one()
    assert row.tool_name == "http_fetch"
    assert row.reason == "unknown_tool"


def test_the_row_is_deleted_with_its_attempt(database: Database) -> None:
    """The foreign key cascades: a purged project takes its tool calls with it.

    WeightsDB turns SQLite's foreign keys on per connection, so ``ON DELETE CASCADE`` is enforced
    here rather than merely declared — which is the half a migration alone does not prove.
    """
    project_id, run_id = _project_and_run(database)
    store = CollectingToolCallStore()
    store.append(_record())
    record_attempt(
        database,
        stage_run_id=run_id,
        stage="research",
        result=None,
        project_id=project_id,
        tool_calls=store.records,
    )

    with database.write() as session:
        session.delete(session.get(ProjectRow, project_id))

    with database.read() as session:
        assert session.scalars(select(ToolCallRow)).all() == []


def test_records_without_a_project_are_a_caller_bug(database: Database) -> None:
    """A row that cannot be found from its project is refused before it is written."""
    _project_id, run_id = _project_and_run(database)
    store = CollectingToolCallStore()
    store.append(_record())

    with pytest.raises(ValueError, match="project_id"):
        record_attempt(
            database,
            stage_run_id=run_id,
            stage="research",
            result=None,
            tool_calls=store.records,
        )


def test_an_attempt_with_no_tool_calls_writes_no_rows(database: Database) -> None:
    """Every other call site of `record_attempt` is unaffected, by construction."""
    _project_id, run_id = _project_and_run(database)
    record_attempt(database, stage_run_id=run_id, stage="draft", result=None)

    with database.read() as session:
        assert session.scalars(select(ToolCallRow)).all() == []


def test_the_store_snapshot_cannot_be_surprised_by_a_later_append() -> None:
    """`records` is a tuple, so a caller iterating it holds what it held."""
    store = CollectingToolCallStore()
    store.append(_record())
    snapshot = store.records
    store.append(_record())
    assert len(snapshot) == 1
    assert len(store.records) == 2
