"""Watchlist alarms (Block C, C1.3).

Pure rule layer for Telegram notifications about watchlist players. Mirrors the
`app.notify.events` pattern: small `Event` records with a `key` and a `text`,
collected asynchronously (to read the dedupe set + baselines from kv) and
committed after a successful send. No Kickbase / DB I/O lives in here – callers
pass the data in, this module decides what fires and why not.
"""

import html
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

import yaml

from app.kickbase import models as m
from app.lineups.service import TeamPrediction
from app.notify.events import KEEP_SENT, SENT_KEY, Event, WatchlistConfig, WatchlistRule
from app.services.watchlist import WatchlistItem
from app.web.formatting import money_short

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "notifications.yaml"
WATCHLIST_STATUS_KEY = "notify.watchlist.status"  # baseline status per watched player
WATCHLIST_LINEUP_KEY = "notify.watchlist.lineup"  # baseline: was the player in the XI last time?


class KeyValue(Protocol):
    async def get(self, key: str) -> Any | None: ...
    async def set(self, key: str, value: Any) -> None: ...


def _watchlist_link(item: WatchlistItem, base_url: str) -> str:
    return (
        f'<a href="{base_url}/spieler/{item.entry.player_id}">{html.escape(item.player_name)}</a>'
    )


def _market_link(player: m.MarketPlayer, base_url: str) -> str:
    name = f"{player.first_name} {player.last_name}".strip()
    return f'<a href="{base_url}/spieler/{player.id}">{html.escape(name)}</a>'


def listing_events(
    watchlist: list[WatchlistItem],
    market: list[m.MarketPlayer],
    squad_ids: set[str],
    my_offer_listing_keys: set[str],
    rule: WatchlistRule,
    base_url: str,
) -> list[Event]:
    """Player newly listed on the market.

    Suppressed for own squad (covered by `notify_squad`) and for any listing the
    manager already bid on (the price/status alarms still apply; this would
    just be noise on top of the user's own intent)."""
    if not rule.enabled:
        return []
    by_id = {p.id: p for p in market}
    events: list[Event] = []
    for item in watchlist:
        if item.entry.player_id in squad_ids:
            continue
        player = by_id.get(item.entry.player_id)
        if player is None:
            continue
        if player.listing_key in my_offer_listing_keys:
            continue
        events.append(
            Event(
                key=f"watchlist.listing:{player.listing_key}",
                text=(
                    f"🛒 {_market_link(player, base_url)} ist auf dem Markt "
                    f"({money_short(player.price)})"
                ),
            )
        )
    return events


def price_events(
    watchlist: list[WatchlistItem],
    market: list[m.MarketPlayer],
    squad_ids: set[str],
    rule: WatchlistRule,
    base_url: str,
) -> list[Event]:
    """A watched player crosses below `price_below`.

    Compared against the live asking price when the player is listed, otherwise
    the current market value (still useful as a "value" alert). Own squad is
    skipped – the user already owns the player, no buy decision involved."""
    if not rule.enabled:
        return []
    by_id = {p.id: p for p in market}
    events: list[Event] = []
    for item in watchlist:
        threshold = item.entry.price_below
        if threshold is None:
            continue
        if item.entry.player_id in squad_ids:
            continue
        player = by_id.get(item.entry.player_id)
        if player is not None:
            price = player.price
            listed = True
        else:
            price = item.market_value
            listed = False
        if price > threshold:
            continue
        icon = "💸" if listed else "💰"
        context = "Marktangebot" if listed else "Marktwert"
        events.append(
            Event(
                key=f"watchlist.price:{item.entry.player_id}:{threshold}:{price}",
                text=(
                    f"{icon} {_watchlist_link(item, base_url)}: "
                    f"{context} {money_short(price)} ≤ Schwelle {money_short(threshold)}"
                ),
            )
        )
    return events


def status_events(
    watchlist: list[WatchlistItem],
    previous: dict[str, int],
    rule: WatchlistRule,
    base_url: str,
) -> list[Event]:
    """Status change for a watched player.

    Players new to the watchlist are *not* changes – like `events.status_events`
    the first run just builds the baseline, then only real transitions fire."""
    if not rule.enabled:
        return []
    events: list[Event] = []
    for item in watchlist:
        old = previous.get(item.entry.player_id)
        if old is None or old == item.status:
            continue
        icon = "✅" if item.availability is m.Availability.FIT else "🚑"
        events.append(
            Event(
                key=f"watchlist.status:{item.entry.player_id}:{old}>{item.status}",
                text=(
                    f"{icon} {_watchlist_link(item, base_url)}: "
                    f"{m.status_label(old)} → {item.status_label}"
                ),
            )
        )
    return events


def lineup_dropped_events(
    watchlist: list[WatchlistItem],
    team_by_player: dict[str, str],
    predicted: dict[str, TeamPrediction],
    previous_in_xi: dict[str, bool],
    rule: WatchlistRule,
    base_url: str,
) -> list[Event]:
    """A watched player is no longer in the LigaInsider starting eleven.

    Fires only when the previous run had him as a starter and this run does not
    (either missing or only an "alternative"). Clubs without a current
    prediction are ignored – we can't tell whether the player dropped or
    nothing is known."""
    if not rule.enabled:
        return []
    events: list[Event] = []
    for item in watchlist:
        team_id = team_by_player.get(item.entry.player_id)
        if team_id is None:
            continue
        team = predicted.get(team_id)
        if team is None:
            continue  # no current prediction for his club
        was_in_xi = previous_in_xi.get(item.entry.player_id)
        if not was_in_xi:
            continue  # not in the XI last run → nothing changed
        if item.entry.player_id in team.starting:
            continue  # still a starter
        events.append(
            Event(
                key=f"watchlist.lineup:{item.entry.player_id}",
                text=(f"⚠️ {_watchlist_link(item, base_url)} fällt aus der Prognose-Elf"),
            )
        )
    return events


@dataclass(frozen=True)
class Collected:
    """Outcome of one watchlist run – events to send plus the new baselines."""

    events: list[Event] = field(default_factory=list)
    # Status of every watched player now; saved with `commit` once the message went out.
    statuses: dict[str, int] = field(default_factory=dict)
    # `True` for every watched player who is in the current starting eleven;
    # fed back as `previous_in_xi` on the next run.
    lineups: dict[str, bool] = field(default_factory=dict)


def _current_in_xi(
    watchlist: list[WatchlistItem],
    team_by_player: dict[str, str],
    predicted: dict[str, TeamPrediction],
) -> dict[str, bool]:
    result: dict[str, bool] = {}
    for item in watchlist:
        team_id = team_by_player.get(item.entry.player_id)
        team = predicted.get(team_id) if team_id else None
        result[item.entry.player_id] = bool(team and item.entry.player_id in team.starting)
    return result


async def collect_watchlist_events(
    watchlist: list[WatchlistItem],
    market: list[m.MarketPlayer],
    predicted: dict[str, TeamPrediction],
    squad_ids: set[str],
    my_offer_listing_keys: set[str],
    team_by_player: dict[str, str],
    config: WatchlistConfig,
    kv: KeyValue,
    base_url: str,
) -> Collected:
    """Build the watchlist events, deduped against the shared `SENT_KEY`.

    Reads the status and lineup baselines from kv – a `None` baseline means
    "first run" and turns off status / lineup alarms for this pass (the caller
    still gets the new baseline back so it can be persisted)."""
    events: list[Event] = []
    events += listing_events(
        watchlist, market, squad_ids, my_offer_listing_keys, config.listing, base_url
    )
    events += price_events(watchlist, market, squad_ids, config.price_below, base_url)
    previous_status = await kv.get(WATCHLIST_STATUS_KEY)
    if config.status_changes.enabled and previous_status is not None:
        events += status_events(watchlist, previous_status, config.status_changes, base_url)
    previous_in_xi = await kv.get(WATCHLIST_LINEUP_KEY)
    if config.lineup_dropped.enabled and previous_in_xi is not None:
        events += lineup_dropped_events(
            watchlist, team_by_player, predicted, previous_in_xi, config.lineup_dropped, base_url
        )
    sent = await kv.get(SENT_KEY) or {}
    return Collected(
        events=[event for event in events if event.key not in sent],
        statuses={item.entry.player_id: item.status for item in watchlist},
        lineups=_current_in_xi(watchlist, team_by_player, predicted),
    )


async def commit_watchlist(kv: KeyValue, collected: Collected, today: Any) -> None:
    """Record sent events (shared dedupe set) and the new watchlist baselines.

    Called after a successful send or when there was nothing to send – never
    before, so a failed send keeps the event for the next run."""
    sent = await kv.get(SENT_KEY) or {}
    sent.update({event.key: today.isoformat() for event in collected.events})
    cutoff = (today - KEEP_SENT).isoformat()
    await kv.set(SENT_KEY, {key: day for key, day in sent.items() if day >= cutoff})
    await kv.set(WATCHLIST_STATUS_KEY, collected.statuses)
    await kv.set(WATCHLIST_LINEUP_KEY, collected.lineups)


@lru_cache
def load_watchlist_config(path: Path = CONFIG_PATH) -> WatchlistConfig:
    """Read the `watchlist:` section from `config/notifications.yaml`.

    A file without that section falls back to all alarms on – the watchlist
    feature is opt-in via UI, so the default behaviour should be helpful, not
    silent. A bare `true` / `false` is accepted as a shortcut for `{enabled: ...}`
    so the YAML stays short (the validator on `WatchlistConfig` does the same)."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    section = data.get("watchlist") or {}
    return WatchlistConfig.model_validate(section)
