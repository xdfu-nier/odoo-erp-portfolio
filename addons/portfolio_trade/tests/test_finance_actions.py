"""Usable Billing entry points and original-line bounded partial credits."""
import unittest

from lxml import etree
from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from .test_fulfillment_ledger import FulfillmentLedgerCommon


@tagged("post_install", "-at_install", "portfolio_finance_actions")
class TestFinanceActions(FulfillmentLedgerCommon):
    def public_method(self, records, name):
        method = getattr(records, name, None)
        self.assertTrue(callable(method), f"Missing public Finance action: {records._name}.{name}")
        return method

    def wizard_model(self, actor=None):
        name = "portfolio.partial.credit"
        self.assertTrue(name in self.env.registry.models, "Missing bounded partial-credit wizard")
        return self.env[name].with_user(actor or self.billing).with_company(self.company)

    def create_invoice(self, source=None):
        source = (source or self.source).with_user(self.billing).with_company(self.company)
        action = self.public_method(source, "action_portfolio_create_invoice")()
        return self.action_move(action)

    def action_move(self, action, actor=None):
        self.assertIsInstance(action, dict)
        self.assertEqual(action.get("type"), "ir.actions.act_window")
        self.assertEqual(action.get("res_model"), "account.move")
        self.assertTrue(action.get("res_id"), "The native invoice form must open the created document")
        move = self.env["account.move"].with_user(actor or self.billing).browse(action["res_id"])
        self.assertTrue(move.exists())
        self.assertTrue(move.read(["state", "move_type", "amount_untaxed"]))
        return move

    def posted_original(self):
        self.invoice.action_post()
        return self.invoice.invoice_line_ids.filtered(lambda entry: entry.display_type == "product")

    def credit_wizard(self, quantity=10, original_line=None, **context):
        original_line = original_line if original_line is not None else self.invoice.invoice_line_ids.filtered(
            lambda entry: entry.display_type == "product",
        )
        return self.wizard_model().with_context(**context).create({
            "original_line_id": original_line.id, "quantity": quantity,
        })

    def create_credit(self, quantity=10, original_line=None, **context):
        wizard = self.credit_wizard(quantity, original_line, **context)
        action = self.public_method(wizard, "action_create_credit")()
        return self.action_move(action)

    def snapshot(self):
        """Capture live business rows plus the serialization revision after a failure."""
        self.env.flush_all()
        self.env.invalidate_all()
        moves = self.env["account.move"].search([("company_id", "in", self.companies.ids)], order="id")
        entries = moves.line_ids.sorted("id")
        stock = self.env["stock.move"].search([("sale_line_id", "in", self.line.ids)], order="id")
        return (
            tuple((company.id, company.portfolio_stock_revision) for company in self.companies),
            tuple((move.id, move.state, move.move_type, move.reversed_entry_id.id,
                   move.amount_untaxed, move.amount_tax, tuple(move.line_ids.ids)) for move in moves),
            tuple((entry.id, entry.move_id.id, entry.quantity, entry.price_unit, entry.discount,
                   entry.balance, entry.amount_currency, tuple(entry.sale_line_ids.ids)) for entry in entries),
            tuple(self.line.invoice_lines.ids),
            tuple((move.id, move.state, move.quantity, tuple(move.move_line_ids.ids)) for move in stock),
        )

    def assert_atomic_denied(self, operation, exception=UserError):
        # Odoo's self.assertRaises adds its own rollback and would conceal a
        # missing public-method savepoint. Only this outer fixture cleanup frame
        # is supplied by the test; catch the failure with the stdlib assertion.
        with self.env.cr.savepoint():
            before = self.snapshot()
            with unittest.TestCase.assertRaises(self, exception):
                operation()
            self.assertEqual(self.snapshot(), before)

    def hide_source(self):
        self.env["ir.rule"].create({
            "name": "Finance test: deny the selected real source",
            "model_id": self.env["ir.model"]._get_id("sale.order"),
            "domain_force": repr([("id", "!=", self.source.id)]),
        })

    def test_billing_action_creates_first_seventy_and_native_posts_5950(self):
        self.invoice.button_cancel()
        invoice = self.create_invoice()
        self.assertEqual(invoice.create_uid, self.billing)
        self.assertEqual(invoice.state, "draft")
        self.assertEqual(invoice.invoice_line_ids.quantity, 70)
        self.assertEqual(invoice.invoice_line_ids.sale_line_ids, self.line)
        self.assertEqual(invoice.amount_untaxed, 5950)
        invoice.action_post()
        self.assertEqual(invoice.state, "posted")
        self.assertEqual(sum(invoice.line_ids.mapped("balance")), 0)

    def test_billing_action_creates_second_thirty_and_native_posts_2550(self):
        self.invoice.action_post()
        self.validate(self.receipt_backorder, 30)
        self.validate(self.delivery_backorder, 30)
        second = self.create_invoice()
        self.assertEqual(second.invoice_line_ids.quantity, 30)
        self.assertEqual(second.amount_untaxed, 2550)
        second.action_post()
        self.assertEqual(second.state, "posted")

    def test_invoice_action_requires_actual_billing_uid_even_in_sudo_and_forged_context(self):
        self.public_method(self.source, "action_portfolio_create_invoice")
        for actor in (self.sales, self.manager, self.buyer, self.warehouse, self.settings_only):
            with self.subTest(actor=actor.login):
                source = self.source.with_user(actor).sudo().with_context(
                    portfolio_finance=True, skip_portfolio_fulfillment=True,
                    check_move_validity=False, allowed_company_ids=self.companies.ids,
                )
                self.assert_atomic_denied(source.action_portfolio_create_invoice, AccessError)

    def test_invoice_action_rejects_forged_actual_company_membership_before_mutation(self):
        source = self.source.with_user(self.billing_b).sudo().with_context(
            allowed_company_ids=self.companies.ids,
        )
        action = self.public_method(source, "action_portfolio_create_invoice")
        self.assert_atomic_denied(action, AccessError)

    def test_invoice_action_rejects_inactive_company_even_for_actual_member(self):
        source = self.source.with_user(self.billing).sudo().with_context(
            allowed_company_ids=self.other_company.ids,
        )
        action = self.public_method(source, "action_portfolio_create_invoice")
        self.assert_atomic_denied(action, AccessError)

    def test_invoice_action_retains_actual_source_read_rules_even_in_sudo(self):
        action = self.public_method(self.source.with_user(self.billing).sudo(), "action_portfolio_create_invoice")
        self.hide_source()
        self.assert_atomic_denied(action, AccessError)

    def test_invoice_action_no_available_quantity_has_live_atomic_failure(self):
        action = self.public_method(self.source.with_user(self.billing), "action_portfolio_create_invoice")
        self.assert_atomic_denied(action)

    def test_billing_can_web_read_real_sale_form_and_reach_finance_menu_without_sales_role(self):
        for group in ("sales_team.group_sale_salesman", "stock.group_stock_user", "base.group_system"):
            self.assertFalse(self.billing.has_group(group))
        self.assertFalse(self.env["sale.advance.payment.inv"].with_user(self.billing).has_access("create"))
        source = self.source.with_user(self.billing)
        view = source.get_view(view_id=self.env.ref("sale.view_order_form").id, view_type="form")
        arch = etree.fromstring(view["arch"].encode())
        self.assertTrue(arch.xpath(".//button[@name='action_portfolio_create_invoice']"))
        specification = {node.get("name"): {} for node in arch.xpath(".//field[not(ancestor::field)]")}
        if "portfolio_approval_history_ids" in specification:
            specification["portfolio_approval_history_ids"] = {"fields": {"event": {}}}
        result = source.web_read(specification)
        self.assertEqual(result[0]["id"], source.id)
        visible = self.env["ir.ui.menu"].with_user(self.billing)._visible_menu_ids()
        self.assertIn(self.env.ref("portfolio_trade.menu_portfolio_sales").id, visible)
        self.assertIn(self.env.ref("portfolio_trade.menu_portfolio_root").id, visible)
        menu_action = self.env["ir.actions.act_window"].with_user(self.billing)._for_xml_id(
            "portfolio_trade.action_portfolio_sales",
        )
        self.assertEqual(menu_action["res_model"], "sale.order")

    def test_sales_form_hides_custom_finance_button_from_sales_actor(self):
        view = self.source.with_user(self.sales).get_view(
            view_id=self.env.ref("sale.view_order_form").id, view_type="form",
        )
        self.assertFalse(etree.fromstring(view["arch"].encode()).xpath(
            ".//button[@name='action_portfolio_create_invoice']",
        ))

    def test_original_invoice_opens_real_billing_credit_form(self):
        original = self.posted_original()
        action = self.public_method(self.invoice, "action_portfolio_partial_credit")()
        self.assertEqual(action.get("type"), "ir.actions.act_window")
        self.assertEqual(action.get("res_model"), "portfolio.partial.credit")
        self.assertEqual(action.get("target"), "new")
        model = self.wizard_model()
        view = model.get_view(view_type="form", view_id=action.get("view_id"))
        arch = etree.fromstring(view["arch"].encode())
        self.assertTrue(arch.xpath(".//field[@name='original_line_id']"))
        self.assertTrue(arch.xpath(".//field[@name='quantity']"))
        self.assertTrue(arch.xpath(".//field[@name='available_quantity']"))
        self.assertTrue(arch.xpath(".//button[@name='action_create_credit']"))
        if action.get("res_id"):
            wizard = model.browse(action["res_id"])
        else:
            wizard = model.with_context(**action.get("context", {})).create({})
        self.assertEqual(wizard.original_line_id, original)
        invoice_view = self.invoice.get_view(view_id=self.env.ref("account.view_move_form").id, view_type="form")
        self.assertTrue(etree.fromstring(invoice_view["arch"].encode()).xpath(
            ".//button[@name='action_portfolio_partial_credit']",
        ))

    def test_partial_credit_wizard_acl_is_billing_only(self):
        model = self.wizard_model()
        for operation in ("read", "write", "create", "unlink"):
            self.assertTrue(model.has_access(operation))
            for actor in (self.sales, self.manager, self.buyer, self.warehouse, self.settings_only):
                with self.subTest(operation=operation, actor=actor.login):
                    self.assertFalse(model.with_user(actor).has_access(operation))

    def test_service_first_mixed_invoice_opens_credit_for_returned_physical_line(self):
        service = self.env["product.product"].with_company(self.company).create({
            "name": "Finance mixed invoice service", "default_code": "FINANCE-MIXED-SVC",
            "type": "service", "invoice_policy": "order", "taxes_id": [Command.clear()],
            "property_account_income_id": self.company_data["default_account_revenue"].id,
        })
        service_order = self.make_sale(quantity=3, product=service)
        self.invoice.button_cancel()
        original = self.make_invoice(70, invoice_line_ids=[
            Command.create(service_order.order_line._prepare_invoice_line(sequence=1, quantity=3)),
            Command.create(self.line._prepare_invoice_line(sequence=2, quantity=70)),
        ])
        self.assertEqual(original.invoice_line_ids.mapped("product_id"), service | self.product)
        original.action_post()
        self.assertEqual(original.state, "posted")
        self.make_return(10)
        try:
            action = self.public_method(original, "action_portfolio_partial_credit")()
        except UserError as error:
            self.fail(f"A legitimate service-first mixed invoice must open its physical credit line: {error}")
        self.assertEqual(action.get("res_model"), "portfolio.partial.credit")
        model = self.wizard_model()
        wizard = model.browse(action["res_id"]) if action.get("res_id") else model.with_context(
            **action.get("context", {}),
        ).create({})
        physical = original.invoice_line_ids.filtered(lambda entry: entry.product_id == self.product)
        self.assertEqual(wizard.original_line_id, physical)
        self.assertEqual(wizard.available_quantity, 10)
        wizard.quantity = 10
        credit = self.action_move(self.public_method(wizard, "action_create_credit")())
        self.assertEqual(credit.invoice_line_ids.sale_line_ids, self.line)
        self.assertEqual(credit.invoice_line_ids.product_id, self.product)
        self.assertEqual(credit.amount_untaxed, 850)
        credit.action_post()
        self.assertEqual(credit.state, "posted")

    def test_bounded_ten_credit_directly_creates_850_at_exact_original_basis_and_native_posts(self):
        original = self.posted_original()
        self.make_return(10)
        credit = self.create_credit(10)
        entry = credit.invoice_line_ids.filtered(lambda entry: entry.display_type == "product")
        self.assertEqual(credit.create_uid, self.billing)
        self.assertEqual(credit.state, "draft")
        self.assertEqual(credit.move_type, "out_refund")
        self.assertEqual(credit.reversed_entry_id, self.invoice)
        for name in ("company_id", "currency_id", "partner_id", "journal_id"):
            self.assertEqual(credit[name], self.invoice[name])
        for name in ("product_id", "product_uom_id", "price_unit", "discount", "tax_ids", "account_id", "sale_line_ids"):
            self.assertEqual(entry[name], original[name])
        self.assertEqual(entry.quantity, 10)
        self.assertEqual(credit.amount_untaxed, 850)
        self.assertEqual(self.api("financial_facts")(self.line)[self.line.id]["credit_reserved"], 10)
        credit.action_post()
        self.assertEqual(credit.state, "posted")
        self.assertEqual(sum(credit.line_ids.mapped("balance")), 0)
        self.assertEqual(self.env["account.move"].search_count([
            ("reversed_entry_id", "=", self.invoice.id), ("move_type", "=", "out_refund"),
        ]), 1)

    def test_credit_revalidates_stale_displayed_allowance_with_live_atomic_failure(self):
        self.posted_original()
        self.make_return(10)
        wizard = self.credit_wizard(7)
        self.assertEqual(wizard.available_quantity, 10)
        self.create_credit(4)
        action = self.public_method(wizard, "action_create_credit")
        self.assert_atomic_denied(action)

    def test_cancelled_draft_credit_releases_public_wizard_allowance(self):
        self.posted_original()
        self.make_return(10)
        credit = self.create_credit(10)
        credit.button_cancel()
        wizard = self.credit_wizard(10)
        self.assertEqual(wizard.available_quantity, 10)
        replacement = self.action_move(self.public_method(wizard, "action_create_credit")())
        self.assertEqual(replacement.amount_untaxed, 850)

    def test_credit_quantity_must_be_finite_and_strictly_positive(self):
        self.posted_original()
        self.make_return(10)
        self.wizard_model()
        for quantity in (0, -1, float("nan"), float("inf"), -float("inf")):
            with self.subTest(quantity=quantity):
                self.assert_atomic_denied(lambda quantity=quantity: self.create_credit(quantity))

    def test_credit_opener_and_submission_require_actual_billing_uid_even_in_sudo(self):
        self.posted_original()
        self.make_return(10)
        wizard = self.credit_wizard(10)
        self.public_method(self.invoice, "action_portfolio_partial_credit")
        self.public_method(wizard, "action_create_credit")
        for actor in (self.sales, self.manager, self.buyer, self.warehouse, self.settings_only):
            with self.subTest(actor=actor.login):
                invoice = self.invoice.with_user(actor).sudo().with_context(portfolio_finance=True)
                forged = wizard.with_user(actor).sudo().with_context(portfolio_finance=True)
                self.assert_atomic_denied(invoice.action_portfolio_partial_credit, AccessError)
                self.assert_atomic_denied(forged.action_create_credit, AccessError)

    def test_credit_actual_and_active_company_are_revalidated_even_in_sudo(self):
        self.posted_original()
        self.make_return(10)
        wizard = self.credit_wizard(10)
        self.public_method(self.invoice, "action_portfolio_partial_credit")
        self.public_method(wizard, "action_create_credit")
        cases = ((self.billing_b, self.companies.ids), (self.billing, self.other_company.ids))
        for actor, active in cases:
            with self.subTest(actor=actor.login, active=active):
                invoice = self.invoice.with_user(actor).sudo().with_context(allowed_company_ids=active)
                forged = wizard.with_user(actor).sudo().with_context(allowed_company_ids=active)
                self.assert_atomic_denied(invoice.action_portfolio_partial_credit, AccessError)
                self.assert_atomic_denied(forged.action_create_credit, AccessError)

    def test_credit_opener_and_submission_retain_actual_source_read_rules(self):
        self.posted_original()
        self.make_return(10)
        wizard = self.credit_wizard(10)
        opener = self.public_method(self.invoice.sudo(), "action_portfolio_partial_credit")
        action = self.public_method(wizard.sudo(), "action_create_credit")
        self.hide_source()
        self.assert_atomic_denied(opener, AccessError)
        self.assert_atomic_denied(action, AccessError)

    def test_draft_original_is_rejected_by_opener_and_credit_submission(self):
        self.make_return(10)
        opener = self.public_method(self.invoice, "action_portfolio_partial_credit")
        self.assert_atomic_denied(opener)
        self.assert_atomic_denied(lambda: self.create_credit(10))

    def test_credit_note_cannot_be_used_as_a_new_original(self):
        self.posted_original()
        self.make_return(10)
        credit = self.create_credit(5)
        credit.action_post()
        opener = self.public_method(credit, "action_portfolio_partial_credit")
        self.assert_atomic_denied(opener)
        self.assert_atomic_denied(lambda: self.create_credit(1, credit.invoice_line_ids))

    def test_non_product_original_accounting_line_is_rejected(self):
        self.posted_original()
        self.make_return(10)
        counterpart = self.invoice.line_ids.filtered(lambda entry: entry.display_type == "payment_term")
        self.assertTrue(counterpart)
        self.wizard_model()
        self.assert_atomic_denied(lambda: self.create_credit(1, counterpart))

    def test_original_with_missing_sale_source_is_rejected(self):
        original = self.invoice.invoice_line_ids
        with self.legacy_fixture():
            original.write({"sale_line_ids": [Command.clear()]})
            self.invoice.action_post()
        self.make_return(10)
        self.wizard_model()
        self.assert_atomic_denied(lambda: self.create_credit(1, original))

    def test_original_with_ambiguous_sale_source_is_rejected(self):
        other = self.make_sale(quantity=1).order_line
        original = self.invoice.invoice_line_ids
        with self.legacy_fixture():
            original.write({"sale_line_ids": [Command.link(other.id)]})
            self.invoice.action_post()
        self.make_return(10)
        self.wizard_model()
        self.assert_atomic_denied(lambda: self.create_credit(1, original))

    def test_original_with_wrong_product_is_rejected(self):
        product = self.product.copy({"name": "Different original product", "default_code": "FINANCE-OTHER"})
        original = self.invoice.invoice_line_ids
        with self.legacy_fixture():
            original.write({"product_id": product.id})
            self.invoice.action_post()
        self.make_return(10)
        self.wizard_model()
        self.assert_atomic_denied(lambda: self.create_credit(1, original))

    def test_credit_requires_actual_completed_return(self):
        self.posted_original()
        self.wizard_model()
        self.assert_atomic_denied(lambda: self.create_credit(1))

    def test_selected_original_quantity_limits_display_and_server(self):
        self.invoice.button_cancel()
        first = self.make_invoice(5)
        second = self.make_invoice(65)
        (first | second).action_post()
        self.make_return(10)
        wizard = self.credit_wizard(6, first.invoice_line_ids)
        self.assertEqual(wizard.available_quantity, 5)
        self.assert_atomic_denied(self.public_method(wizard, "action_create_credit"))

    def test_original_pricing_groups_share_global_allowance_without_borrowing_other_group(self):
        self.invoice.button_cancel()
        original = self.make_invoice(5)
        original.write({"invoice_line_ids": [Command.create(self.line._prepare_invoice_line(quantity=65, price_unit=200))]})
        original.action_post()
        self.make_return(10)
        inexpensive = original.invoice_line_ids.filtered(lambda entry: entry.price_unit == 100)
        expensive = original.invoice_line_ids.filtered(lambda entry: entry.price_unit == 200)
        self.assertEqual(self.credit_wizard(5, inexpensive).available_quantity, 5)
        first = self.create_credit(3, inexpensive)
        wizard = self.credit_wizard(3, inexpensive)
        self.assertEqual(wizard.available_quantity, 2)
        self.assert_atomic_denied(self.public_method(wizard, "action_create_credit"))
        self.assertEqual(self.credit_wizard(7, expensive).available_quantity, 7)
        second = self.create_credit(7, expensive)
        self.assertEqual(first.amount_untaxed, 255)
        self.assertEqual(second.amount_untaxed, 1190)

    def test_credit_quantity_and_display_use_original_invoice_line_uom(self):
        self.invoice.button_cancel()
        original = self.make_invoice(2, uom=self.uom_dozen)
        original.invoice_line_ids.write({"price_unit": 1200})
        original.action_post()
        self.make_return(12)
        wizard = self.credit_wizard(1, original.invoice_line_ids)
        self.assertEqual(wizard.available_quantity, 1)
        credit = self.action_move(self.public_method(wizard, "action_create_credit")())
        self.assertEqual(credit.invoice_line_ids.quantity, 1)
        self.assertEqual(credit.invoice_line_ids.product_uom_id, self.uom_dozen)
        self.assertEqual(credit.invoice_line_ids.price_unit, 1200)
        self.assertEqual(credit.amount_untaxed, 1020)
        self.assertEqual(self.api("financial_facts")(self.line)[self.line.id]["credit_reserved"], 12)

    def test_credit_preserves_native_tax_and_counterpart_synchronization(self):
        tax = self.env["account.tax"].create({
            "name": "Original invoice ten percent", "amount": 10,
            "type_tax_use": "sale", "company_id": self.company.id,
        })
        self.invoice.invoice_line_ids.write({"tax_ids": [Command.set(tax.ids)]})
        self.posted_original()
        self.make_return(10)
        credit = self.create_credit(10)
        self.assertEqual(credit.invoice_line_ids.tax_ids, tax)
        self.assertEqual(credit.amount_untaxed, 850)
        self.assertEqual(credit.amount_tax, 85)
        self.assertEqual(credit.amount_total, 935)
        credit.action_post()
        self.assertEqual(sum(credit.line_ids.mapped("balance")), 0)
        self.assertEqual(len(credit.line_ids.filtered("tax_line_id")), 1)
        self.assertEqual(sum(credit.line_ids.filtered(
            lambda entry: entry.display_type == "payment_term",
        ).mapped("balance")), -935)

    def test_context_defaults_and_stale_maximum_do_not_authorize_extra_credit(self):
        self.posted_original()
        self.make_return(10)
        wizard = self.credit_wizard(11, default_available_quantity=999, available_quantity=999)
        action = self.public_method(wizard.with_context(
            skip_portfolio_fulfillment=True, check_move_validity=False,
            default_price_unit=1, default_discount=0, default_quantity=999,
        ), "action_create_credit")
        self.assert_atomic_denied(action)

    def test_original_state_is_revalidated_after_wizard_opened(self):
        self.posted_original()
        self.make_return(10)
        wizard = self.credit_wizard(10)
        self.invoice.button_cancel()
        self.assert_atomic_denied(self.public_method(wizard, "action_create_credit"))

    def test_full_native_role_sequence_ends_in_stock_ten_and_posted_product_net_7650(self):
        # The common performs initial40 / PO60 / receipt30 / delivery70 with
        # separate actual actors. Replace only its native draft with our action.
        self.invoice.button_cancel()
        first = self.create_invoice()
        first.action_post()
        self.validate(self.receipt_backorder, 30)
        self.validate(self.delivery_backorder, 30)
        second = self.create_invoice()
        second.action_post()
        self.make_return(10)
        credit = self.create_credit(10, first.invoice_line_ids)
        credit.action_post()
        self.assertEqual((first.amount_untaxed, second.amount_untaxed, credit.amount_untaxed), (5950, 2550, 850))
        quants = self.env["stock.quant"].with_user(self.warehouse).search([
            ("product_id", "=", self.product.id), ("location_id", "=", self.location.id),
        ])
        self.assertEqual(sum(quants.mapped("quantity")), 10)
        physical = self.api("physical_facts")(self.line)[self.line.id]
        self.assertEqual(physical, {"delivered": 100.0, "returned": 10.0})
        self.assertEqual(physical["delivered"] - physical["returned"], 90)
        posted_products = self.env["account.move.line"].with_user(self.billing).search([
            ("sale_line_ids", "in", self.line.ids), ("display_type", "=", "product"),
            ("parent_state", "=", "posted"), ("move_id.move_type", "in", ["out_invoice", "out_refund"]),
        ])
        self.assertEqual(set(posted_products.move_id.ids), set((first | second | credit).ids))
        self.assertEqual(-sum(posted_products.mapped("balance")), 7650)
        financial = self.api("financial_facts")(self.line)[self.line.id]
        self.assertEqual(financial["invoice_posted"], 100)
        self.assertEqual(financial["credit_posted"], 10)
        self.assertEqual(financial["signed_posted"], 90)
