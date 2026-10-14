"""Provider-neutral client for chat completions in the OpenAI-compatible format (Mistral,
OpenRouter, ...). Only structured answers: every request carries a JSON schema and the answer is
validated against a Pydantic model before anyone sees it.

The API key travels in the Authorization header only; error messages carry status codes and the
provider's short error text, never request headers.
"""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from app.llm.errors import (
    LlmBadOutput,
    LlmQuotaExhausted,
    LlmRejected,
    LlmTruncated,
    LlmUnavailable,
)
from app.llm.usage import UsageGuard, estimate_tokens

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Completion[T: BaseModel]:
    data: T
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float


class LlmClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        models: tuple[str, ...],
        guard: UsageGuard,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not models:
            raise ValueError("mindestens ein Modell nötig")
        self.models = tuple(dict.fromkeys(models))  # unique, order kept
        self.guard = guard
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(60.0, connect=10.0),
            transport=transport,
        )
        self._sleep = sleep
        self._monotonic = monotonic
        self._lock = asyncio.Lock()
        self._last_request: float | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    async def complete_text(self, task: str, system: str, user: str) -> Completion[str]:
        """Like `complete`, but the answer is plain text (no JSON schema). Long German prose
        with quotes and line breaks breaks a small model's JSON far more often than plain text."""
        return await self.complete(task, system, user, "", None)  # type: ignore[arg-type]

    async def complete[T: BaseModel](
        self, task: str, system: str, user: str, schema_name: str, output: type[T]
    ) -> Completion[T]:
        """Ask the first model; on a rate limit or an unusable answer the next one. Raises
        LlmLimitReached before sending anything if our own limits don't allow the request.

        When no model succeeded, a temporary problem (LlmUnavailable) wins over bad output:
        the caller should try again later rather than give up on the input."""
        max_tokens = self.guard.config.task(task).max_tokens
        prompt_estimate = estimate_tokens(system, user)
        errors: list[LlmUnavailable | LlmBadOutput] = []
        for model in self.models:
            try:
                async with self.guard.exclusive():
                    await self.guard.check(model, prompt_estimate, max_tokens, task)
                    return await self._request(
                        task, model, system, user, schema_name, output, max_tokens
                    )
            except (LlmUnavailable, LlmBadOutput) as exc:
                logger.warning("KI-Aufruf (%s, %s) fehlgeschlagen: %s", task, model, exc)
                errors.append(exc)
        unavailable = [e for e in errors if isinstance(e, LlmUnavailable)]
        if unavailable:
            raise unavailable[0]
        truncated = [e for e in errors if isinstance(e, LlmTruncated)]
        raise truncated[-1] if len(truncated) == len(errors) else errors[-1]

    async def _request[T: BaseModel](
        self,
        task: str,
        model: str,
        system: str,
        user: str,
        schema_name: str,
        output: type[T],
        max_tokens: int,
    ) -> Completion[T]:
        payload = {
            "model": model,
            "temperature": 0,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if output is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": strict_schema(output),
                },
            }
        worst_case = (estimate_tokens(system, user), max_tokens)
        response = await self._post(task, model, payload, worst_case)
        try:
            body = response.json()
            usage = body.get("usage")
            # Missing or malformed counters must never silently become free usage.
            usage = usage if isinstance(usage, dict) else {}
            prompt_tokens = _usage_counter(usage.get("prompt_tokens"), worst_case[0])
            completion_tokens = _usage_counter(usage.get("completion_tokens"), worst_case[1])
        except (ValueError, AttributeError, TypeError):
            # Not a completion at all (e.g. a proxy error page): temporary, try again later.
            await self.guard.record(task, model, "error", *worst_case)
            raise LlmUnavailable("Antwort des KI-Anbieters ist kein JSON-Dokument") from None
        try:
            choice = body["choices"][0]
            content = choice["message"]["content"]
            finish_reason = choice.get("finish_reason")
        except (KeyError, IndexError, TypeError):
            await self.guard.record(task, model, "error", prompt_tokens, completion_tokens)
            raise LlmUnavailable(f"Antwort ohne Inhalt ({_error_text(response)})") from None
        if finish_reason == "length":
            await self.guard.record(task, model, "bad_output", prompt_tokens, completion_tokens)
            raise LlmTruncated(f"Antwort nach {completion_tokens} Tokens abgeschnitten")
        try:
            if output is None:
                if not isinstance(content, str) or not content.strip():
                    raise TypeError("leere Antwort")
                data = content.strip()
            else:
                data = output.model_validate(json.loads(content))
        except (json.JSONDecodeError, TypeError, ValidationError) as exc:
            await self.guard.record(task, model, "bad_output", prompt_tokens, completion_tokens)
            raise LlmBadOutput(f"Antwort passt nicht zum Schema ({type(exc).__name__})") from None
        cost = await self.guard.record(task, model, "ok", prompt_tokens, completion_tokens)
        logger.info(
            "KI-Aufruf %s mit %s: %d + %d Tokens (%.4f $)",
            task,
            model,
            prompt_tokens,
            completion_tokens,
            cost,
        )
        return Completion(data, model, prompt_tokens, completion_tokens, cost)

    async def _post(
        self, task: str, model: str, payload: dict[str, Any], worst_case: tuple[int, int]
    ) -> httpx.Response:
        async with self._lock:
            await self._pace()
            try:
                response = await self._http.post("/chat/completions", json=payload)
            except httpx.HTTPError as exc:
                # After a read timeout the provider has usually done (and billed) the work.
                billed = isinstance(exc, httpx.TimeoutException) and not isinstance(
                    exc, httpx.ConnectTimeout
                )
                await self.guard.record(task, model, "error", *(worst_case if billed else (0, 0)))
                raise LlmUnavailable(
                    f"KI-Anbieter nicht erreichbar ({type(exc).__name__})"
                ) from None
            finally:
                self._last_request = self._monotonic()
        if response.status_code == 200:
            return response
        detail = _error_text(response)
        status = response.status_code
        if status == 429 and _is_quota(response, detail):
            await self.guard.record(task, model, "quota")
            raise LlmQuotaExhausted(f"Guthaben des Monats aufgebraucht: {detail}")
        if status == 402:
            await self.guard.record(task, model, "quota")
            raise LlmQuotaExhausted(f"Guthaben aufgebraucht: {detail}")
        if status == 429:
            await self.guard.record(task, model, "rate_limited")
            raise LlmUnavailable(f"Rate-Limit bei {model}")
        await self.guard.record(task, model, "error")
        if status >= 500:
            raise LlmUnavailable(f"KI-Anbieter antwortet mit HTTP {status}")
        if status in (401, 403):
            raise LlmRejected(f"API-Key abgelehnt (HTTP {status})")
        raise LlmRejected(f"Anfrage abgelehnt: HTTP {status} {detail}")

    async def _pace(self) -> None:
        interval = self.guard.config.limits.min_interval_seconds
        if self._last_request is None or interval <= 0:
            return
        wait = self._last_request + interval - self._monotonic()
        if wait > 0:
            await self._sleep(wait)


def _body(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _usage_counter(value: Any, fallback: int) -> int:
    return value if type(value) is int and value >= 0 else fallback


def _error_text(response: httpx.Response) -> str:
    body = _body(response)
    message = body.get("message") or body.get("error") or body.get("detail") or ""
    if isinstance(message, dict):
        # e.g. Mistral's validation errors: {"message": {"detail": [...]}}
        message = message.get("message") or json.dumps(message, ensure_ascii=False)
    elif isinstance(message, list):
        message = json.dumps(message, ensure_ascii=False)
    return str(message)[:200]


QUOTA_WORDS = ("quota", "credit", "budget", "billing", "insufficient", "exhausted", "payment")


def _is_quota(response: httpx.Response, detail: str) -> bool:
    """A 429 caused by used-up credit instead of a short rate limit (message, type or code)."""
    body = _body(response)
    error = body.get("error") if isinstance(body.get("error"), dict) else {}
    fields = [detail, body.get("type"), body.get("code"), error.get("type"), error.get("code")]
    text = " ".join(str(f) for f in fields if f).lower()
    return any(word in text for word in QUOTA_WORDS)


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON schema for strict structured output: every property required, no extras, no
    `$ref` indirection (some providers don't resolve it)."""
    schema = model.model_json_schema()
    definitions = schema.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return resolve(definitions[node["$ref"].split("/")[-1]])
            node = {k: resolve(v) for k, v in node.items() if k not in ("title", "default")}
            options = node.get("anyOf")
            if options and all(set(o) == {"type"} and isinstance(o["type"], str) for o in options):
                # Optional[str] → {"type": ["string", "null"]}, understood by every provider.
                node.pop("anyOf")
                node["type"] = [o["type"] for o in options]
            if node.get("type") == "object" and "properties" in node:
                node["required"] = list(node["properties"])
                node["additionalProperties"] = False
            return node
        if isinstance(node, list):
            return [resolve(item) for item in node]
        return node

    return resolve(schema)
