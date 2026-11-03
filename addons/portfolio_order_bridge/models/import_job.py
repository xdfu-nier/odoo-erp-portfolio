"""Immutable reception audit; processing belongs to the later worker boundary."""

import json
from decimal import Decimal

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from ..payload import PayloadValidationError, normalize_order_payload
from .access import CONFIG_GROUPS, INTEGRATION, OPERATOR, ImportAccess

AUDIT_GROUPS = f"{INTEGRATION},{OPERATOR}"


class ImportSourceIngress(models.Model):
    _inherit = "portfolio.import.source"

    ingress_revision = fields.Integer(default=0, readonly=True, groups=CONFIG_GROUPS, copy=False)

    def _import_prepare_create(self, values):
        if "ingress_revision" in values:
            raise AccessError(_("The import source revision is server-controlled."))
        values = super()._import_prepare_create(values)
        # Explicitly defeat both caller context defaults and ir.default values.
        return {**values, "ingress_revision": 0}

    def _import_prepare_write(self, values):
        if "ingress_revision" in values:
            raise AccessError(_("The import source revision is server-controlled."))
        return super()._import_prepare_write(values)


class ImportJob(ImportAccess, models.Model):
    _name = "portfolio.import.job"
    _description = "Immutable external order reception"
    _rec_name = "external_order_id"
    _order = "id"
    _check_company_auto = True

    source_id = fields.Many2one(
        "portfolio.import.source", required=True, readonly=True, check_company=True,
        ondelete="restrict", index=True,
    )
    company_id = fields.Many2one(
        "res.company", required=True, readonly=True, ondelete="restrict", index=True,
    )
    external_order_id = fields.Char(required=True, readonly=True, index=True)
    payload_hash = fields.Char(required=True, readonly=True, groups=AUDIT_GROUPS)
    raw_payload = fields.Text(required=True, readonly=True, groups=AUDIT_GROUPS)
    canonical_payload = fields.Text(required=True, readonly=True, groups=AUDIT_GROUPS)
    submitted_by_id = fields.Many2one(
        "res.users", required=True, readonly=True, ondelete="restrict",
    )
    received_at = fields.Datetime(required=True, readonly=True, default=fields.Datetime.now)
    state = fields.Selection([
        ("pending", "Pending"), ("processing", "Processing"), ("done", "Done"),
        ("failed", "Failed"),
    ], required=True, readonly=True, default="pending", index=True)
    attempt_count = fields.Integer(required=True, readonly=True, default=0)
    last_attempt_at = fields.Datetime(readonly=True)
    error_code = fields.Char(readonly=True, groups=AUDIT_GROUPS)
    error_detail = fields.Text(readonly=True, groups=AUDIT_GROUPS)
    sale_order_id = fields.Many2one(
        "sale.order", readonly=True, check_company=True, ondelete="restrict",
    )

    _sql_constraints = [
        ("order_identity_unique", "unique(company_id, source_id, external_order_id)",
         "An external order may be received only once per company and source."),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        raise AccessError(_("Import jobs can only be created through submit_order."))

    def write(self, vals):
        raise AccessError(_("Import jobs are server-controlled."))

    def unlink(self):
        raise AccessError(_("Import jobs are immutable audit records."))

    def copy(self, default=None):
        raise AccessError(_("Import jobs cannot be copied."))

    def copy_data(self, default=None):
        raise AccessError(_("Import jobs cannot be copied."))

    def _ingress_actor(self):
        actor = self._import_actor()
        if not actor.env.user.has_group(INTEGRATION):
            raise AccessError(_("Order reception requires an active internal integration user."))
        return actor

    def _ingress_source(self, data):
        """Authorize identity and membership before reading any business defaults."""
        actor = self._ingress_actor()
        source = actor.env["portfolio.import.source"].with_context(active_test=False).search([
            ("company_code", "=", data["company_code"]), ("code", "=", data["source_code"]),
            ("company_id", "in", actor.env.companies.ids),
        ], limit=1)
        _actor, source = actor._ingress_authorized_source(source)
        return source

    def _ingress_authorized_source(self, source):
        """Rebuild a private capability from the actual actor and supplied ID."""
        actor = self._ingress_actor()
        if getattr(source, "_name", None) != "portfolio.import.source" or len(source) != 1:
            raise AccessError(_("The import source is unavailable or unauthorized."))
        # Discard the supplied recordset's sudo/user/company context before use.
        source = actor.env["portfolio.import.source"].with_context(active_test=False).search([
            ("id", "=", source.id), ("company_id", "in", actor.env.companies.ids),
        ], limit=1)
        # The bounded membership check also closes group-rule unions for actors
        # holding both Settings and Integration. No configuration is exposed by RPC.
        if (
            not source or not source.active
            or actor.env.uid not in source.sudo().with_context(active_test=False).integration_user_ids.ids
        ):
            raise AccessError(_("The import source is unavailable or unauthorized."))
        actor._import_authorize_company(source.company_id.id)
        return actor, source

    def _ingress_lock(self, source):
        """Write the authorized source row so stale snapshots fail natively."""
        actor, source = self._ingress_authorized_source(source)
        source.flush_recordset()
        actor.env.cr.execute("""
            UPDATE portfolio_import_source
               SET ingress_revision = ingress_revision + 1
             WHERE id = %s
         RETURNING ingress_revision
        """, [source.id])
        if not actor.env.cr.fetchone():
            raise AccessError(_("The import source is unavailable or unauthorized."))
        source.invalidate_recordset()

    def _ingress_configuration(self, source, data):
        actor, source = self._ingress_authorized_source(source)
        # This is a bounded technical read only after explicit source authorization.
        configured = source.sudo().with_context(active_test=False)
        configured.company_id.invalidate_recordset(["currency_id"])
        configured.pricelist_id.invalidate_recordset(["active", "currency_id", "company_id"])
        configured.warehouse_id.invalidate_recordset(["active", "company_id"])
        users = configured.sales_user_id | configured.processor_user_id | configured.integration_user_ids
        users.invalidate_recordset(["active", "share", "groups_id", "company_id", "company_ids"])
        try:
            configured._validate_configuration()
        except ValidationError:
            raise UserError(_("invalid_configuration: source")) from None
        currency = configured.company_id.currency_id
        if data["currency"] != currency.name or data["currency"] != configured.pricelist_id.currency_id.name:
            raise UserError(_("currency_mismatch: currency"))
        rounding = Decimal(str(currency.rounding))
        discount_digits = actor.env["decimal.precision"].precision_get("Discount")
        discount_step = Decimal(1).scaleb(-discount_digits)
        for index, line in enumerate(data["lines"]):
            if Decimal(line["unit_price"]) % rounding:
                raise UserError(_("currency_precision: lines[%(index)s].unit_price", index=index))
            if Decimal(line["discount"]) % discount_step:
                raise UserError(_("discount_precision: lines[%(index)s].discount", index=index))

    def _ingress_result(self, reused):
        record = self._import_read_actor()
        record.ensure_one()
        # Return only the foreign key, never a sale order read or credentials.
        return {
            "job_id": record.id, "state": record.state,
            "sale_order_id": record.sale_order_id.id or False, "reused": reused,
        }

    @api.model
    def submit_order(self, payload):
        actor = self._ingress_actor()
        # Savepoint ownership includes the technical revision, all validation and
        # native ORM creation. Real serialization failures propagate to RPC retrying.
        with actor.env.cr.savepoint():
            try:
                normalized = normalize_order_payload(payload)
            except PayloadValidationError as error:
                raise UserError(_("%(code)s: %(path)s", code=error.code, path=error.path)) from None
            data = normalized.as_dict()
            source = actor._ingress_source(data)
            actor._ingress_lock(source)
            actor = actor._ingress_actor()
            source = actor._ingress_source(data)
            actor._ingress_configuration(source, json.loads(normalized.raw_json))
            existing = actor.search([
                ("company_id", "=", source.company_id.id), ("source_id", "=", source.id),
                ("external_order_id", "=", data["external_order_id"]),
            ], limit=1)
            if existing:
                if existing.payload_hash != normalized.payload_hash:
                    raise UserError(_("order_conflict: external_order_id"))
                return existing._ingress_result(reused=True)
            # Skip the public job/configuration CRUD overrides only after the
            # private role, source, current defaults and precision checks above.
            job = super(ImportAccess, actor).create({
                "source_id": source.id, "company_id": source.company_id.id,
                "external_order_id": data["external_order_id"],
                "payload_hash": normalized.payload_hash, "raw_payload": normalized.raw_json,
                "canonical_payload": normalized.canonical_json,
                "submitted_by_id": actor.env.uid, "received_at": fields.Datetime.now(),
                "state": "pending", "attempt_count": 0, "last_attempt_at": False,
                "error_code": False, "error_detail": False, "sale_order_id": False,
            })
            return job._ingress_result(reused=False)
