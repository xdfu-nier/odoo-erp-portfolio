import hashlib
import json

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

ORDER_COMMERCIAL = {
    "partner_id", "partner_invoice_id", "partner_shipping_id", "company_id", "currency_id",
    "pricelist_id", "payment_term_id", "commitment_date", "user_id", "order_line",
}
LINE_COMMERCIAL = {
    "order_id", "product_id", "product_uom", "product_uom_qty", "price_unit", "discount", "tax_id",
    "display_type", "is_downpayment", "technical_price_unit", "customer_lead",
}
PROTECTED = {
    "portfolio_creator_id", "portfolio_approval_state", "portfolio_approval_submitter_id",
    "portfolio_approval_fingerprint", "portfolio_approval_policy_version", "portfolio_revision",
    "portfolio_approval_history_ids", "portfolio_approval_enabled", "portfolio_approval_current", "create_uid",
}


class SaleOrder(models.Model):
    _inherit = "sale.order"

    portfolio_creator_id = fields.Many2one("res.users", readonly=True, copy=False)
    portfolio_approval_state = fields.Selection([
        ("draft", "Draft"), ("submitted", "Awaiting approval"),
        ("approved", "Approved"), ("rejected", "Rejected"),
    ], default="draft", required=True, readonly=True, copy=False)
    portfolio_approval_submitter_id = fields.Many2one("res.users", readonly=True, copy=False)
    portfolio_approval_fingerprint = fields.Char(readonly=True, copy=False)
    portfolio_approval_policy_version = fields.Integer(readonly=True, copy=False)
    portfolio_revision = fields.Integer(default=0, readonly=True, copy=False)
    portfolio_approval_history_ids = fields.One2many("portfolio.sale.approval", "order_id", readonly=True, copy=False)
    portfolio_approval_enabled = fields.Boolean(related="company_id.portfolio_approval_enabled")
    portfolio_approval_current = fields.Boolean(compute="_compute_portfolio_approval_current")

    @api.depends("portfolio_approval_fingerprint", "portfolio_approval_policy_version", "company_id.portfolio_approval_policy_version")
    def _compute_portfolio_approval_current(self):
        for order in self:
            order.portfolio_approval_current = order._portfolio_current() if order.id else False

    def _portfolio_lock(self, extra_companies=None):
        """Authorize before SQL, company IDs then order IDs, then discard caches.

        Every sensitive operation also writes the parent revision through ORM. A
        line-only change must update its parent: under PostgreSQL repeatable read,
        a waiter otherwise keeps a pre-change snapshot even after acquiring locks.
        Odoo's transaction retry handles serialization failures; no stale decision
        is permitted inside an existing transaction.
        """
        self.check_access("write")
        companies = self.company_id | (extra_companies or self.env["res.company"])
        companies._portfolio_lock_policy()
        if self:
            self.env.cr.execute(
                "SELECT id FROM sale_order WHERE id IN %s ORDER BY id FOR UPDATE", [tuple(self.ids)],
            )
            self.invalidate_recordset()
            self.env["sale.order.line"].invalidate_model()
            self.check_access("write")
            if self.company_id - companies:
                raise UserError(_("The order company changed concurrently. Please retry."))
            for order in self:
                super(SaleOrder, order).write({"portfolio_revision": order.portfolio_revision + 1})

    def _portfolio_snapshot(self):
        self.ensure_one()
        data = {
            "company": self.company_id.id,
            "policy": self.company_id.portfolio_approval_policy_version,
            "customer": self.partner_id.id,
            "invoice": self.partner_invoice_id.id,
            "delivery": self.partner_shipping_id.id,
            "currency": self.currency_id.id,
            "pricelist": self.pricelist_id.id,
            "terms": self.payment_term_id.id,
            "commitment": fields.Datetime.to_string(self.commitment_date),
            "salesperson": self.user_id.id,
            "lines": [{
                "id": line.id, "product": line.product_id.id, "uom": line.product_uom.id,
                "quantity": line.product_uom_qty, "price": line.price_unit, "discount": line.discount,
                "taxes": sorted(line.tax_id.ids), "type": line.display_type,
                "downpayment": line.is_downpayment, "lead": line.customer_lead,
            } for line in self.order_line.sorted("id")],
        }
        snapshot = json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return snapshot, hashlib.sha256(snapshot.encode()).hexdigest()

    def _portfolio_event(self, event, reason=None):
        self.ensure_one()
        snapshot, fingerprint = self._portfolio_snapshot()
        return self.env["portfolio.sale.approval"]._append_event({
            "order_id": self.id, "company_id": self.company_id.id, "event": event, "actor_id": self.env.uid,
            "reason": reason, "snapshot": snapshot, "fingerprint": fingerprint,
            "policy_version": self.company_id.portfolio_approval_policy_version,
        })

    def _portfolio_invalidate(self):
        for order in self.filtered(lambda item: item.portfolio_approval_state != "draft"):
            order._portfolio_event("invalidated", _("Commercial content changed; submit again."))
            super(SaleOrder, order).write({
                "portfolio_approval_state": "draft", "portfolio_approval_fingerprint": False,
                "portfolio_approval_submitter_id": False, "portfolio_approval_policy_version": 0,
            })

    def _portfolio_mutable(self, extra_companies=None):
        for order in self:
            enabled = order.company_id.portfolio_approval_enabled or (
                extra_companies and any(extra_companies.mapped("portfolio_approval_enabled"))
            )
            if enabled and order.state not in ("draft", "sent"):
                raise UserError(_("Commercial content can only change on a draft or sent quotation."))

    def _portfolio_currency(self):
        for order in self:
            if order.currency_id != order.company_id.currency_id:
                raise UserError(_("Sales approval currently supports only the company currency."))

    def _portfolio_requires_approval(self):
        self.ensure_one()
        company = self.company_id
        return company.currency_id.compare_amounts(self.amount_untaxed, company.portfolio_approval_amount) >= 0 or any(
            line.discount > company.portfolio_approval_discount
            for line in self.order_line if line.product_id and not line.display_type
        )

    def _portfolio_current(self):
        self.ensure_one()
        return (
            self.portfolio_approval_policy_version == self.company_id.portfolio_approval_policy_version
            and self.portfolio_approval_fingerprint == self._portfolio_snapshot()[1]
        )

    def _portfolio_check_confirmation(self):
        for order in self.filtered("portfolio_approval_enabled"):
            order._portfolio_currency()
            if order._portfolio_requires_approval() and (
                order.portfolio_approval_state != "approved" or not order._portfolio_current()
            ):
                raise UserError(_("This quotation requires current approval before confirmation. Submit it for approval."))

    @api.model_create_multi
    def create(self, vals_list):
        if any(f"default_{key}" in self.env.context for key in PROTECTED):
            raise AccessError(_("Approval fields cannot be supplied through defaults."))
        for values in vals_list:
            if PROTECTED.intersection(values):
                raise AccessError(_("Approval fields and the true creator cannot be supplied."))
            if values.get("state", "draft") not in ("draft", "sent"):
                raise UserError(_("Create a quotation, then use the standard confirmation action."))
        orders = super().create(vals_list)
        for order in orders:
            super(SaleOrder, order).write({"portfolio_creator_id": self.env.uid})
        return orders

    def write(self, values):
        if PROTECTED.intersection(values):
            raise AccessError(_("Approval fields and the true creator cannot be modified directly."))
        relevant = ORDER_COMMERCIAL.intersection(values) or "state" in values
        if not relevant:
            return super().write(values)
        extra = self.env["res.company"].browse(values.get("company_id", []))
        # Include parents of lines moved through One2many commands before nesting
        # into line.write, preserving a single stable lock order for both parents.
        affected = self
        for command in values.get("order_line", []):
            if command[0] in (1, 2, 3, 4) and command[1]:
                line = self.env["sale.order.line"].browse(command[1])
                line.check_access("write")
                affected |= line.order_id
            elif command[0] == 6:
                lines = self.env["sale.order.line"].browse(command[2])
                lines.check_access("write")
                affected |= lines.order_id
        affected._portfolio_lock(extra)
        if ORDER_COMMERCIAL.intersection(values):
            self._portfolio_mutable(extra)
            self._portfolio_invalidate()
        if values.get("state") == "sale":
            self._portfolio_check_confirmation()
        elif "state" in values and values["state"] != "sent":
            if values["state"] == "draft" and any(
                order.state == "sale" and order.portfolio_approval_enabled for order in self
            ):
                raise UserError(_("A confirmed order cannot be reset directly to a quotation."))
            self._portfolio_invalidate()
        return super().write(values)

    def action_portfolio_submit(self):
        self._portfolio_lock()
        self._portfolio_mutable()
        for order in self:
            if not order.portfolio_approval_enabled:
                raise UserError(_("Sales approval policy is disabled for this company."))
            order._portfolio_currency()
            if order.portfolio_approval_state == "submitted" and order._portfolio_current():
                raise UserError(_("This quotation is already awaiting approval."))
            event = order._portfolio_event("submitted")
            super(SaleOrder, order).write({
                "portfolio_approval_state": "submitted", "portfolio_approval_submitter_id": self.env.uid,
                "portfolio_approval_fingerprint": event.fingerprint,
                "portfolio_approval_policy_version": event.policy_version,
            })
        return True

    def _portfolio_reviewable(self):
        if not self.env.user.has_group("sales_team.group_sale_manager"):
            raise AccessError(_("Only sales managers can review quotations."))
        self._portfolio_mutable()
        for order in self:
            if not order.portfolio_approval_enabled or order.portfolio_approval_state != "submitted":
                raise UserError(_("Only a submitted quotation can be reviewed."))
            if self.env.uid in (order.portfolio_creator_id.id, order.create_uid.id, order.user_id.id, order.portfolio_approval_submitter_id.id):
                raise UserError(_("You cannot review an order you created, own, or submitted."))
            if not order._portfolio_current():
                raise UserError(_("The quotation or company policy changed. Submit again."))

    def action_portfolio_approve(self):
        self._portfolio_lock()
        self._portfolio_reviewable()
        for order in self:
            order._portfolio_event("approved")
        super().write({"portfolio_approval_state": "approved"})
        return True

    def action_portfolio_reject(self, reason):
        self._portfolio_lock()
        self._portfolio_reviewable()
        if not isinstance(reason, str) or not reason.strip():
            raise UserError(_("Enter a reason for rejecting the quotation."))
        for order in self:
            order._portfolio_event("rejected", reason.strip())
        super().write({"portfolio_approval_state": "rejected"})
        return True

    def action_portfolio_reject_wizard(self):
        self.ensure_one()
        self.check_access("write")
        self._portfolio_reviewable()
        return {
            "type": "ir.actions.act_window", "name": _("Reject quotation"),
            "res_model": "portfolio.sale.approval.reject", "view_mode": "form", "target": "new",
            "context": {"default_order_id": self.id},
        }

    def action_confirm(self):
        self._portfolio_lock()
        self._portfolio_check_confirmation()
        return super().action_confirm()


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    @api.model_create_multi
    def create(self, vals_list):
        order_ids = {values.get("order_id") or self.env.context.get("default_order_id") for values in vals_list}
        orders = self.env["sale.order"].browse(sorted(order_ids - {None, False}))
        orders._portfolio_lock()
        orders._portfolio_mutable()
        orders._portfolio_invalidate()
        return super().create(vals_list)

    def write(self, values):
        if not LINE_COMMERCIAL.intersection(values):
            return super().write(values)
        self.check_access("write")
        orders = self.order_id | self.env["sale.order"].browse(values.get("order_id", []))
        orders._portfolio_lock()
        self.invalidate_recordset()
        self.check_access("write")
        if self.order_id - orders:
            raise UserError(_("A line moved concurrently. Please retry."))
        orders._portfolio_mutable()
        orders._portfolio_invalidate()
        return super().write(values)

    def unlink(self):
        self.check_access("unlink")
        orders = self.order_id
        orders._portfolio_lock()
        self.invalidate_recordset()
        self.check_access("unlink")
        if self.order_id - orders:
            raise UserError(_("A line moved concurrently. Please retry."))
        orders._portfolio_mutable()
        orders._portfolio_invalidate()
        return super().unlink()
