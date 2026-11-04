"""The import-flow CLI reads existing private credentials before importing Odoo."""

import builtins
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
ROLES = ("settings", "operator", "integration")
SECRET = "NEVER-PRINT-LOCAL-IMPORT-SECRET"


class RunLocalOrderImportTests(unittest.TestCase):
    def setUp(self):
        script = ROOT / "scripts" / "run_local_order_import.py"
        self.assertTrue(script.is_file(), "Explicit local order import CLI is missing")
        spec = importlib.util.spec_from_file_location("local_order_import_cli_under_test", script)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        base = ROOT / ".runtime" / "tests"
        base.mkdir(parents=True, exist_ok=True)
        directory = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.config = self.directory / "odoo.conf"
        self.credentials = self.directory / "order-bridge-accounts.json"
        self.config.write_text(f"[options]\ndb_host = 127.0.0.1\ndb_password = {SECRET}\n")
        self.passwords = {role: f"{role}-{SECRET}" for role in ROLES}
        self.data = {
            "schema_version": 1, "database": "odoo_portfolio",
            "accounts": {role: {"login": f"portfolio.bridge.acceptance.{role}",
                                "password": self.passwords[role]} for role in ROLES},
        }
        self.credentials.write_text(json.dumps(self.data))
        self.credentials.chmod(0o600)

    def argv(self, *extra):
        return ["run_local_order_import.py", "--config", str(self.config),
                "--database", "odoo_portfolio", "--allow-local-acceptance",
                "--credentials", str(self.credentials), *extra]

    def snapshot(self):
        return {path.name: (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
                for path in self.directory.iterdir() if path.is_file()}

    def assert_denied_before_odoo(self, argv):
        before = self.snapshot()
        original, imports = builtins.__import__, []

        def guarded_import(name, *args, **kwargs):
            if name == "odoo" or name.startswith("odoo."):
                imports.append(name)
                raise AssertionError("Invalid local import invocation imported Odoo")
            return original(name, *args, **kwargs)

        output, errors = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", argv), patch("builtins.__import__", guarded_import), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            with self.assertRaises(SystemExit) as raised:
                self.module.main()
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(imports, [])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn(SECRET, errors.getvalue())
        for value in self.passwords.values():
            self.assertNotIn(value, errors.getvalue())

    def test_explicit_opt_in_is_required_before_import_or_any_file_write(self):
        argv = self.argv()
        argv.remove("--allow-local-acceptance")
        self.assert_denied_before_odoo(argv)

    def test_database_and_strict_numeric_loopback_are_gated_before_import(self):
        for database, host in (("production", "127.0.0.1"), ("odoo_portfolio", "localhost"),
                               ("odoo_portfolio", "::1"), ("odoo_portfolio", "remote.invalid"),
                               ("odoo_portfolio", "")):
            with self.subTest(database=database, host=host):
                self.config.write_text(f"[options]\ndb_host = {host}\ndb_password = {SECRET}\n")
                argv = self.argv()
                argv[argv.index("--database") + 1] = database
                self.assert_denied_before_odoo(argv)

    def test_missing_malformed_or_wrong_section_configuration_is_safe(self):
        self.config.unlink()
        self.assert_denied_before_odoo(self.argv())
        for content in (SECRET, "[wrong]\ndb_host = 127.0.0.1\n",
                        f"[options]\ndb_host = 127.0.0.1\n{SECRET}\n",
                        f"[options]\ndb_host = 127.0.0.1\ndb_host = {SECRET}\n"):
            with self.subTest(content=content):
                self.config.write_text(content)
                self.assert_denied_before_odoo(self.argv())

    def test_missing_credentials_fail_before_odoo_and_are_never_generated(self):
        self.credentials.unlink()
        with patch("order_bridge_credentials.ensure_credentials",
                   side_effect=AssertionError("Import flow must never create credentials")):
            self.assert_denied_before_odoo(self.argv())
        self.assertFalse(self.credentials.exists())

    def test_unsafe_or_malformed_credentials_are_rejected_without_echo(self):
        for mode, content in ((0o644, json.dumps(self.data)), (0o600, SECRET),
                              (0o600, json.dumps({**self.data, "database": "production"})),
                              (0o600, json.dumps({**self.data, "schema_version": True})),
                              (0o600, json.dumps({**self.data, "accounts": {}}))):
            with self.subTest(mode=mode, content=content):
                self.credentials.write_text(content)
                self.credentials.chmod(mode)
                self.assert_denied_before_odoo(self.argv())

    def test_credential_path_must_be_existing_private_project_runtime_file(self):
        outside = ROOT / ".cache" / "local-import-outside-tests"
        outside.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=outside) as directory:
            path = Path(directory) / "accounts.json"
            path.write_bytes(self.credentials.read_bytes())
            path.chmod(0o600)
            argv = self.argv()
            argv[argv.index("--credentials") + 1] = str(path)
            self.assert_denied_before_odoo(argv)
        target = self.directory / "linked-accounts.json"
        target.symlink_to(self.credentials)
        argv = self.argv()
        argv[argv.index("--credentials") + 1] = str(target)
        self.assert_denied_before_odoo(argv)

    def test_preparation_http_and_policy_flags_are_not_accepted(self):
        for extra in (("--prepare",), ("--enable-fulfillment",), ("--install",), ("--http",)):
            with self.subTest(extra=extra):
                self.assert_denied_before_odoo(self.argv(*extra))

    def fake_odoo(self):
        result = {"schema_version": 1, "database": "odoo_portfolio",
                  "namespace": "portfolio_order_bridge_flow", "company_id": 501,
                  "source_id": 601, "identities": {}, "jobs": {}, "quotes": {},
                  "events": {}, "m1_preservation": {"bearing_stock": 40.0}}
        helper, cursor = Mock(return_value=result), MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.__exit__.return_value = False
        registry = Mock(return_value=SimpleNamespace(cursor=Mock(return_value=cursor)))
        env = MagicMock()
        env.cr, env.uid, env.context = cursor, 1, {}
        environment = Mock(return_value=env)
        modules = {name: ModuleType(name) for name in (
            "odoo", "odoo.modules", "odoo.modules.registry", "odoo.addons",
            "odoo.addons.portfolio_order_bridge", "odoo.addons.portfolio_order_bridge.local_import_flow",
        )}
        modules["odoo"].SUPERUSER_ID = 1
        modules["odoo"].api = SimpleNamespace(Environment=environment)
        parse_config = Mock()
        modules["odoo"].tools = SimpleNamespace(config=SimpleNamespace(parse_config=parse_config))
        modules["odoo.modules.registry"].Registry = registry
        modules["odoo.addons.portfolio_order_bridge.local_import_flow"].run_local_import = helper
        return modules, helper, cursor, registry, environment, parse_config, env, result

    def invoke(self, modules):
        output = io.StringIO()
        with patch.dict(sys.modules, modules), patch.object(sys, "argv", self.argv()), \
                contextlib.redirect_stdout(output):
            status = self.module.main()
        return status, output.getvalue()

    def test_valid_cli_calls_plain_flow_with_existing_passwords_and_commits_once(self):
        before = self.snapshot()
        modules, helper, cursor, registry, environment, parse_config, env, result = self.fake_odoo()
        with patch("order_bridge_credentials.ensure_credentials",
                   side_effect=AssertionError("Flow cannot create or replace credentials")):
            status, output = self.invoke(modules)
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output), result)
        helper.assert_called_once_with(env, self.passwords, allow_local=True)
        registry.assert_called_once_with("odoo_portfolio")
        environment.assert_called_once_with(cursor, 1, {})
        cursor.commit.assert_called_once_with()
        args = parse_config.call_args.args[0]
        self.assertIn("--no-http", args)
        self.assertIn(str(self.config.resolve()), args)
        self.assertIn(f"{ROOT / 'addons'},{ROOT / '.upstream' / 'odoo' / 'addons'}", args)
        self.assertEqual(self.snapshot(), before)
        for value in (SECRET, *self.passwords.values()):
            self.assertNotIn(value, output)

    def test_completed_reports_have_identical_sorted_json_bytes_and_no_private_values(self):
        modules, _helper, _cursor, _registry, _environment, _config, _env, result = self.fake_odoo()
        first, second = self.invoke(modules)[1], self.invoke(modules)[1]
        self.assertEqual(first, second)
        self.assertEqual(first, json.dumps(result, sort_keys=True) + "\n")
        for value in (SECRET, *self.passwords.values(), "password", "cookie", "dsn", "raw_payload"):
            self.assertNotIn(value, first)

    def test_helper_exception_never_commits_or_exposes_private_exception(self):
        modules, helper, cursor, *_ = self.fake_odoo()
        helper.side_effect = RuntimeError(f"{SECRET} password session")
        output, errors = io.StringIO(), io.StringIO()
        with patch.dict(sys.modules, modules), patch.object(sys, "argv", self.argv()), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            with self.assertRaises(SystemExit) as raised:
                self.module.main()
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn(SECRET, errors.getvalue())
        cursor.commit.assert_not_called()

    def test_percent_signs_in_config_are_not_interpolated(self):
        self.config.write_text(f"[options]\ndb_host = 127.0.0.1\ndb_password = {SECRET}%xyz\n")
        modules, helper, cursor, *_ = self.fake_odoo()
        self.assertEqual(self.invoke(modules)[0], 0)
        helper.assert_called_once()
        cursor.commit.assert_called_once_with()

    def test_credential_reader_rejects_short_duplicate_or_wrong_role_passwords(self):
        for invalid in ("short", self.passwords["settings"]):
            self.data["accounts"]["operator"]["password"] = invalid
            self.credentials.write_text(json.dumps(self.data))
            self.assert_denied_before_odoo(self.argv())
        self.data["accounts"]["operator"]["password"] = self.passwords["operator"]
        self.data["accounts"]["operator"]["login"] = "unexpected.actor"
        self.credentials.write_text(json.dumps(self.data))
        self.assert_denied_before_odoo(self.argv())

    def test_shared_main_runtime_path_is_supported_only_for_known_worktree_environment(self):
        main = ROOT.parent.parent if ROOT.parent.name == ".worktrees" else ROOT
        private = main / ".runtime" / "order-bridge-accounts.json"
        if main == ROOT:
            self.assertEqual(self.module._credential_path(private), private)
        elif (ROOT / ".venv").resolve().parent == main:
            self.assertEqual(self.module._credential_path(private), private)
        self.assertEqual(os.stat(self.credentials).st_mode & 0o777, 0o600)
