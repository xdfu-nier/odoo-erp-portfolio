"""The local flow CLI gates imports, owns commit, and emits only safe JSON."""
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
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


class RunLocalFulfillmentTests(unittest.TestCase):
    def setUp(self):
        script = ROOT / "scripts" / "run_local_fulfillment.py"
        self.assertTrue(script.is_file(), "Explicit local fulfillment CLI is missing")
        spec = importlib.util.spec_from_file_location("local_fulfillment_cli_under_test", script)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        base = ROOT / ".cache" / "local-fulfillment-tests"
        base.mkdir(parents=True, exist_ok=True)
        directory = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.config = self.directory / "odoo.conf"
        self.config.write_text("[options]\ndb_host = 127.0.0.1\ndb_password = NEVER-PRINT-THIS\n")

    def argv(self, *extra):
        return ["run_local_fulfillment.py", "--config", str(self.config),
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
                self.config.write_text(f"[options]\ndb_host = {host}\ndb_password = NEVER-PRINT-THIS\n")
                self.assert_denied_before_odoo([
                    "run_local_fulfillment.py", "--config", str(self.config),
                    "--database", database, "--allow-local-acceptance",
                ])

    def test_missing_explicit_opt_in_is_rejected_before_odoo_import(self):
        self.assert_denied_before_odoo(self.argv()[:-1])

    def test_missing_malformed_and_wrong_section_configs_are_rejected_without_secrets(self):
        self.config.unlink()
        self.assert_denied_before_odoo(self.argv())
        for content in ("NEVER-PRINT-THIS invalid INI data", "[unrelated]\ndb_password = NEVER-PRINT-THIS\n"):
            with self.subTest(content=content):
                self.config.write_text(content)
                self.assert_denied_before_odoo(self.argv())

    def test_credential_and_preparation_flags_are_not_accepted(self):
        for extra in (("--credentials", str(self.directory / "credentials.json")),
                      ("--enable-fulfillment",), ("--prepare-finance",), ("--check",)):
            with self.subTest(extra=extra):
                self.assert_denied_before_odoo(self.argv(*extra))

    def fake_odoo(self):
        result = {
            "database": "odoo_portfolio", "company_id": 501,
            "namespace": "portfolio_trade_fulfillment", "identities": {"sale_order": {"id": 701}},
            "physical": {"delivered": 100.0, "returned": 10.0, "net": 90.0, "stock": 10.0},
            "financial": {"invoice_posted": 100.0, "credit_posted": 10.0,
                          "signed_posted": 90.0, "net_amount": 7650.0, "currency": "CNY"},
        }
        helper = Mock(return_value=result)
        cursor = Mock()
        cursor.__enter__ = Mock(return_value=cursor)
        cursor.__exit__ = Mock(return_value=False)
        registry = Mock(return_value=SimpleNamespace(cursor=Mock(return_value=cursor)))
        company = SimpleNamespace(id=501, ids=[501])

        class Actor:
            company_ids = company
            has_group = Mock(return_value=True)

            @property
            def password(self):
                raise AssertionError("The CLI must never read role credentials")

        actor = Actor()

        class Environment:
            def __init__(self, context=None):
                self.cr, self.uid, self.user = cursor, 1, actor
                self.context = context or {}

            def __call__(self, **kwargs):
                return Environment(kwargs.get("context", self.context))

            def __getitem__(self, name):
                if name != "res.users":
                    raise AssertionError(f"The CLI must not mutate business models: {name}")
                return SimpleNamespace(with_user=lambda uid: SimpleNamespace(env=self))

            def ref(self, name):
                if name != "portfolio_trade_acceptance.company_a":
                    raise AssertionError(f"Unexpected CLI identity lookup: {name}")
                return company

        initial_env = Environment()
        environment = Mock(return_value=initial_env)
        modules = {name: ModuleType(name) for name in (
            "odoo", "odoo.modules", "odoo.modules.registry", "odoo.addons",
            "odoo.addons.portfolio_trade", "odoo.addons.portfolio_trade.local_fulfillment",
        )}
        modules["odoo"].SUPERUSER_ID = 1
        modules["odoo"].api = SimpleNamespace(Environment=environment)
        parse_config = Mock()
        modules["odoo"].tools = SimpleNamespace(config=SimpleNamespace(parse_config=parse_config))
        modules["odoo.modules.registry"].Registry = registry
        modules["odoo.addons.portfolio_trade.local_fulfillment"].run_local_fulfillment = helper
        return modules, helper, cursor, registry, parse_config, result, actor

    def invoke(self, modules):
        output = io.StringIO()
        with patch.dict(sys.modules, modules), patch.object(sys, "argv", self.argv()), \
                contextlib.redirect_stdout(output):
            status = self.module.main()
        return status, output.getvalue()

    def test_valid_cli_scopes_actual_member_to_a_calls_helper_and_commits_once(self):
        modules, helper, cursor, registry, parse_config, result, _actor = self.fake_odoo()
        status, output = self.invoke(modules)
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output), result)
        registry.assert_called_once_with("odoo_portfolio")
        helper.assert_called_once()
        env = helper.call_args.args[0]
        self.assertIs(env.cr, cursor)
        self.assertEqual(env.context["allowed_company_ids"], [501])
        self.assertEqual(env.uid, 1)
        self.assertEqual(helper.call_args.kwargs, {"allow_local": True})
        cursor.commit.assert_called_once_with()
        self.assertIn(str(self.config.resolve()), parse_config.call_args.args[0])
        self.assertEqual(set(path.name for path in self.directory.iterdir()), {"odoo.conf"})

    def test_helper_failure_never_commits_or_emits_a_success_report(self):
        modules, helper, cursor, *_ = self.fake_odoo()
        helper.side_effect = RuntimeError("Injected flow failure")
        output = io.StringIO()
        with patch.dict(sys.modules, modules), patch.object(sys, "argv", self.argv()), \
                contextlib.redirect_stdout(output), self.assertRaisesRegex(RuntimeError, "Injected flow failure"):
            self.module.main()
        cursor.commit.assert_not_called()
        self.assertEqual(output.getvalue(), "")

    def test_cli_does_not_grant_settings_or_actual_company_membership(self):
        for denial in ("settings", "membership"):
            with self.subTest(denial=denial):
                modules, helper, cursor, _registry, _parse, _result, actor = self.fake_odoo()
                if denial == "settings":
                    actor.has_group.return_value = False
                else:
                    actor.company_ids = SimpleNamespace(ids=[502])
                with self.assertRaises(PermissionError):
                    self.invoke(modules)
                helper.assert_not_called()
                cursor.commit.assert_not_called()

    def test_valid_reports_are_stable_safe_json_and_do_not_read_or_create_credentials(self):
        modules, _helper, _cursor, _registry, _parse, result, _actor = self.fake_odoo()
        first = self.invoke(modules)[1]
        second = self.invoke(modules)[1]
        self.assertEqual(first, second)
        self.assertEqual(json.loads(first), result)
        for secret in ("NEVER-PRINT-THIS", "password", "dsn", "config", "credentials"):
            self.assertNotIn(secret, first)
        self.assertEqual(set(path.name for path in self.directory.iterdir()), {"odoo.conf"})

    def test_prepare_senders_calls_only_explicit_sender_helper_and_commits_once(self):
        modules, flow, cursor, _registry, _parse, _result, _actor = self.fake_odoo()
        result = {"database": "odoo_portfolio", "company_id": 501,
                  "namespace": "portfolio_trade_fulfillment", "senders": {}}
        prepare = Mock(return_value=result)
        modules["odoo.addons.portfolio_trade.local_fulfillment"].prepare_local_fulfillment_senders = prepare
        output = io.StringIO()
        with patch.dict(sys.modules, modules), patch.object(sys, "argv", self.argv("--prepare-senders")), \
                contextlib.redirect_stdout(output):
            try:
                status = self.module.main()
            except SystemExit as error:
                self.fail(f"Explicit --prepare-senders option is missing (exit {error.code})")
            self.assertEqual(status, 0)
        flow.assert_not_called()
        prepare.assert_called_once()
        env = prepare.call_args.args[0]
        self.assertIs(env.cr, cursor)
        self.assertEqual(env.context["allowed_company_ids"], [501])
        self.assertEqual(prepare.call_args.kwargs, {"allow_local": True})
        cursor.commit.assert_called_once_with()
        self.assertEqual(json.loads(output.getvalue()), result)
        self.assertNotIn("NEVER-PRINT-THIS", output.getvalue())
