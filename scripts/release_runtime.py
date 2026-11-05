"""Private, project-local boundaries shared by release commands (stdlib on import)."""

from __future__ import annotations

import argparse
import configparser
import contextlib
import gzip
import hashlib
import json
import os
import re
import stat
import tarfile
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
DATABASE = "odoo_portfolio"
ODOO_REVISION = "98e48692891fc9a3aaa7f53001305b322f53cfa2"
MODULE_VERSIONS = {"portfolio_trade": "18.0.1.5.0", "portfolio_order_bridge": "18.0.1.3.0"}
PG_OPTIONS = "-c search_path=pg_catalog,public -c statement_timeout=120000"
MAX_CONFIG = 64 * 1024
MAX_MANIFEST = 32 * 1024 * 1024
MAX_REPORT = 1024 * 1024
MAX_FILES = 100000
MAX_EXPANDED = 1024 * 1024 * 1024
ERROR_CODES = frozenset({
    "release_failed", "invalid_arguments", "invalid_target", "invalid_config", "unsafe_path",
    "unsafe_file", "invalid_manifest", "integrity_failed", "source_busy", "source_changed",
    "database_failed", "invalid_source", "archive_failed", "dump_failed", "provenance_failed",
    "output_exists", "opt_in_required",
})
IDENTITIES = {
    "portfolio_trade_acceptance": {
        "company_a": "res.company", "company_b": "res.company", "warehouse_a": "stock.warehouse",
        "customer": "res.partner", "vendor": "res.partner", "initial_bearing_move": "stock.move",
        **{f"user_{r}": "res.users" for r in (
            "sales", "manager", "manager2", "buyer", "warehouse", "finance", "other_sales")},
        **{f"product_{k}": "product.product" for k in ("bearing", "sensor", "bolt")},
        **{f"supplier_{k}": "product.supplierinfo" for k in ("bearing", "sensor", "bolt")},
    },
    "portfolio_trade_fulfillment": {
        "sale_order": "sale.order", "purchase_order": "purchase.order",
        **{k: "stock.picking" for k in (
            "receipt_first", "receipt_second", "delivery_first", "delivery_second", "return_picking")},
        **{k: "account.move" for k in ("invoice_first", "invoice_second", "credit_note")},
    },
    "portfolio_order_bridge_acceptance": {
        **{f"user_{r}": "res.users" for r in ("settings", "operator", "integration")},
        "customer": "res.partner", "vendor": "res.partner", "source": "portfolio.import.source",
        **{f"product_item{i}": "product.product" for i in (1, 2)},
        **{f"template_item{i}": "product.template" for i in (1, 2)},
        **{f"supplier_item{i}": "product.supplierinfo" for i in (1, 2)},
        "customer_map": "portfolio.import.customer.map",
        **{f"product_map_item{i}": "portfolio.import.product.map" for i in (1, 2)},
        "processing_cron": "ir.cron", "processing_server_action": "ir.actions.server",
    },
    "portfolio_order_bridge_flow": {
        "job_success": "portfolio.import.job", "job_repaired": "portfolio.import.job",
        "quote_success": "sale.order", "quote_repaired": "sale.order",
        **{f"attempt_{k}": "portfolio.import.attempt" for k in ("success", "failed", "retry", "repaired")},
        "product_map_repair": "portfolio.import.product.map",
    },
}
GOLDEN = {"stock": 10, "delivered": 100, "returned": 10, "net": 90, "net_amount": 7650}


class ReleaseError(Exception):
    """Only fixed public classifications may cross the private boundary."""

    def __init__(self, code="release_failed"):
        self.code = code if type(code) is str and code in ERROR_CODES else "release_failed"
        super().__init__(self.code)


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ReleaseError("invalid_arguments")


@dataclass(frozen=True)
class LocalConfig:
    path: Path
    database: str
    db_host: str
    db_port: int
    db_user: str
    db_password: str = field(repr=False)
    http_port: int = 8069
    data_dir: Path = ROOT / ".runtime" / "filestore"


def runtime_roots():
    roots = [ROOT / ".runtime"]
    if ROOT.parent.name == ".worktrees":
        main = ROOT.parent.parent
        # Only the established three shared tool links identify this project.
        if all((ROOT / name).is_symlink() and (ROOT / name).resolve() == main / name
               for name in (".venv", ".tools", ".upstream")):
            roots.append(main / ".runtime")
    return tuple(roots)


def runtime_path(path, existing=False):
    try:
        path = Path(path)
        if not path.is_absolute() or any(part in (".", "..") for part in path.parts):
            raise ReleaseError("unsafe_path")
        if not any(path.is_relative_to(root) for root in runtime_roots()):
            raise ReleaseError("unsafe_path")
        for component in (path, *path.parents):
            if component.is_symlink():
                raise ReleaseError("unsafe_path")
        if existing and not path.exists():
            raise ReleaseError("unsafe_path")
        return path
    except (OSError, TypeError, ValueError):
        raise ReleaseError("unsafe_path") from None


def private_file(path, limit=None):
    path = runtime_path(path, existing=True)
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.getuid() or info.st_nlink != 1
                or (limit is not None and info.st_size > limit)):
            raise ReleaseError("unsafe_file")
        return path
    except OSError:
        raise ReleaseError("unsafe_file") from None


def read_private(path, limit):
    path = private_file(path, limit)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_uid != os.getuid() or info.st_nlink != 1):
                raise ReleaseError("unsafe_file")
            content = handle.read(limit + 1)
        if len(content) > limit:
            raise ReleaseError("unsafe_file")
        return content
    except OSError:
        raise ReleaseError("unsafe_file") from None


def exclusive_private(path):
    path = runtime_path(path)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.fchmod(descriptor, 0o600)
        return os.fdopen(descriptor, "wb")
    except FileExistsError:
        raise ReleaseError("output_exists") from None
    except OSError:
        raise ReleaseError("unsafe_file") from None


def load_local_config(config_path):
    path = runtime_path(config_path, existing=True)
    try:
        config = configparser.ConfigParser(interpolation=None)
        config.read_string(read_private(path, MAX_CONFIG).decode("utf-8"))
        values = config["options"]
        if (values["db_host"] != "127.0.0.1" or values["db_name"] != DATABASE
                or values["db_port"] != "55432" or values["http_port"] != "8069"
                or values.get("http_interface") != "127.0.0.1"
                or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", values["db_user"])
                or values["db_user"] in {"postgres", "portfolio_db_owner"}
                or not values["db_password"].strip()):
            raise ReleaseError("invalid_config")
        data = runtime_path(values["data_dir"], existing=True)
        if not data.is_dir():
            raise ReleaseError("invalid_config")
        return LocalConfig(path, DATABASE, "127.0.0.1", 55432, values["db_user"],
                           values["db_password"], 8069, data)
    except (KeyError, UnicodeError, ValueError, configparser.Error, OSError):
        raise ReleaseError("invalid_config") from None


def pg_environment(config, parent_env):
    result = {key: value for key, value in parent_env.items() if not key.startswith("PG")}
    result.update(PGHOST="127.0.0.1", PGHOSTADDR="127.0.0.1", PGPORT=str(config.db_port),
                  PGUSER=config.db_user, PGPASSWORD=config.db_password, PGDATABASE=config.database,
                  PGCONNECT_TIMEOUT="5", PGSSLMODE="disable", PGOPTIONS=PG_OPTIONS)
    return result


@contextlib.contextmanager
def isolated_pg_environment():
    """Native libpq/Odoo imports cannot inherit service or address overrides."""
    inherited = {key: value for key, value in os.environ.items() if key.startswith("PG")}
    for key in inherited:
        del os.environ[key]
    try:
        yield
    finally:
        for key in list(os.environ):
            if key.startswith("PG"):
                del os.environ[key]
        os.environ.update(inherited)


def target_database(database, purpose=None):
    if database == DATABASE and purpose is None:
        return database
    prefix = {"restore": "restore", "benchmark": "bench"}.get(purpose)
    if prefix and type(database) is str and re.fullmatch(
            rf"odoo_portfolio_{prefix}_[0-9]{{8}}_[0-9a-f]{{8}}", database):
        return database
    raise ReleaseError("invalid_target")



def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False, default=_json_default).encode("utf-8")


def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (bytes, memoryview)):
        return bytes(value).hex()
    raise TypeError


def digest(value):
    return hashlib.sha256(canonical_json(value)).hexdigest()


def file_digest(path):
    path = private_file(path)
    result, size = hashlib.sha256(), 0
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                result.update(chunk)
                size += len(chunk)
        return {"path": path.name, "size": size, "sha256": result.hexdigest()}
    except OSError:
        raise ReleaseError("integrity_failed") from None


def safe_relative(value):
    if (type(value) is not str or not 1 <= len(value) <= 256 or "\\" in value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or value.startswith("/") or any(part in ("", ".", "..") for part in value.split("/"))):
        raise ReleaseError("invalid_manifest")
    return PurePosixPath(value)


def _keys(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise ReleaseError("invalid_manifest")


def _hash(value, length=64):
    if type(value) is not str or not re.fullmatch(rf"[0-9a-f]{{{length}}}", value):
        raise ReleaseError("invalid_manifest")


def _integer(value):
    if type(value) is not int or value < 0:
        raise ReleaseError("invalid_manifest")


def strict_json(content):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ReleaseError("invalid_manifest")
            result[key] = value
        return result

    try:
        return json.loads(content, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ReleaseError("invalid_manifest")))
    except (ValueError, UnicodeError, RecursionError):
        raise ReleaseError("invalid_manifest") from None


def validate_snapshot(snapshot):
    _keys(snapshot, {"schema_version", "database", "namespaces", "module_versions", "role_fingerprint",
                     "policy_fingerprint", "business_fingerprint", "attachment_fingerprint", "attachments", "golden"})
    if (type(snapshot["schema_version"]) is not int or snapshot["schema_version"] != 1
            or snapshot["database"] != DATABASE or snapshot["module_versions"] != MODULE_VERSIONS
            or snapshot["golden"] != GOLDEN):
        raise ReleaseError("invalid_manifest")
    _keys(snapshot["golden"], GOLDEN)
    if any(type(v) not in (int, float) for v in snapshot["golden"].values()):
        raise ReleaseError("invalid_manifest")
    _keys(snapshot["namespaces"], IDENTITIES)
    for namespace, expected in IDENTITIES.items():
        rows = snapshot["namespaces"][namespace]
        if type(rows) is not list or len(rows) != len(expected):
            raise ReleaseError("invalid_manifest")
        for row, (name, model) in zip(rows, sorted(expected.items()), strict=True):
            _keys(row, {"name", "model", "id"})
            if row["name"] != name or row["model"] != model or type(row["id"]) is not int or row["id"] < 1:
                raise ReleaseError("invalid_manifest")
    for name in ("role_fingerprint", "policy_fingerprint", "business_fingerprint", "attachment_fingerprint"):
        _hash(snapshot[name])
    if type(snapshot["attachments"]) is not list or len(snapshot["attachments"]) > MAX_FILES:
        raise ReleaseError("invalid_manifest")
    previous = 0
    for attachment in snapshot["attachments"]:
        _keys(attachment, {"id", "type", "store_fname", "checksum", "file_size", "db_sha256", "url_sha256"})
        _integer(attachment["id"])
        _integer(attachment["file_size"])
        if attachment["id"] <= previous or attachment["type"] not in {"binary", "url"}:
            raise ReleaseError("invalid_manifest")
        previous = attachment["id"]
        if attachment["store_fname"] is not None:
            safe_relative(attachment["store_fname"])
        if attachment["checksum"] is not None:
            _hash(attachment["checksum"], 40)
        for name in ("db_sha256", "url_sha256"):
            if attachment[name] is not None:
                _hash(attachment[name])


class _ExpansionReader:
    """Enforce the decompressed budget before any read reaches gzip."""

    def __init__(self, stream):
        self.stream = stream
        self.remaining = MAX_EXPANDED

    def read(self, size):
        if type(size) is not int or size < 0 or size > self.remaining:
            raise ReleaseError("invalid_manifest")
        content = self.stream.read(size)
        self.remaining -= len(content)
        return content

    def finish(self):
        # Tar parsers stop at an EOF header; compressed padding/trailing bytes
        # still consume the archive budget. A one-byte EOF probe handles an
        # expansion exactly at the limit without allocating an oversized buffer.
        while self.remaining:
            if not self.read(min(64 * 1024, self.remaining)):
                return
        if self.stream.read(1):
            raise ReleaseError("invalid_manifest")


@contextlib.contextmanager
def _bounded_tar(path):
    with os.fdopen(os.open(private_file(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as compressed, \
            gzip.GzipFile(fileobj=compressed, mode="rb") as decompressed:
        reader = _ExpansionReader(decompressed)
        headers = 0

        class BoundedTarInfo(tarfile.TarInfo):
            @classmethod
            def frombuf(cls, buffer, encoding, errors):
                nonlocal headers
                member = super().frombuf(buffer, encoding, errors)
                headers += 1
                # GNU/PAX bodies are consumed before ordinary members yield.
                # Bound both physical header count and body reads at this point.
                if (headers > MAX_FILES or member.size < 0
                        or member._block(member.size) > reader.remaining):
                    raise ReleaseError("invalid_manifest")
                return member

        with tarfile.open(fileobj=reader, mode="r|", bufsize=tarfile.BLOCKSIZE,
                          tarinfo=BoundedTarInfo) as archive:
            yield archive
        reader.finish()


def archive_inventory(path):
    """Read bounded ordinary members; callers never extract untrusted paths."""
    files, seen, total = [], set(), 0
    prefix = "filestore/" + DATABASE
    try:
        with _bounded_tar(path) as archive:
            for index, member in enumerate(archive):
                if index >= MAX_FILES:
                    raise ReleaseError("invalid_manifest")
                safe_relative(member.name)
                if (member.name in seen or (member.name != prefix and not member.name.startswith(prefix + "/"))
                        or not (member.isfile() or member.isdir()) or member.size < 0):
                    raise ReleaseError("invalid_manifest")
                seen.add(member.name)
                if member.isdir():
                    if member.size:
                        raise ReleaseError("invalid_manifest")
                    continue
                if member.name == prefix:
                    raise ReleaseError("invalid_manifest")
                relative = member.name[len(prefix) + 1:]
                safe_relative(relative)
                total += member.size
                if total > MAX_EXPANDED:
                    raise ReleaseError("invalid_manifest")
                result, length = hashlib.sha256(), 0
                with archive.extractfile(member) as handle:
                    while chunk := handle.read(1024 * 1024):
                        length += len(chunk)
                        result.update(chunk)
                if length != member.size:
                    raise ReleaseError("integrity_failed")
                files.append({"path": relative, "size": length, "sha256": result.hexdigest()})
        return sorted(files, key=lambda item: item["path"])
    except (OSError, tarfile.TarError, EOFError, RecursionError):
        raise ReleaseError("archive_failed") from None


def read_manifest(backup_dir):
    backup = runtime_path(backup_dir, existing=True)
    info = backup.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.getuid():
        raise ReleaseError("unsafe_file")
    manifest = strict_json(read_private(backup / "manifest.json", MAX_MANIFEST))
    _keys(manifest, {"schema_version", "database", "created_at", "provenance", "source_snapshot",
                     "source_snapshot_sha256", "dump", "archive", "files", "restore_verified"})
    if (type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
            or manifest["database"] != DATABASE or manifest["restore_verified"] is not False
            or type(manifest["created_at"]) is not str
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", manifest["created_at"])):
        raise ReleaseError("invalid_manifest")
    provenance = manifest["provenance"]
    _keys(provenance, {"git_commit", "addons_sha256", "upstream_sha256", "odoo_revision", "python_version",
                       "postgres_version", "module_versions"})
    _hash(provenance["git_commit"], 40)
    _hash(provenance["addons_sha256"])
    _hash(provenance["upstream_sha256"])
    if (provenance["odoo_revision"] != ODOO_REVISION or provenance["module_versions"] != MODULE_VERSIONS
            or type(provenance["python_version"]) is not str or not re.fullmatch(r"3\.12\.\d+", provenance["python_version"])
            or type(provenance["postgres_version"]) is not str or not re.match(r"16(?:\.|$)", provenance["postgres_version"])):
        raise ReleaseError("invalid_manifest")
    validate_snapshot(manifest["source_snapshot"])
    _hash(manifest["source_snapshot_sha256"])
    if digest(manifest["source_snapshot"]) != manifest["source_snapshot_sha256"]:
        raise ReleaseError("integrity_failed")
    files = manifest["files"]
    if type(files) is not list or len(files) > MAX_FILES:
        raise ReleaseError("invalid_manifest")
    paths = []
    for item in files:
        _keys(item, {"path", "size", "sha256"})
        safe_relative(item["path"])
        _integer(item["size"])
        _hash(item["sha256"])
        paths.append(item["path"])
    if paths != sorted(set(paths)):
        raise ReleaseError("invalid_manifest")
    for key, filename in (("dump", "database.dump"), ("archive", "filestore.tar.gz")):
        item = manifest[key]
        _keys(item, {"path", "size", "sha256"})
        _integer(item["size"])
        _hash(item["sha256"])
        if item["path"] != filename or item["size"] == 0 or file_digest(backup / filename) != item:
            raise ReleaseError("integrity_failed")
    if archive_inventory(backup / "filestore.tar.gz") != files:
        raise ReleaseError("integrity_failed")
    return manifest


def source_snapshot(config, database=None, purpose=None):
    from release_snapshot import source_snapshot as collect

    return collect(config, database, purpose)
