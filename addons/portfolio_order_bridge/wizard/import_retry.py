"""A creator-owned retry form that rechecks its current job on every read or apply."""

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import AccessError, UserError

from ..models.access import ImportAccess
from ..models.import_values import ImportBusinessError, retry_reason


class ImportRetry(ImportAccess, models.TransientModel):
    _name = "portfolio.import.retry"
    _description = "Retry external order import / 重试外部订单导入"
    _check_company_auto = True

    job_id = fields.Many2one(
        "portfolio.import.job", string="Import job / 导入任务", required=True, readonly=True,
        check_company=True, ondelete="cascade",
    )
    company_id = fields.Many2one(related="job_id.company_id", store=True, readonly=True)
    reason = fields.Text(string="Repair reason / 修复说明", required=True)

    def _import_actor(self):
        actor = super()._import_actor()
        # Queries and aggregation must require the same current operator
        # eligibility as record reads, not merely a remaining model ACL.
        actor.env["portfolio.import.job"]._worker_actor()
        return actor

    def _retry_actor(self):
        return self._import_actor()

    def _retry_job(self, supplied):
        actor = self._retry_actor()
        if getattr(supplied, "_name", None) == "portfolio.import.job" and len(supplied) == 1:
            supplied = supplied.id
        if type(supplied) is not int or supplied < 1:
            raise AccessError(_("Select one explicit authorized failed import job."))
        job = actor.env["portfolio.import.job"].browse(supplied)._worker_authorized_job()
        if job.state != "failed":
            raise UserError(_("Only a currently failed import job can be retried."))
        return job

    @api.model
    def _retry_reason(self, reason):
        try:
            return retry_reason(reason)
        except ImportBusinessError as invalid:
            raise UserError(_("%(code)s: %(path)s", code=invalid.code, path=invalid.path)) from None

    def _retry_authorized(self):
        actor = self._retry_actor()
        actor.check_access("read")
        for wizard in actor:
            # Native onchange reads a NewId before create_uid exists. Its job
            # still passes the same current actor/source/company/state checks.
            if wizard._origin.id and wizard._origin.create_uid.id != actor.env.uid:
                raise AccessError(_("A retry form is available only to its creator."))
            actor._retry_job(wizard.job_id.id)
        return actor

    def _import_read_actor(self):
        return self._retry_authorized()

    @api.model_create_multi
    def create(self, vals_list):
        actor = self._retry_actor()
        with actor.env.cr.savepoint():
            values = []
            for supplied in vals_list:
                if set(supplied) - {"job_id", "reason"}:
                    raise AccessError(_("Only the explicit import job and repair reason may be supplied."))
                if "job_id" not in supplied:
                    raise AccessError(_("An explicit import job is required."))
                job = actor._retry_job(supplied["job_id"])
                values.append({"job_id": job.id, "reason": actor._retry_reason(supplied.get("reason"))})
            return super(ImportAccess, actor).create(values)

    def write(self, values):
        actor = self._retry_authorized()
        if set(values) - {"reason"}:
            raise AccessError(_("A retry form's job, owner and company cannot be changed."))
        clean = {"reason": actor._retry_reason(values["reason"])} if "reason" in values else {}
        with actor.env.cr.savepoint():
            actor.check_access("write")
            return super(ImportAccess, actor).write(clean)

    def unlink(self):
        # Native transient vacuum runs with the actual SUPERUSER_ID. Ordinary
        # users retaining sudo still need current role, source and ownership.
        if self.env.uid == SUPERUSER_ID and self.env.su:
            return super(ImportAccess, self).unlink()
        actor = self._retry_authorized()
        actor.check_access("unlink")
        return super(ImportAccess, actor).unlink()

    def action_apply(self):
        actor = self._retry_authorized()
        if len(actor) != 1:
            raise AccessError(_("Select exactly one authorized retry form."))
        reason = actor._retry_reason(actor.reason)
        job = actor._retry_job(actor.job_id.id)
        job.action_retry(reason)
        return {"type": "ir.actions.act_window_close", "infos": {"reload": True}}
