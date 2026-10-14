import json
from datetime import UTC, date, datetime

import httpx
import pytest
from pydantic import ValidationError

from app.kickbase import models as m
from app.notify.events import (
    SENT_KEY,
    STATUS_KEY,
    MarketValueRule,
    NotificationConfig,
    collect_events,
    commit,
    compose_message,
    load_notification_config,
    market_value_events,
)
from app.notify.telegram import TelegramError, TelegramNotifier, build_notifier
from app.web.formatting import NBSP
from tests.fakes import MemoryKV, fixture

TOKEN = "123456:SECRET-token"
BASE = "https://kickbase.example.com"
RULE = MarketValueRule(min_percent=10, min_amount=1_500_000)
CONFIG = NotificationConfig(market_value=RULE, status_changes=True)
NOW = datetime(2026, 10, 7, 20, 45, tzinfo=UTC)
TODAY = date(2026, 10, 7)


def player(pid="1", name="Hansen", mv=4_000_000, change=0, status=0) -> m.SquadPlayer:
    raw = fixture("league_squad")["it"][0] | {
        "i": pid,
        "n": name,
        "mv": mv,
        "tfhmvt": change,
        "st": status,
    }
    return m.SquadPlayer.model_validate(raw)


# ---------------------------------------------------------------- Telegram API


class FakeTelegram:
    def __init__(self, response=None):
        self.requests: list[httpx.Request] = []
        self.response = response or httpx.Response(200, json={"ok": True})

    def handler(self, request):
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


async def test_send_posts_html_message():
    fake = FakeTelegram()
    notifier = TelegramNotifier(TOKEN, "42", transport=httpx.MockTransport(fake.handler))
    await notifier.send("<b>Hallo</b>")
    await notifier.aclose()
    request = fake.requests[0]
    assert request.method == "POST"
    assert request.url.path == f"/bot{TOKEN}/sendMessage"
    body = json.loads(request.content)
    assert body == {
        "chat_id": "42",
        "text": "<b>Hallo</b>",
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }


async def test_send_markdown_renders_and_escapes():
    fake = FakeTelegram()
    notifier = TelegramNotifier(TOKEN, "42", transport=httpx.MockTransport(fake.handler))
    await notifier.send_markdown("**stark** und *kursiv*, 5 < 10")
    await notifier.aclose()
    body = json.loads(fake.requests[0].content)
    assert body["parse_mode"] == "HTML"
    assert body["text"] == "<b>stark</b> und <i>kursiv</i>, 5 &lt; 10"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"ok": False, "description": "Unauthorized"}),
        httpx.Response(500, text="oops"),
        httpx.ConnectError(f"connect to https://api.telegram.org/bot{TOKEN}/sendMessage"),
    ],
)
async def test_errors_never_contain_the_token(response, caplog):
    fake = FakeTelegram(response)
    notifier = TelegramNotifier(TOKEN, "42", transport=httpx.MockTransport(fake.handler))
    with pytest.raises(TelegramError) as exc_info:
        await notifier.send("x")
    await notifier.aclose()
    assert TOKEN not in str(exc_info.value)
    assert TOKEN not in caplog.text


async def test_long_messages_are_cut_to_telegram_limit():
    fake = FakeTelegram()
    notifier = TelegramNotifier(TOKEN, "42", transport=httpx.MockTransport(fake.handler))
    await notifier.send("x" * 5000)
    await notifier.aclose()
    assert len(json.loads(fake.requests[0].content)["text"]) == 4096


def test_notifier_needs_chat_id_and_token(settings, tmp_path):
    settings.secrets_dir = tmp_path
    assert build_notifier(settings) is None  # no chat id
    settings.telegram_chat_id = "42"
    assert build_notifier(settings) is None  # no token file
    (tmp_path / "telegram_bot_token").write_text(TOKEN + "\n")
    assert isinstance(build_notifier(settings), TelegramNotifier)


# ---------------------------------------------------------------- events


@pytest.mark.parametrize(
    ("mv", "change", "expected"),
    [
        (4_000_000, 400_000, True),  # +11.1 %
        (4_000_000, -300_000, False),  # −7 %, under 1.5 Mio.
        (30_000_000, 1_600_000, True),  # only +5.6 %, but 1.6 Mio.
        (30_000_000, 1_000_000, False),
        (500_000, 0, False),
    ],
)
def test_market_value_thresholds(mv, change, expected):
    events = market_value_events([player(mv=mv, change=change)], RULE, BASE)
    assert bool(events) is expected


def test_market_value_message():
    [event] = market_value_events([player(mv=3_600_000, change=-400_000)], RULE, BASE)
    assert event.key == "mv:1:3600000:-400000"
    expected = (
        f'📉 <a href="{BASE}/spieler/1">Hansen</a>: Marktwert −10,0 % (−400 Tsd.) auf 3,60 Mio.'
    )
    assert event.text == expected.replace(" %", f"{NBSP}%").replace(" Tsd.", f"{NBSP}Tsd.").replace(
        " Mio.", f"{NBSP}Mio."
    )


def test_names_are_escaped():
    [event] = market_value_events([player(name="<b>Böse</b> & Co", change=900_000)], RULE, BASE)
    assert "&lt;b&gt;Böse&lt;/b&gt; &amp; Co" in event.text


async def test_first_run_only_records_status_baseline():
    kv = MemoryKV()
    collected = await collect_events([player(status=2)], kv, CONFIG, TODAY, BASE)
    assert collected.events == []
    await commit(kv, collected, TODAY)
    assert kv.data[STATUS_KEY] == {"1": 2}


async def test_status_changes_both_ways():
    kv = MemoryKV()
    kv.data[STATUS_KEY] = {"1": 0, "2": 1}
    squad = [player("1", "Maza", status=2), player("2", "Sano", status=0), player("3", "Neu")]
    collected = await collect_events(squad, kv, CONFIG, TODAY, BASE)
    texts = [e.text for e in collected.events]
    assert any("🚑" in t and "Maza" in t and "fit → angeschlagen" in t for t in texts)
    assert any("✅" in t and "Sano" in t and "verletzt → fit" in t for t in texts)
    assert len(collected.events) == 2  # the newcomer is no change
    disabled = NotificationConfig(market_value=RULE, status_changes=False)
    kv.data[STATUS_KEY] = {"1": 0}
    assert (await collect_events([player(status=2)], kv, disabled, TODAY, BASE)).events == []


async def test_events_are_sent_only_once_and_old_keys_expire():
    kv = MemoryKV()
    squad = [player(change=900_000)]
    first = await collect_events(squad, kv, CONFIG, TODAY, BASE)
    assert first.events
    await commit(kv, first, TODAY)
    assert (await collect_events(squad, kv, CONFIG, TODAY, BASE)).events == []
    kv.data[SENT_KEY]["old"] = "2026-09-01"
    await commit(kv, await collect_events(squad, kv, CONFIG, TODAY, BASE), TODAY)
    assert "old" not in kv.data[SENT_KEY]


async def test_same_jump_seen_evening_and_next_morning_is_one_event():
    kv = MemoryKV()
    jump = [player(mv=4_400_000, change=400_000)]
    evening = await collect_events(jump, kv, CONFIG, TODAY, BASE)
    await commit(kv, evening, TODAY)
    next_morning = await collect_events(jump, kv, CONFIG, date(2026, 10, 8), BASE)
    assert len(evening.events) == 1 and next_morning.events == []


def test_long_messages_are_cut_by_whole_lines():
    from app.notify.events import Event

    events = [Event(str(i), f'<a href="x">Spieler {i}</a> ' + "x" * 80) for i in range(100)]
    text = compose_message("Liga", events)
    assert len(text) <= 3600
    assert text.endswith("weitere")
    assert text.count("<a ") == text.count("</a>")


def test_compose_message():
    text = compose_message(
        "Testliga <11>", [type("E", (), {"text": "a"})(), type("E", (), {"text": "b"})()]
    )
    assert text == "<b>Testliga &lt;11&gt;</b>\na\nb"


def test_config_file_and_validation():
    config = load_notification_config()
    assert config.market_value.min_percent == 10
    assert config.market_value.min_amount == 1_500_000
    with pytest.raises(ValidationError):
        NotificationConfig.model_validate({"market_value": {"min_percent": 0, "min_amount": 1}})


# ---------------------------------------------------------------- job


async def test_notify_job_sends_one_message_once(tmp_path):
    from zoneinfo import ZoneInfo

    from app.kickbase.league import LeagueContext
    from app.worker.jobs import JobContext, notify_squad
    from tests.fakes import TEAM_LEAGUE_ID, fake_with_team_data, make_client

    fake = fake_with_team_data()
    squad = fixture("league_squad")
    squad["it"][0]["tfhmvt"] = 2_000_000  # big jump for the first player
    fake.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/squad", squad)
    telegram = FakeTelegram()
    kv = MemoryKV()
    async with make_client(fake) as client:
        ctx = JobContext(
            client=client,
            sessions=None,
            league=LeagueContext(client, kv, "Testliga"),
            kv=kv,
            tz=ZoneInfo("Europe/Berlin"),
            notifier=TelegramNotifier(TOKEN, "42", transport=httpx.MockTransport(telegram.handler)),
            notifications=CONFIG,
            public_url=BASE,
        )
        assert await notify_squad(ctx) == "1 Ereignisse gemeldet"
        assert await notify_squad(ctx) == "Keine neuen Ereignisse"
        await ctx.notifier.aclose()
    assert len(telegram.requests) == 1
    text = json.loads(telegram.requests[0].content)["text"]
    assert text.startswith("<b>Testliga</b>\n📈")


async def test_notify_job_without_telegram():
    from app.worker.jobs import JobContext, notify_squad

    ctx = JobContext(client=None, sessions=None, league=None, kv=MemoryKV(), tz=None)
    assert await notify_squad(ctx) == "Benachrichtigungen aus (Telegram nicht konfiguriert)"


async def test_failed_send_is_retried_including_status_changes():
    from zoneinfo import ZoneInfo

    from app.kickbase.league import LeagueContext
    from app.worker.jobs import JobContext, notify_squad
    from tests.fakes import TEAM_LEAGUE_ID, fake_with_team_data, make_client

    fake = fake_with_team_data()
    squad = fixture("league_squad")
    kv = MemoryKV()
    kv.data[STATUS_KEY] = {p["i"]: 0 for p in squad["it"]}  # everyone was fit
    squad["it"][0]["st"] = 1  # now injured
    fake.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/squad", squad)
    telegram = FakeTelegram(httpx.Response(502))
    async with make_client(fake, refresh_min_age=0) as client:
        ctx = JobContext(
            client=client,
            sessions=None,
            league=LeagueContext(client, kv, "Testliga"),
            kv=kv,
            tz=ZoneInfo("Europe/Berlin"),
            notifier=TelegramNotifier(TOKEN, "42", transport=httpx.MockTransport(telegram.handler)),
            notifications=CONFIG,
            public_url=BASE,
        )
        with pytest.raises(TelegramError):
            await notify_squad(ctx)
        telegram.response = httpx.Response(200, json={"ok": True})
        assert await notify_squad(ctx) != "Keine neuen Ereignisse"
        await ctx.notifier.aclose()
    text = json.loads(telegram.requests[-1].content)["text"]
    assert "verletzt" in text


async def test_get_updates_rejects_unexpected_bodies():
    for body in ([1, 2], {"result": "x"}):
        fake = FakeTelegram(httpx.Response(200, json=body))
        notifier = TelegramNotifier(TOKEN, "42", transport=httpx.MockTransport(fake.handler))
        with pytest.raises(TelegramError):
            await notifier.get_updates()
        await notifier.aclose()
    fake = FakeTelegram(httpx.Response(200, json={"result": [{"update_id": 1}, "kaputt"]}))
    notifier = TelegramNotifier(TOKEN, "42", transport=httpx.MockTransport(fake.handler))
    assert await notifier.get_updates() == [{"update_id": 1}]
    await notifier.aclose()


async def test_send_markdown_nests_bold_italic():
    fake = FakeTelegram()
    notifier = TelegramNotifier(TOKEN, "42", transport=httpx.MockTransport(fake.handler))
    await notifier.send_markdown("***Kane*** & **Musiala** <x>")
    await notifier.aclose()
    text = json.loads(fake.requests[0].content)["text"]
    assert text == "<b><i>Kane</i></b> &amp; <b>Musiala</b> &lt;x&gt;"
