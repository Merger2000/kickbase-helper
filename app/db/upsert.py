from collections.abc import Sequence
from typing import Any

from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase

# Postgres allows at most 65535 bind parameters per statement.
BATCH_SIZE = 500


async def upsert(
    session: AsyncSession, model: type[DeclarativeBase], values: dict[str, Any], keys: list[str]
) -> None:
    """INSERT ... ON CONFLICT (keys) DO UPDATE for Postgres and SQLite."""
    await upsert_many(session, model, [values], keys)


async def upsert_many(
    session: AsyncSession,
    model: type[DeclarativeBase],
    rows: Sequence[dict[str, Any]],
    keys: list[str],
) -> None:
    if not rows:
        return
    dialect = session.bind.dialect.name
    insert = {"postgresql": postgresql.insert, "sqlite": sqlite.insert}[dialect]
    for start in range(0, len(rows), BATCH_SIZE):
        stmt = insert(model).values(list(rows[start : start + BATCH_SIZE]))
        updates = {k: stmt.excluded[k] for k in rows[0] if k not in keys}
        if updates:
            stmt = stmt.on_conflict_do_update(index_elements=keys, set_=updates)
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=keys)
        await session.execute(stmt)
