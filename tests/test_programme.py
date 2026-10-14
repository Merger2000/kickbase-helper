import logging
from datetime import UTC, datetime

import pytest

from app.openligadb.client import MATCHES, TABLE, OpenLigaDbError
from app.openligadb.models import OldbMatch
from app.services.programme import (
    load_programmes,
    load_team_mapping,
    map_teams,
    programme_for,
    team_strengths,
)
from app.services.scoring import load_scoring_config
from tests.fakes import FakeOpenLigaDb, fixture, make_oldb_client, oldb_fixture

CONFIG = load_scoring_config().programme
TABLE_2026 = TABLE.validate_python(oldb_fixture("table2026"))
TABLE_2025 = TABLE.validate_python(oldb_fixture("table2025"))
KICKBASE_TEAMS = {t["tid"]: t["tn"] for t in fixture("competition_table")["it"]}


def test_configured_mapping_covers_all_clubs_and_matches_names():
    configured = load_team_mapping()
    assert set(configured) == set(KICKBASE_TEAMS)
    assert map_teams(KICKBASE_TEAMS, TABLE_2026, {}) == (configured, {})


def test_unknown_club_is_reported_not_guessed(caplog):
    with caplog.at_level(logging.WARNING):
        mapping, missing = map_teams({"99": "Atlantis"}, TABLE_2026, {})
    assert mapping == {} and missing == {"99": "Zuordnung nicht gefunden"}
    assert "nicht gefunden" in caplog.text


def test_outdated_configured_id_falls_back_to_name(caplog):
    with caplog.at_level(logging.WARNING):
        mapping, missing = map_teams({"2": "Bayern"}, TABLE_2026, {"2": 999999})
    assert mapping == {"2": 40} and not missing
    assert "ist nicht in der Tabelle" in caplog.text


def test_strengths_blend_previous_season_early():
    early = [row.model_copy(update={"matches": 1, "points": 3}) for row in TABLE_2026]
    blended = team_strengths(early, TABLE_2025, CONFIG)
    pure = team_strengths(TABLE_2026, TABLE_2025, CONFIG)
    bayern = 40
    # one match played: a quarter current form, three quarters last season
    last = next(r for r in TABLE_2025 if r.team_id == bayern).points_per_game
    assert blended[bayern] == pytest.approx((0.25 * 3 + 0.75 * last) / 3)
    assert all(0 <= v <= 1 for v in pure.values())


def test_promoted_clubs_get_the_configured_strength():
    elversberg = 198
    early = [
        row.model_copy(update={"matches": 0, "points": 0})
        for row in TABLE_2026
        if row.team_id == elversberg
    ]
    assert team_strengths(early, TABLE_2025, CONFIG)[elversberg] == pytest.approx(
        CONFIG.promoted_points_per_game / 3
    )


def match(team1: int, team2: int, day: int, finished=False) -> OldbMatch:
    return OldbMatch.model_validate(
        {
            "matchID": day * 100 + team1,
            "matchDateTimeUTC": datetime(2026, 10, day, 13, 30, tzinfo=UTC).isoformat(),
            "group": {"groupOrderID": day},
            "team1": {"teamId": team1, "teamName": f"T{team1}"},
            "team2": {"teamId": team2, "teamName": f"T{team2}"},
            "matchIsFinished": finished,
        }
    )


def test_programme_uses_next_unfinished_matches_with_home_away_factor():
    strengths = {1: 0.5, 2: 1.0, 3: 0.2, 4: 0.9}
    matches = [
        match(1, 2, 3, finished=True),  # already played
        match(1, 3, 4),  # home vs weak
        match(2, 1, 5),  # away vs strong
        match(1, 4, 6),
        match(4, 1, 7),  # beyond the configured three
    ]
    programme = programme_for(1, matches, strengths, {3: "Schwach", 2: "Stark"}, CONFIG)
    assert programme.fixtures == ("Schwach (H)", "Stark (A)", "T4 (H)")
    expected = 1 - (0.2 * 0.9 + min(1.0 * 1.1, 1.0) + 0.9 * 0.9) / 3
    assert programme.value == pytest.approx(expected)


def test_no_upcoming_matches_means_no_programme():
    assert programme_for(1, [match(1, 2, 3, finished=True)], {}, {}, CONFIG) is None


NOW = datetime(2026, 10, 7, 6, 0, tzinfo=UTC)


async def test_load_programmes_from_openligadb():
    fake = FakeOpenLigaDb()
    async with make_oldb_client(fake) as client:
        result = await load_programmes(client, 2026, 5, KICKBASE_TEAMS, CONFIG, NOW)
    matches = MATCHES.validate_python(oldb_fixture("matchday5"))
    assert len(result.by_team) == 2 * len(matches) == 18
    assert result.missing == {}
    paderborn = result.by_team["29"]
    assert paderborn.fixtures == ("Stuttgart (H)",)  # Kickbase names in the labels
    assert 0 <= paderborn.value <= 1
    assert len(fake.requests) == 2 + 4  # two tables, matchdays 5-8 (no "current group")


async def test_matches_far_in_the_future_are_ignored():
    fake = FakeOpenLigaDb()
    async with make_oldb_client(fake) as client:
        later = NOW.replace(year=2026, month=7)  # summer: matchday 5 is months away
        result = await load_programmes(client, 2026, 5, KICKBASE_TEAMS, CONFIG, later)
    assert result.by_team == {}
    assert set(result.missing.values()) == {"keine Spiele in den nächsten Wochen"}


@pytest.mark.parametrize("route", ["/getbltable/bl1/2026", "/getmatchdata/bl1/2026/5"])
async def test_empty_answers_are_errors_not_neutral_data(route):
    fake = FakeOpenLigaDb()
    fake.routes[route] = []
    async with make_oldb_client(fake) as client:
        with pytest.raises(OpenLigaDbError):
            await load_programmes(client, 2026, 5, KICKBASE_TEAMS, CONFIG, NOW)


async def test_changed_format_is_a_schema_error():
    from app.openligadb.client import OpenLigaDbSchemaError

    fake = FakeOpenLigaDb()
    fake.routes["/getbltable/bl1/2026"] = [{"unexpected": True}]
    async with make_oldb_client(fake) as client:
        with pytest.raises(OpenLigaDbSchemaError):
            await client.table(2026)


async def test_openligadb_outage_without_cache_raises():
    fake = FakeOpenLigaDb()
    fake.down = True
    async with make_oldb_client(fake) as client:
        with pytest.raises(OpenLigaDbError):
            await load_programmes(client, 2026, 5, KICKBASE_TEAMS, CONFIG, NOW)


async def test_openligadb_cache_and_stale_fallback():
    from tests.fakes import FakeClock

    fake = FakeOpenLigaDb()
    clock = FakeClock()
    async with make_oldb_client(fake, clock=clock) as client:
        first = await client.table(2026)
        await client.table(2026)
        assert len(fake.requests) == 1  # cached
        clock.advance(hours=2)
        fake.down = True
        stale = await client.table(2026)
    assert stale.stale and stale.data == first.data
