"""Native Sales invoicing bridge and derived sales invoice cache boundaries."""
from odoo import _, api, models
from odoo.exceptions import UserError

from .account_capability import permits_sale_computation, sale_computation
from .financial_policy import lock_financial, require_billing
from .stock_policy import controlled

COUNTERS = {"qty_invoiced", "qty_invoiced_posted", "qty_to_invoice", "invoice_status"}


class SaleOrder(models.Model):
    _inherit = "sale.order"

    def _create_invoices(self, grouped=False, final=False, date=None):
        enabled = self.filtered(lambda order: order.sudo().company_id.portfolio_fulfillment_enabled)
        if not enabled:
            return super()._create_invoices(grouped=grouped, final=final, date=date)
        require_billing(self.env, self.sudo().company_id)
        with self.env.cr.savepoint():
            lock_financial(self.order_line)
            return super()._create_invoices(grouped=grouped, final=final, date=date)


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    @api.model_create_multi
    def create(self, vals_list):
        defaults = self.default_get(["order_id"])
        configured = self.env["ir.default"]._get_model_defaults(self._name)
        for values in vals_list:
            order = self.env["sale.order"].browse(values.get("order_id", defaults.get("order_id")) or [])
            if controlled(order.sudo().company_id):
                supplied = COUNTERS.intersection(values) | COUNTERS.intersection(configured)
                supplied |= {name for name in COUNTERS if f"default_{name}" in self.env.context}
                if supplied or "invoice_lines" in values or "invoice_lines" in configured or "default_invoice_lines" in self.env.context:
                    raise UserError(_("Invoice quantities, status and links are derived from native accounting."))
        return super().create(vals_list)

    def write(self, values):
        orders = self.order_id | self.env["sale.order"].browse(values.get("order_id") or [])
        if controlled(orders.sudo().company_id):
            supplied = COUNTERS.intersection(values)
            if supplied and not permits_sale_computation(self, supplied) or "invoice_lines" in values:
                raise UserError(_("Invoice quantities, status and links are derived from native accounting."))
        return super().write(values)

    def _compute_qty_invoiced(self):
        with sale_computation(self, {"qty_invoiced"}):
            return super()._compute_qty_invoiced()

    def _compute_qty_invoiced_posted(self):
        with sale_computation(self, {"qty_invoiced_posted"}):
            return super()._compute_qty_invoiced_posted()

    def _compute_qty_to_invoice(self):
        with sale_computation(self, {"qty_to_invoice"}):
            return super()._compute_qty_to_invoice()

    def _compute_invoice_status(self):
        with sale_computation(self, {"invoice_status"}):
            return super()._compute_invoice_status()
