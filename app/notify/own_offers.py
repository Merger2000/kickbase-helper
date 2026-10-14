"""Own-offers alarms (Block C, C2.2/C2.3).

Pure rule layer for Telegram notifications about bids on the manager's own
players. Mirrors the `app.notify.events` pattern: small `Event` records with
a `key` and a `text`; `collect_own_offer_events` also dedupes against the
shared `SENT_KEY`, and `commit_own_offers` records what was sent. The worker
(`C2.3`) wires the Kickbase data in and the Telegram send out. No Kickbase /
DB I/O lives in here – callers pass the data in, this module decides what
fires and why not.

The rule triggers on a listing whose seller is the manager themselves *and*
whose best bid clears the configured premium over the player's market value.
Kickbase's own sale offer (no `seller`) is skipped: it always pays the market
value, never a premium, and there is nothing to react to.
"""

import html
from datetime import date, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from app.kickbase import models as m
from app.notify.events import KEEP_SENT, SENT_KEY, Event, OwnOffersRule
from app.web.formatting import money_short, percent


class KeyValue(Protocol):
    async def get(self, key: str) -> Any | None: ...
    async def set(self, key: str, value: Any) -> None: ...


def _player_link(player: m.MarketPlayer, base_url: str) -> str:
    name = f"{player.first_name} {player.last_name}".strip()
    return f'<a href="{base_url}/spieler/{player.id}">{html.escape(name)}</a>'


def _expiry_label(player: m.MarketPlayer, fetched_at: datetime, tz: ZoneInfo, now: datetime) -> str:
    """German short label for the listing expiry; falls back to "ohne Ablauf" when the
    manager posted an open-ended listing (`expires_in_seconds == None`)."""
    end = player.expires_at(fetched_at)
    if end is None:
        return "ohne Ablauf"
    local = end.astimezone(tz)
    same_day = local.date() == now.astimezone(tz).date()
    return f"läuft bis {local:%H:%M}" if same_day else f"läuft bis {local:%d.%m. %H:%M}"


def own_offer_events(
    market: list[m.MarketPlayer],
    my_user_id: str,
    rule: OwnOffersRule,
    fetched_at: datetime,
    tz: ZoneInfo,
    base_url: str,
    now: datetime | None = None,
) -> list[Event]:
    """One event per (listing, bid amount) combination that clears the premium.

    Filters applied in order – cheapest first:

    * the alarm itself is disabled in config → nothing
    * the listing has no `seller` (Kickbase sells the player) → never a premium, skip
    * the seller is not the manager → not an own listing, skip
    * nobody has bid yet → no premium to react to, skip
    * the best bid does not exceed MV × (1 + `min_premium_pct` / 100) → skip

    The dedupe key is `own_offers:{listing_key}:{amount}`. A higher follow-up bid
    produces a new key, so the manager is warned again – the spec says "höheres
    Gebot = neuer Alarm"."""
    if not rule.enabled:
        return []
    threshold_factor = 1 + rule.min_premium_pct / 100
    events: list[Event] = []
    now = now or datetime.now(tz)
    for player in market:
        if player.seller is None or player.seller.id != my_user_id:
            continue
        best = player.best_offer
        if best is None:
            continue
        if player.market_value <= 0:
            continue
        threshold_amount = player.market_value * threshold_factor
        if best.amount <= threshold_amount:
            continue
        premium = best.amount - player.market_value
        premium_pct = premium / player.market_value * 100
        expiry = _expiry_label(player, fetched_at, tz, now)
        events.append(
            Event(
                key=f"own_offers:{player.listing_key}:{best.amount}",
                text=(
                    f"💰 {_player_link(player, base_url)}: "
                    f"Gebot {money_short(best.amount)} "
                    f"(MW {money_short(player.market_value)}, "
                    f"+{money_short(premium)} / +{percent(premium_pct)}), "
                    f"{expiry} – annehmen nur in der Kickbase-App"
                ),
            )
        )
    return events


async def collect_own_offer_events(
    market: list[m.MarketPlayer],
    my_user_id: str,
    rule: OwnOffersRule,
    fetched_at: datetime,
    tz: ZoneInfo,
    base_url: str,
    kv: KeyValue,
    now: datetime | None = None,
) -> list[Event]:
    """Build the own-offers events, deduped against the shared `SENT_KEY`.

    Pure rule layer returns one event per (listing, bid amount) that clears
    the premium; here we strip out the ones already announced so the worker
    can fire-and-forget the rest. No DB writes."""
    candidates = own_offer_events(market, my_user_id, rule, fetched_at, tz, base_url, now=now)
    sent = await kv.get(SENT_KEY) or {}
    return [event for event in candidates if event.key not in sent]


async def commit_own_offers(kv: KeyValue, events: list[Event], today: date) -> None:
    """Record the sent events and trim `SENT_KEY` to the last two weeks.

    Called only after the Telegram send succeeded – a failed send keeps the
    event for the next run. There is no per-rule baseline to persist: the
    `listing_key:amount` already encodes every input that would matter."""
    sent = await kv.get(SENT_KEY) or {}
    sent.update({event.key: today.isoformat() for event in events})
    cutoff = (today - KEEP_SENT).isoformat()
    await kv.set(SENT_KEY, {key: day for key, day in sent.items() if day >= cutoff})
