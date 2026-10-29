"""Prepare fake acceptance data explicitly in an allowlisted local Odoo database."""

from __future__ import annotations

import argparse
import configparser
import json
import sys
from pathlib import Path

from acceptance_credentials import ensure_credentials, validate_target

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / ".runtime" / "odoo.conf")
    parser.add_argument("--database", required=True)
    parser.add_argument("--allow-local-acceptance", action="store_true")
    parser.add_argument("--credentials", type=Path, default=ROOT / ".runtime" / "acceptance-accounts.json")
    args = parser.parse_args()
    config = configparser.ConfigParser(interpolation=None)
    if not config.read(args.config):
        parser.error("Local Odoo config does not exist; configure the development environment first.")
    try:
        validate_target(args.database, config.get("options", "db_host", fallback=""), args.allow_local_acceptance)
        credentials = ensure_credentials(args.credentials, args.database)
    except ValueError as error:
        parser.error(str(error))
    sys.path.insert(0, str(ROOT / ".upstream" / "odoo"))
    import odoo
    from odoo.modules.registry import Registry

    odoo.tools.config.parse_config([
        "-c", str(args.config.resolve()), "-d", args.database, "--no-http",
        "--addons-path", f"{ROOT / 'addons'},{ROOT / '.upstream' / 'odoo' / 'addons'}",
    ])
    with Registry(args.database).cursor() as cursor:
        from odoo.addons.portfolio_trade.acceptance_data import prepare_acceptance

        env = odoo.api.Environment(cursor, odoo.SUPERUSER_ID, {})
        result = prepare_acceptance(
            env, {role: account["password"] for role, account in credentials["accounts"].items()},
            allow_local=True,
        )
        cursor.commit()
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
