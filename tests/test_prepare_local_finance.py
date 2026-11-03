"""The finance CLI validates its target before importing or connecting Odoo."""
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


class PrepareLocalFinanceTests(unittest.TestCase):
    def setUp(self):
        script = ROOT / "scripts" / "prepare_local_finance.py"
        self.assertTrue(script.is_file(), "Explicit local finance CLI is missing")
        spec = importlib.util.spec_from_file_location("prepare_local_finance_under_test", script)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        base = ROOT / ".cache" / "local-finance-tests"
        base.mkdir(parents=True, exist_ok=True)
        directory = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.config = self.directory / "odoo.conf"
        self.config.write_text("[options]\ndb_host = 127.0.0.1\ndb_password = NEVER-PRINT-THIS\n")

    def argv(self, *extra):
        return ["prepare_local_finance.py", "--config", str(self.config),
                "--database", "odoo_portfolio", "--allow-local-acceptance", *extra]

    def assert_denied_before_odoo(self, argv):
        before = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        original_import = builtins.__import__
        imports = []

        def guarded_import(name, *args, **kwargs):
            if name == "odoo" or name.startswith("odoo."):
                imports.append(name)
                raise AssertionError("An invalid CLI target must not import Odoo")
            return original_import(name, *args, **kwargs)

        output, errors = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", argv), patch("builtins.__import__", guarded_import), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            with self.assertRaises(SystemExit) as raised:
                self.module.main()
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(imports, [])
        self.assertEqual({path.name: path.read_bytes() for path in self.directory.iterdir()}, before)
        self.assertNotIn("NEVER-PRINT-THIS", output.getvalue() + errors.getvalue())

    def test_invalid_database_and_host_are_rejected_before_odoo_import(self):
        for database, host in (("production", "127.0.0.1"), ("odoo_portfolio", "remote.example"),
                               ("odoo_portfolio", "localhost"), ("odoo_portfolio", "")):
            with self.subTest(database=database, host=host):
                self.config.write_text(f"[options]\ndb_host = {host}\n")
                self.assert_denied_before_odoo([
                    "prepare_local_finance.py", "--config", str(self.config),
                    "--database", database, "--allow-local-acceptance",
                ])

    def test_missing_explicit_opt_in_is_rejected_before_odoo_import(self):
        self.assert_denied_before_odoo(self.argv()[:-1])

    def test_missing_malformed_and_wrong_section_configs_are_rejected(self):
        self.config.unlink()
        self.assert_denied_before_odoo(self.argv())
        for content in ("invalid INI data", "[unrelated]\ndb_host = 127.0.0.1\n"):
            with self.subTest(content=content):
                self.config.write_text(content)
                self.assert_denied_before_odoo(self.argv())

    def test_check_and_enable_are_mutually_exclusive_before_odoo_import(self):
        self.assert_denied_before_odoo(self.argv("--check", "--enable-fulfillment"))

    def test_credential_and_unknown_flags_are_rejected_without_files(self):
        self.assert_denied_before_odoo(self.argv("--credentials", str(self.directory / "secrets.json")))
        self.assert_denied_before_odoo(self.argv("--enable-every-company"))

    def fake_odoo(self):
        result = {"database": "odoo_portfolio", "namespace": "portfolio_trade_finance", "ready": True}
        helper = Mock(return_value=result)
        cursor = Mock()
        cursor.__enter__ = Mock(return_value=cursor)
        cursor.__exit__ = Mock(return_value=False)
        registry = Mock(return_value=SimpleNamespace(cursor=Mock(return_value=cursor)))
        company = SimpleNamespace(id=501, ids=[501])
        actor = SimpleNamespace(has_group=Mock(return_value=True), company_ids=company)
        initial_env, company_env = MagicMock(), MagicMock()
        for env in (initial_env, company_env):
            env.cr, env.uid, env.user = cursor, 1, actor
            env.context = {}
            env.ref.return_value = company
            env.__getitem__.return_value.with_user.return_value.env = env
            env.return_value = company_env
            env.with_context.return_value = company_env
            env.__getitem__.return_value.with_company.return_value.env = company_env
        environment = Mock(return_value=initial_env)
        modules = {}
        for name in ("odoo", "odoo.modules", "odoo.modules.registry", "odoo.addons",
                     "odoo.addons.portfolio_trade", "odoo.addons.portfolio_trade.local_finance"):
            modules[name] = ModuleType(name)
        modules["odoo"].SUPERUSER_ID = 1
        modules["odoo"].api = SimpleNamespace(Environment=environment)
        parse_config = Mock()
        modules["odoo"].tools = SimpleNamespace(config=SimpleNamespace(parse_config=parse_config))
        modules["odoo.modules.registry"].Registry = registry
        modules["odoo.addons.portfolio_trade.local_finance"].prepare_local_finance = helper
        return modules, helper, cursor, registry, environment, parse_config, result

    def invoke_valid(self, *extra):
        modules, helper, cursor, registry, environment, parse_config, result = self.fake_odoo()
        output = io.StringIO()
        with patch.dict(sys.modules, modules), patch.object(sys, "argv", self.argv(*extra)), \
                contextlib.redirect_stdout(output):
            self.assertEqual(self.module.main(), 0)
        self.assertEqual(json.loads(output.getvalue()), result)
        self.assertNotIn("NEVER-PRINT-THIS", output.getvalue())
        registry.assert_called_once_with("odoo_portfolio")
        self.assertEqual(helper.call_args.args[0].cr, cursor)
        self.assertIn(str(self.config.resolve()), parse_config.call_args.args[0])
        self.assertFalse((self.directory / "acceptance-accounts.json").exists())
        return helper, cursor, helper.call_args.args[0]

    def test_default_preparation_calls_plain_helper_and_commits_once(self):
        helper, cursor, env = self.invoke_valid()
        helper.assert_called_once_with(env, allow_local=True, check_only=False, enable_fulfillment=False)
        cursor.commit.assert_called_once_with()

    def test_check_calls_read_only_helper_without_commit(self):
        helper, cursor, env = self.invoke_valid("--check")
        helper.assert_called_once_with(env, allow_local=True, check_only=True, enable_fulfillment=False)
        cursor.commit.assert_not_called()

    def test_enable_is_forwarded_only_when_explicit(self):
        helper, cursor, env = self.invoke_valid("--enable-fulfillment")
        helper.assert_called_once_with(env, allow_local=True, check_only=False, enable_fulfillment=True)
        cursor.commit.assert_called_once_with()

    def test_helper_failure_never_commits(self):
        modules, helper, cursor, *_ = self.fake_odoo()
        helper.side_effect = RuntimeError("Injected helper failure")
        with patch.dict(sys.modules, modules), patch.object(sys, "argv", self.argv()), \
                self.assertRaisesRegex(RuntimeError, "Injected helper failure"):
            self.module.main()
        cursor.commit.assert_not_called()
