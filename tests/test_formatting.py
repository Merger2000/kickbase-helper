from datetime import UTC, datetime

import pytest

from app.web.formatting import MINUS, NBSP, local_time, money_full, money_short, percent


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (17012409, f"17,01{NBSP}Mio."),
        (154370651, f"154,4{NBSP}Mio."),
        (535178, f"535{NBSP}Tsd."),
        (500, f"500{NBSP}€"),
        (-535178, f"{MINUS}535{NBSP}Tsd."),
        (0, f"0{NBSP}€"),
        (999, f"999{NBSP}€"),
        (999_500, f"1,00{NBSP}Mio."),
        (999_999, f"1,00{NBSP}Mio."),
        (999_499, f"999{NBSP}Tsd."),
        (99_995_000, f"100,0{NBSP}Mio."),
        (99_994_000, f"99,99{NBSP}Mio."),
    ],
)
def test_money_short(value, expected):
    assert money_short(value) == expected


def test_signed_money_and_full():
    assert money_short(28500, signed=True) == f"+28{NBSP}Tsd."
    assert money_full(17012409) == f"17.012.409{NBSP}€"
    assert money_full(-1500) == f"{MINUS}1.500{NBSP}€"


def test_percent():
    assert percent(-12.63) == f"{MINUS}12,6{NBSP}%"
    assert percent(2.06) == f"+2,1{NBSP}%"
    assert percent(-0.04) == f"0,0{NBSP}%"
    assert percent(None) == ""
    assert money_short(-400, signed=True) == f"{MINUS}400{NBSP}€"
    assert money_short(0, signed=True) == f"0{NBSP}€"


def test_local_time_uses_local_date_across_midnight():
    now = datetime(2026, 10, 6, 22, 30, tzinfo=UTC)  # 00:30 on 7.10. in Berlin
    assert (
        local_time(datetime(2026, 10, 6, 21, 0, tzinfo=UTC), "Europe/Berlin", now) == "6.10. 23:00"
    )
    assert local_time(datetime(2026, 10, 6, 22, 15, tzinfo=UTC), "Europe/Berlin", now) == "00:15"


def test_local_time_today_and_other_day():
    now = datetime(2026, 10, 6, 20, 0, tzinfo=UTC)
    assert local_time(datetime(2026, 10, 6, 19, 35, tzinfo=UTC), "Europe/Berlin", now) == "21:35"
    assert (
        local_time(datetime(2026, 10, 4, 8, 5, tzinfo=UTC), "Europe/Berlin", now) == "4.10. 10:05"
    )


def test_markdown_renders_bold_italic_and_breaks():
    from app.web.formatting import markdown

    assert str(markdown("**stark** und *kursiv*")) == "<strong>stark</strong> und <em>kursiv</em>"
    assert str(markdown("Zeile 1\nZeile 2")) == "Zeile 1<br>Zeile 2"


def test_markdown_escapes_html():
    from app.web.formatting import markdown

    assert str(markdown("<script>alert(1)</script>")) == "&lt;script&gt;alert(1)&lt;/script&gt;"
