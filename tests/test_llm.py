import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from pydantic import BaseModel
from sqlalchemy import select

from app.db.models import AiUsage, Base
from app.db.session import make_engine, make_sessionmaker
from app.llm.client import LlmClient, strict_schema
from app.llm.config import AiConfig, load_ai_config
from app.llm.errors import (
    LlmBadOutput,
    LlmLimitReached,
    LlmQuotaExhausted,
    LlmRejected,
    LlmTruncated,
    LlmUnavailable,
)
from app.llm.factory import build_llm
from app.llm.usage import UsageGuard, estimate_tokens
from tests.fakes import FakeClock

KEY = "test-key"
TZ = ZoneInfo("Europe/Berlin")


class Answer(BaseModel):
    text: str
    count: int | None


@pytest.fixture(params=["sqlite", "postgres"])
async def sessions(request, tmp_path):
    if request.param == "sqlite":
        url = f"sqlite+pysqlite:///{tmp_path / 'llm.db'}"
    else:
        url = os.environ.get("TEST_POSTGRES_URL")
        if not url:
            pytest.skip("TEST_POSTGRES_URL not set")
    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield make_sessionmaker(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


def config(**limits) -> AiConfig:
    data = load_ai_config().model_dump()
    data["limits"] |= {"min_interval_seconds": 0} | limits
    return AiConfig.model_validate(data)


class FakeProvider:
    """Answers chat completions from a queue of (status, body) per model."""

    def __init__(self) -> None:
        self.answers: dict[str, list[tuple[int, dict]]] = {}
        self.requests: list[dict] = []

    def answer(self, model: str, status: int = 200, body: dict | None = None, **kwargs) -> None:
        self.answers.setdefault(model, []).append((status, body or ok_body(**kwargs)))

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == f"Bearer {KEY}"
        payload = json.loads(request.content)
        self.requests.append(payload)
        status, body = self.answers[payload["model"]].pop(0)
        if isinstance(body, bytes):
            return httpx.Response(status, content=body)
        return httpx.Response(status, json=body)


def ok_body(content: str = '{"text": "hallo", "count": 3}', finish: str = "stop") -> dict:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30},
    }


@pytest.fixture
def provider():
    return FakeProvider()


@pytest.fixture
def clock():
    return FakeClock()


def make_llm(sessions, provider, clock, models=("small", "tiny"), **limits) -> LlmClient:
    guard = UsageGuard(sessions, config(**limits), TZ, clock)
    return LlmClient(
        "https://llm.test/v1",
        KEY,
        models,
        guard,
        transport=httpx.MockTransport(provider.handler),
    )


async def usage_rows(sessions) -> list[AiUsage]:
    async with sessions() as session:
        return list((await session.execute(select(AiUsage).order_by(AiUsage.id))).scalars())


async def test_concurrent_clients_cannot_spend_the_last_call_twice(sessions, provider, clock):
    provider.answer("small")
    provider.answer("small")
    clients = [make_llm(sessions, provider, clock, calls_per_day=1) for _ in range(2)]
    try:
        results = await asyncio.gather(
            *(llm.complete("news", "system", "user", "answer", Answer) for llm in clients),
            return_exceptions=True,
        )
        assert sum(isinstance(r, LlmLimitReached) for r in results) == 1
        assert len(provider.requests) == 1
        assert len(await usage_rows(sessions)) == 1
    finally:
        for llm in clients:
            await llm.aclose()


async def test_independent_postgres_engines_share_the_usage_lock(sessions, provider, clock):
    url = sessions.kw["bind"].url
    if url.get_backend_name() != "postgresql":
        pytest.skip("Cross-process lock requires PostgreSQL")
    engine = make_engine(str(url.render_as_string(hide_password=False)))
    other_sessions = make_sessionmaker(engine)
    provider.answer("small")
    provider.answer("small")
    first = make_llm(sessions, provider, clock, calls_per_day=1)
    second = make_llm(other_sessions, provider, clock, calls_per_day=1)
    assert first.guard._lock is not second.guard._lock
    try:
        results = await asyncio.gather(
            first.complete("news", "system", "user", "answer", Answer),
            second.complete("news", "system", "user", "answer", Answer),
            return_exceptions=True,
        )
        assert sum(isinstance(r, LlmLimitReached) for r in results) == 1
        assert len(provider.requests) == 1
        assert len(await usage_rows(sessions)) == 1
    finally:
        await first.aclose()
        await second.aclose()
        await engine.dispose()


@pytest.mark.parametrize(
    "usage",
    [
        {"total_tokens": 150},
        {"prompt_tokens": 120},
        {"completion_tokens": -3},
        {"prompt_tokens": True, "completion_tokens": "30"},
        [120, 30],
    ],
)
async def test_partial_or_invalid_usage_books_missing_counters(sessions, provider, clock, usage):
    body = ok_body()
    body["usage"] = usage
    provider.answer("small", body=body)
    llm = make_llm(sessions, provider, clock)
    try:
        result = await llm.complete("news", "system", "user", "answer", Answer)
        assert result.prompt_tokens == (
            120
            if isinstance(usage, dict) and usage.get("prompt_tokens") == 120
            else estimate_tokens("system", "user")
        )
        assert result.completion_tokens == llm.guard.config.task("news").max_tokens
    finally:
        await llm.aclose()


async def test_structured_answer_is_validated_and_booked(sessions, provider, clock):
    provider.answer("small")
    llm = make_llm(sessions, provider, clock)
    result = await llm.complete("news", "system", "user", "answer", Answer)
    assert result.data == Answer(text="hallo", count=3)
    assert result.model == "small"
    request = provider.requests[0]
    assert request["temperature"] == 0
    assert request["max_tokens"] == load_ai_config().task("news").max_tokens
    schema = request["response_format"]["json_schema"]
    assert schema["strict"] is True
    assert schema["schema"]["required"] == ["text", "count"]
    assert schema["schema"]["additionalProperties"] is False
    [row] = await usage_rows(sessions)
    assert (row.task, row.model, row.status) == ("news", "small", "ok")
    assert (row.prompt_tokens, row.completion_tokens) == (120, 30)
    # Unknown model: the deliberately high default price.
    assert row.cost_usd == pytest.approx((120 * 1.0 + 30 * 3.0) / 1_000_000)
    await llm.aclose()


async def test_rate_limit_falls_back_to_second_model(sessions, provider, clock):
    provider.answer("small", 429, {"message": "Rate limit exceeded"})
    provider.answer("tiny")
    llm = make_llm(sessions, provider, clock)
    result = await llm.complete("news", "s", "u", "answer", Answer)
    assert result.model == "tiny"
    assert [r.status for r in await usage_rows(sessions)] == ["rate_limited", "ok"]
    await llm.aclose()


async def test_invalid_body_and_missing_usage_book_the_worst_case(sessions, provider, clock):
    max_tokens = load_ai_config().task("news").max_tokens
    provider.answer("small", 200, b"<html>proxy error</html>")
    body = ok_body()
    del body["usage"]
    provider.answer("tiny", 200, body)
    llm = make_llm(sessions, provider, clock)
    result = await llm.complete("news", "s" * 300, "u" * 300, "answer", Answer)
    assert result.model == "tiny"
    error, ok = await usage_rows(sessions)
    assert error.status == "error"  # a proxy page is temporary, not bad model output
    assert (error.completion_tokens, ok.completion_tokens) == (max_tokens, max_tokens)
    assert ok.prompt_tokens == estimate_tokens("s" * 300, "u" * 300)
    await llm.aclose()


async def test_not_a_completion_is_temporary(sessions, provider, clock):
    provider.answer("small", 200, {"error": {"message": "upstream overloaded"}})
    llm = make_llm(sessions, provider, clock, models=("small",))
    with pytest.raises(LlmUnavailable, match="upstream overloaded"):
        await llm.complete("news", "s", "u", "answer", Answer)
    await llm.aclose()


async def test_temporary_error_wins_over_bad_output(sessions, provider, clock):
    # The main model was only unavailable: the input must stay pending, not be given up.
    provider.answer("small", 503, {"message": "busy"})
    provider.answer("tiny", content='{"text": "abgesch', finish="length")
    llm = make_llm(sessions, provider, clock)
    with pytest.raises(LlmUnavailable):
        await llm.complete("news", "s", "u", "answer", Answer)
    await llm.aclose()


async def test_rate_limit_on_every_model(sessions, provider, clock):
    provider.answer("small", 429, {"message": "Rate limit exceeded"})
    provider.answer("tiny", 429, {"message": "Rate limit exceeded"})
    llm = make_llm(sessions, provider, clock)
    with pytest.raises(LlmUnavailable, match="Rate-Limit"):
        await llm.complete("news", "s", "u", "answer", Answer)
    await llm.aclose()


@pytest.mark.parametrize(
    ("content", "finish", "message"),
    [
        ("kein json", "stop", "Schema"),
        ('{"text": 5}', "stop", "Schema"),
        ('{"text": "abgeschn', "length", "abgeschnitten"),
    ],
)
async def test_unusable_answer_tries_next_model(
    sessions, provider, clock, content, finish, message
):
    provider.answer("small", content=content, finish=finish)
    provider.answer("tiny", content=content, finish=finish)
    llm = make_llm(sessions, provider, clock)
    with pytest.raises(LlmBadOutput, match=message) as info:
        await llm.complete("news", "s", "u", "answer", Answer)
    assert isinstance(info.value, LlmTruncated) == (finish == "length")
    rows = await usage_rows(sessions)
    assert [(r.status, r.prompt_tokens, r.completion_tokens) for r in rows] == [
        ("bad_output", 120, 30),
        ("bad_output", 120, 30),
    ]
    await llm.aclose()


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (402, {"message": "Payment required"}, LlmQuotaExhausted),
        (429, {"message": "Monthly credit exhausted"}, LlmQuotaExhausted),
        (429, {"message": "Too many", "code": "insufficient_quota"}, LlmQuotaExhausted),
        (429, {"error": {"message": "x", "type": "billing_hard_limit"}}, LlmQuotaExhausted),
        (401, {"message": "Unauthorized"}, LlmRejected),
        (400, {"message": "Invalid schema"}, LlmRejected),
    ],
)
async def test_errors_that_no_fallback_fixes(sessions, provider, clock, status, body, error):
    provider.answer("small", status, body)
    llm = make_llm(sessions, provider, clock)
    with pytest.raises(error):
        await llm.complete("news", "s", "u", "answer", Answer)
    assert len(provider.requests) == 1  # the second model is not asked
    await llm.aclose()


async def test_validation_details_are_kept(sessions, provider, clock):
    detail = {"detail": [{"loc": ["body", "response_format"], "msg": "enum too long"}]}
    provider.answer("small", 422, {"message": detail})
    llm = make_llm(sessions, provider, clock)
    with pytest.raises(LlmRejected, match="enum too long"):
        await llm.complete("news", "s", "u", "answer", Answer)
    await llm.aclose()


@pytest.mark.parametrize(
    ("error", "booked"),
    [(httpx.ReadTimeout, True), (httpx.ConnectTimeout, False), (httpx.ConnectError, False)],
)
async def test_timeouts_book_the_worst_case(sessions, clock, error, booked):
    def broken(request):
        raise error("boom", request=request)

    guard = UsageGuard(sessions, config(), TZ, clock)
    llm = LlmClient(
        "https://llm.test/v1", KEY, ("small",), guard, transport=httpx.MockTransport(broken)
    )
    with pytest.raises(LlmUnavailable):
        await llm.complete("news", "s", "u", "answer", Answer)
    [row] = await usage_rows(sessions)
    max_tokens = load_ai_config().task("news").max_tokens
    assert row.completion_tokens == (max_tokens if booked else 0)
    await llm.aclose()


async def test_server_error_and_network_error(sessions, provider, clock):
    provider.answer("small", 503, {"message": "busy"})
    provider.answer("tiny", 503, {"message": "busy"})
    llm = make_llm(sessions, provider, clock)
    with pytest.raises(LlmUnavailable, match="HTTP 503"):
        await llm.complete("news", "s", "u", "answer", Answer)
    await llm.aclose()

    def broken(request):
        raise httpx.ConnectError("boom", request=request)

    guard = UsageGuard(sessions, config(), TZ, clock)
    llm = LlmClient(
        "https://llm.test/v1", KEY, ("small",), guard, transport=httpx.MockTransport(broken)
    )
    with pytest.raises(LlmUnavailable, match="nicht erreichbar"):
        await llm.complete("news", "s", "u", "answer", Answer)
    await llm.aclose()


async def test_key_never_in_errors_or_logs(sessions, provider, clock, caplog):
    caplog.set_level(logging.DEBUG)
    provider.answer("small", 401, {"message": "Unauthorized"})
    llm = make_llm(sessions, provider, clock)
    with pytest.raises(LlmRejected) as info:
        await llm.complete("news", "s", "u", "answer", Answer)
    assert KEY not in str(info.value)
    assert KEY not in caplog.text
    await llm.aclose()


async def test_daily_call_limit_stops_before_sending(sessions, provider, clock):
    provider.answer("small")
    provider.answer("small")
    llm = make_llm(sessions, provider, clock, models=("small",), calls_per_day=1)
    await llm.complete("news", "s", "u", "answer", Answer)
    with pytest.raises(LlmLimitReached, match="Tageslimit") as info:
        await llm.complete("news", "s", "u", "answer", Answer)
    assert info.value.period == "day"
    assert len(provider.requests) == 1
    # The next day (local time) the counter starts again.
    clock.advance(days=1)
    await llm.complete("news", "s", "u", "answer", Answer)
    assert len(provider.requests) == 2
    await llm.aclose()


async def test_daily_token_limit(sessions, clock):
    guard = UsageGuard(sessions, config(tokens_per_day=5000), TZ, clock)
    await guard.record("news", "small", "ok", 3000, 500)
    with pytest.raises(LlmLimitReached, match="Tageslimit von 5000 Tokens") as info:
        await guard.check("small", 100, 1500)
    assert info.value.period == "day"
    await guard.check("small", 100, 1000)  # exactly 5000: still fits


async def test_budget_counts_the_worst_case(sessions, provider, clock):
    # One request may cost up to max_tokens output at the default price (3 $ per million):
    # 1500 × 3 / 1e6 = 0.0045 $ plus the prompt – more than this budget allows.
    llm = make_llm(sessions, provider, clock, models=("small",), budget_usd_per_month=0.004)
    with pytest.raises(LlmLimitReached, match="Monatsbudget") as info:
        await llm.complete("news", "s", "u", "answer", Answer)
    assert info.value.period == "month"
    assert provider.requests == []
    await llm.aclose()


async def test_monthly_tokens_reset_on_the_first(sessions, provider, clock):
    guard = UsageGuard(sessions, config(tokens_per_month=5000), TZ, clock)
    await guard.record("news", "small", "ok", 3000, 500)
    with pytest.raises(LlmLimitReached, match="Monatslimit"):
        await guard.check("small", 100, 1500)
    clock.advance(days=30)  # 6.10. → 5.11.
    await guard.check("small", 100, 1500)


async def test_requests_are_paced(sessions, provider, clock):
    provider.answer("small")
    provider.answer("small")
    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    guard = UsageGuard(sessions, config(min_interval_seconds=2.0), TZ, clock)
    llm = LlmClient(
        "https://llm.test/v1",
        KEY,
        ("small",),
        guard,
        transport=httpx.MockTransport(provider.handler),
        sleep=sleep,
        monotonic=lambda: 100.0,
    )
    await llm.complete("news", "s", "u", "answer", Answer)
    await llm.complete("news", "s", "u", "answer", Answer)
    assert waits == [2.0]
    await llm.aclose()


def test_strict_schema_inlines_and_requires_everything():
    class Inner(BaseModel):
        name: str
        note: str | None = None

    class Outer(BaseModel):
        items: list[Inner]

    schema = strict_schema(Outer)
    item = schema["properties"]["items"]["items"]
    assert "$defs" not in schema and "$ref" not in json.dumps(schema)
    assert item["required"] == ["name", "note"]
    assert item["additionalProperties"] is False
    assert item["properties"]["note"]["type"] == ["string", "null"]


def test_ai_config_caps_the_budget():
    data = load_ai_config().model_dump()
    data["limits"]["budget_usd_per_month"] = 10
    with pytest.raises(ValueError):
        AiConfig.model_validate(data)


def test_build_llm_without_key_is_off(settings, tmp_path):
    assert build_llm(settings, sessions=None) is None
    settings.secrets_dir.mkdir()
    (settings.secrets_dir / "llm_api_key").write_text(KEY + "\n", encoding="utf-8")
    llm = build_llm(settings, sessions=None)
    assert llm is not None
    assert llm.models == ("ministral-14b-latest", "ministral-8b-latest")


def test_month_boundary_uses_local_time(clock):
    # 31.10. 23:30 UTC is already 1.11. in Berlin (CET, UTC+1).
    clock.now = datetime(2026, 10, 31, 23, 30, tzinfo=UTC)
    guard = UsageGuard(None, config(), TZ, clock)
    day, month = guard._period_starts()
    assert day == datetime(2026, 10, 31, 23, 0, tzinfo=UTC)
    assert month == day


async def test_task_has_its_own_daily_call_limit(sessions, provider, clock):
    data = load_ai_config().model_dump()
    data["limits"]["min_interval_seconds"] = 0
    data["tasks"]["chat"] = {"max_tokens": 600, "calls_per_day": 2}
    guard = UsageGuard(sessions, AiConfig.model_validate(data), TZ, clock)
    for _ in range(2):
        await guard.record("chat", "small", "ok", 10, 10)
    with pytest.raises(LlmLimitReached, match="für „chat“"):
        await guard.check("small", 10, 600, "chat")
    await guard.check("small", 10, 600, "news")  # the other tasks still have their calls


async def test_text_answer_without_schema(sessions, provider, clock):
    provider.answer("small", body=ok_body("  Hallo **Welt**  \n"))
    llm = make_llm(sessions, provider, clock)
    result = await llm.complete_text("chat", "system", "user")
    assert result.data == "Hallo **Welt**"
    assert "response_format" not in provider.requests[0]
    [row] = await usage_rows(sessions)
    assert (row.task, row.status) == ("chat", "ok")
    await llm.aclose()


async def test_empty_text_answer_tries_the_next_model(sessions, provider, clock):
    provider.answer("small", body=ok_body("   "))
    provider.answer("tiny", body=ok_body("Antwort"))
    llm = make_llm(sessions, provider, clock)
    result = await llm.complete_text("chat", "system", "user")
    assert (result.data, result.model) == ("Antwort", "tiny")
    assert [r.status for r in await usage_rows(sessions)] == ["bad_output", "ok"]
    await llm.aclose()
