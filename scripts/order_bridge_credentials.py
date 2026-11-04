"""Private standard-library credentials for the three independent bridge actors."""

from __future__ import annotations

import json
import os
import secrets
import stat
import tempfile
from pathlib import Path

ROLES = ("settings", "operator", "integration")
DATABASES = frozenset(("odoo_portfolio", "odoo_portfolio_m1_test"))


def validate_target(database: str, host: str, opt_in: bool) -> None:
    if not isinstance(database, str) or database not in DATABASES or host != "127.0.0.1" or opt_in is not True:
        raise ValueError("Bridge preparation requires explicit opt-in and an approved local database.")


def _validate(data, database):
    if (
        not isinstance(data, dict) or set(data) != {"schema_version", "database", "accounts"}
        or type(data["schema_version"]) is not int or data["schema_version"] != 1
        or data["database"] != database or not isinstance(data["accounts"], dict)
        or set(data["accounts"]) != set(ROLES)
    ):
        raise ValueError("Invalid bridge credential schema or database.")
    passwords = set()
    for role in ROLES:
        account = data["accounts"][role]
        if (
            not isinstance(account, dict) or set(account) != {"login", "password"}
            or account["login"] != f"portfolio.bridge.acceptance.{role}"
            or not isinstance(account["password"], str) or len(account["password"]) < 24
            or not account["password"].strip() or account["password"] in passwords
        ):
            raise ValueError("Invalid or duplicate bridge credentials.")
        passwords.add(account["password"])
    return data


def _safe_path(path):
    """Reject symlinks in every existing component, including the final file."""
    path = Path(os.path.abspath(path))
    for component in (*reversed(path.parents), path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("Bridge credential paths must not contain symlinks.")
        if component != path and not stat.S_ISDIR(info.st_mode):
            raise ValueError("Bridge credential parents must be directories.")
    return path


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate credential keys.")
        result[key] = value
    return result


def _read(path, database):
    descriptor = None
    try:
        _safe_path(path)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.getuid() or info.st_size > 65536
        ):
            raise ValueError("Bridge credentials require a private owned regular file with mode0600.")
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            descriptor = None
            data = json.load(handle, object_pairs_hook=_unique_object)
        return _validate(data, database)
    except (OSError, ValueError, UnicodeError):
        raise ValueError("Unsafe or malformed private bridge credential file.") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def ensure_credentials(path: Path, database: str) -> dict:
    """Publish once using an exclusive hard link; never replace existing data."""
    if not isinstance(database, str) or database not in DATABASES:
        raise ValueError("Unsupported bridge acceptance database.")
    path = _safe_path(path)
    if path.exists():
        return _read(path, database)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _safe_path(path)
        data = _validate({
            "schema_version": 1, "database": database,
            "accounts": {
                role: {"login": f"portfolio.bridge.acceptance.{role}",
                       "password": secrets.token_urlsafe(32)}
                for role in ROLES
            },
        }, database)
        descriptor, temporary = tempfile.mkstemp(prefix=".order-bridge-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            _safe_path(path)
            try:
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError:
                pass
        finally:
            os.unlink(temporary)
    except (OSError, ValueError):
        raise ValueError("Unable to safely prepare private bridge credentials.") from None
    return _read(path, database)
