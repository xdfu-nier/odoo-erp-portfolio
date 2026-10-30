"""Authoritative native stock/account quantities, without financial ORM guards."""
import importlib
import unittest
from contextlib import contextmanager

from odoo import Command, fields
from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.exceptions import AccessError, UserError
from odoo.tests import new_test_user, tagged


class FulfillmentLedgerCommon(AccountTestInvoicingCommon):
    chart_template = "generic_coa"

    @classmethod
    def setup_independent_company(cls, **kwargs):
        # The native accounting common creates a separate test company before
        # collecting accounts. Never load its generic US chart in the developer
        # company; _create_company restores the explicitly supplied CNY after
        # the native chart loader has applied its currency defaults.
        cny = cls.env.ref("base.CNY")
        cny.active = True
        return cls._create_company(name="Ledger fulfillment A", currency_id=cny.id, **kwargs)

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company.write({
            "portfolio_fulfillment_enabled": True, "portfolio_approval_enabled": True,
            "portfolio_masterdata_enabled": True,
        })
        cls.other_company = cls._create_company(
            name="Ledger native B", currency_id=cls.env.ref("base.CNY").id,
        )
        cls.companies = cls.company | cls.other_company
        cls.env = cls.env(context={**cls.env.context, "allowed_company_ids": cls.companies.ids})
        cls.other_data = cls.collect_company_accounting_data(cls.other_company)
        actors = (
            ("sales", "sales_team.group_sale_salesman", cls.companies),
            ("other_sales", "sales_team.group_sale_salesman", cls.companies),
            ("manager", "sales_team.group_sale_manager", cls.companies),
            ("buyer", "purchase.group_purchase_user", cls.companies),
            ("warehouse", "stock.group_stock_user", cls.companies),
            ("stock_manager", "stock.group_stock_manager", cls.companies),
            ("billing", "account.group_account_invoice", cls.companies),
            ("billing_a", "account.group_account_invoice", cls.company),
            ("billing_b", "account.group_account_invoice", cls.other_company),
            ("settings_only", "base.group_system", cls.companies),
        )
        for name, groups, companies in actors:
            setattr(cls, name, new_test_user(
                cls.env, login=f"ledger_{name}", groups=groups,
                company_id=companies[0].id, company_ids=[Command.set(companies.ids)],
            ))
        cls.warehouse_a = cls.env["stock.warehouse"].search([("company_id", "=", cls.company.id)], limit=1)
        cls.warehouse_b = cls.env["stock.warehouse"].search([("company_id", "=", cls.other_company.id)], limit=1)
        cls.location = cls.warehouse_a.lot_stock_id
        cls.other_location = cls.warehouse_b.lot_stock_id
        cls.customer_location = cls.env.ref("stock.stock_location_customers")
        cls.customer = cls.env["res.partner"].create({"name": "Ledger customer", "ref": "LEDGER-C"})
        cls.vendor = cls.env["res.partner"].create({"name": "Ledger supplier", "ref": "LEDGER-V"})
        cls.product = cls.env["product.product"].with_company(cls.company).create({
            "name": "Ledger bearing", "default_code": "LEDGER-BRG", "type": "consu",
            "is_storable": True, "invoice_policy": "delivery", "list_price": 100,
            "standard_price": 60, "taxes_id": [Command.clear()], "supplier_taxes_id": [Command.clear()],
            "property_account_income_id": cls.company_data["default_account_revenue"].id,
            "property_account_expense_id": cls.company_data["default_account_expense"].id,
        })
        cls.product.with_company(cls.other_company).write({
            "property_account_income_id": cls.other_data["default_account_revenue"].id,
            "property_account_expense_id": cls.other_data["default_account_expense"].id,
        })
        cls.env["product.supplierinfo"].create({
            "product_tmpl_id": cls.product.product_tmpl_id.id, "partner_id": cls.vendor.id,
            "company_id": cls.company.id, "price": 60, "currency_id": cls.company.currency_id.id,
        })
        for company, location in ((cls.company, cls.location), (cls.other_company, cls.other_location)):
            quant = cls.env["stock.quant"].with_user(cls.stock_manager).with_company(company).with_context(
                inventory_mode=True,
            ).create({"product_id": cls.product.id, "location_id": location.id, "inventory_quantity": 40})
            quant.action_apply_inventory()
        cls.source = cls.make_sale()
        cls.line = cls.source.order_line.with_user(cls.billing).with_company(cls.company)
        cls.purchase = cls.env["purchase.order"].with_user(cls.buyer).with_company(cls.company).create({
            "partner_id": cls.vendor.id, "company_id": cls.company.id,
            "portfolio_source_sale_id": cls.source.id,
            "order_line": [Command.create({
                "product_id": cls.product.id, "product_qty": 60, "price_unit": 60,
                "product_uom": cls.product.uom_po_id.id, "date_planned": fields.Datetime.now(),
                "taxes_id": [Command.clear()],
            })],
        })
        cls.purchase.button_confirm()
        cls.receipt = cls.purchase.picking_ids
        cls.receipt_backorder = cls.validate(cls.receipt, 30)
        cls.delivery = cls.source.picking_ids
        cls.delivery_backorder = cls.validate(cls.delivery, 70)
        # This exact native draft is the positive baseline; only Billing creates
        # and later posts financial documents. Native Sales uses sudo internally,
        # but keeps the Billing uid, and we restore its ordinary env afterwards.
        cls.invoice = cls.source.with_user(cls.billing)._create_invoices().with_user(cls.billing)

    @classmethod
    def make_sale(cls, quantity=100, company=None, product=None, uom=None, confirm=True):
        company = company or cls.company
        product = product or cls.product
        order = cls.env["sale.order"].with_user(cls.sales).with_company(company).create({
            "partner_id": cls.customer.id, "company_id": company.id, "user_id": cls.sales.id,
            "order_line": [Command.create({
                "product_id": product.id, "product_uom_qty": quantity,
                "product_uom": (uom or product.uom_id).id, "price_unit": 100,
                "discount": 15, "tax_id": [Command.clear()],
            })],
        })
        if confirm:
            if company.portfolio_approval_enabled:
                order.action_portfolio_submit()
                order.with_user(cls.manager).action_portfolio_approve()
            order.action_confirm()
        return order

    @classmethod
    def validate(cls, picking, quantity):
        picking = picking.with_user(cls.warehouse).with_company(picking.company_id)
        picking.move_ids.write({"quantity": quantity, "picked": True})
        result = picking.button_validate()
        if isinstance(result, dict) and result.get("res_model") == "stock.backorder.confirmation":
            cls.env["stock.backorder.confirmation"].with_user(cls.warehouse).with_context(
                **result["context"],
            ).create({}).process()
        return cls.env["stock.picking"].search([("backorder_id", "=", picking.id)])

    def api(self, name):
        """Missing helpers fail assertions after registry/fixtures load normally."""
        module_name = "odoo.addons.portfolio_trade.models.financial_policy"
        try:
            policy = importlib.import_module(module_name)
        except ModuleNotFoundError as error:
            if error.name != module_name:
                raise
            policy = None
        helper = getattr(policy, name, None)
        self.assertTrue(callable(helper), f"Missing authoritative financial helper: {name}")
        return helper

    def make_invoice(self, quantity, line=None, original=None, company=None, uom=None, **values):
        line = line if line is not None else self.line
        company = company or line.order_id.company_id
        data = self.company_data if company == self.company else self.other_data
        return self.env["account.move"].with_user(self.billing).with_company(company).create({
            "move_type": "out_refund" if original else "out_invoice",
            "company_id": company.id, "partner_id": self.customer.id,
            "invoice_date": fields.Date.today(), "journal_id": data["default_journal_sale"].id,
            "reversed_entry_id": original.id if original else False,
            "invoice_line_ids": [Command.create({
                "product_id": line.product_id.id, "product_uom_id": (uom or line.product_uom).id,
                "account_id": data["default_account_revenue"].id,
                "quantity": quantity, "price_unit": 100, "discount": 15,
                "tax_ids": [Command.clear()], "sale_line_ids": [Command.set(line.ids)],
            })], **values,
        })

    def make_return(self, quantity=10, picking=None, to_refund=True):
        picking = (picking or self.delivery).with_user(self.warehouse)
        wizard = self.env["stock.return.picking"].with_user(self.warehouse).with_context(
            active_model="stock.picking", active_id=picking.id, active_ids=picking.ids,
        ).create({})
        wizard.product_return_moves.write({"quantity": quantity})
        result = wizard.action_create_returns()
        returned = self.env["stock.picking"].browse(result["res_id"])
        returned.with_user(self.warehouse).move_ids.write({"to_refund": to_refund})
        self.validate(returned, quantity)
        self.assertEqual(returned.state, "done")
        return returned

    def assert_denied_before_revision(self, operation):
        self.company.invalidate_recordset(["portfolio_stock_revision"])
        before = self.company.portfolio_stock_revision
        # stdlib assertRaises leaves the live transaction intact: an internal
        # company revision bump before authorization must remain observable.
        with unittest.TestCase.assertRaises(self, AccessError):
            operation()
        self.company.invalidate_recordset(["portfolio_stock_revision"])
        self.assertEqual(self.company.portfolio_stock_revision, before)

    @contextmanager
    def legacy_fixture(self):
        """Prepare explicitly pre-policy defects, then read facts with policy on."""
        company = self.company.with_user(self.settings_only)
        enabled = company.portfolio_fulfillment_enabled
        company.write({"portfolio_fulfillment_enabled": False})
        try:
            yield
        finally:
            company.write({"portfolio_fulfillment_enabled": enabled})


@tagged("post_install", "-at_install", "portfolio_ledger")
class TestFulfillmentLedger(FulfillmentLedgerCommon):
    def test_native_cny_approved_partial_fixture_and_gross_physical_facts(self):
        physical_facts = self.api("physical_facts")
        self.assertEqual(self.company.currency_id, self.env.ref("base.CNY"))
        self.assertEqual(self.other_company.currency_id, self.env.ref("base.CNY"))
        self.assertEqual(self.source.portfolio_approval_state, "approved")
        self.assertNotEqual(self.source.user_id, self.manager)
        self.assertEqual(self.source.order_line.product_uom_qty, 100)
        self.assertEqual(self.source.order_line.discount, 15)
        self.assertEqual(self.source.amount_untaxed, 8500)
        self.assertEqual(self.invoice.invoice_line_ids.quantity, 70)
        self.assertEqual(self.invoice.create_uid, self.billing)
        self.assertEqual(physical_facts(self.line), {self.line.id: {"delivered": 70.0, "returned": 0.0}})

    def test_second_partial_delivery_accumulates_without_counting_receipts(self):
        physical_facts = self.api("physical_facts")
        self.validate(self.receipt_backorder, 30)
        self.validate(self.delivery_backorder, 30)
        self.assertEqual(physical_facts(self.line)[self.line.id], {"delivered": 100.0, "returned": 0.0})

    def test_done_return_is_separate_from_gross_delivery(self):
        physical_facts = self.api("physical_facts")
        self.make_return()
        self.assertEqual(physical_facts(self.line)[self.line.id], {"delivered": 70.0, "returned": 10.0})

    def test_return_is_physical_even_when_native_to_refund_counter_flag_is_false(self):
        physical_facts = self.api("physical_facts")
        with self.legacy_fixture():
            self.make_return(to_refund=False)
        self.assertEqual(physical_facts(self.line)[self.line.id]["returned"], 10)

    def test_presentation_accrual_date_cannot_hide_done_return(self):
        physical_facts = self.api("physical_facts")
        self.make_return()
        contextual = self.line.with_context(accrual_entry_date="2000-01-01", to_date="2000-01-01", active_test=True)
        self.assertEqual(physical_facts(contextual)[self.line.id], {"delivered": 70.0, "returned": 10.0})

    def test_billing_reads_physical_facts_without_stock_or_sales_or_settings_groups(self):
        physical_facts = self.api("physical_facts")
        for group in ("stock.group_stock_user", "sales_team.group_sale_salesman", "base.group_system"):
            self.assertFalse(self.billing.has_group(group))
        # Pinned stock_account grants Billing native stock.move access without
        # implying the Stock role. Preserve that native compatibility.
        self.env["stock.move"].with_user(self.billing).check_access("read")
        self.assertEqual(physical_facts(self.line)[self.line.id]["delivered"], 70)

    def test_delivery_move_uom_is_converted_to_source_uom(self):
        physical_facts = self.api("physical_facts")
        order = self.make_sale(quantity=2, uom=self.uom_dozen)
        # Native source is two dozen, but its stock move can execute in units.
        move = order.picking_ids.move_ids.with_user(self.warehouse)
        move.write({"product_uom": self.product.uom_id.id, "product_uom_qty": 24})
        self.make_return(quantity=24)
        self.validate(order.picking_ids, 24)
        line = order.order_line.with_user(self.billing)
        self.assertEqual(physical_facts(line)[line.id], {"delivered": 2.0, "returned": 0.0})

    def test_false_stored_sales_counters_do_not_replace_native_facts(self):
        physical_facts = self.api("physical_facts")
        financial_facts = self.api("financial_facts")
        # Delivered is protected by M2a: construct a legacy cache while policy is
        # disabled, then re-enable it before reading allowances. No business SQL.
        self.company.portfolio_fulfillment_enabled = False
        self.line.with_user(self.sales).write({"qty_delivered": 999, "qty_delivered_method": "manual", "qty_invoiced": 999})
        self.company.portfolio_fulfillment_enabled = True
        self.assertEqual(physical_facts(self.line)[self.line.id]["delivered"], 70)
        self.assertEqual(financial_facts(self.line)[self.line.id]["invoice_reserved"], 70)

    def test_wrong_product_legacy_done_move_cannot_inflate_source_delivery(self):
        physical_facts = self.api("physical_facts")
        order = self.make_sale(quantity=5, company=self.other_company)
        product = self.env["product.product"].create({"name": "Wrong ledger item", "type": "consu", "is_storable": True})
        move = self.env["stock.move"].with_user(self.warehouse).with_company(self.other_company).create({
            "name": "Legacy wrong source product", "company_id": self.other_company.id,
            "product_id": product.id, "product_uom": product.uom_id.id, "product_uom_qty": 5,
            "location_id": self.other_location.id, "location_dest_id": self.customer_location.id,
        })
        move._action_confirm(merge=False)
        move.write({"quantity": 5, "picked": True})
        move._action_done()
        move.write({"sale_line_id": order.order_line.id})
        self.other_company.portfolio_fulfillment_enabled = True
        line = order.order_line.with_user(self.billing)
        self.assertEqual(physical_facts(line)[line.id], {"delivered": 0.0, "returned": 0.0})

    def test_legacy_customer_return_without_genuine_origin_fails_closed(self):
        physical_facts = self.api("physical_facts")
        order = self.make_sale(quantity=5, company=self.other_company)
        move = self.env["stock.move"].with_user(self.warehouse).with_company(self.other_company).create({
            "name": "Legacy unproven customer return", "company_id": self.other_company.id,
            "product_id": self.product.id, "product_uom": self.product.uom_id.id, "product_uom_qty": 5,
            "location_id": self.customer_location.id, "location_dest_id": self.other_location.id,
            "sale_line_id": order.order_line.id, "to_refund": True,
        })
        move._action_confirm(merge=False)
        move.write({"quantity": 5, "picked": True})
        move._action_done()
        self.other_company.portfolio_fulfillment_enabled = True
        with self.assertRaises(UserError):
            physical_facts(order.order_line.with_user(self.billing))

    def test_draft_and_posted_invoice_reservations_have_separate_totals(self):
        financial_facts = self.api("financial_facts")
        facts = financial_facts(self.line)[self.line.id]
        self.assertEqual((facts["invoice_reserved"], facts["invoice_posted"], facts["credit_reserved"]), (70, 0, 0))
        self.invoice.action_post()
        facts = financial_facts(self.line)[self.line.id]
        self.assertEqual((facts["invoice_reserved"], facts["invoice_posted"], facts["signed_reserved"]), (70, 70, 70))

    def test_cancelled_normal_invoice_releases_reservation(self):
        financial_facts = self.api("financial_facts")
        self.invoice.button_cancel()
        facts = financial_facts(self.line)[self.line.id]
        self.assertEqual((facts["invoice_reserved"], facts["invoice_posted"], facts["signed_reserved"]), (0, 0, 0))

    def test_draft_posted_and_cancelled_credit_keep_gross_and_signed_facts_separate(self):
        financial_facts = self.api("financial_facts")
        self.invoice.action_post()
        self.make_return()
        credit = self.make_invoice(10, original=self.invoice)
        facts = financial_facts(self.line)[self.line.id]
        self.assertEqual((facts["invoice_reserved"], facts["credit_reserved"], facts["credit_posted"], facts["signed_reserved"]), (70, 10, 0, 60))
        credit.action_post()
        facts = financial_facts(self.line)[self.line.id]
        self.assertEqual((facts["credit_reserved"], facts["credit_posted"], facts["signed_posted"]), (10, 10, 60))
        credit.button_cancel()
        facts = financial_facts(self.line)[self.line.id]
        self.assertEqual((facts["credit_reserved"], facts["credit_posted"], facts["signed_reserved"]), (0, 0, 70))

    def test_original_invoice_budgets_remain_separate_for_same_source(self):
        financial_facts = self.api("financial_facts")
        self.invoice.button_cancel()
        first = self.make_invoice(40)
        second = self.make_invoice(30)
        (first | second).action_post()
        self.make_return()
        self.make_invoice(4, original=first)
        posted_credit = self.make_invoice(6, original=second)
        posted_credit.action_post()
        facts = financial_facts(self.line)[self.line.id]
        self.assertEqual((facts["invoice_reserved"], facts["credit_reserved"]), (70, 10))
        self.assertEqual((facts["originals"][first.id]["invoice_posted"], facts["originals"][first.id]["credit_reserved"]), (40, 4))
        self.assertEqual((facts["originals"][second.id]["invoice_posted"], facts["originals"][second.id]["credit_posted"]), (30, 6))

    def test_credit_exposes_draft_or_cancelled_original_consistency(self):
        financial_facts = self.api("financial_facts")
        with self.legacy_fixture():
            credit = self.make_invoice(1, original=self.invoice)
        facts = financial_facts(self.line)[self.line.id]
        self.assertEqual(facts["originals"][self.invoice.id]["state"], "draft")
        self.assertIn(credit.invoice_line_ids.id, facts["invalid_line_ids"])
        with self.legacy_fixture():
            self.invoice.button_cancel()
        facts = financial_facts(self.line)[self.line.id]
        self.assertEqual(facts["originals"][self.invoice.id]["state"], "cancel")
        self.assertEqual(facts["originals"][self.invoice.id]["invoice_posted"], 0)
        self.assertIn(credit.invoice_line_ids.id, facts["invalid_line_ids"])

    def test_credit_against_other_source_has_no_original_invoice_budget(self):
        financial_facts = self.api("financial_facts")
        other = self.make_sale(quantity=1).order_line.with_user(self.billing)
        with self.legacy_fixture():
            original = self.make_invoice(1, line=other)
            original.action_post()
            credit = self.make_invoice(1, original=original)
        facts = financial_facts(self.line)[self.line.id]
        self.assertEqual(facts["originals"][original.id]["invoice_posted"], 0)
        self.assertIn(credit.invoice_line_ids.id, facts["invalid_line_ids"])

    def test_invoice_and_credit_uom_conversions_share_source_units(self):
        financial_facts = self.api("financial_facts")
        self.invoice.button_cancel()
        original = self.make_invoice(2, uom=self.uom_dozen)
        original.action_post()
        self.make_return(quantity=12)
        self.make_invoice(1, original=original, uom=self.uom_dozen)
        facts = financial_facts(self.line)[self.line.id]
        self.assertEqual((facts["invoice_reserved"], facts["credit_reserved"], facts["signed_reserved"]), (24, 12, 12))

    def test_multi_source_invoice_keeps_distinct_product_lines(self):
        financial_facts = self.api("financial_facts")
        self.invoice.button_cancel()
        other_order = self.make_sale(quantity=2)
        self.validate(self.receipt_backorder, 2)
        self.validate(other_order.picking_ids, 2)
        other = other_order.order_line.with_user(self.billing)
        invoice = self.make_invoice(3)
        data = other._prepare_invoice_line(quantity=2)
        invoice.write({"invoice_line_ids": [Command.create(data)]})
        lines = self.line | other
        facts = financial_facts(lines)
        self.assertEqual(facts[self.line.id]["invoice_reserved"], 3)
        self.assertEqual(facts[other.id]["invoice_reserved"], 2)

    def test_ambiguous_multi_source_aml_never_duplicates_one_quantity(self):
        financial_facts = self.api("financial_facts")
        self.invoice.button_cancel()
        other = self.make_sale(quantity=2).order_line.with_user(self.billing)
        invoice = self.make_invoice(5)
        with self.legacy_fixture():
            invoice.invoice_line_ids.write({"sale_line_ids": [Command.set((self.line | other).ids)]})
        facts = financial_facts(self.line | other)
        for line in self.line | other:
            self.assertEqual(facts[line.id]["invoice_reserved"], 0)
            self.assertIn(invoice.invoice_line_ids.id, facts[line.id]["invalid_line_ids"])

    def test_wrong_product_aml_is_not_a_source_quantity(self):
        financial_facts = self.api("financial_facts")
        self.invoice.button_cancel()
        product = self.env["product.product"].create({"name": "Wrong invoice item", "type": "consu"})
        with self.legacy_fixture():
            invoice = self.make_invoice(999)
            invoice.invoice_line_ids.write({"product_id": product.id, "tax_ids": [Command.clear()]})
        facts = financial_facts(self.line)[self.line.id]
        self.assertEqual(facts["invoice_reserved"], 0)
        self.assertIn(invoice.invoice_line_ids.id, facts["invalid_line_ids"])

    def test_source_global_and_original_credit_budgets_are_independent(self):
        financial_facts = self.api("financial_facts")
        physical_facts = self.api("physical_facts")
        self.invoice.button_cancel()
        first = self.make_invoice(5)
        second = self.make_invoice(65)
        (first | second).action_post()
        self.make_return()
        with self.legacy_fixture():
            self.make_invoice(6, original=first)
        facts = financial_facts(self.line)[self.line.id]
        self.assertEqual(physical_facts(self.line)[self.line.id]["returned"] - facts["credit_reserved"], 4)
        budget = facts["originals"][first.id]
        self.assertEqual(budget["invoice_posted"] - budget["credit_reserved"], -1)

    def test_source_read_rules_are_checked_under_sudo_before_elevated_facts(self):
        for name in ("physical_facts", "financial_facts", "lock_financial"):
            helper = self.api(name)
            with self.subTest(helper=name):
                hidden = self.line.with_user(self.other_sales).sudo()
                self.assert_denied_before_revision(lambda: helper(hidden))

    def test_actual_company_membership_cannot_be_forged_under_sudo(self):
        other = self.make_sale(quantity=1, company=self.other_company).order_line
        for name in ("physical_facts", "financial_facts", "lock_financial"):
            helper = self.api(name)
            with self.subTest(helper=name):
                forged = other.with_user(self.billing_a).sudo().with_context(allowed_company_ids=self.companies.ids)
                self.assert_denied_before_revision(lambda: helper(forged))

    def test_inactive_company_is_denied_even_to_real_member_under_sudo(self):
        helper = self.api("lock_financial")
        other = self.make_sale(quantity=1, company=self.other_company).order_line
        inactive = other.with_user(self.billing).sudo().with_context(allowed_company_ids=self.company.ids)
        self.assert_denied_before_revision(lambda: helper(inactive))

    def test_billing_role_is_exact_original_uid_even_under_sudo(self):
        require_billing = self.api("require_billing")
        require_billing(self.line.env, self.company.with_env(self.line.env))
        for actor in (self.sales, self.manager, self.buyer, self.warehouse, self.settings_only):
            with self.subTest(actor=actor.login):
                model = self.env["account.move"].with_user(actor).sudo()
                self.assert_denied_before_revision(lambda: require_billing(model.env, self.company.with_env(model.env)))

    def test_billing_actual_membership_and_active_companies_are_required(self):
        require_billing = self.api("require_billing")
        forged = self.env["account.move"].with_user(self.billing_a).sudo().with_context(allowed_company_ids=self.companies.ids)
        self.assert_denied_before_revision(lambda: require_billing(forged.env, self.other_company.with_env(forged.env)))
        inactive = self.env["account.move"].with_user(self.billing).sudo().with_context(allowed_company_ids=self.company.ids)
        self.assert_denied_before_revision(lambda: require_billing(inactive.env, self.other_company.with_env(inactive.env)))

    def test_financial_lock_uses_shared_revision_without_sales_business_write(self):
        lock_financial = self.api("lock_financial")
        self.assertFalse(self.billing.has_group("sales_team.group_sale_salesman"))
        with self.assertRaises(AccessError):
            self.source.with_user(self.billing).write({"note": "No sales business write"})
        before = self.company.portfolio_stock_revision
        lock_financial(self.line)
        self.company.invalidate_recordset(["portfolio_stock_revision"])
        self.assertEqual(self.company.portfolio_stock_revision, before + 1)

    def test_financial_lock_authorizes_candidate_account_company_before_sql(self):
        lock_financial = self.api("lock_financial")
        lines = self.line.with_user(self.billing_a).sudo().with_context(allowed_company_ids=self.companies.ids)
        self.assert_denied_before_revision(lambda: lock_financial(lines, extra_companies=self.other_company))

    def test_cross_company_account_identity_is_authorized_before_source_lock(self):
        lock_financial = self.api("lock_financial")
        financial_facts = self.api("financial_facts")
        with self.legacy_fixture():
            invoice = self.make_invoice(9, company=self.other_company)
        facts = financial_facts(self.line)[self.line.id]
        self.assertEqual(facts["invoice_reserved"], 70)
        self.assertIn(invoice.invoice_line_ids.id, facts["invalid_line_ids"])
        lines = self.line.with_user(self.billing_a).sudo().with_context(allowed_company_ids=self.companies.ids)
        self.assert_denied_before_revision(lambda: lock_financial(lines))

    def test_service_source_has_no_physical_budget_but_still_requires_billing(self):
        physical_facts = self.api("physical_facts")
        financial_facts = self.api("financial_facts")
        require_billing = self.api("require_billing")
        service = self.env["product.product"].create({
            "name": "Ledger service", "default_code": "LEDGER-SVC", "type": "service", "invoice_policy": "order",
            "taxes_id": [Command.clear()],
        })
        line = self.make_sale(quantity=1, product=service).order_line.with_user(self.billing)
        self.assertEqual(physical_facts(line), {})
        self.assertEqual(financial_facts(line), {})
        require_billing(line.env, line.company_id)
        sales_env = line.with_user(self.sales).sudo().env
        self.assert_denied_before_revision(lambda: require_billing(sales_env, line.company_id.with_env(sales_env)))
