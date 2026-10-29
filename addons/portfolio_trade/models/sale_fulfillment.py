"""Protect the commercial source independently of the M1 price approval flag."""
from odoo import _, api, models
from odoo.exceptions import AccessError, UserError

from .sale_order import _PORTFOLIO_STATE_CAPABILITY, LINE_COMMERCIAL, ORDER_COMMERCIAL
from .stock_policy import authorize_companies, controlled


def _require_sales(model, companies, operation="write"):
    authorize_companies(companies)
    actor_model = model.with_user(model.env.uid)
    if not actor_model.env.user.has_group("sales_team.group_sale_salesman"):
        raise AccessError(_("Only sales users can change commercial sales inputs."))
    actor_model.check_access(operation)


class SaleOrder(models.Model):
    _inherit = "sale.order"

    def _portfolio_has_execution(self):
        self.ensure_one()
        # Native picking.sale_id derives from procurement group, so a manual
        # delivery can belong to a real sales line without appearing in
        # order.picking_ids. Inspect both native links, never cached counters.
        return bool(
            self.order_line.move_ids.filtered(lambda move: move.state == "done")
            or self.picking_ids.filtered(lambda picking: picking.state == "done")
            or self.invoice_ids.filtered(lambda invoice: invoice.state == "posted")
        )

    def _portfolio_mutable(self, extra_companies=None):
        super()._portfolio_mutable(extra_companies)
        for order in self:
            companies = order.company_id | (extra_companies or self.env["res.company"])
            if controlled(companies):
                if order._portfolio_has_execution():
                    raise UserError(_("Executed sales sources are immutable; use returns and credit notes."))
                if order.state not in ("draft", "sent", "cancel"):
                    raise UserError(_("Commercial content can only change on an unexecuted quotation."))

    @api.model_create_multi
    def create(self, vals_list):
        default_company = self.default_get(["company_id"]).get("company_id", self.env.company.id)
        companies = self.env["res.company"].browse(sorted({values.get("company_id", default_company) for values in vals_list}))
        if controlled(companies):
            _require_sales(self, companies, "create")
        return super().create(vals_list)

    def write(self, values):
        if ORDER_COMMERCIAL.intersection(values) or "state" in values:
            companies = self.sudo().company_id | self.env["res.company"].browse(values.get("company_id") or [])
            if controlled(companies):
                _require_sales(self, companies)
                state = values.get("state")
                cap = _PORTFOLIO_STATE_CAPABILITY.get()
                permitted = cap and cap[0] is self.env.cr and cap[1] == self.env.uid and cap[2].issuperset(self.ids) and cap[3] == state
                if state and not permitted:
                    for order in self:
                        if order.state == "sale" or state == "cancel" or (
                            state == "draft" and order.state != "draft"
                        ) or (state == "sent" and order.state not in ("draft", "sent")):
                            raise UserError(_("Use the standard sales actions to cancel or reopen a quotation."))
        return super().write(values)

    def action_confirm(self):
        enabled = self.filtered(lambda order: order.company_id.portfolio_fulfillment_enabled)
        if enabled:
            _require_sales(enabled, enabled.company_id)
            if any(order.user_id.id != self.env.uid for order in enabled):
                raise AccessError(_("Only the responsible salesperson can confirm this quotation."))
            enabled.company_id._portfolio_lock_fulfillment()
        return super().action_confirm()

    def action_draft(self):
        enabled = self.filtered(lambda order: order.company_id.portfolio_fulfillment_enabled)
        if enabled:
            _require_sales(enabled, enabled.company_id)
            # Recheck execution after the shared company/SO locks, so a
            # concurrent physical completion cannot race source reopening.
            self._portfolio_lock()
        for order in enabled:
            if order._portfolio_has_execution():
                raise UserError(_("Executed orders cannot reopen; use returns and credit notes."))
        return super().action_draft()

    def unlink(self):
        enabled = self.filtered(lambda order: order.company_id.portfolio_fulfillment_enabled)
        if enabled:
            _require_sales(self, self.company_id, "unlink")
            self._portfolio_lock()
            if any(order._portfolio_has_execution() for order in enabled):
                raise UserError(_("Executed sales sources cannot be deleted; retain their stock and invoice history."))
        return super().unlink()


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    def _portfolio_physical_inputs(self, values):
        products = self.product_id | self.env["product.product"].browse(values.get("product_id") or [])
        orders = self.order_id | self.env["sale.order"].browse(values.get("order_id") or [])
        return controlled(orders.sudo().company_id) and any(product.type == "consu" for product in products)

    @api.model_create_multi
    def create(self, vals_list):
        defaults = self.default_get(["order_id", "product_id"])
        configured = self.env["ir.default"]._get_model_defaults(self._name)
        for values in vals_list:
            effective = {**defaults, **values}
            order = self.env["sale.order"].browse(effective.get("order_id") or [])
            if controlled(order.sudo().company_id):
                _require_sales(order, order.sudo().company_id)
                supplied = {"qty_delivered", "qty_delivered_method"}.intersection(values)
                supplied |= {"qty_delivered", "qty_delivered_method"}.intersection(configured)
                supplied |= {name for name in ("qty_delivered", "qty_delivered_method") if f"default_{name}" in self.env.context}
                if supplied and self._portfolio_physical_inputs(effective):
                    raise UserError(_("Physical delivered quantities are derived from completed stock moves."))
        return super().create(vals_list)

    def write(self, values):
        if {"qty_delivered", "qty_delivered_method"}.intersection(values) and self._portfolio_physical_inputs(values):
            raise UserError(_("Physical delivered quantities are derived from completed stock moves."))
        if LINE_COMMERCIAL.intersection(values):
            orders = self.order_id | self.env["sale.order"].browse(values.get("order_id") or [])
            companies = orders.sudo().company_id
            if controlled(companies):
                _require_sales(orders, companies)
                self.with_user(self.env.uid).check_access("write")
        return super().write(values)

    def unlink(self):
        companies = self.order_id.sudo().company_id
        if controlled(companies):
            _require_sales(self.order_id, companies)
        return super().unlink()
