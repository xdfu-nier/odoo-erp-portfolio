import math

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError


class ResCompany(models.Model):
    _inherit = "res.company"

    portfolio_approval_enabled = fields.Boolean(string="Sales approval policy", default=False)
    portfolio_masterdata_enabled = fields.Boolean(
        string="Industrial parts master-data validation", default=False,
    )
    portfolio_fulfillment_enabled = fields.Boolean(string="Order fulfillment policy", default=False)
    portfolio_stock_revision = fields.Integer(default=0, readonly=True, copy=False)
    portfolio_approval_amount = fields.Monetary(
        string="Approval untaxed amount", default=50000, currency_field="currency_id",
    )
    portfolio_approval_discount = fields.Float(string="Approval discount limit (%)", default=10)
    portfolio_approval_policy_version = fields.Integer(default=1, readonly=True, copy=False)

    @api.model
    def _portfolio_validate_values(self, values):
        # Monetary field conversion rounds before @constrains runs; reject NaN /
        # infinity here so callers get a business validation error, not OverflowError.
        for key, maximum in (("portfolio_approval_amount", None), ("portfolio_approval_discount", 100)):
            if key in values:
                value = float(values[key])
                if not math.isfinite(value) or value < 0 or (maximum is not None and value > maximum):
                    raise ValidationError(_("Approval thresholds must be finite and nonnegative; discount cannot exceed 100."))

    def _portfolio_lock_policy(self):
        """First lock in the shared protocol: companies, then parent sales orders.

        Read permission is sufficient to serialize a business operation on policy;
        actual policy mutations separately require settings and company write access.
        """
        self.check_access("read")
        if any(company.id not in self.env.companies.ids for company in self):
            raise AccessError(_("The approval company is outside your allowed companies."))
        if self:
            self.env.cr.execute(
                "SELECT id FROM res_company WHERE id IN %s ORDER BY id FOR UPDATE", [tuple(self.ids)],
            )
            self.invalidate_recordset()
            self.check_access("read")

    def _portfolio_lock_fulfillment(self):
        """Serialize stock facts without granting the business actor company writes.

        Native stock/accounting paths may use sudo, which keeps the original uid.
        Check that actor's actual membership and read access before locking; a
        caller-controlled allowed_company_ids must never expand their companies.
        Updating this technical column makes a stale repeatable-read contender
        fail serialization and enter Odoo's normal transaction retry protocol.
        """
        actor_companies = self.with_user(self.env.uid)
        if set(self.ids) - set(actor_companies.env.user.company_ids.ids):
            raise AccessError(_("The fulfillment company is outside your allowed companies."))
        actor_companies.check_access("read")
        if set(self.ids) - set(actor_companies.env.companies.ids):
            raise AccessError(_("The fulfillment company is outside your active companies."))
        self._portfolio_lock_policy()
        actor_companies.check_access("read")
        enabled = self.filtered("portfolio_fulfillment_enabled")
        if enabled:
            enabled.flush_recordset(["portfolio_stock_revision"])
            self.env.cr.execute(
                "UPDATE res_company SET portfolio_stock_revision = portfolio_stock_revision + 1 "
                "WHERE id IN %s", [tuple(enabled.ids)],
            )
            enabled.invalidate_recordset(["portfolio_stock_revision"])
            enabled.modified(["portfolio_stock_revision"])

    @api.constrains("portfolio_approval_amount", "portfolio_approval_discount")
    def _check_portfolio_policy(self):
        for company in self:
            amount, discount = company.portfolio_approval_amount, company.portfolio_approval_discount
            if not math.isfinite(amount) or amount < 0:
                raise ValidationError(_("Approval amount must be finite and nonnegative."))
            if not math.isfinite(discount) or not 0 <= discount <= 100:
                raise ValidationError(_("Approval discount must be between 0 and 100."))

    @api.model_create_multi
    def create(self, vals_list):
        controlled = {
            "portfolio_approval_enabled", "portfolio_approval_amount", "portfolio_approval_discount",
            "portfolio_masterdata_enabled", "portfolio_fulfillment_enabled",
        }
        protected = {"portfolio_approval_policy_version", "portfolio_stock_revision"}
        defaults = self.env["ir.default"]._get_model_defaults(self._name)
        if protected.intersection(defaults) or any(f"default_{key}" in self.env.context for key in protected):
            raise AccessError(_("Company policy revisions are maintained by the system."))
        if not self.env.user.has_group("base.group_system") and (
            controlled.intersection(defaults)
            or any(f"default_{key}" in self.env.context for key in controlled)
        ):
            raise AccessError(_("Only settings administrators can configure company policies."))
        for values in vals_list:
            self._portfolio_validate_values(values)
            if protected.intersection(values):
                raise AccessError(_("Company policy revisions are maintained by the system."))
            if controlled.intersection(values):
                if not self.env.user.has_group("base.group_system"):
                    raise AccessError(_("Only settings administrators can configure approval policy."))
        return super().create(vals_list)

    def write(self, values):
        if {"portfolio_approval_policy_version", "portfolio_stock_revision"}.intersection(values):
            raise AccessError(_("Company policy revisions are maintained by the system."))
        policy_fields = {
            "portfolio_approval_enabled", "portfolio_approval_amount", "portfolio_approval_discount",
            "currency_id", "portfolio_masterdata_enabled", "portfolio_fulfillment_enabled",
        }
        if not policy_fields.intersection(values):
            return super().write(values)
        if not self.env.user.has_group("base.group_system"):
            raise AccessError(_("Only settings administrators can configure approval policy."))
        self.check_access("write")
        self._portfolio_validate_values(values)
        self._portfolio_lock_policy()
        for company in self:
            updated = dict(values)
            if (policy_fields - {"portfolio_masterdata_enabled", "portfolio_fulfillment_enabled"}).intersection(values):
                updated["portfolio_approval_policy_version"] = company.portfolio_approval_policy_version + 1
            super(ResCompany, company).write(updated)
        return True
