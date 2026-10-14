"""Connection check for deployments: `docker compose exec app python -m app.kickbase.check`.

Always contacts Kickbase (no response cache; the stored token is reused), resolves the
configured league and prints a short summary. Exits non-zero on any problem. Prints no
credentials, tokens or other managers' data.
"""

import asyncio
import sys

from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.db.session import make_engine, make_sessionmaker
from app.db.stores import DbTokenStore, KeyValueStore
from app.kickbase.client import KickbaseClient
from app.kickbase.errors import KickbaseError
from app.kickbase.league import resolve_league
from app.kickbase.stores import MemoryResponseCache
from app.log import configure_logging
from app.secret_files import read_secret


async def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    engine = make_engine(settings.database_url)
    sessions = make_sessionmaker(engine)

    def credentials() -> tuple[str, str]:
        return (
            read_secret(settings.secrets_dir, "kickbase_email"),
            read_secret(settings.secrets_dir, "kickbase_password"),
        )

    try:
        async with KickbaseClient(
            credentials, DbTokenStore(sessions), MemoryResponseCache(), serve_stale=False
        ) as client:
            league = await resolve_league(
                client, KeyValueStore(sessions), settings.kickbase_league_name, refresh=True
            )
            budget = await client.budget(league.id)
            squad = await client.squad(league.id)
    except KickbaseError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 1
    except SQLAlchemyError as exc:
        print(f"Datenbankfehler: {type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    print(f"Liga: {league.name} (id {league.id}, Wettbewerb {league.competition_id})")
    print(f"Budget: {budget.data.budget:,} €".replace(",", "."))
    print(f"Kader: {len(squad.data.players)} Spieler")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
