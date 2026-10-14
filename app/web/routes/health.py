from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.db.session import database_reachable

router = APIRouter()


@router.get("/health", include_in_schema=False)
async def health(request: Request) -> JSONResponse:
    if not await database_reachable(request.app.state.engine):
        return JSONResponse({"status": "error", "db": "unreachable"}, status_code=503)
    return JSONResponse({"status": "ok", "db": "ok"})
