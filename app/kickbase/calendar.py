"""When Kickbase market values change and which day they belong to."""

from datetime import UTC, date, datetime, timedelta

# Kickbase publishes new market values daily at about 20:00 UTC (`mvud` in the market) and dates
# them by that UTC day; until the next update this value is the current one.
MARKET_VALUE_UPDATE_UTC = timedelta(hours=20)
UPDATE_SAFETY = timedelta(minutes=15)


def market_day(now: datetime) -> date:
    """The Kickbase date of the market value that is current at `now`: the value fetched at
    07:00 belongs to the previous day, the one fetched at 21:00 UTC to the same day."""
    return (now.astimezone(UTC) - MARKET_VALUE_UPDATE_UTC - UPDATE_SAFETY).date()


def near_market_value_update(now: datetime) -> bool:
    """True from 10 minutes before to 30 minutes after the daily update (values in flux)."""
    utc = now.astimezone(UTC)
    since_midnight = timedelta(hours=utc.hour, minutes=utc.minute)
    start = MARKET_VALUE_UPDATE_UTC - timedelta(minutes=10)
    return start <= since_midnight <= MARKET_VALUE_UPDATE_UTC + timedelta(minutes=30)
