from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.db.stores import DbResponseCache, DbTokenStore
from app.kickbase.client import CacheTTL, KickbaseClient
from app.secret_files import read_secret


def build_kickbase_client(
    settings: Settings,
    sessions: async_sessionmaker,
    *,
    serve_stale: bool = True,
    min_interval: float | None = None,
) -> KickbaseClient:
    """`serve_stale=False` for the worker: its time series must never store old data as new."""

    def credentials() -> tuple[str, str]:
        # Read only when a login is needed; never stored or logged.
        return (
            read_secret(settings.secrets_dir, "kickbase_email"),
            read_secret(settings.secrets_dir, "kickbase_password"),
        )

    return KickbaseClient(
        credentials,
        DbTokenStore(sessions),
        DbResponseCache(sessions),
        ttl=CacheTTL(
            live=settings.cache_ttl_live,
            player=settings.cache_ttl_player,
            static=settings.cache_ttl_static,
        ),
        # One request at a time per process: app and worker together stay within the hard
        # limit of two parallel Kickbase requests.
        max_parallel=1,
        min_interval=settings.kickbase_min_interval if min_interval is None else min_interval,
    )
