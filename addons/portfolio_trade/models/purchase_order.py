from odoo import _, models
from odoo.exceptions import UserError


class PurchaseOrder(models.Model):
    _inherit = "purchase.order"

    def button_confirm(self):
        self.check_access("write")
        companies = self.company_id
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
        self.env["portfolio.master.data"]._validate_orders(self, "vendor")
        return super().button_confirm()
