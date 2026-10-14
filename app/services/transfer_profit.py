"""Own transfer profit from recorded events, never from guessed purchase prices.

An acquisition can be paired only with the next recorded transfer of that player. Missing
seller identity, a different buyer, ambiguous timestamps or another purchase break the chain.
The result is an explicitly partial accounting of known trades, not a full account balance.
"""

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from itertools import groupby

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import JobRun, LeagueTransfer
from app.kickbase import models as m
from app.services.transfers import JOB_MAX_AGE, JOB_NAME

SALE_PERIOD_DAYS = 60


@dataclass(frozen=True)
class TransferEvent:
    id: str
    player_id: str
    name: str
    at: datetime
    kind: int
    price: int
    bought_by_me: bool
    sold_by_me: bool | None


@dataclass(frozen=True)
class ProfitRow:
    player_id: str
    name: str
    purchased_at: datetime | None
    purchase_price: int | None
    sold_at: datetime | None
    sale_price: int | None
    market_value: int | None = None

    @property
    def gain(self) -> int | None:
        value = self.sale_price if self.sold_at is not None else self.market_value
        return None if self.purchase_price is None or value is None else value - self.purchase_price

    @property
    def held_days(self) -> int | None:
        return (
            None
            if self.purchased_at is None or self.sold_at is None
            else (self.sold_at - self.purchased_at).days
        )


@dataclass(frozen=True)
class ProfitView:
    since: datetime
    closed: tuple[ProfitRow, ...]
    holdings: tuple[ProfitRow, ...]
    unknown_sellers: int
    history_since: datetime | None
    stale: bool = False
    fetched_at: datetime | None = None
    collection_warning: str | None = None

    @property
    def known_closed(self) -> tuple[ProfitRow, ...]:
        return tuple(r for r in self.closed if r.gain is not None)

    @property
    def realized(self) -> int | None:
        return sum(r.gain for r in self.known_closed) if self.known_closed else None

    @property
    def open_gain(self) -> int | None:
        known = [r.gain for r in self.holdings if r.gain is not None]
        return sum(known) if known else None


def build_profit(
    events: list[TransferEvent], squad: list[m.SquadPlayer], now: datetime
) -> ProfitView:
    since = now - timedelta(days=SALE_PERIOD_DAYS)
    by_player: dict[str, list[TransferEvent]] = defaultdict(list)
    # Database activity IDs are unique. Deduping also makes replay of identical input safe.
    for e in {e.id: e for e in events if e.at <= now}.values():
        by_player[e.player_id].append(e)
    acquisitions: dict[str, TransferEvent] = {}
    closed = []
    unknown_sellers = 0
    for pid, history in by_player.items():
        purchase = None
        for _, at_events in groupby(sorted(history, key=lambda e: e.at), key=lambda e: e.at):
            group = list(at_events)
            # No ordering from IDs: two transfers at one timestamp are ambiguous.
            ambiguous = len(group) > 1
            if ambiguous:
                purchase = None
            for event in group:
                valid = (
                    event.kind in (m.TransferKind.BOUGHT, m.TransferKind.SOLD_TO_KICKBASE)
                    and event.price >= 0
                )
                sale = valid and event.sold_by_me is True and not event.bought_by_me
                if sale and event.at >= since:
                    closed.append(
                        ProfitRow(
                            pid,
                            event.name,
                            purchase.at if purchase else None,
                            purchase.price if purchase else None,
                            event.at,
                            event.price,
                        )
                    )
                if (
                    event.kind == m.TransferKind.SOLD_TO_KICKBASE
                    and event.sold_by_me is None
                    and event.at >= since
                ):
                    unknown_sellers += 1
                # Every transfer invalidates the old possession. Only an unambiguous own
                # purchase starts a new one; never bridge a sale with unknown identity.
                purchase = (
                    event
                    if valid
                    and not ambiguous
                    and event.kind == m.TransferKind.BOUGHT
                    and event.bought_by_me
                    and event.sold_by_me is not True
                    else None
                )
        if purchase:
            acquisitions[pid] = purchase
    holdings = []
    for player in squad:
        buy = acquisitions.get(player.id)
        holdings.append(
            ProfitRow(
                player.id,
                player.name,
                buy.at if buy else None,
                buy.price if buy else None,
                None,
                None,
                player.market_value,
            )
        )
    closed.sort(key=lambda r: r.sold_at, reverse=True)
    holdings.sort(key=lambda r: (r.gain is None, -(r.gain or 0)))
    return ProfitView(
        since,
        tuple(closed),
        tuple(holdings),
        unknown_sellers,
        min((e.at for history in by_player.values() for e in history), default=None),
    )


async def load_profit(
    sessions: async_sessionmaker, league_id: str, squad: list[m.SquadPlayer], now: datetime
) -> ProfitView:
    async with sessions() as session:
        rows = (
            await session.scalars(
                select(LeagueTransfer)
                .where(LeagueTransfer.league_id == league_id, LeagueTransfer.at <= now)
                .order_by(LeagueTransfer.at)
            )
        ).all()
        last = await session.scalar(
            select(JobRun)
            .where(JobRun.job == JOB_NAME, JobRun.finished_at.is_not(None))
            .order_by(JobRun.finished_at.desc())
            .limit(1)
        )
    warning = None
    if last is None:
        warning = (
            "Noch kein abgeschlossener Transfer-Sammlerlauf gespeichert. Historie kann fehlen."
        )
    elif last.status != "ok":
        warning = "Letzter Transfer-Abruf fehlgeschlagen. Neuere Käufe und Verkäufe können fehlen."
    elif last.detail and "nicht lesbar" in last.detail:
        warning = (
            "Transfer-Abruf teilweise unlesbar. Die erfassten Gewinne können unvollständig sein."
        )
    elif now - last.finished_at > JOB_MAX_AGE:
        warning = (
            "Transferhistorie seit über 6 Stunden nicht aktualisiert. "
            "Neuere Abschlüsse können fehlen."
        )
    data = build_profit(
        [
            TransferEvent(
                r.activity_id,
                r.player_id,
                r.player_name,
                r.at,
                r.kind,
                r.price,
                r.by_me,
                r.sold_by_me,
            )
            for r in rows
        ],
        squad,
        now,
    )
    return replace(data, collection_warning=warning)
