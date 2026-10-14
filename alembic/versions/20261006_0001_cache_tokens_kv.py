"""api_cache, auth_tokens and kv tables

Revision ID: 0001
Revises:
Create Date: 2026-10-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "api_cache",
        sa.Column("key", sa.String(512), nullable=False),
        sa.Column("payload", JSON, nullable=False),
        sa.Column("fetched_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_api_cache")),
    )
    op.create_index(op.f("ix_api_cache_expires_at"), "api_cache", ["expires_at"])
    op.create_table(
        "auth_tokens",
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("token", sa.Text(), nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("provider", name=op.f("pk_auth_tokens")),
    )
    op.create_table(
        "kv",
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("value", JSON, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_kv")),
    )


def downgrade() -> None:
    op.drop_table("kv")
    op.drop_table("auth_tokens")
    op.drop_index(op.f("ix_api_cache_expires_at"), table_name="api_cache")
    op.drop_table("api_cache")
