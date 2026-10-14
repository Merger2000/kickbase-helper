import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

REPO = Path(__file__).resolve().parents[1]


def alembic(url: str, *args: str) -> subprocess.CompletedProcess:
    # Subprocess on purpose: alembic's fileConfig would disable the app's loggers in-process.
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO,
        env=os.environ | {"DATABASE_URL": url},
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture(params=["sqlite", "postgres"])
def database_url(request, tmp_path):
    if request.param == "sqlite":
        return f"sqlite+pysqlite:///{tmp_path / 'migrate.db'}"
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TEST_POSTGRES_URL not set")
    return url


def test_migrations_upgrade_check_downgrade(database_url):
    for args in (("upgrade", "head"), ("check",), ("downgrade", "base")):
        result = alembic(database_url, *args)
        assert result.returncode == 0, f"alembic {' '.join(args)}: {result.stderr}"


def test_transfer_seller_upgrade_preserves_legacy_unknown(database_url):
    assert alembic(database_url, "upgrade", "0013").returncode == 0
    engine = create_engine(database_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO league_transfers "
                    "(activity_id,league_id,at,player_id,player_name,kind,price,by_me) "
                    "VALUES ('legacy','fixture',CURRENT_TIMESTAMP,'1',"
                    "'Fixture Spieler',2,123,False)"
                )
            )
        assert alembic(database_url, "upgrade", "head").returncode == 0
        with engine.connect() as conn:
            assert tuple(
                conn.execute(
                    text("SELECT price,sold_by_me FROM league_transfers WHERE activity_id='legacy'")
                ).one()
            ) == (123, None)
    finally:
        engine.dispose()
        assert alembic(database_url, "downgrade", "base").returncode == 0


def test_news_revision_upgrade_retains_old_notification_state(database_url):
    assert alembic(database_url, "upgrade", "0014").returncode == 0
    engine = create_engine(database_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO news_items "
                    "(guid,source,title,teaser,link,categories,published_at,fetched_at) "
                    "VALUES ('legacy','kicker','Fixture','','https://kicker.de/fixture','[]',"
                    "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO news_reports "
                    "(news_guid,published_at,player_name,kind,status,notified_at) "
                    "VALUES ('legacy',CURRENT_TIMESTAMP,'Fixture','verletzung','faellt_aus',"
                    "CURRENT_TIMESTAMP)"
                )
            )
        assert alembic(database_url, "upgrade", "head").returncode == 0
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT revision,reprocess_count,notification_handled_at FROM news_items "
                    "WHERE guid='legacy'"
                )
            ).one()
            assert row[0:2] == (1, 0) and row[2] is not None
            assert (
                conn.scalar(text("SELECT revision FROM news_reports WHERE news_guid='legacy'")) == 1
            )
    finally:
        engine.dispose()
        assert alembic(database_url, "downgrade", "base").returncode == 0


def test_matchday_results_table_exists_with_expected_columns(database_url, tmp_path):
    # Block C, C3.2: own per-matchday summary. The composite PK is
    # (league_id, season, matchday). C3.5 added `missed_points`.
    assert alembic(database_url, "upgrade", "head").returncode == 0
    engine = create_engine(database_url)
    try:
        columns = {c["name"] for c in inspect(engine).get_columns("matchday_results")}
        pk = inspect(engine).get_pk_constraint("matchday_results")
        assert pk["constrained_columns"] == ["league_id", "season", "matchday"]
        assert {
            "league_id",
            "season",
            "matchday",
            "points",
            "placement",
            "matchday_placement",
            "team_value",
            "missed_points",
            "created_at",
        } <= columns
    finally:
        engine.dispose()
        if database_url.startswith("sqlite+pysqlite:///"):
            (tmp_path / "migrate.db").unlink(missing_ok=True)
        else:
            alembic(database_url, "downgrade", "base")
