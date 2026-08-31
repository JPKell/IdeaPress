"""ideapress.infrastructure.db.models — the declarative base and the P1 tables.

IdeaPress owns this ``MetaData``/``DeclarativeBase`` exclusively (database standards §1): WeightsDB
provides plumbing only and defines no application table, so each application keeps its own base
with no cross-application meaning. **No application ever reads another's database.**

The naming convention is not cosmetic. Alembic's autogenerate diff and SQLite's batch-mode ALTER
both need every constraint and index to have a stable, predictable name; without one, a constraint
recreated by batch mode gets an auto-generated name that differs from the one the model produces,
and the parity check (database standards §5.2) fails forever on a schema that is actually correct.

``projects``, ``sources``, ``settings`` and ``api_tokens`` come from Phase 1's migration ``0001``;
``requirements``, ``units``, ``stage_runs``, ``attempts`` and ``stage_events`` from Phase 3's
``0002``.
SQLAlchemy models never leave the repository layer: a service returns a frozen domain value object,
never one of these.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from weightsdb import PortableJSON, UtcDateTime, ulid_primary_key

__all__ = [
    "ApiToken",
    "Attempt",
    "Base",
    "Project",
    "Requirement",
    "Setting",
    "Source",
    "StageEvent",
    "StageRun",
    "Unit",
    "utcnow",
]

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """The one declarative base for every IdeaPress-owned table.

    ``metadata`` here is the single source of truth Alembic's autogenerate compares against
    (``MigrationRunner.check_parity``) — a model added without importing it here is invisible to
    that check, not merely untested.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utcnow() -> datetime:
    """Return the current instant, timezone-aware in UTC.

    Used only as a ``mapped_column`` default for ``created_at``-style columns — an
    infrastructure-layer concern distinct from the ``Clock`` a service takes as a parameter.
    """
    return datetime.now(UTC)


class Project(Base):
    """One project: an idea, its brief, its plan, its units and its state (data model §2)."""

    __tablename__ = "projects"

    id: Mapped[str] = ulid_primary_key()
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    content_type: Mapped[str] = mapped_column(String(50), nullable=False)
    content_type_version: Mapped[str] = mapped_column(String(20), nullable=False)
    workflow_id: Mapped[str] = mapped_column(String(50), nullable=False)
    workflow_version: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    brief_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    author_material_json: Mapped[dict[str, Any]] = mapped_column(
        PortableJSON, nullable=False, default=dict
    )
    config_json: Mapped[dict[str, Any]] = mapped_column(PortableJSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    # Data model §5: the project list is `(status, updated_at DESC)`, asserted in a query-plan test.
    __table_args__ = (Index("ix_projects_status_updated_at", "status", "updated_at"),)


class Source(Base):
    """Author material attached to a project: a file, a note, or (opt-in) a URL."""

    __tablename__ = "sources"

    id: Mapped[str] = ulid_primary_key()
    project_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    path: Mapped[str | None] = mapped_column(Text, nullable=True)
    sha256: Mapped[str] = mapped_column(String(71), nullable=False)
    content_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        PortableJSON, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)

    __table_args__ = (Index("ix_sources_project_id", "project_id"),)


class Setting(Base):
    """Runtime-changeable settings, keyed by dotted path (api.md §6)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value_json: Mapped[Any] = mapped_column(PortableJSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )


class ApiToken(Base):
    """A bearer token for non-loopback access, stored as a hash and never in the clear."""

    __tablename__ = "api_tokens"

    id: Mapped[str] = ulid_primary_key()
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    token_sha256: Mapped[str] = mapped_column(String(71), nullable=False)
    scopes: Mapped[str] = mapped_column(String(100), nullable=False, default="read")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    use_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (UniqueConstraint("token_sha256", name="uq_api_tokens_token_sha256"),)


class Requirement(Base):
    """A compiled requirement (data model §2).

    Immutable after compilation: recompilation creates a new ``generation`` and the old rows are
    retained, because a project records which generation it is working against and a committed
    unit's coverage report must stay readable against the requirements it was actually judged on.
    """

    __tablename__ = "requirements"

    id: Mapped[str] = ulid_primary_key()
    project_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    requirement_key: Mapped[str] = mapped_column(String(20), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    blocking: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    checks_json: Mapped[list[Any]] = mapped_column(PortableJSON, nullable=False, default=list)
    source_document: Mapped[str] = mapped_column(String(300), nullable=False)
    source_quote: Mapped[str] = mapped_column(Text, nullable=False)
    source_anchor: Mapped[str | None] = mapped_column(String(200), nullable=True)
    compiled_by_prompt_id: Mapped[str] = mapped_column(String(120), nullable=False)
    compiled_by_prompt_version: Mapped[str] = mapped_column(String(20), nullable=False)
    compiled_by_prompt_sha256: Mapped[str | None] = mapped_column(String(71), nullable=True)
    compiled_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "project_id", "generation", "requirement_key", name="uq_requirements_project_gen_key"
        ),
        Index("ix_requirements_project_id_generation", "project_id", "generation"),
    )


class Unit(Base):
    """One unit of the plan, and its place in the state machine (data model §3)."""

    __tablename__ = "units"

    id: Mapped[str] = ulid_primary_key()
    project_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    unit_key: Mapped[str] = mapped_column(String(20), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    goal_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    requirement_keys_json: Mapped[list[Any]] = mapped_column(
        PortableJSON, nullable=False, default=list
    )
    target_words: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="planned")
    current_version_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    paused_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        UniqueConstraint("project_id", "unit_key", name="uq_units_project_id_unit_key"),
        # Data model §5: the unit list uses `(project_id, ordinal)`.
        Index("ix_units_project_id_ordinal", "project_id", "ordinal"),
    )


class StageRun(Base):
    """One execution of one stage over a project's units."""

    __tablename__ = "stage_runs"

    id: Mapped[str] = ulid_primary_key()
    project_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    stage: Mapped[str] = mapped_column(String(30), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    units_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    units_completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    units_paused: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    options_json: Mapped[dict[str, Any]] = mapped_column(PortableJSON, nullable=False, default=dict)
    backend: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    backend_mode: Mapped[str] = mapped_column(String(40), nullable=False, default="")

    __table_args__ = (Index("ix_stage_runs_project_id_started_at", "project_id", "started_at"),)


class Attempt(Base):
    """The unit of provenance: one bounded model task, or one deterministic stage step."""

    __tablename__ = "attempts"

    id: Mapped[str] = ulid_primary_key()
    stage_run_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("stage_runs.id", ondelete="CASCADE"), nullable=False
    )
    unit_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    stage: Mapped[str] = mapped_column(String(30), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    round: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    backend: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    backend_mode: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    model_provider_kind: Mapped[str | None] = mapped_column(String(40), nullable=True)
    model_provider_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    model_digest: Mapped[str | None] = mapped_column(String(71), nullable=True)
    model_canonical_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    prompt_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    prompt_sha256: Mapped[str | None] = mapped_column(String(71), nullable=True)
    rendered_prompt_sha256: Mapped[str | None] = mapped_column(String(71), nullable=True)
    prompt_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_hash: Mapped[str | None] = mapped_column(String(71), nullable=True)
    structured_output_json: Mapped[Any | None] = mapped_column(PortableJSON, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    thinking_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    overhead_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    ttft_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    outcome: Mapped[str] = mapped_column(String(30), nullable=False, default="completed")
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    routing_json: Mapped[Any | None] = mapped_column(PortableJSON, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(60), nullable=True)
    degradations_json: Mapped[list[Any]] = mapped_column(PortableJSON, nullable=False, default=list)
    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "stage_run_id",
            "unit_id",
            "stage",
            "attempt",
            "round",
            name="uq_attempts_run_unit_stage_attempt_round",
        ),
        # Data model §5: attempt lookup uses `(stage_run_id, unit_id, stage)`.
        Index("ix_attempts_stage_run_id_unit_id_stage", "stage_run_id", "unit_id", "stage"),
    )


class StageEvent(Base):
    """One persisted event of a stage run.

    ``sequence`` is dense and starts at 1 (data model §5, api.md §9): SSE replay from
    ``Last-Event-ID`` is only correct if the numbering has no gaps, so the repository assigns it
    inside the same transaction as the insert and a test asserts the whole series.
    """

    __tablename__ = "stage_events"

    id: Mapped[str] = ulid_primary_key()
    stage_run_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("stage_runs.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    unit_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    data_json: Mapped[dict[str, Any]] = mapped_column(PortableJSON, nullable=False, default=dict)

    __table_args__ = (
        UniqueConstraint("stage_run_id", "sequence", name="uq_stage_events_run_sequence"),
        # Data model §5: event replay uses `(stage_run_id, sequence)`.
        Index("ix_stage_events_stage_run_id_sequence", "stage_run_id", "sequence"),
    )
