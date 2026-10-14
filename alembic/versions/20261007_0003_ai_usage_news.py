"""ai usage, news items and news reports

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)
JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "ai_usage",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("at", TS, nullable=False),
        sa.Column("task", sa.String(32), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_usage")),
    )
    op.create_index(op.f("ix_ai_usage_at"), "ai_usage", ["at"])
    op.create_table(
        "news_items",
        sa.Column("guid", sa.String(512), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("teaser", sa.Text(), nullable=False),
        sa.Column("link", sa.String(1024), nullable=False),
        sa.Column("categories", JSON, nullable=False),
        sa.Column("published_at", TS, nullable=False),
        sa.Column("fetched_at", TS, nullable=False),
        sa.Column("processed_at", TS, nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.PrimaryKeyConstraint("guid", name=op.f("pk_news_items")),
    )
    op.create_index(op.f("ix_news_items_published_at"), "news_items", ["published_at"])
    op.create_table(
        "news_reports",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("news_guid", sa.String(512), nullable=False),
        sa.Column("published_at", TS, nullable=False),
        sa.Column("player_name", sa.String(128), nullable=False),
        sa.Column("team_id", sa.String(16), nullable=True),
        sa.Column("player_id", sa.String(16), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("absence", sa.Text(), nullable=True),
        sa.Column("expected_return", sa.Text(), nullable=True),
        sa.Column("notified_at", TS, nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_news_reports")),
    )
    op.create_index(op.f("ix_news_reports_news_guid"), "news_reports", ["news_guid"])
    op.create_index(op.f("ix_news_reports_published_at"), "news_reports", ["published_at"])
    op.create_index(op.f("ix_news_reports_player_id"), "news_reports", ["player_id"])
    op.create_index(op.f("ix_news_reports_notified_at"), "news_reports", ["notified_at"])


def downgrade() -> None:
    op.drop_table("news_reports")
    op.drop_table("news_items")
    op.drop_table("ai_usage")
