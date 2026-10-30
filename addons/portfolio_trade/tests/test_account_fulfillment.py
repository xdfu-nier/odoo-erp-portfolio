"""Financial mutation boundaries exercised through native account ORM/actions."""
import unittest
from unittest.mock import patch

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import new_test_user, tagged

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

    def posted_append_values(self):
        values = self.invoice_values(1)["invoice_line_ids"][0][2]
        values["price_unit"] = 0
        return values

    def test_public_aml_create_cannot_append_semantic_claim_to_posted_invoice(self):
        self.invoice.invoice_line_ids.write({"quantity": 60})
        self.invoice.action_post()
        with self.env.cr.savepoint():
            before = self.accounting_snapshot()
            with unittest.TestCase.assertRaises(self, UserError):
                self.env["account.move.line"].with_user(self.billing).create({
                    **self.posted_append_values(), "move_id": self.invoice.id,
                })
            self.assertEqual(self.accounting_snapshot(), before)

    def test_default_posted_aml_parent_is_rejected_at_api_preflight(self):
        self.invoice.invoice_line_ids.write({"quantity": 60})
        self.invoice.action_post()
        model = self.env["account.move.line"].with_user(self.billing).with_context(default_move_id=self.invoice.id)
        draft = self.empty_invoice()
        created = model.create({**self.posted_append_values(), "move_id": draft.id})
        self.assertEqual(created.move_id, draft, "An explicit draft parent overrides the posted default.")
        with self.env.cr.savepoint():
            before = self.accounting_snapshot()
            try:
                with unittest.TestCase.assertRaises(self, UserError):
                    model.create(self.posted_append_values())
            except KeyError as error:
                self.fail(f"Protected effective posted parent must fail policy preflight before native requires explicit move_id: {error}")
            self.assertEqual(self.accounting_snapshot(), before)

    def test_false_and_none_relation_forms_cannot_clear_direct_or_nested_source(self):
        for nested in (False, True):
            # Native [] means no commands; an empty tuple/recordset means SET[].
            if nested:
                self.invoice.write({"invoice_line_ids": [Command.update(
                    self.invoice.invoice_line_ids.id, {"sale_line_ids": []},
                )]})
            else:
                self.invoice.invoice_line_ids.write({"sale_line_ids": []})
            self.assertEqual(self.invoice.invoice_line_ids.sale_line_ids, self.line)
            for value in (False, None, (), self.line.browse()):
                with self.subTest(nested=nested, value=value), self.env.cr.savepoint():
                    before = self.accounting_snapshot()
                    with unittest.TestCase.assertRaises(self, UserError):
                        values = {"sale_line_ids": value}
                        if nested:
                            self.invoice.write({"invoice_line_ids": [Command.update(self.invoice.invoice_line_ids.id, values)]})
                        else:
                            self.invoice.invoice_line_ids.write(values)
                    self.assertEqual(self.accounting_snapshot(), before)

    def test_native_relation_forms_authorize_candidate_company_before_revision(self):
        # Native ORM accepts a recordset as SET; retaining the same source is a
        # positive compatibility control before trying foreign-company sources.
        self.invoice.invoice_line_ids.write({"sale_line_ids": self.line})
        self.assertEqual(self.invoice.invoice_line_ids.sale_line_ids, self.line)
        other = self.make_sale(quantity=1, company=self.other_company).order_line
        for value in (other.ids, tuple(other.ids), other):
            with self.subTest(form=type(value).__name__), self.env.cr.savepoint():
                entries = self.invoice.invoice_line_ids.with_user(self.billing_a).sudo().with_context(
                    allowed_company_ids=self.company.ids,
                )
                self.assertEqual(self.billing_a.company_ids, self.company)
                self.assertEqual(entries.with_user(self.billing_a).env.companies, self.company)
                self.api("require_billing")(entries.env, self.company.with_env(entries.env))
                before = self.accounting_snapshot()
                self.assert_denied_before_revision(lambda: entries.write({"sale_line_ids": value}))
                self.assertEqual(self.accounting_snapshot(), before)

    def test_draft_aml_cannot_move_into_posted_parent_directly_or_nested(self):
        self.invoice.invoice_line_ids.write({"quantity": 60})
        self.invoice.action_post()
        draft = self.make_invoice(1)
        draft.invoice_line_ids.write({"price_unit": 0})
        for nested in (False, True):
            with self.subTest(nested=nested), self.env.cr.savepoint():
                before = self.accounting_snapshot()
                with unittest.TestCase.assertRaises(self, UserError):
                    values = {"move_id": self.invoice.id}
                    if nested:
                        draft.write({"invoice_line_ids": [Command.update(draft.invoice_line_ids.id, values)]})
                    else:
                        draft.invoice_line_ids.write(values)
                self.assertEqual(self.accounting_snapshot(), before)

    def test_nested_effective_account_default_authorizes_company_before_revision(self):
        model = self.env["account.move"].with_user(self.billing_a).sudo().with_context(
            allowed_company_ids=self.company.ids,
        )
        account_a = self.company_data["default_account_revenue"]
        account_b = self.other_data["default_account_revenue"]
        omitted = self.invoice_values(0)
        omitted["invoice_line_ids"][0][2].pop("account_id")
        positive = model.with_context(default_account_id=account_a.id).create(omitted)
        self.assertEqual(positive.invoice_line_ids.account_id, account_a)
        overridden = model.with_context(default_account_id=account_b.id).create(self.invoice_values(0))
        self.assertEqual(overridden.invoice_line_ids.account_id, account_a)
        with self.env.cr.savepoint():
            before = self.accounting_snapshot()
            observed_locks = []
            native_lock = type(self.company)._portfolio_lock_fulfillment

            def observe_native_lock(companies):
                # Delegate the real lock unchanged; observe ordering even when
                # an outer accounting savepoint would roll its revision back.
                observed_locks.extend(companies.ids)
                return native_lock(companies)

            try:
                with patch.object(type(self.company), "_portfolio_lock_fulfillment", observe_native_lock):
                    with unittest.TestCase.assertRaises(self, AccessError):
                        model.with_context(default_account_id=account_b.id).create(omitted)
            except UserError as error:
                self.fail(f"Foreign effective account default requires actor AccessError before accounting validation: {error}")
            self.assertFalse(observed_locks, "Candidate account authorization must precede the technical revision.")
            self.assertEqual(self.accounting_snapshot(), before)

    def source_creation_snapshot(self):
        accounting = self.accounting_snapshot()
        sources = self.env["sale.order.line"].search([("company_id", "=", self.company.id)])
        return accounting, tuple((source.id, source.order_id.id, source.product_id.id,
                                  source.product_uom_qty, tuple(source.invoice_lines.ids)) for source in sources)

    def test_nested_new_sales_source_cannot_bypass_billing_or_leave_half_rows(self):
        composite = new_test_user(
            self.env, login="invoice_source_sales_billing",
            groups="sales_team.group_sale_salesman,account.group_account_invoice",
            company_id=self.company.id, company_ids=[Command.set(self.companies.ids)],
        )
        for actor, error in ((self.sales, AccessError), (composite, UserError)):
            draft = self.env["sale.order"].with_user(actor).with_company(self.company).create({
                "partner_id": self.customer.id, "company_id": self.company.id, "user_id": actor.id,
                "order_line": [Command.create({
                    "product_id": self.product.id, "product_uom": self.product.uom_id.id,
                    "product_uom_qty": 1, "price_unit": 100, "tax_id": [Command.clear()],
                })],
            })
            values = self.invoice_values(1)
            values["invoice_line_ids"][0][2]["sale_line_ids"] = [Command.create({
                "order_id": draft.id, "product_id": self.product.id,
                "product_uom": self.product.uom_id.id, "product_uom_qty": 1,
                "price_unit": 100, "tax_id": [Command.clear()],
            })]
            with self.subTest(actor=actor.login), self.env.cr.savepoint():
                before = self.source_creation_snapshot()
                with unittest.TestCase.assertRaises(self, error):
                    self.env["account.move"].with_user(actor).sudo().with_context(
                        allowed_company_ids=self.company.ids,
                    ).create(values)
                self.assertEqual(self.source_creation_snapshot(), before)

    def test_refund_write_direction_sign_cannot_corrupt_native_amounts(self):
        self.invoice.action_post()
        self.make_return(10)
        credit = self.make_invoice(10, original=self.invoice)
        with self.env.cr.savepoint():
            before = self.accounting_snapshot()
            try:
                credit.write({"direction_sign": -1, "invoice_line_ids": [Command.update(
                    credit.invoice_line_ids.id, {"quantity": 9},
                )]})
            except UserError:
                self.assertEqual(self.accounting_snapshot(), before)
            else:
                self.env.flush_all()
                self.env.invalidate_all()
                self.assertEqual(credit.move_type, "out_refund")
                self.assertEqual(credit.invoice_line_ids.quantity, 9)
                self.assertEqual(credit.invoice_line_ids.balance, 765)
                self.assertEqual(credit.invoice_line_ids.amount_currency, 765)
                self.assertEqual(credit.amount_untaxed, 765)
                credit.action_post()
                self.env.flush_all()
                self.env.invalidate_all()
                self.assertEqual(credit.state, "posted")
                self.assertEqual(credit.invoice_line_ids.balance, 765)
                self.assertEqual(credit.invoice_line_ids.amount_currency, 765)

    def test_sales_credit_product_cannot_escape_reservation_by_becoming_tax_line(self):
        self.invoice.action_post()
        self.make_return(10)
        credit = self.make_invoice(10, original=self.invoice)
        commercial = credit.invoice_line_ids
        self.assertEqual(credit.amount_untaxed, 850)
        context = {
            "skip_invoice_sync": True, "skip_invoice_line_sync": True,
            "check_move_validity": False,
        }

        def classification_snapshot():
            accounting = self.accounting_snapshot()
            entries = self.env["account.move.line"].search([("company_id", "=", self.company.id)])
            return accounting, tuple((
                entry.id, entry.display_type, entry.account_id.id, entry.product_id.id,
                entry.product_uom_id.id, entry.price_unit, entry.discount,
                entry.debit, entry.credit, entry.amount_currency, tuple(entry.tax_ids.ids),
            ) for entry in entries)

        with self.env.cr.savepoint():
            before = classification_snapshot()
            try:
                commercial.with_context(**context).write({"display_type": "tax"})
            except UserError:
                self.assertEqual(classification_snapshot(), before)
            else:
                self.env.flush_all()
                self.env.invalidate_all()
                reserved = self.api("financial_facts")(self.line)[self.line.id]["credit_reserved"]
                if commercial.display_type == "product" and reserved == 10:
                    self.assertEqual(classification_snapshot(), before)
                    return
                observed = {
                    "classification": commercial.display_type,
                    "lines": [(entry.id, entry.display_type, entry.quantity, entry.balance,
                               entry.amount_currency, entry.sale_line_ids.ids)
                              for entry in credit.line_ids],
                    "balanced": sum(credit.line_ids.mapped("balance")),
                    "credit_reserved_after_write": reserved,
                }
                try:
                    credit.with_context(**context).action_post()
                except UserError as error:
                    observed["post_error"] = str(error)
                else:
                    self.env.flush_all()
                    self.env.invalidate_all()
                    observed["posted_state"] = credit.state
                    observed["posted_lines"] = [(entry.id, entry.display_type, entry.quantity,
                                                 entry.balance, entry.sale_line_ids.ids)
                                                for entry in credit.line_ids]
                try:
                    second = self.make_invoice(10, original=self.invoice)
                except UserError as error:
                    observed["second_claim_error"] = str(error)
                else:
                    observed["second_claim"] = (second.id, second.state, second.amount_untaxed)
                self.env.flush_all()
                self.env.invalidate_all()
                observed["credit_reserved_final"] = self.api("financial_facts")(self.line)[self.line.id]["credit_reserved"]
                self.fail(f"A source-linked commercial credit must remain a product reservation: {observed}")

    def test_native_invoice_preserves_linked_sales_sections_and_notes(self):
        order = self.env["sale.order"].with_user(self.sales).with_company(self.company).create({
            "partner_id": self.customer.id, "company_id": self.company.id, "user_id": self.sales.id,
            "order_line": [
                Command.create({"display_type": "line_section", "name": "Mechanical parts"}),
                Command.create({
                    "product_id": self.product.id, "product_uom": self.product.uom_id.id,
                    "product_uom_qty": 1, "price_unit": 100, "discount": 15,
                    "tax_id": [Command.clear()],
                }),
                Command.create({"display_type": "line_note", "name": "Retain delivery documents"}),
            ],
        })
        order.action_portfolio_submit()
        order.with_user(self.manager).action_portfolio_approve()
        order.action_confirm()
        self.validate(self.receipt_backorder, 30)
        self.validate(order.picking_ids, 1)
        invoice = order.with_user(self.billing)._create_invoices().with_user(self.billing)
        for display in ("line_section", "line_note"):
            self.assertEqual(
                invoice.invoice_line_ids.filtered(lambda entry: entry.display_type == display).sale_line_ids,
                order.order_line.filtered(lambda source: source.display_type == display),
            )
        self.assertEqual(invoice.invoice_line_ids.filtered(lambda entry: entry.display_type == "product").quantity, 1)
        invoice.action_post()
        self.assertEqual(invoice.state, "posted")
        self.assertEqual(invoice.amount_untaxed, 85)
        self.assertEqual(sum(invoice.line_ids.mapped("balance")), 0)

    def test_credit_cannot_hide_as_tax_by_replacing_commercial_source_with_section(self):
        draft = self.env["sale.order"].with_user(self.sales).with_company(self.company).create({
            "partner_id": self.customer.id, "company_id": self.company.id, "user_id": self.sales.id,
            "order_line": [Command.create({
                "display_type": "line_section", "name": "Legitimate draft quotation section",
            })],
        })
        section = draft.order_line
        self.invoice.action_post()
        self.make_return(10)
        credit = self.make_invoice(10, original=self.invoice)
        commercial = credit.invoice_line_ids
        self.assertEqual(credit.amount_untaxed, 850)
        context = {
            "skip_invoice_sync": True, "skip_invoice_line_sync": True,
            "check_move_validity": False,
        }

        def mapping_snapshot():
            accounting = self.source_creation_snapshot()
            entries = self.env["account.move.line"].search([("company_id", "=", self.company.id)])
            rows = tuple((entry.id, entry.display_type, entry.account_id.id, entry.product_id.id,
                          entry.product_uom_id.id, entry.price_unit, entry.discount, entry.debit,
                          entry.credit, entry.amount_currency, tuple(entry.tax_ids.ids)) for entry in entries)
            reserved = self.api("financial_facts")(self.line)[self.line.id]["credit_reserved"]
            return accounting, rows, reserved

        with self.env.cr.savepoint():
            before = mapping_snapshot()
            self.assertEqual(before[2], 10)
            try:
                commercial.with_context(**context).write({
                    "display_type": "tax", "sale_line_ids": [Command.set(section.ids)],
                })
            except UserError:
                self.assertEqual(mapping_snapshot(), before)
            else:
                self.env.flush_all()
                self.env.invalidate_all()
                reserved = self.api("financial_facts")(self.line)[self.line.id]["credit_reserved"]
                if commercial.display_type == "product" and commercial.sale_line_ids == self.line and reserved == 10:
                    self.assertEqual(mapping_snapshot(), before)
                    return
                observed = {
                    "original_source": self.line.id, "replacement_section": section.id,
                    "classification": commercial.display_type,
                    "lines": [(entry.id, entry.display_type, entry.quantity, entry.balance,
                               entry.amount_currency, entry.sale_line_ids.ids) for entry in credit.line_ids],
                    "balanced": sum(credit.line_ids.mapped("balance")),
                    "credit_reserved_after_write": reserved,
                }
                try:
                    credit.with_context(**context).action_post()
                except UserError as error:
                    observed["post_error"] = str(error)
                else:
                    self.env.flush_all()
                    self.env.invalidate_all()
                    observed["posted_state"] = credit.state
                    observed["posted_lines"] = [(entry.id, entry.display_type, entry.quantity,
                                                 entry.balance, entry.sale_line_ids.ids) for entry in credit.line_ids]
                try:
                    second = self.make_invoice(10, original=self.invoice)
                except UserError as error:
                    observed["second_claim_error"] = str(error)
                else:
                    observed["second_claim"] = (second.id, second.state, second.amount_untaxed)
                self.env.flush_all()
                self.env.invalidate_all()
                observed["credit_reserved_final"] = self.api("financial_facts")(self.line)[self.line.id]["credit_reserved"]
                self.fail(f"A credit cannot lose its commercial claim through section source replacement: {observed}")
