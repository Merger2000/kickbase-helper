import asyncio
from zoneinfo import ZoneInfo

from app.bot import polling
from app.kickbase.league import ResolvedLeague
from tests.fakes import MemoryKV

LEAGUE = ResolvedLeague("1000002", "Testliga", "1")


class FakeNotifier:
    chat_id = "42"

    def __init__(self, rounds):
        self.rounds = list(rounds)  # each: list of updates, or an exception to raise
        self.sent: list[str] = []
        self.offsets: list[int | None] = []

    async def get_updates(self, offset=None):
        self.offsets.append(offset)
        item = self.rounds.pop(0) if self.rounds else []
        if isinstance(item, Exception):
            raise item
        return item

    async def send_markdown(self, text):
        self.sent.append(text)

    async def send_plain(self, text):
        self.sent.append(text)

    async def send_typing(self):
        pass


class FakeLlm:
    def __init__(self):
        self.calls = 0
        self.prompts: list[str] = []

    async def complete_text(self, task, system, user):
        self.calls += 1
        self.prompts.append(user)
        return type("C", (), {"data": "Alles gut."})()


class League:
    async def get(self):
        return LEAGUE


class Ctx:
    def __init__(self, notifier):
        self.notifier = notifier
        self.llm = FakeLlm()
        self.kv = MemoryKV()
        self.tz = ZoneInfo("Europe/Berlin")
        self.league = League()
        self.client = self.sessions = self.oldb = None
        self.claude = None


def message(update_id, chat_id, text):
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


async def test_poll_once_answers_only_the_own_chat_and_advances_the_offset():
    notifier = FakeNotifier(
        [[message(5, 42, "Hallo?"), message(6, 99, "Fremder"), {"update_id": 7}]]
    )
    ctx = Ctx(notifier)
    offset = await polling._poll_once(ctx, ctx.llm, 30, None)
    assert offset == 8
    assert notifier.sent == ["Alles gut."]
    assert ctx.llm.calls == 1


async def test_daily_question_limit():
    notifier = FakeNotifier([[message(1, 42, "a"), message(2, 42, "b")]])
    ctx = Ctx(notifier)
    await polling._poll_once(ctx, ctx.llm, 1, None)
    assert notifier.sent == ["Alles gut.", "Das Fragen-Kontingent für heute ist aufgebraucht."]


async def test_polling_survives_errors_and_stops_on_request(monkeypatch):
    monkeypatch.setattr(polling, "POLL_ERROR_SLEEP", 0)
    stop = asyncio.Event()
    notifier = FakeNotifier([AttributeError("unerwartet"), [message(1, 42, "Frage")]])
    ctx = Ctx(notifier)
    original = polling._poll_once

    async def once_then_stop(*args):
        result = await original(*args)
        stop.set()
        return result

    monkeypatch.setattr(polling, "_poll_once", once_then_stop)
    await asyncio.wait_for(polling.poll_questions(ctx, stop), timeout=2)
    assert notifier.sent == ["Alles gut."]  # the error didn't end the bot


async def test_failure_in_one_update_neither_stops_the_batch_nor_replays_it():
    class BrokenOnce(FakeNotifier):
        failed = False

        async def send_markdown(self, text):
            if not self.failed:
                self.failed = True
                raise AttributeError("kaputte Antwort")
            self.sent.append(text)

    notifier = BrokenOnce([[message(1, 42, "a"), message(2, 42, "b")]])
    ctx = Ctx(notifier)
    offset = await polling._poll_once(ctx, ctx.llm, 30, None)
    assert offset == 3  # both updates are past: nothing is answered twice
    assert notifier.sent == ["Alles gut."]  # the second question was still answered
    assert ctx.llm.calls == 2


async def test_follow_up_questions_see_the_recent_chat():
    notifier = FakeNotifier(
        [[message(1, 42, "Soll ich Hansen verkaufen?")], [message(2, 42, "Und Maza?")]]
    )
    ctx = Ctx(notifier)
    offset = await polling._poll_once(ctx, ctx.llm, 30, None)
    await polling._poll_once(ctx, ctx.llm, 30, offset)
    assert "BISHERIGER CHAT" not in ctx.llm.prompts[0]
    assert "Nutzer: Soll ich Hansen verkaufen?\nDu: Alles gut." in ctx.llm.prompts[1]


async def test_old_or_broken_history_is_ignored():
    from datetime import UTC, datetime, timedelta

    ctx = Ctx(FakeNotifier([]))
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    await ctx.kv.set(polling.HISTORY_KEY, [{"at": old, "q": "alt", "a": "alt"}, {"kaputt": 1}, "x"])
    assert await polling._recent_history(ctx, datetime.now(UTC)) == []


async def test_history_drops_the_unverified_note():
    class Llm(FakeLlm):
        async def complete_text(self, task, system, user):
            self.calls += 1
            self.prompts.append(user)
            return type("C", (), {"data": "Kostet 999 Mio."})()

    notifier = FakeNotifier([[message(1, 42, "Was kostet X?")]])
    ctx = Ctx(notifier)
    ctx.llm = Llm()
    await polling._poll_once(ctx, ctx.llm, 30, None)
    assert "Ohne Gewähr" in notifier.sent[0]
    [entry] = await ctx.kv.get(polling.HISTORY_KEY)
    assert entry["a"] == "Kostet 999 Mio."


class FakeClaude:
    def __init__(self, fail=None):
        self.fail = fail
        self.prompts: list[str] = []

    async def chat(self, prompt, **kw):
        from app.llm.claude_bridge import ClaudeReply

        self.prompts.append(prompt)
        if self.fail:
            raise self.fail
        return ClaudeReply("Claude sagt Hallo.", "sess-1", "claude-sonnet-5-5", 0.01)


async def test_claude_window_takes_over_until_stopped(monkeypatch):
    from app.bot import claude_mode

    async def no_situation(ctx, now):
        return None

    monkeypatch.setattr(claude_mode, "_situation", no_situation)
    notifier = FakeNotifier(
        [
            [
                message(1, 42, "@claude Hallo"),
                message(2, 42, "Und jetzt?"),
                message(3, 42, "/claude_aus"),
            ],
            [message(4, 42, "Normale Frage")],
        ]
    )
    ctx = Ctx(notifier)
    ctx.claude = FakeClaude()
    offset = await polling._poll_once(ctx, ctx.llm, 30, None)
    await polling._poll_once(ctx, ctx.llm, 30, offset)
    assert len(ctx.claude.prompts) == 2
    assert notifier.sent[0].startswith("Claude sagt Hallo.")
    assert notifier.sent[2] == "Claude-Modus beendet."
    assert notifier.sent[3] == "Alles gut."
    assert ctx.llm.calls == 1


async def test_claude_failure_falls_back_to_the_normal_ai():
    from app.llm.claude_bridge import ClaudeBridgeError

    notifier = FakeNotifier(
        [[message(1, 42, "/claude_model haiku"), message(2, 42, "@claude Frage")]]
    )
    ctx = Ctx(notifier)
    ctx.claude = FakeClaude(fail=ClaudeBridgeError("Bridge nicht erreichbar (ConnectError)"))
    await polling._poll_once(ctx, ctx.llm, 30, None)
    assert notifier.sent[0] == "Claude-Modell ist jetzt **haiku**."
    assert "die normale KI antwortet" in notifier.sent[1]
    assert notifier.sent[2] == "Alles gut."
    assert ctx.llm.prompts and "Frage" in ctx.llm.prompts[0]


async def test_without_normal_ai_only_claude_answers():
    notifier = FakeNotifier([[message(1, 42, "Normale Frage")]])
    ctx = Ctx(notifier)
    await polling._poll_once(ctx, None, 30, None)
    assert notifier.sent == ["Die normale KI ist nicht eingerichtet – frag mit @claude …"]
