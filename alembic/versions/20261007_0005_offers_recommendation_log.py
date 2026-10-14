"""own offers and recommendation log (track record)

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)
JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "my_offers",
        sa.Column("offer_id", sa.String(32), nullable=False),
        sa.Column("league_id", sa.String(16), nullable=False),
        sa.Column("player_id", sa.String(16), nullable=False),
        sa.Column("player_name", sa.String(128), nullable=False),
        sa.Column("first_seen", TS, nullable=False),
        sa.Column("last_seen", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=True),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("asking_price", sa.BigInteger(), nullable=False),
        sa.Column("market_value", sa.BigInteger(), nullable=False),
        sa.Column("outcome", sa.String(8), server_default="open", nullable=False),
        sa.Column("final_price", sa.BigInteger(), nullable=True),
        sa.Column("evaluated_at", TS, nullable=True),
        sa.PrimaryKeyConstraint("offer_id", name=op.f("pk_my_offers")),
    )
    op.create_index(op.f("ix_my_offers_league_id"), "my_offers", ["league_id"])
    op.create_index(op.f("ix_my_offers_first_seen"), "my_offers", ["first_seen"])
    op.create_table(
        "recommendation_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("league_id", sa.String(16), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("view", sa.String(16), nullable=False),
        sa.Column("player_id", sa.String(16), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("recommended", sa.Boolean(), nullable=False),
        sa.Column("price", sa.BigInteger(), nullable=True),
        sa.Column("market_value", sa.BigInteger(), nullable=False),
        sa.Column("components", JSON, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recommendation_log")),
        sa.UniqueConstraint(
            "league_id",
            "day",
            "view",
            "player_id",
            name=op.f("uq_recommendation_log_league_id"),
        ),
    )
    op.create_index("ix_recommendation_log_day", "recommendation_log", ["day"])


def downgrade() -> None:
    op.drop_table("recommendation_log")
    op.drop_table("my_offers")
