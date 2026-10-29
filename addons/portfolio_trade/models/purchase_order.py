import contextvars

from odoo import _, api, fields, models
from odoo.exceptions import UserError

# Execution-local authorization for an exact native action, never Odoo context.
_PURCHASE_STATE_CAPABILITY = contextvars.ContextVar("portfolio_purchase_state", default=None)
_PROMOTED_STATES = {"to approve", "purchase", "done"}


class PurchaseOrder(models.Model):
    _inherit = "purchase.order"

    portfolio_source_sale_id = fields.Many2one(
        "sale.order", string="Source Sales Order", check_company=True,
        ondelete="restrict", copy=False, index=True,
        help="Informational source only; this does not reserve stock or create a procurement route.",
    )

    def _portfolio_masterdata_lock(self, extra_companies=None, extra_sales=None):
        """Shared order: company IDs, source SO IDs, then purchase IDs.

        Buyers have read-only sales access. Do not use sale._portfolio_lock,
        which requires sales write access and updates the approval revision.
        """
        if self:
            self.check_access("write")
        sources = (self.portfolio_source_sale_id | (extra_sales or self.env["sale.order"])).with_user(self.env.uid)
        if sources:
            sources.check_access("read")
        companies = self.company_id | (extra_companies or self.env["res.company"])
        companies |= sources.company_id
        companies._portfolio_lock_policy()
        if sources:
            self.env.cr.execute(
                "SELECT id FROM sale_order WHERE id IN %s ORDER BY id FOR UPDATE", [tuple(sources.ids)],
            )
            sources.invalidate_recordset()
            sources.check_access("read")
        if self:
            self.env.cr.execute(
                "SELECT id FROM purchase_order WHERE id IN %s ORDER BY id FOR UPDATE", [tuple(self.ids)],
            )
            self.invalidate_recordset()
            self.env["purchase.order.line"].invalidate_model()
            self.check_access("write")
            if self.company_id - companies:
                raise UserError(_("The purchase company changed concurrently. Please retry."))
            if self.portfolio_source_sale_id - sources:
                raise UserError(_("The purchase source changed concurrently. Please retry."))

    def _portfolio_validate_source(self, source, company, require_confirmed=True):
        if not source:
            return
        source = source.with_user(self.env.uid)
        source.check_access("read")
        if source.company_id != company:
            raise UserError(_("The source sales order must belong to the purchase company."))
        if require_confirmed and source.state != "sale":
            raise UserError(_("The source sales order must be confirmed."))

    def _portfolio_validate_sources(self, require_confirmed=True):
        for order in self:
            order._portfolio_validate_source(
                order.portfolio_source_sale_id, order.company_id, require_confirmed=require_confirmed,
            )

    def _portfolio_controlled(self):
        return self.filtered(lambda order: (
            order.company_id.portfolio_masterdata_enabled or order.company_id.portfolio_fulfillment_enabled
        ))

    @api.model_create_multi
    def create(self, vals_list):
        self.check_access("create")
        defaults = self.default_get(["company_id", "state", "portfolio_source_sale_id"])
        values_list = [{
            **values,
            "company_id": values.get("company_id", defaults.get("company_id", self.env.company.id)),
            "state": values.get("state", defaults.get("state", "draft")),
            "portfolio_source_sale_id": values.get(
                "portfolio_source_sale_id", defaults.get("portfolio_source_sale_id", False),
            ),
        } for values in vals_list]
        companies = self.env["res.company"].browse(sorted({
            values["company_id"] for values in values_list if values["company_id"]
        }))
        sources = self.env["sale.order"].browse(sorted({
            values["portfolio_source_sale_id"] for values in values_list if values["portfolio_source_sale_id"]
        }))
        self._portfolio_masterdata_lock(companies, sources)
        for values in values_list:
            company = companies.browse(values["company_id"])
            self._portfolio_validate_source(self.env["sale.order"].browse(values["portfolio_source_sale_id"]), company)
            if (
                company.portfolio_masterdata_enabled or company.portfolio_fulfillment_enabled
            ) and values["state"] in _PROMOTED_STATES:
                raise UserError(_("Create a purchase quotation, then use the standard confirmation actions."))
        return super().create(values_list)

    def write(self, values):
        if (
            values.get("state") not in _PROMOTED_STATES
            and not {"company_id", "portfolio_source_sale_id"}.intersection(values)
        ):
            return super().write(values)
        extra = self.env["res.company"].browse(values.get("company_id", []))
        candidate = self.env["sale.order"].browse(values.get("portfolio_source_sale_id", []))
        self._portfolio_masterdata_lock(extra, candidate)
        for order in self:
            if (
                "portfolio_source_sale_id" in values and order.state in _PROMOTED_STATES
                and order.portfolio_source_sale_id.id != values["portfolio_source_sale_id"]
            ):
                raise UserError(_("The purchase source cannot change after submission or confirmation."))
            source = candidate if "portfolio_source_sale_id" in values else order.portfolio_source_sale_id
            company = extra if "company_id" in values else order.company_id
            source_changed = "portfolio_source_sale_id" in values and source != order.portfolio_source_sale_id
            entering_confirmation = values.get("state") in _PROMOTED_STATES and order.state not in ("purchase", "done")
            order._portfolio_validate_source(
                source, company, require_confirmed=source_changed or entering_confirmation,
            )
        if "company_id" in values:
            for order in self:
                if (
                    order.company_id.id != values["company_id"] and order.state in _PROMOTED_STATES
                    and (
                        order.company_id.portfolio_masterdata_enabled or extra.portfolio_masterdata_enabled
                        or order.company_id.portfolio_fulfillment_enabled or extra.portfolio_fulfillment_enabled
                    )
                ):
                    raise UserError(_("Change the purchase company on a draft quotation before confirmation."))
        if values.get("state") not in _PROMOTED_STATES:
            return super().write(values)
        controlled = self._portfolio_controlled()
        if extra.portfolio_masterdata_enabled or extra.portfolio_fulfillment_enabled:
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
        # Closing/reopening an already confirmed PO keeps historical provenance,
        # even when its source sale has since been canceled.
        self._portfolio_validate_sources(require_confirmed=not confirmed_only)
        controlled = self._portfolio_controlled()
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
