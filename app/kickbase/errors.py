class KickbaseError(Exception):
    """Base class for all Kickbase client errors."""


class ReadOnlyViolation(KickbaseError):
    """A request other than GET (or the login POST) was attempted. Always a programming error."""

    def __init__(self, method: str, path: str) -> None:
        super().__init__(f"Kickbase-Client ist nur lesend: {method} {path} ist nicht erlaubt")
        self.method = method
        self.path = path


class KickbaseAuthError(KickbaseError):
    """Authentication failed and was not recoverable by a single re-login."""


class KickbaseLoginFailed(KickbaseAuthError):
    """Kickbase rejected the credentials (or a recent rejection is still cooling down)."""


class KickbaseCredentialsMissing(KickbaseAuthError):
    """A Docker secret with the Kickbase credentials is missing, empty or unreadable."""


class KickbaseUnavailable(KickbaseError):
    """Network error, timeout, 429 or 5xx after all retries."""


class KickbaseSchemaError(KickbaseError):
    """The response was not what we expect (no JSON, missing fields, unknown structure)."""


class KickbaseHTTPError(KickbaseError):
    """Any other unexpected HTTP status."""

    def __init__(
        self, status_code: int, path: str, *, method: str = "GET", detail: str | None = None
    ) -> None:
        message = f"Kickbase antwortet mit HTTP {status_code} auf {method} {path}"
        super().__init__(f"{message} ({detail})" if detail else message)
        self.status_code = status_code
        self.path = path
        self.method = method
