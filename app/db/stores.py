"""Database-backed implementations of the Kickbase token store, response cache and a small
key-value store, shared between app and worker."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import ApiCache, AuthToken, KeyValue
from app.db.upsert import upsert
from app.kickbase.stores import CacheEntry, Token


class DbTokenStore:
    def __init__(self, sessions: async_sessionmaker, provider: str = "kickbase") -> None:
        self._sessions = sessions
        self._provider = provider

    async def load(self) -> Token | None:
        async with self._sessions() as session:
            row = await session.get(AuthToken, self._provider)
            return Token(row.token, row.expires_at) if row else None

    async def save(self, token: Token) -> None:
        async with self._sessions.begin() as session:
            await upsert(
                session,
                AuthToken,
                {
                    "provider": self._provider,
                    "token": token.value,
                    "expires_at": token.expires_at,
                    "updated_at": datetime.now(UTC),
                },
                keys=["provider"],
            )

    async def clear(self) -> None:
        async with self._sessions.begin() as session:
            await session.execute(delete(AuthToken).where(AuthToken.provider == self._provider))


class DbResponseCache:
    def __init__(self, sessions: async_sessionmaker) -> None:
        self._sessions = sessions

    async def get(self, key: str) -> CacheEntry | None:
        async with self._sessions() as session:
            row = await session.get(ApiCache, key)
            return CacheEntry(row.payload, row.fetched_at, row.expires_at) if row else None

    async def set(self, key: str, entry: CacheEntry) -> None:
        async with self._sessions.begin() as session:
            await upsert(
                session,
                ApiCache,
                {
                    "key": key,
                    "payload": entry.payload,
                    "fetched_at": entry.fetched_at,
                    "expires_at": entry.expires_at,
                },
                keys=["key"],
            )

    async def purge_expired(self, before: datetime) -> int:
        """Delete entries that expired before `before`. Pass a time well in the past (e.g. now
        minus a week): expired entries are still served as stale data when Kickbase is down."""
        async with self._sessions.begin() as session:
            result = await session.execute(delete(ApiCache).where(ApiCache.expires_at < before))
            return result.rowcount or 0


class KeyValueStore:
    def __init__(self, sessions: async_sessionmaker) -> None:
        self._sessions = sessions

    async def get(self, key: str) -> Any | None:
        async with self._sessions() as session:
            value = await session.scalar(select(KeyValue.value).where(KeyValue.key == key))
            return value

    async def set(self, key: str, value: Any) -> None:
        async with self._sessions.begin() as session:
            await upsert(
                session,
                KeyValue,
                {"key": key, "value": value, "updated_at": datetime.now(UTC)},
                keys=["key"],
            )
