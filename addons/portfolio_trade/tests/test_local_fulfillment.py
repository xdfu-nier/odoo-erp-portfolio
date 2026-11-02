"""Real-role local acceptance using the genuine M1 and minimal CNY fixtures."""
import importlib
import importlib.util
import json
import secrets
import unittest
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from odoo import SUPERUSER_ID, Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged

from ..acceptance_data import ROLE_GROUPS, prepare_acceptance
from ..local_finance import prepare_local_finance
from ..models.financial_policy import financial_facts, physical_facts

ACCEPTANCE = "portfolio_trade_acceptance"
FINANCE = "portfolio_trade_finance"
NAMESPACE = "portfolio_trade_fulfillment"
IDENTITIES = {
    "sale_order": "sale.order", "purchase_order": "purchase.order",
    "receipt_first": "stock.picking", "receipt_second": "stock.picking",
    "delivery_first": "stock.picking", "delivery_second": "stock.picking",
    "return_picking": "stock.picking", "invoice_first": "account.move",
    "invoice_second": "account.move", "credit_note": "account.move",
}


class _UndoFixture(Exception):
    pass


@tagged("post_install", "-at_install", "portfolio_local_fulfillment")
class TestLocalFulfillment(TransactionCase):
    def setUp(self):
        super().setUp()
        # These real preparations deliberately precede the missing-API assertion
        # so RED proves that the M1 / minimal-finance native fixtures execute.
        prepare_acceptance(
            self.env, {role: secrets.token_urlsafe(24) for role in ROLE_GROUPS}, allow_local=True,
        )
        self.company, self.other = self.owned("company_a"), self.owned("company_b")
        self.local_env = self.env(context={"allowed_company_ids": self.company.ids})
        self.local_env = self.local_env["res.company"].with_company(self.company).env
        prepared = prepare_local_finance(self.local_env, allow_local=True, enable_fulfillment=True)
        self.assertTrue(prepared["ready"])
        self.assertTrue(self.company.portfolio_fulfillment_enabled)
        self.assertEqual(self.owned("initial_bearing_move").quantity, 40)
        self.assertEqual(len(self.markers(ACCEPTANCE)), 19)
        self.actors = {role: self.owned(f"user_{role}") for role in ROLE_GROUPS}
        # Native partial-operation chatter needs an ordinary sender. This is
        # explicit test configuration, independent of the sender preparation API.
        for role, user in self.actors.items():
            if role != "other_sales":
                user.write({"email": f"portfolio.acceptance.{role}@portfolio.invalid"})
        name = "odoo.addons.portfolio_trade.local_fulfillment"
        self.assertIsNotNone(importlib.util.find_spec(name), "Explicit local fulfillment API is missing")
        self.module = importlib.import_module(name)
        self.assertTrue(callable(getattr(self.module, "run_local_fulfillment", None)))

    def markers(self, namespace=NAMESPACE):
        return self.env["ir.model.data"].search([("module", "=", namespace)], order="name")

    def owned(self, key, namespace=ACCEPTANCE):
        marker = self.markers(namespace).filtered(lambda item: item.name == key)
        return self.env[marker.model].browse(marker.res_id) if marker else self.env["ir.model.data"]

    def identities(self, namespace=NAMESPACE):
        return tuple((item.name, item.model, item.res_id) for item in self.markers(namespace))

    def run_flow(self, **kwargs):
        return self.module.run_local_fulfillment(
            kwargs.pop("env", self.local_env), allow_local=kwargs.pop("allow_local", True), **kwargs,
        )

    @contextmanager
    def mutation(self):
        try:
            with self.cr.savepoint():
                yield
                raise _UndoFixture()
        except _UndoFixture:
            pass

    @staticmethod
    def scalar(value):
        if hasattr(value, "ids"):
            return tuple(value.ids)
        return str(value) if hasattr(value, "isoformat") else value

    def live_snapshot(self):
        """Flush/invalidate and inspect persisted rows, never pre-failure caches."""
        self.env.flush_all()
        self.env.invalidate_all()
        result = []
        models = {
            "sale.order": ("state", "create_uid", "write_uid", "write_date", "partner_id", "portfolio_revision", "portfolio_approval_state"),
            "sale.order.line": ("order_id", "product_id", "product_uom_qty", "price_unit", "discount"),
            "portfolio.sale.approval": ("order_id", "event", "actor_id", "fingerprint"),
            "purchase.order": ("state", "create_uid", "portfolio_source_sale_id", "write_date"),
            "purchase.order.line": ("order_id", "product_id", "product_qty", "price_unit"),
            "stock.picking": ("state", "create_uid", "write_date", "backorder_id", "origin"),
            "stock.move": ("state", "product_id", "quantity", "product_uom_qty", "sale_line_id", "purchase_line_id", "origin_returned_move_id", "picking_id", "move_line_ids"),
            "stock.move.line": ("state", "product_id", "quantity", "move_id", "location_id", "location_dest_id"),
            "stock.quant": ("product_id", "location_id", "quantity", "reserved_quantity", "inventory_quantity"),
            "account.move": ("state", "move_type", "create_uid", "write_date", "reversed_entry_id", "line_ids"),
            "account.move.line": ("move_id", "product_id", "display_type", "quantity", "price_unit", "discount", "balance", "amount_currency", "sale_line_ids"),
        }
        for model, fields in models.items():
            rows = self.local_env[model].sudo().with_context(active_test=False).search([
                ("company_id", "=", self.company.id),
            ], order="id")
            result.append((model, tuple((row.id, *(self.scalar(row[field]) for field in fields)) for row in rows)))
        result.append(("companies", tuple((company.id, company.portfolio_fulfillment_enabled,
                                          company.portfolio_approval_policy_version, company.portfolio_stock_revision)
                                         for company in (self.company, self.other))))
        result.append(("markers", self.identities(), self.identities(ACCEPTANCE), self.identities(FINANCE)))
        result.append(("actors", tuple((role, user.id, user.email, user.notification_type,
                                        tuple(user.groups_id.ids), tuple(user.company_ids.ids), user.company_id.id)
                                       for role, user in self.actors.items())))
        return tuple(result)

    def preserved_snapshot(self):
        self.env.flush_all()
        self.env.invalidate_all()
        users = tuple((role, user.id, user.active, user.login, tuple(user.groups_id.ids),
                       tuple(user.company_ids.ids), user.company_id.id, user.email, user.notification_type)
                      for role, user in self.actors.items())
        properties = []
        for key in ("customer", "vendor"):
            partner = self.owned(key).with_company(self.other)
            properties.append((key, partner.property_account_receivable_id.id, partner.property_account_payable_id.id))
        for key in ("bearing", "sensor", "bolt"):
            product = self.owned(f"product_{key}").product_tmpl_id.with_company(self.other)
            properties.append((key, product.property_account_income_id.id, product.property_account_expense_id.id,
                               product.categ_id.property_account_income_categ_id.id,
                               product.categ_id.property_account_expense_categ_id.id))
        original = self.owned("initial_bearing_move")
        evidence = (original.id, original.state, original.quantity, original.is_inventory,
                    original.product_id.id, original.location_id.id, original.location_dest_id.id,
                    tuple((line.id, line.quantity, line.state) for line in original.move_line_ids))
        other = (self.other.portfolio_masterdata_enabled, self.other.portfolio_approval_enabled,
                 self.other.portfolio_fulfillment_enabled, self.other.portfolio_approval_policy_version,
                 self.other.portfolio_stock_revision)
        return self.identities(ACCEPTANCE), self.identities(FINANCE), users, tuple(properties), evidence, other

    def assert_atomic_denied(self, operation, exception=UserError):
        # Odoo's assertRaises rolls back itself and cannot prove helper atomicity.
        with self.cr.savepoint():
            before = self.live_snapshot()
            with unittest.TestCase.assertRaises(self, exception):
                operation()
            self.assertEqual(self.live_snapshot(), before)

    def assert_native_completion(self):
        markers = self.markers()
        self.assertEqual({item.name: item.model for item in markers}, IDENTITIES)
        self.assertEqual(len(markers), 10)
        records = {key: self.owned(key, NAMESPACE) for key in IDENTITIES}
        self.assertEqual(len({(record._name, record.id) for record in records.values()}), 10)
        self.assertTrue(all(record.exists() and record.company_id == self.company for record in records.values()))
        sale, purchase = records["sale_order"], records["purchase_order"]
        line = sale.order_line
        self.assertEqual(sale.state, "sale")
        self.assertEqual(sale.portfolio_approval_state, "approved")
        self.assertTrue(sale.portfolio_approval_current)
        self.assertEqual(sale.create_uid, self.actors["sales"])
        self.assertEqual(sale.user_id, self.actors["sales"])
        self.assertEqual(sale.portfolio_creator_id, self.actors["sales"])
        self.assertEqual(sale.portfolio_approval_submitter_id, self.actors["sales"])
        history = sale.portfolio_approval_history_ids
        self.assertEqual(history.mapped("event"), ["submitted", "approved"])
        self.assertEqual(history.mapped("actor_id"), self.actors["sales"] | self.actors["manager"])
        self.assertNotEqual(history[-1].actor_id, sale.user_id)
        self.assertEqual(sale.partner_id, self.owned("customer"))
        self.assertEqual(sale.pricelist_id, self.owned("pricelist_cny", FINANCE))
        self.assertEqual(sale.currency_id.name, "CNY")
        self.assertEqual(len(line), 1)
        self.assertEqual(line.product_id, self.owned("product_bearing"))
        self.assertEqual((line.product_uom_qty, line.price_unit, line.discount), (100, 100, 15))
        self.assertFalse(line.tax_id)
        self.assertEqual(purchase.state, "purchase")
        self.assertEqual(purchase.create_uid, self.actors["buyer"])
        self.assertEqual(purchase.partner_id, self.owned("vendor"))
        self.assertEqual(purchase.portfolio_source_sale_id, sale)
        self.assertEqual(purchase.currency_id.name, "CNY")
        self.assertEqual(len(purchase.order_line), 1)
        self.assertEqual(purchase.order_line.product_id, line.product_id)
        self.assertEqual((purchase.order_line.product_qty, purchase.order_line.price_unit), (60, 60))
        self.assertFalse(purchase.order_line.taxes_id)
        for key, quantity, creator in (
            ("receipt_first", 30, "buyer"), ("receipt_second", 30, "warehouse"),
            ("delivery_first", 70, "sales"), ("delivery_second", 30, "warehouse"),
            ("return_picking", 10, "warehouse"),
        ):
            picking = records[key]
            self.assertEqual(picking.state, "done")
            # Pinned purchase_stock._create_picking explicitly uses UID 1 only
            # for the original receipt; the Buyer dispatch is checked below.
            creator_id = SUPERUSER_ID if key == "receipt_first" else self.actors[creator].id
            self.assertEqual(picking.create_uid.id, creator_id)
            self.assertEqual(len(picking.move_ids), 1)
            self.assertEqual(picking.move_ids.quantity, quantity)
            self.assertEqual(picking.move_ids.product_id, line.product_id)
            if key.startswith("receipt"):
                self.assertEqual(picking.move_ids.purchase_line_id, purchase.order_line)
                self.assertFalse(picking.move_ids.sale_line_id)
                self.assertEqual(picking.picking_type_code, "incoming")
            else:
                self.assertEqual(picking.move_ids.sale_line_id, line)
        self.assertEqual(records["receipt_second"].backorder_id, records["receipt_first"])
        self.assertEqual(records["delivery_second"].backorder_id, records["delivery_first"])
        returned = records["return_picking"].move_ids
        self.assertEqual(returned.origin_returned_move_id, records["delivery_first"].move_ids)
        self.assertTrue(returned.to_refund)
        self.assertEqual(returned.location_id.usage, "customer")
        self.assertEqual(returned.location_dest_id, self.owned("warehouse_a").lot_stock_id)
        for key, quantity, amount in (("invoice_first", 70, 5950), ("invoice_second", 30, 2550),
                                      ("credit_note", 10, 850)):
            move = records[key]
            self.assertEqual(move.create_uid, self.actors["finance"])
            self.assertEqual(move.state, "posted")
            self.assertEqual(move.move_type, "out_refund" if key == "credit_note" else "out_invoice")
            self.assertEqual(move.journal_id, self.owned("journal_sale", FINANCE))
            self.assertEqual(move.partner_id, self.owned("customer"))
            self.assertEqual(move.currency_id.name, "CNY")
            product_line = move.invoice_line_ids.filtered(lambda entry: entry.display_type == "product")
            self.assertEqual(len(product_line), 1)
            self.assertEqual(product_line.sale_line_ids, line)
            self.assertEqual(product_line.product_id, line.product_id)
            self.assertEqual((product_line.quantity, product_line.price_unit, product_line.discount), (quantity, 100, 15))
            self.assertFalse(product_line.tax_ids)
            self.assertEqual(product_line.price_subtotal, amount)
            self.assertEqual(product_line.balance, amount if key == "credit_note" else -amount)
            self.assertEqual(sum(move.line_ids.mapped("balance")), 0)
        self.assertEqual(records["credit_note"].reversed_entry_id, records["invoice_first"])
        native_line = line.with_user(self.actors["finance"]).with_company(self.company)
        physical, financial = physical_facts(native_line)[line.id], financial_facts(native_line)[line.id]
        self.assertEqual(physical, {"delivered": 100.0, "returned": 10.0})
        self.assertEqual((financial["invoice_posted"], financial["credit_posted"], financial["signed_posted"]), (100, 10, 90))
        self.assertFalse(financial["invalid_line_ids"])
        self.assertEqual(set(financial["originals"]), {records["invoice_first"].id, records["invoice_second"].id})
        self.assertEqual(financial["originals"][records["invoice_first"].id]["credit_posted"], 10)
        product_entries = self.local_env["account.move.line"].search([
            ("sale_line_ids", "in", line.ids), ("display_type", "=", "product"),
            ("move_id.state", "=", "posted"), ("move_id.move_type", "in", ["out_invoice", "out_refund"]),
        ])
        self.assertEqual(-sum(product_entries.mapped("balance")), 7650)
        self.assertEqual(line.product_id.with_company(self.company).qty_available, 10)
        return records

    def test_golden_flow_uses_existing_ordinary_roles_and_preserves_m1_b(self):
        preserved = self.preserved_snapshot()
        calls = {}
        actions = (
            ("sale.order", "action_portfolio_submit", "sales"),
            ("sale.order", "action_portfolio_approve", "manager"),
            ("sale.order", "action_confirm", "sales"),
            ("purchase.order", "button_confirm", "buyer"),
            ("stock.picking", "button_validate", "warehouse"),
            ("stock.return.picking", "action_create_returns", "warehouse"),
            ("sale.order", "action_portfolio_create_invoice", "finance"),
            ("portfolio.partial.credit", "action_create_credit", "finance"),
            ("account.move", "action_post", "finance"),
        )
        with ExitStack() as stack:
            for model, method, role in actions:
                original = getattr(type(self.env[model]), method)
                key = (model, method)
                calls[key] = []

                def observe(records, *args, _original=original, _key=key, **kwargs):
                    calls[_key].append((records.env.uid, records.env.su))
                    return _original(records, *args, **kwargs)

                stack.enter_context(patch.object(type(self.env[model]), method, observe))
            result = self.run_flow()
        for model, method, role in actions:
            observed = calls[(model, method)]
            self.assertTrue(observed, f"The real {model}.{method} must execute")
            self.assertEqual(observed[0], (self.actors[role].id, False))
            self.assertTrue(all(uid == self.actors[role].id for uid, _su in observed))
        records = self.assert_native_completion()
        self.assertEqual(self.preserved_snapshot(), preserved)
        self.assertEqual(result["database"], self.cr.dbname)
        self.assertEqual(result["namespace"], NAMESPACE)
        self.assertEqual(result["company_id"], self.company.id)
        self.assertEqual(result["identities"], {
            key: {"model": record._name, "id": record.id, "name": record.name}
            for key, record in records.items()
        })
        self.assertEqual(result["actors"], {role: self.actors[role].id for role in ("sales", "manager", "buyer", "warehouse", "finance")})
        self.assertEqual(result["physical"], {"delivered": 100.0, "returned": 10.0, "net": 90.0, "stock": 10.0})
        self.assertEqual(result["financial"], {
            "invoice_posted": 100.0, "credit_posted": 10.0, "signed_posted": 90.0,
            "net_amount": 7650.0, "currency": "CNY",
        })
        report = json.dumps(result, sort_keys=True).lower()
        for forbidden in ("password", "dsn", "config", "credential", "checked_at", "created_count", "reused_count"):
            self.assertNotIn(forbidden, report)

    def test_completed_rerun_only_reads_and_returns_byte_identical_report(self):
        first = self.run_flow()
        before = self.live_snapshot()
        with ExitStack() as stack:
            for model in ("sale.order", "purchase.order", "stock.picking", "stock.move", "account.move",
                          "portfolio.partial.credit", "stock.return.picking", "portfolio.fulfillment.trace"):
                stack.enter_context(patch.object(type(self.env[model]), "create", side_effect=AssertionError("A completed rerun must only validate")))
            second = self.run_flow()
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertEqual(self.live_snapshot(), before)
        self.assert_native_completion()

    def test_caller_stock_presentation_context_cannot_change_first_run_or_completed_replay(self):
        customer_location = self.env.ref("stock.stock_location_customers")
        product = self.owned("product_bearing").with_company(self.company)
        filtered = product.with_context(location=customer_location.id)
        caller = self.local_env(context={"allowed_company_ids": self.company.ids, "location": customer_location.id})
        self.assertEqual(product.qty_available, 40)
        self.assertEqual(filtered.qty_available, 0)
        try:
            first = self.run_flow(env=caller)
        except UserError:
            self.fail("Caller stock presentation context leaked into the first local fulfillment run")
        self.assert_native_completion()
        self.assertEqual(product.qty_available, 10)
        self.assertEqual(filtered.qty_available, 90)
        before = self.live_snapshot()
        try:
            second = self.run_flow(env=caller)
        except UserError:
            self.fail("Caller stock presentation context leaked into the completed local fulfillment replay")
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertEqual(self.live_snapshot(), before)
        self.assert_native_completion()

    def test_helper_never_commits_the_callers_transaction(self):
        with patch.object(type(self.cr), "commit", side_effect=AssertionError("The caller owns commit")):
            self.run_flow()
        self.assert_native_completion()

    def test_explicit_opt_in_is_required_including_default(self):
        self.assert_atomic_denied(lambda: self.run_flow(allow_local=False))
        self.assert_atomic_denied(lambda: self.module.run_local_fulfillment(self.local_env))

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
                self.assert_atomic_denied(lambda: self.run_flow(env=self.target_proxy(database, host)))

    def test_actual_settings_uid_is_required_even_when_sales_uses_sudo(self):
        forged = self.local_env["res.users"].with_user(self.actors["sales"]).sudo().env
        self.assert_atomic_denied(lambda: self.run_flow(env=forged), AccessError)

    def test_actual_and_active_a_membership_are_required_for_settings_actor(self):
        settings = self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "Fulfillment Settings actor", "login": "portfolio.local.fulfillment.settings",
            "company_id": self.other.id, "company_ids": [Command.set(self.other.ids)],
            "groups_id": [Command.set((self.env.ref("base.group_user") | self.env.ref("base.group_system")).ids)],
        })
        forged = self.local_env["res.users"].with_user(settings).sudo().env
        self.assert_atomic_denied(lambda: self.run_flow(env=forged), AccessError)
        settings.write({"company_ids": [Command.link(self.company.id)]})
        inactive = self.local_env["res.users"].with_user(settings).with_context(allowed_company_ids=self.other.ids).sudo().env
        self.assert_atomic_denied(lambda: self.run_flow(env=inactive), AccessError)

    def test_disabled_a_is_refused_without_automatically_enabling(self):
        self.company.portfolio_fulfillment_enabled = False
        self.assert_atomic_denied(self.run_flow)
        self.assertFalse(self.company.portfolio_fulfillment_enabled)

    def test_incomplete_finance_or_installed_readiness_is_refused_without_repair(self):
        for namespace, name in ((FINANCE, "account_income"), (FINANCE, "pricelist_cny"),
                                (ACCEPTANCE, "initial_bearing_move"),
                                ("portfolio_trade", "fulfillment_trace_form")):
            with self.subTest(namespace=namespace, name=name), self.mutation():
                self.markers(namespace).filtered(lambda item: item.name == name).unlink()
                self.assert_atomic_denied(self.run_flow)

    def test_partial_missing_wrong_model_stale_and_duplicate_flow_markers_are_refused(self):
        self.run_flow()
        for damage in ("missing", "unexpected", "wrong_model", "stale", "duplicate"):
            with self.subTest(damage=damage), self.mutation():
                marker = self.markers().filtered(lambda item: item.name == "credit_note")
                if damage == "missing":
                    marker.unlink()
                elif damage == "unexpected":
                    marker.copy({"name": "unrecognized"})
                elif damage == "wrong_model":
                    marker.write({"model": "sale.order"})
                elif damage == "stale":
                    marker.write({"res_id": 2147483647})
                else:
                    marker.write({"res_id": self.owned("invoice_first", NAMESPACE).id})
                self.assert_atomic_denied(self.run_flow)

    def test_user_edited_purchase_pricing_is_refused_without_restoration(self):
        self.run_flow()
        line = self.owned("purchase_order", NAMESPACE).order_line.with_user(self.actors["buyer"])
        line.write({"price_unit": 61})
        self.assert_atomic_denied(self.run_flow)
        self.assertEqual(line.price_unit, 61)

    def set_inventory(self, quantity):
        quant = self.local_env["stock.quant"].with_context(inventory_mode=True).search([
            ("product_id", "=", self.owned("product_bearing").id),
            ("location_id", "=", self.owned("warehouse_a").lot_stock_id.id),
        ])
        quant.inventory_quantity = quantity
        quant.action_apply_inventory()

    def test_new_flow_rejects_stock_35_and_extra_history_even_after_stock_returns_to_40(self):
        for quantities in ((35,), (35, 40)):
            with self.subTest(quantities=quantities), self.mutation():
                for quantity in quantities:
                    self.set_inventory(quantity)
                self.assertEqual(self.owned("initial_bearing_move").quantity, 40)
                self.assertEqual(self.owned("product_bearing").with_company(self.company).qty_available, quantities[-1])
                self.assert_atomic_denied(self.run_flow)
                self.assertFalse(self.markers())

    def test_completed_flow_rejects_changed_current_stock_without_reset(self):
        self.run_flow()
        self.set_inventory(11)
        self.assert_atomic_denied(self.run_flow)
        self.assertEqual(self.owned("product_bearing").with_company(self.company).qty_available, 11)

    def test_late_failure_after_real_credit_post_rolls_back_entire_flow(self):
        original = type(self.env["account.move"]).action_post
        observed = []

        def fail_after_native_credit(records, *args, **kwargs):
            result = original(records, *args, **kwargs)
            credits = records.filtered(lambda move: move.move_type == "out_refund")
            if credits:
                self.assertEqual(credits.state, "posted")
                self.assertEqual(credits.env.uid, self.actors["finance"].id)
                entries = credits.invoice_line_ids.filtered(lambda entry: entry.display_type == "product")
                self.assertEqual((entries.quantity, entries.balance), (10, 850))
                observed.append(credits.id)
                raise RuntimeError("Injected after the real final credit post")
            return result

        with patch.object(type(self.env["account.move"]), "action_post", fail_after_native_credit):
            self.assert_atomic_denied(self.run_flow, RuntimeError)
        self.assertEqual(len(observed), 1, "The injected failure must follow the real credit posting")
        self.assertFalse(self.markers())
        self.assertEqual(self.owned("product_bearing").with_company(self.company).qty_available, 40)

    def test_explicit_sender_preparation_sets_only_missing_a_emails_and_is_stable(self):
        prepare = getattr(self.module, "prepare_local_fulfillment_senders", None)
        self.assertTrue(callable(prepare), "Explicit local sender preparation API is missing")
        owners = {role: user for role, user in self.actors.items() if role != "other_sales"}
        for user in owners.values():
            user.write({"email": False})
        self.actors["sales"].write({"email": "existing.acceptance@portfolio.invalid"})
        before, preserved = self.live_snapshot(), self.preserved_snapshot()
        with patch.object(type(self.cr), "commit", side_effect=AssertionError("The caller owns commit")):
            first = prepare(self.local_env, allow_local=True)
        self.env.flush_all()
        self.env.invalidate_all()
        expected = {role: ("existing.acceptance@portfolio.invalid" if role == "sales"
                           else f"portfolio.acceptance.{role}@portfolio.invalid") for role in owners}
        self.assertEqual({role: user.email for role, user in owners.items()}, expected)
        self.assertTrue(all(user.notification_type == "inbox" for user in owners.values()))
        self.assertEqual(tuple(item for item in self.live_snapshot() if item[0] != "actors"),
                         tuple(item for item in before if item[0] != "actors"))
        after_preserved = self.preserved_snapshot()
        self.assertEqual(after_preserved[:2] + after_preserved[3:], preserved[:2] + preserved[3:])
        self.assertEqual(tuple(user[:-2] + user[-1:] for user in after_preserved[2]),
                         tuple(user[:-2] + user[-1:] for user in preserved[2]))
        self.assertEqual(after_preserved[2][-1], preserved[2][-1])  # PARTS-B's other_sales is untouched.
        self.assertEqual(first["database"], self.cr.dbname)
        self.assertEqual(first["company_id"], self.company.id)
        self.assertEqual(first["namespace"], NAMESPACE)
        self.assertEqual(first["senders"], {
            role: {"id": owners[role].id, "email": email} for role, email in expected.items()
        })
        stable = self.live_snapshot()
        with patch.object(type(self.env["res.users"]), "write", side_effect=AssertionError("Sender rerun must only validate")):
            second = prepare(self.local_env, allow_local=True)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertEqual(self.live_snapshot(), stable)
        for forbidden in ("password", "dsn", "config", "credential", "checked_at", "created_count", "reused_count"):
            self.assertNotIn(forbidden, json.dumps(first).lower())

    def test_sender_preparation_rejects_conflicts_and_is_atomic_after_real_writes(self):
        prepare = getattr(self.module, "prepare_local_fulfillment_senders", None)
        self.assertTrue(callable(prepare), "Explicit local sender preparation API is missing")
        owners = {role: user for role, user in self.actors.items() if role != "other_sales"}
        for user in owners.values():
            user.write({"email": False})
        for values in ({"email": "unknown@example.com"}, {"email": "broken-address"},
                       {"email": "one@portfolio.invalid, two@portfolio.invalid"}, {"notification_type": "email"}):
            with self.subTest(values=values), self.mutation():
                self.actors["warehouse"].write(values)
                self.assert_atomic_denied(lambda: prepare(self.local_env, allow_local=True))
        self.assert_atomic_denied(lambda: prepare(self.local_env))
        self.assert_atomic_denied(lambda: prepare(self.local_env, allow_local=False))
        for database, host in (("production", "127.0.0.1"), (self.cr.dbname, "remote.example")):
            with self.subTest(database=database, host=host):
                self.assert_atomic_denied(lambda: prepare(self.target_proxy(database, host), allow_local=True))
        forged = self.local_env["res.users"].with_user(self.actors["sales"]).sudo().env
        self.assert_atomic_denied(lambda: prepare(forged, allow_local=True), AccessError)
        inactive = self.local_env(context={"allowed_company_ids": self.other.ids})
        self.assert_atomic_denied(lambda: prepare(inactive, allow_local=True), AccessError)
        with self.mutation():
            self.company.portfolio_fulfillment_enabled = False
            prepare(self.local_env, allow_local=True)
            self.assertFalse(self.company.portfolio_fulfillment_enabled)
        with self.mutation():
            self.markers(FINANCE).filtered(lambda item: item.name == "journal_sale").unlink()
            self.assert_atomic_denied(lambda: prepare(self.local_env, allow_local=True))
        actual_write = type(self.env["res.users"]).write
        observed = []

        def fail_after_native_sender_write(users, values):
            result = actual_write(users, values)
            if "email" in values and any(user.id in users.ids for user in owners.values()):
                observed.extend(users.ids)
                if len(observed) >= 3:
                    self.assertTrue(any(user.email for user in owners.values()))
                    raise RuntimeError("Injected after real sender writes")
            return result

        with patch.object(type(self.env["res.users"]), "write", fail_after_native_sender_write):
            self.assert_atomic_denied(lambda: prepare(self.local_env, allow_local=True), RuntimeError)
        self.assertGreaterEqual(len(observed), 3)
        self.assertFalse(any(user.email for user in owners.values()))
