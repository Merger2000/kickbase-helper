import logging
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.auth import (
    SESSION_CSRF_KEY,
    csrf_valid,
    is_logged_in,
    log_in,
    log_out,
    safe_next_path,
    verify_password,
)
from app.web.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()


def _login_page(request: Request, *, next_path: str, error: str | None, status_code: int):
    return templates.TemplateResponse(
        request,
        "login.html",
        {"next": next_path, "error": error},
        status_code=status_code,
    )


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str | None = None) -> Response:
    next_path = safe_next_path(next)
    if is_logged_in(request):
        return RedirectResponse(next_path, status_code=303)
    return _login_page(request, next_path=next_path, error=None, status_code=200)


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    password: Annotated[str, Form()] = "",
    csrf: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "/",
) -> Response:
    next_path = safe_next_path(next)
    throttle = request.app.state.login_throttle
    if not csrf_valid(request, csrf):
        # Without a CSRF token in the session the cookie did not come back (blocked cookies,
        # plain HTTP, or SESSION_SECRET changed); otherwise the form was from an old session.
        cause = "veraltetes Formular" if SESSION_CSRF_KEY in request.session else "kein Cookie"
        logger.warning("Login abgelehnt: CSRF-Prüfung fehlgeschlagen (%s)", cause)
        return _login_page(
            request,
            next_path=next_path,
            error="Die Anmeldung konnte nicht geprüft werden. Lade die Seite neu und versuch es "
            "noch einmal.",
            status_code=400,
        )
    if throttle.blocked():
        logger.warning("Login gesperrt: zu viele Fehlversuche")
        return _login_page(
            request,
            next_path=next_path,
            error="Zu viele Fehlversuche. Warte ein paar Minuten und versuch es dann erneut.",
            status_code=429,
        )
    if not verify_password(request.app.state.settings.app_password_hash, password):
        throttle.record_failure()
        logger.warning("Login fehlgeschlagen (%d in Folge)", throttle.failure_count())
        return _login_page(
            request, next_path=next_path, error="Das Passwort stimmt nicht.", status_code=401
        )
    throttle.reset()
    log_in(request)
    logger.info("Login erfolgreich")
    return RedirectResponse(next_path, status_code=303)


@router.post("/logout")
def logout(request: Request) -> Response:
    # No CSRF check needed: the session cookie is SameSite=Lax, so cross-site POSTs do not
    # carry it, and a forged logout would be harmless anyway. A stale token must not keep
    # the user logged in.
    log_out(request)
    return RedirectResponse("/login", status_code=303)
