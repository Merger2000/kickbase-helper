"""Claude im Telegram-Chat: Fenster, Befehle, Kontingent, Fallback."""

import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.bot import claude_mode
from app.llm.claude_bridge import ClaudeBridgeError, ClaudeReply
from tests.fakes import MemoryKV

NOW = datetime(2026, 10, 13, 18, 0, tzinfo=UTC)  # 20:00 in Berlin
SESSION = "0b6f1c1e-3f9e-4a39-9b0c-6a8f8f2f1a11"


class FakeClaude:
    def __init__(self, *, fail: Exception | None = None, text: str = "Kane spielt."):
        self.fail = fail
        self.text = text
        self.calls: list[dict] = []

    async def chat(self, prompt, **kw):
        self.calls.append({"prompt": prompt, **kw})
        if self.fail:
            raise self.fail
        return ClaudeReply(self.text, SESSION, "claude-sonnet-5-5", 0.05)


class FakeNotifier:
    def __init__(self):
        self.typing = 0

    async def send_typing(self):
        self.typing += 1


class Ctx:
    def __init__(self, claude=None):
        self.claude = claude
        self.notifier = FakeNotifier()
        self.kv = MemoryKV()
        self.tz = ZoneInfo("Europe/Berlin")


@pytest.fixture(autouse=True)
def no_situation(monkeypatch):
    async def fake(ctx, now):
        return "Kontostand 3,2 Mio"

    monkeypatch.setattr(claude_mode, "_situation", fake)


def handle(ctx, text, now=NOW):
    return asyncio.run(claude_mode.maybe_handle(ctx, text, now))


def test_ignores_normal_messages():
    assert handle(Ctx(FakeClaude()), "Wer spielt am Samstag?") is None


@pytest.mark.parametrize("text", ["@claude", "/claude", "@Claude:", "/claude@KickBot"])
def test_start_opens_the_window(text):
    ctx = Ctx(FakeClaude())
    result = handle(ctx, text)
    assert "hört bis 20:10 mit" in result.replies[0]
    assert ctx.claude.calls == []
    # Follow-up messages inside the window go to Claude.
    assert handle(ctx, "Soll ich Kane verkaufen?", NOW + timedelta(minutes=5)) is not None
    assert len(ctx.claude.calls) == 1


def test_claudette_is_not_a_command():
    assert handle(Ctx(FakeClaude()), "/claudette hallo") is None


def test_question_with_start_asks_claude():
    ctx = Ctx(FakeClaude())
    result = handle(ctx, "@claude wie steht mein Team?")
    assert result.fallback_question is None
    assert result.replies[0].startswith("Kane spielt.")
    assert "claude-sonnet-5-5 · aktiv bis 20:10" in result.replies[-1]
    call = ctx.claude.calls[0]
    assert call["model"] == "sonnet" and call["session_id"] is None
    assert call["system"] == claude_mode.SYSTEM
    assert "[Jetzt: Dienstag, 13.10.2026 20:00 (Europe/Berlin)]" in call["prompt"]
    assert "LAGEBILD" in call["prompt"] and "Frage: wie steht mein Team?" in call["prompt"]


def test_follow_up_resumes_the_session_without_lagebild():
    ctx = Ctx(FakeClaude())
    handle(ctx, "@claude erste Frage")
    handle(ctx, "zweite Frage", NOW + timedelta(minutes=9))
    second = ctx.claude.calls[1]
    assert second["session_id"] == SESSION
    assert "LAGEBILD" not in second["prompt"]
    # Each answer extends the window.
    assert handle(ctx, "dritte", NOW + timedelta(minutes=18)) is not None


def test_window_expires():
    ctx = Ctx(FakeClaude())
    handle(ctx, "@claude")
    assert handle(ctx, "noch da?", NOW + timedelta(minutes=11)) is None


def test_stop_command():
    ctx = Ctx(FakeClaude())
    assert handle(ctx, "/claude_aus").replies == ["Claude-Modus war nicht aktiv."]
    handle(ctx, "@claude")
    assert handle(ctx, "/claude-aus").replies == ["Claude-Modus beendet."]
    assert handle(ctx, "noch da?") is None


def test_model_command():
    ctx = Ctx(FakeClaude())
    assert "Claude-Modell: **sonnet**" in handle(ctx, "/claude_model").replies[0]
    assert "Unbekanntes Modell" in handle(ctx, "/claude_model gpt-5").replies[0]
    assert handle(ctx, "/claude-model Opus").replies == ["Claude-Modell ist jetzt **opus**."]
    handle(ctx, "@claude Frage")
    assert ctx.claude.calls[0]["model"] == "opus"
    assert "**claude-opus-5-5**" in handle(ctx, "/claude_model claude-opus-5-5").replies[0]


def test_not_configured():
    result = handle(Ctx(claude=None), "@claude Hallo")
    assert "nicht eingerichtet" in result.replies[0]
    # Commands still work without the bridge.
    assert handle(Ctx(claude=None), "/claude_model") is not None


def test_bridge_error_falls_back_to_normal_ai():
    ctx = Ctx(FakeClaude(fail=ClaudeBridgeError("Zeitüberschreitung")))
    result = handle(ctx, "@claude Frage")
    assert result.fallback_question == "Frage"
    assert "Zeitüberschreitung" in result.replies[0]
    # The window stays open, but the question is not counted.
    assert asyncio.run(ctx.kv.get(f"{claude_mode.QUESTIONS_KEY}.2026-10-13")) is None


def test_daily_limit(monkeypatch):
    ctx = Ctx(FakeClaude())
    key = f"{claude_mode.QUESTIONS_KEY}.2026-10-13"
    asyncio.run(ctx.kv.set(key, 40))
    result = handle(ctx, "@claude Frage")
    assert result.fallback_question == "Frage" and "Kontingent" in result.replies[0]
    assert ctx.claude.calls == []


def test_counts_questions():
    ctx = Ctx(FakeClaude())
    handle(ctx, "@claude eins")
    handle(ctx, "zwei")
    assert asyncio.run(ctx.kv.get(f"{claude_mode.QUESTIONS_KEY}.2026-10-13")) == 2


def test_typing_while_waiting(monkeypatch):
    class Slow(FakeClaude):
        async def chat(self, prompt, **kw):
            await asyncio.sleep(0.05)
            return await super().chat(prompt, **kw)

    monkeypatch.setattr(claude_mode, "TYPING_EVERY", 0.01)
    ctx = Ctx(Slow())
    handle(ctx, "@claude Frage")
    assert ctx.notifier.typing >= 2


def test_long_answers_are_split():
    ctx = Ctx(FakeClaude(text="\n\n".join(["x" * 2000] * 3)))
    replies = handle(ctx, "@claude lang").replies
    assert len(replies) == 3
    assert all(len(r) <= claude_mode.CHUNK for r in replies)


def test_split_message():
    assert claude_mode.split_message("kurz") == ["kurz"]
    assert claude_mode.split_message("") == []
    words = claude_mode.split_message("aaaa bbbb cccc", limit=9)
    assert words == ["aaaa bbbb", "cccc"]
    assert claude_mode.split_message("x" * 25, limit=10) == ["x" * 10, "x" * 10, "x" * 5]


def test_corrupt_state_is_ignored():
    ctx = Ctx(FakeClaude())
    asyncio.run(ctx.kv.set(claude_mode.STATE_KEY, {"until": "kaputt"}))
    assert handle(ctx, "Hallo") is None


def test_old_window_without_bridge_goes_back_to_normal_ai():
    ctx = Ctx(FakeClaude())
    handle(ctx, "@claude")
    ctx.claude = None  # bridge switched off while the window was open
    assert handle(ctx, "Wer spielt?", NOW + timedelta(minutes=1)) is None
    assert asyncio.run(ctx.kv.get(claude_mode.STATE_KEY)) is None


def test_window_counts_from_the_answer(monkeypatch):
    ticks = [0.0]

    def clock():  # every answer takes Claude five minutes
        ticks[0] += 300.0
        return ticks[0]

    monkeypatch.setattr(claude_mode, "_clock", clock)
    ctx = Ctx(FakeClaude())
    result = handle(ctx, "@claude lange Recherche")
    assert "aktiv bis 20:15" in result.replies[-1]
    assert handle(ctx, "noch was", NOW + timedelta(minutes=14)) is not None
