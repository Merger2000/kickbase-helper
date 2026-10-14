"""injuries and suspensions (Phase 7: LigaInsider)

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "injuries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("team_id", sa.String(16), nullable=False),
        sa.Column("player_id", sa.String(16), nullable=True),
        sa.Column("player_name", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("since", sa.String(64), nullable=True),
        sa.Column("fetched_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_injuries")),
    )
    op.create_index("ix_injuries_player", "injuries", ["player_id"])


def downgrade() -> None:
    op.drop_table("injuries")
