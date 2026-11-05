"""The explicit bridge CLI rejects unsafe targets before Odoo or private writes."""

import builtins
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


class PrepareOrderBridgeTests(unittest.TestCase):
    def setUp(self):
        script = ROOT / "scripts" / "prepare_order_bridge.py"
        self.assertTrue(script.is_file(), "Explicit order bridge preparation CLI is missing")
        spec = importlib.util.spec_from_file_location("prepare_order_bridge_under_test", script)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        base = ROOT / ".runtime" / "tests"
        base.mkdir(parents=True, exist_ok=True)
        directory = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.config = self.directory / "odoo.conf"
        self.credentials = self.directory / "order-bridge-accounts.json"
        self.config.write_text("[options]\ndb_host = 127.0.0.1\ndb_password = NEVER-PRINT-BRIDGE-SECRET\n")

    def argv(self, *extra):
        return ["prepare_order_bridge.py", "--config", str(self.config), "--database", "odoo_portfolio",
                "--allow-local-acceptance", "--credentials", str(self.credentials), *extra]

    def snapshot(self):
        return {path.name: path.read_bytes() for path in self.directory.iterdir() if path.is_file()}

    def assert_denied_before_odoo(self, argv):
        before = self.snapshot()
        original = builtins.__import__
        imports = []

        def guarded_import(name, *args, **kwargs):
            if name == "odoo" or name.startswith("odoo."):
                imports.append(name)
                raise AssertionError("Unsafe CLI target imported Odoo")
            return original(name, *args, **kwargs)

        output, errors = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", argv), patch("builtins.__import__", guarded_import), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            with self.assertRaises(SystemExit) as raised:
                self.module.main()
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(imports, [])
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.credentials.exists())
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn("NEVER-PRINT-BRIDGE-SECRET", errors.getvalue())

    def test_no_explicit_opt_in_rejects_before_odoo_or_credentials(self):
        argv = self.argv()
        argv.remove("--allow-local-acceptance")
        self.assert_denied_before_odoo(argv)

    def test_bad_database_or_host_rejects_before_odoo_or_credentials(self):
        for database, host in (("production", "127.0.0.1"), ("odoo_portfolio", "localhost"),
                               ("odoo_portfolio", "::1"), ("odoo_portfolio", "remote.invalid"),
                               ("odoo_portfolio", "")):
            with self.subTest(database=database, host=host):
                self.config.write_text(f"[options]\ndb_host = {host}\n")
                argv = self.argv()
                argv[argv.index("--database") + 1] = database
                self.assert_denied_before_odoo(argv)

    def test_missing_bad_and_wrong_section_configuration_is_safe(self):
        self.config.unlink()
        self.assert_denied_before_odoo(self.argv())
        for content in ("invalid\nNEVER-PRINT-BRIDGE-SECRET", "[wrong]\ndb_host = 127.0.0.1\n",
                        "[options]\ndb_host = 127.0.0.1\nNEVER-PRINT-BRIDGE-SECRET\n",
                        "[options]\ndb_host = 127.0.0.1\ndb_host = NEVER-PRINT-BRIDGE-SECRET\n"):
            with self.subTest(content=content):
                self.config.write_text(content)
                self.assert_denied_before_odoo(self.argv())

    def test_credentials_outside_project_runtime_are_rejected_before_odoo(self):
        # The full suite intentionally sets TMPDIR inside project runtime.
        # Choose known outside parents and clean unexpected files on failure.
        for parent in (ROOT, ROOT / ".cache", ROOT.parent):
            parent.mkdir(parents=True, exist_ok=True)
            with self.subTest(parent=parent), tempfile.TemporaryDirectory(
                prefix=".bridge-outside-tests-", dir=parent,
            ) as directory:
                path = Path(directory) / "bridge-accounts.json"
                argv = self.argv()
                argv[argv.index("--credentials") + 1] = str(path)
                self.assert_denied_before_odoo(argv)
                self.assertFalse(path.exists())

    def test_symlink_credential_parent_is_rejected_before_odoo(self):
        real = self.directory / "real"
        real.mkdir()
        linked = self.directory / "linked"
        linked.symlink_to(real, target_is_directory=True)
        argv = self.argv()
        argv[argv.index("--credentials") + 1] = str(linked / "accounts.json")
        self.assert_denied_before_odoo(argv)
        self.assertEqual(list(real.iterdir()), [])

    def test_unknown_flags_reject_without_odoo_or_private_files(self):
        self.assert_denied_before_odoo(self.argv("--enable-fulfillment"))
        self.assert_denied_before_odoo(self.argv("--install"))

    def fake_odoo(self):
        result = {"schema_version": 1, "database": "odoo_portfolio",
                  "namespace": "portfolio_order_bridge_acceptance", "company_id": 501,
                  "source_id": 601, "identities": {}, "cron_active": False,
                  "borrowed_m1_identities": {}, "borrowed_finance_pricelist_id": 701}
        helper = Mock(return_value=result)
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.__exit__.return_value = False
        registry = Mock(return_value=SimpleNamespace(cursor=Mock(return_value=cursor)))
        env = MagicMock()
        env.cr, env.uid, env.context = cursor, 1, {}
        environment = Mock(return_value=env)
        modules = {name: ModuleType(name) for name in (
            "odoo", "odoo.modules", "odoo.modules.registry", "odoo.addons",
            "odoo.addons.portfolio_order_bridge", "odoo.addons.portfolio_order_bridge.local_acceptance",
        )}
        modules["odoo"].SUPERUSER_ID = 1
        modules["odoo"].api = SimpleNamespace(Environment=environment)
        parse_config = Mock()
        modules["odoo"].tools = SimpleNamespace(config=SimpleNamespace(parse_config=parse_config))
        modules["odoo.modules.registry"].Registry = registry
        modules["odoo.addons.portfolio_order_bridge.local_acceptance"].prepare_local_import = helper
        return modules, helper, cursor, registry, environment, parse_config, env, result

    def test_valid_cli_calls_plain_helper_with_private_three_role_passwords_and_commits_once(self):
        modules, helper, cursor, registry, environment, parse_config, env, result = self.fake_odoo()
        output = io.StringIO()
        with patch.dict(sys.modules, modules), patch.object(sys, "argv", self.argv()), \
                contextlib.redirect_stdout(output):
            self.assertEqual(self.module.main(), 0)
        self.assertEqual(json.loads(output.getvalue()), result)
        credentials = json.loads(self.credentials.read_text())
        passwords = {role: account["password"] for role, account in credentials["accounts"].items()}
        helper.assert_called_once_with(env, passwords, allow_local=True)
        cursor.commit.assert_called_once_with()
        registry.assert_called_once_with("odoo_portfolio")
        environment.assert_called_once_with(cursor, 1, {})
        arguments = parse_config.call_args.args[0]
        self.assertIn("--no-http", arguments)
        self.assertIn(str(self.config.resolve()), arguments)
        self.assertIn(f"{ROOT / 'addons'},{ROOT / '.upstream' / 'odoo' / 'addons'}", arguments)
        for value in ["NEVER-PRINT-BRIDGE-SECRET", *passwords.values()]:
            self.assertNotIn(value, output.getvalue())
        self.assertFalse((self.directory / "acceptance-accounts.json").exists())

    def test_config_parser_does_not_interpolate_percent_in_private_configuration(self):
        self.config.write_text("[options]\ndb_host = 127.0.0.1\ndb_password = NEVER-PRINT-BRIDGE-SECRET%xyz\n")
        modules, helper, cursor, *_ = self.fake_odoo()
        with patch.dict(sys.modules, modules), patch.object(sys, "argv", self.argv()), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.module.main(), 0)
        helper.assert_called_once()
        cursor.commit.assert_called_once_with()

    def test_helper_failure_never_commits_or_echoes_private_exception(self):
        modules, helper, cursor, *_ = self.fake_odoo()
        helper.side_effect = RuntimeError("NEVER-PRINT-BRIDGE-SECRET password session")
        output, errors = io.StringIO(), io.StringIO()
        with patch.dict(sys.modules, modules), patch.object(sys, "argv", self.argv()), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            with self.assertRaises(SystemExit) as raised:
                self.module.main()
        self.assertEqual(raised.exception.code, 2)
        cursor.commit.assert_not_called()
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn("NEVER-PRINT-BRIDGE-SECRET", errors.getvalue())
