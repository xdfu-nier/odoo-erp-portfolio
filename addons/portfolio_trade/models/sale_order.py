import contextvars
import hashlib
import json

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

# Python execution-local capability, never read from caller-controlled Odoo context.
# It authorizes only the standard action's cursor, actor, exact records and target.
_PORTFOLIO_STATE_CAPABILITY = contextvars.ContextVar("portfolio_state_capability", default=None)

ORDER_COMMERCIAL = {
    "partner_id", "partner_invoice_id", "partner_shipping_id", "company_id", "currency_id",
    "pricelist_id", "payment_term_id", "commitment_date", "user_id", "order_line", "validity_date",
    "note", "fiscal_position_id", "warehouse_id", "incoterm", "incoterm_location", "require_signature",
    "require_payment", "prepayment_percent",
}
LINE_COMMERCIAL = {
    "order_id", "product_id", "product_uom", "product_uom_qty", "price_unit", "discount", "tax_id",
    "display_type", "is_downpayment", "technical_price_unit", "customer_lead", "name",
    "analytic_distribution", "product_packaging_id", "product_packaging_qty", "route_id", "is_expense",
    "linked_line_id", "linked_line_ids",
}
PROTECTED = {
    "portfolio_creator_id", "portfolio_approval_state", "portfolio_approval_submitter_id",
    "portfolio_approval_fingerprint", "portfolio_approval_policy_version", "portfolio_revision",
    "portfolio_approval_history_ids", "portfolio_approval_enabled", "portfolio_approval_current", "create_uid",
}
ORDER_DERIVED_FINANCIAL = {
    "amount_untaxed", "amount_tax", "amount_total", "amount_to_invoice", "amount_invoiced",
    "amount_paid", "amount_undiscounted", "tax_totals",
}
LINE_DERIVED_FINANCIAL = {
    "price_subtotal", "price_tax", "price_total", "amount_invoiced", "amount_to_invoice",
    "untaxed_amount_invoiced", "untaxed_amount_to_invoice",
}
LINE_DERIVED_IDENTITY = {
    "company_id", "currency_id", "order_partner_id", "salesman_id", "state", "warehouse_id",
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
    portfolio_purchase_ids = fields.One2many(
        "purchase.order", "portfolio_source_sale_id", string="Related Purchases",
        readonly=True, copy=False, check_company=True,
    )

    def action_portfolio_purchases(self):
        self.ensure_one()
        self.check_access("read")
        purchases = self.env["purchase.order"].search([("portfolio_source_sale_id", "=", self.id)])
        return {
            "type": "ir.actions.act_window", "name": _("Related Purchases"),
            "res_model": "purchase.order", "view_mode": "list,form",
            "domain": [("id", "in", purchases.ids)],
            "context": {"create": False, "edit": False, "delete": False},
        }

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
            "validity": fields.Date.to_string(self.validity_date),
            "note": self.note,
            "fiscal_position": self.fiscal_position_id.id,
            "warehouse": self.warehouse_id.id,
            "incoterm": self.incoterm.id,
            "incoterm_location": self.incoterm_location,
            "require_signature": self.require_signature,
            "require_payment": self.require_payment,
            "prepayment_percent": self.prepayment_percent,
            "salesperson": self.user_id.id,
            "lines": [{
                "id": line.id, "product": line.product_id.id, "uom": line.product_uom.id,
                "quantity": line.product_uom_qty, "price": line.price_unit, "discount": line.discount,
                "taxes": sorted(line.tax_id.ids), "type": line.display_type,
                "downpayment": line.is_downpayment, "lead": line.customer_lead, "name": line.name,
                "analytic": line.analytic_distribution,
                "packaging": line.product_packaging_id.id, "packaging_qty": line.product_packaging_qty,
                "route": line.route_id.id, "expense": line.is_expense, "linked_line": line.linked_line_id.id,
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
        defaults = self.env["ir.default"]._get_model_defaults(self._name)
        if PROTECTED.intersection(defaults) or defaults.get("state", "draft") not in ("draft", "sent"):
            raise AccessError(_("Approval fields and state cannot be supplied through defaults."))
        if self.env.context.get("default_state", "draft") not in ("draft", "sent"):
            raise AccessError(_("Create a quotation before confirming it."))
        if any(f"default_{key}" in self.env.context for key in PROTECTED):
            raise AccessError(_("Approval fields cannot be supplied through defaults."))
        if ORDER_DERIVED_FINANCIAL.intersection(defaults) or any(
            f"default_{key}" in self.env.context for key in ORDER_DERIVED_FINANCIAL
        ):
            raise AccessError(_("Computed financial totals cannot be supplied through defaults."))
        for values in vals_list:
            if PROTECTED.intersection(values) or ORDER_DERIVED_FINANCIAL.intersection(values):
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
        if ORDER_DERIVED_FINANCIAL.intersection(values):
            raise UserError(_("Computed financial totals are derived from commercial order lines."))
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
                if command[0] == 1 and "order_id" in command[2] and command[2]["order_id"] != line.order_id.id:
                    raise UserError(_("Moving lines through quotation updates is not supported."))
                affected |= line.order_id
            elif command[0] == 6:
                lines = self.env["sale.order.line"].browse(command[2])
                lines.check_access("write")
                affected |= lines.order_id
        affected._portfolio_lock(extra)
        if "company_id" in values:
            changing = self.filtered(lambda order: order.company_id.id != values["company_id"])
            # Sales write access and the shared company/SO locks are already
            # checked. Read only the existence of incompatible company links;
            # the salesperson need not read hidden PO payloads or display names.
            if changing and self.env["purchase.order"].sudo().search_count([
                ("portfolio_source_sale_id", "in", changing.ids),
                ("company_id", "!=", values["company_id"]),
            ], limit=1):
                raise UserError(_("The sales company must remain consistent with its linked purchases."))
            for order in self:
                if (
                    order.company_id.id != values["company_id"] and order.state == "sale"
                    and (
                        order.company_id.portfolio_masterdata_enabled or extra.portfolio_masterdata_enabled
                        or order.company_id.portfolio_fulfillment_enabled or extra.portfolio_fulfillment_enabled
                    )
                ):
                    raise UserError(_("Change the sales company on a draft quotation before confirmation."))
        if ORDER_COMMERCIAL.intersection(values):
            self._portfolio_mutable(extra)
            self._portfolio_invalidate()
        state = values.get("state")
        capability = _PORTFOLIO_STATE_CAPABILITY.get()
        capability_allows = (
            capability and capability[0] is self.env.cr and capability[1] == self.env.uid
            and capability[2].issuperset(self.ids) and capability[3] == state
        )
        if state and state == "sale" and not capability_allows:
            raise UserError(_("Use the standard Confirm action to confirm a quotation."))
        if state and not capability_allows:
            for order in self.filtered("portfolio_approval_enabled"):
                if order.state == "sale" or state == "cancel" or (
                    state == "draft" and order.state != "draft"
                ) or (state == "sent" and order.state not in ("draft", "sent")):
                    raise UserError(_("Use the standard sales actions to cancel or reopen a quotation."))
        if state == "sale":
            self._portfolio_check_confirmation()
        elif "state" in values and values["state"] != "sent":
            self._portfolio_invalidate()
        return super().write(values)

    def _portfolio_run_standard_state_action(self, state, action):
        capability = _PORTFOLIO_STATE_CAPABILITY.set((self.env.cr, self.env.uid, frozenset(self.ids), state))
        try:
            return action()
        finally:
            _PORTFOLIO_STATE_CAPABILITY.reset(capability)

    def action_portfolio_submit(self):
        self._portfolio_lock()
        self.env["portfolio.master.data"]._validate_orders(self, "customer")
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
        self.env["portfolio.master.data"]._validate_orders(self, "customer")
        self._portfolio_check_confirmation()
        for order in self.filtered("portfolio_approval_enabled"):
            if order.user_id != self.env.user:
                raise AccessError(_("Only the responsible salesperson can confirm this quotation."))
        return self._portfolio_run_standard_state_action("sale", lambda: super(SaleOrder, self).action_confirm())

    def action_draft(self):
        self._portfolio_lock()
        for order in self.filtered("portfolio_approval_enabled"):
            if order.picking_ids.filtered(lambda picking: picking.state == "done") or order.invoice_ids.filtered(
                lambda invoice: invoice.state == "posted"
            ):
                raise UserError(_("Executed orders cannot reopen; use returns and credit notes."))
        return self._portfolio_run_standard_state_action("draft", lambda: super(SaleOrder, self).action_draft())

    def _action_cancel(self):
        self._portfolio_lock()
        return self._portfolio_run_standard_state_action("cancel", lambda: super(SaleOrder, self)._action_cancel())


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    @api.constrains("linked_line_id", "linked_line_ids", "order_id")
    def _portfolio_check_linked_order(self):
        for line in self:
            if line.linked_line_id and line.linked_line_id.order_id != line.order_id:
                raise UserError(_("A linked sale line must belong to the same quotation."))
            # The FK cascade ignores record rules. Inspect only relation identity
            # with elevation so a hidden legacy cross-order child cannot be lost.
            if any(child.order_id.id != line.order_id.id for child in line.sudo().linked_line_ids):
                raise UserError(_("Linked sale lines cannot cross quotations or companies."))

    def _portfolio_check_link_commands(self, values, order):
        for command in values.get("linked_line_ids", []):
            if command[0] in (0, 1) and "order_id" in command[2] and command[2]["order_id"] != order.id:
                raise UserError(_("Linked sale lines cannot cross quotations or companies."))
            if command[0] in (1, 2, 3, 4) and command[1]:
                children = self.browse(command[1])
            elif command[0] == 6:
                children = self.browse(command[2])
            else:
                continue
            children.check_access("write")
            if any(child.order_id != order for child in children):
                raise UserError(_("Linked sale lines cannot cross quotations or companies."))

    @api.model_create_multi
    def create(self, vals_list):
        defaults = self.env["ir.default"]._get_model_defaults(self._name)
        if "state" in defaults or "default_state" in self.env.context:
            raise AccessError(_("Sale line state cannot be supplied through defaults."))
        if "linked_line_ids" in defaults or "default_linked_line_ids" in self.env.context:
            raise AccessError(_("Linked sale line defaults are not supported."))
        derived = LINE_DERIVED_FINANCIAL | LINE_DERIVED_IDENTITY
        if derived.intersection(defaults) or any(
            f"default_{key}" in self.env.context for key in derived
        ):
            raise AccessError(_("Derived sale line values cannot be supplied through defaults."))
        if any(derived.intersection(values) for values in vals_list):
            raise AccessError(_("Derived sale line values cannot be supplied directly."))
        vals_list = [dict(values) for values in vals_list]
        defaults = self.default_get(["order_id", "linked_line_id"])
        default_order = defaults.get("order_id")
        default_linked_line = defaults.get("linked_line_id")
        # Freeze the effective ORM default before taking the parent lock; super()
        # must create under that same parent, rather than resolving defaults again.
        for values in vals_list:
            if "order_id" not in values:
                values["order_id"] = default_order
            if "linked_line_id" not in values:
                values["linked_line_id"] = default_linked_line
        for values in vals_list:
            self._portfolio_check_link_commands(values, self.env["sale.order"].browse(values["order_id"]))
            linked_line_id = values.get("linked_line_id")
            if linked_line_id:
                linked_line = self.browse(linked_line_id)
                linked_line.check_access("read")
                if not linked_line.exists() or linked_line.order_id != self.env["sale.order"].browse(values["order_id"]):
                    raise UserError(_("A linked sale line must belong to the same quotation."))
        order_ids = {values.get("order_id") for values in vals_list}
        orders = self.env["sale.order"].browse(sorted(order_ids - {None, False}))
        orders._portfolio_lock()
        orders._portfolio_mutable()
        orders._portfolio_invalidate()
        return super().create(vals_list)

    def write(self, values):
        if (LINE_DERIVED_FINANCIAL | LINE_DERIVED_IDENTITY).intersection(values):
            raise UserError(_("Derived sale line values are maintained by the system."))
        if not LINE_COMMERCIAL.intersection(values):
            return super().write(values)
        self.check_access("write")
        for line in self:
            self._portfolio_check_link_commands(values, line.order_id)
        linked_line = self.browse(values["linked_line_id"]) if values.get("linked_line_id") else self.browse()
        if linked_line:
            linked_line.check_access("read")
            if linked_line.order_id != self.order_id:
                raise UserError(_("A linked sale line must belong to the same quotation."))
        orders = self.order_id | self.env["sale.order"].browse(values.get("order_id", [])) | linked_line.order_id
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
        self._portfolio_check_linked_order()
        orders = self.order_id
        orders._portfolio_lock()
        self.invalidate_recordset()
        self.check_access("unlink")
        if self.order_id - orders:
            raise UserError(_("A line moved concurrently. Please retry."))
        orders._portfolio_mutable()
        orders._portfolio_invalidate()
        return super().unlink()
