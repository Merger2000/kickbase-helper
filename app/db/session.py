import logging

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

logger = logging.getLogger(__name__)


def async_database_url(database_url: str) -> URL:
    """The app uses async drivers: psycopg (async capable) for Postgres, aiosqlite in tests."""
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite":
        return url.set(drivername="sqlite+aiosqlite")
    if url.get_backend_name() == "postgresql":
        return url.set(drivername="postgresql+psycopg")
    return url


def make_engine(database_url: str) -> AsyncEngine:
    url = async_database_url(database_url)
    connect_args = {"connect_timeout": 5} if url.get_backend_name() == "postgresql" else {}
    # hide_parameters: SQL errors must not echo values (tokens, cached payloads) into logs.
    return create_async_engine(
        url, pool_pre_ping=True, hide_parameters=True, connect_args=connect_args
    )


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


async def database_reachable(engine: AsyncEngine) -> bool:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        logger.warning(
            "Datenbank nicht erreichbar: %s: %s", type(exc).__name__, getattr(exc, "orig", exc)
        )
        return False
    return True
