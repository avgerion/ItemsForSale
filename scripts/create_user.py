from __future__ import annotations

import getpass
import json

from app.security import hash_password


def main() -> None:
    username = input("Username: ").strip()
    if not username or any(character.isspace() for character in username):
        raise SystemExit("Choose a non-empty username without spaces.")
    password = getpass.getpass("Password (minimum 14 characters): ")
    confirmation = getpass.getpass("Confirm password: ")
    if len(password) < 14:
        raise SystemExit("Use a password with at least 14 characters.")
    if password != confirmation:
        raise SystemExit("Passwords do not match.")
    print("Add this entry to INVENTORY_USERS JSON, then restart the app:")
    print(json.dumps({username: hash_password(password)}))


if __name__ == "__main__":
    main()
