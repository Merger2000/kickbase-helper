"""lineup predictions (Phase 7: LigaInsider starting elevens)

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "lineup_predictions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("team_id", sa.String(16), nullable=False),
        sa.Column("player_id", sa.String(16), nullable=True),
        sa.Column("player_name", sa.String(128), nullable=False),
        sa.Column("fetched_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_lineup_predictions")),
    )
    op.create_index("ix_lineup_predictions_team", "lineup_predictions", ["team_id"])


def downgrade() -> None:
    op.drop_table("lineup_predictions")
