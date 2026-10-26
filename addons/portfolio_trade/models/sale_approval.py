from odoo import _, api, fields, models
from odoo.exceptions import AccessError


class SaleApproval(models.Model):
    _name = "portfolio.sale.approval"
    _description = "Sales approval immutable event"
    _order = "id"
    _check_company_auto = True

    order_id = fields.Many2one("sale.order", required=True, ondelete="restrict", check_company=True, index=True)
    company_id = fields.Many2one("res.company", required=True, index=True)
    event = fields.Selection([
        ("submitted", "Submitted"), ("approved", "Approved"),
        ("rejected", "Rejected"), ("invalidated", "Invalidated"),
    ], required=True)
    actor_id = fields.Many2one("res.users", required=True, ondelete="restrict")
    occurred_at = fields.Datetime(required=True, default=fields.Datetime.now)
    reason = fields.Text()
    snapshot = fields.Text(required=True)
    fingerprint = fields.Char(required=True)
    policy_version = fields.Integer(required=True)

    @api.model_create_multi
    def create(self, vals_list):
        raise AccessError(_("Approval history can only be created by approval actions."))

    def write(self, values):
        raise AccessError(_("Approval history is immutable."))

    def unlink(self):
        raise AccessError(_("Approval history is immutable."))

    @api.model
    def _append_event(self, values):
        # Private (not callable through RPC). Callers first authorize and lock the
        # order. Elevation is limited to this append-only audit creation.
        return super(SaleApproval, self.sudo()).create(values)


class SaleApprovalReject(models.TransientModel):
    _name = "portfolio.sale.approval.reject"
    _description = "Sales approval rejection reason"

    order_id = fields.Many2one("sale.order", required=True)
    reason = fields.Text(required=True)

    def action_reject(self):
        self.ensure_one()
        self.order_id.action_portfolio_reject(self.reason)
        return {"type": "ir.actions.act_window_close"}
