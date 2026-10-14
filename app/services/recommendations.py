"""Buy, sell and line-up recommendations built from the explainable score."""

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import MarketValue
from app.db.stores import KeyValueStore
from app.kickbase import models as m
from app.kickbase.calendar import market_day, near_market_value_update
from app.kickbase.client import Fetched, KickbaseClient
from app.kickbase.league import ResolvedLeague
from app.lineups.service import (
    InjuryNote,
    TeamPrediction,
    injuries_for_players,
    ligainsider_hint,
    predicted_players,
    prediction_for,
)
from app.openligadb.client import OpenLigaDbClient, OpenLigaDbError, OpenLigaDbSchemaError
from app.services.form import FormTable, load_form
from app.services.holding import holding_signal
from app.services.holding_roles import roles_for
from app.services.klasse import ClassInput, load_class
from app.services.lineup import FORMATIONS, Formation
from app.services.momentum import Momentum, daily_change, load_momentum
from app.services.players import (
    DEPARTED_AFTER,
    StartProbability,
    StartQuote,
    compute_start_probability,
    start_quotes_all,
)
from app.services.programme import Programmes, load_programmes
from app.services.scoring import (
    Profile,
    Score,
    ScoreInputs,
    ScoringConfig,
    compute_score,
    load_scoring_config,
)
from app.services.sell_guide import GuideWeights, SalePlayer, SellGuide, build_sell_guide
from app.services.team import image_url
from app.services.transfers import BidAdvice, BidModel
from app.services.xp import XP, expected_points

logger = logging.getLogger(__name__)

BUY_LIMIT = 20
LINE_ORDER = (m.Position.GOALKEEPER, m.Position.DEFENDER, m.Position.MIDFIELDER, m.Position.FORWARD)


@dataclass(frozen=True)
class Candidate:
    id: str
    name: str
    team_id: str
    team_name: str | None
    position: m.Position
    status: int
    image: str | None

    @property
    def availability(self) -> m.Availability:
        return m.availability(self.status)

    @property
    def status_label(self) -> str:
        return m.status_label(self.status)

    @property
    def initials(self) -> str:
        return "".join(part[0] for part in self.name.split()[:2]).upper() or "?"


@dataclass(frozen=True)
class Recommendation:
    player: Candidate
    score: Score
    market_value: int
    price: int | None = None
    per_million: float | None = None
    expires_at: datetime | None = None
    seller: str | None = None
    seller_is_kickbase: bool = False
    gain: int | None = None
    reasons: tuple[str, ...] = ()
    bid: BidAdvice | None = None
    # True: the offer ends before the next lineup deadline (the player can still play then).
    before_deadline: bool | None = None
    start: StartProbability | None = None  # start chance with reasons (best eleven)
    my_offer: int | None = None  # own bid on this offer (buy list)
    # Relative lineup index (legacy internal name xP). Used to pick the best eleven.
    xp: XP | None = None
    # C4.3: same metric per Mio. € price – cheaper xP wins on a tight budget (None: no price
    # or no xP at all). Stays None rather than 0 so a price-less Kickbase listing does not
    # look like a free player.
    xp_per_million: float | None = None
    momentum: Momentum | None = None
    holding_role: str = "auto"


@dataclass
class Basis:
    """Everything the scores need, loaded once per page."""

    config: ScoringConfig
    form: FormTable
    programmes: Programmes | None
    team_names: dict[str, str]
    fetched: list[Fetched] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # informational, may be folded away
    # A source failed or is outdated: always shown, never folded (AGENTS.md: make it visible).
    warnings: list[str] = field(default_factory=list)
    # Phase 7.5/7.6: start-probability inputs (predicted XI, injuries, start quotes).
    predicted: dict[str, TeamPrediction] = field(default_factory=dict)
    injuries: dict[str, InjuryNote] = field(default_factory=dict)
    quotes: dict[str, StartQuote] = field(default_factory=dict)
    klasse: dict[str, ClassInput] = field(default_factory=dict)
    holding_roles: dict[str, str] = field(default_factory=dict)

    def start_probability(self, player: Candidate) -> StartProbability:
        return compute_start_probability(
            prediction_for(self.predicted, player.team_id, player.id),
            self.injuries.get(player.id),
            player.availability,
            self.quotes.get(player.id),
            player.status_label,
        )

    def einsatz_value(self, player: Candidate) -> float | None:
        """Start probability 0…1, or None without any data (scored as neutral)."""
        prob = self.start_probability(player)
        return prob.percent / 100 if prob.percent is not None else None

    def xp_for(self, player: Candidate) -> XP:
        """Relative lineup index = start probability · positional form rank · programme."""
        programme = self.programmes.by_team.get(player.team_id) if self.programmes else None
        return expected_points(
            start=self.start_probability(player),
            form=self.form.by_player.get(player.id),
            programme=programme.as_input() if programme else None,
            config=self.config.xp,
        )

    def inputs(self, player: Candidate, trend_percent: float | None) -> ScoreInputs:
        programme = self.programmes.by_team.get(player.team_id) if self.programmes else None
        return ScoreInputs(
            availability=player.availability,
            status_label=player.status_label,
            form=self.form.by_player.get(player.id),
            trend_percent=trend_percent,
            programme=programme.as_input() if programme else None,
            einsatz=self.einsatz_value(player),
            klasse=self.klasse.get(player.id),
        )

    def score(self, player: Candidate, trend_percent: float | None, profile: str) -> Score:
        return compute_score(
            self.inputs(player, trend_percent), self.config.profiles[profile], self.config
        )

    @property
    def all_notes(self) -> list[str]:
        return [*self.warnings, *self.notes]

    @property
    def fetched_at(self) -> datetime:
        times = [f.fetched_at for f in self.fetched]
        if self.programmes:
            times.append(self.programmes.fetched_at)
        return min(times)

    @property
    def stale(self) -> bool:
        return any(f.stale for f in self.fetched) or bool(self.programmes and self.programmes.stale)


async def load_basis(
    client: KickbaseClient,
    oldb: OpenLigaDbClient,
    sessions: async_sessionmaker,
    league: ResolvedLeague,
    now: datetime | None = None,
) -> Basis:
    config = load_scoring_config()
    schedule = await client.competition_matchdays(league.competition_id)
    table = await client.competition_table(league.competition_id)
    team_names = {t.team_id: t.team_name for t in table.data.teams}
    season = schedule.data.season_start_year
    basis = Basis(config, FormTable({}, ()), None, team_names, [schedule, table])
    basis.holding_roles = await roles_for(sessions, league.id)
    if season is None:
        basis.notes.append("Kein Spielplan veröffentlicht: Form und Programm fehlen.")
        return basis
    finished = {
        md.matchday: 2 * len(md.matches)
        for md in schedule.data.matchdays
        if md.matches and all(match.is_finished for match in md.matches)
    }
    basis.form = await load_form(sessions, season, finished, config.form.recency_weights)
    now_for_class = now or datetime.now(UTC)
    basis.klasse = await load_class(
        sessions,
        season,
        now_for_class - DEPARTED_AFTER,
        config.klasse.max_season_share,
        config.klasse.full_after_appearances,
    )
    if not basis.form.matchdays:
        basis.notes.append("Noch keine Spieltagspunkte gesammelt: Form zählt für alle neutral.")
    if basis.form.incomplete:
        days = ", ".join(f"{md}." for md in basis.form.incomplete)
        basis.notes.append(
            f"Spieltag {days} ist noch nicht vollständig gesammelt; die Form berücksichtigt "
            "ihn erst nach dem nächsten Abgleich."
        )
    next_matchday = schedule.data.next_matchday or (max(finished, default=0) + 1)
    try:
        basis.programmes = await load_programmes(
            oldb, season, next_matchday, team_names, config.programme, schedule.fetched_at
        )
    except OpenLigaDbSchemaError as exc:
        logger.error("Programm nicht verfügbar: %s", exc)
        basis.warnings.append("OpenLigaDB liefert ein unerwartetes Format: Programm zählt neutral.")
    except OpenLigaDbError as exc:
        logger.warning("Programm nicht verfügbar: %s", exc)
        basis.warnings.append(f"Programm nicht verfügbar ({exc}): zählt für alle neutral.")
    else:
        missing = basis.programmes.missing
        if missing and set(missing.values()) == {"Saison beendet"}:
            basis.notes.append("Saison beendet: das Programm zählt neutral.")
        elif missing:
            teams = ", ".join(f"{team_names.get(t, t)} ({why})" for t, why in missing.items())
            basis.notes.append(f"Kein Programm für {teams}: neutral angesetzt.")
    # Phase 7.6: start-probability inputs for the "Einsatz" score component.
    now = now or datetime.now(UTC)
    basis.predicted = await predicted_players(sessions, now)
    basis.injuries = await injuries_for_players(sessions, now)
    basis.quotes = await start_quotes_all(sessions, season)
    if hint := await ligainsider_hint(sessions, KeyValueStore(sessions), now):
        basis.warnings.append(hint)
    return basis


def _candidate(
    player_id: str,
    name: str,
    team_id: str,
    position: m.Position,
    status: int,
    image: str | None,
    team_names: dict[str, str],
) -> Candidate:
    return Candidate(
        id=player_id,
        name=name,
        team_id=team_id,
        team_name=team_names.get(team_id),
        position=position,
        status=status,
        image=image_url(image),
    )


def change_percent(current: int, change: int) -> float | None:
    before = current - change
    return change / before * 100 if before > 0 else None


async def market_values_around(
    sessions: async_sessionmaker, player_ids: list[str], target: date
) -> dict[str, int]:
    """Value per player on `target` or up to two days before (latest one wins)."""
    if not player_ids:
        return {}
    async with sessions() as session:
        rows = (
            await session.execute(
                select(MarketValue.player_id, MarketValue.day, MarketValue.value)
                .where(
                    MarketValue.player_id.in_(player_ids),
                    MarketValue.day <= target,
                    MarketValue.day >= target - timedelta(days=2),
                )
                .order_by(MarketValue.day)
            )
        ).all()
    return {player_id: value for player_id, _, value in rows}


# ---------------------------------------------------------------------------- buy


@dataclass(frozen=True)
class BuyView:
    budget: int
    # All candidates above the minimum score, best score per million first (unfiltered).
    recommendations: tuple[Recommendation, ...]
    # Good offers above the budget (same bar: min score, some appearances).
    over_budget: int
    below_min_score: int
    without_appearances: int
    min_score: float
    profile: Profile
    # Next lineup deadline; None when Kickbase sends none or only a past one.
    deadline: datetime | None = None
    # Positions missing for a full eleven of available squad players.
    gaps: dict[m.Position, int] = field(default_factory=dict)
    # Why bid recommendations are missing or may not be current (None: all fine).
    bid_hint: str | None = None
    # Every offer on the market with its score, also those not recommended (track record).
    scored: tuple[Recommendation, ...] = ()
    # Good offers above the budget, shown when the user asks to see them too.
    over_budget_recs: tuple[Recommendation, ...] = ()


def still_listed(market: Fetched[m.Market], now: datetime) -> list[m.MarketPlayer]:
    """Market listings that haven't ended yet – a cached (or stale) market may still contain
    offers whose time ran out since it was fetched."""
    return [
        p
        for p in market.data.players
        if (end := p.expires_at(market.fetched_at)) is None or end > now
    ]


def squad_gaps(players) -> dict[m.Position, int]:
    """Empty slots of the best formation when only players that are not out count."""
    counts = {pos: 0 for pos in LINE_ORDER}
    for p in players:
        if m.availability(p.status) is not m.Availability.OUT:
            counts[m.Position(p.position)] += 1
    best: dict[m.Position, int] | None = None
    for formation in (Formation(*f) for f in FORMATIONS):
        missing = {
            pos: formation.slots(pos) - counts[pos]
            for pos in LINE_ORDER
            if formation.slots(pos) > counts[pos]
        }
        if best is None or sum(missing.values()) < sum(best.values()):
            best = missing
    return best or {}


async def buy_view(
    client: KickbaseClient,
    sessions: async_sessionmaker,
    league: ResolvedLeague,
    basis: Basis,
    bids: BidModel | None = None,
    bid_hint: str | None = None,
    now: datetime | None = None,
) -> BuyView:
    me = await client.user_me()
    budget = await client.budget(league.id)
    market = await client.market(league.id)
    squad = await client.squad(league.id)
    basis.fetched += [budget, market, squad]
    now = now or datetime.now(UTC)
    deadline = market.data.deadline
    if deadline is not None and deadline <= now:
        deadline = None  # a deadline in the past says nothing about the next matchday
    profile = basis.config.profiles["kaufen"]
    available = budget.data.budget_after_offers

    listings = [
        p for p in still_listed(market, now) if not (p.seller and p.seller.id == me.data.user.id)
    ]
    days = basis.config.trend.days
    earlier = await market_values_around(
        sessions,
        [p.id for p in listings],
        market_day(market.fetched_at) - timedelta(days=days),
    )
    if near_market_value_update(market.fetched_at) or near_market_value_update(now):
        # API values can switch before market_day's safety boundary; never label the
        # resulting two-update difference as a daily movement.
        momentum = {p.id: Momentum(None, None) for p in listings}
        basis.notes.append(
            "Marktwert-Update läuft: Tagestrend der Angebote vorübergehend unbekannt."
        )
    else:
        momentum = await load_momentum(
            sessions, {p.id: p.market_value for p in listings}, market_day(market.fetched_at)
        )
    recommendations = []
    for listing in listings:
        player = _candidate(
            listing.id,
            f"{listing.first_name} {listing.last_name}".strip(),
            listing.team_id,
            listing.position,
            listing.status,
            listing.image,
            basis.team_names,
        )
        old = earlier.get(listing.id)
        trend = (listing.market_value - old) / old * 100 if old else None
        score = basis.score(player, trend, "kaufen")
        xp = basis.xp_for(player)
        expires_at = listing.expires_at(market.fetched_at)
        # C4.3: xP per Mio. € price. None when the price is missing/zero or no xP could be
        # computed – a 0 would silently break the sort by pushing every price-less listing
        # to the top.
        xp_per_million = (
            xp.value / (listing.price / 1_000_000) if xp is not None and listing.price else None
        )
        recommendations.append(
            Recommendation(
                player=player,
                score=score,
                market_value=listing.market_value,
                price=listing.price,
                per_million=score.total / (listing.price / 1_000_000) if listing.price else None,
                xp=xp,
                xp_per_million=xp_per_million,
                momentum=momentum.get(listing.id),
                expires_at=expires_at,
                seller=listing.seller.name if listing.seller else "Kickbase",
                seller_is_kickbase=listing.seller is None,
                bid=bids.advise(listing.market_value, listing.price) if bids else None,
                my_offer=listing.my_offer,
                before_deadline=(
                    expires_at <= deadline if expires_at is not None and deadline else None
                ),
            )
        )
    # Own bids are already deducted from budget_after_offers. Replacing one only requires
    # the additional amount, not its entire asking price a second time.
    affordable = [
        r
        for r in recommendations
        if r.price is not None and r.price <= available + (r.my_offer or 0)
    ]
    # Players without a single appearance would top "score per million" just by being cheap.
    benched = [r for r in affordable if no_appearances(basis, r.player.id)]
    candidates = [r for r in affordable if r not in benched]
    min_score = profile.min_score or 0
    good = sorted(
        (r for r in candidates if r.score.total >= min_score),
        key=lambda r: (r.per_million or 0, r.score.total),
        reverse=True,
    )
    # The same bar without the budget limit: in Kickbase one often goes into the minus and
    # makes the money back through sales, so over-budget offers can still be worth showing.
    over_budget_good = sorted(
        (
            r
            for r in recommendations
            if r.price is not None
            and r.price > available + (r.my_offer or 0)
            and r.score.total >= min_score
            and not no_appearances(basis, r.player.id)
        ),
        key=lambda r: (r.per_million or 0, r.score.total),
        reverse=True,
    )
    return BuyView(
        budget=available,
        recommendations=tuple(good),
        over_budget=len(over_budget_good),
        below_min_score=len(candidates) - len(good),
        without_appearances=len(benched),
        min_score=min_score,
        profile=profile,
        deadline=deadline,
        gaps=squad_gaps(squad.data.players),
        bid_hint=bid_hint,
        scored=tuple(recommendations),
        over_budget_recs=tuple(over_budget_good),
    )


BUY_SORTS = {
    "pro-mio": ("Score pro Mio.", lambda r: (-(r.per_million or 0), -r.score.total)),
    "score": ("Score", lambda r: (-r.score.total, r.price or 0)),
    "preis": ("Preis", lambda r: (r.price or 0, -r.score.total)),
    "ende": ("Ablauf", lambda r: (r.expires_at is None, r.expires_at or datetime.max)),
    # C4.3: xP per Mio. € – highest relative index per euro first. Tiebreaker is the raw
    # xP, then the score, so players without xP data sink to the bottom of the xP sorts
    # even when their score is great.
    "xp-pro-mio": (
        "Index pro Mio.",
        lambda r: (
            -(r.xp_per_million or 0),
            -(r.xp.value if r.xp else 0),
            -r.score.total,
        ),
    ),
}


@dataclass(frozen=True)
class BuyFilters:
    sort: str = "pro-mio"
    position: int = 0  # 0 = all
    before_deadline: bool = True
    gaps_only: bool = False
    max_price: int | None = None
    over_budget: bool = False


def select_buys(view: BuyView, filters: BuyFilters) -> tuple[list[Recommendation], int]:
    """The candidates matching the filters, sorted and limited; plus how many matched."""
    pool = list(view.recommendations)
    if filters.over_budget:
        pool += view.over_budget_recs
    chosen = [
        r
        for r in pool
        if (not filters.position or r.player.position == filters.position)
        and not (filters.before_deadline and r.before_deadline is False)
        # Without gaps the (then hidden) gaps filter would hide everything: ignore it.
        and not (filters.gaps_only and view.gaps and r.player.position not in view.gaps)
        and not (filters.max_price is not None and (r.price or 0) > filters.max_price)
    ]
    _, key = BUY_SORTS.get(filters.sort, BUY_SORTS["pro-mio"])
    chosen.sort(key=key)
    return chosen[:BUY_LIMIT], len(chosen)


def no_appearances(basis: Basis, player_id: str) -> bool:
    form = basis.form.by_player.get(player_id)
    return form is not None and form.matchdays > 0 and form.appearances == 0


# ---------------------------------------------------------------------------- sell


@dataclass(frozen=True)
class SellView:
    recommendations: tuple[Recommendation, ...]
    unflagged: int
    threshold: float
    profile: Profile
    # Every squad player with his sell score, flagged or not (track record).
    scored: tuple[Recommendation, ...] = ()
    guide: SellGuide | None = None  # only when the real budget is negative
    budget: int = 0  # real account balance
    budget_after_offers: int = 0  # if every open bid wins
    trading_held: tuple[Recommendation, ...] = ()

    @property
    def minus_if_bids_win(self) -> int | None:
        """Amount below zero if every open bid wins – when that is more than today's minus."""
        if self.budget_after_offers < min(self.budget, 0):
            return -self.budget_after_offers
        return None


async def sell_view(
    client: KickbaseClient, league: ResolvedLeague, basis: Basis, now: datetime | None = None
) -> SellView:
    squad = await client.squad(league.id)
    budget = await client.budget(league.id)
    market = await client.market(league.id)
    basis.fetched += [squad, budget, market]
    now = now or datetime.now(UTC)
    profile = basis.config.profiles["verkaufen"]
    threshold = profile.trend_threshold_percent  # required for "verkaufen" (config check)
    flagged, scored, sale, held = [], [], [], []
    for p in squad.data.players:
        player = _candidate(
            p.id, p.name, p.team_id, p.position, p.status, p.image, basis.team_names
        )
        trend = change_percent(p.market_value, p.market_value_change_7d)
        score = basis.score(player, trend, "verkaufen")
        # A player who is out or unlikely to start next game is a sell candidate; the reason
        # is the one of the start probability (injury, rehab, not in the predicted eleven …).
        start = basis.start_probability(player)
        signal = holding_signal(
            current=p.market_value,
            change_24h=daily_change(p),
            start=start,
            deadline=market.data.deadline,
            now=now,
            config=basis.config.trading,
        )
        sale.append(
            SalePlayer(
                name=p.name,
                value=p.market_value,
                average_points=p.average_points,
                start_percent=start.percent,
                position=p.position,
                # 0 % = out per Kickbase or LigaInsider: he doesn't fill a slot anyway.
                out=start.percent == 0,
                id=p.id,
            )
        )
        rec = Recommendation(
            player=player,
            score=score,
            market_value=p.market_value,
            gain=p.market_value_gain if "market_value_gain" in p.model_fields_set else None,
            reasons=signal.reasons,
            holding_role=basis.holding_roles.get(p.id, "auto"),
            start=start,
            momentum=Momentum(daily_change(p), change_percent(p.market_value, daily_change(p)))
            if daily_change(p) is not None
            else None,
        )
        scored.append(rec)
        if signal.sell:
            flagged.append(rec)
        elif signal.trading:
            held.append(rec)
    flagged.sort(key=lambda r: r.score.total)
    # "Im Minus" means the real budget: open bids only matter if they win (shown separately).
    flagged_ids = {r.player.id for r in flagged}
    g = basis.config.sell_guide
    guide = build_sell_guide(
        budget.data.budget,
        sale,
        [s for s, p in zip(sale, squad.data.players, strict=True) if p.id in flagged_ids],
        GuideWeights(g.season_weight, g.matchday_weight, g.empty_slot_points, g.max_players),
        protected_ids={pid for pid, role in basis.holding_roles.items() if role == "core"},
    )
    return SellView(
        recommendations=tuple(flagged),
        unflagged=len(squad.data.players) - len(flagged),
        threshold=threshold,
        profile=profile,
        scored=tuple(scored),
        guide=guide,
        budget=budget.data.budget,
        budget_after_offers=budget.data.budget_after_offers,
        trading_held=tuple(held),
    )


# ---------------------------------------------------------------------------- best eleven


@dataclass(frozen=True)
class ElevenLine:
    position: m.Position
    players: tuple[Recommendation, ...]
    empty_slots: int


@dataclass(frozen=True)
class ElevenView:
    formation: Formation
    lines: tuple[ElevenLine, ...]  # forwards first, goalkeeper last (pitch order)
    total: float
    # Sum of the relative lineup indices of the picked eleven. The lineup is maximised by this
    # number; `total` is the score sum kept around for the UI.
    total_xp: float
    coming_in: tuple[Recommendation, ...]
    going_out: tuple[str, ...]  # names of currently lined-up players not in the best eleven
    excluded: tuple[Recommendation, ...]  # injured / suspended
    bench: tuple[Recommendation, ...]
    # Not out, but start probability below 50 % (and not needed as a stand-in).
    risky: tuple[Recommendation, ...]
    profile: Profile
    # Players unlikely to start who fill otherwise empty slots (an empty slot costs points).
    stand_ins: tuple[Recommendation, ...] = ()

    @property
    def total_index(self) -> float:
        """Sum of the displayed 0–100 indices; the objective optimized by the lineup."""
        return self.total_xp * 100

    @property
    def total_xp_pct(self) -> float:
        """Legacy accessor for the mean lineup index (0–100), not a percentage.

        The current page and bot display the sum optimized by choose_formation instead.
        """
        n = sum(len(line.players) for line in self.lines)
        if n == 0:
            return 0.0
        return self.total_xp / n * 100


def choose_formation(
    scored: list[Recommendation], fallback: list[Recommendation] | None = None
) -> tuple[Formation, dict[m.Position, list[Recommendation]]]:
    """The allowed formation with the fewest empty slots, then the fewest stand-ins from
    `fallback` (players unlikely to start: still better than an empty slot, which costs points),
    then the highest total lineup index. C4: the lineup is picked by xP, not by the
    explainable score – we still need a numeric key, and the score is what `scored` carries."""

    def by_position(players, key):
        return {
            pos: sorted((r for r in players if r.player.position is pos), key=key, reverse=True)
            for pos in LINE_ORDER
        }

    # xP (0…1) when set; fall back to the score (0…100 → 0…1) so older callers and tests that
    # don't set `xp` still work and keep their relative ranking.
    def xp_key(r: Recommendation) -> float:
        return r.xp.value if r.xp is not None else r.score.total / 100

    regular = by_position(scored, xp_key)
    # Stand-ins: the likeliest to start first, then the best index. A zero form rank
    # does not mean the player is unavailable: filling a slot still avoids the empty-slot
    # penalty. Only an actual absence / zero start probability excludes a stand-in.
    stand_ins_all = by_position(
        fallback or [],
        lambda r: ((r.start.percent or 0) if r.start else 0, xp_key(r)),
    )
    stand_ins = {
        pos: [
            r
            for r in stand_ins_all[pos]
            if r.player.availability is not m.Availability.OUT
            and (r.start is None or r.start.percent != 0)
        ]
        for pos in LINE_ORDER
    }
    best_key, best = None, None
    for formation in (Formation(*f) for f in FORMATIONS):
        picked, used = {}, 0
        for pos in LINE_ORDER:
            slots = formation.slots(pos)
            players = regular[pos][:slots]
            extra = stand_ins[pos][: slots - len(players)]
            used += len(extra)
            picked[pos] = players + extra
        missing = sum(formation.slots(pos) - len(picked[pos]) for pos in LINE_ORDER)
        total = sum(xp_key(r) for players in picked.values() for r in players)
        key = (missing, used, -total)
        if best_key is None or key < best_key:
            best_key, best = key, (formation, picked)
    return best


async def eleven_view(client: KickbaseClient, league: ResolvedLeague, basis: Basis) -> ElevenView:
    squad = await client.squad(league.id)
    eleven = await client.my_eleven(league.id)
    basis.fetched += [squad, eleven]
    profile = basis.config.profiles["aufstellung"]
    recommendations = []
    for p in squad.data.players:
        player = _candidate(
            p.id, p.name, p.team_id, p.position, p.status, p.image, basis.team_names
        )
        rec = Recommendation(
            player=player,
            score=basis.score(
                player, change_percent(p.market_value, p.market_value_change_7d), "aufstellung"
            ),
            market_value=p.market_value,
            xp=basis.xp_for(player),
        )
        start = basis.start_probability(player)
        rec = replace(rec, start=start)
        recommendations.append(rec)

    return build_eleven(recommendations, {p.id: p.name for p in eleven.data.players}, profile)


def build_eleven(
    recommendations: list[Recommendation], current: dict[str, str], profile: Profile
) -> ElevenView:
    """Pure lineup selection, shared by the real roster and local transfer scenarios."""
    scored, excluded, risky = [], [], []
    for rec in recommendations:
        start = rec.start
        if rec.player.availability is m.Availability.OUT or (start and start.percent == 0):
            excluded.append(rec)
        elif start and start.percent is not None and start.percent < 50:
            risky.append(rec)
        else:
            scored.append(rec)

    formation, picked = choose_formation(scored, risky)
    chosen = [r for players in picked.values() for r in players]
    chosen_ids = {r.player.id for r in chosen}
    stand_ins = tuple(r for r in risky if r.player.id in chosen_ids)
    lines = tuple(
        ElevenLine(pos, tuple(picked[pos]), formation.slots(pos) - len(picked[pos]))
        for pos in reversed(LINE_ORDER)
    )

    def by_xp(rs: Iterable[Recommendation]) -> tuple[Recommendation, ...]:
        return tuple(sorted(rs, key=lambda r: r.xp.value if r.xp else 0, reverse=True))

    return ElevenView(
        formation=formation,
        lines=lines,
        total=sum(r.score.total for r in chosen),
        total_xp=sum(r.xp.value for r in chosen if r.xp is not None),
        coming_in=tuple(r for r in chosen if r.player.id not in current),
        going_out=tuple(name for pid, name in current.items() if pid not in chosen_ids),
        excluded=tuple(excluded),
        bench=by_xp(r for r in scored if r.player.id not in chosen_ids),
        risky=by_xp(r for r in risky if r.player.id not in chosen_ids),
        profile=profile,
        stand_ins=stand_ins,
    )
