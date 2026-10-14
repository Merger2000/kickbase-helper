"""Local roster roles, additive and independent from Kickbase data.

Revision ID: 0013
Revises: 0012
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "holding_roles",
        sa.Column("league_id", sa.String(16), primary_key=True),
        sa.Column("player_id", sa.String(16), primary_key=True),
        sa.Column("role", sa.String(5), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("role IN ('core', 'trade')", name=op.f("ck_holding_roles_valid_role")),
    )


def downgrade() -> None:
    op.drop_table("holding_roles")
