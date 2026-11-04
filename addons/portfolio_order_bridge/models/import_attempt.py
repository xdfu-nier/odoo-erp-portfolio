"""Immutable actor-derived outcomes and explicit manual retry history."""

import json

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from .access import INTEGRATION, OPERATOR, ImportAccess
from .import_values import ImportBusinessError, retry_reason

ATTEMPT_GROUPS = f"{INTEGRATION},{OPERATOR}"


class ImportAttempt(ImportAccess, models.Model):
    _name = "portfolio.import.attempt"
    _description = "Immutable external order processing attempt"
    _rec_name = "job_id"
    _order = "id"
    _check_company_auto = True

    job_id = fields.Many2one("portfolio.import.job", required=True, readonly=True,
                             check_company=True, ondelete="restrict", index=True)
    company_id = fields.Many2one(related="job_id.company_id", store=True, readonly=True, index=True)
    attempt_no = fields.Integer(required=True, readonly=True)
    event_type = fields.Selection([("done", "Done"), ("failed", "Failed"), ("retry", "Manual retry")],
                                  required=True, readonly=True)
    actor_id = fields.Many2one("res.users", required=True, readonly=True, ondelete="restrict")
    started_at = fields.Datetime(required=True, readonly=True)
    finished_at = fields.Datetime(required=True, readonly=True)
    error_code = fields.Char(readonly=True, groups=ATTEMPT_GROUPS)
    error_path = fields.Char(readonly=True, groups=ATTEMPT_GROUPS)
    error_detail = fields.Text(readonly=True, groups=ATTEMPT_GROUPS)
    retry_reason = fields.Text(readonly=True, groups=ATTEMPT_GROUPS)
    mapping_snapshot = fields.Text(required=True, readonly=True, groups=ATTEMPT_GROUPS)
    source_defaults_snapshot = fields.Text(required=True, readonly=True, groups=ATTEMPT_GROUPS)

    _sql_constraints = [
        ("job_attempt_event_unique", "unique(job_id, attempt_no, event_type)",
         "An attempt outcome or retry event may only be recorded once."),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        raise AccessError(_("Import attempts are created only by the authorized processing service."))

    def write(self, vals):
        raise AccessError(_("Import attempts are immutable."))

    def unlink(self):
        raise AccessError(_("Import attempts are immutable."))

    def copy(self, default=None):
        raise AccessError(_("Import attempts cannot be copied."))

    def copy_data(self, default=None):
        raise AccessError(_("Import attempts cannot be copied."))

    @api.model
    def _append_event(self, job, event_type, snapshot, error=None, reason=None):
        actor = self._import_actor()
        if getattr(job, "_name", None) != "portfolio.import.job" or len(job) != 1:
            raise AccessError(_("The import job is unavailable or unauthorized."))
        job = actor.env["portfolio.import.job"].browse(job.id)._worker_lock_job()
        if event_type not in {"done", "failed", "retry"} or job.attempt_count < 1:
            raise AccessError(_("The attempt does not match a processing outcome."))
        expected_state = "failed" if event_type == "retry" else event_type
        if job.state != expected_state or (event_type == "done" and not job.sale_order_id):
            raise AccessError(_("The attempt does not match the current job state."))
        if actor.search_count([
            ("job_id", "=", job.id), ("attempt_no", "=", job.attempt_count),
            ("event_type", "=", event_type),
        ]):
            raise UserError(_("This attempt event has already been recorded."))
        if event_type == "failed" and not isinstance(error, ImportBusinessError):
            raise AccessError(_("Failed attempts require a safe processing diagnostic."))
        if event_type != "failed" and error is not None:
            raise AccessError(_("This event cannot contain a failure diagnostic."))
        if event_type == "retry":
            try:
                reason = retry_reason(reason)
            except ImportBusinessError as invalid:
                raise UserError(_("%(code)s: %(path)s", code=invalid.code, path=invalid.path)) from None
        elif reason is not None:
            raise AccessError(_("Only manual retry events accept a reason."))
        source = job.source_id
        defaults = {
            "source_id": source.id, "company_id": source.company_id.id,
            "warehouse_id": source.warehouse_id.id, "pricelist_id": source.pricelist_id.id,
            "sales_user_id": source.sales_user_id.id, "processor_user_id": source.processor_user_id.id,
        }
        # Copy only verified native identifiers. A private caller cannot turn the
        # snapshot parameter into an arbitrary text/credential persistence path.
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("mapping", {}), dict):
            raise AccessError(_("The resolved mapping snapshot is invalid."))
        supplied = {} if event_type == "retry" else snapshot.get("mapping", {})
        mapping = {"lines": []}
        if "customer" in supplied:
            customer = supplied["customer"]
            if not isinstance(customer, dict) or any(
                type(customer.get(key)) is not int for key in ("mapping_id", "partner_id")
            ):
                raise AccessError(_("The resolved customer snapshot is invalid."))
            customer_map = actor.env["portfolio.import.customer.map"].with_context(active_test=False).search([
                ("id", "=", customer["mapping_id"]), ("source_id", "=", source.id),
                ("partner_id", "=", customer["partner_id"]),
            ], limit=1)
            if not customer_map:
                raise AccessError(_("The resolved customer mapping is unavailable."))
            mapping["customer"] = {"mapping_id": customer_map.id, "partner_id": customer_map.partner_id.id}
        supplied_lines = supplied.get("lines", [])
        if not isinstance(supplied_lines, list) or len(supplied_lines) > 100:
            raise AccessError(_("The resolved product snapshot is invalid."))
        for item in supplied_lines:
            if not isinstance(item, dict) or any(
                type(item.get(key)) is not int
                for key in ("line_index", "mapping_id", "product_id", "uom_id")
            ) or not 0 <= item["line_index"] < 100:
                raise AccessError(_("The resolved product snapshot is invalid."))
            product_map = actor.env["portfolio.import.product.map"].with_context(active_test=False).search([
                ("id", "=", item["mapping_id"]), ("source_id", "=", source.id),
                ("product_id", "=", item["product_id"]),
            ], limit=1)
            if not product_map or product_map.product_id.sudo().with_context(active_test=False).uom_id.id != item["uom_id"]:
                raise AccessError(_("The resolved product mapping is unavailable."))
            mapping["lines"].append({key: item[key] for key in (
                "line_index", "mapping_id", "product_id", "uom_id",
            )})
        if event_type == "retry":
            previous = actor.search([
                ("job_id", "=", job.id), ("attempt_no", "=", job.attempt_count),
                ("event_type", "=", "failed"),
            ], limit=1)
            if not previous:
                raise AccessError(_("The failed attempt audit is missing."))
            # Repairing a mapping must not rewrite its preceding failure facts.
            mapping = json.loads(previous.mapping_snapshot)
            defaults = json.loads(previous.source_defaults_snapshot)
        now = fields.Datetime.now()
        # The trusted caller supplies only bounded resolved identifiers, never a
        # payload, credentials, exception text, or actor/company/count defaults.
        values = {
            "job_id": job.id, "attempt_no": job.attempt_count, "event_type": event_type,
            "actor_id": actor.env.uid, "started_at": now if event_type == "retry" else job.last_attempt_at,
            "finished_at": now, "retry_reason": reason or False,
            "error_code": error.code if error else False, "error_path": error.path if error else False,
            "error_detail": error.detail if error else False,
            "mapping_snapshot": json.dumps(mapping, sort_keys=True, allow_nan=False),
            "source_defaults_snapshot": json.dumps(defaults, sort_keys=True, allow_nan=False),
        }
        return super(ImportAccess, actor).create(values)
