"""Builds the language model client from the settings, or None when no key is configured."""

import logging
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.llm.client import LlmClient
from app.llm.config import load_ai_config
from app.llm.usage import UsageGuard
from app.secret_files import SecretStatus, read_secret, secret_status

logger = logging.getLogger(__name__)

KEY_SECRET = "llm_api_key"


def build_llm(settings, sessions: async_sessionmaker) -> LlmClient | None:
    status = secret_status(settings.secrets_dir, KEY_SECRET)
    if status is not SecretStatus.OK:
        logger.info("KI aus: Secret %s ist %s", KEY_SECRET, status)
        return None
    models = tuple(m for m in (settings.llm_model, settings.llm_fallback_model) if m)
    guard = UsageGuard(sessions, load_ai_config(), ZoneInfo(settings.tz))
    logger.info("KI an: %s über %s", ", ".join(models), settings.llm_base_url)
    return LlmClient(
        settings.llm_base_url, read_secret(settings.secrets_dir, KEY_SECRET), models, guard
    )
