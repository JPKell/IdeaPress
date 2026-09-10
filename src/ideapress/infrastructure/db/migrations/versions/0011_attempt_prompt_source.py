"""an attempt records where its prompt came from

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-10 00:00:00.000000

Row W9 (prompt standards §6): IdeaPress now loads the operator's overrides from
`$XDG_CONFIG_HOME/ideapress/prompts/`, and an attempt that rendered one is not comparable with one
that rendered the shipped record. `prompt_source` says which it was: `pack` or `user_override`.

**Existing attempts with a prompt are back-filled `pack`, and that is the true statement.** Every
attempt recorded before this migration was rendered by a build that could not load an override at
all, so its prompt came from the installed pack — a fact about that build, not a default (contrast
migration 0009's `NULL`s, where nothing was known). An attempt with no prompt stays `NULL`.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    with op.batch_alter_table("attempts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("prompt_source", sa.String(length=20), nullable=True))
    op.execute("UPDATE attempts SET prompt_source = 'pack' WHERE prompt_id IS NOT NULL")


def downgrade() -> None:
    with op.batch_alter_table("attempts", schema=None) as batch_op:
        batch_op.drop_column("prompt_source")
