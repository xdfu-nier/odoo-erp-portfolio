"""Native Odoo development, using a PostgreSQL wheel inside the virtualenv."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.pycache_prefix = str(ROOT / ".cache" / "pycache")

import argparse  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402

os.environ["PYTHONPYCACHEPREFIX"] = str(ROOT / ".cache" / "pycache")
os.environ["TMPDIR"] = str(ROOT / ".runtime" / "tmp")
(ROOT / ".runtime" / "tmp").mkdir(parents=True, exist_ok=True)

from runtime import Settings, initialize_env, write_odoo_config  # noqa: E402


def postgres_command(name: str) -> str:
    # pgserver 0.1.4 exports commands via __all__, not this constant.
    from pgserver._commands import POSTGRES_BIN_PATH

    return str(Path(POSTGRES_BIN_PATH) / name)


def start_database(settings: Settings) -> None:
    import psycopg2
    from psycopg2 import sql

    data = settings.runtime / "postgres"
    if not (data / "PG_VERSION").exists():
        password_file = settings.runtime / "initdb-password"
        with password_file.open("w") as handle:
            password_file.chmod(0o600)
            handle.write(settings.values["PGPASSWORD"])
        try:
            subprocess.run([
                postgres_command("initdb"), "-D", str(data),
                "-U", "portfolio_db_owner", "--auth-local=trust",
                "--auth-host=scram-sha-256", "--pwfile", str(password_file),
                "--encoding=UTF8", "--locale=C",
            ], check=True)
        finally:
            password_file.unlink(missing_ok=True)
    status = subprocess.run(
        [postgres_command("pg_ctl"), "-D", str(data), "status"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if status.returncode:
        subprocess.run([
            postgres_command("pg_ctl"), "-D", str(data),
            "-l", str(settings.runtime / "postgres.log"),
            "-o", f"-h 127.0.0.1 -p {settings.values['PGPORT']} -k ''", "-w", "start",
        ], check=True)
    with psycopg2.connect(
        host="127.0.0.1", port=settings.values["PGPORT"],
        user="portfolio_db_owner", password=settings.values["PGPASSWORD"],
        dbname="postgres",
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (settings.values["PGUSER"],))
            if not cursor.fetchone():
                cursor.execute(
                    sql.SQL("CREATE ROLE {} WITH LOGIN CREATEDB NOSUPERUSER PASSWORD %s").format(
                        sql.Identifier(settings.values["PGUSER"])
                    ),
                    (settings.values["PGPASSWORD"],),
                )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("configure", "init", "start", "update", "stop-db"))
    args = parser.parse_args()
    initialize_env(ROOT)
    settings = Settings.load(ROOT)
    config = write_odoo_config(settings)
    if args.command == "configure":
        print("Project-local .env and .runtime/odoo.conf are ready.")
        return 0
    if args.command == "stop-db":
        return subprocess.call([
            postgres_command("pg_ctl"), "-D", str(settings.runtime / "postgres"),
            "-m", "fast", "-w", "stop",
        ])
    start_database(settings)
    command = [sys.executable, str(ROOT / ".upstream/odoo/odoo-bin"), "-c", str(config)]
    if args.command == "init":
        command.extend(["-i", "portfolio_trade", "--without-demo=all", "--stop-after-init"])
    elif args.command == "update":
        command.extend(["-u", "portfolio_trade", "--stop-after-init"])
    else:
        command.append("--dev=xml")
        print(f"Open http://127.0.0.1:{settings.values['ODOO_PORT']}", flush=True)
    return subprocess.call(command, cwd=ROOT, env={**os.environ, "PYTHONUNBUFFERED": "1"})


if __name__ == "__main__":
    raise SystemExit(main())
