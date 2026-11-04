"""Guarded native test actor: exit after a real quotation flush, before commit.

The stdlib-only target validation intentionally precedes every Odoo import.
This is a test helper, never a service or a general-purpose Odoo command.
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

DATABASE = "odoo_portfolio_m1_test"
PROJECT = Path(__file__).resolve().parents[3]
MAIN_PROJECT = PROJECT.parents[1] if PROJECT.parent.name == ".worktrees" else PROJECT


class TargetRejected(ValueError):
    """Invalid target; its potentially sensitive input is never echoed."""


class QuietParser(argparse.ArgumentParser):
    def error(self, message):
        raise TargetRejected("invalid arguments")


@dataclass(frozen=True)
class Target:
    config: Path
    witness: Path
    database: str
    uid: int
    company_id: int
    source_id: int
    job_id: int


def validate_target(argv, *, project=PROJECT, main_project=MAIN_PROJECT):
    parser = QuietParser(add_help=False, allow_abbrev=False)
    for name in ("config", "witness", "database"):
        parser.add_argument(f"--{name}", required=True)
    for name in ("uid", "company-id", "source-id", "job-id"):
        parser.add_argument(f"--{name}", type=int, required=True)
    args = parser.parse_args(argv)
    if args.database != DATABASE or args.uid <= 1 or any(
        value <= 0 for value in (args.company_id, args.source_id, args.job_id)
    ):
        raise TargetRejected("invalid target")
    project, main_project = project.resolve(), main_project.resolve()
    config = Path(args.config)
    allowed = {root / ".runtime" / "odoo.conf" for root in (project, main_project)}
    if (
        not config.is_absolute() or config not in allowed or config != config.resolve()
        or not config.is_file() or config.is_symlink()
    ):
        raise TargetRejected("invalid configuration path")
    options = configparser.ConfigParser(interpolation=None)
    try:
        with config.open(encoding="utf-8") as handle:
            options.read_file(handle)
        host = options.get("options", "db_host")
        port = options.getint("options", "db_port")
    except (OSError, ValueError, configparser.Error):
        raise TargetRejected("invalid configuration") from None
    if host != "127.0.0.1" or not 1024 <= port <= 65535:
        raise TargetRejected("nonlocal database")
    if any(options.get("options", name, fallback="").strip().lower() not in {"", "false", "0", "none"}
           for name in ("init", "update", "test_enable", "test_tags")):
        raise TargetRejected("unsafe configuration mode")
    witness = Path(args.witness)
    directory = project / ".runtime" / "tmp"
    if (
        not witness.is_absolute() or witness.parent != directory
        or not re.fullmatch(r"worker-exit-[0-9a-f]{32}\.json", witness.name)
        or witness != witness.resolve() or os.path.lexists(witness)
    ):
        raise TargetRejected("invalid witness path")
    for parent in (project / ".runtime", directory):
        if (
            not parent.is_dir() or parent.is_symlink() or parent.resolve() != parent
            or parent.stat().st_uid != os.getuid()
        ):
            raise TargetRejected("invalid witness directory")
    return Target(config, witness, args.database, args.uid, args.company_id, args.source_id, args.job_id)


def write_witness(path, values):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        if stat.S_IMODE(os.fstat(handle.fileno()).st_mode) != 0o600:
            raise TargetRejected("invalid witness permissions")
        json.dump(values, handle, sort_keys=True, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())


def run_native(target):
    # Fixed repository paths only; CLI cannot inject Python modules or addons.
    upstream = (MAIN_PROJECT / ".upstream" / "odoo").resolve()
    if (PROJECT / ".upstream" / "odoo").resolve() != upstream:
        raise TargetRejected("invalid upstream")
    sys.pycache_prefix = str(PROJECT / ".cache" / "pycache")
    os.environ["PYTHONPYCACHEPREFIX"] = sys.pycache_prefix
    os.environ["TMPDIR"] = str(PROJECT / ".runtime" / "tmp")
    sys.path.insert(0, str(upstream))
    from unittest.mock import patch

    from odoo import api
    from odoo.modules.registry import Registry
    from odoo.tools import config

    config.parse_config([
        "-c", str(target.config), "-d", target.database,
        "--addons-path", f"{upstream / 'addons'},{PROJECT / 'addons'}",
        "--no-http", "--max-cron-threads=0",
    ])
    registry = Registry(target.database)
    with registry.cursor() as cr:
        env = api.Environment(cr, target.uid, {"allowed_company_ids": [target.company_id], "lang": "en_US"})
        model = env["portfolio.import.job"]
        model._worker_actor()
        job = model.browse(target.job_id)._worker_authorized_job()
        if (
            env.su or env.uid != target.uid or env.company.id != target.company_id
            or job.source_id.id != target.source_id or job.state != "pending"
            or job.attempt_count or job.last_attempt_at or job.sale_order_id
            or model.search([("state", "=", "pending")]).ids != [job.id]
        ):
            raise TargetRejected("invalid native fixture")
        sale_class = type(env["sale.order"])
        real_create = sale_class.create

        @api.model_create_multi
        def exit_after_real_create(records, values):
            quotes = real_create(records, values)
            env.flush_all()
            job.invalidate_recordset()
            if (
                records.env.uid != target.uid or records.env.su or len(quotes) != 1
                or quotes.company_id.id != target.company_id or len(quotes.order_line) != 2
                or quotes.create_uid.id != target.uid
                or quotes.order_line.mapped("create_uid").ids != [target.uid]
                or job.state != "processing" or job.attempt_count != 1
                or not job.last_attempt_at or job.sale_order_id
            ):
                raise TargetRejected("invalid native checkpoint")
            write_witness(target.witness, {
                "stage": "native_quote_flushed_before_commit", "uid": records.env.uid,
                "su": records.env.su, "company_id": target.company_id,
                "source_id": target.source_id, "job_id": job.id, "quote_id": quotes.id,
                "line_ids": quotes.order_line.ids, "processing": job.state == "processing",
                "attempt_count": job.attempt_count, "last_attempt": bool(job.last_attempt_at),
                "linked_quote": bool(job.sale_order_id),
            })
            # Deliberately bypass cursor cleanup, rollback handlers and atexit.
            os._exit(73)

        with patch.object(sale_class, "create", exit_after_real_create):
            model._process_pending()
    raise TargetRejected("checkpoint not reached")


def main(argv=None):
    try:
        target = validate_target(sys.argv[1:] if argv is None else argv)
    except (TargetRejected, OSError):
        return 64
    try:
        run_native(target)
    except BaseException:
        # Parent captures both streams; neither config nor native exception text
        # may become test output even when this probe fails unexpectedly.
        return 74
    return 74


if __name__ == "__main__":
    sys.exit(main())
