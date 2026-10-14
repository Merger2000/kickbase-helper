import hashlib
from functools import lru_cache
from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.auth import csrf_token, is_logged_in
from app.web.formatting import FILTERS

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


@lru_cache
def static_url(path: str) -> str:
    """URL for a static asset with a content hash, so browsers and the service worker
    pick up changes immediately after a deploy."""
    digest = hashlib.sha256((STATIC_DIR / path).read_bytes()).hexdigest()[:10]
    return f"/static/{path}?v={digest}"


templates = Jinja2Templates(directory=TEMPLATES_DIR)
templates.env.filters.update(FILTERS)
templates.env.globals.update(
    csrf_token=csrf_token,
    is_logged_in=is_logged_in,
    static_url=static_url,
)
