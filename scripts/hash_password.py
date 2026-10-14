"""Create an Argon2 hash for the app login (value for APP_PASSWORD_HASH in .env).

Usage:  uv run python scripts/hash_password.py          # prompts for the password
        uv run python scripts/hash_password.py --generate  # creates a random password

Put the hash in single quotes in .env, otherwise Docker Compose interprets the `$` signs.
"""

import argparse
import getpass
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.auth import hash_password

ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o, 1/l/i: easy to type on a phone


def generate_password(groups: int = 4, group_len: int = 4) -> str:
    return "-".join(
        "".join(secrets.choice(ALPHABET) for _ in range(group_len)) for _ in range(groups)
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generate", action="store_true", help="zufälliges Passwort erzeugen")
    args = parser.parse_args()

    if args.generate:
        password = generate_password()
        print(f"Passwort: {password}", file=sys.stderr)
    else:
        password = getpass.getpass("Passwort: ")
        if not password:
            sys.exit("Das Passwort darf nicht leer sein.")
        if password != getpass.getpass("Wiederholen: "):
            sys.exit("Die Passwörter stimmen nicht überein.")
    print(hash_password(password))


if __name__ == "__main__":
    main()
