from datetime import UTC, datetime

from app.kickbase import models as m
from app.lineups.service import Prognosis
from app.services.player_ai import PlayerSummary, build_prompt, summarize
from app.services.players import PlayerPage, StartQuote
from tests.fakes import MemoryKV

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def make_page(**kwargs) -> PlayerPage:
    defaults = dict(
        id="1",
        name="Max Mustermann",
        team_name="Bayern",
        position=m.Position.FORWARD,
        status=0,
        image=None,
        market_value=17_000_000,
        market_value_change_7d=1_000_000,
        average_points=10,
        total_points=80,
        goals=5,
        assists=3,
        market_values=(),
        matchdays=(),
        upcoming=(),
        season=2026,
        collected=True,
        fetched_at=NOW,
        stale=False,
        start_quote=StartQuote(3, 5),
        prognosis=Prognosis(True),
    )
    defaults.update(kwargs)
    return PlayerPage(**defaults)


def test_build_prompt_includes_the_key_numbers():
    prompt = build_prompt(make_page(), [])
    assert "Max Mustermann" in prompt
    assert "17.0 Mio." in prompt
    assert "5 Tore" in prompt
    assert "LigaInsider: in der voraussichtlichen Startelf" in prompt


class FakeLlm:
    def __init__(self, text: str = "Ein starker Angreifer."):
        self.calls = 0
        self.text = text

    async def complete(self, task, system, user, schema_name, output):
        self.calls += 1
        return type("C", (), {"data": PlayerSummary(text=self.text)})()


async def test_summarize_caches_per_market_day():
    from datetime import timedelta

    llm = FakeLlm()
    kv = MemoryKV()
    page = make_page()
    first = await summarize(llm, kv, page, [], NOW)
    second = await summarize(llm, kv, page, [], NOW)
    assert first == second == "Ein starker Angreifer."
    assert llm.calls == 1
    await summarize(llm, kv, page, [], NOW + timedelta(days=1))  # a new market day
    assert llm.calls == 2


async def test_summarize_drops_text_with_unknown_numbers():
    llm = FakeLlm(text="Der Spieler hat 99 Tore in 12 Spielen.")
    kv = MemoryKV()
    result = await summarize(llm, kv, make_page(), [], NOW)
    assert result is None and llm.calls == 1  # not cached, nothing stored


async def test_summarize_drops_empty_text():
    llm = FakeLlm(text="   ")
    kv = MemoryKV()
    assert await summarize(llm, kv, make_page(), [], NOW) is None


def test_build_prompt_explains_the_dashboard_verdict():
    from app.services.decision import Decision
    from app.services.players import StartProbability
    from app.services.scoring import Component, Score

    components = (
        Component("form", "Form", 0.8, 0.35, 28.0, "Ø 12 Punkte in den letzten Spielen"),
        Component("trend", "Trend", 0.4, 0.30, 12.0, "Marktwert −2 % in 7 Tagen"),
    )
    score = Score(total=40.0, components=components, clamped=False)
    decision = Decision(
        in_squad=False,
        profile="kaufen",
        score=score,
        verdict="Kaufenswert",
        tone="ok",
        reasons=(),
        start=StartProbability(81, ()),
        trend_percent=-2.0,
    )
    prompt = build_prompt(make_page(), [], decision)
    assert "Einschätzung des Dashboards (nicht im eigenen Kader): Kaufenswert, Score 40" in prompt
    assert "- Form +28: Ø 12 Punkte in den letzten Spielen" in prompt
    assert "Kickbase-Punkte diese Saison: 80 gesamt" in prompt


async def test_summary_points_get_their_own_lines():
    text = "**Dafür:** gut. **Dagegen:** teuer. **Achte auf:** den Status."
    result = await summarize(FakeLlm(text), MemoryKV(), make_page(), [], NOW)
    assert result.splitlines() == [
        "**Dafür:** gut.",
        "**Dagegen:** teuer.",
        "**Achte auf:** den Status.",
    ]
