"""a discarded model call is an attempt row of its own

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-11 00:00:00.000000

Row WPF7. `InferenceGateway` retries a generation that came back empty after exhausting its output
budget, and until now only the answer it kept was ever recorded: the discarded call left no attempt
row, no tokens and no budget debit, and a stage that ran out of budget twice recorded **nothing at
all** (WP6 finding 8 — a `project_review` with `stage.started`, `stage.failed` and zero attempts).

`transport_call` is which physical call within one attempt a row reports: `0` for the call whose
answer the attempt kept, `1` and up for the calls the gateway discarded. It joins the uniqueness
key, because a discarded call shares its attempt's number — the transport retry is deliberately not
one of the stage's `max_attempts_per_stage`, which exist for defects in content that exists.

**Existing rows are `0`, and that is the true statement.** Every attempt recorded before this
migration is a kept answer: the builds that wrote them never recorded a discarded call.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_CONSTRAINT = "uq_attempts_run_unit_stage_attempt_round"
_COLUMNS = ("stage_run_id", "unit_id", "stage", "attempt", "round")


def upgrade() -> None:
    with op.batch_alter_table("attempts", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("transport_call", sa.Integer(), nullable=False, server_default=sa.text("0"))
        )
    with op.batch_alter_table("attempts", schema=None) as batch_op:
        batch_op.drop_constraint(_CONSTRAINT, type_="unique")
        batch_op.create_unique_constraint(_CONSTRAINT, [*_COLUMNS, "transport_call"])


def downgrade() -> None:
    with op.batch_alter_table("attempts", schema=None) as batch_op:
        batch_op.drop_constraint(_CONSTRAINT, type_="unique")
        batch_op.create_unique_constraint(_CONSTRAINT, list(_COLUMNS))
    with op.batch_alter_table("attempts", schema=None) as batch_op:
        batch_op.drop_column("transport_call")
