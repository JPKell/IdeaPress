"""every research tool call, whatever its outcome

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-08 00:00:00.000000

Row M1 (ADR-0116). ToolYard is the one harness-arc package that ships **no** table at all — it
defines `toolyard.ToolCallRecord` and one `append` method and owns no data (its spec §10) — so
unlike `0007` and `0008` there is nothing here to mount. The columns are IdeaPress's, chosen to
carry every field the package's record produces without reshaping one, and the two extras
(`project_id`, `attempt_id`) are the facts a record cannot know: which project asked, and which
attempt the call belongs to.

**Refused and failed calls get rows.** A host outside the allowlist, a denied egress verdict, a
path escaping the project's `sources/` directory — each is a structured result rather than an
exception (ADR-0053), and each is written here. A table that kept only successes would say nothing
about the fetch an operator is trying to work out why they never got.

`invocation_id` is not decoration. The egress decision for a fetch is rendered and recorded
*before* the executor is entered (ADR-0073), which is before this row's attempt exists, so
`egress_decisions.source_ref` carries the invocation id and this column is the other half of that
join.
"""

from __future__ import annotations

import sqlalchemy as sa
import weightsdb
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_call_records",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("project_id", sa.String(length=26), nullable=False),
        sa.Column("attempt_id", sa.String(length=26), nullable=False),
        sa.Column("invocation_id", sa.String(length=64), nullable=False),
        sa.Column("tool_name", sa.String(length=120), nullable=False),
        sa.Column("args_json", sa.Text(), nullable=True),
        sa.Column("args_sha256", sa.String(length=71), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.String(length=60), nullable=True),
        sa.Column("reason_detail", sa.Text(), nullable=True),
        sa.Column("result_summary", sa.Text(), nullable=False),
        sa.Column("result_sha256", sa.String(length=71), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("risk_class", sa.String(length=20), nullable=False),
        sa.Column("egress", sa.String(length=20), nullable=False),
        sa.Column("started_at", weightsdb.UtcDateTime(), nullable=False),
        sa.Column("created_at", weightsdb.UtcDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["attempts.id"],
            name="fk_tool_call_records_attempt_id_attempts",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_tool_call_records_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tool_call_records"),
    )
    op.create_index(
        "ix_tool_call_records_attempt_id", "tool_call_records", ["attempt_id"], unique=False
    )
    op.create_index(
        "ix_tool_call_records_project_id_started_at",
        "tool_call_records",
        ["project_id", "started_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_tool_call_records_project_id_started_at", table_name="tool_call_records")
    op.drop_index("ix_tool_call_records_attempt_id", table_name="tool_call_records")
    op.drop_table("tool_call_records")
