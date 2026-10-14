"""Mobile screenshots (375 px wide) of app pages, plus a check for horizontal scrolling.

Usage:
    KBH_PASSWORD=... uv run --with playwright python scripts/screenshot.py \
        --base https://kickbase.example.com [--out artifacts/screenshots] \
        [--scheme dark|light] [--prefix phase-0-] /login=login /=team ...

A path without "=name" is named after the path. "/login" is captured before logging in.
Exits with status 1 if any page scrolls horizontally.
"""

import argparse
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

VIEWPORT = {"width": 375, "height": 812}


def parse_targets(raw: list[str]) -> list[tuple[str, str]]:
    targets = []
    for item in raw:
        path, _, name = item.partition("=")
        targets.append((path, name or path.strip("/").replace("/", "-") or "start"))
    return targets


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="URL der zu prüfenden Installation")
    parser.add_argument("--out", default="artifacts/screenshots", type=Path)
    parser.add_argument("--scheme", default="dark", choices=["dark", "light"])
    parser.add_argument("--prefix", default="")
    parser.add_argument("--full-page", action="store_true")
    parser.add_argument(
        "--blur",
        default=".rec__seller",
        help="CSS-Selektor für Inhalte, die unkenntlich gemacht werden (z. B. Managernamen)",
    )
    parser.add_argument("targets", nargs="+")
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    targets = parse_targets(args.targets)
    overflow = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(
            viewport=VIEWPORT,
            device_scale_factor=2,
            color_scheme=args.scheme,
            is_mobile=True,
            bypass_csp=True,  # only so the blur style can be injected
        )

        def capture(path: str, name: str) -> None:
            response = page.goto(args.base + path, wait_until="networkidle")
            if response is None or not response.ok:
                sys.exit(f"{path}: HTTP {response.status if response else '?'}")
            if path != "/login" and urlsplit(page.url).path == "/login":
                sys.exit(f"{path}: auf die Login-Seite umgeleitet")
            if args.blur:
                page.add_style_tag(content=f"{args.blur} {{ filter: blur(6px); }}")
            scroll_width = page.evaluate("document.documentElement.scrollWidth")
            if scroll_width > VIEWPORT["width"]:
                overflow.append(f"{path} ({scroll_width}px)")
            file = args.out / f"{args.prefix}{name}-{args.scheme}.png"
            page.screenshot(path=str(file), full_page=args.full_page)
            print(f"{path} -> {file} (scrollWidth {scroll_width}px)")

        for path, name in [t for t in targets if t[0] == "/login"]:
            capture(path, name)

        rest = [t for t in targets if t[0] != "/login"]
        if rest:
            password = os.environ.get("KBH_PASSWORD")
            if not password:
                sys.exit("KBH_PASSWORD fehlt")
            page.goto(args.base + "/login")
            page.fill("#password", password)
            page.click("button[type=submit]")
            page.wait_for_load_state("networkidle")
            if urlsplit(page.url).path == "/login":
                alert = page.locator(".alert")
                message = alert.inner_text() if alert.count() else "unbekannter Fehler"
                sys.exit(f"Login fehlgeschlagen: {message}")
            for path, name in rest:
                capture(path, name)
        browser.close()

    if overflow:
        print("Horizontal scrolling on: " + ", ".join(overflow), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
