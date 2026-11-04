"""Private, independent credentials for the explicit M3 local profile."""

import contextlib
import importlib.util
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
ROLES = ("settings", "operator", "integration")


class OrderBridgeCredentialTests(unittest.TestCase):
    def setUp(self):
        script = ROOT / "scripts" / "order_bridge_credentials.py"
        self.assertTrue(script.is_file(), "Independent order bridge credentials are missing")
        spec = importlib.util.spec_from_file_location("order_bridge_credentials_under_test", script)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        base = ROOT / ".runtime" / "tests"
        base.mkdir(parents=True, exist_ok=True)
        directory = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.path = self.directory / "order-bridge-accounts.json"

    def valid(self):
        return {
            "schema_version": 1, "database": "odoo_portfolio",
            "accounts": {
                role: {"login": f"portfolio.bridge.acceptance.{role}",
                       "password": f"private-{role}-" + "x" * 32}
                for role in ROLES
            },
        }

    def write(self, data):
        self.path.write_text(json.dumps(data), encoding="utf-8")
        self.path.chmod(0o600)

    def assert_rejected_unchanged(self, database="odoo_portfolio"):
        before = self.path.read_bytes()
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            with self.assertRaises(ValueError):
                self.module.ensure_credentials(self.path, database)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(output.getvalue() + errors.getvalue(), "")

    def test_exact_three_roles_have_independent_fixed_logins_and_strong_passwords(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = self.module.ensure_credentials(self.path, "odoo_portfolio")
        self.assertEqual(tuple(self.module.ROLES), ROLES)
        self.assertEqual(set(result), {"schema_version", "database", "accounts"})
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["database"], "odoo_portfolio")
        self.assertEqual(set(result["accounts"]), set(ROLES))
        passwords = []
        for role, account in result["accounts"].items():
            self.assertEqual(set(account), {"login", "password"})
            self.assertEqual(account["login"], f"portfolio.bridge.acceptance.{role}")
            self.assertIsInstance(account["password"], str)
            self.assertGreaterEqual(len(account["password"]), 24)
            self.assertTrue(account["password"].strip())
            passwords.append(account["password"])
        self.assertEqual(len(set(passwords)), 3)
        self.assertEqual(output.getvalue(), "")
        self.assertFalse((self.directory / "acceptance-accounts.json").exists())

    def test_new_credentials_use_current_owner_regular_file_and_exact_mode_0600(self):
        self.module.ensure_credentials(self.path, "odoo_portfolio_m1_test")
        info = self.path.lstat()
        self.assertTrue(stat.S_ISREG(info.st_mode))
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
        self.assertEqual(info.st_uid, os.getuid())

    def test_existing_file_is_reused_byte_for_byte_without_generation_or_write(self):
        first = self.module.ensure_credentials(self.path, "odoo_portfolio")
        before = self.path.read_bytes(), self.path.stat().st_mtime_ns
        with patch.object(self.module.secrets, "token_urlsafe", side_effect=AssertionError("must reuse")):
            self.assertEqual(self.module.ensure_credentials(self.path, "odoo_portfolio"), first)
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_mtime_ns), before)

    def test_generation_requests_urlsafe_32_bytes_for_each_role(self):
        generated = ["A" * 43, "B" * 43, "C" * 43]
        with patch.object(self.module.secrets, "token_urlsafe", side_effect=generated) as generate:
            self.module.ensure_credentials(self.path, "odoo_portfolio")
        self.assertEqual(generate.call_count, 3)
        self.assertTrue(all(call.args == (32,) for call in generate.call_args_list))

    def test_publication_never_overwrites_a_concurrently_published_valid_file(self):
        winner = self.valid()
        real_link = os.link

        def publish_winner(source, target, *args, **kwargs):
            self.write(winner)
            return real_link(source, target, *args, **kwargs)

        with patch.object(self.module.os, "link", side_effect=publish_winner):
            result = self.module.ensure_credentials(self.path, "odoo_portfolio")
        self.assertEqual(result, winner)
        self.assertEqual(set(self.directory.iterdir()), {self.path})

    def test_only_exact_explicit_loopback_targets_are_accepted(self):
        for database in ("odoo_portfolio", "odoo_portfolio_m1_test"):
            self.module.validate_target(database, "127.0.0.1", True)
        for database, host, opt_in in (
            ("production", "127.0.0.1", True), ("odoo_portfolio", "localhost", True),
            ("odoo_portfolio", "::1", True), ("odoo_portfolio", "remote.invalid", True),
            ("odoo_portfolio", None, True), ("odoo_portfolio", "127.0.0.1", False),
            ("odoo_portfolio", "127.0.0.1", 1), ("odoo_portfolio", "127.0.0.1", "true"),
        ):
            with self.subTest(database=database, host=host, opt_in=opt_in), self.assertRaises(ValueError):
                self.module.validate_target(database, host, opt_in)

    def test_unsupported_database_rejects_before_parent_or_file_creation(self):
        target = self.directory / "not-created" / "credentials.json"
        for database in ("production", "", None, True):
            with self.subTest(database=database), self.assertRaises(ValueError):
                self.module.ensure_credentials(target, database)
        self.assertFalse(target.parent.exists())

    def test_malformed_wrong_database_and_strict_schema_never_overwrite_existing(self):
        mutations = (
            lambda data: data.update(schema_version=2),
            lambda data: data.update(schema_version=True),
            lambda data: data.update(database="odoo_portfolio_m1_test"),
            lambda data: data.update(extra="unexpected"),
            lambda data: data["accounts"].pop("operator"),
            lambda data: data["accounts"].update(sales=data["accounts"]["settings"]),
            lambda data: data["accounts"]["operator"].update(login="portfolio.acceptance.sales"),
            lambda data: data["accounts"]["operator"].update(password="short"),
            lambda data: data["accounts"]["operator"].update(password=" " * 24),
            lambda data: data["accounts"]["operator"].update(password=123456789012345678901234),
            lambda data: data["accounts"]["operator"].update(password=data["accounts"]["settings"]["password"]),
            lambda data: data["accounts"]["operator"].update(extra="unexpected"),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                data = self.valid()
                mutate(data)
                self.write(data)
                self.assert_rejected_unchanged()
        for data in (None, [], {}, {"schema_version": 1, "database": "odoo_portfolio", "accounts": []}):
            with self.subTest(data=data):
                self.write(data)
                self.assert_rejected_unchanged()

    def test_invalid_json_and_encoding_do_not_echo_private_contents(self):
        for content in (b'{"private-secret-that-must-not-print":', b"\xffprivate-secret"):
            with self.subTest(content=content):
                self.path.write_bytes(content)
                self.path.chmod(0o600)
                self.assert_rejected_unchanged()

    def test_unsafe_modes_are_rejected_without_chmod_or_reset(self):
        self.write(self.valid())
        for mode in (0o400, 0o640, 0o644, 0o660, 0o700):
            with self.subTest(mode=mode):
                self.path.chmod(mode)
                self.assert_rejected_unchanged()
                self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), mode)

    def test_foreign_owner_is_rejected(self):
        self.write(self.valid())
        actual = os.getuid()
        with patch.object(self.module.os, "getuid", return_value=actual + 1):
            self.assert_rejected_unchanged()

    def test_final_symlink_and_symlink_parent_are_rejected_without_touching_target(self):
        target = self.directory / "real.json"
        target.write_text("{}")
        target.chmod(0o600)
        self.path.symlink_to(target)
        with self.assertRaises(ValueError):
            self.module.ensure_credentials(self.path, "odoo_portfolio")
        self.assertEqual(target.read_text(), "{}")
        self.path.unlink()
        real = self.directory / "real-directory"
        real.mkdir()
        linked = self.directory / "linked-directory"
        linked.symlink_to(real, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.module.ensure_credentials(linked / "new.json", "odoo_portfolio")
        self.assertEqual(list(real.iterdir()), [])

    def test_nonregular_directory_and_fifo_are_rejected_without_blocking(self):
        self.path.mkdir()
        with self.assertRaises(ValueError):
            self.module.ensure_credentials(self.path, "odoo_portfolio")
        self.path.rmdir()
        os.mkfifo(self.path, 0o600)
        with self.assertRaises(ValueError):
            self.module.ensure_credentials(self.path, "odoo_portfolio")
