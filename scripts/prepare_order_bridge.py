"""Explicitly prepare the bounded local order bridge profile and nothing else."""

from __future__ import annotations

import argparse
import configparser
import json
import sys
from pathlib import Path

from order_bridge_credentials import _safe_path, ensure_credentials, validate_target

ROOT = Path(__file__).resolve().parents[1]


def _credential_path(path):
    path = _safe_path(path)
    roots = {ROOT / ".runtime"}
    # Feature worktrees share the existing project environment and config.
    environment = ROOT / ".venv"
    main = ROOT.parent.parent if ROOT.parent.name == ".worktrees" else ROOT
    if environment.exists() and environment.resolve().parent == main:
        roots.add(main / ".runtime")
    if not any(path.is_relative_to(root) for root in roots):
        raise ValueError("Private bridge credentials must stay in a project runtime directory.")
    return path


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
        private_path = _credential_path(args.credentials)
    except (configparser.Error, OSError, ValueError):
        parser.error("Invalid local bridge configuration, target or credential path.")
    try:
        credentials = ensure_credentials(private_path, args.database)
        sys.path.insert(0, str(ROOT / ".upstream" / "odoo"))
        import odoo
        from odoo.modules.registry import Registry

        odoo.tools.config.parse_config([
            "-c", str(args.config.resolve()), "-d", args.database, "--no-http",
            "--addons-path", f"{ROOT / 'addons'},{ROOT / '.upstream' / 'odoo' / 'addons'}",
        ])
        with Registry(args.database).cursor() as cursor:
            from odoo.addons.portfolio_order_bridge.local_acceptance import prepare_local_import

            env = odoo.api.Environment(cursor, odoo.SUPERUSER_ID, {})
            report = prepare_local_import(
                env, {role: account["password"] for role, account in credentials["accounts"].items()},
                allow_local=True,
            )
            cursor.commit()
    except Exception:
        # ORM and credential exceptions can contain submitted private values.
        parser.error("Local bridge preparation failed; the profile was not committed.")
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
