import importlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


class AcceptanceCredentialTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(
            importlib.util.find_spec("acceptance_credentials"),
            "Private acceptance credentials and target guard are not implemented",
        )
        self.module = importlib.import_module("acceptance_credentials")
        base = ROOT / ".runtime" / "tests"
        base.mkdir(parents=True, exist_ok=True)
        directory = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "acceptance-accounts.json"

    def test_only_explicit_local_acceptance_targets_are_allowed(self):
        for name in ("odoo_portfolio", "odoo_portfolio_m1_test"):
            self.module.validate_target(name, "127.0.0.1", True)
        for name, host, opt_in in (
            ("production", "127.0.0.1", True),
            ("odoo_portfolio", "remote.example", True),
            ("odoo_portfolio", "127.0.0.1", False),
        ):
            with self.subTest(database=name, host=host), self.assertRaises(ValueError):
                self.module.validate_target(name, host, opt_in)

    def test_credentials_are_private_unique_and_reused_without_overwrite(self):
        first = self.module.ensure_credentials(self.path, "odoo_portfolio")
        content = self.path.read_bytes()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(first["accounts"]), 7)
        passwords = {item["password"] for item in first["accounts"].values()}
        self.assertEqual(len(passwords), 7)
        self.assertTrue(all(len(value) >= 24 for value in passwords))
        self.assertEqual(first, self.module.ensure_credentials(self.path, "odoo_portfolio"))
        self.assertEqual(content, self.path.read_bytes())

    def test_credentials_from_another_database_are_rejected(self):
        self.module.ensure_credentials(self.path, "odoo_portfolio")
        with self.assertRaises(ValueError):
            self.module.ensure_credentials(self.path, "odoo_portfolio_m1_test")

    def test_unsafe_file_permissions_are_rejected(self):
        self.module.ensure_credentials(self.path, "odoo_portfolio")
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):
            self.module.ensure_credentials(self.path, "odoo_portfolio")

    def test_symlink_credentials_are_rejected(self):
        target = self.path.with_name("target.json")
        target.write_text("{}")
        self.path.symlink_to(target)
        with self.assertRaises(ValueError):
            self.module.ensure_credentials(self.path, "odoo_portfolio")

    def test_malformed_credentials_are_rejected(self):
        self.path.write_text(json.dumps({"database": "odoo_portfolio", "accounts": {}}))
        self.path.chmod(0o600)
        with self.assertRaises(ValueError):
            self.module.ensure_credentials(self.path, "odoo_portfolio")
