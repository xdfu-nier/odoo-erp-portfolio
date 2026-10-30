"""Create only the requested, original-priced portion of a customer credit."""
import math

from odoo import Command, _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools.float_utils import float_compare

from ..models.account_fulfillment import _pricing_key
from ..models.financial_policy import (
    _authorized_sources,
    financial_facts,
    lock_financial,
    physical_facts,
    require_billing,
)


def _credit_original(entry):
    """Authorize canonical identities before reading the original's payload."""
    entry = entry.with_user(entry.env.uid)
    require_billing(entry.env, entry.sudo().company_id | entry.sudo().move_id.company_id)
    if len(entry) != 1 or not entry.exists():
        raise UserError(_("Select one original customer invoice product line."))
    entry.check_access("read")
    original = entry.move_id
    original.check_access("read")
    sources = _authorized_sources(entry.sale_line_ids)
    if (
        original.state != "posted" or original.move_type != "out_invoice"
        or entry.display_type != "product" or len(sources) != 1
        or sources.display_type or sources.product_id.type != "consu"
        or sources.is_downpayment or entry.is_downpayment
        or entry.product_id != sources.product_id
        or entry.company_id != original.company_id or sources.order_id.company_id != original.company_id
        or entry.currency_id != original.currency_id or original.currency_id != original.company_id.currency_id
        or not entry.product_uom_id or entry.product_uom_id.category_id != sources.product_uom.category_id
        or not math.isfinite(entry.quantity) or entry.quantity <= 0
        or not math.isfinite(entry.price_unit) or not math.isfinite(entry.discount)
    ):
        raise UserError(_("A partial credit requires a consistent original posted customer invoice product line."))
    return entry, sources


def _available_credit(entry, source):
    """Minimum of the real return, selected original and exact pricing budgets."""
    financial = financial_facts(source)[source.id]
    if financial["invalid_line_ids"]:
        raise UserError(_("Malformed active invoice facts prevent granting a credit allowance."))
    physical = physical_facts(source)[source.id]
    original = entry.move_id
    original_facts = financial["originals"].get(original.id)
    if not original_facts or original_facts["state"] != "posted":
        raise UserError(_("The selected original invoice no longer grants a posted allowance."))
    key = _pricing_key(entry, source, original)
    billed = sum(
        line.product_uom_id._compute_quantity(line.quantity, source.product_uom, round=False)
        for line in original.invoice_line_ids
        if line.display_type == "product" and line.sale_line_ids == source
        and _pricing_key(line, source, original) == key
    )
    # financial_facts has already authorized every discovered actual company and
    # sales source. This query reads only the selected original's bounded claims.
    credits = source.env["account.move.line"].search([
        ("sale_line_ids", "in", source.ids), ("display_type", "=", "product"),
        ("move_id.move_type", "=", "out_refund"), ("move_id.state", "!=", "cancel"),
        ("move_id.reversed_entry_id", "=", original.id),
    ])
    claimed = sum(
        line.product_uom_id._compute_quantity(line.quantity, source.product_uom, round=False)
        for line in credits if _pricing_key(line, source, original) == key
    )
    available = max(0, min(
        physical["returned"] - financial["credit_reserved"],
        original_facts["invoice_posted"] - original_facts["credit_reserved"],
        billed - claimed,
    ))
    return source.product_uom._compute_quantity(available, entry.product_uom_id, round=False)


class PartialCredit(models.TransientModel):
    _name = "portfolio.partial.credit"
    _description = "Original invoice partial customer credit"

    original_line_id = fields.Many2one("account.move.line", required=True, string="Original invoice line")
    quantity = fields.Float(required=True, default=1, string="Quantity to credit")
    product_uom_id = fields.Many2one(related="original_line_id.product_uom_id", readonly=True)
    available_quantity = fields.Float(compute="_compute_available_quantity", string="Available to credit")

    @api.depends("original_line_id")
    def _compute_available_quantity(self):
        for wizard in self:
            if wizard.original_line_id:
                entry, source = _credit_original(wizard.original_line_id)
                wizard.available_quantity = _available_credit(entry, source)
            else:
                wizard.available_quantity = 0

    def action_create_credit(self):
        self.ensure_one()
        with self.env.cr.savepoint():
            wizard = self.with_user(self.env.uid)
            require_billing(wizard.env, wizard.sudo().original_line_id.company_id)
            wizard.check_access("read")
            entry, source = _credit_original(wizard.original_line_id)
            quantity = wizard.quantity
            if not math.isfinite(quantity) or quantity <= 0:
                raise UserError(_("The credit quantity must be finite and strictly positive."))
            lock_financial(source, extra_companies=entry.move_id.company_id)
            entry, source = _credit_original(wizard.original_line_id)
            available = _available_credit(entry, source)
            if float_compare(quantity, available, precision_rounding=entry.product_uom_id.rounding) > 0:
                raise UserError(_("The requested credit exceeds the remaining original-priced return allowance."))
            original = entry.move_id
            # Start directly at the bounded quantity. No full reversal, temporary
            # overclaim or caller context capability is involved.
            model = wizard.env["account.move"].with_context({
                "allowed_company_ids": wizard.env.companies.ids,
            }).with_company(original.company_id)
            credit = model.create({
                "move_type": "out_refund", "company_id": original.company_id.id,
                "currency_id": original.currency_id.id, "partner_id": original.partner_id.id,
                "partner_shipping_id": original.partner_shipping_id.id,
                "journal_id": original.journal_id.id, "invoice_date": fields.Date.today(),
                "reversed_entry_id": original.id, "invoice_origin": original.name,
                "fiscal_position_id": original.fiscal_position_id.id,
                "invoice_payment_term_id": original.invoice_payment_term_id.id,
                "invoice_cash_rounding_id": original.invoice_cash_rounding_id.id,
                "invoice_line_ids": [Command.create({
                    "display_type": "product", "name": entry.name,
                    "product_id": entry.product_id.id, "product_uom_id": entry.product_uom_id.id,
                    "account_id": entry.account_id.id, "quantity": quantity,
                    "price_unit": entry.price_unit, "discount": entry.discount,
                    "tax_ids": [Command.set(entry.tax_ids.ids)],
                    "sale_line_ids": [Command.set(source.ids)],
                })],
            })
            credit.check_access("read")
            return credit.get_formview_action()


class AccountMove(models.Model):
    _inherit = "account.move"

    portfolio_finance_enabled = fields.Boolean(related="company_id.portfolio_fulfillment_enabled")

    def action_portfolio_partial_credit(self):
        self.ensure_one()
        with self.env.cr.savepoint():
            original = self.with_user(self.env.uid)
            require_billing(original.env, original.sudo().company_id)
            original.check_access("read")
            _authorized_sources(original.line_ids.sale_line_ids)
            if original.state != "posted" or original.move_type != "out_invoice":
                raise UserError(_("Select an original posted customer invoice."))
            entries = original.invoice_line_ids.filtered(
                lambda line: line.display_type == "product" and line.sale_line_ids,
            )
            entry, _source = _credit_original(entries[:1])
            view = original.env.ref("portfolio_trade.partial_credit_form")
            return {
                "type": "ir.actions.act_window", "name": _("Partial customer credit"),
                "res_model": "portfolio.partial.credit", "view_mode": "form",
                "view_id": view.id, "views": [(view.id, "form")], "target": "new",
                "context": {
                    "allowed_company_ids": original.env.companies.ids,
                    "default_original_line_id": entry.id, "default_quantity": 1,
                },
            }
