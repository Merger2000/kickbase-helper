"""Limits and prices for the language model, from `config/ai.yaml`."""

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "ai.yaml"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Limits(StrictModel):
    calls_per_day: int = Field(ge=1)
    tokens_per_day: int = Field(ge=1000)
    tokens_per_month: int = Field(ge=1000)
    questions_per_day: int = Field(default=30, ge=1)
    budget_usd_per_month: float = Field(gt=0, le=3.5)  # never more than about 3 € a month
    min_interval_seconds: float = Field(ge=0)


class Price(StrictModel):
    input: float = Field(ge=0)  # USD per million tokens
    output: float = Field(ge=0)

    def cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return (prompt_tokens * self.input + completion_tokens * self.output) / 1_000_000


class Task(StrictModel):
    max_tokens: int = Field(ge=50, le=4000)
    # Own daily limit of calls for this task, so it can't use up the calls of the others.
    calls_per_day: int | None = Field(default=None, ge=1)


class ClaudeConfig(StrictModel):
    """Claude im Telegram-Chat (über die Bridge auf der VM, bezahlt über das Claude-Abo)."""

    window_minutes: int = Field(default=10, ge=1, le=120)
    questions_per_day: int = Field(default=40, ge=1)
    default_model: str = "sonnet"
    # Kurznamen für /claude_model; volle Namen (claude-...) sind zusätzlich erlaubt.
    models: tuple[str, ...] = ("sonnet", "opus", "haiku")
    max_budget_usd_per_question: float = Field(default=2.0, gt=0, le=5.0)
    timeout_seconds: int = Field(default=240, ge=30, le=600)


class AiConfig(StrictModel):
    limits: Limits
    prices: dict[str, Price]
    default_price: Price
    tasks: dict[str, Task]
    claude: ClaudeConfig = ClaudeConfig()

    def price(self, model: str) -> Price:
        return self.prices.get(model, self.default_price)

    def task(self, name: str) -> Task:
        try:
            return self.tasks[name]
        except KeyError:
            raise ValueError(f"Aufgabe '{name}' fehlt in config/ai.yaml") from None


@lru_cache
def load_ai_config(path: Path = CONFIG_PATH) -> AiConfig:
    return AiConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
