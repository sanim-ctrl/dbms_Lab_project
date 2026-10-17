#!/usr/bin/env python3
"""Generate a Werkzeug password hash for the shared admin password.

The plaintext is read interactively with getpass, so it is never part of the
source code, a command-line argument, or your shell history.

Usage
-----
    python scripts/generate_admin_hash.py

Then paste the printed value into your PRIVATE .env file (never into source):

    ADMIN_PASSWORD_HASH=<printed hash>

and add the same value to your host's environment variables
(Vercel: Project Settings -> Environment Variables).

The printed hash is safe to store in the environment; the plaintext password is
not. Re-run this script to change the password.
"""

import getpass
import sys

from werkzeug.security import generate_password_hash

MIN_LENGTH = 12


def main():
    try:
        password = getpass.getpass("Admin password (hidden): ")
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.", file=sys.stderr)
        return 1

    confirm = getpass.getpass("Confirm password (hidden): ")
    if password != confirm:
        print("Passwords do not match.", file=sys.stderr)
        return 1
    if len(password) < MIN_LENGTH:
        print(f"Password must be at least {MIN_LENGTH} characters.", file=sys.stderr)
        return 1

    print("\nADMIN_PASSWORD_HASH=" + generate_password_hash(password))
    return 0


if __name__ == "__main__":
    sys.exit(main())
