from lxml import etree
from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, new_test_user, tagged


@tagged("post_install", "-at_install", "portfolio_procurement")
class TestProcurementProvenance(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env["res.company"].create({
            "name": "Procurement controlled A", "portfolio_masterdata_enabled": True,
        })
        cls.other_company = cls.env["res.company"].create({"name": "Procurement ordinary B"})
        cls.companies = cls.company | cls.other_company
        cls.sales = new_test_user(
            cls.env, login="provenance_sales", groups="sales_team.group_sale_salesman",
            company_id=cls.company.id, company_ids=[Command.set(cls.company.ids)],
        )
        cls.manager = new_test_user(
            cls.env, login="provenance_manager",
            groups="sales_team.group_sale_manager,purchase.group_purchase_manager",
            company_id=cls.company.id, company_ids=[Command.set(cls.companies.ids)],
        )
        cls.buyer = new_test_user(
            cls.env, login="provenance_buyer", groups="purchase.group_purchase_user",
            company_id=cls.company.id, company_ids=[Command.set(cls.company.ids)],
        )
        cls.buyer_ab = new_test_user(
            cls.env, login="provenance_buyer_ab", groups="purchase.group_purchase_user",
            company_id=cls.company.id, company_ids=[Command.set(cls.companies.ids)],
        )
        cls.creator = new_test_user(
            cls.env, login="provenance_company_creator", groups="base.group_erp_manager",
            company_id=cls.company.id, company_ids=[Command.set(cls.company.ids)],
        )
        cls.customer = cls.env["res.partner"].create({"name": "Provenance customer", "ref": "PROV-C"})
        cls.vendor = cls.env["res.partner"].create({"name": "Provenance vendor", "ref": "PROV-V"})
        cls.product = cls.env["product.product"].create({
            "name": "Provenance bearing", "default_code": "PROV-BRG", "type": "consu",
            "is_storable": True, "invoice_policy": "delivery", "list_price": 100,
            "taxes_id": [Command.clear()], "supplier_taxes_id": [Command.clear()],
        })
        cls.env["product.supplierinfo"].create({
            "product_tmpl_id": cls.product.product_tmpl_id.id, "partner_id": cls.vendor.id,
            "company_id": cls.company.id, "price": 60, "currency_id": cls.company.currency_id.id,
        })
        cls.source = cls.env["sale.order"].with_user(cls.sales).with_company(cls.company).create({
            "partner_id": cls.customer.id, "user_id": cls.sales.id,
            "order_line": [Command.create({
                "product_id": cls.product.id, "product_uom_qty": 2, "price_unit": 100,
                "tax_id": [Command.clear()],
            })],
        })
        cls.source.action_confirm()

    def features(self):
        self.assertIn("portfolio_fulfillment_enabled", self.company._fields)
        self.assertIn("portfolio_stock_revision", self.company._fields)
        self.assertIn("portfolio_source_sale_id", self.env["purchase.order"]._fields)
        self.assertIn("portfolio_purchase_ids", self.env["sale.order"]._fields)

    def enable(self):
        self.features()
        self.company.write({"portfolio_fulfillment_enabled": True})

    def sale(self, company=None, confirmed=True):
        company = company or self.company
        order = self.env["sale.order"].with_user(self.manager).with_company(company).create({
            "partner_id": self.customer.id, "company_id": company.id, "user_id": self.manager.id,
            "order_line": [Command.create({
                "product_id": self.product.id, "product_uom_qty": 1, "price_unit": 100,
                "tax_id": [Command.clear()],
            })],
        })
        if confirmed:
            order.action_confirm()
        return order

    def purchase(self, user=None, company=None, **values):
        company = company or self.company
        return self.env["purchase.order"].with_user(user or self.buyer).with_company(company).create({
            "partner_id": self.vendor.id, "company_id": company.id,
            "order_line": [Command.create({
                "product_id": self.product.id, "product_qty": 2, "price_unit": 60,
                "product_uom": self.product.uom_po_id.id, "date_planned": fields.Datetime.now(),
                "taxes_id": [Command.clear()],
            })], **values,
        })

    def test_policy_defaults_and_fulfillment_only_version(self):
        self.features()
        self.assertFalse(self.company.portfolio_fulfillment_enabled)
        self.assertEqual(self.company.portfolio_stock_revision, 0)
        version = self.company.portfolio_approval_policy_version
        self.enable()
        self.assertEqual(self.company.portfolio_approval_policy_version, version)
        self.company.portfolio_fulfillment_enabled = False
        self.assertEqual(self.company.portfolio_approval_policy_version, version)
        enabled = self.env["res.company"].with_context(default_portfolio_fulfillment_enabled=True).create({
            "name": "Administrator fulfillment default",
        })
        self.assertTrue(enabled.portfolio_fulfillment_enabled)

    def test_only_actual_settings_admin_can_configure_policy(self):
        self.enable()
        for user in (self.sales, self.manager, self.buyer):
            with self.subTest(user=user.login), self.assertRaises(AccessError):
                self.company.with_user(user).sudo().write({"portfolio_fulfillment_enabled": False})
        with self.assertRaises(AccessError):
            self.env["res.company"].with_user(self.creator).create({
                "name": "Forbidden explicit policy", "portfolio_fulfillment_enabled": True,
            })

    def test_non_admin_context_and_ir_defaults_cannot_set_policy(self):
        self.features()
        with self.assertRaises(AccessError):
            self.env["res.company"].with_user(self.creator).with_context(
                default_portfolio_fulfillment_enabled=True,
            ).create({"name": "Forbidden context policy"})
        self.env["ir.default"].set(
            "res.company", "portfolio_fulfillment_enabled", True, user_id=self.creator.id,
        )
        with self.assertRaises(AccessError):
            self.env["res.company"].with_user(self.creator).create({"name": "Forbidden default policy"})

    def test_revision_is_protected_from_public_values_and_defaults(self):
        self.enable()
        with self.assertRaises(AccessError):
            self.company.write({"portfolio_stock_revision": 42})
        for context, values in (
            ({}, {"portfolio_stock_revision": 42}),
            ({"default_portfolio_stock_revision": 42}, {}),
        ):
            with self.subTest(context=context), self.assertRaises(AccessError):
                self.env["res.company"].with_context(**context).create({"name": "Forged revision", **values})
        self.env["ir.default"].set("res.company", "portfolio_stock_revision", 42)
        with self.assertRaises(AccessError):
            self.env["res.company"].create({"name": "Forged default revision"})

    def test_fulfillment_lock_updates_only_enabled_technical_counter_without_company_write(self):
        self.enable()
        version = self.company.portfolio_approval_policy_version
        self.assertFalse(self.company.with_user(self.buyer).has_access("write"))
        self.companies.with_user(self.buyer_ab)._portfolio_lock_fulfillment()
        self.assertEqual(self.company.portfolio_stock_revision, 1)
        self.assertEqual(self.other_company.portfolio_stock_revision, 0)
        self.assertEqual(self.company.portfolio_approval_policy_version, version)
        self.assertTrue(self.company.portfolio_fulfillment_enabled)

    def test_fulfillment_lock_rejects_spoofed_company_even_under_sudo(self):
        self.enable()
        with self.assertRaises(AccessError):
            self.other_company.with_user(self.buyer).sudo().with_context(
                allowed_company_ids=self.companies.ids,
            )._portfolio_lock_fulfillment()
        self.assertEqual(self.other_company.portfolio_stock_revision, 0)

    def test_buyer_can_read_confirmed_sources_and_create_provenance_only_receipt(self):
        self.enable()
        self.assertTrue(self.source.with_user(self.buyer).read(["name", "order_line"]))
        self.assertTrue(self.source.order_line.with_user(self.buyer).read(["product_id", "product_uom_qty"]))
        order = self.purchase(portfolio_source_sale_id=self.source.id)
        self.assertEqual(order.portfolio_source_sale_id, self.source)
        order.button_confirm()
        self.assertEqual(order.state, "purchase")
        self.assertTrue(order.picking_ids)
        self.assertFalse(order.order_line.sale_line_id)
        self.assertFalse(order.picking_ids.move_ids.sale_line_id)
        self.assertFalse(order.picking_ids.move_ids.route_ids)
        self.assertEqual(self.source.with_user(self.manager).portfolio_purchase_ids, order)

    def test_buyer_can_web_read_actual_source_form_without_sales_audit_access(self):
        self.enable()
        self.assertFalse(self.buyer.has_group("sales_team.group_sale_salesman"))
        self.assertFalse(self.env["portfolio.sale.approval"].with_user(self.buyer).has_access("read"))
        source = self.source.with_user(self.buyer)
        view = source.get_view(view_id=self.env.ref("sale.view_order_form").id, view_type="form")
        arch = etree.fromstring(view["arch"].encode())
        specification = {
            node.get("name"): {} for node in arch.xpath(".//field[not(ancestor::field)]")
        }
        if "portfolio_approval_history_ids" in specification:
            specification["portfolio_approval_history_ids"] = {"fields": {"event": {}}}
        source.invalidate_recordset(["portfolio_approval_history_ids"])
        result = source.web_read(specification)
        self.assertEqual(result[0]["id"], source.id)
        self.assertNotIn("portfolio_approval_history_ids", specification)
        self.assertFalse(arch.xpath(".//button[@name='action_portfolio_submit']"))

    def test_buyer_receives_no_business_mutation_groups_or_sales_permissions(self):
        self.enable()
        draft = self.sale(confirmed=False)
        for group in (
            "sales_team.group_sale_salesman", "stock.group_stock_user", "account.group_account_invoice",
        ):
            self.assertFalse(self.buyer.has_group(group))
        for action in (
            lambda: self.source.with_user(self.buyer).write({"note": "Unauthorized edit"}),
            lambda: draft.with_user(self.buyer).read(["name"]),
            lambda: draft.with_user(self.buyer).action_confirm(),
            lambda: draft.with_user(self.buyer).action_portfolio_approve(),
            lambda: self.env["sale.order"].with_user(self.buyer).create({"partner_id": self.customer.id}),
            lambda: self.source.order_line.with_user(self.buyer).write({"product_uom_qty": 99}),
        ):
            with self.assertRaises(AccessError):
                action()

    def test_buyer_read_rule_does_not_broaden_combined_own_sales_write_scope(self):
        self.enable()
        self.buyer.write({"groups_id": [Command.link(self.env.ref("sales_team.group_sale_salesman").id)]})
        self.assertTrue(self.source.with_user(self.buyer).read(["name"]))
        with self.assertRaises(AccessError):
            self.source.with_user(self.buyer).write({"note": "Other salesperson's order"})
        with self.assertRaises(AccessError):
            self.source.order_line.with_user(self.buyer).write({"name": "Other salesperson's line"})

    def test_cross_company_source_is_rejected_for_ab_authorized_buyer(self):
        self.enable()
        self.other_company.portfolio_fulfillment_enabled = True
        other = self.sale(company=self.other_company)
        self.assertTrue(other.with_user(self.buyer_ab).read(["name"]))
        with self.assertRaises(UserError), self.cr.savepoint():
            self.purchase(user=self.buyer_ab, portfolio_source_sale_id=other.id)

    def test_unreadable_source_is_rejected_without_sudo_bridge(self):
        self.enable()
        other = self.sale(company=self.other_company)
        with self.assertRaises(AccessError), self.cr.savepoint():
            self.purchase(portfolio_source_sale_id=other.id)

    def test_draft_source_is_rejected_on_create_and_change(self):
        self.enable()
        draft = self.sale(confirmed=False)
        with self.assertRaises(UserError), self.cr.savepoint():
            self.purchase(user=self.manager, portfolio_source_sale_id=draft.id)
        order = self.purchase(user=self.manager, portfolio_source_sale_id=self.source.id)
        with self.assertRaises(UserError), self.cr.savepoint():
            order.write({"portfolio_source_sale_id": draft.id})

    def test_source_defaults_are_validated_and_preserved(self):
        self.enable()
        order = self.env["purchase.order"].with_user(self.buyer).with_company(self.company).with_context(
            default_portfolio_source_sale_id=self.source.id,
        ).create({"partner_id": self.vendor.id})
        self.assertEqual(order.portfolio_source_sale_id, self.source)
        draft = self.sale(confirmed=False)
        self.env["ir.default"].set("purchase.order", "portfolio_source_sale_id", draft.id, user_id=self.manager.id)
        with self.assertRaises(UserError), self.cr.savepoint():
            self.purchase(user=self.manager)

    def test_source_rechecked_on_confirmation(self):
        self.enable()
        order = self.purchase(portfolio_source_sale_id=self.source.id)
        self.source.with_context(disable_cancel_warning=True).action_cancel()
        with self.assertRaises(UserError), self.cr.savepoint():
            order.button_confirm()
        self.assertEqual(order.state, "draft")
        self.assertFalse(order.picking_ids)

    def test_source_rechecked_on_manager_approval(self):
        self.enable()
        self.company.write({"po_double_validation": "two_step", "po_double_validation_amount": 1})
        order = self.purchase(portfolio_source_sale_id=self.source.id)
        order.button_confirm()
        self.assertEqual(order.state, "to approve")
        self.source.with_context(disable_cancel_warning=True).action_cancel()
        with self.assertRaises(UserError), self.cr.savepoint():
            order.with_user(self.manager).button_approve()
        self.assertEqual(order.state, "to approve")
        self.assertFalse(order.picking_ids)

    def test_confirmed_purchase_lock_unlock_keeps_canceled_source_history(self):
        self.enable()
        order = self.purchase(portfolio_source_sale_id=self.source.id)
        order.button_confirm()
        self.source.with_context(disable_cancel_warning=True).action_cancel()
        self.assertEqual(self.source.state, "cancel")
        self.assertTrue(self.source.with_user(self.buyer).read(["name"]))
        order.button_done()
        self.assertEqual(order.state, "done")
        self.assertEqual(order.portfolio_source_sale_id, self.source)
        order.button_unlock()
        self.assertEqual(order.state, "purchase")
        self.assertEqual(order.portfolio_source_sale_id, self.source)

    def test_disabled_policies_preserve_confirmed_purchase_lock_unlock_history(self):
        self.enable()
        order = self.purchase(portfolio_source_sale_id=self.source.id)
        order.button_confirm()
        self.source.with_context(disable_cancel_warning=True).action_cancel()
        self.company.write({
            "portfolio_fulfillment_enabled": False, "portfolio_masterdata_enabled": False,
            "portfolio_approval_enabled": False,
        })
        self.assertTrue(self.source.with_user(self.buyer).read(["name"]))
        order.button_done()
        self.assertEqual(order.state, "done")
        self.assertEqual(order.portfolio_source_sale_id, self.source)
        order.button_unlock()
        self.assertEqual(order.state, "purchase")
        self.assertEqual(order.portfolio_source_sale_id, self.source)

    def test_submitted_and_confirmed_source_cannot_be_changed_or_cleared(self):
        self.enable()
        replacement = self.sale()
        for double in ("two_step", "one_step"):
            self.company.write({"po_double_validation": double, "po_double_validation_amount": 1})
            order = self.purchase(portfolio_source_sale_id=self.source.id)
            order.button_confirm()
            self.assertIn(order.state, ("to approve", "purchase"))
            for source in (False, replacement.id):
                with self.subTest(state=order.state, source=source), self.assertRaises(UserError), self.cr.savepoint():
                    order.write({"portfolio_source_sale_id": source})
            order.write({"portfolio_source_sale_id": self.source.id})

    def test_inverse_commands_cannot_reassign_or_clear_confirmed_provenance(self):
        self.enable()
        order = self.purchase(portfolio_source_sale_id=self.source.id)
        order.button_confirm()
        replacement = self.sale()
        for source, commands in (
            (replacement, [Command.link(order.id)]),
            (self.source.with_user(self.manager), [Command.clear()]),
            (replacement, [Command.update(order.id, {"portfolio_source_sale_id": replacement.id})]),
        ):
            with self.assertRaises(UserError), self.cr.savepoint():
                source.write({"portfolio_purchase_ids": commands})
        self.assertEqual(order.portfolio_source_sale_id, self.source)

    def test_buyer_history_survives_cancel_and_policy_disable_with_company_boundary(self):
        self.enable()
        unrelated = self.sale()
        other = self.sale(company=self.other_company)
        order = self.purchase(portfolio_source_sale_id=self.source.id)
        self.source.with_context(disable_cancel_warning=True).action_cancel()
        self.company.portfolio_fulfillment_enabled = False
        self.assertTrue(self.source.with_user(self.buyer).read(["name"]))
        self.assertTrue(self.source.order_line.with_user(self.buyer).read(["product_id"]))
        self.assertEqual(order.portfolio_source_sale_id, self.source)
        for record, user in ((unrelated, self.buyer), (other, self.buyer_ab), (other, self.buyer)):
            with self.assertRaises(AccessError):
                record.with_user(user).read(["name"])

    def test_fulfillment_controls_purchase_promotions_independently_of_m1_flags(self):
        self.enable()
        self.company.write({"portfolio_masterdata_enabled": False, "portfolio_approval_enabled": False})
        order = self.purchase()
        for state in ("to approve", "purchase", "done"):
            with self.assertRaises(UserError), self.cr.savepoint():
                self.purchase(state=state)
            with self.assertRaises(UserError), self.cr.savepoint():
                order.with_context(portfolio_fulfillment_enabled=False).write({"state": state})
        order.button_confirm()
        self.assertEqual(order.state, "purchase")

    def test_fulfillment_company_transfer_guard_is_independent_of_m1_flags(self):
        self.enable()
        self.company.write({"portfolio_masterdata_enabled": False, "portfolio_approval_enabled": False})
        service = self.env["product.product"].create({"name": "Provenance transfer service", "type": "service"})
        warehouse_a = self.env["stock.warehouse"].search([("company_id", "=", self.company.id)], limit=1)
        warehouse_b = self.env["stock.warehouse"].search([("company_id", "=", self.other_company.id)], limit=1)
        sale = self.sale(company=self.other_company, confirmed=False)
        sale.order_line.write({"product_id": service.id})
        sale.action_confirm()
        purchase = self.purchase(user=self.buyer_ab, company=self.other_company,
                                 order_line=[Command.create({
                                     "product_id": service.id, "product_qty": 1, "price_unit": 1,
                                     "product_uom": service.uom_po_id.id, "date_planned": fields.Datetime.now(),
                                 })])
        purchase.button_confirm()
        for order, values in (
            (sale, {"company_id": self.company.id, "warehouse_id": warehouse_a.id}),
            (purchase, {"company_id": self.company.id, "picking_type_id": warehouse_a.in_type_id.id}),
        ):
            with self.assertRaises(UserError), self.cr.savepoint():
                order.with_company(self.company).write(values)
            self.assertEqual(order.company_id, self.other_company)
        self.assertEqual(sale.warehouse_id, warehouse_b)

    def test_canceled_source_company_transfer_cannot_break_linked_purchase_history(self):
        self.enable()
        self.company.write({"portfolio_masterdata_enabled": False, "portfolio_approval_enabled": False})
        actor = new_test_user(
            self.env, login="provenance_source_transfer_sales", groups="sales_team.group_sale_salesman",
            company_id=self.company.id, company_ids=[Command.set(self.companies.ids)],
        )
        self.assertFalse(self.env["purchase.order"].with_user(actor).has_access("read"))
        service = self.env["product.product"].create({"name": "Canceled source service", "type": "service"})
        warehouse_b = self.env["stock.warehouse"].search([("company_id", "=", self.other_company.id)], limit=1)
        for policy_enabled in (True, False):
            with self.subTest(policy_enabled=policy_enabled), self.cr.savepoint():
                self.company.portfolio_fulfillment_enabled = True
                source = self.env["sale.order"].with_user(actor).with_company(self.company).create({
                    "partner_id": self.customer.id, "user_id": actor.id,
                    "order_line": [Command.create({
                        "product_id": service.id, "product_uom_qty": 1, "price_unit": 100,
                        "tax_id": [Command.clear()],
                    })],
                })
                source.action_confirm()
                order = self.purchase(portfolio_source_sale_id=source.id, order_line=[Command.create({
                    "product_id": service.id, "product_qty": 1, "price_unit": 60,
                    "product_uom": service.uom_po_id.id, "date_planned": fields.Datetime.now(),
                    "taxes_id": [Command.clear()],
                })])
                order.button_confirm()
                source.with_context(disable_cancel_warning=True).action_cancel()
                self.company.portfolio_fulfillment_enabled = policy_enabled
                self.assertEqual(source.state, "cancel")
                self.assertEqual(order.state, "purchase")
                with self.assertRaises(UserError):
                    source.with_company(self.other_company).write({
                        "company_id": self.other_company.id, "warehouse_id": warehouse_b.id,
                    })
                self.assertEqual(source.company_id, self.company)
                self.assertEqual(order.company_id, self.company)
                self.assertEqual(order.portfolio_source_sale_id, source)
                self.assertTrue(source.with_user(self.buyer).read(["name"]))

    def test_unlinked_canceled_source_keeps_native_company_transfer(self):
        self.enable()
        self.company.write({"portfolio_masterdata_enabled": False, "portfolio_approval_enabled": False})
        service = self.env["product.product"].create({"name": "Unlinked canceled service", "type": "service"})
        source = self.sale(confirmed=False)
        source.order_line.write({"product_id": service.id})
        source.action_confirm()
        source.with_context(disable_cancel_warning=True).action_cancel()
        warehouse_b = self.env["stock.warehouse"].search([("company_id", "=", self.other_company.id)], limit=1)
        source.with_company(self.other_company).write({
            "company_id": self.other_company.id, "warehouse_id": warehouse_b.id,
        })
        self.assertEqual(source.state, "cancel")
        self.assertEqual(source.company_id, self.other_company)
        self.assertEqual(source.warehouse_id, warehouse_b)

    def test_disabled_company_keeps_native_purchase_paths_and_commercial_edits(self):
        self.features()
        self.company.write({"portfolio_masterdata_enabled": False, "portfolio_approval_enabled": False})
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
        self.assertTrue(approved.picking_ids)
        self.source.write({"note": "Native confirmed commercial edit"})
        self.assertIn("Native confirmed commercial edit", self.source.note)

    def test_related_purchase_navigation_respects_acl(self):
        self.enable()
        order = self.purchase(portfolio_source_sale_id=self.source.id)
        action = self.source.with_user(self.manager).action_portfolio_purchases()
        self.assertEqual(action["res_model"], "purchase.order")
        self.assertEqual(action["domain"], [("id", "in", order.ids)])
        with self.assertRaises(AccessError):
            self.source.with_user(self.sales).action_portfolio_purchases()
