import copy
import os
from datetime import timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import func, select

from app.db.models import (
    Base,
    JobRun,
    MarketValue,
    MatchdayPoints,
    MatchdayResult,
    MatchLineup,
    Player,
    PlayerStatsDaily,
    Team,
    Watchlist,
)
from app.db.session import make_engine, make_sessionmaker
from app.db.stores import KeyValueStore
from app.kickbase import models as m
from app.kickbase.errors import ReadOnlyViolation
from app.kickbase.league import LeagueContext
from app.lineups.service import TeamPrediction
from app.notify.events import MarketValueRule, NotificationConfig
from app.worker.jobs import (
    LINEUPS_STATE_KEY,
    POINTS_STATE_KEY,
    JobContext,
    backfill_market_values,
    collect_lineups,
    sync_players,
    sync_points,
)
from app.worker.runner import JobRunner
from tests.fakes import (
    NOW,
    TEAM_LEAGUE_ID,
    FakeClock,
    FakeKickbase,
    FakeOpenLigaDb,
    FakeTelegram,
    fixture,
    make_client,
    make_oldb_client,
    oldb_fixture,
)

TABLE = fixture("competition_table")
TEAM_IDS = [t["tid"] for t in TABLE["it"]]


def team_profile(team_id: str) -> dict:
    data = copy.deepcopy(fixture("competition_team_profile"))
    data["tid"] = team_id
    for player in data["it"]:
        player["i"] = f"{team_id}-{player['i']}"
        player["tid"] = team_id
    return data


def team_center(team_id: str) -> dict:
    data = copy.deepcopy(fixture("competition_team_center"))
    data["tid"] = team_id
    for player in data["it"]:
        player["i"] = f"{team_id}-{player['i']}"
    return data


def bundesliga_fake() -> FakeKickbase:
    fake = FakeKickbase()
    fake.on("GET", "/v4/leagues/selection", fixture("leagues_selection"))
    fake.on("GET", "/v4/competitions/1/table", TABLE)
    fake.on("GET", "/v4/competitions/1/matchdays", fixture("competition_matchdays"))
    for team_id in TEAM_IDS:
        fake.on("GET", f"/v4/competitions/1/teams/{team_id}/teamprofile", team_profile(team_id))
        fake.on("GET", f"/v4/competitions/1/teams/{team_id}/teamcenter", team_center(team_id))
    return fake


@pytest.fixture(params=["sqlite", "postgres"])
async def sessions(request, tmp_path):
    if request.param == "sqlite":
        url = f"sqlite+pysqlite:///{tmp_path / 'jobs.db'}"
    else:
        url = os.environ.get("TEST_POSTGRES_URL")
        if not url:
            pytest.skip("TEST_POSTGRES_URL not set")
    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield make_sessionmaker(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def ctx(sessions):
    fake = bundesliga_fake()
    clock = FakeClock()
    client = make_client(fake, clock=clock, serve_stale=False)  # like the worker
    kv = KeyValueStore(sessions)
    context = JobContext(
        client=client,
        sessions=sessions,
        league=LeagueContext(client, kv, "Testliga"),
        kv=kv,
        tz=ZoneInfo("Europe/Berlin"),
        clock=clock,
    )
    context.fake = fake
    yield context
    await client.aclose()


async def count(sessions, model) -> int:
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(model))


@pytest.mark.parametrize("job", [sync_points, collect_lineups])
async def test_incomplete_match_is_retried_without_saving_half_a_match(ctx, job):
    schedule = copy.deepcopy(fixture("competition_matchdays"))
    day = next(md for md in schedule["it"] if any(match["st"] == 2 for match in md["it"]))
    raw_match = next(match for match in day["it"] if match["st"] == 2)
    day["it"] = [raw_match]
    schedule["it"] = [day]
    ctx.fake.on("GET", "/v4/competitions/1/matchdays", schedule)
    match = m.Matchdays.model_validate(schedule).matchdays[0].matches[0]
    if job is sync_points:
        path = f"/v4/competitions/1/teams/{match.team2_id}/teamcenter"
        complete = team_center(match.team2_id)
        incomplete = copy.deepcopy(complete)
        incomplete["it"] = []
        model, key = MatchdayPoints, POINTS_STATE_KEY
        expected_rows = 2 * len(complete["it"])
    else:
        path = f"/v4/matches/{match.id}/details"
        complete = match_details_fixture(match.team1_id, match.team2_id)
        incomplete = copy.deepcopy(complete)
        incomplete["t2lp"] = []
        model, key = MatchLineup, LINEUPS_STATE_KEY
        expected_rows = 5
    ctx.fake.on("GET", path, incomplete, complete)
    await job(ctx)
    assert await count(ctx.sessions, model) == 0
    assert match.id not in (await ctx.kv.get(key) or {}).get("matches", [])
    ctx.clock.advance(minutes=1)
    await job(ctx)
    assert await count(ctx.sessions, model) == expected_rows
    assert match.id in (await ctx.kv.get(key))["matches"]


async def test_sync_players_stores_teams_players_and_daily_values(ctx):
    summary = await sync_players(ctx)
    players_per_team = len(fixture("competition_team_profile")["it"])
    assert summary.startswith(f"18 Vereine, {18 * players_per_team} Spieler")
    assert await count(ctx.sessions, Team) == 18
    assert await count(ctx.sessions, Player) == 18 * players_per_team
    assert await count(ctx.sessions, MarketValue) == 18 * players_per_team
    assert await count(ctx.sessions, PlayerStatsDaily) == 18 * players_per_team
    async with ctx.sessions() as session:
        days = set((await session.execute(select(MarketValue.day))).scalars())
    assert days == {ctx.market_day()}


async def test_sync_players_is_idempotent_per_day(ctx):
    await sync_players(ctx)
    await sync_players(ctx)
    assert await count(ctx.sessions, MarketValue) == await count(ctx.sessions, Player)
    ctx.clock.advance(days=1)
    await sync_players(ctx)
    assert await count(ctx.sessions, MarketValue) == 2 * await count(ctx.sessions, Player)


def match_details_fixture(t1: str, t2: str) -> dict:
    def player(pid: str, pos: int) -> dict:
        return {"i": pid, "n": pid, "pos": pos}

    return {
        "t1": t1,
        "t2": t2,
        "mst": 2,
        "md": "2026-08-22T13:30:00Z",
        "t1lp": [player(f"{t1}-s1", 2), player(f"{t1}-s2", 3)],
        "t1nlp": [player(f"{t1}-b1", 4)],
        "t2lp": [player(f"{t2}-s1", 2)],
        "t2nlp": [player(f"{t2}-b1", 4)],
    }


async def test_collect_lineups_stores_starting_and_bench(ctx):
    schedule = m.Matchdays.model_validate(fixture("competition_matchdays"))
    finished = [m for md in schedule.matchdays for m in md.matches if m.is_finished]
    for match in finished:
        ctx.fake.on(
            "GET",
            f"/v4/matches/{match.id}/details",
            match_details_fixture(match.team1_id, match.team2_id),
        )
    summary = await collect_lineups(ctx)
    assert summary.startswith(f"{len(finished)} Spiele")
    # 2 starting + 1 bench on team 1, 1 starting + 1 bench on team 2.
    assert await count(ctx.sessions, MatchLineup) == len(finished) * 5

    async with ctx.sessions() as session:
        rows = list((await session.execute(select(MatchLineup))).scalars())
    started = {r.player_id for r in rows if r.started}
    benched = {r.player_id for r in rows if not r.started}
    assert started and benched and started.isdisjoint(benched)

    # Idempotent: running again changes nothing – and fetches nothing, even for matches of the
    # last days (a finished match's lineup doesn't change).
    requests = len(ctx.fake.gets())
    # Just after the last match: the old 3-day recheck would have fetched it again.
    ctx.clock.now = max(match.kickoff for match in finished) + timedelta(hours=12)
    summary = await collect_lineups(ctx)
    assert summary.startswith("0 Spiele")
    assert await count(ctx.sessions, MatchLineup) == len(finished) * 5
    assert not [r for r in ctx.fake.gets()[requests:] if "/details" in r.url.path]


async def test_collect_lineups_resumes_after_failure(ctx):
    from app.kickbase.errors import KickbaseHTTPError

    schedule = m.Matchdays.model_validate(fixture("competition_matchdays"))
    finished = [m for md in schedule.matchdays for m in md.matches if m.is_finished]
    failing = finished[2].id
    for match in finished:
        if match.id == failing:
            ctx.fake.on("GET", f"/v4/matches/{match.id}/details", httpx.Response(404))
        else:
            ctx.fake.on(
                "GET",
                f"/v4/matches/{match.id}/details",
                match_details_fixture(match.team1_id, match.team2_id),
            )

    with pytest.raises(KickbaseHTTPError):
        await collect_lineups(ctx)
    partial = await count(ctx.sessions, MatchLineup)
    stored = set((await ctx.kv.get("worker.lineups"))["matches"])
    assert partial > 0 and stored

    for match in finished:
        ctx.fake.on(
            "GET",
            f"/v4/matches/{match.id}/details",
            match_details_fixture(match.team1_id, match.team2_id),
        )
    ctx.clock.advance(hours=1)
    summary = await collect_lineups(ctx)
    assert summary.startswith(f"{len(finished) - len(stored)} Spiele")


async def test_sync_points_stores_finished_matches_once(ctx):
    summary = await sync_points(ctx)
    schedule = m.Matchdays.model_validate(fixture("competition_matchdays"))
    finished = [x for md in schedule.matchdays for x in md.matches if x.is_finished]
    assert summary.startswith(f"{len(finished)} Spiele")
    per_team = len(fixture("competition_team_center")["it"])
    assert await count(ctx.sessions, MatchdayPoints) == len(finished) * 2 * per_team
    requests = len(ctx.fake.gets())

    assert (await sync_points(ctx)).startswith("0 Spiele")  # nothing new
    assert len(ctx.fake.gets()) <= requests + 1  # at most the schedule
    state = await ctx.kv.get(POINTS_STATE_KEY)
    assert state["season"] == 2026 and len(state["matches"]) == len(finished)


async def test_sync_points_rows_carry_opponent_and_home(ctx):
    await sync_points(ctx)
    async with ctx.sessions() as session:
        row = await session.scalar(select(MatchdayPoints).limit(1))
    assert row.opponent_id and row.opponent_id != row.team_id
    assert row.is_home in (True, False)
    assert row.kickoff.tzinfo is not None


async def test_recent_matches_are_fetched_again(ctx):
    schedule = copy.deepcopy(fixture("competition_matchdays"))
    latest = max(
        (md for md in schedule["it"] if all(x["st"] == 2 for x in md["it"])),
        key=lambda md: md["day"],
    )
    for match in latest["it"]:
        match["dt"] = (NOW - timedelta(days=1)).isoformat()
    ctx.fake.on("GET", "/v4/competitions/1/matchdays", schedule)
    await sync_points(ctx)
    ctx.clock.advance(hours=2)  # caches expired, still within the recheck period
    before = len(ctx.fake.gets("/v4/competitions/1/teams/2/teamcenter"))
    summary = await sync_points(ctx)
    assert summary.startswith(f"{len(latest['it'])} Spiele")
    assert len(ctx.fake.gets("/v4/competitions/1/teams/2/teamcenter")) <= before + 1


async def test_backfill_fills_history_and_skips_unknown_players(ctx):
    await sync_players(ctx)
    history = fixture("competition_player_market_value_92")
    async with ctx.sessions() as session:
        ids = list((await session.execute(select(Player.id))).scalars())
    for player_id in ids[1:]:
        ctx.fake.on("GET", f"/v4/competitions/1/players/{player_id}/marketValue/92", history)
    ctx.fake.on("GET", f"/v4/competitions/1/players/{ids[0]}/marketValue/92", httpx.Response(404))
    summary = await backfill_market_values(ctx)
    assert summary == f"{len(ids) - 1} Spieler ergänzt, 1 übersprungen"
    async with ctx.sessions() as session:
        days = await session.scalar(select(func.count()).where(MarketValue.player_id == ids[1]))
    assert days >= 92
    # second run: only the unknown player is left (no error for a single departed player)
    assert await backfill_market_values(ctx) == "0 Spieler ergänzt, 1 übersprungen"


def test_season_start_year():
    schedule = m.Matchdays.model_validate(fixture("competition_matchdays"))
    assert schedule.season_start_year == 2026


async def test_runner_records_success_failure_and_requests(ctx):
    runner = JobRunner(ctx)
    assert await runner.run("sync_players", sync_players)
    ctx.fake.on("GET", "/v4/competitions/1/table", httpx.Response(503))
    ctx.clock.advance(minutes=31)  # table cache expired
    assert not await runner.run("sync_players", sync_players)
    async with ctx.sessions() as session:
        runs = list((await session.execute(select(JobRun).order_by(JobRun.id))).scalars())
    assert [r.status for r in runs] == ["ok", "failed"]
    assert runs[0].requests == 1 + 1 + 1 + 18  # login, leagues, table, 18 team profiles
    assert "Vereine" in runs[0].detail
    assert "KickbaseUnavailable" in runs[1].detail
    assert await runner.succeeded_today("sync_players")


async def test_not_succeeded_today_after_midnight(ctx):
    runner = JobRunner(ctx)
    await runner.run("sync_players", sync_players)
    ctx.clock.now = NOW + timedelta(days=1)
    assert not await runner.succeeded_today("sync_players")


@pytest.mark.parametrize(
    ("utc_time", "expected"),
    [
        ("2026-10-07T05:00:00+00:00", "2026-10-06"),  # morning run: yesterday's value
        ("2026-10-06T19:00:00+00:00", "2026-10-05"),  # before the update
        ("2026-10-06T20:30:00+00:00", "2026-10-06"),  # after the update
        ("2026-12-15T21:30:00+00:00", "2026-12-15"),  # winter time: same UTC rule
    ],
)
def test_market_day(utc_time, expected):
    from datetime import datetime

    from app.kickbase.calendar import market_day

    assert market_day(datetime.fromisoformat(utc_time)).isoformat() == expected


@pytest.mark.parametrize(
    ("utc_time", "near"),
    [("19:49", False), ("19:50", True), ("20:15", True), ("20:30", True), ("20:31", False)],
)
def test_near_market_value_update(utc_time, near):
    from datetime import datetime

    from app.kickbase.calendar import near_market_value_update

    now = datetime.fromisoformat(f"2026-10-06T{utc_time}:00+00:00")
    assert near_market_value_update(now) is near


async def test_daily_rows_use_kickbase_day_not_local_date(ctx):
    from datetime import datetime

    ctx.clock.now = datetime.fromisoformat("2026-10-07T05:00:00+00:00")  # 07:00 in Berlin
    await sync_players(ctx)
    async with ctx.sessions() as session:
        days = set((await session.execute(select(PlayerStatsDaily.day))).scalars())
    assert [d.isoformat() for d in days] == ["2026-10-06"]


async def test_backfill_gives_up_when_endpoint_is_broken(ctx):
    await sync_players(ctx)  # history endpoint not mocked: every player 404s
    with pytest.raises(RuntimeError, match="Endpunkt"):
        await backfill_market_values(ctx)
    assert len(ctx.fake.gets()) < 40  # stopped early instead of trying ~460 players


async def test_backfill_reraises_non_404(ctx):
    from app.kickbase.errors import KickbaseHTTPError

    await sync_players(ctx)
    async with ctx.sessions() as session:
        ids = list((await session.execute(select(Player.id))).scalars())
    for player_id in ids:  # all of them: the order of equal market values is not fixed
        ctx.fake.on(
            "GET", f"/v4/competitions/1/players/{player_id}/marketValue/92", httpx.Response(403)
        )
    before = len(ctx.fake.gets())
    with pytest.raises(KickbaseHTTPError):
        await backfill_market_values(ctx)
    assert len(ctx.fake.gets()) == before + 1  # stopped at the first 403


async def test_sync_points_resumes_after_interruption(ctx):
    from app.kickbase.errors import KickbaseHTTPError

    def fail_on_matchday_3(request):
        if request.url.params["dayNumber"] == "3":
            return httpx.Response(404)
        return httpx.Response(200, json=team_center("2"))

    ctx.fake.on("GET", "/v4/competitions/1/teams/2/teamcenter", fail_on_matchday_3)
    with pytest.raises(KickbaseHTTPError):
        await sync_points(ctx)
    partial = await count(ctx.sessions, MatchdayPoints)
    stored = set((await ctx.kv.get(POINTS_STATE_KEY))["matches"])
    assert partial > 0 and stored
    ctx.fake.on("GET", "/v4/competitions/1/teams/2/teamcenter", team_center("2"))
    ctx.clock.advance(hours=1)
    summary = await sync_points(ctx)
    schedule = m.Matchdays.model_validate(fixture("competition_matchdays"))
    finished = [x for md in schedule.matchdays for x in md.matches if x.is_finished]
    assert summary.startswith(f"{len(finished) - len(stored)} Spiele")


async def test_corrected_points_are_saved_within_recheck_period(ctx):
    schedule = copy.deepcopy(fixture("competition_matchdays"))
    latest = max(
        (md for md in schedule["it"] if all(x["st"] == 2 for x in md["it"])),
        key=lambda md: md["day"],
    )
    for match in latest["it"]:
        match["dt"] = (NOW - timedelta(days=1)).isoformat()
    ctx.fake.on("GET", "/v4/competitions/1/matchdays", schedule)
    await sync_points(ctx)
    corrected = team_center("2")
    corrected["it"][0]["p"] = 999
    ctx.fake.on("GET", "/v4/competitions/1/teams/2/teamcenter", corrected)
    ctx.clock.advance(hours=2)
    await sync_points(ctx)
    async with ctx.sessions() as session:
        points = await session.scalar(
            select(MatchdayPoints.points).where(
                MatchdayPoints.player_id == corrected["it"][0]["i"],
                MatchdayPoints.matchday == latest["day"],
            )
        )
    assert points == 999
    ctx.clock.advance(days=4)  # recheck period over
    assert (await sync_points(ctx)).startswith("0 Spiele")


async def test_new_season_starts_with_fresh_state(ctx):
    await ctx.kv.set(POINTS_STATE_KEY, {"season": 2025, "matches": ["1", "2"]})
    await sync_points(ctx)
    assert (await ctx.kv.get(POINTS_STATE_KEY))["season"] == 2026


async def test_empty_schedule_is_not_an_error(ctx):
    ctx.fake.on("GET", "/v4/competitions/1/matchdays", {"it": [], "day": 1})
    assert await sync_points(ctx) == "Kein Spielplan veröffentlicht"


def test_season_boundaries():
    def schedule(kickoff):
        return m.Matchdays.model_validate(
            {"it": [{"day": 1, "it": [{"mi": "1", "day": 1, "dt": kickoff, "t1": "2", "t2": "3"}]}]}
        )

    assert schedule("2026-07-31T18:30:00Z").season_start_year == 2026
    assert schedule("2027-06-01T18:30:00Z").season_start_year == 2026
    assert m.Matchdays.model_validate({"it": []}).season_start_year is None


async def test_runner_records_unexpected_exceptions(ctx):
    async def broken(_ctx):
        raise ValueError("kaputt")

    assert not await JobRunner(ctx).run("broken", broken)
    async with ctx.sessions() as session:
        run = await session.scalar(select(JobRun).where(JobRun.job == "broken"))
    assert run.status == "failed" and run.finished_at is not None
    assert "ValueError: kaputt" in run.detail


async def test_runner_records_read_only_violation_as_failed(ctx):
    async def bad_job(_ctx):
        raise ReadOnlyViolation("POST", "/v4/leagues/1/market")

    with pytest.raises(ReadOnlyViolation):
        await JobRunner(ctx).run("bad", bad_job)
    async with ctx.sessions() as session:
        run = await session.scalar(select(JobRun).where(JobRun.job == "bad"))
    assert run.status == "failed"


async def test_cancelled_job_is_recorded_and_stale_runs_are_aborted(ctx):
    import asyncio

    started = asyncio.Event()

    async def slow(_ctx):
        started.set()
        await asyncio.sleep(10)
        return "nie"

    runner = JobRunner(ctx)
    task = asyncio.create_task(runner.run("slow", slow))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with ctx.sessions.begin() as session:
        session.add(JobRun(job="ghost", started_at=NOW, status="running", requests=0))
    assert await runner.mark_interrupted_runs() == 1
    async with ctx.sessions() as session:
        statuses = dict((await session.execute(select(JobRun.job, JobRun.status))).all())
    assert statuses == {"slow": "cancelled", "ghost": "aborted"}


async def test_succeeded_today_uses_berlin_date(ctx):
    from datetime import datetime

    runner = JobRunner(ctx)
    ctx.clock.now = datetime.fromisoformat("2026-10-06T21:30:00+00:00")  # 23:30 Berlin
    await runner.run("sync_players", sync_players)
    ctx.clock.now = datetime.fromisoformat("2026-10-06T22:30:00+00:00")  # 00:30 next day
    assert not await runner.succeeded_today("sync_players")


def test_deadline_risks_flags_low_start_probability():
    from app.lineups.service import InjuryNote
    from app.worker.jobs import _deadline_risks

    class P:
        def __init__(self, id_, name, team_id, status):
            self.id = id_
            self.name = name
            self.team_id = team_id
            self.status = status

    # injured player → risk at 0 %
    injuries = {"1": InjuryNote("verletzung", "Kreuzbandriss", None)}
    risks = _deadline_risks([P("1", "X", "2", 0)], {}, {}, injuries, {})
    assert risks and "X" in risks[0] and "0 %" in risks[0]

    # not in the predicted eleven → 15 % → risk
    risks = _deadline_risks(
        [P("2", "Y", "2", 0)], {}, {"2": TeamPrediction(frozenset({"other"}))}, {}, {}
    )
    assert risks and "Y" in risks[0] and "15 %" in risks[0]

    # in the predicted eleven → 85 % → no risk
    risks = _deadline_risks(
        [P("2", "Y", "2", 0)], {}, {"2": TeamPrediction(frozenset({"2"}))}, {}, {}
    )
    assert risks == []


async def test_deadline_warning_without_notifier(ctx):
    from app.worker.jobs import deadline_warning

    ctx.notifier = None
    assert await deadline_warning(ctx) == "Benachrichtigungen aus (Telegram nicht konfiguriert)"


async def test_deadline_warning_outside_window(ctx):
    from datetime import timedelta

    from app.worker.jobs import deadline_warning
    from tests.fakes import TEAM_LEAGUE_ID

    class FakeNotifier:
        async def send(self, text):
            pass

        async def aclose(self):
            pass

    ctx.notifier = FakeNotifier()
    market = copy.deepcopy(fixture("league_market"))
    market["dt"] = (ctx.clock() + timedelta(hours=30)).isoformat().replace("+00:00", "Z")
    ctx.fake.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/market", market)
    assert await deadline_warning(ctx) == "keine Deadline in den nächsten 2 Stunden"


async def test_deadline_warning_sends_the_data_hint_alone(ctx):
    """LigaInsider data outdated and nothing else to warn about: the warning still goes out,
    because the start chances were checked without their data (review finding A4)."""
    from datetime import timedelta

    from app.db.models import LineupPrediction
    from app.worker.jobs import deadline_warning
    from tests.fakes import TEAM_LEAGUE_ID

    sent = []

    class FakeNotifier:
        async def send(self, text):
            sent.append(text)

        async def aclose(self):
            pass

    ctx.notifier = FakeNotifier()
    market = copy.deepcopy(fixture("league_market"))
    market["dt"] = (ctx.clock() + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    for p in market["it"]:
        p.pop("uop", None)
    ctx.fake.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/market", market)
    squad = copy.deepcopy(fixture("league_squad"))
    for i in range(2):  # two forwards: no squad gaps
        squad["it"].append(dict(squad["it"][0], i=f"fw{i}", pos=4))
    ctx.fake.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/squad", squad)
    ctx.fake.on(
        "GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/teamcenter/myeleven", fixture("league_myeleven")
    )
    async with ctx.sessions.begin() as session:
        session.add(
            LineupPrediction(
                source="ligainsider",
                team_id="2",
                player_id=None,
                player_name="X",
                fetched_at=ctx.clock() - timedelta(days=3),
            )
        )
    assert await deadline_warning(ctx) == "gewarnt"
    [text] = sent
    assert "⚠️ Keine aktuelle Startelf-Prognose für" in text
    assert "Freie Plätze" not in text
    # The fixture lineup has only 9 players: the empty slots are named too (finding A5).
    assert "❗ Nur 9 von 11 Plätzen aufgestellt" in text


# ---------------------------------------------------------------- notify_watchlist (C1.4)


TOKEN = "123456:SECRET-token"

NOTIFY_CONFIG = NotificationConfig(
    market_value=MarketValueRule(min_percent=10, min_amount=1_500_000),
    status_changes=True,
)


async def _make_watch_ctx(ctx):
    from app.notify.telegram import TelegramNotifier

    telegram = FakeTelegram()
    ctx.notifier = TelegramNotifier(TOKEN, "42", transport=httpx.MockTransport(telegram.handler))
    ctx.notifications = NOTIFY_CONFIG
    ctx.public_url = "https://kickbase.example.com"
    ctx.telegram = telegram
    return ctx


async def test_notify_watchlist_without_telegram_returns_immediately(ctx):
    from app.worker.jobs import notify_watchlist

    requests_before = len(ctx.fake.gets())
    ctx.notifier = None
    ctx.notifications = NOTIFY_CONFIG
    assert await notify_watchlist(ctx) == "Benachrichtigungen aus (Telegram nicht konfiguriert)"
    assert len(ctx.fake.gets()) == requests_before  # no API call


async def test_notify_watchlist_empty_skips_api_call(ctx):
    from app.worker.jobs import notify_watchlist

    ctx = await _make_watch_ctx(ctx)
    requests_before = len(ctx.fake.gets())
    assert await notify_watchlist(ctx) == "nichts zu tun"
    assert len(ctx.fake.gets()) == requests_before


async def test_notify_watchlist_sends_listing_and_records_baseline(ctx):

    from app.worker.jobs import notify_watchlist

    ctx = await _make_watch_ctx(ctx)
    ctx.fake.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/market", fixture("league_market"))
    ctx.fake.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/squad", fixture("league_squad"))
    player_id = "577"
    async with ctx.sessions.begin() as session:
        session.add(
            Player(
                id=player_id,
                name="Batz",
                team_id="15",
                position=1,
                status=0,
                market_value=500_000,
                image=None,
                updated_at=NOW,
            )
        )
        session.add(
            Watchlist(
                league_id=TEAM_LEAGUE_ID,
                player_id=player_id,
                added_at=NOW,
                price_below=None,
            )
        )
    summary = await notify_watchlist(ctx)
    assert summary == "1 Merklisten-Ereignisse gemeldet"
    assert len(ctx.telegram.texts) == 1
    [text] = ctx.telegram.texts
    assert text.startswith("<b>Testliga</b>\n🛒")
    assert "Batz" in text
    # Baseline is recorded so a status alarm on the next run works
    status = await ctx.kv.get("notify.watchlist.status")
    assert status is not None and status.get(player_id) == 0
    # Second run: dedupe via SENT_KEY, no new message
    assert await notify_watchlist(ctx) == "Keine neuen Ereignisse"
    assert len(ctx.telegram.texts) == 1


async def test_notify_watchlist_silences_own_squad(ctx):

    from app.worker.jobs import notify_watchlist

    ctx = await _make_watch_ctx(ctx)
    ctx.fake.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/market", fixture("league_market"))
    ctx.fake.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/squad", fixture("league_squad"))
    own_id = fixture("league_squad")["it"][0]["i"]  # in the squad
    async with ctx.sessions.begin() as session:
        session.add(
            Player(
                id=own_id,
                name="Hansen",
                team_id="29",
                position=2,
                status=0,
                market_value=3_722_776,
                image=None,
                updated_at=NOW,
            )
        )
        session.add(
            Watchlist(
                league_id=TEAM_LEAGUE_ID,
                player_id=own_id,
                added_at=NOW,
                price_below=None,
            )
        )
    assert await notify_watchlist(ctx) == "Keine neuen Ereignisse"
    assert ctx.telegram.texts == []


async def test_notify_watchlist_silences_own_offer(ctx):

    from app.worker.jobs import notify_watchlist

    ctx = await _make_watch_ctx(ctx)
    market = copy.deepcopy(fixture("league_market"))
    market["it"][0]["uop"] = 700_000  # own bid on the first listing (577/Batz)
    ctx.fake.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/market", market)
    ctx.fake.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/squad", fixture("league_squad"))
    player_id = market["it"][0]["i"]
    async with ctx.sessions.begin() as session:
        session.add(
            Player(
                id=player_id,
                name="Batz",
                team_id="15",
                position=1,
                status=0,
                market_value=500_000,
                image=None,
                updated_at=NOW,
            )
        )
        session.add(
            Watchlist(
                league_id=TEAM_LEAGUE_ID,
                player_id=player_id,
                added_at=NOW,
                price_below=None,
            )
        )
    assert await notify_watchlist(ctx) == "Keine neuen Ereignisse"
    assert ctx.telegram.texts == []


async def test_notify_watchlist_failed_send_is_retried(ctx):

    from app.notify.telegram import TelegramError
    from app.worker.jobs import notify_watchlist

    ctx = await _make_watch_ctx(ctx)
    ctx.fake.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/market", fixture("league_market"))
    ctx.fake.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/squad", fixture("league_squad"))
    player_id = "577"
    async with ctx.sessions.begin() as session:
        session.add(
            Player(
                id=player_id,
                name="Batz",
                team_id="15",
                position=1,
                status=0,
                market_value=500_000,
                image=None,
                updated_at=NOW,
            )
        )
        session.add(
            Watchlist(
                league_id=TEAM_LEAGUE_ID,
                player_id=player_id,
                added_at=NOW,
                price_below=None,
            )
        )
    ctx.telegram.fail = True
    with pytest.raises(TelegramError):
        await notify_watchlist(ctx)
    # No baselines committed: the event is still pending
    assert await ctx.kv.get("notify.watchlist.status") is None
    assert await ctx.kv.get("notify.sent") in (None, {})
    # Retry succeeds: the message goes out and baselines get recorded
    ctx.telegram.fail = False
    assert await notify_watchlist(ctx) == "1 Merklisten-Ereignisse gemeldet"
    assert len(ctx.telegram.texts) == 1  # the failed attempt never reached Telegram


async def test_notify_watchlist_first_run_records_baseline_without_alarm(ctx):
    from app.db.models import Player, Watchlist
    from app.worker.jobs import notify_watchlist

    ctx = await _make_watch_ctx(ctx)
    ctx.fake.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/market", fixture("league_market"))
    squad = copy.deepcopy(fixture("league_squad"))
    squad["it"][0]["st"] = 1  # injured now (status change would fire on second run)
    ctx.fake.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/squad", squad)
    player_id = squad["it"][0]["i"]
    async with ctx.sessions.begin() as session:
        session.add(
            Player(
                id=player_id,
                name="Hansen",
                team_id="29",
                position=2,
                status=1,
                market_value=3_722_776,
                image=None,
                updated_at=NOW,
            )
        )
        session.add(
            Watchlist(
                league_id=TEAM_LEAGUE_ID,
                player_id=player_id,
                added_at=NOW,
                price_below=None,
            )
        )
    assert await notify_watchlist(ctx) == "Keine neuen Ereignisse"
    assert ctx.telegram.texts == []
    # Baseline written for the next run
    status = await ctx.kv.get("notify.watchlist.status")
    assert status is not None and status.get(player_id) == 1


# ---------------------------------------------------------------- notify_own_offers (C2.3)


async def _own_listing_market(mv: int = 500_000, bid: int = 600_000) -> dict:
    """A market with a single entry sold by `9000001` (the manager from `user_me.json`)
    carrying one offer above the market value. The Caci/Røssing-Lelesiit entries from the
    live fixture are Kickbase sales, so they are stripped here."""
    market = copy.deepcopy(fixture("league_market"))
    market["it"] = [
        {
            "i": "577",
            "fn": "Daniel",
            "n": "Batz",
            "tid": "15",
            "pos": 1,
            "st": 0,
            "mvt": 0,
            "mv": mv,
            "ofc": 1,
            "exs": 3600,
            "prc": mv,
            "isn": False,
            "iposl": False,
            "dt": "2026-10-09T12:00:00Z",
            "u": {"i": "9000001", "n": "Manager 1", "st": 0},
            "ofs": [{"uop": bid, "uoid": "9000002", "st": 0, "unm": "Manager Bieter"}],
        }
    ]
    return market


async def test_notify_own_offers_without_telegram_returns_immediately(ctx):
    from app.worker.jobs import notify_own_offers

    requests_before = len(ctx.fake.gets())
    ctx.notifier = None
    ctx.notifications = NOTIFY_CONFIG
    assert await notify_own_offers(ctx) == "Benachrichtigungen aus (Telegram nicht konfiguriert)"
    assert len(ctx.fake.gets()) == requests_before  # no API call


async def test_notify_own_offers_disabled_returns_immediately(ctx):
    from app.notify.events import OwnOffersRule
    from app.worker.jobs import notify_own_offers

    ctx = await _make_watch_ctx(ctx)
    ctx.notifications = NOTIFY_CONFIG.model_copy(
        update={"own_offers": OwnOffersRule(enabled=False)}
    )
    requests_before = len(ctx.fake.gets())
    assert await notify_own_offers(ctx) == "Benachrichtigungen aus (Eigene-Angebote-Alarm aus)"
    assert len(ctx.fake.gets()) == requests_before
    assert ctx.telegram.texts == []


async def test_notify_own_offers_sends_message_and_records_sent_key(ctx):
    from app.worker.jobs import notify_own_offers

    ctx = await _make_watch_ctx(ctx)
    ctx.fake.on("GET", "/v4/user/me", fixture("user_me"))
    ctx.fake.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/market", await _own_listing_market())
    assert await notify_own_offers(ctx) == "1 Gebot auf eigene Spieler gemeldet"
    assert len(ctx.telegram.texts) == 1
    text = ctx.telegram.texts[0]
    assert text.startswith("<b>Testliga</b>\n💰")
    assert "Batz" in text
    assert "+100" in text  # +100k premium over 500k MV
    assert "annehmen nur in der Kickbase-App" in text
    # Bidder name from `ofs` must never reach the Telegram text (other manager's private data).
    assert "Manager Bieter" not in text
    assert "9000002" not in text
    # SENT_KEY holds the key for the next run to filter
    sent = await ctx.kv.get("notify.sent")
    assert sent and any(k.startswith("own_offers:") for k in sent)
    # Second run: dedupe via SENT_KEY, no new message
    assert await notify_own_offers(ctx) == "Keine neuen Ereignisse"
    assert len(ctx.telegram.texts) == 1


async def test_notify_own_offers_silences_foreign_seller(ctx):
    from app.worker.jobs import notify_own_offers

    ctx = await _make_watch_ctx(ctx)
    market = await _own_listing_market()
    # Replace the manager seller with another manager
    market["it"][0]["u"] = {"i": "9000002", "n": "Manager 2", "st": 0}
    ctx.fake.on("GET", "/v4/user/me", fixture("user_me"))
    ctx.fake.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/market", market)
    assert await notify_own_offers(ctx) == "Keine neuen Ereignisse"
    assert ctx.telegram.texts == []
    # No sent entry recorded – a foreign seller is not interesting at all
    assert await ctx.kv.get("notify.sent") in (None, {})


async def test_notify_own_offers_silences_kickbase_seller(ctx):
    from app.worker.jobs import notify_own_offers

    ctx = await _make_watch_ctx(ctx)
    market = await _own_listing_market()
    # Kickbase's own sale: no `seller`, market value price
    market["it"][0].pop("u", None)
    ctx.fake.on("GET", "/v4/user/me", fixture("user_me"))
    ctx.fake.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/market", market)
    assert await notify_own_offers(ctx) == "Keine neuen Ereignisse"
    assert ctx.telegram.texts == []


async def test_notify_own_offers_failed_send_is_retried(ctx):
    from app.notify.telegram import TelegramError
    from app.worker.jobs import notify_own_offers

    ctx = await _make_watch_ctx(ctx)
    ctx.fake.on("GET", "/v4/user/me", fixture("user_me"))
    ctx.fake.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/market", await _own_listing_market())
    ctx.telegram.fail = True
    with pytest.raises(TelegramError):
        await notify_own_offers(ctx)
    # No SENT_KEY recorded: the event is still pending for the next run
    assert await ctx.kv.get("notify.sent") in (None, {})
    # Retry succeeds: the message goes out and the sent key gets recorded
    ctx.telegram.fail = False
    assert await notify_own_offers(ctx) == "1 Gebot auf eigene Spieler gemeldet"
    assert len(ctx.telegram.texts) == 1  # the failed attempt never reached Telegram


async def test_notify_own_offers_higher_follow_up_bid_sends_new_message(ctx):
    from app.worker.jobs import notify_own_offers

    ctx = await _make_watch_ctx(ctx)
    ctx.fake.on("GET", "/v4/user/me", fixture("user_me"))
    ctx.fake.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/market", await _own_listing_market())
    await notify_own_offers(ctx)
    # Same listing, higher bid – new dedupe key, new message. Advance the clock past the
    # client's refresh_min_age so the second market fetch actually re-reads the fixture.
    ctx.clock.advance(seconds=120)
    ctx.fake.on(
        "GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/market", await _own_listing_market(bid=700_000)
    )
    assert await notify_own_offers(ctx) == "1 Gebot auf eigene Spieler gemeldet"
    assert len(ctx.telegram.texts) == 2


# ---------------------------------------------------------------- matchday_report (C3.3)


MATCHDAY_USER_ID = "9000001"  # Manager 1, the owner of the `user_me.json` fixture
MATCHDAY_NUMBER = 5  # `league_ranking_day.json` → latest_matchday = 5
MATCHDAY_KEY = "notify.matchday_report"
USER_CENTER_PATH = f"/v4/leagues/{TEAM_LEAGUE_ID}/users/{MATCHDAY_USER_ID}/teamcenter"

# Every player that appears in the lineup or on the bench of the teamcenter fixture,
# with the position the local `players` table must know for the cross-position swap check.
_LINEUP_PLAYERS: dict[str, int] = {
    "3204": 4,  # Conté (ST)
    "3470": 2,  # Caci (ABW)
    "3759": 2,  # Gutiérrez (ABW)
    "6061": 3,  # Maza (MF)
    "7737": 3,  # Veerman (MF)
    "8227": 1,  # Vandevoordt (TW)
    "8307": 3,  # Sano (MF)
    "10120": 4,  # Røssing-Lelesiit (ST)
    "11956": 3,  # Fernández (MF)
    "17285": 2,  # Brassier (ABW)
    "17288": 3,  # Regeer (MF)
}
# Squad fixture (`league_squad.json`) has Hansen (6292) – the only player not in the lineup,
# so the bench fallback (squad minus lineup) finds exactly him.
_BENCH_PLAYERS: dict[str, int] = {"6292": 2}


async def _setup_matchday_ctx(
    ctx, *, finished: bool = True, seed_matchday_1_to_4: bool = True
) -> FakeOpenLigaDb:
    """Mock every endpoint and seed the Player rows `matchday_report` needs.

    `finished=True` (default) marks all OpenLigaDB matches as done so the job proceeds to
    write the row; the default fixture has every match still open, used for the "in
    progress" branch.

    `seed_matchday_1_to_4=True` (default) pre-populates `matchday_results` for earlier
    matchdays so the one-shot backfill is a no-op. The backfill test passes
    `seed_matchday_1_to_4=False` to leave the table empty and trigger backfill."""
    ctx.fake.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/ranking", fixture("league_ranking_day"))
    ctx.fake.on("GET", USER_CENTER_PATH, fixture("league_user_teamcenter"))
    ctx.fake.on("GET", "/v4/user/me", fixture("user_me"))
    ctx.fake.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/squad", fixture("league_squad"))

    oldb = FakeOpenLigaDb()
    if finished:
        md5 = copy.deepcopy(oldb_fixture("matchday5"))
        for match in md5:
            match["matchIsFinished"] = True
        oldb.routes["/getmatchdata/bl1/2026/5"] = md5
    ctx.oldb = make_oldb_client(oldb)
    ctx.oldb_fake = oldb

    async with ctx.sessions.begin() as session:
        for pid, pos in {**_LINEUP_PLAYERS, **_BENCH_PLAYERS}.items():
            session.add(
                Player(
                    id=pid,
                    name=f"P{pid}",
                    team_id="2",
                    position=pos,
                    status=0,
                    market_value=1_000_000,
                    image=None,
                    updated_at=ctx.clock(),
                )
            )
        if seed_matchday_1_to_4:
            # Earlier matchdays already recorded → backfill is a no-op, job focuses on day 5.
            for matchday in (1, 2, 3, 4):
                session.add(
                    MatchdayResult(
                        league_id=TEAM_LEAGUE_ID,
                        season=2026,
                        matchday=matchday,
                        points=100,
                        placement=3,
                        matchday_placement=3,
                        team_value=180_000_000,
                        created_at=ctx.clock(),
                    )
                )
    return oldb


async def _matchday_result_count(ctx) -> int:
    async with ctx.sessions() as session:
        return await session.scalar(select(func.count()).select_from(MatchdayResult))


async def _matchday_result_count_for(ctx, matchday: int) -> int:
    async with ctx.sessions() as session:
        return await session.scalar(
            select(func.count())
            .select_from(MatchdayResult)
            .where(MatchdayResult.matchday == matchday)
        )


async def test_matchday_report_sends_message_and_writes_row_and_kv(ctx):
    from app.worker.jobs import matchday_report

    await _setup_matchday_ctx(ctx)
    ctx = await _make_watch_ctx(ctx)

    assert await matchday_report(ctx) == "Spieltag 5 gemeldet (Backfill: nichts zu tun)"
    assert len(ctx.telegram.texts) == 1
    [text] = ctx.telegram.texts
    assert text.startswith("<b>Spieltag 5 · Testliga</b>")
    assert "934 Punkte" in text  # ranking fixture → matchday_points for Manager 1
    assert "Platz 2" in text

    async with ctx.sessions() as session:
        row = await session.scalar(
            select(MatchdayResult).where(MatchdayResult.matchday == MATCHDAY_NUMBER)
        )
    assert row.league_id == TEAM_LEAGUE_ID
    assert row.season == 2026
    assert row.matchday == MATCHDAY_NUMBER
    assert row.points == 934
    assert row.placement == 2
    assert row.matchday_placement == 2
    assert row.team_value == 182839707

    state = await ctx.kv.get(MATCHDAY_KEY)
    assert state["matchday"] == MATCHDAY_NUMBER
    assert state["backfill_done"] is True


async def test_matchday_report_does_not_alarm_a_second_time(ctx):
    from app.worker.jobs import matchday_report

    await _setup_matchday_ctx(ctx)
    ctx = await _make_watch_ctx(ctx)
    await matchday_report(ctx)
    assert len(ctx.telegram.texts) == 1
    assert await _matchday_result_count_for(ctx, MATCHDAY_NUMBER) == 1

    assert await matchday_report(ctx) == "schon gemeldet"
    assert len(ctx.telegram.texts) == 1
    assert await _matchday_result_count_for(ctx, MATCHDAY_NUMBER) == 1


async def test_matchday_report_failed_send_keeps_no_row_and_retries(ctx):
    from app.worker.jobs import matchday_report

    await _setup_matchday_ctx(ctx)
    ctx = await _make_watch_ctx(ctx)
    ctx.telegram.fail = True
    # The job catches the TelegramError and surfaces the retry message instead of
    # propagating – the worker must not see a failed run as a job failure.
    assert await matchday_report(ctx) == (
        f"Spieltag {MATCHDAY_NUMBER}: Telegram-Versand fehlgeschlagen,"
        " nächster Lauf wiederholt (Backfill: nichts zu tun)"
    )

    # No row for matchday 5, no matchday in state: the event is still pending for the next run.
    assert await _matchday_result_count_for(ctx, MATCHDAY_NUMBER) == 0
    state = await ctx.kv.get(MATCHDAY_KEY)
    assert state is None or "matchday" not in state

    # Retry: send now succeeds and the row + state get written. The first run set
    # `backfill_done` even on the failure path, so the retry is a plain "Spieltag
    # gemeldet" with no backfill noise.
    ctx.telegram.fail = False
    assert await matchday_report(ctx) == "Spieltag 5 gemeldet"
    assert len(ctx.telegram.texts) == 1  # only the retry reached Telegram
    assert await _matchday_result_count_for(ctx, MATCHDAY_NUMBER) == 1
    assert (await ctx.kv.get(MATCHDAY_KEY))["matchday"] == MATCHDAY_NUMBER


async def test_matchday_report_skips_when_matchday_is_still_in_progress(ctx):
    from app.worker.jobs import matchday_report

    await _setup_matchday_ctx(ctx, finished=False)
    ctx = await _make_watch_ctx(ctx)

    assert await matchday_report(ctx) == "Spieltag 5 läuft noch"
    assert ctx.telegram.texts == []
    assert await _matchday_result_count_for(ctx, MATCHDAY_NUMBER) == 0
    # The backfill (no-op with 1–4 seeded) ran first and stuck `backfill_done`, but
    # the live report is still gated on the OpenLigaDB "finished" check, so the row
    # and `matchday` in state are not written yet.
    state = await ctx.kv.get(MATCHDAY_KEY) or {}
    assert state.get("matchday") is None
    assert state.get("backfill_done") is True


async def test_matchday_report_without_telegram_writes_only_the_row(ctx):
    from app.worker.jobs import matchday_report

    await _setup_matchday_ctx(ctx)
    ctx.notifier = None
    ctx.notifications = NOTIFY_CONFIG

    assert await matchday_report(ctx) == "Spieltag 5 gemeldet (Backfill: nichts zu tun)"
    assert await _matchday_result_count_for(ctx, MATCHDAY_NUMBER) == 1
    assert (await ctx.kv.get(MATCHDAY_KEY))["matchday"] == MATCHDAY_NUMBER


async def test_matchday_report_with_disabled_rule_writes_only_the_row(ctx):
    from app.notify.events import MatchdayReportRule
    from app.worker.jobs import matchday_report

    await _setup_matchday_ctx(ctx)
    ctx = await _make_watch_ctx(ctx)
    ctx.notifications = NOTIFY_CONFIG.model_copy(
        update={"matchday_report": MatchdayReportRule(enabled=False)}
    )

    assert await matchday_report(ctx) == "Spieltag 5 gemeldet (Backfill: nichts zu tun)"
    assert ctx.telegram.texts == []
    assert await _matchday_result_count_for(ctx, MATCHDAY_NUMBER) == 1
    assert (await ctx.kv.get(MATCHDAY_KEY))["matchday"] == MATCHDAY_NUMBER


async def test_matchday_report_backfills_missing_earlier_matchdays_until_done(ctx):
    from app.worker.jobs import matchday_report

    await _setup_matchday_ctx(ctx, seed_matchday_1_to_4=False)
    ctx = await _make_watch_ctx(ctx)

    assert await ctx.kv.get(MATCHDAY_KEY) in (None, {})

    summary = await matchday_report(ctx)
    assert summary.startswith("Spieltag 5 gemeldet (Backfill:")
    # 4 earlier matchdays (1..4), the per-run budget is 2 – the rest stay missing.
    assert "2/4" in summary

    async with ctx.sessions() as session:
        rows = list(
            (
                await session.execute(select(MatchdayResult).order_by(MatchdayResult.matchday))
            ).scalars()
        )
    backfilled = sorted(r.matchday for r in rows if r.matchday < MATCHDAY_NUMBER)
    # Newest first: 4 and 3, not 1 and 2.
    assert backfilled == [3, 4]
    assert MATCHDAY_NUMBER in {r.matchday for r in rows}

    # `backfill_done` must NOT stick yet – two matchdays are still missing.
    state = await ctx.kv.get(MATCHDAY_KEY)
    assert state["matchday"] == MATCHDAY_NUMBER
    assert state.get("backfill_done") is not True

    # Second run: latest matchday already reported, but the backfill continues for
    # the remaining matchdays 2 and 1 (newest first, fits in the budget of 2). No
    # new alarm because the live matchday was already reported in the first run.
    ctx.telegram.texts.clear()
    summary = await matchday_report(ctx)
    assert summary == "Backfill: 2/2 nachgetragen"
    assert ctx.telegram.texts == []  # backfill is Telegram-free, no second alarm

    async with ctx.sessions() as session:
        rows = list(
            (
                await session.execute(select(MatchdayResult).order_by(MatchdayResult.matchday))
            ).scalars()
        )
    backfilled = sorted(r.matchday for r in rows if r.matchday < MATCHDAY_NUMBER)
    assert backfilled == [1, 2, 3, 4]

    # Now the table is complete: `backfill_done` sticks and a third run is a no-op.
    state = await ctx.kv.get(MATCHDAY_KEY)
    assert state["backfill_done"] is True
    assert await matchday_report(ctx) == "schon gemeldet"
