"""Fetch one real response per Kickbase endpoint, anonymise it and store it as a test fixture.

Runs inside the app container, because only there the Docker secrets are mounted:

    docker compose run --rm --no-deps \
        -v ./scripts:/srv/scripts:ro -v ./tests/fixtures:/srv/tests/fixtures \
        -v /some/private/dir:/raw app python scripts/probe.py --raw-dir /raw

About 22 GET requests, one per endpoint, paced to one per second. With `--raw-dir` the
unmodified responses are kept outside the repository, so the anonymisation can be refined with
`--offline` (no API requests; runs on the host with `uv run`). Anonymisation replaces the
names, ids, e-mail addresses and images of all managers (including the account owner) and the
league names; player and club data are public and stay unchanged. The script refuses to write
anything if a known personal value would still be present afterwards.
"""

import argparse
import asyncio
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.kickbase.client import KickbaseClient
from app.kickbase.stores import MemoryResponseCache, MemoryTokenStore
from app.secret_files import read_secret

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "kickbase"

# Keys whose values identify a person no matter where they appear.
EMAIL_KEYS = {"em", "email", "vemail", "emve"}
TOKEN_KEYS = {"tkn", "chttkn"}
USER_IMAGE_KEYS = {"uim", "profile", "cover", "ua", "prfu"}
# Keys holding a manager name next to the manager id key, e.g. offers (u/unm), owners (oui/onm).
USER_NAME_KEYS = {"unm": "u", "onm": "oui"}
# Keys holding a manager name without any id: buyer and seller of a transfer in the feed.
NAME_ONLY_KEYS = {"byr", "slr"}
ANON_NAME = re.compile(r"Manager \d+")
ANON_IMAGE = "content/file/anonymous.png"
ANON_EMAIL = "manager@example.com"


class Anonymizer:
    def __init__(self) -> None:
        self.replacements: dict[str, str] = {}
        self._users = 0
        self._leagues = 0

    def add_user(self, user_id: Any, name: Any) -> None:
        if isinstance(user_id, str) and user_id:
            if user_id not in self.replacements:
                self._users += 1
                self.replacements[user_id] = str(9_000_000 + self._users)
            number = int(self.replacements[user_id]) - 9_000_000
        else:
            self._users += 1
            number = self._users
        if isinstance(name, str) and name and name not in self.replacements:
            self.replacements[name] = f"Manager {number}"

    def add_league(self, league_id: Any, name: Any, *, target: bool) -> None:
        self._leagues += 1
        if isinstance(league_id, str) and league_id:
            self.replacements.setdefault(league_id, str(1_000_000 + self._leagues))
        if isinstance(name, str) and name:
            self.replacements.setdefault(
                name, "Testliga" if target else f"Andere Liga {self._leagues}"
            )

    def apply(self, value: Any, key: str | None = None) -> Any:
        if isinstance(value, dict):
            return {k: self.apply(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [self.apply(v, key) for v in value]
        if isinstance(value, str):
            if key in EMAIL_KEYS:
                return ANON_EMAIL
            if key in TOKEN_KEYS:
                return "REDACTED"
            if key in USER_IMAGE_KEYS:
                return ANON_IMAGE
            return self.replacements.get(value, value)
        return value

    def leaks(self, payloads: dict[str, Any], extra: set[str]) -> list[str]:
        """Locations (fixture and JSON path, never the value) where personal data remains."""
        sensitive = set(self.replacements) | extra
        found: list[str] = []
        for name, payload in payloads.items():
            for path, key, value in walk(payload):
                if not isinstance(value, str):
                    continue
                hit = value in sensitive or any(len(s) >= 4 and s in value for s in sensitive)
                if (key in USER_NAME_KEYS or key in NAME_ONLY_KEYS) and not ANON_NAME.fullmatch(
                    value
                ):
                    hit = True  # a manager name that was not replaced
                if hit:
                    found.append(f"{name}:{path}")
        return found


def walk(value: Any, path: str = "$", key: str | None = None):
    if isinstance(value, dict):
        for k, v in value.items():
            yield from walk(v, f"{path}.{k}", k)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from walk(v, f"{path}[{i}]", key)
    else:
        yield path, key, value


def find_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for v in value.values():
            yield from find_dicts(v)
    elif isinstance(value, list):
        for v in value:
            yield from find_dicts(v)


class LeakError(RuntimeError):
    pass


def anonymize_all(raw: dict[str, Any], league_name: str) -> tuple[dict[str, Any], int]:
    """Anonymise all responses. Raises LeakError if a known personal value would remain."""
    anonymizer = Anonymizer()
    extra_sensitive: set[str] = set()
    for d in find_dicts(raw):
        for key in EMAIL_KEYS | TOKEN_KEYS:
            if isinstance(d.get(key), str):
                extra_sensitive.add(d[key])
    # Leagues (selection items carry the competition id "cpi").
    for d in find_dicts(raw.get("leagues_selection", {})):
        if "cpi" in d and "n" in d:
            is_target = d["n"] == league_name
            anonymizer.add_league(d.get("i"), d["n"], target=is_target)
    # Managers: explicit containers first (not every manager has a profile image), then any
    # other object that looks like a user as a fallback.
    owner = raw.get("user_me", {}).get("u", {})
    anonymizer.add_user(owner.get("id"), owner.get("name"))
    for user in raw.get("league_ranking", {}).get("us", []):
        anonymizer.add_user(user.get("i"), user.get("n"))
    for user in raw.get("league_myeleven", {}).get("mu", {}).values():
        if isinstance(user, dict):
            anonymizer.add_user(user.get("i"), user.get("n"))
    for item in raw.get("league_market", {}).get("it", []):
        if isinstance(item.get("u"), dict):
            anonymizer.add_user(item["u"].get("i"), item["u"].get("n"))
        for offer in item.get("ofs", []):
            anonymizer.add_user(offer.get("u"), offer.get("unm"))
    for d in find_dicts(raw):
        if "cpi" in d:
            continue
        if "uim" in d and "i" in d and "n" in d:
            anonymizer.add_user(d["i"], d["n"])
        for name_key, id_key in USER_NAME_KEYS.items():
            if name_key in d:
                anonymizer.add_user(d.get(id_key), d[name_key])
        for name_key in NAME_ONLY_KEYS:
            if isinstance(d.get(name_key), str):
                anonymizer.add_user(None, d[name_key])

    anonymized = {name: anonymizer.apply(payload) for name, payload in raw.items()}
    leaks = anonymizer.leaks(anonymized, extra_sensitive)
    if leaks:
        raise LeakError("Persönliche Daten wären noch enthalten an: " + ", ".join(leaks[:20]))
    return anonymized, len(anonymizer.replacements)


async def fetch_all(
    league_name: str, secrets_dir: Path, raw_dir: Path | None = None
) -> dict[str, Any]:
    def credentials() -> tuple[str, str]:
        return (
            read_secret(secrets_dir, "kickbase_email"),
            read_secret(secrets_dir, "kickbase_password"),
        )

    raw: dict[str, Any] = {}
    async with KickbaseClient(
        credentials, MemoryTokenStore(), MemoryResponseCache(), min_interval=1.0
    ) as client:

        async def get(name: str, path: str, **params: Any) -> Any:
            fetched = await client.get_json(path, ttl=0, params=params or None)
            raw[name] = fetched.data
            if raw_dir:  # written right away, so a failure later does not lose it
                (raw_dir / f"{name}.json").write_text(json.dumps(fetched.data, ensure_ascii=False))
            return fetched.data

        me = await get("user_me", "/v4/user/me")
        selection = await get("leagues_selection", "/v4/leagues/selection")

        league = next(
            (d for d in find_dicts([me, selection]) if league_name in (d.get("name"), d.get("n"))),
            None,
        )
        if league is None:
            raise SystemExit("Liga nicht gefunden, Probe abgebrochen")
        league_id = league.get("id") or league.get("i")
        competition_id = str(league.get("cpi") or "1")
        user_id = me["u"]["id"]
        base = f"/v4/leagues/{league_id}"

        await get("league_me", f"{base}/me")
        await get("league_budget", f"{base}/me/budget")
        squad = await get("league_squad", f"{base}/squad")
        await get("league_lineup", f"{base}/lineup")
        await get("league_myeleven", f"{base}/teamcenter/myeleven")
        await get("league_ranking", f"{base}/ranking")
        await get("league_market", f"{base}/market")
        # Transfers only (type 15): buyers are manager names, anonymised like all others.
        await get(
            "league_activities_transfers", f"{base}/activitiesFeed", start=0, max=50, filter=15
        )
        # Block C, C3.1: finished-matchday snapshots. The latest `day` from the default ranking
        # is the day to query; the responses are immutable and reused for the matchday report.
        ranking = raw["league_ranking"]
        day = ranking.get("day")
        if isinstance(day, int) and day >= 1:
            await get("league_ranking_day", f"{base}/ranking", dayNumber=day)
            await get(
                "league_user_teamcenter",
                f"{base}/users/{user_id}/teamcenter",
                dayNumber=day,
            )

        player_id = squad["it"][0]["i"]
        await get("league_player", f"{base}/players/{player_id}")
        await get("league_player_performance", f"{base}/players/{player_id}/performance")
        await get("league_player_market_value_92", f"{base}/players/{player_id}/marketValue/92")

        cbase = f"/v4/competitions/{competition_id}"
        await get("competitions", "/v4/competitions")
        await get("competition_players", f"{cbase}/players")
        await get("competition_table", f"{cbase}/table")
        await get("competition_matchdays", f"{cbase}/matchdays")
        await get("competition_player", f"{cbase}/players/{player_id}")
        await get("competition_player_performance", f"{cbase}/players/{player_id}/performance")
        await get(
            "competition_player_market_value_92", f"{cbase}/players/{player_id}/marketValue/92"
        )
        team_id = squad["it"][0]["tid"]
        await get("competition_team_profile", f"{cbase}/teams/{team_id}/teamprofile")
        ranking = raw["league_ranking"]
        await get(
            "competition_team_center",
            f"{cbase}/teams/{team_id}/teamcenter",
            dayNumber=ranking["day"],
        )
    return raw


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, help="unveränderte Antworten hier ablegen/lesen")
    parser.add_argument("--offline", action="store_true", help="nur aus --raw-dir anonymisieren")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = get_settings()

    if args.offline:
        if not args.raw_dir:
            parser.error("--offline braucht --raw-dir")
        raw = {p.stem: json.loads(p.read_text()) for p in sorted(args.raw_dir.glob("*.json"))}
    else:
        if args.raw_dir:
            args.raw_dir.mkdir(parents=True, exist_ok=True)
        raw = await fetch_all(settings.kickbase_league_name, settings.secrets_dir, args.raw_dir)

    try:
        anonymized, replaced = anonymize_all(raw, settings.kickbase_league_name)
    except LeakError as exc:
        print(f"Abbruch: {exc}", file=sys.stderr)
        return 1

    FIXTURES.mkdir(parents=True, exist_ok=True)
    for name, payload in anonymized.items():
        path = FIXTURES / f"{name}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"{path.name}: {len(path.read_bytes())} bytes")
    print(f"{replaced} Werte ersetzt")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
