"""Bounded article revisions and durable per-article notification history.

Revision ID: 0015
Revises: 0014
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "news_items", sa.Column("revision", sa.Integer(), nullable=False, server_default="1")
    )
    op.add_column(
        "news_items", sa.Column("reprocess_count", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("news_items", sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "news_items",
        sa.Column("notification_handled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "news_reports", sa.Column("revision", sa.Integer(), nullable=False, server_default="1")
    )
    op.add_column("news_reports", sa.Column("team_name", sa.String(128), nullable=True))
    op.add_column("news_reports", sa.Column("source_excerpt", sa.String(280), nullable=True))
    op.execute(
        sa.text(
            "UPDATE news_items SET notification_handled_at = "
            "(SELECT MAX(notified_at) FROM news_reports "
            "WHERE news_reports.news_guid = news_items.guid)"
        )
    )


def downgrade() -> None:
    for name in ["source_excerpt", "team_name", "revision"]:
        op.drop_column("news_reports", name)
    for name in ["notification_handled_at", "updated_at", "reprocess_count", "revision"]:
        op.drop_column("news_items", name)
