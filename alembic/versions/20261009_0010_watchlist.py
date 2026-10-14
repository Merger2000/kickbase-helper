"""watchlist: players to keep an eye on (Block C, C1)

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "watchlist",
        sa.Column("league_id", sa.String(16), nullable=False),
        sa.Column("player_id", sa.String(16), nullable=False),
        sa.Column("added_at", TS, nullable=False),
        sa.Column("price_below", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint(
            "league_id", "player_id", name=op.f("pk_watchlist")
        ),
    )


def downgrade() -> None:
    op.drop_table("watchlist")
