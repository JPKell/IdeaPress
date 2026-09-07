"""an attempt records the two cache token classes

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-07 00:00:00.000000

The two columns row J1's handoff §8 said were missing. `baseaicore.TokenUsage` has four disjoint
billable classes; IdeaPress's own domain type carried two, so every conversion left cache write and
cache read `UNSUPPORTED`, LoadLedger counted every debit as unmetered, and every rendered figure
said "at least" forever — priced or not (ADR-0069's floor, arrived at for the wrong reason). These
are the spelling ADR-0070 rule 4 fixed and the one LoadCoach already puts on its wire (rule 7).

**Existing rows stay `NULL`, and that is the true statement.** An attempt recorded before this
migration was recorded by a build that never asked its backend for a cache figure, so nothing is
known about what those calls were billed. `NULL` means exactly that. A back-fill of `0` would claim
those calls read nothing from cache — the fabricated zero ADR-0016 forbids — and it would silently
turn old floors into totals that nobody measured. A person reading a project's cost sees "at least"
on its old attempts and a bare total on its new ones, which is the honest pair.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    with op.batch_alter_table("attempts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("cache_write_tokens", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("cache_read_tokens", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("attempts", schema=None) as batch_op:
        batch_op.drop_column("cache_read_tokens")
        batch_op.drop_column("cache_write_tokens")
