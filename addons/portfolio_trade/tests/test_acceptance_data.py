import importlib
import importlib.util
import secrets

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged

ROLES = ("sales", "manager", "manager2", "buyer", "warehouse", "finance", "other_sales")
NAMESPACE = "portfolio_trade_acceptance"


@tagged("post_install", "-at_install", "portfolio_acceptance_data")
class TestAcceptanceData(TransactionCase):
    def prepare(self, **kwargs):
        name = "odoo.addons.portfolio_trade.acceptance_data"
        self.assertIsNotNone(importlib.util.find_spec(name), "Explicit acceptance-data tool is not implemented")
        module = importlib.import_module(name)
        return module.prepare_acceptance(
            self.env, {key: secrets.token_urlsafe(24) for key in ROLES},
            allow_local=kwargs.pop("allow_local", True), **kwargs,
        )

    def owned(self, key):
        return self.env.ref(f"{NAMESPACE}.{key}")

    def test_fixture_roles_catalog_and_standard_initial_inventory(self):
        result = self.prepare()
        company, other = self.owned("company_a"), self.owned("company_b")
        self.assertEqual(company.name, "PARTS-A")
        self.assertEqual(company.currency_id.name, "CNY")
        self.assertTrue(company.portfolio_masterdata_enabled)
        for key, group in (
            ("sales", "sales_team.group_sale_salesman"),
            ("manager", "sales_team.group_sale_manager"),
            ("manager2", "sales_team.group_sale_manager"),
            ("buyer", "purchase.group_purchase_user"),
            ("warehouse", "stock.group_stock_user"),
            ("finance", "account.group_account_invoice"),
            ("other_sales", "sales_team.group_sale_salesman"),
        ):
            user = self.owned(f"user_{key}")
            self.assertTrue(user.has_group(group), key)
            self.assertFalse(user.has_group("base.group_system"), key)
            self.assertEqual(user.company_ids, other if key == "other_sales" else company)
        self.assertFalse(self.owned("user_sales").has_group("sales_team.group_sale_salesman_all_leads"))
        self.assertEqual(self.owned("customer").ref, "CUST-001")
        self.assertEqual(self.owned("vendor").ref, "VEND-001")
        bearing = self.owned("product_bearing")
        self.assertEqual(bearing.default_code, "BRG-6205")
        self.assertEqual(bearing.invoice_policy, "delivery")
        self.assertEqual(bearing.tracking, "none")
        self.assertEqual(bearing.list_price, 100)
        self.assertEqual(bearing.with_company(company).standard_price, 60)
        self.assertTrue(bearing.seller_ids)
        sensor = self.owned("product_sensor")
        self.assertEqual(sensor.tracking, "serial")
        self.assertEqual(sensor.with_company(company).qty_available, 0)
        self.assertEqual(self.owned("product_bolt").default_code, "BOLT-M8")
        move = self.owned("initial_bearing_move")
        self.assertTrue(move.is_inventory)
        self.assertEqual(move.state, "done")
        self.assertEqual(move.quantity, 40)
        self.assertEqual(move.product_id, bearing)
        self.assertEqual(bearing.with_company(company).qty_available, 40)
        self.assertNotIn("password", repr(result).lower())
        users = self.env["res.users"].search([("login", "like", "portfolio.acceptance.%")])
        self.assertFalse(self.env["mail.mail"].search([("recipient_ids", "in", users.partner_id.ids)]))

    def test_rerun_preserves_records_user_edits_and_inventory_evidence(self):
        self.prepare()
        markers = self.env["ir.model.data"].search([("module", "=", NAMESPACE)])
        identities = {(item.name, item.model, item.res_id) for item in markers}
        bearing = self.owned("product_bearing")
        bearing.list_price = 123
        user = self.owned("user_sales")
        user.name = "User edited salesperson"
        move_count = self.env["stock.move"].search_count([("product_id", "=", bearing.id)])
        self.prepare()
        self.assertEqual(identities, {
            (item.name, item.model, item.res_id)
            for item in self.env["ir.model.data"].search([("module", "=", NAMESPACE)])
        })
        self.assertEqual(bearing.list_price, 123)
        self.assertEqual(user.name, "User edited salesperson")
        self.assertEqual(self.env["stock.move"].search_count([("product_id", "=", bearing.id)]), move_count)
        self.assertEqual(bearing.with_company(self.owned("company_a")).qty_available, 40)

    def test_unknown_company_name_conflict_is_not_adopted(self):
        self.env["res.company"].create({"name": "PARTS-A"})
        with self.cr.savepoint(), self.assertRaisesRegex(UserError, "conflict"):
            self.prepare()

    def test_missing_stock_marker_refuses_second_adjustment(self):
        self.prepare()
        bearing = self.owned("product_bearing")
        marker = self.env["ir.model.data"].search([
            ("module", "=", NAMESPACE), ("name", "=", "initial_bearing_move"),
        ])
        marker.unlink()
        with self.cr.savepoint(), self.assertRaisesRegex(UserError, "inventory"):
            self.prepare()
        self.assertEqual(bearing.with_company(self.owned("company_a")).qty_available, 40)

    def test_stock_marker_cannot_be_replaced_with_a_different_adjustment(self):
        self.prepare()
        company, bearing = self.owned("company_a"), self.owned("product_bearing")
        quant = self.env["stock.quant"].with_company(company).search([
            ("product_id", "=", bearing.id), ("location_id.usage", "=", "internal"),
        ])
        quant.with_context(inventory_mode=True).inventory_quantity = 30
        quant.action_apply_inventory()
        move = self.env["stock.move"].search([
            ("product_id", "=", bearing.id), ("is_inventory", "=", True),
            ("id", "!=", self.owned("initial_bearing_move").id),
        ], limit=1)
        self.assertEqual(move.quantity, 10)
        self.env["ir.model.data"].search([
            ("module", "=", NAMESPACE), ("name", "=", "initial_bearing_move"),
        ]).res_id = move.id
        with self.cr.savepoint(), self.assertRaisesRegex(UserError, "inventory"):
            self.prepare()
        self.assertEqual(bearing.with_company(company).qty_available, 30)

    def test_stock_marker_requires_original_inventory_location(self):
        self.prepare()
        company, bearing = self.owned("company_a"), self.owned("product_bearing")
        location = self.env["stock.location"].create({
            "name": "Unrelated acceptance inventory", "usage": "internal", "company_id": company.id,
        })
        quant = self.env["stock.quant"].with_company(company).with_context(inventory_mode=True).create({
            "product_id": bearing.id, "location_id": location.id, "inventory_quantity": 40,
        })
        quant.action_apply_inventory()
        move = self.env["stock.move"].search([
            ("product_id", "=", bearing.id), ("is_inventory", "=", True),
            ("location_dest_id", "=", location.id),
        ], limit=1)
        self.assertEqual(move.quantity, 40)
        self.env["ir.model.data"].search([
            ("module", "=", NAMESPACE), ("name", "=", "initial_bearing_move"),
        ]).res_id = move.id
        with self.cr.savepoint(), self.assertRaisesRegex(UserError, "inventory"):
            self.prepare()

    def test_standard_company_isolation_for_fixture_salespeople(self):
        self.prepare()
        company, other = self.owned("company_a"), self.owned("company_b")
        customer = self.env["res.partner"].create({"name": "B isolated customer", "company_id": other.id})
        product = self.env["product.product"].with_company(other).create({
            "name": "B isolated service", "type": "service", "company_id": other.id,
        })
        order = self.env["sale.order"].with_company(other).create({
            "partner_id": customer.id, "user_id": self.owned("user_other_sales").id,
            "order_line": [Command.create({"product_id": product.id, "price_unit": 1})],
        })
        for key in ("sales", "manager"):
            user = self.owned(f"user_{key}")
            scoped = order.with_user(user).with_context(allowed_company_ids=company.ids)
            with self.assertRaises(AccessError):
                scoped.read(["partner_id"])
            self.assertFalse(self.env["sale.order"].with_user(user).search([("id", "=", order.id)]))

    def test_fixture_requires_explicit_opt_in(self):
        with self.assertRaisesRegex(UserError, "opt.in"):
            self.prepare(allow_local=False)
