"""an attempt names the adapter subject that answered it

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-05 00:00:00.000000

The adapter axis on `attempts`, beside the four model columns migration 0002 created
(ADR-0058, ADR-0080). Three columns: the adapter's manifest name, its artifact digest — which is
the adapter's identity — and the canonical subject string, which is written and never parsed
(ADR-0024 §4).

**Existing rows are base subjects, and are left alone.** Every attempt written before IdeaPress 1.1
was served by bare weights: there was no way to ask for an adapter and no LoadCoach that could
serve one. All three columns are therefore `NULL` on those rows, which is the true statement.
`NULL` rather than `''` deliberately: "no adapter answered" and "an adapter whose name we do not
know" are different facts, and a back-fill that made them one would destroy the distinction the
columns exist to record.

`subject_canonical_id` is left `NULL` rather than copied from `model_canonical_id`. With no adapter
the two are byte-for-byte equal (ADR-0058 §3), so copying would lose nothing — but it would also
assert that LoadCoach reported a subject for a row where it reported none, and a provenance record
that states something nobody observed is exactly what this column exists to prevent.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    with op.batch_alter_table("attempts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("adapter_name", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("adapter_digest", sa.String(length=71), nullable=True))
        batch_op.add_column(sa.Column("subject_canonical_id", sa.String(length=400), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("attempts", schema=None) as batch_op:
        batch_op.drop_column("subject_canonical_id")
        batch_op.drop_column("adapter_digest")
        batch_op.drop_column("adapter_name")
