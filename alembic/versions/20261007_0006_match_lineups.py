"""match lineups (Phase 7: start quote)

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "match_lineups",
        sa.Column("match_id", sa.String(16), nullable=False),
        sa.Column("player_id", sa.String(16), nullable=False),
        sa.Column("season", sa.SmallInteger(), nullable=False),
        sa.Column("matchday", sa.SmallInteger(), nullable=False),
        sa.Column("team_id", sa.String(16), nullable=False),
        sa.Column("started", sa.Boolean(), nullable=False),
        sa.Column("kickoff", TS, nullable=True),
        sa.PrimaryKeyConstraint("match_id", "player_id", name=op.f("pk_match_lineups")),
    )
    op.create_index("ix_match_lineups_player", "match_lineups", ["player_id", "season"])


def downgrade() -> None:
    op.drop_table("match_lineups")
