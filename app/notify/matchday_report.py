"""Per-matchday Telegram notification (Block C, C3.3).

Pure: takes a `MatchdayReport` (already-built by `app.services.matchday_report`) plus
`public_url` and returns the Telegram HTML message. No Kickbase / DB I/O lives in here –
the worker job wires up the sources and decides what to do with the result.

Only the own team's data appears in the text. No other manager names, no opposition
player names, no opponent team names – the Telegram message is private and the free
LLM tier may train on inputs (AGENTS.md). The kickbase-helper dashboard already
visualises everything for those who want more.
"""

import html

from app.kickbase.models import Position
from app.services.matchday_report import LineupEntry, MatchdayReport, MissedSwap


def _player_link(player_id: str, name: str, base_url: str) -> str:
    return f'<a href="{base_url}/spieler/{player_id}">{html.escape(name)}</a>'


def _format_players(players: tuple[LineupEntry, ...], base_url: str) -> str:
    return ", ".join(f"{_player_link(p.id, p.name, base_url)} {p.points}P" for p in players)


def _format_missed_swaps(swaps: tuple[MissedSwap, ...], base_url: str) -> str:
    """Up to five swaps, ordered by impact (descending diff). Same position, but the
    cross-position guard is already enforced by the report builder."""
    ordered = sorted(swaps, key=lambda s: (-s.diff, s.bench_name))[:5]
    parts = []
    for s in ordered:
        parts.append(
            f"{_player_link(s.bench_id, s.bench_name, base_url)} → "
            f"{_player_link(s.lineup_id, s.lineup_name, base_url)} "
            f"({s.lineup_points} → +{s.diff})"
        )
    return "; ".join(parts)


def _position_label(position: int) -> str:
    return Position(position).short_label


def _placement_change(delta: int | None) -> str:
    """Short placement-change label for the header line (None on the first matchday)."""
    if delta is None:
        return " – 1. Spieltag"
    if delta > 0:
        return f" (▲{delta})"
    if delta < 0:
        return f" (▼{abs(delta)})"
    return " (±0)"


def compose_message(league_name: str, report: MatchdayReport, public_url: str) -> str:
    """One Telegram HTML message for a finished matchday.

    Sections:
    - Header with points and placement (with delta vs. previous matchday)
    - Spieltagsplatz (ranking's `mdpl`, when Kickbase has it)
    - Top 3 and Flop 3 of the own lineup (with player links)
    - Verpasste Punkte (only when at least one missed swap exists)
    """
    title = f"<b>Spieltag {report.matchday} · {html.escape(league_name)}</b>"

    header = (
        f"📊 {report.points} Punkte · Platz {report.placement}"
        f"{_placement_change(report.placement_delta)}"
    )
    if report.matchday_placement is not None:
        header += f" · Spieltagsplatz {report.matchday_placement}"

    lines: list[str] = [title, header]
    if report.top_players:
        lines.append(f"🏆 Top: {_format_players(report.top_players, public_url)}")
    if report.flop_players:
        lines.append(f"💤 Flop: {_format_players(report.flop_players, public_url)}")
    if report.missed_swaps:
        flag = " (Näherung, Bank aus Kader)" if report.bank_is_approximation else ""
        lines.append(f"❌ Verpasste Punkte: {report.missed_points}{flag}")
        lines.append(_format_missed_swaps(report.missed_swaps, public_url))
    return "\n".join(lines)
