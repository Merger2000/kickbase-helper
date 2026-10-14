"""Hard limits for the language model.

Every request is booked in `ai_usage` (also failed ones, so retries count). Before a request the
worst case – estimated prompt plus the full `max_tokens` – must still fit into the limits of the
day and month; otherwise `LlmLimitReached` is raised and nothing is sent. Days and months follow
the local time zone, so "until the 1st" means the 1st in Germany.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from weakref import WeakKeyDictionary
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.db.models import AiUsage
from app.llm.config import AiConfig
from app.llm.errors import LlmLimitReached

logger = logging.getLogger(__name__)

# German text: about 3.5 characters per token; 3 errs on the safe side.
CHARS_PER_TOKEN = 3
# Shared across clients on one engine (also supports the SQLite fixture tests).
_locks: WeakKeyDictionary[AsyncEngine, asyncio.Lock] = WeakKeyDictionary()
USAGE_LOCK_KEY = 0x4B42485F4149


def estimate_tokens(*texts: str) -> int:
    return sum(len(t) for t in texts) // CHARS_PER_TOKEN + 20


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class UsageSummary:
    calls_today: int
    tokens_today: int
    tokens_month: int
    cost_month: float


class UsageGuard:
    def __init__(
        self,
        sessions: async_sessionmaker,
        config: AiConfig,
        tz: ZoneInfo,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.sessions = sessions
        self.config = config
        self.tz = tz
        self.clock = clock
        self._lock = (
            _locks.setdefault(sessions.kw["bind"], asyncio.Lock())
            if sessions is not None
            else asyncio.Lock()
        )

    @asynccontextmanager
    async def exclusive(self) -> AsyncIterator[None]:
        """Serialize check → request → accounting, including app/worker on PostgreSQL.

        The transaction-scoped advisory lock is released on exceptions and cancellation.
        Accounting uses its own committed transaction before this lock is released.
        """
        async with self._lock, self.sessions.begin() as session:
            if session.get_bind().dialect.name == "postgresql":
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"), {"key": USAGE_LOCK_KEY}
                )
            yield

    def _period_starts(self) -> tuple[datetime, datetime]:
        local = self.clock().astimezone(self.tz)
        day = local.replace(hour=0, minute=0, second=0, microsecond=0)
        month = day.replace(day=1)
        return day.astimezone(UTC), month.astimezone(UTC)

    async def summary(self) -> UsageSummary:
        day_start, month_start = self._period_starts()
        tokens = AiUsage.prompt_tokens + AiUsage.completion_tokens
        async with self.sessions() as session:
            today = (
                await session.execute(
                    select(func.count(), func.coalesce(func.sum(tokens), 0)).where(
                        AiUsage.at >= day_start
                    )
                )
            ).one()
            month = (
                await session.execute(
                    select(
                        func.coalesce(func.sum(tokens), 0),
                        func.coalesce(func.sum(AiUsage.cost_usd), 0),
                    ).where(AiUsage.at >= month_start)
                )
            ).one()
        return UsageSummary(
            calls_today=int(today[0]),
            tokens_today=int(today[1]),
            tokens_month=int(month[0]),
            cost_month=float(month[1]),
        )

    async def task_calls_today(self, task: str) -> int:
        day_start, _ = self._period_starts()
        async with self.sessions() as session:
            return int(
                await session.scalar(
                    select(func.count()).where(AiUsage.at >= day_start, AiUsage.task == task)
                )
            )

    async def check(
        self, model: str, prompt_tokens: int, max_tokens: int, task: str | None = None
    ) -> None:
        """Raise LlmLimitReached unless the worst case of the next request fits."""
        limits = self.config.limits
        if task is not None and task in self.config.tasks:
            own = self.config.tasks[task].calls_per_day
            if own is not None and await self.task_calls_today(task) + 1 > own:
                raise LlmLimitReached(
                    f"Tageslimit von {own} KI-Aufrufen für „{task}“ erreicht", "day"
                )
        used = await self.summary()
        worst = prompt_tokens + max_tokens
        worst_cost = self.config.price(model).cost(prompt_tokens, max_tokens)
        if used.calls_today + 1 > limits.calls_per_day:
            raise LlmLimitReached(
                f"Tageslimit von {limits.calls_per_day} KI-Aufrufen erreicht", "day"
            )
        if used.tokens_today + worst > limits.tokens_per_day:
            raise LlmLimitReached(f"Tageslimit von {limits.tokens_per_day} Tokens erreicht", "day")
        if used.tokens_month + worst > limits.tokens_per_month:
            raise LlmLimitReached(
                f"Monatslimit von {limits.tokens_per_month} Tokens erreicht", "month"
            )
        if used.cost_month + worst_cost > limits.budget_usd_per_month:
            raise LlmLimitReached(
                f"Monatsbudget von {limits.budget_usd_per_month:.2f} $ erreicht "
                f"(verbraucht {used.cost_month:.2f} $)",
                "month",
            )

    async def record(
        self, task: str, model: str, status: str, prompt_tokens: int = 0, completion_tokens: int = 0
    ) -> float:
        cost = self.config.price(model).cost(prompt_tokens, completion_tokens)
        async with self.sessions.begin() as session:
            session.add(
                AiUsage(
                    at=self.clock(),
                    task=task,
                    model=model,
                    status=status,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    cost_usd=cost,
                )
            )
        return cost
