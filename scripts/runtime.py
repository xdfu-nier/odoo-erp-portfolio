"""Project-local configuration shared by the native development commands."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from pathlib import Path


DEFAULTS = {
    "PGHOST": "127.0.0.1",
    "PGPORT": "55432",
    "PGUSER": "odoo",
    "PGPASSWORD": "",
    "DB_NAME": "odoo_portfolio",
    "ODOO_PORT": "8069",
    "ODOO_MASTER_PASSWORD": "",
}


def initialize_env(root: Path) -> None:
    """Create local credentials once; never replace user configuration."""
    target = root / ".env"
    if target.exists():
        return
    content = (root / ".env.example").read_text(encoding="utf-8")
    content = content.replace("odoo-local-dev", secrets.token_urlsafe(24))
    content = content.replace("local-dev-master-change-me", secrets.token_urlsafe(32))
    with target.open("x", encoding="utf-8") as handle:
        target.chmod(0o600)
        handle.write(content)


@dataclass(frozen=True)
class Settings:
    root: Path
    values: dict[str, str]

    @classmethod
    def load(cls, root: Path) -> Settings:
        values = dict(DEFAULTS)
        for raw in (root / ".env").read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            if not separator or key.strip() not in DEFAULTS:
                raise ValueError(f"Unsupported .env setting: {key!r}")
            values[key.strip()] = value.strip()
        for name in ("PGPORT", "ODOO_PORT"):
            if not values[name].isdigit() or not 1024 <= int(values[name]) <= 65535:
                raise ValueError(f"{name} must be a port between 1024 and 65535")
        if values["PGHOST"] != "127.0.0.1":
            raise ValueError("This development profile requires PGHOST=127.0.0.1")
        if values["PGPORT"] == values["ODOO_PORT"]:
            raise ValueError("PostgreSQL and Odoo require different ports")
        for name in ("DB_NAME", "PGUSER"):
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", values[name]):
                raise ValueError(f"{name} must be a lowercase SQL identifier")
        if values["PGUSER"] in ("postgres", "portfolio_db_owner"):
            raise ValueError("PGUSER must be a dedicated Odoo role")
        return cls(root.resolve(), values)

    @property
    def runtime(self) -> Path:
        return self.root / ".runtime"


def write_odoo_config(settings: Settings) -> Path:
    values = settings.values
    if not values["PGPASSWORD"] or not values["ODOO_MASTER_PASSWORD"]:
        raise ValueError("Set PGPASSWORD and ODOO_MASTER_PASSWORD in .env")
    settings.runtime.mkdir(parents=True, exist_ok=True)
    path = settings.runtime / "odoo.conf"
    content = "\n".join([
        "[options]",
        f"admin_passwd = {values['ODOO_MASTER_PASSWORD']}",
        f"db_host = {values['PGHOST']}",
        f"db_port = {values['PGPORT']}",
        f"db_user = {values['PGUSER']}",
        f"db_password = {values['PGPASSWORD']}",
        f"db_name = {values['DB_NAME']}",
        f"dbfilter = ^{values['DB_NAME']}$",
        "list_db = False",
        "http_interface = 127.0.0.1",
        f"http_port = {values['ODOO_PORT']}",
        "workers = 0",
        "max_cron_threads = 0",
        f"addons_path = {settings.root / '.upstream/odoo/addons'},{settings.root / 'addons'}",
        f"data_dir = {settings.runtime / 'filestore'}",
        "",
    ])
    with path.open("w", encoding="utf-8") as handle:
        path.chmod(0o600)
        handle.write(content)
    return path
