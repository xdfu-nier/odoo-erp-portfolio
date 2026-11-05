"""Pure contracts for private release input and backup integrity boundaries."""

import gzip
import hashlib
import importlib.util
import io
import json
import os
import stat
import sys
import tarfile
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
ODOO_REVISION = "98e48692891fc9a3aaa7f53001305b322f53cfa2"
MODULES = {"portfolio_trade": "18.0.1.5.0", "portfolio_order_bridge": "18.0.1.3.0"}
IDENTITIES = {
    "portfolio_trade_acceptance": {
        "company_a": "res.company", "company_b": "res.company", "warehouse_a": "stock.warehouse",
        "customer": "res.partner", "vendor": "res.partner", "initial_bearing_move": "stock.move",
        **{f"user_{role}": "res.users" for role in (
            "sales", "manager", "manager2", "buyer", "warehouse", "finance", "other_sales")},
        **{f"product_{key}": "product.product" for key in ("bearing", "sensor", "bolt")},
        **{f"supplier_{key}": "product.supplierinfo" for key in ("bearing", "sensor", "bolt")},
    },
    "portfolio_trade_fulfillment": {
        "sale_order": "sale.order", "purchase_order": "purchase.order",
        **{key: "stock.picking" for key in (
            "receipt_first", "receipt_second", "delivery_first", "delivery_second", "return_picking")},
        **{key: "account.move" for key in ("invoice_first", "invoice_second", "credit_note")},
    },
    "portfolio_order_bridge_acceptance": {
        **{f"user_{role}": "res.users" for role in ("settings", "operator", "integration")},
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
        **{f"attempt_{key}": "portfolio.import.attempt" for key in ("success", "failed", "retry", "repaired")},
        "product_map_repair": "portfolio.import.product.map",
    },
}


def sha256(content):
    return hashlib.sha256(content).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def load_script(name):
    script = ROOT / "scripts" / f"{name}.py"
    if not script.is_file():
        raise AssertionError(f"Missing release implementation: {name}.py")
    spec = importlib.util.spec_from_file_location(name, script)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class ReleaseFixture(unittest.TestCase):
    def setUp(self):
        self.runtime = load_script("release_runtime")
        base = ROOT / ".runtime" / "tests"
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.directory.chmod(0o700)
        self.config = self.directory / "odoo.conf"
        self.data = self.directory / "data"
        self.data.mkdir(mode=0o700)
        self.secret = "private-password-must-never-appear"
        self.write_config()

    def write_config(self, **overrides):
        values = {"db_host": "127.0.0.1", "db_port": "55432", "db_user": "odoo",
                  "db_password": self.secret, "db_name": "odoo_portfolio",
                  "http_interface": "127.0.0.1", "http_port": "8069", "data_dir": str(self.data)}
        values.update(overrides)
        self.config.write_text("[options]\n" + "".join(f"{k} = {v}\n" for k, v in values.items()))
        self.config.chmod(0o600)

    def assert_rejected(self, operation):
        with self.assertRaises(self.runtime.ReleaseError) as error:
            operation()
        self.assertRegex(str(error.exception), r"^[a-z][a-z0-9_]{0,63}$")
        self.assertNotIn(self.secret, str(error.exception))

    def backup_fixture(self):
        backup = self.directory / "backup"
        backup.mkdir(mode=0o700)
        payload = b"synthetic opaque filestore content"
        dump = backup / "database.dump"
        dump.write_bytes(b"PGDMP synthetic private data")
        dump.chmod(0o600)
        tree = self.directory / "source-files"
        tree.mkdir(exist_ok=True)
        (tree / "ab").mkdir(exist_ok=True)
        (tree / "ab" / "ordinary").write_bytes(payload)
        archive = backup / "filestore.tar.gz"
        with tarfile.open(archive, "w:gz") as handle:
            handle.add(tree, arcname="filestore/odoo_portfolio")
        archive.chmod(0o600)
        # Private snapshots bind original identity/audit/configuration digests;
        # neither these data nor this fixture are public result schemas.
        snapshot = {
            "schema_version": 1, "database": "odoo_portfolio", "namespaces": {
                ns: [{"name": name, "model": model, "id": i + 1}
                     for i, (name, model) in enumerate(sorted(identities.items()))]
                for ns, identities in IDENTITIES.items()
            },
            "module_versions": MODULES, "role_fingerprint": "1" * 64,
            "policy_fingerprint": "2" * 64, "business_fingerprint": "3" * 64,
            "attachment_fingerprint": "4" * 64, "attachments": [],
            "golden": {"stock": 10, "delivered": 100, "returned": 10, "net": 90, "net_amount": 7650},
        }
        manifest = {
            "schema_version": 1, "database": "odoo_portfolio", "created_at": "2026-11-05T00:00:00Z",
            "provenance": {"git_commit": "a" * 40, "addons_sha256": "b" * 64,
                           "upstream_sha256": "c" * 64,
                           "odoo_revision": ODOO_REVISION, "python_version": "3.12.12",
                           "postgres_version": "16.4", "module_versions": MODULES},
            "source_snapshot": snapshot, "source_snapshot_sha256": sha256(canonical(snapshot)),
            "dump": {"path": "database.dump", "size": dump.stat().st_size,
                     "sha256": sha256(dump.read_bytes())},
            "archive": {"path": "filestore.tar.gz", "size": archive.stat().st_size,
                        "sha256": sha256(archive.read_bytes())},
            "files": [{"path": "ab/ordinary", "size": len(payload), "sha256": sha256(payload)}],
            "restore_verified": False,
        }
        self.write_manifest(backup, manifest)
        return backup, manifest

    def write_manifest(self, backup, manifest):
        path = backup / "manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        path.chmod(0o600)


class ReleaseRuntimeTests(ReleaseFixture):
    def test_private_config_round_trips_without_disclosing_password_in_repr(self):
        config = self.runtime.load_local_config(self.config)
        self.assertEqual(config.database, "odoo_portfolio")
        self.assertEqual(config.db_host, "127.0.0.1")
        self.assertEqual(config.db_port, 55432)
        self.assertEqual(config.http_port, 8069)
        self.assertEqual(config.data_dir, self.data)
        self.assertNotIn(self.secret, repr(config))

    def test_wrong_hosts_ports_database_and_privileged_role_names_fail_closed(self):
        for key, values in {"db_host": ("localhost", "::1", "remote.invalid", ""),
                            "db_port": ("5432", "55433", "55432.0", "true"),
                            "http_interface": ("0.0.0.0", "localhost"),
                            "http_port": ("8070", "0"),
                            "db_name": ("production", "odoo_portfolio_m1_test", ""),
                            "db_user": ("postgres", "portfolio_db_owner", "user;SELECT 1")}.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    self.write_config(**{key: value})
                    self.assert_rejected(lambda: self.runtime.load_local_config(self.config))

    def test_config_is_regular_current_owner_exact_0600_without_repair(self):
        for mode in (0o400, 0o640, 0o644, 0o700):
            self.config.chmod(mode)
            before = self.config.read_bytes()
            self.assert_rejected(lambda: self.runtime.load_local_config(self.config))
            self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), mode)
            self.assertEqual(self.config.read_bytes(), before)
        self.config.chmod(0o600)
        with patch.object(self.runtime.os, "getuid", return_value=os.getuid() + 1):
            self.assert_rejected(lambda: self.runtime.load_local_config(self.config))

    def test_config_rejects_symlink_hardlink_fifo_and_directory_without_blocking(self):
        target = self.directory / "saved.conf"
        self.config.rename(target)
        self.config.symlink_to(target)
        self.assert_rejected(lambda: self.runtime.load_local_config(self.config))
        self.config.unlink()
        os.link(target, self.config)
        self.assert_rejected(lambda: self.runtime.load_local_config(self.config))
        self.config.unlink()
        os.mkfifo(self.config, 0o600)
        self.assert_rejected(lambda: self.runtime.load_local_config(self.config))
        self.config.unlink()
        self.config.mkdir()
        self.assert_rejected(lambda: self.runtime.load_local_config(self.config))

    def test_config_limit_missing_required_keys_duplicates_and_invalid_encoding_reject(self):
        for content in (b"x" * (64 * 1024 + 1), b"[options]\ndb_host=127.0.0.1\n",
                        b"[options]\ndb_host=127.0.0.1\ndb_host=127.0.0.1\n", b"\xffsecret"):
            self.config.write_bytes(content)
            self.assert_rejected(lambda: self.runtime.load_local_config(self.config))

    def test_paths_require_real_runtime_components_and_never_create_targets(self):
        target = self.directory / "not-created" / "backup"
        self.assertEqual(self.runtime.runtime_path(target), target)
        self.assertFalse(target.parent.exists())
        self.assertEqual(self.runtime.runtime_path(self.data, existing=True), self.data)
        for path in (ROOT / "addons" / "output", self.directory / ".." / "escape", Path(".runtime/test")):
            self.assert_rejected(lambda path=path: self.runtime.runtime_path(path))
        self.assert_rejected(lambda: self.runtime.runtime_path(target, existing=True))

    def test_symlink_components_and_runtime_root_symlinks_are_not_data_roots(self):
        link = self.directory / "linked"
        link.symlink_to(self.data, target_is_directory=True)
        self.assert_rejected(lambda: self.runtime.runtime_path(link / "new"))
        self.write_config(data_dir=link)
        self.assert_rejected(lambda: self.runtime.load_local_config(self.config))

    def test_main_runtime_is_allowed_only_for_verified_project_shared_worktree(self):
        if ROOT.parent.name != ".worktrees":
            self.skipTest("main/shared worktree distinction requires project worktree")
        main = ROOT.parent.parent
        self.assertEqual(self.runtime.runtime_path(main / ".runtime" / "new-release"),
                         main / ".runtime" / "new-release")
        self.assert_rejected(lambda: self.runtime.runtime_path(main.parent / ".runtime" / "new-release"))

    def test_hostile_inherited_pg_environment_is_completely_replaced(self):
        config = self.runtime.load_local_config(self.config)
        parent = {"PATH": "/synthetic/tools", "LANG": "C", "PGHOST": "remote.invalid",
                  "PGHOSTADDR": "203.0.113.1", "PGPORT": "5432", "PGUSER": "postgres",
                  "PGPASSWORD": "inherited-password", "PGDATABASE": "production",
                  "PGSERVICE": "remote", "PGSERVICEFILE": "/private/service", "PGOPTIONS": "-c role=postgres",
                  "PGPASSFILE": "/private/pass", "PGSSLMODE": "require", "PGEXTRA_ATTACK": "x"}
        before = parent.copy()
        result = self.runtime.pg_environment(config, parent)
        self.assertEqual(parent, before)
        self.assertEqual({k: v for k, v in result.items() if not k.startswith("PG")},
                         {"PATH": "/synthetic/tools", "LANG": "C"})
        self.assertEqual({k for k in result if k.startswith("PG")},
                         {"PGHOST", "PGHOSTADDR", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE",
                          "PGCONNECT_TIMEOUT", "PGOPTIONS", "PGSSLMODE"})
        self.assertEqual(result["PGHOST"], "127.0.0.1")
        self.assertEqual(result["PGHOSTADDR"], "127.0.0.1")
        self.assertEqual(result["PGPORT"], "55432")
        self.assertEqual(result["PGUSER"], "odoo")
        self.assertEqual(result["PGPASSWORD"], self.secret)
        self.assertEqual(result["PGDATABASE"], "odoo_portfolio")
        self.assertEqual(result["PGSSLMODE"], "disable")
        self.assertEqual(result["PGOPTIONS"], "-c search_path=pg_catalog,public -c statement_timeout=120000")
        self.assertEqual(result["PGCONNECT_TIMEOUT"], "5")

    def test_error_codes_cannot_carry_untrusted_exception_bodies(self):
        for value in (self.secret, "database_failed: password='secret'", "unknown_error", True, None):
            self.assertEqual(str(self.runtime.ReleaseError(value)), "release_failed")

    def test_valid_manifest_integrity_is_read_without_modifying_private_files(self):
        backup, manifest = self.backup_fixture()
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in backup.iterdir()}
        self.assertEqual(self.runtime.read_manifest(backup), manifest)
        self.assertEqual({p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in backup.iterdir()}, before)

    def test_manifest_schema_version_strict_types_unknown_fields_and_false_verification(self):
        for mutate in (lambda m: m.update(schema_version=True), lambda m: m.update(schema_version=2),
                       lambda m: m.update(database="production"), lambda m: m.update(extra="secret"),
                       lambda m: m.update(restore_verified=True), lambda m: m["dump"].update(size=True),
                       lambda m: m["files"][0].update(size=-1), lambda m: m["files"][0].update(extra=1)):
            with self.subTest(mutate=mutate):
                backup, manifest = self.backup_fixture()
                mutate(manifest)
                self.write_manifest(backup, manifest)
                self.assert_rejected(lambda: self.runtime.read_manifest(backup))
                for path in backup.iterdir():
                    path.unlink()
                backup.rmdir()

    def test_manifest_duplicate_keys_nonfinite_numbers_invalid_json_and_size_are_rejected(self):
        backup, _ = self.backup_fixture()
        path = backup / "manifest.json"
        for content in (b'{"schema_version":1,"schema_version":1}', b'{"schema_version":NaN}',
                        b'{"schema_version":Infinity}', b"\xffprivate", b"x" * (32 * 1024 * 1024 + 1)):
            path.write_bytes(content)
            self.assert_rejected(lambda: self.runtime.read_manifest(backup))

    def test_manifest_rejects_corrupted_dump_archive_and_private_snapshot_digest(self):
        backup, manifest = self.backup_fixture()
        for filename in ("database.dump", "filestore.tar.gz"):
            path = backup / filename
            original = path.read_bytes()
            path.write_bytes(original + b"changed")
            self.assert_rejected(lambda: self.runtime.read_manifest(backup))
            path.write_bytes(original)
        manifest["source_snapshot"]["business_fingerprint"] = "f" * 64
        self.write_manifest(backup, manifest)
        self.assert_rejected(lambda: self.runtime.read_manifest(backup))

    def test_manifest_provenance_rejects_wrong_upstream_modules_and_invalid_hashes(self):
        backup, manifest = self.backup_fixture()
        for key, value in (("git_commit", "main"), ("addons_sha256", "g" * 64),
                           ("upstream_sha256", "g" * 64),
                           ("odoo_revision", "0" * 40), ("postgres_version", "15.9"),
                           ("module_versions", {**MODULES, "portfolio_trade": "18.0.1.4.0"})):
            original = manifest["provenance"][key]
            manifest["provenance"][key] = value
            self.write_manifest(backup, manifest)
            self.assert_rejected(lambda: self.runtime.read_manifest(backup))
            manifest["provenance"][key] = original

    def test_manifest_file_inventory_rejects_traversal_duplicates_controls_and_large_sets(self):
        backup, manifest = self.backup_fixture()
        original = manifest["files"][0]
        for value in ("../escape", "/absolute", "a/./b", "a//b", "a/../b", "a\\b", "a\nb", "x" * 257):
            manifest["files"] = [{**original, "path": value}]
            self.write_manifest(backup, manifest)
            self.assert_rejected(lambda: self.runtime.read_manifest(backup))
        for files in ([original, original], [original] * 100001):
            manifest["files"] = files
            self.write_manifest(backup, manifest)
            self.assert_rejected(lambda: self.runtime.read_manifest(backup))

    def test_manifest_file_inventory_is_bound_to_actual_archive_content(self):
        backup, manifest = self.backup_fixture()
        manifest["files"][0]["sha256"] = "0" * 64
        self.write_manifest(backup, manifest)
        self.assert_rejected(lambda: self.runtime.read_manifest(backup))

    def test_manifest_and_payloads_require_private_regular_unaliased_files(self):
        backup, _ = self.backup_fixture()
        for filename in ("manifest.json", "database.dump", "filestore.tar.gz"):
            path = backup / filename
            path.chmod(0o644)
            self.assert_rejected(lambda: self.runtime.read_manifest(backup))
            path.chmod(0o600)
            alias = self.directory / "alias"
            os.link(path, alias)
            self.assert_rejected(lambda: self.runtime.read_manifest(backup))
            alias.unlink()
        backup.chmod(0o755)
        self.assert_rejected(lambda: self.runtime.read_manifest(backup))

    def test_oversized_gnu_and_pax_metadata_reject_before_large_decompression_read(self):
        limit = 1024
        for kind in (tarfile.GNUTYPE_LONGNAME, tarfile.XHDTYPE):
            with self.subTest(kind=kind):
                path = self.directory / ("extension-" + kind.decode() + ".tar.gz")
                if kind == tarfile.GNUTYPE_LONGNAME:
                    metadata = b"filestore/odoo_portfolio/" + b"x" * 4096 + b"\0"
                else:
                    body = b" comment=" + b"x" * 4096 + b"\n"
                    length = len(body) + 1
                    while length != len(body) + len(str(length)):
                        length = len(body) + len(str(length))
                    metadata = str(length).encode() + body
                with tarfile.open(path, "w:gz", format=tarfile.USTAR_FORMAT) as archive:
                    extension = tarfile.TarInfo("././@LongLink" if kind == tarfile.GNUTYPE_LONGNAME else "././@PaxHeader")
                    extension.type, extension.size = kind, len(metadata)
                    archive.addfile(extension, io.BytesIO(metadata))
                    ordinary = tarfile.TarInfo("filestore/odoo_portfolio/file")
                    ordinary.size = 4
                    archive.addfile(ordinary, io.BytesIO(b"data"))
                path.chmod(0o600)
                requested, consumed = [], []
                original = gzip.GzipFile.read

                def observe(handle, size=-1):
                    requested.append(size)
                    content = original(handle, size)
                    consumed.append(len(content))
                    return content

                with patch.object(self.runtime, "MAX_EXPANDED", limit), patch.object(gzip.GzipFile, "read", observe):
                    self.assert_rejected(lambda: self.runtime.archive_inventory(path))
                self.assertTrue(all(0 <= size <= limit for size in requested),
                                "reject a huge extension read before calling the decompressor")
                self.assertLessEqual(sum(consumed), limit,
                                     "metadata must consume the same expansion budget as ordinary file bytes")

    def test_physical_pax_extension_headers_count_toward_archive_member_limit(self):
        path = self.directory / "many-metadata.tar.gz"
        with tarfile.open(path, "w:gz", format=tarfile.USTAR_FORMAT) as archive:
            for index in range(4):
                extension = tarfile.TarInfo(f"global-{index}")
                extension.type, extension.size = tarfile.XGLTYPE, 5
                archive.addfile(extension, io.BytesIO(b"5 x=\n"))
            ordinary = tarfile.TarInfo("filestore/odoo_portfolio/file")
            archive.addfile(ordinary)
        path.chmod(0o600)
        with patch.object(self.runtime, "MAX_FILES", 2):
            self.assert_rejected(lambda: self.runtime.archive_inventory(path))

    def test_tar_padding_and_trailing_decompressed_bytes_share_expansion_budget(self):
        path = self.directory / "padded.tar.gz"
        with tarfile.open(path, "w:gz", format=tarfile.USTAR_FORMAT) as archive:
            ordinary = tarfile.TarInfo("filestore/odoo_portfolio/file")
            archive.addfile(ordinary)
        path.chmod(0o600)
        with patch.object(self.runtime, "MAX_EXPANDED", 2048):
            self.assert_rejected(lambda: self.runtime.archive_inventory(path))

    def test_normal_pax_long_path_within_256_characters_preserves_inventory(self):
        path = self.directory / "normal-pax.tar.gz"
        relative = "aa/" + "a" * 110
        content = b"ordinary content"
        with tarfile.open(path, "w:gz", format=tarfile.PAX_FORMAT) as archive:
            member = tarfile.TarInfo("filestore/odoo_portfolio/" + relative)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
        path.chmod(0o600)
        self.assertEqual(self.runtime.archive_inventory(path),
                         [{"path": relative, "size": len(content), "sha256": sha256(content)}])


class ProjectSnapshotRegressionTests(ReleaseFixture):
    """A source-table fixture exercises state effects without parsing or executing SQL."""

    def setUp(self):
        super().setUp()
        self.snapshot = load_script("release_snapshot")
        next_id = {}
        self.namespaces = {}
        for namespace, identities in IDENTITIES.items():
            records = []
            for name, model in sorted(identities.items()):
                next_id[model] = next_id.get(model, 0) + 1
                records.append({"name": name, "model": model, "id": next_id[model]})
            self.namespaces[namespace] = records
        self.named = {ns: {row["name"]: row["id"] for row in data}
                      for ns, data in self.namespaces.items()}
        self.m1 = self.named["portfolio_trade_acceptance"]
        self.bridge = self.named["portfolio_order_bridge_acceptance"]
        self.flow = self.named["portfolio_order_bridge_flow"]
        old_products = [self.m1[f"product_{name}"] for name in ("bearing", "sensor", "bolt")]
        self.bridge_products = [self.bridge[f"product_item{i}"] for i in (1, 2)]
        products = [{"id": identifier, "product_tmpl_id": 1000 + identifier} for identifier in old_products]
        products += [{"id": self.bridge[f"product_item{i}"], "product_tmpl_id": self.bridge[f"template_item{i}"]}
                     for i in (1, 2)]
        self.tables = {
            "products": products,
            "templates": [{"id": row["product_tmpl_id"], "invoice_policy": "order", "uom_id": 1, "uom_po_id": 1}
                          for row in products],
            "source": [{"id": self.bridge["source"], "code": "B2B-ACCEPTANCE"}],
            "stock_move": [{"id": self.m1["initial_bearing_move"], "product_id": old_products[0],
                            "picking_id": None, "state": "done", "quantity": 10, "location_id": 11,
                            "location_dest_id": 12, "company_id": self.m1["company_a"]}],
            "stock_move_line": [{"id": 501, "move_id": self.m1["initial_bearing_move"],
                                 "product_id": old_products[0], "picking_id": None, "quantity": 10,
                                 "location_id": 11, "location_dest_id": 12}],
            "stock_quant": [{"id": 601, "product_id": old_products[0], "quantity": 10,
                             "reserved_quantity": 0, "location_id": 12, "company_id": self.m1["company_a"]}],
            "owned_jobs": [{"id": self.flow[f"job_{suffix}"], "source_id": self.bridge["source"],
                            "state": "done", "submitted_by_id": self.bridge["user_integration"],
                            "sale_order_id": self.flow[f"quote_{suffix}"]} for suffix in ("success", "repaired")],
            "owned_attempts": [{"id": self.flow[f"attempt_{name}"],
                                "job_id": self.flow["job_success" if name == "success" else "job_repaired"],
                                "actor_id": self.bridge["user_operator"]}
                               for name in ("success", "failed", "retry", "repaired")],
            "owned_quotes": [{"id": self.flow[f"quote_{suffix}"], "partner_id": self.bridge["customer"],
                              "origin": "B2B-ACCEPTANCE:" + suffix, "state": "draft", "user_id": self.m1["user_sales"],
                              "amount_total": amount} for suffix, amount in (("success", 309), ("repaired", 161.5))],
            "sale_lines": [{"id": 701 + i, "order_id": self.flow[f"quote_{suffix}"], "product_id": product}
                           for i, (suffix, product) in enumerate(zip(("success", "repaired"), self.bridge_products, strict=True))],
        }

    def read_state(self, scope, parameters):
        """Select controlled rows using the same business ownership, never SQL text."""
        records = self.tables[scope]
        if scope in {"products", "templates"}:
            result = [row for row in records if row["id"] in parameters[0]]
        elif scope in {"stock_move", "stock_move_line", "stock_quant"}:
            result = [row for row in records if row["product_id"] in parameters[0]]
        elif scope == "source":
            result = [row for row in records if row["id"] == parameters[0]]
        elif scope == "owned_jobs":
            result = [row for row in records if row["source_id"] == parameters[0]]
        elif scope == "owned_attempts":
            jobs = {row["id"] for row in self.tables["owned_jobs"] if row["source_id"] == parameters[0]}
            result = [row for row in records if row["job_id"] in jobs]
        elif scope == "owned_quotes":
            customer, origin_pattern, products = parameters
            product_orders = {line["order_id"] for line in self.tables["sale_lines"] if line["product_id"] in products}
            result = [row for row in records if row["partner_id"] == customer
                      or row["origin"].startswith(origin_pattern.removesuffix("%")) or row["id"] in product_orders]
        else:
            raise AssertionError("unexpected project state scope")
        return deepcopy(sorted(result, key=lambda row: row["id"]))

    def state(self):
        return self.snapshot.collect_project_state(self.read_state, self.namespaces)

    def test_project_state_includes_all_five_native_template_parents(self):
        state = self.state()
        self.assertEqual({row["id"] for row in state["product_templates"]},
                         {row["product_tmpl_id"] for row in self.tables["products"]})
        self.assertEqual(len(state["product_templates"]), 5)

    def test_old_unmarked_template_invoice_policy_and_uom_changes_affect_fingerprint(self):
        before = self.runtime.digest(self.state())
        template = self.tables["templates"][0]
        for key, value in (("invoice_policy", "delivery"), ("uom_id", 2), ("uom_po_id", 3)):
            old = template[key]
            template[key] = value
            self.assertNotEqual(self.runtime.digest(self.state()), before)
            template[key] = old

    def test_initial_inventory_move_line_and_location_changes_affect_fingerprint(self):
        before = self.runtime.digest(self.state())
        for scope, key, value in (("stock_move", "location_dest_id", 99),
                                  ("stock_move_line", "location_dest_id", 99),
                                  ("stock_move_line", "quantity", 9)):
            row = self.tables[scope][0]
            old = row[key]
            row[key] = value
            self.assertNotEqual(self.runtime.digest(self.state()), before)
            row[key] = old

    def test_quant_reservation_and_location_changes_affect_fingerprint_with_same_golden_quantity(self):
        before = self.runtime.digest(self.state())
        quant = self.tables["stock_quant"][0]
        for key, value in (("reserved_quantity", 1), ("location_id", 99)):
            old = quant[key]
            quant[key] = value
            self.assertEqual(quant["quantity"], 10)
            self.assertNotEqual(self.runtime.digest(self.state()), before)
            quant[key] = old

    def test_missing_native_template_parent_rejects_without_inventing_business_data(self):
        self.tables["templates"].pop(0)
        before = deepcopy(self.tables)
        self.assert_rejected(self.state)
        self.assertEqual(self.tables, before)

    def test_unmarked_job_owned_by_same_source_rejects_even_when_original_two_are_done(self):
        self.tables["owned_jobs"].append({**self.tables["owned_jobs"][0], "id": 999, "state": "pending"})
        self.assert_rejected(self.state)

    def test_extra_attempt_for_owned_source_rejects_even_when_original_four_unchanged(self):
        self.tables["owned_attempts"].append({**self.tables["owned_attempts"][0], "id": 999})
        self.assert_rejected(self.state)

    def test_extra_quote_owned_by_customer_origin_or_product_each_rejects(self):
        for reason in ("customer", "origin", "product"):
            with self.subTest(reason=reason):
                extra = {**self.tables["owned_quotes"][0], "id": 999, "partner_id": 999, "origin": "unrelated"}
                if reason == "customer":
                    extra["partner_id"] = self.bridge["customer"]
                elif reason == "origin":
                    extra["origin"] = "B2B-ACCEPTANCE:unmarked"
                else:
                    self.tables["sale_lines"].append({"id": 999, "order_id": 999, "product_id": self.bridge_products[0]})
                self.tables["owned_quotes"].append(extra)
                self.assert_rejected(self.state)
                self.tables["owned_quotes"].pop()
                if reason == "product":
                    self.tables["sale_lines"].pop()

    def test_unrelated_products_and_foreign_source_history_do_not_change_project_fingerprint(self):
        before = self.runtime.digest(self.state())
        self.tables["templates"].append({"id": 999, "invoice_policy": "delivery"})
        self.tables["stock_quant"].append({"id": 999, "product_id": 999, "quantity": 100})
        self.tables["owned_jobs"].append({"id": 999, "source_id": 999})
        self.tables["owned_attempts"].append({"id": 999, "job_id": 999})
        self.tables["owned_quotes"].append({"id": 999, "partner_id": 999, "origin": "foreign"})
        self.assertEqual(self.runtime.digest(self.state()), before)


if __name__ == "__main__":
    unittest.main()
