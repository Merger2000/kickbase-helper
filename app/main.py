import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.auth import CsrfFailed, LoginRequired, LoginThrottle, safe_next_path
from app.config import Settings, get_settings
from app.db.session import make_engine, make_sessionmaker
from app.db.stores import DbResponseCache, KeyValueStore
from app.kickbase.factory import build_kickbase_client
from app.kickbase.league import LeagueContext
from app.llm.factory import build_llm
from app.log import configure_logging
from app.openligadb.client import OpenLigaDbClient
from app.secret_files import log_secret_status
from app.services.programme import load_team_mapping
from app.services.scoring import load_scoring_config
from app.web.routes import auth, health, pages, pwa
from app.web.templating import STATIC_DIR

logger = logging.getLogger(__name__)

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data: https://kickbase.b-cdn.net; style-src 'self'; "
        "script-src 'self'; "
        "connect-src 'self'; font-src 'self'; manifest-src 'self'; worker-src 'self'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    "Strict-Transport-Security": "max-age=31536000",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


def _path_of(url: str | None) -> str:
    """Path plus query of an absolute URL (e.g. the HX-Current-URL header)."""
    if not url:
        return "/"
    try:
        parts = urlsplit(url)
    except ValueError:
        return "/"
    return parts.path + (f"?{parts.query}" if parts.query else "")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    # Fail at startup with a clear message instead of a 500 on the recommendation pages.
    load_scoring_config()
    load_team_mapping()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log_secret_status(settings.secrets_dir)
        yield
        await app.state.kickbase.aclose()
        await app.state.openligadb.aclose()
        if app.state.llm is not None:
            await app.state.llm.aclose()
        await app.state.engine.dispose()

    app = FastAPI(
        title="Kickbase Helper", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.settings = settings
    app.state.engine = make_engine(settings.database_url)
    app.state.sessions = make_sessionmaker(app.state.engine)
    app.state.kickbase = build_kickbase_client(settings, app.state.sessions)
    app.state.openligadb = OpenLigaDbClient(DbResponseCache(app.state.sessions))
    app.state.llm = build_llm(settings, app.state.sessions)
    # Resolved on first use, so the app also starts while Kickbase is unreachable.
    app.state.league = LeagueContext(
        app.state.kickbase, KeyValueStore(app.state.sessions), settings.kickbase_league_name
    )
    app.state.login_throttle = LoginThrottle()

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="kbh_session",
        max_age=settings.session_max_age_days * 24 * 3600,
        same_site="lax",
        https_only=settings.session_https_only,
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next) -> Response:
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    @app.exception_handler(LoginRequired)
    async def redirect_to_login(request: Request, exc: LoginRequired) -> Response:
        is_htmx = bool(request.headers.get("HX-Request"))
        next_path = exc.next_path
        if is_htmx:
            # The failed request is a partial; return to the page that loaded it instead.
            next_path = _path_of(request.headers.get("HX-Current-URL"))
        next_path = safe_next_path(next_path)
        target = "/login" if next_path == "/" else "/login?" + urlencode({"next": next_path})
        if is_htmx:
            return Response(status_code=204, headers={"HX-Redirect": target})
        return RedirectResponse(target, status_code=303)

    @app.exception_handler(CsrfFailed)
    async def csrf_failed(request: Request, exc: CsrfFailed) -> Response:
        logger.warning("CSRF-Prüfung fehlgeschlagen für %s %s", request.method, request.url.path)
        if request.headers.get("HX-Request"):
            # Stale token (e.g. logged in again in another tab): reload the page for a new one.
            return Response(status_code=403, headers={"HX-Refresh": "true"})
        return Response("Sitzung abgelaufen – Seite neu laden.", status_code=403)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(health.router)
    app.include_router(pwa.router)
    app.include_router(auth.router)
    app.include_router(pages.router)
    return app


def build_app() -> FastAPI:
    """Entry point for `uvicorn --factory app.main:build_app`."""
    settings = get_settings()
    configure_logging(settings.log_level)
    return create_app(settings)
