"""Errors of the language model client. Messages never contain the API key."""

from typing import Literal

Period = Literal["day", "month"]


class LlmError(Exception):
    pass


class LlmLimitReached(LlmError):
    """One of our own limits (calls, tokens, budget) is reached: no request was sent."""

    def __init__(self, message: str, period: Period) -> None:
        super().__init__(message)
        self.period = period


class LlmQuotaExhausted(LlmError):
    """The provider refuses because the (free) credit of this month is used up."""

    period: Period = "month"


class LlmRejected(LlmError):
    """The provider rejects the request itself (invalid key, bad request): retrying won't help."""


class LlmUnavailable(LlmError):
    """Network problems, server errors, rate limits or answers that are not a completion at
    all (e.g. a proxy error page): worth trying again later."""


class LlmBadOutput(LlmError):
    """The model answered, but the content doesn't match the schema."""


class LlmTruncated(LlmBadOutput):
    """The answer hit max_tokens: the input was too large for one request."""
