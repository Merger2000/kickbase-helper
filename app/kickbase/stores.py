"""Token and response cache storage: in-memory variants for tests, database variants for app and
worker (see `app.db.stores`)."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol


@dataclass(frozen=True)
class Token:
    value: str = field(repr=False)  # never show the token in logs or tracebacks
    expires_at: datetime

    def usable(self, now: datetime, margin: timedelta = timedelta(hours=1)) -> bool:
        return now + margin < self.expires_at


class TokenStore(Protocol):
    async def load(self) -> Token | None: ...
    async def save(self, token: Token) -> None: ...
    async def clear(self) -> None: ...


@dataclass(frozen=True)
class CacheEntry:
    payload: Any
    fetched_at: datetime
    expires_at: datetime


class ResponseCache(Protocol):
    async def get(self, key: str) -> CacheEntry | None: ...
    async def set(self, key: str, entry: CacheEntry) -> None: ...


class MemoryTokenStore:
    def __init__(self, token: Token | None = None) -> None:
        self.token = token

    async def load(self) -> Token | None:
        return self.token

    async def save(self, token: Token) -> None:
        self.token = token

    async def clear(self) -> None:
        self.token = None


class MemoryResponseCache:
    def __init__(self) -> None:
        self.entries: dict[str, CacheEntry] = {}

    async def get(self, key: str) -> CacheEntry | None:
        return self.entries.get(key)

    async def set(self, key: str, entry: CacheEntry) -> None:
        self.entries[key] = entry
