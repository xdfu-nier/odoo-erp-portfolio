import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


@unittest.skipUnless(importlib.util.find_spec("pgserver"), "requires project development dependencies")
class BundledPostgresTests(unittest.TestCase):
    def test_bundled_tools_resolve_inside_virtualenv_and_execute(self):
        from manage import postgres_command

        for name in ("initdb", "pg_ctl", "postgres"):
            with self.subTest(name=name):
                binary = Path(postgres_command(name)).resolve()
                self.assertTrue(binary.is_relative_to((ROOT / ".venv").resolve()))
                result = subprocess.run(
                    [str(binary), "--version"], check=True, capture_output=True, text=True
                )
                self.assertIn("PostgreSQL", result.stdout)


if __name__ == "__main__":
    unittest.main()
