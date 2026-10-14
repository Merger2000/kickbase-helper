"""German number and date formatting for templates."""

import html
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from markupsafe import Markup

MINUS = chr(0x2212)  # typographic minus sign
NBSP = chr(0x00A0)  # keeps number and unit on one line


def _decimal(value: float, digits: int) -> str:
    """German grouping: 1234567.891 -> "1.234.567,89" """
    text = f"{abs(value):,.{digits}f}"
    return text.replace(",", "_").replace(".", ",").replace("_", ".")


def _sign(value: float, signed: bool, digits: int = 0) -> str:
    # Decide on the rounded value, so -0.04 % does not get a minus sign ("0,0 %").
    rounded = round(value, digits)
    if rounded < 0:
        return MINUS
    return "+" if signed and rounded > 0 else ""


def money_full(value: int) -> str:
    """17012409 -> "17.012.409 €" """
    return f"{_sign(value, False)}{_decimal(value, 0)}{NBSP}€"


def money_short(value: int, *, signed: bool = False) -> str:
    """3722776 -> "3,72 Mio." / 535178 -> "535 Tsd." (with signed=True: "+535 Tsd.")"""
    amount = abs(value)
    # Pick the unit on the rounded value: 999.500 is "1,00 Mio.", not "1.000 Tsd.".
    if round(amount / 1_000) >= 1_000:
        millions = amount / 1_000_000
        digits = 1 if round(millions, 2) >= 100 else 2
        number, unit = round(millions, digits), "Mio."
    elif round(amount) >= 1_000:
        digits, number, unit = 0, round(amount / 1_000), "Tsd."
    else:
        digits, number, unit = 0, round(amount), "€"
    sign = _sign(number if value >= 0 else -number, signed, digits)
    return f"{sign}{_decimal(number, digits)}{NBSP}{unit}"


def signed_money(value: int) -> str:
    return money_short(value, signed=True)


def percent(value: float | None, *, signed: bool = True) -> str:
    if value is None:
        return ""
    return f"{_sign(value, signed, 1)}{_decimal(value, 1)}{NBSP}%"


def local_time(value: datetime, tz: str, now: datetime | None = None) -> str:
    """Time in the configured zone; with day and month if it is not today."""
    zone = ZoneInfo(tz)
    local = value.astimezone(zone)
    today = (now or datetime.now(zone)).astimezone(zone).date()
    if local.date() == today:
        return local.strftime("%H:%M")
    return f"{local.day}.{local.month}. {local:%H:%M}"


def decimal(value: float, digits: int = 2) -> str:
    return f"{MINUS if round(value, digits) < 0 else ''}{_decimal(value, digits)}"


def signed_points(value: float) -> str:
    """Score contribution: "+32", "−30", "0"."""
    rounded = round(value)
    if rounded == 0:
        return "0"
    return f"{'+' if rounded > 0 else MINUS}{abs(rounded)}"


def emphasis(escaped: str, bold: str, italic: str) -> str:
    """***bold italic***, **bold**, *italic* on already escaped text, properly nested."""
    escaped = re.sub(r"\*\*\*(.+?)\*\*\*", rf"<{bold}><{italic}>\1</{italic}></{bold}>", escaped)
    escaped = re.sub(r"\*\*(.+?)\*\*", rf"<{bold}>\1</{bold}>", escaped)
    return re.sub(r"\*(.+?)\*", rf"<{italic}>\1</{italic}>", escaped)


def markdown(text: str) -> Markup:
    """Minimal, safe Markdown for short AI texts: **bold**, *italic* and line breaks. The text
    is HTML-escaped first, so no raw HTML can pass through."""
    escaped = emphasis(html.escape(text), "strong", "em")
    return Markup(re.sub(r"\n+", "<br>", escaped))


FILTERS = {
    "decimal": decimal,
    "signed_points": signed_points,
    "money_full": money_full,
    "money_short": money_short,
    "signed_money": signed_money,
    "percent": percent,
    "local_time": local_time,
    "markdown": markdown,
}
