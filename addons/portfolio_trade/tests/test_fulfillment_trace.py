"""Native read-only fulfillment trace acceptance tests."""
import unittest

from lxml import etree
from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import new_test_user, tagged

from .test_fulfillment_ledger import FulfillmentLedgerCommon


@tagged("post_install", "-at_install", "portfolio_fulfillment_trace")
class TestFulfillmentTrace(FulfillmentLedgerCommon):
    """Exercise the trace through native users and source documents."""

    trace_model_name = "portfolio.fulfillment.trace"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.trace_sales_b = new_test_user(
            cls.env, login="trace_parts_b_sales", groups="sales_team.group_sale_salesman",
            company_id=cls.other_company.id, company_ids=[Command.set(cls.other_company.ids)],
        )
        cls.trace_audit = new_test_user(
            cls.env, login="trace_audit", groups=(
                "sales_team.group_sale_salesman,purchase.group_purchase_user,"
                "stock.group_stock_user,account.group_account_invoice"
            ), company_id=cls.company.id, company_ids=[Command.set(cls.company.ids)],
        )

    def trace_model(self, actor=None):
        """Keep the missing-model assertion explicit for the frozen RED run."""
        self.assertTrue(self.trace_model_name in self.env.registry.models, "Missing fulfillment trace model")
        return self.env[self.trace_model_name].with_user(actor or self.billing).with_context(
            allowed_company_ids=self.company.ids,
        ).with_company(self.company)

    def trace_action(self, actor=None, source=None):
        source = (source or self.source).with_user(actor or self.billing).with_context(
            allowed_company_ids=self.company.ids,
        ).with_company(self.company)
        action = getattr(source, "action_portfolio_trace", None)
        self.assertTrue(callable(action), "Missing sale.order.action_portfolio_trace")
        result = action()
        self.assertEqual(result.get("type"), "ir.actions.act_window")
        self.assertEqual(result.get("res_model"), self.trace_model_name)
        self.assertTrue(result.get("res_id"), "Trace action must open a transient snapshot")
        trace = self.trace_model(actor).browse(result["res_id"])
        self.assertTrue(trace.exists())
        return result, trace

    def create_credit(self, quantity, original_line):
        wizard = self.env["portfolio.partial.credit"].with_user(self.billing).with_company(self.company).create({
            "original_line_id": original_line.id, "quantity": quantity,
        })
        action = wizard.action_create_credit()
        return self.env["account.move"].with_user(self.billing).browse(action["res_id"])

    def complete_fixture_with_posted_original_and_draft_followup(self):
        """Complete both native receipt/delivery backorders and a partial return."""
        self.validate(self.receipt_backorder, 30)
        self.validate(self.delivery_backorder, 30)
        self.invoice.action_post()
        # Reserve the second invoice while all 100 units are delivered; the
        # subsequent physical return then leaves 90 net delivery and 30 draft
        # invoice quantity available for the trace snapshot.
        draft_followup = self.make_invoice(30)
        returned = self.make_return(10)
        original_line = self.invoice.invoice_line_ids.filtered(
            lambda entry: entry.display_type == "product",
        )
        credit = self.create_credit(10, original_line=original_line)
        credit.action_post()
        return returned, draft_followup, credit

    def business_snapshot(self):
        self.env.flush_all()
        self.env.invalidate_all()
        moves = self.env["account.move"].search([("company_id", "in", self.companies.ids)], order="id")
        stock = self.env["stock.move"].search([("company_id", "in", self.companies.ids)], order="id")
        orders = self.env["sale.order"].search([("company_id", "in", self.companies.ids)], order="id")
        return (
            tuple((company.id, company.portfolio_stock_revision) for company in self.companies),
            tuple((order.id, order.state, order.portfolio_revision, tuple(order.order_line.ids)) for order in orders),
            tuple((move.id, move.state, move.move_type, move.reversed_entry_id.id,
                   tuple(move.line_ids.ids)) for move in moves),
            tuple((entry.id, entry.quantity, entry.balance, tuple(entry.sale_line_ids.ids))
                  for entry in moves.line_ids.sorted("id")),
            tuple((move.id, move.state, move.quantity, tuple(move.move_line_ids.ids)) for move in stock),
        )

    def assert_atomic_denial(self, operation):
        model = self.trace_model()
        before = self.business_snapshot()
        trace_count = model.search_count([])
        with unittest.TestCase.assertRaises(self, AccessError):
            operation()
        self.assertEqual(self.business_snapshot(), before)
        self.assertEqual(model.search_count([]), trace_count)

    def test_trace_registry_and_sale_action_are_explicit_api(self):
        """The RED run must fail for the missing model/action, not a fixture typo."""
        self.assertTrue(self.trace_model_name in self.env.registry.models, "Missing fulfillment trace model")
        self.assertTrue("portfolio.fulfillment.trace.line" in self.env.registry.models, "Missing fulfillment trace line model")
        self.assertTrue(callable(getattr(self.source, "action_portfolio_trace", None)))

    def test_confirmed_order_trace_lists_native_sources_and_signed_posted_facts(self):
        returned, draft_followup, credit = self.complete_fixture_with_posted_original_and_draft_followup()
        _action, trace = self.trace_action(self.trace_audit)

        self.assertEqual(trace.sale_order_id, self.source)
        self.assertEqual(trace.purchase_order_ids, self.purchase)
        self.assertEqual(trace.receipt_picking_ids, self.receipt | self.receipt_backorder)
        self.assertEqual(trace.delivery_picking_ids, self.delivery | self.delivery_backorder)
        self.assertIn(returned, trace.return_picking_ids)
        self.assertEqual(trace.backorder_picking_ids, self.receipt_backorder | self.delivery_backorder)
        self.assertEqual(trace.gross_delivered_qty, 100)
        self.assertEqual(trace.returned_qty, 10)
        self.assertEqual(trace.net_delivered_qty, 90)
        self.assertEqual(trace.draft_invoice_qty, 30)
        self.assertEqual(trace.posted_invoice_qty, 70)
        self.assertEqual(trace.draft_credit_qty, 0)
        self.assertEqual(trace.posted_credit_qty, 10)
        # Actual posted product AML balance, not account.move.amount_total and
        # not a repeated whole-invoice total: 5,950 - 850 = 5,100 CNY.
        self.assertEqual(trace.posted_signed_amount, 5100)
        self.assertFalse(trace.has_mixed_uom)
        self.assertEqual(trace.line_ids.source_line_id, self.line)
        self.assertEqual(trace.line_ids.product_id, self.product)
        self.assertEqual(trace.line_ids.product_uom_id, self.product.uom_id)
        self.assertEqual(trace.line_ids.gross_delivered_qty, 100)
        self.assertEqual(trace.line_ids.returned_qty, 10)
        self.assertEqual(trace.line_ids.net_delivered_qty, 90)
        self.assertEqual(trace.line_ids.posted_signed_amount, 5100)
        self.assertEqual(draft_followup.state, "draft")
        self.assertEqual(credit.state, "posted")

    def test_trace_form_is_readonly_and_loads_through_get_view_and_web_read(self):
        _action, trace = self.trace_action(self.billing)
        view = trace.get_view(view_type="form")
        arch = etree.fromstring(view["arch"].encode())
        self.assertTrue(arch.xpath("//form"), "Trace must provide a form view")
        fields = arch.xpath("//field")
        self.assertTrue(fields, "Trace form must expose snapshot fields")
        for node in fields:
            self.assertEqual(node.get("readonly"), "1", f"Trace field is writable: {node.get('name')}")
        specification = {node.get("name"): {} for node in fields if node.get("name") in trace._fields}
        result = trace.web_read(specification)
        self.assertEqual(result[0]["id"], trace.id)
        self.assertEqual(result[0]["sale_order_id"], self.source.id)

    def test_sales_buyer_and_billing_can_open_trace_with_their_native_read_rights(self):
        for actor in (self.sales, self.buyer, self.billing):
            with self.subTest(actor=actor.login):
                _action, trace = self.trace_action(actor)
                self.assertEqual(trace.sale_order_id, self.source.with_user(actor))
                self.assertTrue(trace.with_user(actor).read(["sale_order_id"]))

    def test_trace_does_not_cross_company_for_part_b_actor(self):
        # A PARTS-B Sales actor cannot receive the PARTS-A source snapshot even
        # when a forged allowed-company context contains both companies.
        self.trace_model(self.trace_sales_b)
        action = getattr(self.source, "action_portfolio_trace", None)
        self.assertTrue(callable(action), "Missing sale.order.action_portfolio_trace")
        self.assert_atomic_denial(lambda: self.source.with_user(self.trace_sales_b).sudo().with_context(
            allowed_company_ids=self.companies.ids,
        ).action_portfolio_trace())

    def test_service_only_order_has_no_physical_budget_or_stock_sources(self):
        service = self.env["product.product"].with_company(self.company).create({
            "name": "Trace service", "default_code": "TRACE-SVC", "type": "service",
            "invoice_policy": "order", "taxes_id": [Command.clear()],
            "property_account_income_id": self.company_data["default_account_revenue"].id,
        })
        order = self.make_sale(quantity=3, product=service)
        _action, trace = self.trace_action(self.sales, order)
        self.assertEqual(trace.sale_order_id, order)
        self.assertFalse(trace.purchase_order_ids)
        self.assertFalse(trace.receipt_picking_ids | trace.delivery_picking_ids | trace.return_picking_ids)
        self.assertEqual(trace.gross_delivered_qty, 0)
        self.assertEqual(trace.returned_qty, 0)
        self.assertEqual(trace.net_delivered_qty, 0)
        self.assertFalse(trace.line_ids)

    def test_multi_sale_invoice_attributes_each_product_aml_once_to_its_source(self):
        self.invoice.button_cancel()
        self.make_return(10)
        second = self.make_sale(quantity=10)
        self.validate(second.picking_ids, 10)
        # One native invoice contains two source-linked product AMLs. The trace
        # for each SO receives only its own quantity and signed AML balance.
        invoice = self.env["account.move"].with_user(self.billing).with_company(self.company).create({
            "move_type": "out_invoice", "company_id": self.company.id,
            "partner_id": self.customer.id, "invoice_date": fields.Date.today(),
            "journal_id": self.company_data["default_journal_sale"].id,
            "invoice_line_ids": [
                Command.create({
                    "product_id": self.product.id, "product_uom_id": self.product.uom_id.id,
                    "account_id": self.company_data["default_account_revenue"].id,
                    "quantity": 7, "price_unit": 100, "discount": 15,
                    "tax_ids": [Command.clear()], "sale_line_ids": [Command.set(self.line.ids)],
                }),
                Command.create({
                    "product_id": self.product.id, "product_uom_id": self.product.uom_id.id,
                    "account_id": self.company_data["default_account_revenue"].id,
                    "quantity": 3, "price_unit": 100, "discount": 15,
                    "tax_ids": [Command.clear()], "sale_line_ids": [Command.set(second.order_line.ids)],
                }),
            ],
        })
        invoice.action_post()
        _action_a, trace_a = self.trace_action(self.billing, self.source)
        _action_b, trace_b = self.trace_action(self.billing, second)
        self.assertEqual(trace_a.posted_invoice_qty, 7)
        self.assertEqual(trace_b.posted_invoice_qty, 3)
        self.assertEqual(trace_a.posted_signed_amount, 595)
        self.assertEqual(trace_b.posted_signed_amount, 255)
        self.assertEqual(trace_a.posted_signed_amount + trace_b.posted_signed_amount, invoice.amount_untaxed)

    def test_trace_authorizes_source_before_elevated_detail_and_is_atomic_on_denial(self):
        self.trace_model()
        self.env["ir.rule"].create({
            "name": "Trace test deny source", "model_id": self.env["ir.model"]._get_id("sale.order"),
            "domain_force": repr([("id", "!=", self.source.id)]),
        })
        self.assert_atomic_denial(lambda: self.source.with_user(self.billing).sudo().action_portfolio_trace())

    def test_trace_rejects_business_write_and_extra_inputs(self):
        _action, trace = self.trace_action(self.billing)
        values = {"gross_delivered_qty": 999, "net_delivered_qty": 999, "posted_signed_amount": 1}
        with self.assertRaises(AccessError):
            trace.with_user(self.billing).write(values)
        with self.assertRaises(AccessError):
            trace.with_user(self.billing).unlink()
        with self.assertRaises(AccessError):
            self.trace_model().create({"sale_order_id": self.source.id, **values})

    def test_trace_acl_allows_only_sales_purchase_billing_read_and_snapshot_creation(self):
        model = self.trace_model()
        for actor in (self.sales, self.manager, self.buyer, self.billing):
            for operation in ("read", "create"):
                with self.subTest(actor=actor.login, operation=operation):
                    self.assertTrue(model.with_user(actor).has_access(operation))
            for operation in ("write", "unlink"):
                with self.subTest(actor=actor.login, operation=operation):
                    self.assertFalse(model.with_user(actor).has_access(operation))
        for actor in (self.warehouse, self.settings_only):
            for operation in ("read", "write", "create", "unlink"):
                with self.subTest(actor=actor.login, operation=operation):
                    self.assertFalse(model.with_user(actor).has_access(operation))

    def test_trace_rejects_warehouse_and_settings_only_actors_even_in_sudo(self):
        self.trace_model()
        for actor in (self.warehouse, self.settings_only):
            with self.subTest(actor=actor.login):
                self.assert_atomic_denial(lambda: self.source.with_user(actor).sudo().action_portfolio_trace())

    def test_trace_rejects_inactive_company_for_an_actual_member(self):
        self.trace_model()
        self.assert_atomic_denial(lambda: self.source.with_user(self.billing).sudo().with_context(
            allowed_company_ids=self.other_company.ids,
        ).action_portfolio_trace())

    def test_trace_rechecks_source_read_rules_when_reading_a_snapshot(self):
        _action, trace = self.trace_action(self.billing)
        self.env["ir.rule"].create({
            "name": "Trace snapshot deny source", "model_id": self.env["ir.model"]._get_id("sale.order"),
            "domain_force": repr([("id", "!=", self.source.id)]),
        })
        with self.assertRaises(AccessError):
            trace.sudo().read(["gross_delivered_qty", "posted_signed_amount"])

    def test_trace_authorizes_source_line_read_rules_before_elevated_facts(self):
        self.trace_model()
        self.env["ir.rule"].create({
            "name": "Trace deny source line", "model_id": self.env["ir.model"]._get_id("sale.order.line"),
            "domain_force": repr([("id", "!=", self.line.id)]),
        })
        self.assert_atomic_denial(lambda: self.source.with_user(self.billing).sudo().action_portfolio_trace())

    def test_snapshot_creation_and_read_leave_all_business_facts_unchanged(self):
        before = self.business_snapshot()
        _action, trace = self.trace_action(self.billing)
        trace.read(["sale_order_id", "gross_delivered_qty", "posted_signed_amount"])
        self.assertEqual(self.business_snapshot(), before)

    def test_snapshot_ignores_forged_display_defaults_from_context(self):
        model = self.trace_model().with_context(
            default_sale_order_id=self.source.id, default_gross_delivered_qty=999,
            default_posted_signed_amount=999, default_company_id=self.other_company.id,
            accrual_entry_date="2000-01-01", to_date="2000-01-01",
        )
        trace = model.create({})
        self.assertEqual(trace.sale_order_id, self.source)
        self.assertEqual(trace.company_id, self.company)
        self.assertEqual(trace.gross_delivered_qty, 70)
        self.assertEqual(trace.posted_signed_amount, 0)

    def test_default_get_derives_readonly_snapshot_from_its_sale_order_input(self):
        model = self.trace_model().with_context(default_sale_order_id=self.source.id)
        values = model.default_get(["sale_order_id", "gross_delivered_qty", "draft_invoice_qty"])
        self.assertEqual(values["sale_order_id"], self.source.id)
        self.assertEqual(values["gross_delivered_qty"], 70)
        self.assertEqual(values["draft_invoice_qty"], 70)

    def test_trace_button_groups_follow_source_read_roles(self):
        self.trace_model()
        for actor in (self.sales, self.buyer, self.billing):
            with self.subTest(actor=actor.login):
                view = self.source.with_user(actor).get_view(
                    view_id=self.env.ref("sale.view_order_form").id, view_type="form",
                )
                self.assertTrue(etree.fromstring(view["arch"].encode()).xpath(
                    ".//button[@name='action_portfolio_trace']",
                ))

    def test_service_posted_amount_is_native_but_never_creates_physical_quantity(self):
        self.trace_model()
        service = self.env["product.product"].with_company(self.company).create({
            "name": "Trace billed service", "default_code": "TRACE-BILLED-SVC", "type": "service",
            "invoice_policy": "order", "taxes_id": [Command.clear()],
            "property_account_income_id": self.company_data["default_account_revenue"].id,
        })
        order = self.make_sale(quantity=3, product=service)
        invoice = self.make_invoice(3, line=order.order_line)
        invoice.action_post()
        _action, trace = self.trace_action(self.billing, order)
        self.assertEqual(trace.gross_delivered_qty, 0)
        self.assertEqual(trace.posted_invoice_qty, 0)
        self.assertEqual(trace.posted_signed_amount, 255)

    def test_trace_fails_closed_for_ambiguous_multi_source_product_aml(self):
        self.trace_model()
        second = self.make_sale(quantity=10)
        with self.legacy_fixture():
            self.invoice.invoice_line_ids.write({"sale_line_ids": [Command.set((self.line | second.order_line).ids)]})
        before = self.business_snapshot()
        with self.assertRaises(UserError):
            self.trace_action(self.billing)
        self.assertEqual(self.business_snapshot(), before)

    def test_financial_payload_and_fields_require_actual_billing_even_in_sudo(self):
        self.invoice.action_post()
        for actor in (self.sales, self.buyer):
            with self.subTest(actor=actor.login):
                _action, trace = self.trace_action(actor)
                view = trace.get_view(view_type="form")
                arch = etree.fromstring(view["arch"].encode())
                for name in ("draft_invoice_qty", "posted_invoice_qty", "posted_signed_amount", "invoice_ids"):
                    self.assertFalse(arch.xpath(f".//field[@name='{name}']"))
                with self.assertRaises(AccessError):
                    trace.sudo().read(["posted_signed_amount"])
                with self.assertRaises(AccessError):
                    trace.line_ids.sudo().read(["posted_signed_amount"])

    def test_document_lists_obey_actual_native_acl_and_record_rules(self):
        for actor in (self.sales, self.buyer, self.billing):
            with self.subTest(actor=actor.login):
                _action, trace = self.trace_action(actor)
                for field_name, model_name in (
                    ("purchase_order_ids", "purchase.order"),
                    ("receipt_picking_ids", "stock.picking"),
                    ("delivery_picking_ids", "stock.picking"),
                ):
                    model = self.env[model_name].with_user(actor)
                    records = trace[field_name]
                    if model.has_access("read"):
                        records.check_access("read")
                    else:
                        self.assertFalse(records)
        self.env["ir.rule"].create({
            "name": "Trace hide purchase", "model_id": self.env["ir.model"]._get_id("purchase.order"),
            "domain_force": repr([("id", "!=", self.purchase.id)]),
        })
        _action, trace = self.trace_action(self.buyer)
        self.assertNotIn(self.purchase, trace.purchase_order_ids)

    def test_mixed_uom_preserves_per_source_quantities_and_hides_header_totals(self):
        self.trace_model()
        self.make_return(13)
        order = self.make_sale(quantity=1, confirm=False)
        order.write({"order_line": [Command.create({
            "product_id": self.product.id, "product_uom": self.uom_dozen.id,
            "product_uom_qty": 1, "price_unit": 1200, "tax_id": [Command.clear()],
        })]})
        order.action_portfolio_submit()
        order.with_user(self.manager).action_portfolio_approve()
        order.action_confirm()
        for move in order.picking_ids.move_ids.with_user(self.warehouse):
            move.write({"quantity": move.product_uom_qty, "picked": True})
        order.picking_ids.with_user(self.warehouse).button_validate()
        _action, trace = self.trace_action(self.billing, order)
        self.assertTrue(trace.has_mixed_uom)
        self.assertEqual(trace.gross_delivered_qty, 0)
        self.assertEqual(len(trace.line_ids), 2)
        self.assertEqual(set(trace.line_ids.product_uom_id.ids), {self.product.uom_id.id, self.uom_dozen.id})
        self.assertEqual(trace.line_ids.mapped("gross_delivered_qty"), [1, 1])
        arch = etree.fromstring(trace.get_view(view_type="form")["arch"].encode())
        self.assertTrue(arch.xpath(".//group[@invisible='has_mixed_uom']"))

    def test_snapshot_children_reject_direct_input_and_recheck_source_rules(self):
        _action, trace = self.trace_action(self.billing)
        self.assertEqual(len(trace.line_ids), 1)
        child = trace.line_ids
        with self.assertRaises(AccessError):
            child.write({"gross_delivered_qty": 999})
        with self.assertRaises(AccessError):
            self.env["portfolio.fulfillment.trace.line"].with_user(self.billing).create({
                "trace_id": trace.id, "source_line_id": self.line.id, "gross_delivered_qty": 999,
            })
        self.env["ir.rule"].create({
            "name": "Trace child deny source", "model_id": self.env["ir.model"]._get_id("sale.order"),
            "domain_force": repr([("id", "!=", self.source.id)]),
        })
        with self.assertRaises(AccessError):
            child.sudo().read(["gross_delivered_qty"])

    def test_nested_return_provenance_rejects_hidden_original_delivery_move(self):
        returned = self.make_return(10)
        original_moves = self.delivery.move_ids
        _action, existing = self.trace_action(self.trace_audit)
        self.env["ir.rule"].create({
            "name": "Trace hide original delivery move",
            "model_id": self.env["ir.model"]._get_id("stock.move"),
            "domain_force": repr([("id", "not in", original_moves.ids)]),
        })
        actor_moves = self.env["stock.move"].with_user(self.trace_audit).with_context(
            allowed_company_ids=self.company.ids,
        )
        self.assertFalse(actor_moves.browse(original_moves.ids).has_access("read"))
        self.assertTrue(actor_moves.browse(returned.move_ids.ids).has_access("read"))
        before = self.business_snapshot()
        trace_count = self.env[self.trace_model_name].sudo().search_count([])
        specification = {"return_move_ids": {"fields": {
            "origin_returned_move_id": {"fields": {"display_name": {}}},
        }}}
        with unittest.TestCase.assertRaises(self, AccessError):
            _action, trace = self.trace_action(self.trace_audit)
            trace.web_read(specification)
        self.assertEqual(self.env[self.trace_model_name].sudo().search_count([]), trace_count)
        with unittest.TestCase.assertRaises(self, AccessError):
            existing.sudo().web_read(specification)
        self.assertEqual(self.business_snapshot(), before)

    def test_nested_picking_provenance_rejects_hidden_return_and_backorder_origin(self):
        returned = self.make_return(10)
        _action, existing = self.trace_action(self.trace_audit)
        self.env["ir.rule"].create({
            "name": "Trace hide original delivery picking",
            "model_id": self.env["ir.model"]._get_id("stock.picking"),
            "domain_force": repr([("id", "!=", self.delivery.id)]),
        })
        actor_pickings = self.env["stock.picking"].with_user(self.trace_audit).with_context(
            allowed_company_ids=self.company.ids,
        )
        self.assertFalse(actor_pickings.browse(self.delivery.id).has_access("read"))
        self.assertTrue(actor_pickings.browse(returned.id).has_access("read"))
        self.assertTrue(actor_pickings.browse(self.delivery_backorder.id).has_access("read"))
        before = self.business_snapshot()
        trace_count = self.env[self.trace_model_name].sudo().search_count([])
        specification = {
            "return_picking_ids": {"fields": {
                "return_id": {"fields": {"display_name": {}}},
            }},
            "backorder_picking_ids": {"fields": {
                "backorder_id": {"fields": {"display_name": {}}},
            }},
        }
        with unittest.TestCase.assertRaises(self, AccessError):
            _action, trace = self.trace_action(self.trace_audit)
            trace.web_read(specification)
        self.assertEqual(self.env[self.trace_model_name].sudo().search_count([]), trace_count)
        with unittest.TestCase.assertRaises(self, AccessError):
            existing.sudo().web_read(specification)
        self.assertEqual(self.business_snapshot(), before)

    def revoke_posted_trace_source(self):
        self.invoice.action_post()
        _action, trace = self.trace_action(self.billing)
        children = trace.line_ids
        self.assertEqual(trace.posted_signed_amount, 5950)
        self.assertEqual(len(children), 1)
        self.assertEqual([record_id for record_id, _name in trace.name_search(args=[("id", "=", trace.id)])], [trace.id])
        self.assertEqual(trace.search_count([("id", "=", trace.id), ("posted_signed_amount", "=", 5950)]), 1)
        self.env["ir.rule"].create({
            "name": "Trace revoke posted source",
            "model_id": self.env["ir.model"]._get_id("sale.order"),
            "domain_force": repr([("id", "!=", self.source.id)]),
        })
        self.assertFalse(self.source.with_user(self.billing).with_context(
            allowed_company_ids=self.company.ids,
        ).has_access("read"))
        return trace, children

    def test_copy_data_rechecks_revoked_source_for_parent_and_child_snapshots(self):
        trace, children = self.revoke_posted_trace_source()
        before = self.business_snapshot()
        for records in (trace, children):
            for elevated in (False, True):
                with self.subTest(model=records._name, elevated=elevated):
                    target = records.sudo() if elevated else records
                    with unittest.TestCase.assertRaises(self, AccessError):
                        target.copy_data()
        self.assertEqual(self.business_snapshot(), before)

    def test_name_search_does_not_disclose_snapshot_after_source_access_is_revoked(self):
        trace, _children = self.revoke_posted_trace_source()
        before = self.business_snapshot()
        for elevated in (False, True):
            with self.subTest(elevated=elevated):
                target = trace.sudo() if elevated else trace
                try:
                    result = target.name_search(name="", args=[("id", "=", trace.id)])
                except AccessError:
                    continue
                self.assertEqual(result, [], "Source revocation must also hide the trace display name")
        self.assertEqual(self.business_snapshot(), before)

    def test_search_count_cannot_probe_financial_snapshot_after_source_access_is_revoked(self):
        trace, _children = self.revoke_posted_trace_source()
        before = self.business_snapshot()
        for elevated in (False, True):
            for amount in (5950, 5951):
                with self.subTest(elevated=elevated, amount=amount):
                    target = trace.sudo() if elevated else trace
                    try:
                        count = target.search_count([
                            ("id", "=", trace.id), ("posted_signed_amount", "=", amount),
                        ])
                    except AccessError:
                        continue
                    self.assertEqual(count, 0, "Source revocation must prevent financial predicate probes")
        self.assertEqual(self.business_snapshot(), before)

    def test_return_move_picking_link_rejects_hidden_return_picking(self):
        returned = self.make_return(10)
        _action, existing = self.trace_action(self.trace_audit)
        self.env["ir.rule"].create({
            "name": "Trace hide return picking",
            "model_id": self.env["ir.model"]._get_id("stock.picking"),
            "domain_force": repr([("id", "!=", returned.id)]),
        })
        actor_moves = self.env["stock.move"].with_user(self.trace_audit).with_context(
            allowed_company_ids=self.company.ids,
        )
        actor_pickings = self.env["stock.picking"].with_env(actor_moves.env)
        self.assertTrue(actor_moves.browse(returned.move_ids.ids).has_access("read"))
        self.assertTrue(actor_moves.browse(self.delivery.move_ids.ids).has_access("read"))
        self.assertFalse(actor_pickings.browse(returned.id).has_access("read"))
        self.assertTrue(actor_pickings.browse(self.delivery.id).has_access("read"))
        before = self.business_snapshot()
        trace_count = self.env[self.trace_model_name].sudo().search_count([])
        specification = {"return_move_ids": {"fields": {
            "picking_id": {"fields": {"display_name": {}}},
        }}}
        with unittest.TestCase.assertRaises(self, AccessError):
            _action, trace = self.trace_action(self.trace_audit)
            trace.web_read(specification)
        self.assertEqual(self.env[self.trace_model_name].sudo().search_count([]), trace_count)
        with unittest.TestCase.assertRaises(self, AccessError):
            existing.sudo().web_read(specification)
        self.assertEqual(self.business_snapshot(), before)
