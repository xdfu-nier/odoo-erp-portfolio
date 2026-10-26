import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from runtime import Settings, initialize_env, write_odoo_config  # noqa: E402


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary_root = ROOT / ".runtime" / "tests"
        temporary_root.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=temporary_root)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / ".env.example").write_text((ROOT / ".env.example").read_text())

    def test_paths_are_local_and_config_binds_only_to_loopback(self):
        initialize_env(self.root)
        settings = Settings.load(self.root)
        config = write_odoo_config(settings)
        self.assertTrue(config.is_relative_to(self.root))
        content = config.read_text()
        self.assertIn("http_interface = 127.0.0.1", content)
        self.assertIn(str(self.root / ".runtime" / "filestore"), content)
        self.assertIn(str(self.root / "addons"), content)
        self.assertNotIn("list_db = True", content)
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)

    def test_initialization_preserves_existing_env_and_generates_secrets(self):
        initialize_env(self.root)
        first = (self.root / ".env").read_text()
        self.assertNotIn("odoo-local-dev", first)
        self.assertNotIn("local-dev-master-change-me", first)
        initialize_env(self.root)
        self.assertEqual(first, (self.root / ".env").read_text())

    def test_invalid_ports_and_remote_database_hosts_are_rejected(self):
        for value in ("PGPORT=invalid", "PGPORT=70000", "PGHOST=0.0.0.0"):
            with self.subTest(value=value):
                (self.root / ".env").write_text(value)
                with self.assertRaises(ValueError):
                    Settings.load(self.root)

    def test_database_name_must_be_safe_for_odoo_database_filter(self):
        (self.root / ".env").write_text("DB_NAME=portfolio.*")
        with self.assertRaises(ValueError):
            Settings.load(self.root)


if __name__ == "__main__":
    unittest.main()
