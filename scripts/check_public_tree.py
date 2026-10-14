"""Reject private artifact paths in the Git index, including force-added ignored files.

This supplements gitleaks and fixture anonymisation; it is not a content/PII scanner.
It only prints paths and never reads credentials or ignored local files.
"""

import subprocess
from pathlib import PurePosixPath

PRIVATE_SUFFIXES = {
    ".bundle",
    ".db",
    ".dump",
    ".key",
    ".log",
    ".p12",
    ".pem",
    ".pfx",
    ".sql",
    ".sqlite",
    ".sqlite3",
}
PRIVATE_DIRS = {"secrets", "artifacts", ".agent-logs", "node_modules", ".venv"}


def private_path(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        bool(set(path.parts) & PRIVATE_DIRS)
        or (path.name.startswith(".env") and path.name != ".env.example")
        or path.suffix.lower() in PRIVATE_SUFFIXES
        or name.endswith(".sql.gz")
        or name.startswith("docs/screenshots/")
        or (name.startswith("docs/images/") and name != "docs/images/banner.png")
    )


def main() -> int:
    names = subprocess.check_output(["git", "ls-files", "-z"]).decode().split("\0")
    blocked = sorted(name for name in names if name and private_path(name))
    if blocked:
        print("Private artifacts must not be committed:\n" + "\n".join(blocked))
        return 1
    print("Git index: no private artifact paths.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
