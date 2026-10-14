"""matchday_results.missed_points (Block C, C3.5)

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-10

The Bot-Lagebild surfaces the previous matchday's verpasste Punkte; the worker computes the
number in `app.services.matchday_report` but the column was missing from C3.2's schema.
Nullable so older rows stay NULL – the Lagebild just skips the field for them.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "matchday_results",
        sa.Column("missed_points", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("matchday_results", "missed_points")