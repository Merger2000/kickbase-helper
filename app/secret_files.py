"""Access to Docker secrets mounted as files (`/run/secrets/<name>`).

Values are only read at the moment they are needed and are never logged; for startup
diagnostics use `secret_status`, which reports presence without exposing content.
"""

import logging
from enum import StrEnum
from pathlib import Path

logger = logging.getLogger(__name__)

KICKBASE_SECRETS = ("kickbase_email", "kickbase_password")


class SecretStatus(StrEnum):
    OK = "ok"
    MISSING = "fehlt"
    EMPTY = "leer"
    UNREADABLE = "nicht lesbar"


class SecretError(RuntimeError):
    pass


def secret_status(secrets_dir: Path, name: str) -> SecretStatus:
    path = secrets_dir / name
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return SecretStatus.MISSING
    except (OSError, UnicodeDecodeError) as exc:
        # Only the error reason is logged, never file content.
        reason = exc.strerror if isinstance(exc, OSError) else "kein UTF-8"
        logger.warning("Secret %s nicht lesbar: %s", name, reason)
        return SecretStatus.UNREADABLE
    return SecretStatus.OK if content.strip() else SecretStatus.EMPTY


def read_secret(secrets_dir: Path, name: str) -> str:
    status = secret_status(secrets_dir, name)
    if status is not SecretStatus.OK:
        raise SecretError(f"Secret '{name}' ist {status}")
    return (secrets_dir / name).read_text(encoding="utf-8").strip()


def log_secret_status(secrets_dir: Path, names: tuple[str, ...] = KICKBASE_SECRETS) -> bool:
    """Log presence of each secret (never the value). Returns True if all are usable."""
    all_ok = True
    for name in names:
        status = secret_status(secrets_dir, name)
        if status is SecretStatus.OK:
            logger.info("Secret %s: %s", name, status)
        else:
            all_ok = False
            logger.error("Secret %s: %s", name, status)
    return all_ok
