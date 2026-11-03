"""Immutable external source identities and revalidatable native order defaults."""

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .access import (
    CONFIG_GROUPS,
    FORBIDDEN_GROUPS,
    INTEGRATION,
    OPERATOR,
    SALES,
    ImportAccess,
    safe_text,
)


class ImportSource(ImportAccess, models.Model):
    _name = "portfolio.import.source"
    _description = "Authorized external order source"
    _check_company_auto = True
    _order = "id"

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one("res.company", required=True, readonly=True, ondelete="restrict")
    company_code = fields.Char(required=True, readonly=True, index=True)
    code = fields.Char(required=True, readonly=True, index=True)
    pricelist_id = fields.Many2one(
        "product.pricelist", required=True, check_company=True, groups=CONFIG_GROUPS, ondelete="restrict",
    )
    warehouse_id = fields.Many2one(
        "stock.warehouse", required=True, check_company=True, groups=CONFIG_GROUPS, ondelete="restrict",
    )
    sales_user_id = fields.Many2one(
        "res.users", required=True, check_company=True, groups=CONFIG_GROUPS, ondelete="restrict",
    )
    processor_user_id = fields.Many2one(
        "res.users", required=True, check_company=True, groups=CONFIG_GROUPS, ondelete="restrict",
    )
    integration_user_ids = fields.Many2many("res.users", check_company=True, groups=CONFIG_GROUPS)

    _sql_constraints = [
        ("company_source_unique", "unique(company_id, code)", "Source code must be unique per company."),
        ("external_source_unique", "unique(company_code, code)", "External company and source must be unique."),
    ]

    def _import_prepare_create(self, values):
        values.setdefault("company_id", self.env.company.id)
        self._import_authorize_company(values["company_id"])
        self._validate_text_values(values)
        for name in ("pricelist_id", "warehouse_id", "sales_user_id", "processor_user_id"):
            if not values.get(name):
                raise ValidationError(_("A source requires a pricelist, warehouse, salesperson and processor."))
        return values

    def _import_prepare_write(self, values):
        if {"company_id", "company_code", "code"}.intersection(values):
            raise UserError(_("Source company and external identity cannot change after creation."))
        self._validate_text_values(values, partial=True)
        for name in ("pricelist_id", "warehouse_id", "sales_user_id", "processor_user_id"):
            if name in values and not values[name]:
                raise ValidationError(_("Source configuration fields cannot be empty."))
        return values

    @api.model
    def _validate_text_values(self, values, partial=False):
        for name, maximum in (("name", 128), ("company_code", 64), ("code", 64)):
            if not partial or name in values:
                safe_text(self.env, values.get(name), name, maximum)

    def _import_validate(self):
        self._validate_configuration()

    @api.constrains(
        "name", "company_id", "company_code", "code", "pricelist_id", "warehouse_id",
        "sales_user_id", "processor_user_id", "integration_user_ids",
    )
    def _validate_configuration(self):
        # Private: callers at ingress/worker re-run this after native master data changes.
        # X2many conversion otherwise filters archived users before we can reject them.
        for source in self.sudo().with_context(active_test=False):
            source._validate_text_values({
                "name": source.name, "company_code": source.company_code, "code": source.code,
            })
            company = source.company_id
            pricelist = source.pricelist_id.exists()
            warehouse = source.warehouse_id.exists()
            if not company or company.currency_id.name != "CNY":
                raise ValidationError(_("Import sources require a company with CNY base currency."))
            if (
                not pricelist or not pricelist.active or pricelist.currency_id.name != "CNY"
                or (pricelist.company_id and pricelist.company_id != company)
            ):
                raise ValidationError(_("Use an active CNY pricelist shared with or belonging to the source company."))
            if not warehouse or not warehouse.active or warehouse.company_id != company:
                raise ValidationError(_("Use an active warehouse belonging to the source company."))
            salesperson = source.sales_user_id.exists()
            if (
                not salesperson or not salesperson.active or salesperson.share
                or not salesperson.has_group(SALES) or company not in salesperson.company_ids
            ):
                raise ValidationError(_("The salesperson must be an active internal Sales user in the source company."))
            processor = source.processor_user_id.exists()
            if (
                not processor or not processor.active or processor.share or processor == salesperson
                or not processor.has_group(OPERATOR) or not processor.has_group(SALES)
                or processor.company_ids != company
                or any(processor.has_group(group) for group in FORBIDDEN_GROUPS)
            ):
                raise ValidationError(_("Use a separate, active single-company import processor with only basic Sales privileges."))
            for user in source.integration_user_ids:
                if (
                    not user.active or user.share or not user.has_group(INTEGRATION)
                    or company not in user.company_ids
                    or any(user.has_group(group) for group in (*FORBIDDEN_GROUPS, SALES))
                ):
                    raise ValidationError(_("Integration users must be active internal import users in the company without business privileges."))
