"""ideapress.infrastructure.db.models — the declarative base and the P1 tables.

IdeaPress owns this ``MetaData``/``DeclarativeBase`` exclusively (database standards §1): WeightsDB
provides plumbing only and defines no application table, so each application keeps its own base
with no cross-application meaning. **No application ever reads another's database.**

The naming convention is not cosmetic. Alembic's autogenerate diff and SQLite's batch-mode ALTER
both need every constraint and index to have a stable, predictable name; without one, a constraint
recreated by batch mode gets an auto-generated name that differs from the one the model produces,
and the parity check (database standards §5.2) fails forever on a schema that is actually correct.

``projects``, ``sources``, ``settings`` and ``api_tokens`` come from Phase 1's migration ``0001``.
SQLAlchemy models never leave the repository layer: a service returns a frozen domain value object,
never one of these.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
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

__all__ = ["ApiToken", "Base", "Project", "Setting", "Source", "utcnow"]

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
