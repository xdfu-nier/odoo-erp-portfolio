"""Native console actions preserve the existing current-actor service boundaries."""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from .access import OPERATOR, SETTINGS, ImportAccess
from .import_job import AUDIT_GROUPS

PROCESSING_CODE = "model._process_pending()"


class ImportSourceUI(models.Model):
    _inherit = "portfolio.import.source"

    company_id = fields.Many2one(default=lambda self: self.env.company)
    processing_cron_id = fields.Many2one(
        "ir.cron", string="Processing schedule / 处理计划", readonly=True,
        groups=SETTINGS, ondelete="restrict", copy=False,
    )

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        if "processing_cron_id" in fields_list:
            values["processing_cron_id"] = False
        return values

    def _import_prepare_create(self, values):
        if "processing_cron_id" in values:
            raise AccessError(_("The processing schedule is prepared only by its Settings action."))
        values = super()._import_prepare_create(values)
        return {**values, "processing_cron_id": False}

    def _import_prepare_write(self, values):
        if "processing_cron_id" in values:
            raise AccessError(_("The processing schedule link is server-controlled."))
        return super()._import_prepare_write(values)

    def _ui_settings_source(self, active=False):
        actor = self._import_settings_actor()
        if len(actor) != 1:
            raise AccessError(_("Select exactly one authorized import source."))
        source = actor.with_context(active_test=False).search([
            ("id", "=", actor.id), ("company_id", "in", actor.env.companies.ids),
        ], limit=1)
        if not source or (active and not source.active):
            raise AccessError(_("The import source is unavailable or unauthorized."))
        source.check_access("read")
        source._import_authorize_company(source.company_id.id)
        return source

    def _ui_mapping_action(self, model, title):
        actor = self._import_actor()
        if actor.env.user.has_group(SETTINGS):
            source = actor._ui_settings_source()
        elif actor.env.user.has_group(OPERATOR):
            _worker, source = actor.env["portfolio.import.job"]._worker_authorized_source(actor)
        else:
            raise AccessError(_("Mapping maintenance requires Settings or the assigned operator."))
        source.env[model].check_access("read")
        return {
            "type": "ir.actions.act_window", "name": title,
            "res_model": model, "view_mode": "list,form",
            "domain": [("source_id", "=", source.id)],
            "context": {"default_source_id": source.id},
        }

    def action_open_customer_mappings(self):
        return self._ui_mapping_action(
            "portfolio.import.customer.map", _("Customer mappings / 客户映射"),
        )

    def action_open_product_mappings(self):
        return self._ui_mapping_action(
            "portfolio.import.product.map", _("Product mappings / 产品映射"),
        )

    def _ui_validate_processing_cron(self, cron):
        source = self._ui_settings_source(active=True)
        if (
            getattr(cron, "_name", None) != "ir.cron" or len(cron) != 1
            or cron.id != source.processing_cron_id.id
        ):
            raise AccessError(_("Only this source's linked processing schedule can be validated."))
        # Supplied recordsets never convey authority or a foreign environment.
        cron = source.env["ir.cron"].with_context(active_test=False).browse(cron.id)
        cron.check_access("read")
        if (
            not cron.exists() or cron.model_id.model != "portfolio.import.job"
            or cron.state != "code" or cron.code != PROCESSING_CODE
            or cron.user_id != source.processor_user_id
            or cron.interval_number != 1 or cron.interval_type != "minutes"
        ):
            raise UserError(_(
                "The linked processing schedule no longer matches this source. "
                "Review its model, code, processor and interval in Scheduled Actions."
            ))

    def action_prepare_processing_cron(self):
        source = self._ui_settings_source(active=True)
        with source.env.cr.savepoint():
            # The schedule FK and native configuration checks share the existing
            # company -> source lock order used by ingress and processing.
            source.company_id._portfolio_lock_policy()
            source.flush_recordset()
            source.env.cr.execute(
                "SELECT id FROM portfolio_import_source WHERE id = %s FOR UPDATE", [source.id],
            )
            source.invalidate_recordset()
            source = source._ui_settings_source(active=True)
            users = source.sales_user_id | source.processor_user_id | source.integration_user_ids
            users.invalidate_recordset(["active", "share", "groups_id", "company_id", "company_ids"])
            source.company_id.invalidate_recordset(["currency_id"])
            source.pricelist_id.invalidate_recordset(["active", "currency_id", "company_id"])
            source.warehouse_id.invalidate_recordset(["active", "company_id"])
            source._validate_configuration()
            source.env["portfolio.import.job"].with_user(source.processor_user_id).with_context(
                allowed_company_ids=source.company_id.ids,
            )._worker_authorized_source(source)
            cron = source.processing_cron_id.with_context(active_test=False)
            if cron:
                source._ui_validate_processing_cron(cron)
            else:
                # Use native Settings rights with a fixed body and explicit
                # inactive state. Preparing never enables background services.
                cron = source.env["ir.cron"].create({
                    "name": _("Order import / 订单导入: %(source_name)s", source_name=source.name),
                    "model_id": source.env["ir.model"]._get("portfolio.import.job").id,
                    "state": "code", "code": PROCESSING_CODE,
                    "user_id": source.processor_user_id.id,
                    "interval_number": 1, "interval_type": "minutes", "active": False,
                })
                super(ImportAccess, source).write({"processing_cron_id": cron.id})
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {
                "title": _("Processing schedule / 处理计划"),
                "message": _("Schedule prepared. Its current enabled state is preserved."),
                "type": "success", "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "reload"},
            },
        }


class ImportJobUI(models.Model):
    _inherit = "portfolio.import.job"

    attempt_ids = fields.One2many(
        "portfolio.import.attempt", "job_id", string="History / 处理记录",
        readonly=True, groups=AUDIT_GROUPS, copy=False,
    )

    def action_process_pending(self):
        actor = self._worker_actor()
        # A form call carries IDs; authorize every selected record before the
        # fixed-size service can process anything from the actor's own queue.
        for job in actor:
            job._worker_authorized_job()
        count = actor._process_pending()
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {
                "title": _("Order import / 订单导入"),
                "message": _("Processed %(count)s pending import jobs.", count=count),
                "type": "success", "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "reload"},
            },
        }

    def action_open_retry(self):
        job = self._worker_authorized_job()
        if job.state != "failed":
            raise UserError(_("Only a failed import job can be retried manually."))
        return {
            "type": "ir.actions.act_window", "name": _("Retry import / 重试导入"),
            "res_model": "portfolio.import.retry", "view_mode": "form", "target": "new",
            "views": [(job.env.ref("portfolio_order_bridge.view_import_retry_form").id, "form")],
            "context": {"default_job_id": job.id},
        }

    def action_open_sale_order(self):
        job = self._import_read_actor()
        if len(job) != 1:
            raise AccessError(_("Select exactly one authorized import job."))
        if job.state != "done" or not job.sale_order_id:
            raise UserError(_("This import job has no completed quotation."))
        quote = job.env["sale.order"].browse(job.sale_order_id.id)
        quote.check_access("read")
        return {
            "type": "ir.actions.act_window", "name": _("Quotation / 报价单"),
            "res_model": "sale.order", "res_id": quote.id, "view_mode": "form",
        }
