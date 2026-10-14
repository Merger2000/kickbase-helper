"""league transfers (activity feed) for bid recommendations

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "league_transfers",
        sa.Column("activity_id", sa.String(32), nullable=False),
        sa.Column("league_id", sa.String(16), nullable=False),
        sa.Column("at", TS, nullable=False),
        sa.Column("player_id", sa.String(16), nullable=False),
        sa.Column("player_name", sa.String(128), nullable=False),
        sa.Column("team_id", sa.String(16), nullable=True),
        sa.Column("kind", sa.SmallInteger(), nullable=False),
        sa.Column("price", sa.BigInteger(), nullable=False),
        sa.Column("by_me", sa.Boolean(), nullable=False),
        sa.Column("market_value", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("activity_id", name=op.f("pk_league_transfers")),
    )
    op.create_index(op.f("ix_league_transfers_league_id"), "league_transfers", ["league_id"])
    op.create_index(op.f("ix_league_transfers_at"), "league_transfers", ["at"])
    op.create_index(op.f("ix_league_transfers_player_id"), "league_transfers", ["player_id"])


def downgrade() -> None:
    op.drop_table("league_transfers")
