"""Fake Kickbase API (httpx.MockTransport) and helpers for client tests."""

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from app.kickbase.client import KickbaseClient
from app.kickbase.stores import MemoryResponseCache, MemoryTokenStore

FIXTURES = Path(__file__).parent / "fixtures" / "kickbase"
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class FakeClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


class Monotonic:
    """Manually advanced replacement for time.monotonic."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class FakeKickbase:
    """Answers requests from queued responses per (method, path); the last one repeats."""

    routes: dict[tuple[str, str], list[Any]] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    delay: float = 0.0
    in_flight: int = 0
    max_in_flight: int = 0
    login_count: int = 0

    def __post_init__(self) -> None:
        self.on_login()

    def on(self, method: str, path: str, *responses: Any) -> None:
        self.routes[(method, path)] = list(responses)

    def on_login(self, *responses: Any) -> None:
        if not responses:
            responses = (self.login_response("token-1"),)
        self.on("POST", "/v4/user/login", *responses)

    @staticmethod
    def login_response(token: str, expires: datetime = NOW + timedelta(days=7)) -> httpx.Response:
        return httpx.Response(200, json={"tkn": token, "tknex": expires.isoformat()})

    def gets(self, path: str | None = None) -> list[httpx.Request]:
        return [
            r for r in self.requests if r.method == "GET" and (path is None or r.url.path == path)
        ]

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            queue = self.routes.get((request.method, request.url.path))
            if not queue:
                return httpx.Response(404, json={"err": "not mocked"})
            response = queue.pop(0) if len(queue) > 1 else queue[0]
            if request.url.path == "/v4/user/login":
                self.login_count += 1
            if isinstance(response, Exception):
                raise response
            if callable(response):  # dynamic answer, e.g. depending on the token
                response = response(request)
            if isinstance(response, httpx.Response):
                return response
            return httpx.Response(200, json=response)
        finally:
            self.in_flight -= 1


async def no_sleep(_seconds: float) -> None:
    await asyncio.sleep(0)


def make_client(fake: FakeKickbase, **kwargs: Any) -> KickbaseClient:
    defaults: dict[str, Any] = {
        "credentials": lambda: ("manager@example.com", "pw"),
        "tokens": MemoryTokenStore(),
        "cache": MemoryResponseCache(),
        "transport": httpx.MockTransport(fake.handler),
        "min_interval": 0.0,
        "retry_delays": (0.0, 0.0),
        "unavailable_backoff": 0.0,
        "sleep": no_sleep,
        "clock": FakeClock(),
    }
    defaults.update(kwargs)
    credentials = defaults.pop("credentials")
    tokens = defaults.pop("tokens")
    cache = defaults.pop("cache")
    return KickbaseClient(credentials, tokens, cache, **defaults)


TEAM_LEAGUE_ID = "1000002"  # anonymised id of "Testliga" in the fixtures

TEAM_ENDPOINTS = {
    "/v4/user/me": "user_me",
    "/v4/leagues/selection": "leagues_selection",
    f"/v4/leagues/{TEAM_LEAGUE_ID}/me/budget": "league_budget",
    f"/v4/leagues/{TEAM_LEAGUE_ID}/ranking": "league_ranking",
    f"/v4/leagues/{TEAM_LEAGUE_ID}/squad": "league_squad",
    f"/v4/leagues/{TEAM_LEAGUE_ID}/lineup": "league_lineup",
    f"/v4/leagues/{TEAM_LEAGUE_ID}/teamcenter/myeleven": "league_myeleven",
    "/v4/competitions/1/table": "competition_table",
}


def fake_with_team_data() -> FakeKickbase:
    fake = FakeKickbase()
    for path, name in TEAM_ENDPOINTS.items():
        fake.on("GET", path, fixture(name))
    return fake


class MemoryKV:
    def __init__(self) -> None:
        self.data: dict = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value):
        self.data[key] = value


def install_fake_kickbase(app, fake: FakeKickbase, league_name: str = "Testliga", **kwargs: Any):
    """Replace the app's Kickbase client (and league context) with one talking to `fake`."""
    from app.kickbase.league import LeagueContext

    kwargs.setdefault("clock", lambda: datetime.now(UTC))
    client = make_client(fake, **kwargs)
    app.state.kickbase = client
    app.state.league = LeagueContext(client, MemoryKV(), league_name)
    return client


OLDB_FIXTURES = Path(__file__).parent / "fixtures" / "openligadb"


def oldb_fixture(name: str) -> Any:
    return json.loads((OLDB_FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class FakeOpenLigaDb:
    """OpenLigaDB with the recorded fixtures: matchday 5 of 2026/27 and both tables."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.down = False
        self.routes: dict[str, Any] = {
            "/getcurrentgroup/bl1": oldb_fixture("currentgroup"),
            "/getbltable/bl1/2026": oldb_fixture("table2026"),
            "/getbltable/bl1/2025": oldb_fixture("table2025"),
            "/getmatchdata/bl1/2026/5": oldb_fixture("matchday5"),
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            return httpx.Response(503)
        path = request.url.path
        if path in self.routes:
            return httpx.Response(200, json=self.routes[path])
        if path.startswith("/getmatchdata/"):
            return httpx.Response(200, json=[])
        return httpx.Response(404)


def make_oldb_client(fake: FakeOpenLigaDb, **kwargs: Any):
    from app.openligadb.client import OpenLigaDbClient

    return OpenLigaDbClient(
        MemoryResponseCache(), transport=httpx.MockTransport(fake.handler), **kwargs
    )


def add_recommendation_endpoints(fake: FakeKickbase) -> None:
    fake.on("GET", "/v4/competitions/1/matchdays", fixture("competition_matchdays"))
    fake.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/market", fixture("league_market"))


# ---------------------------------------------------------------------------- AI and Telegram


class FakeLlm:
    """Chat completion endpoint answering in order with prepared JSON contents (dicts) or
    complete responses."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        answer = self.answers.pop(0)
        if isinstance(answer, httpx.Response):
            return answer
        return llm_response(json.dumps(answer))


def llm_response(content: str, finish: str = "stop", status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "choices": [{"message": {"content": content}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 500, "completion_tokens": 100},
        },
    )


def make_llm(sessions, fake: FakeLlm, clock, models=("small",), **limits):
    from zoneinfo import ZoneInfo

    from app.llm.client import LlmClient
    from app.llm.config import AiConfig, load_ai_config
    from app.llm.usage import UsageGuard

    data = load_ai_config().model_dump()
    data["limits"] |= {"min_interval_seconds": 0} | limits
    guard = UsageGuard(sessions, AiConfig.model_validate(data), ZoneInfo("Europe/Berlin"), clock)
    return LlmClient(
        "https://llm.test/v1", "key", models, guard, transport=httpx.MockTransport(fake.handler)
    )


class FakeTelegram:
    def __init__(self, fail: bool = False) -> None:
        self.texts: list[str] = []
        self.fail = fail

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.fail:
            return httpx.Response(400, json={"ok": False, "description": "Bad Request"})
        self.texts.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"ok": True})
