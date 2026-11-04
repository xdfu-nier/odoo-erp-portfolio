"""Pure pre-import safety contract for the native process-exit test helper."""

import builtins
import importlib.util
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "addons" / "portfolio_order_bridge" / "tests" / "worker_process_probe.py"
SPEC = importlib.util.spec_from_file_location("worker_process_probe_contract", PROBE)
probe = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = probe
SPEC.loader.exec_module(probe)


class TestWorkerProcessProbe(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.project = Path(self.directory.name).resolve() / "project"
        self.runtime = self.project / ".runtime"
        (self.runtime / "tmp").mkdir(parents=True)
        self.config = self.runtime / "odoo.conf"
        self.config.write_text("[options]\ndb_host = 127.0.0.1\ndb_port = 55432\n", encoding="utf-8")
        self.witness = self.runtime / "tmp" / f"worker-exit-{'a' * 32}.json"
        self.argv = [
            "--config", str(self.config), "--database", probe.DATABASE,
            "--uid", "42", "--company-id", "23", "--source-id", "101", "--job-id", "102",
            "--witness", str(self.witness),
        ]

    def validate(self, args=None):
        return probe.validate_target(
            self.argv if args is None else args, project=self.project, main_project=self.project,
        )

    def replacing(self, key, value):
        args = self.argv.copy()
        args[args.index(key) + 1] = value
        return args

    def assert_preimport_rejected(self, args):
        real_import = builtins.__import__

        def no_odoo(name, *values, **kwargs):
            if name == "odoo" or name.startswith("odoo."):
                self.fail("Invalid target attempted an Odoo import")
            return real_import(name, *values, **kwargs)

        with patch("builtins.__import__", side_effect=no_odoo), patch.object(probe, "run_native") as native:
            with self.assertRaises(probe.TargetRejected):
                self.validate(args)
            native.assert_not_called()
            real_validate = probe.validate_target
            with patch.object(probe, "validate_target", side_effect=lambda argv: real_validate(
                argv, project=self.project, main_project=self.project,
            )):
                self.assertEqual(probe.main(args), 64)
            native.assert_not_called()

    def test_valid_loopback_project_target_has_only_explicit_native_ids(self):
        target = self.validate()
        self.assertEqual(target.database, probe.DATABASE)
        self.assertEqual((target.uid, target.company_id, target.source_id, target.job_id), (42, 23, 101, 102))
        self.assertEqual(target.config, self.config)
        self.assertEqual(target.witness, self.witness)
        self.assertFalse(self.witness.exists())

    def test_nonacceptance_database_is_rejected_before_native_import(self):
        for database in ("odoo_portfolio", "postgres", "odoo_portfolio_m1_test,postgres", ""):
            with self.subTest(database=database):
                self.assert_preimport_rejected(self.replacing("--database", database))
        with patch.object(probe, "run_native") as native:
            self.assertEqual(probe.main(self.replacing("--database", "postgres")), 64)
            native.assert_not_called()

    def test_remote_socket_and_invalid_port_are_rejected_before_native_import(self):
        for host, port in (("db.example.com", "55432"), ("localhost", "55432"),
                           ("/tmp", "55432"), ("127.0.0.1", "543"), ("127.0.0.1", "bad")):
            with self.subTest(host=host, port=port):
                self.config.write_text(f"[options]\ndb_host = {host}\ndb_port = {port}\n", encoding="utf-8")
                self.assert_preimport_rejected(self.argv)

    def test_unknown_destructive_abbreviated_and_missing_flags_are_rejected(self):
        for args in (
            [*self.argv, "--init", "base"], [*self.argv, "--update", "all"],
            [*self.argv, "--drop-database"], [*self.argv, "--help"],
            self.replacing("--uid", "1"), self.replacing("--uid", "-2"),
            self.replacing("--job-id", "not-an-id"), self.argv[:-2],
            ["--conf" if arg == "--config" else arg for arg in self.argv],
        ):
            with self.subTest(args=args):
                self.assert_preimport_rejected(args)

    def test_config_outside_canonical_runtime_and_config_symlink_are_rejected(self):
        outside = self.project / "other.conf"
        outside.write_text(self.config.read_text(encoding="utf-8"), encoding="utf-8")
        self.assert_preimport_rejected(self.replacing("--config", str(outside)))
        self.config.unlink()
        self.config.symlink_to(outside)
        self.assert_preimport_rejected(self.argv)

    def test_configuration_cannot_enable_module_updates_or_test_execution(self):
        for mode, value in (("init", "base"), ("update", "all"), ("test_enable", "True"),
                            ("test_tags", "portfolio_import_worker_concurrency")):
            with self.subTest(mode=mode):
                self.config.write_text(
                    f"[options]\ndb_host = 127.0.0.1\ndb_port = 55432\n{mode} = {value}\n",
                    encoding="utf-8",
                )
                self.assert_preimport_rejected(self.argv)

    def test_witness_outside_runtime_existing_path_and_symlink_are_rejected(self):
        for path in (self.project / self.witness.name, self.runtime / "tmp" / "arbitrary.json"):
            self.assert_preimport_rejected(self.replacing("--witness", str(path)))
        self.witness.write_text("existing", encoding="utf-8")
        self.assert_preimport_rejected(self.argv)
        self.witness.unlink()
        self.witness.symlink_to(self.project / "missing")
        self.assert_preimport_rejected(self.argv)

    def test_symlinked_runtime_temp_directory_is_rejected(self):
        directory = self.runtime / "tmp"
        directory.rmdir()
        outside = self.project / "external-temp"
        outside.mkdir()
        directory.symlink_to(outside, target_is_directory=True)
        self.assert_preimport_rejected(self.argv)

    def test_witness_is_exclusive_private_and_contains_safe_checkpoint_only(self):
        target = self.validate()
        values = {"stage": "native_quote_flushed_before_commit", "uid": 42, "su": False, "line_ids": [1, 2]}
        probe.write_witness(target.witness, values)
        self.assertEqual(stat.S_IMODE(target.witness.stat().st_mode), 0o600)
        self.assertEqual(target.witness.stat().st_uid, os.getuid())
        original = target.witness.read_bytes()
        with self.assertRaises(FileExistsError):
            probe.write_witness(target.witness, {"uid": 999})
        self.assertEqual(target.witness.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
