"""Private credentials for the explicit, local-only acceptance profile."""

from __future__ import annotations

import json
import os
import secrets
import stat
import tempfile
from pathlib import Path

ROLES = ("sales", "manager", "manager2", "buyer", "warehouse", "finance", "other_sales")
DATABASES = frozenset(("odoo_portfolio", "odoo_portfolio_m1_test"))


def validate_target(database: str, host: str, opt_in: bool) -> None:
    if database not in DATABASES or host != "127.0.0.1" or opt_in is not True:
        raise ValueError("Acceptance preparation requires an explicit opt-in and an allowed local database.")


def _validate_credentials(data: object, database: str) -> dict:
    if not isinstance(data, dict) or set(data) != {"schema_version", "database", "accounts"}:
        raise ValueError("Malformed acceptance credential file.")
    accounts = data["accounts"]
    if data["schema_version"] != 1 or data["database"] != database:
        raise ValueError("Acceptance credentials belong to another database or schema.")
    if not isinstance(accounts, dict) or set(accounts) != set(ROLES):
        raise ValueError("Acceptance credentials require all seven roles.")
    passwords = set()
    for role in ROLES:
        account = accounts[role]
        if (
            not isinstance(account, dict) or set(account) != {"login", "password"}
            or account["login"] != f"portfolio.acceptance.{role}"
            or not isinstance(account["password"], str) or len(account["password"]) < 24
            or not account["password"].strip() or account["password"] in passwords
        ):
            raise ValueError("Malformed or duplicate acceptance credentials.")
        passwords.add(account["password"])
    return data


def _read_credentials(path: Path, database: str) -> dict:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        raise ValueError("Acceptance credentials must be a private regular file, never a symlink.") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
            raise ValueError("Acceptance credential file requires ownership by this user and mode0600.")
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            descriptor = None
            data = json.load(handle)
    except (ValueError, UnicodeError) as error:
        raise ValueError("Unsafe or malformed acceptance credential file.") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return _validate_credentials(data, database)


def ensure_credentials(path: Path, database: str) -> dict:
    """Publish a complete mode0600 file once; existing files are never changed."""
    if database not in DATABASES:
        raise ValueError("Unsupported acceptance database.")
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Symlink acceptance credential files are not allowed.")
    if path.exists():
        return _read_credentials(path, database)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "schema_version": 1,
        "database": database,
        "accounts": {
            role: {"login": f"portfolio.acceptance.{role}", "password": secrets.token_urlsafe(32)}
            for role in ROLES
        },
    }
    _validate_credentials(data, database)
    descriptor, temporary = tempfile.mkstemp(prefix=".acceptance-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            # Hard-link publication is atomic and cannot replace an existing file.
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            pass
    finally:
        os.unlink(temporary)
    return _read_credentials(path, database)
