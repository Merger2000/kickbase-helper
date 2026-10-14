"""teams, players, market values, daily stats, matchday points, job runs

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-06
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "teams",
        sa.Column("id", sa.String(16), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("image", sa.String(255), nullable=True),
        sa.Column("updated_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_teams")),
    )
    op.create_table(
        "players",
        sa.Column("id", sa.String(16), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("team_id", sa.String(16), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("status", sa.Integer(), nullable=False),
        sa.Column("market_value", sa.BigInteger(), nullable=False),
        sa.Column("image", sa.String(255), nullable=True),
        sa.Column("updated_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_players")),
    )
    op.create_index(op.f("ix_players_team_id"), "players", ["team_id"])
    op.create_table(
        "market_values",
        sa.Column("player_id", sa.String(16), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("value", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("player_id", "day", name=op.f("pk_market_values")),
    )
    op.create_index("ix_market_values_day", "market_values", ["day"])
    op.create_table(
        "player_stats_daily",
        sa.Column("player_id", sa.String(16), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("status", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.String(16), nullable=False),
        sa.Column("market_value_change_7d", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("player_id", "day", name=op.f("pk_player_stats_daily")),
    )
    op.create_table(
        "matchday_points",
        sa.Column("season", sa.SmallInteger(), nullable=False),
        sa.Column("matchday", sa.SmallInteger(), nullable=False),
        sa.Column("player_id", sa.String(16), nullable=False),
        sa.Column("points", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.String(16), nullable=False),
        sa.Column("opponent_id", sa.String(16), nullable=True),
        sa.Column("is_home", sa.Boolean(), nullable=True),
        sa.Column("kickoff", TS, nullable=True),
        sa.PrimaryKeyConstraint(
            "season", "matchday", "player_id", name=op.f("pk_matchday_points")
        ),
    )
    op.create_index("ix_matchday_points_player", "matchday_points", ["player_id", "season"])
    op.create_table(
        "job_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job", sa.String(64), nullable=False),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("finished_at", TS, nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("requests", sa.Integer(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_runs")),
    )
    op.create_index(op.f("ix_job_runs_job"), "job_runs", ["job"])


def downgrade() -> None:
    op.drop_index(op.f("ix_job_runs_job"), table_name="job_runs")
    op.drop_table("job_runs")
    op.drop_index("ix_matchday_points_player", table_name="matchday_points")
    op.drop_table("matchday_points")
    op.drop_table("player_stats_daily")
    op.drop_index("ix_market_values_day", table_name="market_values")
    op.drop_table("market_values")
    op.drop_index(op.f("ix_players_team_id"), table_name="players")
    op.drop_table("players")
    op.drop_table("teams")
