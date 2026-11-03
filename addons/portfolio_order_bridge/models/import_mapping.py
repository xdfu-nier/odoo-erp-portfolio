"""Source-scoped external keys with explicitly repairable native targets."""

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .access import ImportAccess, safe_text


class ImportMapping(ImportAccess):
    _check_company_auto = True
    _order = "id"

    def _import_prepare_create(self, values):
        if "company_id" in values:
            raise UserError(_("Mapping company is derived from its source and cannot be supplied."))
        source = self.env["portfolio.import.source"].browse(values.get("source_id")).exists()
        if not source:
            raise ValidationError(_("Select an existing explicit import source."))
        source._import_read_actor()
        self._import_authorize_company(source.company_id.id)
        safe_text(self.env, values.get(self._external_key), self._external_key)
        if not values.get(self._target_field):
            raise ValidationError(_("Select an existing mapping target."))
        return values

    def _import_prepare_write(self, values):
        if {"source_id", "company_id"}.intersection(values):
            raise UserError(_("Mapping source and company cannot change after creation."))
        if self._external_key in values:
            safe_text(self.env, values[self._external_key], self._external_key)
        if self._target_field in values and not values[self._target_field]:
            raise ValidationError(_("Mapping target cannot be empty."))
        return values

    def _import_validate(self):
        self._validate_target()

    def _validate_target(self):
        for mapping in self.sudo():
            safe_text(mapping.env, mapping[mapping._external_key], mapping._external_key)
            target = mapping[mapping._target_field].exists()
            company = mapping.source_id.company_id
            if (
                not target or not target.active
                or (target.company_id and target.company_id != company)
            ):
                raise ValidationError(_("Mapping targets must be active and shared with or belong to the source company."))
            if mapping._target_field == "partner_id":
                commercial = target.commercial_partner_id
                if not commercial.active or (commercial.company_id and commercial.company_id != company):
                    raise ValidationError(_("The commercial customer must be active and compatible with the source company."))


class ImportCustomerMapping(ImportMapping, models.Model):
    _name = "portfolio.import.customer.map"
    _description = "External source customer mapping"
    _rec_name = "external_ref"
    _external_key = "external_ref"
    _target_field = "partner_id"

    active = fields.Boolean(default=True)
    source_id = fields.Many2one(
        "portfolio.import.source", required=True, readonly=True, check_company=True, ondelete="restrict",
    )
    company_id = fields.Many2one(related="source_id.company_id", store=True, readonly=True)
    external_ref = fields.Char(required=True, index=True)
    partner_id = fields.Many2one("res.partner", required=True, check_company=True, ondelete="restrict")

    _sql_constraints = [
        ("source_customer_unique", "unique(source_id, external_ref)", "External customer must be unique per source."),
    ]

    @api.constrains("source_id", "external_ref", "partner_id")
    def _check_customer_target(self):
        self._validate_target()


class ImportProductMapping(ImportMapping, models.Model):
    _name = "portfolio.import.product.map"
    _description = "External source product mapping"
    _rec_name = "external_sku"
    _external_key = "external_sku"
    _target_field = "product_id"

    active = fields.Boolean(default=True)
    source_id = fields.Many2one(
        "portfolio.import.source", required=True, readonly=True, check_company=True, ondelete="restrict",
    )
    company_id = fields.Many2one(related="source_id.company_id", store=True, readonly=True)
    external_sku = fields.Char(required=True, index=True)
    product_id = fields.Many2one("product.product", required=True, check_company=True, ondelete="restrict")

    _sql_constraints = [
        ("source_product_unique", "unique(source_id, external_sku)", "External SKU must be unique per source."),
    ]

    @api.constrains("source_id", "external_sku", "product_id")
    def _check_product_target(self):
        self._validate_target()
