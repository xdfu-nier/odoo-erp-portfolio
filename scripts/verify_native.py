"""Verify the running native development environment against actual services."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.pycache_prefix = str(ROOT / ".cache" / "pycache")

import http.cookiejar  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402
import urllib.request  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

from manage import postgres_command  # noqa: E402
from runtime import Settings  # noqa: E402


def validate_navigation(references, actions, menus, existing_models) -> list[str]:
    expected = {
        "sales": "sale.order", "purchases": "purchase.order",
        "stock": "stock.picking", "invoices": "account.move",
    }
    identifiers = {item["name"]: item["res_id"] for item in references}
    menu_records = {item["id"]: item for item in menus}
    action_records = {item["id"]: item for item in actions}
    root = identifiers.get("menu_portfolio_root")
    if not root or root not in menu_records or menu_records[root]["parent_id"]:
        raise ValueError("Portfolio application root menu is missing or incorrectly attached")
    for key, model in expected.items():
        menu = menu_records.get(identifiers.get(f"menu_portfolio_{key}"))
        action_id = identifiers.get(f"action_portfolio_{key}")
        action = action_records.get(action_id)
        if not menu or not menu["parent_id"] or menu["parent_id"][0] != root:
            raise ValueError(f"Portfolio menu {key} is missing or incorrectly attached")
        if menu["action"] != f"ir.actions.act_window,{action_id}":
            raise ValueError(f"Portfolio menu {key} does not invoke its expected action")
        if not action or action["res_model"] != model or model not in existing_models:
            raise ValueError(f"Portfolio action {key} does not resolve to a registered model")
        if not {"list", "form"}.issubset(set(action["view_mode"].split(","))):
            raise ValueError(f"Portfolio action {key} is missing required list/form views")
    return sorted(expected.values())


def main() -> None:
    import psycopg2

    settings = Settings.load(ROOT)
    values = settings.values
    modules = ["portfolio_trade", "sale_management", "purchase_stock", "sale_stock", "account"]
    result = {
        "checked_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
        "python": sys.version.split()[0],
        "odoo_revision": (ROOT / "odoo-source.lock").read_text().strip(),
        "postgres": subprocess.check_output(
            [postgres_command("postgres"), "--version"], text=True
        ).strip(),
    }
    with psycopg2.connect(
        host=values["PGHOST"], port=values["PGPORT"], user=values["PGUSER"],
        password=values["PGPASSWORD"], dbname=values["DB_NAME"],
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname=current_user"
            )
            superuser, createdb, createrole = cursor.fetchone()
            if superuser or createrole or not createdb:
                raise RuntimeError("Unexpected Odoo database role privileges")
            result["database_role"] = {"superuser": superuser, "createdb": createdb,
                                       "createrole": createrole}
            cursor.execute(
                "SELECT name, state FROM ir_module_module WHERE name=ANY(%s)", (modules,)
            )
            states = dict(cursor.fetchall())
            if states != dict.fromkeys(modules, "installed"):
                raise RuntimeError(f"Module installation mismatch: {states}")
            result["installed_modules"] = states
            cursor.execute("SELECT EXISTS(SELECT 1 FROM pg_available_extensions WHERE name='pg_trgm')")
            result["pg_trgm_available"] = cursor.fetchone()[0]
    base = f"http://127.0.0.1:{values['ODOO_PORT']}"
    client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    with client.open(base + "/web/login", timeout=30) as response:
        if response.status != 200:
            raise RuntimeError("Login page did not return HTTP 200")
        response.read()
    result["login_page_http"] = 200

    def rpc(path: str, params: dict):
        payload = json.dumps({"jsonrpc": "2.0", "method": "call", "params": params, "id": 1})
        request = urllib.request.Request(
            base + path, data=payload.encode(), headers={"Content-Type": "application/json"}
        )
        with client.open(request, timeout=30) as response:
            data = json.load(response)
        if data.get("error"):
            raise RuntimeError("Authenticated Odoo RPC check failed")
        return data["result"]

    session = rpc("/web/session/authenticate", {
        "db": values["DB_NAME"], "login": os.environ.get("ODOO_LOGIN", "admin"),
        "password": os.environ.get("ODOO_PASSWORD", "admin"),
    })
    if not session.get("uid"):
        raise RuntimeError("Odoo authentication returned no user")
    result["authenticated"] = True
    installed = rpc("/web/dataset/call_kw/ir.module.module/search_read", {
        "model": "ir.module.module", "method": "search_read",
        "args": [[["name", "=", "portfolio_trade"]]],
        "kwargs": {"fields": ["name", "state"], "limit": 1},
    })
    if not installed or installed[0]["state"] != "installed":
        raise RuntimeError("Installed custom module not readable through authenticated RPC")
    result["authenticated_module_read"] = True
    references = rpc("/web/dataset/call_kw/ir.model.data/search_read", {
        "model": "ir.model.data", "method": "search_read",
        "args": [[["module", "=", "portfolio_trade"]]],
        "kwargs": {"fields": ["name", "res_id", "model"]},
    })
    actions = rpc("/web/dataset/call_kw/ir.actions.act_window/read", {
        "model": "ir.actions.act_window", "method": "read",
        "args": [[reference["res_id"] for reference in references
                  if reference["model"] == "ir.actions.act_window"]],
        "kwargs": {"fields": ["res_model", "view_mode"]},
    })
    expected_models = {"sale.order", "purchase.order", "stock.picking", "account.move"}
    menus = rpc("/web/dataset/call_kw/ir.ui.menu/read", {
        "model": "ir.ui.menu", "method": "read",
        "args": [[reference["res_id"] for reference in references
                  if reference["model"] == "ir.ui.menu"]],
        "kwargs": {"fields": ["parent_id", "action"]},
    })
    models = rpc("/web/dataset/call_kw/ir.model/search_read", {
        "model": "ir.model", "method": "search_read",
        "args": [[["model", "in", sorted(expected_models)]]],
        "kwargs": {"fields": ["model"]},
    })
    result["verified_navigation_models"] = validate_navigation(
        references, actions, menus, {model["model"] for model in models}
    )
    output = settings.runtime / "m0-verification.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
