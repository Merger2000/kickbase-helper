"""Matches a player name from a news text ("Christoph Baumgartner") to a Kickbase player.

Kickbase mostly knows the short name ("Baumgartner", sometimes "S. Michel" or "Tiago Tomás").
A match needs the club and must be unambiguous within it; otherwise the report stays without
player id instead of being attached to the wrong player.
"""

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class KnownPlayer:
    id: str
    name: str
    team_id: str


def normalize(name: str) -> tuple[str, ...]:
    decomposed = unicodedata.normalize("NFKD", name)
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    plain = plain.replace("ø", "o").replace("Ø", "o").replace("ß", "ss").casefold()
    plain = plain.replace("'", "").replace("’", "")  # "N'Dicka" is one name, not "N." + "Dicka"
    return tuple(t for t in re.split(r"[^a-z0-9]+", plain) if t)


def _find(outer: tuple[str, ...], inner: tuple[str, ...]) -> int | None:
    """Start index of `inner` as a contiguous block in `outer`, or None."""
    for i in range(len(outer) - len(inner) + 1):
        if outer[i : i + len(inner)] == inner:
            return i
    return None


def _first_letter(tokens: tuple[str, ...], shared: tuple[str, ...]) -> str | None:
    """First letter of the first name in front of the shared name parts: a full first name
    ("sven michel") or an initial ("s michel"); None when the name has neither."""
    full = tuple(t for t in tokens if len(t) > 1)
    index = _find(full, shared)
    if index:
        return full[index - 1][0]
    initials = [t for t in tokens if len(t) == 1]
    return initials[-1] if initials else None


def _matches(reported: tuple[str, ...], known: tuple[str, ...]) -> bool:
    """Same player when the name parts (initials aside) of one name appear as a block in the
    other: "Christoph Baumgartner" ~ "Baumgartner", "Silva" ~ "Fábio Silva", "Sambi" ~ "Sambi
    Lokonga". Initials must fit a full first name: "T. Becker" ≁ "Jan Becker"."""
    r_names = tuple(t for t in reported if len(t) > 1)
    k_names = tuple(t for t in known if len(t) > 1)
    if not r_names or not k_names:
        return False
    if _find(r_names, k_names) is not None:
        shared = k_names
    elif _find(k_names, r_names) is not None:
        shared = r_names
    else:
        return False
    r_first = _first_letter(reported, shared)
    k_first = _first_letter(known, shared)
    return r_first is None or k_first is None or r_first == k_first


def match_player(
    reported_name: str, team_id: str | None, players: Iterable[KnownPlayer]
) -> str | None:
    if team_id is None:
        return None
    reported = normalize(reported_name)
    hits = {p.id for p in players if p.team_id == team_id and _matches(reported, normalize(p.name))}
    return hits.pop() if len(hits) == 1 else None
