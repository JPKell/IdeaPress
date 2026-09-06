"""An attempt names the adapter subject that answered it (gate E, ADR-0080).

The provenance half of Phase 10. What is asserted is the *source* of the fact as much as the fact:
the row records what the backend said answered, and a stage that asked for an adapter and was given
one records that adapter — but the two are read from different places on purpose, because a pin can
be refused between them (workflows §8).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from ideapress.domain.inference import AdapterSubject, ModelIdentity, StageResult
from ideapress.infrastructure.db.models import Attempt as AttemptRow
from ideapress.services.database import Database, upgrade
from ideapress.services.stages import record_attempt

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

MODEL = ModelIdentity(
    provider_kind="llamacpp",
    provider_model_name="Qwen2.5-1.5B-Instruct.Q8_0",
    artifact_digest="sha256:5926a692b27b",
)
SUBJECT = (
    "llamacpp/Qwen2.5-1.5B-Instruct.Q8_0@sha256:5926a692b27b"
    "+qwen2-5-1-5b-instruct-terse@sha256:c582629216c5"
)


@pytest.fixture
def sqlite_database(tmp_path: Path) -> Iterator[Database]:
    """A migrated, empty database of IdeaPress's own schema."""
    database = Database.from_url(f"sqlite:///{tmp_path / 'ideapress.sqlite3'}")
    upgrade(database)
    yield database
    database.close()


@pytest.fixture
def stage_run(sqlite_database: Database) -> str:
    """One project and one stage run, so an attempt has parents to belong to."""
    from datetime import UTC, datetime

    from ideapress.infrastructure.db.models import Project, StageRun

    now = datetime.now(UTC)
    with sqlite_database.write() as session:
        project = Project(
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
        run = StageRun(
            project_id=project.id,
            stage="draft",
            state="completed",
            units_total=1,
            units_completed=1,
            units_paused=0,
            started_at=now,
            options_json={},
            backend="loadcoach",
            backend_mode="loadcoach",
        )
        session.add(run)
        session.flush()
        return str(run.id)


def _row(database: Database) -> AttemptRow:
    with database.read() as session:
        return session.execute(select(AttemptRow)).scalars().one()


def test_a_pinned_attempt_records_the_adapter_that_answered(
    sqlite_database: Database, stage_run: str
) -> None:
    """Name, digest and subject string, straight from the backend's result."""
    record_attempt(
        sqlite_database,
        stage_run_id=stage_run,
        stage="draft",
        result=StageResult(
            text="terse.",
            model=MODEL,
            backend="loadcoach",
            adapter=AdapterSubject(
                name="qwen2-5-1-5b-instruct-terse",
                artifact_digest="sha256:c582629216c5",
                subject_canonical_id=SUBJECT,
            ),
        ),
    )

    row = _row(sqlite_database)
    assert row.adapter_name == "qwen2-5-1-5b-instruct-terse"
    assert row.adapter_digest == "sha256:c582629216c5"
    assert row.subject_canonical_id == SUBJECT
    assert row.model_canonical_id == MODEL.canonical_id


def test_an_unpinned_attempt_records_null_not_an_empty_string(
    sqlite_database: Database, stage_run: str
) -> None:
    """ "No adapter answered" and "an adapter whose name we do not know" are different facts."""
    record_attempt(
        sqlite_database,
        stage_run_id=stage_run,
        stage="draft",
        result=StageResult(text="plain.", model=MODEL, backend="loadcoach"),
    )

    row = _row(sqlite_database)
    assert row.adapter_name is None
    assert row.adapter_digest is None
    assert row.subject_canonical_id is None


def test_a_failed_attempt_names_no_subject_either(
    sqlite_database: Database, stage_run: str
) -> None:
    """A stage that produced no result produced no subject; nothing is inferred to fill the gap."""
    record_attempt(
        sqlite_database,
        stage_run_id=stage_run,
        stage="draft",
        result=None,
        outcome="provider_error",
        error_code="ADAPTER_NOT_FOUND",
    )

    row = _row(sqlite_database)
    assert row.outcome == "provider_error"
    assert row.error_code == "ADAPTER_NOT_FOUND"
    assert row.adapter_name is None
    assert row.subject_canonical_id is None


def test_three_stages_with_three_adapters_are_distinguishable_from_the_database_alone(
    sqlite_database: Database, stage_run: str
) -> None:
    """The demonstration's third assertion, at the persistence layer.

    A query over `attempts` has to separate the three stages by the subject that answered them,
    without joining anything and without parsing the subject string (ADR-0024 §4) — which is why
    the name and the digest are their own columns beside it.
    """
    for stage, adapter in (
        ("draft", "pirate"),
        ("revise", "terse"),
        ("critique", "verbose"),
    ):
        record_attempt(
            sqlite_database,
            stage_run_id=stage_run,
            stage=stage,
            result=StageResult(
                text=f"{adapter} text",
                model=MODEL,
                backend="loadcoach",
                adapter=AdapterSubject(
                    name=adapter,
                    artifact_digest=f"sha256:{adapter[:12]}",
                    subject_canonical_id=f"{MODEL.canonical_id}+{adapter}@sha256:{adapter[:12]}",
                ),
            ),
        )

    with sqlite_database.read() as session:
        rows = session.execute(
            select(AttemptRow.stage, AttemptRow.adapter_name).order_by(AttemptRow.id)
        ).all()
    assert [(r.stage, r.adapter_name) for r in rows] == [
        ("draft", "pirate"),
        ("revise", "terse"),
        ("critique", "verbose"),
    ]
