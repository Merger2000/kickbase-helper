"""matchday_results: per-matchday summary of the own team (Block C, C3.2)

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "matchday_results",
        sa.Column("league_id", sa.String(16), nullable=False),
        sa.Column("season", sa.SmallInteger(), nullable=False),
        sa.Column("matchday", sa.SmallInteger(), nullable=False),
        sa.Column("points", sa.Integer(), nullable=False),
        sa.Column("placement", sa.Integer(), nullable=False),
        sa.Column("matchday_placement", sa.Integer(), nullable=True),
        sa.Column("team_value", sa.BigInteger(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.PrimaryKeyConstraint(
            "league_id", "season", "matchday", name=op.f("pk_matchday_results")
        ),
    )


def downgrade() -> None:
    op.drop_table("matchday_results")
