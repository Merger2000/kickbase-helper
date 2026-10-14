"""Einzelne Endpoints aus docs/kickbase-openapi.yaml nachschlagen, ohne die ganze Spec zu lesen.

Die Spec hat ~17.000 Zeilen (~115k Tokens). Agents lesen sie nie komplett, sondern nur über
dieses Skript.

Usage:
    uv run python scripts/spec.py --list [FILTER]       # Endpoints auflisten (optional gefiltert)
    uv run python scripts/spec.py PFAD [--method get]   # einen Endpoint kompakt ausgeben

PFAD ist ein exakter Pfad (/v4/leagues/{leagueId}/market) oder ein Teilstring (market). Passen
mehr als MAX_MATCHES Endpoints, wird nur die Liste ausgegeben.
"""

import argparse
import sys
from pathlib import Path

import yaml

SPEC = Path(__file__).resolve().parent.parent / "docs" / "kickbase-openapi.yaml"
METHODS = ("get", "post", "put", "patch", "delete")
MAX_MATCHES = 3


def load_paths() -> dict:
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    with SPEC.open(encoding="utf-8") as handle:
        return yaml.load(handle, Loader=loader).get("paths", {})


def operations(paths: dict) -> list[tuple[str, str, dict]]:
    ops = []
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        for method in METHODS:
            op = item.get(method)
            if isinstance(op, dict):
                ops.append((path, method, op))
    return ops


def select(ops: list, query: str, method: str | None) -> list:
    if method:
        ops = [o for o in ops if o[1] == method.lower()]
    exact = [o for o in ops if o[0] == query]
    if exact:
        return exact
    needle = query.lower()
    return [o for o in ops if needle in o[0].lower()]


def list_line(path: str, method: str, op: dict) -> str:
    return f"{method.upper():6} {path}  – {op.get('summary', '')}"


def render(path: str, method: str, op: dict) -> str:
    lines = [f"# {method.upper()} {path}"]
    params = op.get("parameters") or []
    if params:
        lines.append("\n## Parameter")
        for p in params:
            schema = p.get("schema") or {}
            required = " (Pflicht)" if p.get("required") else ""
            lines.append(f"- {p.get('name')} [{p.get('in')}] {schema.get('type', '')}{required}")
    body = op.get("requestBody")
    if body:
        lines.append("\n## Request-Body")
        lines.append(yaml.safe_dump(body, allow_unicode=True, sort_keys=False).strip())
    description = (op.get("description") or "").strip()
    if description:
        lines.append("\n## Beschreibung")
        lines.append(description)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("query", nargs="?", help="Pfad oder Teilstring")
    parser.add_argument("--list", action="store_true", help="nur Endpoints auflisten")
    parser.add_argument("--method", help="nur diese HTTP-Methode (get, post …)")
    args = parser.parse_args(argv)

    ops = operations(load_paths())
    if args.list:
        chosen = select(ops, args.query, args.method) if args.query else ops
        print("\n".join(list_line(*o) for o in chosen) or "Keine Treffer.")
        return 0
    if not args.query:
        parser.error("PFAD oder --list angeben")

    chosen = select(ops, args.query, args.method)
    if not chosen:
        print(f"Kein Endpoint passt zu {args.query!r}. Tipp: --list {args.query}", file=sys.stderr)
        return 1
    if len(chosen) > MAX_MATCHES:
        print(f"{len(chosen)} Treffer – bitte genauer angeben:")
        print("\n".join(list_line(*o) for o in chosen))
        return 0
    print("\n\n".join(render(*o) for o in chosen))
    return 0


if __name__ == "__main__":
    sys.exit(main())
