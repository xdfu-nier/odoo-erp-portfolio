"""Genuine physical returns and original-price native credit-note reservations."""
import unittest

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import new_test_user, tagged

from .test_fulfillment_ledger import FulfillmentLedgerCommon


@tagged("post_install", "-at_install", "portfolio_return_credit")
class TestReturnCredit(FulfillmentLedgerCommon):
    def pending_return(self, quantity=10):
        wizard = self.return_wizard(quantity)
        result = wizard.action_create_returns()
        return self.env["stock.picking"].with_user(self.warehouse).browse(result["res_id"])

    def return_wizard(self, quantity=10):
        picking = self.delivery.with_user(self.warehouse)
        wizard = self.env["stock.return.picking"].with_user(self.warehouse).with_context(
            active_model="stock.picking", active_id=picking.id, active_ids=picking.ids,
        ).create({})
        wizard.product_return_moves.write({"quantity": quantity})
        return wizard

    def return_values(self, **values):
        origin = self.delivery.move_ids
        return {
            "name": "Controlled customer return", "company_id": self.company.id,
            "product_id": self.product.id, "product_uom": self.product.uom_id.id,
            "product_uom_qty": 10, "location_id": self.customer_location.id,
            "location_dest_id": self.location.id, "picking_type_id": self.warehouse_a.in_type_id.id,
            "sale_line_id": self.line.id, "origin_returned_move_id": origin.id, "to_refund": True,
            **values,
        }

    def credit_values(self, quantity, original=None, **line_values):
        original = original or self.invoice
        entry = original.invoice_line_ids.filtered(lambda line: line.display_type == "product")[:1]
        return {
            "move_type": "out_refund", "company_id": self.company.id,
            "partner_id": self.customer.id, "invoice_date": fields.Date.today(),
            "journal_id": self.company_data["default_journal_sale"].id,
            "reversed_entry_id": original.id,
            "invoice_line_ids": [Command.create({
                "product_id": entry.product_id.id, "product_uom_id": entry.product_uom_id.id,
                "account_id": entry.account_id.id, "quantity": quantity,
                "price_unit": entry.price_unit, "discount": entry.discount,
                "tax_ids": [Command.set(entry.tax_ids.ids)], "sale_line_ids": [Command.set(self.line.ids)],
                **line_values,
            })],
        }

    def create_credit(self, quantity, original=None, **line_values):
        return self.env["account.move"].with_user(self.billing).with_company(self.company).create(
            self.credit_values(quantity, original=original, **line_values),
        )

    def physical_snapshot(self):
        self.env.flush_all()
        self.env.invalidate_all()
        moves = self.env["stock.move"].search([("sale_line_id", "=", self.line.id)])
        quants = self.env["stock.quant"].search([("product_id", "=", self.product.id)])
        return (
            tuple((move.id, move.state, move.quantity, move.origin_returned_move_id.id,
                   move.sale_line_id.id, tuple(move.move_line_ids.ids)) for move in moves),
            tuple((detail.id, detail.state, detail.quantity, detail.move_id.id,
                   detail.location_id.id, detail.location_dest_id.id) for detail in moves.move_line_ids),
            tuple((quant.id, quant.location_id.id, quant.quantity, quant.reserved_quantity) for quant in quants),
            tuple((picking.id, picking.state) for picking in moves.picking_id),
        )

    def test_native_return_wizard_keeps_refund_flag_and_real_origin(self):
        returned = self.pending_return()
        self.assertTrue(returned.move_ids.to_refund)
        self.assertEqual(returned.move_ids.origin_returned_move_id, self.delivery.move_ids)
        self.validate(returned, 10)
        self.assertEqual(self.api("physical_facts")(self.line)[self.line.id]["returned"], 10)

    def test_return_requires_genuine_completed_origin(self):
        for origin in (False, self.delivery_backorder.move_ids.id):
            with self.subTest(origin=origin), self.env.cr.savepoint(), self.assertRaises(UserError):
                self.env["stock.move"].with_user(self.warehouse).create(self.return_values(origin_returned_move_id=origin))

    def test_return_origin_product_must_match(self):
        other = self.env["product.product"].create({"name": "Other returned product", "type": "consu"})
        with self.assertRaises(UserError):
            self.env["stock.move"].with_user(self.warehouse).create(self.return_values(product_id=other.id))

    def test_return_origin_sale_source_must_match(self):
        other = self.make_sale(quantity=1).order_line
        with self.assertRaises(UserError):
            self.env["stock.move"].with_user(self.warehouse).create(self.return_values(sale_line_id=other.id))

    def test_return_locations_must_reverse_customer_delivery(self):
        with self.assertRaises(UserError):
            self.env["stock.move"].with_user(self.warehouse).create(self.return_values(location_id=self.location.id))

    def test_return_uom_must_be_compatible_with_original_delivery(self):
        with self.assertRaises(UserError):
            self.env["stock.move"].with_user(self.warehouse).create(self.return_values(
                product_uom=self.env.ref("uom.product_uom_kgm").id,
            ))

    def test_completed_return_origin_quantity_and_source_are_immutable(self):
        returned = self.make_return(10).with_user(self.warehouse)
        for values in ({"origin_returned_move_id": False}, {"quantity": 11}, {"sale_line_id": False}):
            with self.subTest(values=values), self.assertRaises(UserError):
                returned.move_ids.write(values)

    def test_pending_return_cannot_disable_refund_flag(self):
        returned = self.pending_return()
        with self.assertRaises(UserError):
            returned.move_ids.write({"to_refund": False})

    def test_false_refund_input_and_context_default_are_rejected(self):
        with self.assertRaises(UserError):
            self.env["stock.move"].with_user(self.warehouse).create(self.return_values(to_refund=False))
        values = self.return_values()
        values.pop("to_refund")
        with self.assertRaises(UserError):
            self.env["stock.move"].with_user(self.warehouse).with_context(default_to_refund=False).create(values)

    def test_native_return_wizard_cannot_disable_quantity_update(self):
        wizard = self.return_wizard()
        wizard.product_return_moves.write({"to_refund": False})
        with self.assertRaises(UserError):
            wizard.action_create_returns()

    def test_return_completion_cannot_exceed_actual_origin_delivery(self):
        returned = self.pending_return(71)
        returned.move_ids.write({"quantity": 71, "picked": True})
        with self.env.cr.savepoint():
            before = self.physical_snapshot()
            with unittest.TestCase.assertRaises(self, UserError):
                returned.button_validate()
            self.assertEqual(self.physical_snapshot(), before)

    def test_completed_returns_cumulatively_share_origin_quantity(self):
        self.make_return(40)
        returned = self.pending_return(31)
        with self.assertRaises(UserError):
            self.validate(returned, 31)
        self.assertEqual(self.api("physical_facts")(self.line)[self.line.id]["returned"], 40)

    def test_whole_return_batch_rolls_back_after_late_overreturn(self):
        first = self.pending_return(10)
        second = self.pending_return(61)
        first.move_ids.write({"quantity": 10, "picked": True})
        second.move_ids.write({"quantity": 61, "picked": True})
        with self.env.cr.savepoint():
            before = self.physical_snapshot()
            with unittest.TestCase.assertRaises(self, UserError):
                (first | second).button_validate()
            self.assertEqual(self.physical_snapshot(), before)

    def test_credit_requires_completed_physical_return(self):
        self.invoice.action_post()
        with self.assertRaises(UserError):
            self.create_credit(1)

    def test_pending_return_does_not_grant_credit_allowance(self):
        self.invoice.action_post()
        self.pending_return(10)
        with self.assertRaises(UserError):
            self.create_credit(1)

    def test_native_partial_credit_has_original_price_and_amount(self):
        self.invoice.action_post()
        self.make_return(10)
        credit = self.create_credit(10)
        self.assertEqual(credit.amount_untaxed, 850)
        self.assertEqual(credit.reversed_entry_id, self.invoice)
        self.assertEqual(credit.invoice_line_ids.sale_line_ids, self.line)
        credit.action_post()
        self.assertEqual(credit.state, "posted")
        self.assertEqual(sum(credit.line_ids.mapped("balance")), 0)

    def test_credit_cannot_exceed_source_return_allowance(self):
        self.invoice.action_post()
        self.make_return(10)
        with self.assertRaises(UserError):
            self.create_credit(11)

    def test_two_draft_credits_reserve_same_return_allowance(self):
        self.invoice.action_post()
        self.make_return(10)
        self.create_credit(7)
        with self.assertRaises(UserError):
            self.create_credit(4)

    def test_cancel_releases_credit_and_reset_must_reserve_again(self):
        self.invoice.action_post()
        self.make_return(10)
        credit = self.create_credit(10)
        credit.button_cancel()
        replacement = self.create_credit(10)
        self.assertEqual(replacement.amount_untaxed, 850)
        with self.assertRaises(UserError):
            credit.button_draft()
        self.assertEqual(credit.state, "cancel")

    def test_credit_requires_posted_original_invoice(self):
        self.make_return(10)
        with self.assertRaises(UserError):
            self.create_credit(1)

    def test_credit_cannot_exceed_selected_original_posted_quantity(self):
        self.invoice.button_cancel()
        first = self.make_invoice(5)
        second = self.make_invoice(65)
        (first | second).action_post()
        self.make_return(10)
        with self.assertRaises(UserError):
            self.create_credit(6, original=first)

    def test_different_original_invoices_compete_for_global_return_allowance(self):
        self.invoice.button_cancel()
        first = self.make_invoice(40)
        second = self.make_invoice(30)
        (first | second).action_post()
        self.make_return(10)
        self.create_credit(7, original=first)
        with self.assertRaises(UserError):
            self.create_credit(4, original=second)

    def test_credit_price_and_discount_must_match_original_pricing_basis(self):
        self.invoice.action_post()
        self.make_return(10)
        for values in ({"price_unit": 1}, {"discount": 0}):
            with self.subTest(values=values), self.env.cr.savepoint(), self.assertRaises(UserError):
                self.create_credit(10, **values)

    def test_credit_edit_cannot_replace_original_price(self):
        self.invoice.action_post()
        self.make_return(10)
        credit = self.create_credit(10)
        with self.assertRaises(UserError):
            credit.invoice_line_ids.write({"price_unit": 1})

    def test_one_original_pricing_group_cannot_borrow_other_group_quantity(self):
        self.invoice.button_cancel()
        original = self.make_invoice(5)
        original.write({"invoice_line_ids": [Command.create(self.line._prepare_invoice_line(quantity=65, price_unit=200))]})
        original.action_post()
        self.make_return(10)
        with self.assertRaises(UserError):
            self.create_credit(6, original=original, price_unit=100)

    def test_draft_credits_cumulatively_share_each_original_pricing_group(self):
        self.invoice.button_cancel()
        original = self.make_invoice(5)
        original.write({"invoice_line_ids": [Command.create(self.line._prepare_invoice_line(quantity=65, price_unit=200))]})
        original.action_post()
        self.make_return(10)
        self.create_credit(3, original=original, price_unit=100)
        with self.assertRaises(UserError):
            self.create_credit(3, original=original, price_unit=100)

    def test_two_original_pricing_groups_preserve_each_original_amount(self):
        self.invoice.button_cancel()
        original = self.make_invoice(5)
        original.write({"invoice_line_ids": [Command.create(self.line._prepare_invoice_line(quantity=65, price_unit=200))]})
        original.action_post()
        self.make_return(10)
        first = self.create_credit(5, original=original, price_unit=100)
        second = self.create_credit(5, original=original, price_unit=200)
        (first | second).action_post()
        self.assertEqual(first.amount_untaxed, 425)
        self.assertEqual(second.amount_untaxed, 850)

    def test_credit_uom_and_price_basis_cannot_be_reinterpreted(self):
        self.invoice.action_post()
        self.make_return(12)
        with self.assertRaises(UserError):
            self.create_credit(1, product_uom_id=self.uom_dozen.id)

    def test_zero_quantity_credit_cannot_take_public_raw_amount(self):
        self.invoice.action_post()
        self.make_return(10)
        with self.assertRaises(UserError):
            self.create_credit(0, balance=850, amount_currency=850)

    def test_credit_taxes_must_match_original_pricing_basis(self):
        self.invoice.action_post()
        self.make_return(10)
        tax = self.env["account.tax"].create({
            "name": "Different credit tax", "amount": 10, "type_tax_use": "sale", "company_id": self.company.id,
        })
        with self.assertRaises(UserError):
            self.create_credit(10, tax_ids=[Command.set(tax.ids)])

    def test_credit_claim_unlink_releases_both_allowances(self):
        self.invoice.action_post()
        self.make_return(10)
        credit = self.create_credit(10)
        credit.unlink()
        replacement = self.create_credit(10)
        self.assertEqual(replacement.amount_untaxed, 850)

    def test_credit_cannot_manufacture_new_normal_invoice_allowance(self):
        self.invoice.action_post()
        self.make_return(10)
        self.create_credit(10)
        with self.assertRaises(UserError):
            self.make_invoice(1)

    def test_malformed_legacy_credit_cannot_grant_new_normal_allowance(self):
        self.invoice.action_post()
        with self.legacy_fixture():
            self.create_credit(-1)
        with self.assertRaises(UserError):
            self.make_invoice(1)

    def test_active_credit_prevents_original_invoice_withdrawal(self):
        self.invoice.action_post()
        self.make_return(10)
        self.create_credit(10)
        for action in ("button_draft", "button_cancel"):
            with self.subTest(action=action), self.env.cr.savepoint(), self.assertRaises(UserError):
                getattr(self.invoice, action)()
        self.assertEqual(self.invoice.state, "posted")

    def test_cancelled_credit_does_not_prevent_original_cancellation(self):
        self.invoice.action_post()
        self.make_return(10)
        credit = self.create_credit(10)
        credit.button_cancel()
        self.invoice.button_cancel()
        self.assertEqual(self.invoice.state, "cancel")

    def test_posted_invoice_remains_valid_while_partial_credits_are_pending(self):
        self.invoice.action_post()
        self.make_return(10)
        first = self.create_credit(4)
        first.action_post()
        self.assertEqual(self.invoice.state, "posted")
        self.assertEqual(self.invoice.amount_untaxed, 5950)
        second = self.create_credit(6)
        second.action_post()
        self.assertEqual(first.amount_untaxed + second.amount_untaxed, 850)

    def test_draft_normal_invoice_must_fit_net_delivery_before_posting(self):
        self.make_return(10)
        with self.assertRaises(UserError):
            self.invoice.action_post()
        self.invoice.invoice_line_ids.write({"quantity": 60})
        self.invoice.action_post()
        self.assertEqual(self.invoice.amount_untaxed, 5100)

    def reversal_wizard(self):
        return self.env["account.move.reversal"].with_user(self.billing).with_company(self.company).with_context(
            active_model="account.move", active_ids=self.invoice.ids,
        ).create({"move_ids": [Command.set(self.invoice.ids)], "journal_id": self.invoice.journal_id.id})

    def test_public_full_reversal_cannot_temporarily_exceed_partial_return(self):
        self.invoice.action_post()
        self.make_return(10)
        wizard = self.reversal_wizard()
        with self.assertRaises(UserError):
            wizard.refund_moves()

    def test_native_full_reversal_is_valid_when_all_goods_were_returned(self):
        self.invoice.action_post()
        self.make_return(70)
        wizard = self.reversal_wizard()
        try:
            result = wizard.refund_moves()
        except UserError as error:
            self.fail(f"A genuine fully returned invoice must support native reversal: {error}")
        credit = self.env["account.move"].with_user(self.billing).browse(result["res_id"])
        self.assertEqual(credit.invoice_line_ids.quantity, 70)
        self.assertEqual(credit.amount_untaxed, 5950)
        credit.action_post()
        self.assertEqual(credit.state, "posted")

    def test_stock_and_own_sales_actor_can_return_another_salespersons_delivery(self):
        actor = new_test_user(
            self.env, login="return_stock_own_sales",
            groups="sales_team.group_sale_salesman,stock.group_stock_user",
            company_id=self.company.id, company_ids=[Command.set(self.companies.ids)],
        )
        self.assertFalse(actor.has_group("sales_team.group_sale_manager"))
        with self.assertRaises(AccessError):
            self.source.with_user(actor).check_access("read")
        try:
            wizard = self.env["stock.return.picking"].with_user(actor).with_context(
                allowed_company_ids=self.company.ids, active_model="stock.picking",
                active_id=self.delivery.id, active_ids=self.delivery.ids,
            ).create({})
            wizard.product_return_moves.write({"quantity": 1})
            result = wizard.action_create_returns()
            returned = self.env["stock.picking"].with_user(actor).with_context(
                allowed_company_ids=self.company.ids,
            ).browse(result["res_id"])
            returned.move_ids.write({"quantity": 1, "picked": True})
            returned.button_validate()
        except AccessError as error:
            self.fail(f"The actual Stock actor must retain native return rights without Sales read-all: {error}")
        self.assertEqual(returned.create_uid, actor)
        self.assertEqual(returned.state, "done")

    def test_actual_customer_return_details_cannot_hide_under_internal_parent(self):
        destination = self.env["stock.location"].create({
            "name": "Hidden return destination", "usage": "internal", "company_id": self.company.id,
            "location_id": self.location.id,
        })
        move = self.env["stock.move"].with_user(self.warehouse).create(self.return_values(
            origin_returned_move_id=False, location_id=self.location.id, location_dest_id=destination.id,
            product_uom_qty=1, to_refund=False,
        ))
        move._action_confirm(merge=False)
        self.env["stock.move.line"].with_user(self.warehouse).create({
            "move_id": move.id, "product_id": self.product.id, "product_uom_id": self.product.uom_id.id,
            "quantity": 1, "location_id": self.customer_location.id, "location_dest_id": destination.id,
        })
        move.write({"picked": True})
        with self.env.cr.savepoint():
            before = self.physical_snapshot()
            with unittest.TestCase.assertRaises(self, UserError):
                move._action_done()
            self.assertEqual(self.physical_snapshot(), before)

    def test_genuine_return_detail_cannot_enter_another_warehouse(self):
        other = self.env["stock.warehouse"].create({
            "name": "Other return warehouse", "code": "RCB", "company_id": self.company.id,
        })
        returned = self.pending_return(1)
        returned.move_ids.write({"quantity": 1, "picked": True})
        returned.move_ids.move_line_ids.write({"location_dest_id": other.lot_stock_id.id})
        with self.env.cr.savepoint():
            before = self.physical_snapshot()
            with unittest.TestCase.assertRaises(self, UserError):
                returned.button_validate()
            self.assertEqual(self.physical_snapshot(), before)
