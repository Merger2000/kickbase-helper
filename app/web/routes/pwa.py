from fastapi import APIRouter
from fastapi.responses import FileResponse

from app.web.templating import STATIC_DIR

router = APIRouter(include_in_schema=False)


@router.get("/manifest.webmanifest")
def manifest() -> FileResponse:
    return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")


@router.get("/sw.js")
def service_worker() -> FileResponse:
    # Served from the root so the worker's scope covers the whole app.
    return FileResponse(
        STATIC_DIR / "js" / "sw.js",
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/favicon.ico")
def favicon() -> FileResponse:
    return FileResponse(STATIC_DIR / "icons" / "favicon.ico", media_type="image/x-icon")
