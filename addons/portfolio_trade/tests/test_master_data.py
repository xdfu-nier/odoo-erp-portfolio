from datetime import timedelta

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, new_test_user, tagged


@tagged("post_install", "-at_install", "portfolio_master_data")
class TestMasterData(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.other_company = cls.env["res.company"].create({"name": "Catalog other company"})
        cls.sales = new_test_user(
            cls.env, login="catalog_sales", groups="sales_team.group_sale_salesman",
            company_id=cls.company.id, company_ids=[Command.set(cls.company.ids)],
        )
        cls.manager = new_test_user(
            cls.env, login="catalog_manager", groups="sales_team.group_sale_manager",
            company_id=cls.company.id, company_ids=[Command.set(cls.company.ids)],
        )
        cls.buyer = new_test_user(
            cls.env, login="catalog_buyer", groups="purchase.group_purchase_user",
            company_id=cls.company.id, company_ids=[Command.set(cls.company.ids)],
        )
        cls.customer = cls.env["res.partner"].create({"name": "Catalog customer", "ref": "CAT-C001"})
        cls.vendor = cls.env["res.partner"].create({"name": "Catalog vendor", "ref": "CAT-V001"})
        cls.product = cls.env["product.product"].create({
            "name": "Catalog bearing", "default_code": "CAT-BRG", "type": "consu",
            "is_storable": True, "invoice_policy": "delivery", "list_price": 100,
            "taxes_id": [Command.clear()], "supplier_taxes_id": [Command.clear()],
        })
        cls.seller = cls.env["product.supplierinfo"].create({
            "product_tmpl_id": cls.product.product_tmpl_id.id, "partner_id": cls.vendor.id,
            "price": 60, "currency_id": cls.company.currency_id.id,
            "company_id": cls.company.id,
        })

    def enable_policy(self, approval=False):
        self.assertIn(
            "portfolio_masterdata_enabled", self.company._fields,
            "Company-level industrial master-data policy is not implemented",
        )
        self.company.write({
            "portfolio_masterdata_enabled": True, "portfolio_approval_enabled": approval,
        })

    def sale(self, **values):
        return self.env["sale.order"].with_user(self.sales).with_company(self.company).create({
            "partner_id": self.customer.id, "user_id": self.sales.id,
            "order_line": [Command.create({
                "product_id": self.product.id, "product_uom_qty": 1, "price_unit": 100,
                "tax_id": [Command.clear()],
            })], **values,
        })

    def purchase(self, **values):
        return self.env["purchase.order"].with_user(self.buyer).with_company(self.company).create({
            "partner_id": self.vendor.id,
            "order_line": [Command.create({
                "product_id": self.product.id, "product_qty": 1, "price_unit": 60,
                "product_uom": self.product.uom_po_id.id, "date_planned": fields.Datetime.now(),
                "taxes_id": [Command.clear()],
            })], **values,
        })

    def reject_both_sales_gates(self, order, reason):
        for method in ("action_portfolio_submit", "action_confirm"):
            with self.subTest(method=method), self.cr.savepoint(), self.assertRaisesRegex(UserError, reason):
                getattr(order, method)()
        self.assertEqual(order.state, "draft")

    def test_missing_customer_ref_at_both_sales_gates(self):
        self.enable_policy(approval=True)
        for value in (False, "   ", " CAT-C001 "):
            self.customer.ref = value
            self.reject_both_sales_gates(self.sale(), "customer.*(ref|code)")

    def test_missing_vendor_ref_at_purchase_gate(self):
        self.enable_policy()
        order = self.purchase()
        for value in (False, "   "):
            self.vendor.ref = value
            with self.cr.savepoint(), self.assertRaisesRegex(UserError, "vendor.*(ref|code)"):
                order.button_confirm()
        self.assertEqual(order.state, "draft")

    def test_missing_sku_at_sales_and_purchase_gates(self):
        self.enable_policy(approval=True)
        sale, purchase = self.sale(), self.purchase()
        for value in (False, "   ", " CAT-BRG "):
            self.product.default_code = value
            self.reject_both_sales_gates(sale, "[Ll]ine 1.*SKU")
            with self.cr.savepoint(), self.assertRaisesRegex(UserError, "[Ll]ine 1.*SKU"):
                purchase.button_confirm()

    def test_ambiguous_sku_in_shared_company_scope(self):
        self.enable_policy()
        duplicate = self.env["product.product"].create({
            "name": "Independent duplicate SKU", "company_id": self.company.id,
            "default_code": self.product.default_code, "type": "service",
        })
        self.assertEqual(duplicate.default_code, self.product.default_code)
        with self.assertRaisesRegex(UserError, "SKU.*ambiguous"):
            self.sale().action_confirm()

    def test_ambiguous_commercial_partner_ref(self):
        self.enable_policy()
        self.customer.copy({"name": "Duplicate customer", "company_id": self.company.id})
        with self.assertRaisesRegex(UserError, "customer.*ambiguous"):
            self.sale().action_confirm()

    def test_ambiguous_vendor_ref(self):
        self.enable_policy()
        self.vendor.copy({"name": "Duplicate vendor", "company_id": self.company.id})
        with self.assertRaisesRegex(UserError, "vendor.*ambiguous"):
            self.purchase().button_confirm()

    def test_duplicate_codes_in_other_company_do_not_conflict(self):
        self.enable_policy()
        self.customer.copy({"company_id": self.other_company.id})
        duplicate = self.env["product.product"].with_company(self.other_company).create({
            "name": "Other company duplicate SKU", "company_id": self.other_company.id,
            "default_code": self.product.default_code, "type": "service",
        })
        self.assertEqual(duplicate.default_code, self.product.default_code)
        order = self.sale()
        order.action_confirm()
        self.assertEqual(order.state, "sale")

    def test_commercial_partner_code_supports_contact(self):
        self.enable_policy()
        contact = self.env["res.partner"].create({"name": "Receiving contact", "parent_id": self.customer.id})
        order = self.sale(partner_id=contact.id)
        order.action_confirm()
        self.assertEqual(order.state, "sale")

    def test_foreign_company_customer_rejected_at_gate(self):
        self.enable_policy()
        order = self.sale()
        self.customer.company_id = self.other_company
        with self.assertRaises(UserError):
            order.action_confirm()

    def test_foreign_company_product_rejected_at_gate(self):
        self.enable_policy()
        order = self.sale()
        with self.assertRaises(UserError), self.cr.savepoint():
            self.product.company_id = self.other_company
            order.action_confirm()
        self.assertFalse(self.product.company_id)
        self.assertEqual(order.state, "draft")
        self.assertFalse(order.picking_ids)

    def test_foreign_company_vendor_rejected_at_gate(self):
        self.enable_policy()
        order = self.purchase()
        self.vendor.company_id = self.other_company
        with self.assertRaises(UserError):
            order.button_confirm()

    def test_inactive_customer_product_and_vendor_rejected(self):
        self.enable_policy()
        for record, make_order, method in (
            (self.customer, self.sale, "action_confirm"),
            (self.product, self.sale, "action_confirm"),
            (self.vendor, self.purchase, "button_confirm"),
        ):
            with self.subTest(model=record._name), self.cr.savepoint():
                order = make_order()
                record.active = False
                with self.assertRaisesRegex(UserError, "inactive"):
                    getattr(order, method)()
                record.active = True

    def test_incompatible_sales_line_uom(self):
        self.enable_policy()
        order = self.sale()
        order.order_line.product_uom = self.env.ref("uom.product_uom_kgm")
        with self.assertRaisesRegex(UserError, "[Ll]ine 1.*[Uu]nit"):
            order.action_confirm()

    def test_physical_product_requires_delivery_invoice_policy(self):
        self.enable_policy()
        self.product.invoice_policy = "order"
        with self.assertRaisesRegex(UserError, "[Ll]ine 1.*delivery"):
            self.sale().action_confirm()

    def test_physical_product_requires_current_supplier(self):
        self.enable_policy()
        today = fields.Date.today()
        for values in (
            {"date_end": today - timedelta(days=1)},
            {"date_end": False, "date_start": today + timedelta(days=1)},
            {"date_start": False, "company_id": self.other_company.id},
        ):
            self.seller.write(values)
            with self.cr.savepoint(), self.assertRaisesRegex(UserError, "supplier"):
                self.sale().action_confirm()
        self.seller.unlink()
        with self.assertRaisesRegex(UserError, "supplier"):
            self.sale().action_confirm()

    def test_supplier_requires_valid_vendor_ref(self):
        self.enable_policy()
        self.vendor.ref = False
        with self.assertRaisesRegex(UserError, "(supplier|vendor)"):
            self.sale().action_confirm()

    def test_supplier_minimum_does_not_block_small_sale(self):
        self.enable_policy()
        self.seller.min_qty = 100
        order = self.sale()
        order.action_confirm()
        self.assertEqual(order.state, "sale")

    def test_service_does_not_require_stock_or_supplier_policy(self):
        self.enable_policy()
        self.product.write({"type": "service", "is_storable": False, "invoice_policy": "order"})
        self.seller.unlink()
        order = self.sale()
        order.action_confirm()
        self.assertEqual(order.state, "sale")

    def test_valid_stock_sale_and_purchase_preserve_standard_effects(self):
        self.enable_policy()
        sale, purchase = self.sale(), self.purchase()
        sale.action_confirm()
        purchase.button_confirm()
        self.assertEqual(sale.state, "sale")
        self.assertEqual(purchase.state, "purchase")
        self.assertTrue(sale.picking_ids)
        self.assertTrue(purchase.picking_ids)

    def test_price_policy_off_does_not_disable_master_data(self):
        self.enable_policy()
        self.customer.ref = False
        with self.assertRaisesRegex(UserError, "customer"):
            self.sale().with_context(skip_master_data=True, portfolio_masterdata_enabled=False).action_confirm()

    def test_sales_and_manager_cannot_configure_policy(self):
        self.enable_policy()
        self.assertFalse(self.manager.has_group("base.group_system"))
        for user in (self.sales, self.manager):
            with self.assertRaises(AccessError):
                self.company.with_user(user).write({"portfolio_masterdata_enabled": False})

    def test_policy_defaults_cannot_enable_company_for_non_admin(self):
        self.enable_policy()
        creator = new_test_user(self.env, login="catalog_company_creator", groups="base.group_erp_manager")
        with self.assertRaises(AccessError):
            self.env["res.company"].with_user(creator).with_context(default_portfolio_masterdata_enabled=True).create({
                "name": "Unauthorized policy default",
            })

    def test_user_ir_defaults_cannot_enable_company_policies(self):
        self.enable_policy()
        creator = new_test_user(self.env, login="catalog_default_creator", groups="base.group_erp_manager")
        for name in ("portfolio_masterdata_enabled", "portfolio_approval_enabled"):
            with self.subTest(field=name), self.cr.savepoint():
                self.env["ir.default"].set("res.company", name, True, user_id=creator.id)
                with self.assertRaises(AccessError):
                    self.env["res.company"].with_user(creator).create({"name": f"Unauthorized {name}"})
                self.env["ir.default"].search([
                    ("field_id", "=", self.env["ir.model.fields"]._get("res.company", name).id),
                    ("user_id", "=", creator.id),
                ]).unlink()

    def test_admin_and_ordinary_company_creation_remain_supported(self):
        self.enable_policy()
        ordinary = self.env["res.company"].create({"name": "Ordinary standard company"})
        self.assertFalse(ordinary.portfolio_masterdata_enabled)
        self.assertFalse(ordinary.portfolio_approval_enabled)
        enabled = self.env["res.company"].with_context(default_portfolio_masterdata_enabled=True).create({
            "name": "Administrator enabled catalog company",
        })
        self.assertTrue(enabled.portfolio_masterdata_enabled)
        self.assertFalse(enabled.portfolio_approval_enabled)

    def test_disabled_company_keeps_standard_partial_catalog(self):
        self.enable_policy()
        self.company.portfolio_masterdata_enabled = False
        self.customer.ref = False
        self.product.default_code = False
        self.seller.unlink()
        order = self.sale()
        order.action_confirm()
        self.assertEqual(order.state, "sale")

    def test_partial_draft_does_not_require_global_partner_or_sku(self):
        self.enable_policy()
        prospect = self.env["res.partner"].create({"name": "Unqualified prospect"})
        product = self.env["product.product"].create({"name": "Unclassified service", "type": "service"})
        order = self.sale(partner_id=prospect.id, order_line=[Command.create({
            "product_id": product.id, "product_uom_qty": 1,
        })])
        self.assertEqual(order.state, "draft")
        with self.assertRaises(UserError):
            order.action_confirm()

    def test_public_purchase_approve_revalidates_catalog(self):
        self.enable_policy()
        order = self.purchase()
        self.vendor.ref = False
        with self.assertRaisesRegex(UserError, "vendor"), self.cr.savepoint():
            order.button_approve()
        self.assertEqual(order.state, "draft")
        self.assertFalse(order.picking_ids)

    def test_purchase_raw_state_promotions_require_standard_actions(self):
        self.enable_policy()
        order = self.purchase()
        for state in ("to approve", "purchase", "done"):
            with self.subTest(state=state), self.assertRaises(UserError), self.cr.savepoint():
                order.with_context(skip_master_data=True, portfolio_purchase_state_capability=True).write({"state": state})
        self.assertEqual(order.state, "draft")
        self.assertFalse(order.picking_ids)

    def test_purchase_state_promotion_cannot_mix_invalid_new_commercial_values(self):
        self.enable_policy()
        order = self.purchase()
        incomplete = self.env["res.partner"].create({"name": "Incomplete replacement vendor"})
        with self.assertRaises(UserError), self.cr.savepoint():
            order.write({"state": "purchase", "partner_id": incomplete.id})
        self.assertEqual(order.partner_id, self.vendor)
        self.assertEqual(order.state, "draft")
        self.assertFalse(order.picking_ids)

    def test_purchase_create_cannot_supply_confirmed_or_submitted_state(self):
        self.enable_policy()
        before = self.env["purchase.order"].search_count([])
        for state in ("to approve", "purchase", "done"):
            with self.subTest(state=state), self.assertRaises(UserError), self.cr.savepoint():
                self.purchase(state=state)
        self.assertEqual(self.env["purchase.order"].search_count([]), before)

    def test_purchase_context_default_cannot_supply_confirmed_state(self):
        self.enable_policy()
        with self.assertRaises(UserError), self.cr.savepoint():
            self.env["purchase.order"].with_user(self.buyer).with_company(self.company).with_context(
                default_state="purchase",
            ).create({"partner_id": self.vendor.id})

    def test_purchase_user_default_cannot_supply_confirmed_state(self):
        self.enable_policy()
        self.env["ir.default"].set("purchase.order", "state", "purchase", user_id=self.buyer.id)
        with self.assertRaises(UserError), self.cr.savepoint():
            self.purchase()

    def test_two_step_purchase_manager_approval_revalidates_changed_catalog(self):
        self.enable_policy()
        self.company.write({"po_double_validation": "two_step", "po_double_validation_amount": 1})
        manager = new_test_user(
            self.env, login="catalog_purchase_manager", groups="purchase.group_purchase_manager",
            company_id=self.company.id, company_ids=[Command.set(self.company.ids)],
        )
        order = self.purchase()
        order.button_confirm()
        self.assertEqual(order.state, "to approve")
        self.assertFalse(order.picking_ids)
        self.vendor.ref = False
        with self.assertRaisesRegex(UserError, "vendor"), self.cr.savepoint():
            order.with_user(manager).button_approve()
        self.assertEqual(order.state, "to approve")
        self.assertFalse(order.picking_ids)
        self.vendor.ref = "CAT-V001"
        order.with_user(manager).button_approve()
        self.assertEqual(order.state, "purchase")
        self.assertTrue(order.picking_ids)

    def test_purchase_lock_unlock_and_done_preserve_standard_confirmed_flow(self):
        self.enable_policy()
        self.company.po_lock = "lock"
        order = self.purchase()
        order.button_confirm()
        self.assertEqual(order.state, "done")
        self.assertTrue(order.picking_ids)
        order.button_unlock()
        self.assertEqual(order.state, "purchase")
        order.button_done()
        self.assertEqual(order.state, "done")
        self.vendor.ref = False
        with self.assertRaisesRegex(UserError, "vendor"), self.cr.savepoint():
            order.button_unlock()
        self.assertEqual(order.state, "done")

    def test_purchase_unlock_and_done_cannot_confirm_a_draft(self):
        self.enable_policy()
        order = self.purchase()
        for method in ("button_unlock", "button_done"):
            with self.subTest(method=method), self.assertRaises(UserError), self.cr.savepoint():
                getattr(order, method)()
        self.assertEqual(order.state, "draft")
        self.assertFalse(order.picking_ids)

    def test_disabled_purchase_policy_preserves_native_state_and_approve_paths(self):
        self.enable_policy()
        self.company.portfolio_masterdata_enabled = False
        self.vendor.ref = False
        created = self.purchase(state="purchase")
        self.assertEqual(created.state, "purchase")
        order = self.purchase()
        order.write({"state": "purchase"})
        order.button_done()
        order.button_unlock()
        self.assertEqual(order.state, "purchase")
        approved = self.purchase()
        approved.button_approve()
        self.assertEqual(approved.state, "purchase")
        self.assertTrue(approved.picking_ids)

    def test_purchase_company_transfer_cannot_import_unvalidated_confirmed_state(self):
        self.enable_policy()
        self.assertFalse(self.other_company.portfolio_masterdata_enabled)
        buyer = new_test_user(
            self.env, login="catalog_multicompany_buyer", groups="purchase.group_purchase_user",
            company_id=self.other_company.id,
            company_ids=[Command.set((self.company | self.other_company).ids)],
        )
        vendor = self.env["res.partner"].create({"name": "Shared incomplete transfer vendor", "ref": False})
        service = self.env["product.product"].create({
            "name": "Shared incomplete transfer service", "type": "service", "default_code": False,
        })
        warehouse_a = self.env["stock.warehouse"].search([("company_id", "=", self.company.id)], limit=1)
        warehouse_b = self.env["stock.warehouse"].search([("company_id", "=", self.other_company.id)], limit=1)
        self.assertTrue(warehouse_a.in_type_id)
        self.assertTrue(warehouse_b.in_type_id)
        order = self.env["purchase.order"].with_user(buyer).with_context(
            allowed_company_ids=[self.other_company.id, self.company.id],
        ).create({
            "partner_id": vendor.id, "company_id": self.other_company.id,
            "picking_type_id": warehouse_b.in_type_id.id, "state": "purchase",
            "order_line": [Command.create({
                "product_id": service.id, "product_qty": 1, "price_unit": 1,
                "product_uom": service.uom_po_id.id, "date_planned": fields.Datetime.now(),
                "taxes_id": [Command.clear()],
            })],
        })
        self.assertEqual(order.state, "purchase")
        self.assertEqual(order.company_id, self.other_company)
        self.assertFalse(order.picking_ids)
        move_count = self.env["stock.move"].search_count([("product_id", "=", service.id)])
        with self.assertRaises(UserError), self.cr.savepoint():
            order.with_company(self.company).write({
                "company_id": self.company.id, "picking_type_id": warehouse_a.in_type_id.id,
            })
        self.assertEqual(order.company_id, self.other_company)
        self.assertEqual(order.picking_type_id, warehouse_b.in_type_id)
        self.assertEqual(order.state, "purchase")
        self.assertFalse(order.picking_ids)
        self.assertEqual(self.env["stock.move"].search_count([("product_id", "=", service.id)]), move_count)

    def test_sale_company_transfer_cannot_import_unvalidated_confirmed_state(self):
        self.enable_policy(approval=False)
        self.assertFalse(self.company.portfolio_approval_enabled)
        self.assertFalse(self.other_company.portfolio_approval_enabled)
        self.assertFalse(self.other_company.portfolio_masterdata_enabled)
        salesperson = new_test_user(
            self.env, login="catalog_multicompany_sales", groups="sales_team.group_sale_salesman",
            company_id=self.other_company.id,
            company_ids=[Command.set((self.company | self.other_company).ids)],
        )
        customer = self.env["res.partner"].create({"name": "Shared incomplete transfer customer", "ref": False})
        service = self.env["product.product"].create({
            "name": "Shared incomplete sales transfer service", "type": "service", "default_code": False,
        })
        warehouse_a = self.env["stock.warehouse"].search([("company_id", "=", self.company.id)], limit=1)
        warehouse_b = self.env["stock.warehouse"].search([("company_id", "=", self.other_company.id)], limit=1)
        self.assertTrue(warehouse_a)
        self.assertTrue(warehouse_b)
        order = self.env["sale.order"].with_user(salesperson).with_context(
            allowed_company_ids=[self.other_company.id, self.company.id],
        ).create({
            "partner_id": customer.id, "user_id": salesperson.id,
            "company_id": self.other_company.id, "warehouse_id": warehouse_b.id,
            "order_line": [Command.create({
                "product_id": service.id, "product_uom_qty": 1, "price_unit": 1,
                "tax_id": [Command.clear()],
            })],
        })
        order.action_confirm()
        self.assertEqual(order.state, "sale")
        self.assertEqual(order.company_id, self.other_company)
        self.assertFalse(order.picking_ids)
        revision = order.portfolio_revision
        with self.assertRaises(UserError), self.cr.savepoint():
            order.with_company(self.company).write({
                "company_id": self.company.id, "warehouse_id": warehouse_a.id,
            })
        self.assertEqual(order.company_id, self.other_company)
        self.assertEqual(order.warehouse_id, warehouse_b)
        self.assertEqual(order.state, "sale")
        self.assertEqual(order.portfolio_revision, revision)
        self.assertFalse(order.picking_ids)

    def test_draft_company_changes_remain_supported_with_master_data_policy(self):
        self.enable_policy(approval=False)
        for user in (self.sales, self.buyer):
            user.write({"company_ids": [Command.link(self.other_company.id)]})
        scope = {"allowed_company_ids": (self.company | self.other_company).ids}
        sale, purchase = self.sale().with_context(**scope), self.purchase().with_context(**scope)
        for company in (self.other_company, self.company):
            warehouse = self.env["stock.warehouse"].search([("company_id", "=", company.id)], limit=1)
            sale.with_company(company).write({"company_id": company.id, "warehouse_id": warehouse.id})
            purchase.with_company(company).write({"company_id": company.id, "picking_type_id": warehouse.in_type_id.id})
            self.assertEqual(sale.company_id, company)
            self.assertEqual(purchase.company_id, company)
            self.assertEqual(sale.state, "draft")
            self.assertEqual(purchase.state, "draft")
            self.assertFalse(sale.picking_ids | purchase.picking_ids)

    def test_disabled_confirmed_company_changes_preserve_native_behavior(self):
        self.enable_policy(approval=False)
        self.company.portfolio_masterdata_enabled = False
        self.assertFalse(self.other_company.portfolio_masterdata_enabled)
        for user in (self.sales, self.buyer):
            user.write({"company_ids": [Command.link(self.other_company.id)]})
        service = self.env["product.product"].create({
            "name": "Native compatible company transfer service", "type": "service", "default_code": "COMPAT-XFER",
        })
        sale = self.sale(order_line=[Command.create({
            "product_id": service.id, "product_uom_qty": 1, "price_unit": 1, "tax_id": [Command.clear()],
        })])
        purchase = self.purchase(order_line=[Command.create({
            "product_id": service.id, "product_qty": 1, "price_unit": 1,
            "product_uom": service.uom_po_id.id, "date_planned": fields.Datetime.now(),
            "taxes_id": [Command.clear()],
        })])
        scope = {"allowed_company_ids": (self.company | self.other_company).ids}
        sale, purchase = sale.with_context(**scope), purchase.with_context(**scope)
        sale.action_confirm()
        purchase.button_confirm()
        self.assertEqual(sale.state, "sale")
        self.assertEqual(purchase.state, "purchase")
        warehouse = self.env["stock.warehouse"].search([("company_id", "=", self.other_company.id)], limit=1)
        sale.with_company(self.other_company).write({
            "company_id": self.other_company.id, "warehouse_id": warehouse.id,
        })
        purchase.with_company(self.other_company).write({
            "company_id": self.other_company.id, "picking_type_id": warehouse.in_type_id.id,
        })
        self.assertEqual(sale.company_id, self.other_company)
        self.assertEqual(purchase.company_id, self.other_company)
        self.assertEqual(sale.state, "sale")
        self.assertEqual(purchase.state, "purchase")
        self.assertFalse(sale.picking_ids | purchase.picking_ids)
