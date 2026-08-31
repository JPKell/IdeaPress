"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-08-31 00:00:00.000000

Phase 1's four tables from data model §2: `projects`, `sources`, `settings` and `api_tokens`.
Portable across SQLite and PostgreSQL — `weightsdb.PortableJSON` and `weightsdb.UtcDateTime` are
what make one migration correct on both, and the PostgreSQL job runs this same file against a real
server rather than trusting that it would.
"""

from __future__ import annotations

import sqlalchemy as sa
import weightsdb
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("slug", sa.String(length=200), nullable=False),
        sa.Column("content_type", sa.String(length=50), nullable=False),
        sa.Column("content_type_version", sa.String(length=20), nullable=False),
        sa.Column("workflow_id", sa.String(length=50), nullable=False),
        sa.Column("workflow_version", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("brief_text", sa.Text(), nullable=False),
        sa.Column("author_material_json", weightsdb.PortableJSON(), nullable=False),
        sa.Column("config_json", weightsdb.PortableJSON(), nullable=False),
        sa.Column("created_at", weightsdb.UtcDateTime(), nullable=False),
        sa.Column("updated_at", weightsdb.UtcDateTime(), nullable=False),
        sa.Column("completed_at", weightsdb.UtcDateTime(), nullable=True),
        sa.Column("archived_at", weightsdb.UtcDateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_projects")),
        sa.UniqueConstraint("slug", name=op.f("uq_projects_slug")),
    )
    op.create_index("ix_projects_status_updated_at", "projects", ["status", "updated_at"])

    op.create_table(
        "sources",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("project_id", sa.String(length=26), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("path", sa.Text(), nullable=True),
        sa.Column("sha256", sa.String(length=71), nullable=False),
        sa.Column("content_text", sa.Text(), nullable=True),
        sa.Column("metadata_json", weightsdb.PortableJSON(), nullable=False),
        sa.Column("created_at", weightsdb.UtcDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_sources_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sources")),
    )
    op.create_index("ix_sources_project_id", "sources", ["project_id"])

    op.create_table(
        "settings",
        sa.Column("key", sa.String(length=120), nullable=False),
        sa.Column("value_json", weightsdb.PortableJSON(), nullable=False),
        sa.Column("updated_at", weightsdb.UtcDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_settings")),
    )

    op.create_table(
        "api_tokens",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("token_sha256", sa.String(length=71), nullable=False),
        sa.Column("scopes", sa.String(length=100), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", weightsdb.UtcDateTime(), nullable=False),
        sa.Column("last_used_at", weightsdb.UtcDateTime(), nullable=True),
        sa.Column("revoked_at", weightsdb.UtcDateTime(), nullable=True),
        sa.Column("use_count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_tokens")),
        sa.UniqueConstraint("token_sha256", name="uq_api_tokens_token_sha256"),
    )


def downgrade() -> None:
    op.drop_table("api_tokens")
    op.drop_table("settings")
    op.drop_index("ix_sources_project_id", table_name="sources")
    op.drop_table("sources")
    op.drop_index("ix_projects_status_updated_at", table_name="projects")
    op.drop_table("projects")
