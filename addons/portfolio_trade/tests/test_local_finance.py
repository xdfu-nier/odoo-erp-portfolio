"""Explicit local finance preparation without a chart template or business flow."""
import importlib
import importlib.util
import json
import secrets
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged

from ..acceptance_data import EXPECTED_IDENTITIES, ROLE_GROUPS, prepare_acceptance

ACCEPTANCE = "portfolio_trade_acceptance"
NAMESPACE = "portfolio_trade_finance"
IDENTITIES = {
    "account_income": "account.account",
    "account_expense": "account.account",
    "account_receivable": "account.account",
    "account_payable": "account.account",
    "journal_sale": "account.journal",
    "pricelist_cny": "product.pricelist",
}
ACCOUNTS = {
    "account_income": ("PF4100", "Portfolio local acceptance income", "income"),
    "account_expense": ("PF5100", "Portfolio local acceptance expense", "expense"),
    "account_receivable": ("PF1100", "Portfolio local acceptance receivable", "asset_receivable"),
    "account_payable": ("PF2100", "Portfolio local acceptance payable", "liability_payable"),
}


class _RollbackFixture(Exception):
    """Undo only a deliberate subtest fixture mutation."""


@tagged("post_install", "-at_install", "portfolio_local_finance")
class TestLocalFinance(TransactionCase):
    def setUp(self):
        super().setUp()
        prepare_acceptance(
            self.env, {role: secrets.token_urlsafe(24) for role in ROLE_GROUPS}, allow_local=True,
        )
        self.acceptance_records = {key: self.owned(key) for key in EXPECTED_IDENTITIES}
        self.company, self.other = self.owned("company_a"), self.owned("company_b")
        self.local_env = self.env(context={"allowed_company_ids": (self.company | self.other).ids})
        self.local_env = self.local_env["res.company"].with_company(self.company).env
        name = "odoo.addons.portfolio_trade.local_finance"
        self.assertIsNotNone(importlib.util.find_spec(name), "Explicit local finance API is missing")
        self.module = importlib.import_module(name)
        self.assertTrue(callable(getattr(self.module, "prepare_local_finance", None)))

    def owned(self, key, namespace=ACCEPTANCE):
        # Search the live marker rather than relying on env.ref's XML-ID cache.
        marker = self.env["ir.model.data"].search([("module", "=", namespace), ("name", "=", key)])
        return self.env[marker.model].browse(marker.res_id) if marker else self.env["ir.model.data"]

    def markers(self, namespace=NAMESPACE):
        return self.env["ir.model.data"].search([("module", "=", namespace)], order="name")

    def identities(self, namespace=NAMESPACE):
        return tuple((item.name, item.model, item.res_id) for item in self.markers(namespace))

    def prepare(self, **kwargs):
        return self.module.prepare_local_finance(
            kwargs.pop("env", self.local_env), allow_local=kwargs.pop("allow_local", True), **kwargs,
        )

    def live_snapshot(self):
        """Read persisted rows and properties after flushing/invalidation."""
        self.env.flush_all()
        self.env.invalidate_all()
        counts = tuple((name, self.env[name].with_context(active_test=False).search_count([])) for name in (
            "account.account", "account.journal", "product.pricelist", "res.company", "res.users",
            "res.groups", "res.currency", "stock.move", "stock.quant", "ir.default",
        ))
        properties = []
        for company in (self.company, self.other):
            for key in ("customer", "vendor"):
                partner = self.acceptance_records[key].with_company(company)
                properties.append((company.id, key, partner.property_account_receivable_id.id,
                                   partner.property_account_payable_id.id))
            for key in ("bearing", "sensor", "bolt"):
                template = self.acceptance_records[f"product_{key}"].product_tmpl_id.with_company(company)
                properties.append((company.id, key, template.property_account_income_id.id,
                                   template.property_account_expense_id.id,
                                   template.categ_id.property_account_income_categ_id.id,
                                   template.categ_id.property_account_expense_categ_id.id))
        users = tuple((self.acceptance_records[f"user_{role}"].id,
                       tuple(self.acceptance_records[f"user_{role}"].groups_id.ids),
                       tuple(self.acceptance_records[f"user_{role}"].company_ids.ids)) for role in ROLE_GROUPS)
        currencies = tuple((item.id, item.active) for item in self.env["res.currency"].with_context(
            active_test=False,
        ).search([], order="id"))
        defaults = tuple((item.id, item.field_id.id, item.company_id.id, item.user_id.id, item.json_value)
                         for item in self.env["ir.default"].search([], order="id"))
        policies = tuple((company.id, company.currency_id.id, company.portfolio_fulfillment_enabled,
                          company.portfolio_approval_policy_version, company.portfolio_stock_revision)
                         for company in (self.company, self.other))
        return counts, tuple(properties), users, currencies, defaults, policies, self.identities(), self.identities(ACCEPTANCE)

    def assert_atomic_denied(self, operation, exception=UserError):
        # Odoo's self.assertRaises supplies its own rollback. Use stdlib inside
        # this outer cleanup savepoint so only the helper can undo partial work.
        with self.cr.savepoint():
            before = self.live_snapshot()
            with unittest.TestCase.assertRaises(self, exception):
                operation()
            self.assertEqual(self.live_snapshot(), before)

    @contextmanager
    def mutation(self):
        try:
            with self.cr.savepoint():
                yield
                raise _RollbackFixture()
        except _RollbackFixture:
            pass

    def assert_profile(self):
        markers = self.markers()
        self.assertEqual({item.name: item.model for item in markers}, IDENTITIES)
        self.assertEqual(len(markers), 6)
        for key, (code, name, account_type) in ACCOUNTS.items():
            account = self.owned(key, NAMESPACE).with_company(self.company)
            self.assertEqual((account.code, account.name, account.account_type), (code, name, account_type))
            self.assertEqual(account.company_ids, self.company)
            self.assertFalse(account.deprecated)
            self.assertFalse(account.currency_id)
            self.assertFalse(account.tax_ids)
            self.assertEqual(account.reconcile, key in {"account_receivable", "account_payable"})
        journal = self.owned("journal_sale", NAMESPACE)
        self.assertEqual((journal.type, journal.code, journal.name),
                         ("sale", "PFSAL", "Portfolio local acceptance sales"))
        self.assertEqual(journal.company_id, self.company)
        self.assertEqual(journal.currency_id, self.company.currency_id)
        self.assertEqual(journal.default_account_id, self.owned("account_income", NAMESPACE))
        pricelist = self.owned("pricelist_cny", NAMESPACE)
        self.assertEqual(pricelist.name, "Portfolio local acceptance CNY")
        self.assertEqual(pricelist.company_id, self.company)
        self.assertEqual(pricelist.currency_id, self.company.currency_id)
        self.assertEqual(self.owned("customer").with_company(self.company).property_account_receivable_id,
                         self.owned("account_receivable", NAMESPACE))
        self.assertEqual(self.owned("vendor").with_company(self.company).property_account_payable_id,
                         self.owned("account_payable", NAMESPACE))
        for key in ("bearing", "sensor", "bolt"):
            template = self.owned(f"product_{key}").product_tmpl_id.with_company(self.company)
            self.assertEqual(template.property_account_income_id, self.owned("account_income", NAMESPACE))
            self.assertEqual(template.property_account_expense_id, self.owned("account_expense", NAMESPACE))

    def new_account(self, company=None, **values):
        company = company or self.company
        return self.env["account.account"].with_company(company).create({
            "code": "PF9999", "name": "Unknown outside acceptance account", "account_type": "income",
            "company_ids": [Command.set(company.ids)], **values,
        })

    def test_preparation_creates_minimal_native_profile_and_preserves_fixture(self):
        before = self.live_snapshot()
        result = self.prepare()
        self.assert_profile()
        self.assertFalse(self.company.portfolio_fulfillment_enabled)
        self.assertFalse(self.other.portfolio_fulfillment_enabled)
        after = self.live_snapshot()
        self.assertEqual(after[2:5], before[2:5])  # Roles, currencies and ir.default are unchanged.
        self.assertEqual(after[5], before[5])
        self.assertEqual(after[7], before[7])
        self.assertEqual(len(after[7]), len(EXPECTED_IDENTITIES))
        self.assertEqual(self.owned("initial_bearing_move").quantity, 40)
        before_other = [item for item in before[1] if item[0] == self.other.id]
        self.assertEqual([item for item in after[1] if item[0] == self.other.id], before_other)
        for model, expected in (("account.account", 4), ("account.journal", 1), ("product.pricelist", 1)):
            self.assertEqual(dict(after[0])[model] - dict(before[0])[model], expected)
        self.assertTrue(result["ready"])
        self.assertEqual(result["created_count"], 6)
        self.assertEqual(result["reused_count"], 0)

    def test_report_contains_actual_module_version_and_safe_facts(self):
        result = self.prepare()
        module = self.env["ir.module.module"].search([("name", "=", "portfolio_trade")])
        self.assertEqual(result["module_version"], module.installed_version)
        self.assertEqual(result["database"], self.cr.dbname)
        self.assertEqual(result["namespace"], NAMESPACE)
        self.assertEqual(result["company_id"], self.company.id)
        self.assertEqual(result["currency"], "CNY")
        self.assertEqual(result["initial_bearing_quantity"], 40)
        self.assertEqual(result["bearing_quantity"], 40)
        self.assertEqual(set(result["identities"]), set(IDENTITIES))
        text = json.dumps(result).lower()
        for secret in ("password", "dsn", "db_host", "db_user", "config", "credentials"):
            self.assertNotIn(secret, text)

    def test_check_missing_profile_reports_six_keys_without_writes(self):
        before = self.live_snapshot()
        result = self.prepare(check_only=True)
        self.assertFalse(result["ready"])
        self.assertEqual(set(result["would_create"]), set(IDENTITIES))
        self.assertEqual(self.live_snapshot(), before)

    def test_complete_check_and_rerun_preserve_all_identities(self):
        self.prepare()
        before = self.live_snapshot()
        for options in ({"check_only": True}, {}):
            result = self.prepare(**options)
            self.assertTrue(result["ready"])
            self.assertEqual(result["created_count"], 0)
            self.assertEqual(result["reused_count"], 6)
            self.assertEqual(self.live_snapshot(), before)

    def test_explicit_enable_changes_only_a_and_plain_rerun_keeps_enabled(self):
        self.prepare(enable_fulfillment=True)
        self.assert_profile()
        self.assertTrue(self.company.portfolio_fulfillment_enabled)
        self.assertFalse(self.other.portfolio_fulfillment_enabled)
        before = self.live_snapshot()
        self.prepare()
        self.assertEqual(self.live_snapshot(), before)

    def test_enabled_a_without_profile_is_rejected(self):
        self.company.portfolio_fulfillment_enabled = True
        self.assert_atomic_denied(lambda: self.prepare())

    def test_check_cannot_enable_fulfillment(self):
        self.assert_atomic_denied(lambda: self.prepare(check_only=True, enable_fulfillment=True))

    def test_helper_requires_explicit_opt_in_including_default(self):
        self.assert_atomic_denied(lambda: self.prepare(allow_local=False))
        self.assert_atomic_denied(lambda: self.module.prepare_local_finance(self.local_env))

    def target_proxy(self, database, host):
        actual = self.local_env

        class TargetEnvironment:
            cr = SimpleNamespace(dbname=database, _cnx=SimpleNamespace(info=SimpleNamespace(host=host)))
            uid, user, context = actual.uid, actual.user, actual.context

            def __call__(self, **kwargs):
                return actual(**kwargs)

            def __getitem__(self, name):
                return actual[name]

        return TargetEnvironment()

    def test_actual_database_and_host_are_independently_allowlisted(self):
        for database, host in (("production", "127.0.0.1"), (self.cr.dbname, "remote.example"),
                               (self.cr.dbname, "localhost"), (self.cr.dbname, None)):
            with self.subTest(database=database, host=host):
                self.assert_atomic_denied(lambda: self.prepare(env=self.target_proxy(database, host)))

    def test_sudo_keeps_original_sales_settings_denial(self):
        sales_env = self.local_env["res.users"].with_user(self.owned("user_sales")).sudo().env
        self.assert_atomic_denied(lambda: self.prepare(env=sales_env), AccessError)

    def test_settings_actor_requires_actual_and_active_company_a(self):
        settings = self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "Finance preparation settings actor", "login": "portfolio.local.finance.settings",
            "company_id": self.other.id, "company_ids": [Command.set(self.other.ids)],
            "groups_id": [Command.set((self.env.ref("base.group_user") | self.env.ref("base.group_system")).ids)],
        })
        forged = self.local_env["res.users"].with_user(settings).sudo().env
        self.assert_atomic_denied(lambda: self.prepare(env=forged), AccessError)
        settings.write({"company_ids": [Command.link(self.company.id)]})
        inactive = self.local_env["res.users"].with_user(settings).with_context(allowed_company_ids=self.other.ids).sudo().env
        self.assert_atomic_denied(lambda: self.prepare(env=inactive), AccessError)

    def test_missing_acceptance_identity_never_creates_a_replacement(self):
        for key in ("initial_bearing_move", "company_b", "product_sensor", "user_sales"):
            with self.subTest(identity=key), self.mutation():
                self.markers(ACCEPTANCE).filtered(lambda marker: marker.name == key).unlink()
                self.assert_atomic_denied(lambda: self.prepare())

    def test_unexpected_acceptance_identity_is_rejected(self):
        self.env["ir.model.data"].create({
            "module": ACCEPTANCE, "name": "unexpected", "model": "res.company", "res_id": self.company.id,
        })
        self.assert_atomic_denied(lambda: self.prepare(check_only=True))

    def test_original_inventory_evidence_requires_genuine_done_40(self):
        for values in ({"quantity": 39}, {"is_inventory": False}, {"state": "draft"},
                       {"product_id": self.owned("product_bolt").id},
                       {"company_id": self.other.id},
                       {"location_dest_id": self.owned("warehouse_a").wh_input_stock_loc_id.id}):
            with self.subTest(values=values), self.mutation():
                self.owned("initial_bearing_move").write(values)
                self.assert_atomic_denied(lambda: self.prepare(check_only=True))
        with self.subTest(source_usage="internal"), self.mutation():
            move = self.owned("initial_bearing_move")
            details = move.move_line_ids
            # Native DONE-move writes would unlink its immutable detail lines.
            # This source contains the initial negative inventory counterpart,
            # so the native location write accepts the invalid source usage.
            move.location_id.write({"usage": "internal"})
            self.assertEqual(move.state, "done")
            self.assertEqual(move.move_line_ids, details)
            self.assert_atomic_denied(lambda: self.prepare(check_only=True))

    def test_current_stock_can_change_while_original_move_stays_40(self):
        bearing = self.owned("product_bearing").with_company(self.company)
        quant = self.local_env["stock.quant"].with_company(self.company).search([
            ("product_id", "=", bearing.id),
            ("location_id", "=", self.owned("warehouse_a").lot_stock_id.id),
        ])
        quant.with_context(inventory_mode=True).inventory_quantity = 35
        quant.action_apply_inventory()
        self.assertEqual(self.owned("initial_bearing_move").quantity, 40)
        self.assertEqual(bearing.qty_available, 35)
        result = self.prepare()
        self.assertEqual(result["bearing_quantity"], 35)
        self.assertEqual(result["initial_bearing_quantity"], 40)

    def test_non_cny_company_and_foreign_owned_fixture_are_rejected(self):
        for record, values in ((self.company, {"currency_id": self.env.ref("base.USD").id}),
                               (self.owned("customer"), {"company_id": self.other.id}),
                               (self.owned("product_bolt"), {"company_id": self.other.id}),
                               (self.owned("user_other_sales"), {"company_ids": [Command.link(self.company.id)]})):
            with self.subTest(model=record._name, values=values), self.mutation():
                record.write(values)
                self.assert_atomic_denied(lambda: self.prepare())

    def test_unmarked_account_code_and_name_conflicts_are_not_adopted(self):
        for values in ({"code": ACCOUNTS["account_income"][0]},
                       {"name": ACCOUNTS["account_expense"][1]},
                       {"code": ACCOUNTS["account_receivable"][0], "deprecated": True}):
            with self.subTest(values=values), self.mutation():
                self.new_account(**values)
                self.assert_atomic_denied(lambda: self.prepare())
                self.assert_atomic_denied(lambda: self.prepare(check_only=True))

    def test_unmarked_inactive_journal_and_pricelist_conflicts_are_not_adopted(self):
        for model, values in (
            ("account.journal", {"name": "Unmarked journal", "code": "PFSAL", "type": "sale", "active": False}),
            ("account.journal", {"name": "Portfolio local acceptance sales", "code": "ZZZ", "type": "sale"}),
            ("product.pricelist", {"name": "Portfolio local acceptance CNY", "currency_id": self.company.currency_id.id, "active": False}),
        ):
            with self.subTest(model=model), self.mutation():
                self.local_env[model].create({"company_id": self.company.id, **values})
                self.assert_atomic_denied(lambda: self.prepare())

    def test_partial_or_foreign_finance_namespace_is_never_repaired(self):
        self.prepare()
        for operation in (
            lambda: self.markers().filtered(lambda marker: marker.name == "account_expense").unlink(),
            lambda: self.markers().filtered(lambda marker: marker.name == "account_income").write({"model": "res.company", "res_id": self.company.id}),
            lambda: self.env["ir.model.data"].create({"module": NAMESPACE, "name": "extra", "model": "res.company", "res_id": self.company.id}),
        ):
            with self.mutation():
                operation()
                self.assert_atomic_denied(lambda: self.prepare(enable_fulfillment=True))

    def test_altered_finance_records_fail_closed(self):
        self.prepare()
        alterations = (
            ("account_income", {"code": "PFALTERED"}),
            ("account_expense", {"name": "User edited expense"}),
            ("account_income", {"account_type": "income_other"}),
            ("account_payable", {"deprecated": True}),
            ("account_income", {
                "code_mapping_ids": [Command.create({"company_id": self.other.id, "code": "PFB4100"})],
                "company_ids": [Command.link(self.other.id)],
            }),
            ("account_income", {"currency_id": self.env.ref("base.USD").id}),
            ("journal_sale", {"currency_id": self.env.ref("base.USD").id}),
            ("journal_sale", {"default_account_id": self.owned("account_expense", NAMESPACE).id}),
            ("pricelist_cny", {"currency_id": self.env.ref("base.USD").id}),
            ("pricelist_cny", {"active": False}),
        )
        for key, values in alterations:
            with self.subTest(identity=key, values=values), self.mutation():
                self.owned(key, NAMESPACE).with_company(self.company).write(values)
                self.assert_atomic_denied(lambda: self.prepare(check_only=True))

    def test_unknown_partner_and_product_properties_are_never_overwritten(self):
        account = self.new_account()
        receivable = self.new_account(code="PF9998", account_type="asset_receivable", reconcile=True)
        payable = self.new_account(code="PF9997", account_type="liability_payable", reconcile=True)
        for record, values in (
            (self.owned("customer"), {"property_account_receivable_id": receivable.id}),
            (self.owned("vendor"), {"property_account_payable_id": payable.id}),
            (self.owned("product_sensor").product_tmpl_id, {"property_account_income_id": account.id}),
            (self.owned("product_bolt").product_tmpl_id, {"property_account_expense_id": account.id}),
        ):
            with self.subTest(model=record._name, values=values), self.mutation():
                record.with_company(self.company).write(values)
                self.assert_atomic_denied(lambda: self.prepare())

    def test_missing_binding_on_complete_profile_is_not_silently_repaired(self):
        self.prepare()
        self.owned("product_bearing").product_tmpl_id.with_company(self.company).property_account_income_id = False
        self.assert_atomic_denied(lambda: self.prepare())

    def test_late_failure_rolls_back_records_markers_properties_and_policy(self):
        company_type = type(self.company)
        original_write = company_type.write

        def fail_after_enabling(records, values):
            result = original_write(records, values)
            if self.company.id in records.ids and values.get("portfolio_fulfillment_enabled") is True:
                self.assert_profile()  # Injection is after every finance write.
                raise UserError("Injected late local finance failure")
            return result

        with patch.object(company_type, "write", fail_after_enabling):
            self.assert_atomic_denied(lambda: self.prepare(enable_fulfillment=True))
