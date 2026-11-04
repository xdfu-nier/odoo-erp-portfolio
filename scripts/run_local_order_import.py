"""Run the isolated local order-import flow using existing private role credentials."""

from __future__ import annotations

import argparse
import configparser
import json
import sys
from pathlib import Path

from order_bridge_credentials import _read, validate_target
from prepare_order_bridge import _credential_path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / ".runtime" / "odoo.conf")
    parser.add_argument("--database", required=True)
    parser.add_argument("--allow-local-acceptance", action="store_true")
    parser.add_argument("--credentials", type=Path, default=ROOT / ".runtime" / "order-bridge-accounts.json")
    args = parser.parse_args()
    try:
        config = configparser.ConfigParser(interpolation=None)
        if not config.read(args.config):
            raise ValueError("Missing local configuration.")
        validate_target(args.database, config.get("options", "db_host", fallback=""), args.allow_local_acceptance)
        credentials = _read(_credential_path(args.credentials), args.database)
    except (configparser.Error, OSError, ValueError):
        parser.error("Invalid local import configuration, target or existing private credentials.")
    try:
        sys.path.insert(0, str(ROOT / ".upstream" / "odoo"))
        import odoo
        from odoo.modules.registry import Registry

        odoo.tools.config.parse_config([
            "-c", str(args.config.resolve()), "-d", args.database, "--no-http",
            "--addons-path", f"{ROOT / 'addons'},{ROOT / '.upstream' / 'odoo' / 'addons'}",
        ])
        with Registry(args.database).cursor() as cursor:
            from odoo.addons.portfolio_order_bridge.local_import_flow import run_local_import

            env = odoo.api.Environment(cursor, odoo.SUPERUSER_ID, {})
            report = run_local_import(
                env, {role: account["password"] for role, account in credentials["accounts"].items()},
                allow_local=True,
            )
            cursor.commit()
    except Exception:
        parser.error("Local order import failed; the flow was not committed.")
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
