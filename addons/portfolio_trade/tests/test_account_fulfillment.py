"""Financial mutation boundaries exercised through native account ORM/actions."""
import unittest

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from .test_fulfillment_ledger import FulfillmentLedgerCommon


@tagged("post_install", "-at_install", "portfolio_account_controls")
class TestAccountFulfillment(FulfillmentLedgerCommon):
    def invoice_values(self, quantity, **values):
        return {
            "move_type": "out_invoice", "company_id": self.company.id,
            "partner_id": self.customer.id, "invoice_date": fields.Date.today(),
            "journal_id": self.company_data["default_journal_sale"].id,
            "invoice_line_ids": [Command.create({
                "product_id": self.product.id, "product_uom_id": self.product.uom_id.id,
                "account_id": self.company_data["default_account_revenue"].id,
                "quantity": quantity, "price_unit": 100, "discount": 15,
                "tax_ids": [Command.clear()], "sale_line_ids": [Command.set(self.line.ids)],
            })], **values,
        }

    def accounting_snapshot(self):
        self.env.flush_all()
        self.env.invalidate_all()
        moves = self.env["account.move"].search([("company_id", "=", self.company.id)])
        lines = moves.line_ids
        return (
            tuple((move.id, move.state, tuple(move.line_ids.ids)) for move in moves),
            tuple((line.id, line.move_id.id, line.quantity, line.balance, tuple(line.sale_line_ids.ids)) for line in lines),
            tuple(self.line.invoice_lines.ids),
        )

    def test_invoice_before_physical_delivery_is_rejected(self):
        pending = self.make_sale(quantity=5).order_line.with_user(self.billing)
        with self.assertRaises(UserError):
            self.make_invoice(1, line=pending)

    def test_second_draft_cannot_use_quantity_reserved_by_first(self):
        with self.assertRaises(UserError):
            self.make_invoice(1)

    def test_quantity_increase_cannot_exceed_actual_delivery(self):
        with self.assertRaises(UserError):
            self.invoice.invoice_line_ids.write({"quantity": 71})

    def test_cancel_releases_quantity_and_reset_must_reserve_again(self):
        self.invoice.button_cancel()
        replacement = self.make_invoice(70)
        self.assertEqual(replacement.amount_untaxed, 5950)
        with self.assertRaises(UserError):
            self.invoice.button_draft()
        self.assertEqual(self.invoice.state, "cancel")

    def test_ordinary_billing_can_edit_semantic_draft_price_and_post(self):
        self.assertFalse(self.billing.has_group("sales_team.group_sale_salesman"))
        self.assertFalse(self.billing.has_group("stock.group_stock_user"))
        self.invoice.invoice_line_ids.write({"price_unit": 90, "discount": 10})
        self.invoice.action_post()
        self.assertEqual(self.invoice.state, "posted")
        self.assertEqual(self.invoice.amount_untaxed, 5670)
        self.assertEqual(sum(self.invoice.line_ids.mapped("balance")), 0)

    def test_native_sales_sudo_bridge_requires_original_billing_actor(self):
        self.invoice.button_cancel()
        for actor in (self.sales, self.manager, self.buyer, self.warehouse, self.settings_only):
            with self.subTest(actor=actor.login), self.env.cr.savepoint():
                self.assertEqual(self.line.qty_to_invoice, 70)
                with self.assertRaises(AccessError):
                    self.source.with_user(actor).sudo()._create_invoices()

    def test_direct_account_creation_requires_billing_even_under_sudo(self):
        self.invoice.button_cancel()
        for actor in (self.sales, self.buyer, self.warehouse, self.settings_only):
            with self.subTest(actor=actor.login), self.assertRaises(AccessError):
                self.env["account.move"].with_user(actor).sudo().create(self.invoice_values(1))

    def test_actual_account_company_membership_cannot_be_context_forged(self):
        self.invoice.button_cancel()
        model = self.env["account.move"].with_user(self.billing_b).sudo().with_context(
            allowed_company_ids=self.companies.ids,
        )
        with self.assertRaises(AccessError):
            model.create(self.invoice_values(1))

    def test_inactive_source_company_cannot_be_used_under_sudo(self):
        self.invoice.button_cancel()
        model = self.env["account.move"].with_user(self.billing).sudo().with_context(
            allowed_company_ids=self.other_company.ids,
        )
        with self.assertRaises(AccessError):
            model.create(self.invoice_values(1))

    def test_source_default_on_direct_aml_cannot_hide_billing_requirement(self):
        values = self.invoice_values(0)["invoice_line_ids"][0][2]
        values.pop("sale_line_ids")
        values["move_id"] = self.invoice.id
        with self.assertRaises(AccessError):
            self.env["account.move.line"].with_user(self.warehouse).sudo().with_context(
                default_sale_line_ids=[Command.set(self.line.ids)],
            ).create(values)

    def test_nested_source_command_cannot_change_to_undelivered_source(self):
        pending = self.make_sale(quantity=5).order_line
        with self.assertRaises(UserError):
            self.invoice.write({"invoice_line_ids": [Command.update(
                self.invoice.invoice_line_ids.id, {"sale_line_ids": [Command.set(pending.ids)]},
            )]})

    def test_direct_source_change_cannot_use_undelivered_source(self):
        pending = self.make_sale(quantity=5).order_line
        with self.assertRaises(UserError):
            self.invoice.invoice_line_ids.write({"sale_line_ids": [Command.set(pending.ids)]})

    def test_ambiguous_source_relation_is_rejected(self):
        pending = self.make_sale(quantity=5).order_line
        with self.assertRaises(UserError):
            self.invoice.invoice_line_ids.write({"sale_line_ids": [Command.link(pending.id)]})

    def test_product_and_source_cannot_disagree(self):
        other = self.env["product.product"].create({"name": "Other controlled invoice item", "type": "consu"})
        with self.assertRaises(UserError):
            self.invoice.invoice_line_ids.write({"product_id": other.id, "tax_ids": [Command.clear()]})

    def test_compatible_uom_quantity_is_measured_in_source_units(self):
        self.invoice.invoice_line_ids.write({"quantity": 5, "product_uom_id": self.uom_dozen.id})
        self.assertEqual(self.api("financial_facts")(self.line)[self.line.id]["invoice_reserved"], 60)
        with self.assertRaises(UserError):
            self.invoice.invoice_line_ids.write({"quantity": 6})

    def test_inverse_sale_invoice_relation_cannot_detach_a_claim(self):
        with self.assertRaises(UserError):
            self.line.with_user(self.sales).sudo().write({"invoice_lines": [Command.clear()]})

    def test_public_sales_invoice_counters_are_not_inputs(self):
        for values in ({"qty_invoiced": -999}, {"qty_to_invoice": 999}, {"invoice_status": "to invoice"}):
            with self.subTest(values=values), self.assertRaises(UserError):
                self.line.with_user(self.sales).sudo().write(values)

    def test_sales_counter_context_defaults_are_rejected(self):
        order = self.make_sale(quantity=1, confirm=False)
        for field, value in (("qty_invoiced", -999), ("qty_to_invoice", 999), ("invoice_status", "to invoice")):
            with self.subTest(field=field), self.assertRaises(UserError):
                self.env["sale.order.line"].with_user(self.sales).with_context(**{f"default_{field}": value}).create({
                    "order_id": order.id, "product_id": self.product.id, "product_uom_qty": 1,
                    "price_unit": 100, "tax_id": [Command.clear()],
                })

    def test_public_posted_state_write_cannot_replace_action_post(self):
        with self.assertRaises(UserError):
            self.invoice.with_context(skip_readonly_check=True, check_move_validity=False).write({"state": "posted"})

    def test_context_default_cannot_create_posted_invoice(self):
        self.invoice.button_cancel()
        with self.assertRaises(UserError):
            self.env["account.move"].with_user(self.billing).with_context(default_state="posted").create(self.invoice_values(1))

    def test_ir_default_cannot_create_posted_invoice(self):
        self.invoice.button_cancel()
        self.env["ir.default"].set("account.move", "state", "posted", user_id=self.billing.id, company_id=self.company.id)
        with self.assertRaises(UserError):
            self.env["account.move"].with_user(self.billing).with_company(self.company).create(self.invoice_values(1))

    def test_posted_facts_cannot_change_through_public_skip_context(self):
        self.invoice.action_post()
        for values in ({"quantity": 69}, {"price_unit": 1}, {"sale_line_ids": [Command.clear()]}):
            with self.subTest(values=values), self.assertRaises(UserError):
                self.invoice.invoice_line_ids.with_context(
                    skip_readonly_check=True, check_move_validity=False, skip_invoice_line_sync=True,
                ).write(values)

    def test_public_raw_monetary_line_writes_are_rejected(self):
        payloads = {
            "balance": -100, "debit": 0, "credit": 100, "amount_currency": -100,
            "price_subtotal": 1, "price_total": 1,
        }
        for field, value in payloads.items():
            with self.subTest(field=field), self.assertRaises(UserError):
                self.invoice.invoice_line_ids.with_context(
                    check_move_validity=False, skip_readonly_check=True, skip_invoice_line_sync=True,
                ).write({field: value})

    def test_nested_raw_monetary_line_command_is_rejected(self):
        with self.assertRaises(UserError):
            self.invoice.with_context(check_move_validity=False).write({"invoice_line_ids": [Command.update(
                self.invoice.invoice_line_ids.id, {"balance": -1, "amount_currency": -1},
            )]})

    def test_raw_monetary_context_defaults_are_rejected_on_product_line(self):
        self.invoice.button_cancel()
        for field in ("balance", "amount_currency", "price_subtotal"):
            with self.subTest(field=field), self.assertRaises(UserError):
                self.env["account.move"].with_user(self.billing).with_context(
                    **{f"default_{field}": 100}, check_move_validity=False,
                ).create(self.invoice_values(0))

    def test_whole_multi_invoice_create_rolls_back_after_late_quota_failure(self):
        self.invoice.button_cancel()
        with self.env.cr.savepoint():
            before = self.accounting_snapshot()
            with unittest.TestCase.assertRaises(self, UserError):
                self.env["account.move"].with_user(self.billing).with_company(self.company).create([
                    self.invoice_values(70), self.invoice_values(1),
                ])
            self.assertEqual(self.accounting_snapshot(), before)

    def test_whole_multi_line_write_rolls_back_after_late_quota_failure(self):
        self.invoice.button_cancel()
        first = self.make_invoice(30)
        second = self.make_invoice(40)
        with self.env.cr.savepoint():
            before = self.accounting_snapshot()
            with unittest.TestCase.assertRaises(self, UserError):
                (first | second).invoice_line_ids.write({"quantity": 36})
            self.assertEqual(self.accounting_snapshot(), before)

    def test_disabled_company_native_invoice_has_no_physical_budget(self):
        other = self.make_sale(quantity=2, company=self.other_company).order_line
        invoice = self.make_invoice(2, line=other, company=self.other_company)
        invoice.action_post()
        self.assertEqual(invoice.state, "posted")

    def test_service_invoice_has_no_physical_quota_but_requires_billing(self):
        service = self.env["product.product"].create({
            "name": "Controlled service", "default_code": "CONTROL-SVC", "type": "service", "invoice_policy": "order",
            "taxes_id": [Command.clear()],
            "property_account_income_id": self.company_data["default_account_revenue"].id,
        })
        order = self.make_sale(quantity=3, product=service)
        invoice = order.with_user(self.billing)._create_invoices().with_user(self.billing)
        invoice.action_post()
        self.assertEqual(invoice.amount_untaxed, 255)
        with self.assertRaises(AccessError):
            invoice.with_user(self.sales).sudo().button_draft()

    def empty_invoice(self):
        return self.env["account.move"].with_user(self.billing).with_company(self.company).create(
            self.invoice_values(0, invoice_line_ids=[]),
        )

    def test_nested_link_cannot_steal_product_line_from_posted_invoice(self):
        self.invoice.action_post()
        target = self.empty_invoice()
        with self.env.cr.savepoint():
            before = self.accounting_snapshot()
            with unittest.TestCase.assertRaises(self, UserError):
                target.with_context(check_move_validity=False, skip_readonly_check=True).write({
                    "line_ids": [Command.link(self.invoice.invoice_line_ids.id)],
                })
            self.assertEqual(self.accounting_snapshot(), before)

    def test_nested_set_cannot_empty_posted_parent_by_stealing_all_lines(self):
        self.invoice.action_post()
        target = self.empty_invoice()
        with self.env.cr.savepoint():
            before = self.accounting_snapshot()
            with unittest.TestCase.assertRaises(self, UserError):
                target.with_context(check_move_validity=False, skip_readonly_check=True).write({
                    "line_ids": [Command.set(self.invoice.line_ids.ids)],
                })
            self.assertEqual(self.accounting_snapshot(), before)

    def test_nested_clear_cannot_abandon_draft_sales_source(self):
        with self.env.cr.savepoint():
            before = self.accounting_snapshot()
            with unittest.TestCase.assertRaises(self, UserError):
                self.invoice.write({"invoice_line_ids": [Command.update(
                    self.invoice.invoice_line_ids.id, {"sale_line_ids": [Command.clear()]},
                )]})
            self.assertEqual(self.accounting_snapshot(), before)

    def test_inverse_invoice_relation_default_cannot_attach_posted_line(self):
        self.invoice.action_post()
        order = self.make_sale(quantity=1, confirm=False)
        with self.env.cr.savepoint():
            before = self.accounting_snapshot()
            with unittest.TestCase.assertRaises(self, UserError):
                self.env["sale.order.line"].with_user(self.sales).sudo().with_context(
                    default_invoice_lines=[Command.set(self.invoice.invoice_line_ids.ids)],
                ).create({
                    "order_id": order.id, "product_id": self.product.id, "product_uom_qty": 1,
                    "price_unit": 100, "tax_id": [Command.clear()],
                })
            self.assertEqual(self.accounting_snapshot(), before)
