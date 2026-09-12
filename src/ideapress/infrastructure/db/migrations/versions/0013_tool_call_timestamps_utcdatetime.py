"""tool_call_records timestamps match the model's UtcDateTime on PostgreSQL

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-12 00:00:00.000000

`0010` created `started_at` and `created_at` as `sa.DateTime(timezone=True)` while
`ToolCallRecordRow` maps both with `weightsdb.UtcDateTime`, whose `impl` is a plain `DateTime`.
On SQLite the two are the same column — both land on `DATETIME` affinity and the type decorator
stores naive UTC either way — which is why the divergence survived the SQLite leg of the gate. On
PostgreSQL they are not: the table got `TIMESTAMP WITH TIME ZONE` and the model expects
`TIMESTAMP WITHOUT TIME ZONE`, so `test_models_and_migration_agree_on_postgresql` reported a
`modify_type` on both columns, and every value written through the decorator was handed to a
tz-aware column as a naive instant for the session's `TimeZone` to interpret.

`0010` itself now creates the column the model's way, so a database created after this revision
needs nothing here. This migration is for the databases that already ran the old `0010` —
1.3.0 through 1.4.0 — and it only touches PostgreSQL: `AT TIME ZONE 'UTC'` reads the stored
instant back as the naive UTC value the decorator has been writing all along, which is what the
rows hold on any server whose session `TimeZone` was UTC and what they should have held on any
other.
"""

from __future__ import annotations

import sqlalchemy as sa
import weightsdb
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_COLUMNS = ("started_at", "created_at")


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for column in _COLUMNS:
        op.alter_column(
            "tool_call_records",
            column,
            existing_type=sa.DateTime(timezone=True),
            type_=weightsdb.UtcDateTime(),
            existing_nullable=False,
            postgresql_using=f"{column} AT TIME ZONE 'UTC'",
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for column in _COLUMNS:
        op.alter_column(
            "tool_call_records",
            column,
            existing_type=weightsdb.UtcDateTime(),
            type_=sa.DateTime(timezone=True),
            existing_nullable=False,
            postgresql_using=f"{column} AT TIME ZONE 'UTC'",
        )
