import contextvars

from odoo import _, api, models
from odoo.exceptions import UserError

# Execution-local authorization for an exact native action, never Odoo context.
_PURCHASE_STATE_CAPABILITY = contextvars.ContextVar("portfolio_purchase_state", default=None)
_PROMOTED_STATES = {"to approve", "purchase", "done"}


class PurchaseOrder(models.Model):
    _inherit = "purchase.order"

    def _portfolio_masterdata_lock(self, extra_companies=None):
        self.check_access("write")
        companies = self.company_id | (extra_companies or self.env["res.company"])
        companies._portfolio_lock_policy()
        if self:
            self.env.cr.execute(
                "SELECT id FROM purchase_order WHERE id IN %s ORDER BY id FOR UPDATE", [tuple(self.ids)],
            )
            self.invalidate_recordset()
            self.env["purchase.order.line"].invalidate_model()
            self.check_access("write")
            if self.company_id - companies:
                raise UserError(_("The purchase company changed concurrently. Please retry."))

    @api.model_create_multi
    def create(self, vals_list):
        defaults = self.default_get(["company_id", "state"])
        values_list = [{
            **values,
            "company_id": values.get("company_id", defaults.get("company_id", self.env.company.id)),
            "state": values.get("state", defaults.get("state", "draft")),
        } for values in vals_list]
        companies = self.env["res.company"].browse(sorted({
            values["company_id"] for values in values_list if values["company_id"]
        }))
        companies._portfolio_lock_policy()
        for values in values_list:
            company = companies.browse(values["company_id"])
            if company.portfolio_masterdata_enabled and values["state"] in _PROMOTED_STATES:
                raise UserError(_("Create a purchase quotation, then use the standard confirmation actions."))
        return super().create(values_list)

    def write(self, values):
        if values.get("state") not in _PROMOTED_STATES and "company_id" not in values:
            return super().write(values)
        extra = self.env["res.company"].browse(values.get("company_id", []))
        self._portfolio_masterdata_lock(extra)
        if "company_id" in values:
            for order in self:
                if (
                    order.company_id.id != values["company_id"] and order.state in _PROMOTED_STATES
                    and (order.company_id.portfolio_masterdata_enabled or extra.portfolio_masterdata_enabled)
                ):
                    raise UserError(_("Change the purchase company on a draft quotation before confirmation."))
        if values.get("state") not in _PROMOTED_STATES:
            return super().write(values)
        controlled = self.filtered(lambda order: order.company_id.portfolio_masterdata_enabled)
        if extra.portfolio_masterdata_enabled:
            controlled = self
        if controlled:
            capability = _PURCHASE_STATE_CAPABILITY.get()
            authorized = (
                capability and capability[0] is self.env.cr and capability[1] == self.env.uid
                and capability[2].issuperset(self.ids) and values["state"] in capability[3]
            )
            if not authorized:
                raise UserError(_("Use the standard purchase confirmation, approval, lock or unlock actions."))
            # The gate validated these exact commercial values. Native state
            # writes cannot combine confirmation with a new partner or lines.
            if set(values) - {"state", "date_approve", "priority"}:
                raise UserError(_("Change purchase content before using the standard confirmation actions."))
            self.env["portfolio.master.data"]._validate_orders(controlled, "vendor")
        return super().write(values)

    def _portfolio_purchase_action(self, states, action, confirmed_only=False):
        self._portfolio_masterdata_lock()
        controlled = self.filtered(lambda order: order.company_id.portfolio_masterdata_enabled)
        if confirmed_only and any(order.state not in ("purchase", "done") for order in controlled):
            raise UserError(_("Confirm the purchase order before locking or unlocking it."))
        self.env["portfolio.master.data"]._validate_orders(self, "vendor")
        token = _PURCHASE_STATE_CAPABILITY.set((self.env.cr, self.env.uid, frozenset(self.ids), frozenset(states)))
        try:
            return action()
        finally:
            _PURCHASE_STATE_CAPABILITY.reset(token)

    def button_confirm(self):
        return self._portfolio_purchase_action(
            {"to approve"}, lambda: super(PurchaseOrder, self).button_confirm(),
        )

    def button_approve(self, force=False):
        return self._portfolio_purchase_action(
            {"purchase", "done"}, lambda: super(PurchaseOrder, self).button_approve(force=force),
        )

    def button_unlock(self):
        return self._portfolio_purchase_action(
            {"purchase"}, lambda: super(PurchaseOrder, self).button_unlock(), confirmed_only=True,
        )

    def button_done(self):
        return self._portfolio_purchase_action(
            {"done"}, lambda: super(PurchaseOrder, self).button_done(), confirmed_only=True,
        )
