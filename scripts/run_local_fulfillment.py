"""Run the real-role fulfillment flow in an explicitly approved local database."""

from __future__ import annotations

import argparse
import configparser
import json
import sys
from pathlib import Path

from acceptance_credentials import validate_target

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / ".runtime" / "odoo.conf")
    parser.add_argument("--database", required=True)
    parser.add_argument("--allow-local-acceptance", action="store_true")
    parser.add_argument("--prepare-senders", action="store_true")
    args = parser.parse_args()
    config = configparser.ConfigParser(interpolation=None)
    try:
        if not config.read(args.config):
            raise ValueError("Local Odoo config does not exist; configure development first.")
        validate_target(args.database, config.get("options", "db_host", fallback=""), args.allow_local_acceptance)
    except (configparser.Error, OSError, ValueError) as error:
        parser.error("Invalid local Odoo configuration or target." if isinstance(error, configparser.Error) else str(error))
    sys.path.insert(0, str(ROOT / ".upstream" / "odoo"))
    import odoo
    from odoo.modules.registry import Registry

    odoo.tools.config.parse_config([
        "-c", str(args.config.resolve()), "-d", args.database, "--no-http",
        "--addons-path", f"{ROOT / 'addons'},{ROOT / '.upstream' / 'odoo' / 'addons'}",
    ])
    with Registry(args.database).cursor() as cursor:
        env = odoo.api.Environment(cursor, odoo.SUPERUSER_ID, {})
        actor = env["res.users"].with_user(env.uid).env.user
        if not actor.has_group("base.group_system"):
            raise PermissionError("Only actual Settings administrators can run local fulfillment.")
        company = env.ref("portfolio_trade_acceptance.company_a")
        if company.id not in actor.company_ids.ids:
            raise PermissionError("The fulfillment company is outside the administrator's actual companies.")
        env = env(context={"allowed_company_ids": company.ids})
        if args.prepare_senders:
            from odoo.addons.portfolio_trade.local_fulfillment import (
                prepare_local_fulfillment_senders,
            )

            result = prepare_local_fulfillment_senders(env, allow_local=True)
        else:
            from odoo.addons.portfolio_trade.local_fulfillment import run_local_fulfillment

            result = run_local_fulfillment(env, allow_local=True)
        cursor.commit()
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
