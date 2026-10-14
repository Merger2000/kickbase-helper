"""The decision box on the player page: buy (or keep/sell, for own players) with the score."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kickbase.calendar import market_day
from app.kickbase.client import KickbaseClient
from app.kickbase.league import ResolvedLeague
from app.openligadb.client import OpenLigaDbClient
from app.services.holding import holding_signal
from app.services.momentum import Momentum, daily_change
from app.services.momentum import change_percent as daily_percent
from app.services.players import PlayerPage, StartProbability
from app.services.recommendations import (
    Candidate,
    change_percent,
    load_basis,
    market_values_around,
    no_appearances,
)
from app.services.scoring import Score
from app.services.xp import XP

START_RISK = 50  # percent, like the sell view and the deadline warning


@dataclass(frozen=True)
class Decision:
    in_squad: bool
    profile: str  # "kaufen" or "verkaufen" (scores weight trend higher for own players)
    score: Score
    verdict: str
    tone: str  # "ok" | "warn"
    reasons: tuple[str, ...]
    start: StartProbability
    trend_percent: float | None
    min_score: float | None = None
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stale: bool = False
    # Erwartete Punkte am nächsten Spieltag (Startchance × Form × Gegner); None ohne Daten.
    xp: XP | None = None
    momentum: Momentum | None = None
    gain: int | None = None


async def player_decision(
    client: KickbaseClient,
    oldb: OpenLigaDbClient,
    sessions: async_sessionmaker,
    league: ResolvedLeague,
    page: PlayerPage,
    now: datetime | None = None,
) -> Decision:
    """Same rules as the buy and sell lists, so the player page never disagrees with them."""
    now = now or datetime.now(UTC)
    basis = await load_basis(client, oldb, sessions, league, now)
    squad = await client.squad(league.id)
    basis.fetched.append(squad)
    own = next((p for p in squad.data.players if p.id == page.id), None)
    in_squad = own is not None
    candidate = Candidate(
        id=page.id,
        name=page.name,
        team_id=page.team_id or "",
        team_name=page.team_name,
        position=page.position,
        # Like the lists: an own player's status from the squad, else from the player data.
        status=own.status if own else page.status,
        image=page.image,
    )
    if own is not None:
        # The trend exactly as on the sell list (Kickbase's 7-day change of the squad).
        trend = change_percent(own.market_value, own.market_value_change_7d)
    else:
        # The trend exactly as on the buy list (collected value `trend.days` ago).
        market = await client.market(league.id)
        basis.fetched.append(market)
        target = market_day(market.fetched_at) - timedelta(days=basis.config.trend.days)
        old = (await market_values_around(sessions, [page.id], target)).get(page.id)
        listing = next((p for p in market.data.players if p.id == page.id), None)
        value = listing.market_value if listing else page.market_value
        trend = (value - old) / old * 100 if old else None
    start = basis.start_probability(candidate)
    risky_start = start.percent is not None and start.percent < START_RISK
    start_reason = f"Startchance {start.percent} %: {', '.join(start.reasons)}"
    xp = basis.xp_for(candidate)

    if in_squad:
        profile = basis.config.profiles["verkaufen"]
        score = basis.score(candidate, trend, "verkaufen")
        market = await client.market(league.id)
        basis.fetched.append(market)
        signal = holding_signal(
            current=own.market_value,
            change_24h=daily_change(own),
            start=start,
            deadline=market.data.deadline,
            now=now,
            config=basis.config.trading,
        )
        return Decision(
            True,
            "verkaufen",
            score,
            signal.verdict,
            "warn" if signal.sell or signal.trading else "ok",
            signal.reasons,
            start,
            trend,
            notes=list(basis.notes),
            warnings=list(basis.warnings),
            stale=basis.stale,
            xp=xp,
            momentum=Momentum(
                daily_change(own), daily_percent(own.market_value, daily_change(own))
            ),
            gain=own.market_value_gain if "market_value_gain" in own.model_fields_set else None,
        )

    profile = basis.config.profiles["kaufen"]
    score = basis.score(candidate, trend, "kaufen")
    min_score = profile.min_score or 0
    reasons = [start_reason] if risky_start else []
    if no_appearances(basis, page.id):
        # The buy list leaves these out ("ohne Einsatz"), however cheap or rising.
        verdict, tone = "Diese Saison ohne Einsatz – nicht empfohlen", "warn"
    elif score.total >= min_score and not risky_start:
        verdict, tone = "Kaufenswert", "ok"
    elif score.total >= min_score:
        verdict, tone = "Kaufenswert, aber unsicherer Einsatz", "warn"
    else:
        verdict, tone = f"Unter dem Mindest-Score ({min_score:g})", "warn"
    return Decision(
        False,
        "kaufen",
        score,
        verdict,
        tone,
        tuple(reasons),
        start,
        trend,
        min_score=min_score,
        notes=list(basis.notes),
        warnings=list(basis.warnings),
        stale=basis.stale,
        xp=xp,
    )
