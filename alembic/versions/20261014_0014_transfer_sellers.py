"""Own seller identity, unknown for historical transfers until verified from feed/cache.

Revision ID: 0014
Revises: 0013
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("league_transfers", sa.Column("sold_by_me", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("league_transfers", "sold_by_me")
