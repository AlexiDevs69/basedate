"""
Prints a bcrypt hash for the admin password.

    python scripts/hash_admin_password.py

Put the printed value into the ADMIN_PASSWORD_HASH environment variable
(Render -> Environment) and remove ADMIN_PASSWORD. Bcrypt hashes contain "$"
characters -- paste them as-is, without quotes.
"""
import getpass
import sys

import bcrypt


def main() -> int:
    password = getpass.getpass("New admin password: ")
    if getpass.getpass("Repeat: ") != password:
        print("Passwords do not match.", file=sys.stderr)
        return 1
    raw = password.encode("utf-8")
    if len(raw) < 10:
        print("Use at least 10 characters.", file=sys.stderr)
        return 1
    if len(raw) > 72:
        print("bcrypt only uses the first 72 bytes -- use a shorter password.", file=sys.stderr)
        return 1
    print(bcrypt.hashpw(raw, bcrypt.gensalt(rounds=12)).decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
