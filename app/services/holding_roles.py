"""Own local roster preferences, scoped by league; no Kickbase mutation."""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import HoldingRole
from app.db.upsert import upsert


class Role(StrEnum):
    AUTO = "auto"
    CORE = "core"
    TRADE = "trade"


async def roles_for(sessions: async_sessionmaker, league_id: str) -> dict[str, str]:
    async with sessions() as session:
        return dict(
            (
                await session.execute(
                    select(HoldingRole.player_id, HoldingRole.role).where(
                        HoldingRole.league_id == league_id
                    )
                )
            ).all()
        )


async def set_role(
    sessions: async_sessionmaker, league_id: str, player_id: str, role: Role, now: datetime
) -> None:
    role = Role(role)
    async with sessions.begin() as session:
        if role is Role.AUTO:
            await session.execute(
                delete(HoldingRole).where(
                    HoldingRole.league_id == league_id,
                    HoldingRole.player_id == player_id,
                )
            )
        else:
            await upsert(
                session,
                HoldingRole,
                {
                    "league_id": league_id,
                    "player_id": player_id,
                    "role": role.value,
                    "changed_at": now,
                },
                keys=["league_id", "player_id"],
            )
