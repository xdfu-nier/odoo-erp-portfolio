"""Actor-scoped pending jobs become ordinary native draft sales quotations."""

import json
from decimal import Decimal, localcontext

from odoo import Command, _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from ..payload import PayloadValidationError, normalize_order_payload
from .access import FORBIDDEN_GROUPS, OPERATOR, SALES, ImportAccess
from .import_values import (
    ImportBusinessError,
    check_precision,
    exact_float,
    retry_reason,
    rounded_amount,
)


class ImportJobWorker(models.Model):
    _inherit = "portfolio.import.job"

    def _worker_actor(self):
        actor = self._import_actor()
        user = actor.env.user.sudo()
        user.invalidate_recordset(["active", "share", "groups_id", "company_id", "company_ids"])
        if (
            not user.active or user.share or not user.has_group(OPERATOR) or not user.has_group(SALES)
            or any(user.has_group(group) for group in FORBIDDEN_GROUPS)
            or len(user.company_ids) != 1 or user.company_id != user.company_ids
            or actor.env.company != user.company_id or actor.env.companies != user.company_ids
        ):
            raise AccessError(_("Processing requires the assigned active single-company import operator."))
        return actor

    def _worker_authorized_source(self, source):
        actor = self._worker_actor()
        if getattr(source, "_name", None) != "portfolio.import.source" or len(source) != 1:
            raise AccessError(_("The import source is unavailable or unauthorized."))
        source = actor.env["portfolio.import.source"].with_context(active_test=False).search([
            ("id", "=", source.id), ("active", "=", True),
            ("company_id", "=", actor.env.company.id), ("processor_user_id", "=", actor.env.uid),
        ], limit=1)
        if not source:
            raise AccessError(_("The import source is unavailable or unauthorized."))
        source.check_access("read")
        return actor, source

    def _worker_authorized_job(self):
        actor = self._worker_actor()
        if len(actor) != 1:
            raise AccessError(_("Select exactly one authorized import job."))
        job = actor.search([
            ("id", "=", actor.id), ("company_id", "=", actor.env.company.id),
            ("source_id.active", "=", True), ("source_id.processor_user_id", "=", actor.env.uid),
        ], limit=1)
        if not job:
            raise AccessError(_("The import job is unavailable or unauthorized."))
        job._worker_authorized_source(job.source_id)
        job.check_access("read")
        return job

    def _worker_lock_job(self):
        job = self._worker_authorized_job()
        job.company_id._portfolio_lock_policy()
        source = job.source_id
        source.flush_recordset()
        job.env.cr.execute("SELECT id FROM portfolio_import_source WHERE id = %s FOR UPDATE", [source.id])
        source.invalidate_recordset()
        job.flush_recordset()
        job.env.cr.execute("SELECT id FROM portfolio_import_job WHERE id = %s FOR UPDATE", [job.id])
        job.invalidate_recordset()
        return job._worker_authorized_job()

    def _worker_start(self):
        job = self._worker_lock_job()
        if job.state != "pending":
            raise UserError(_("Only a pending import job can begin processing."))
        super(ImportAccess, job).write({
            "state": "processing", "attempt_count": job.attempt_count + 1,
            "last_attempt_at": fields.Datetime.now(), "error_code": False,
            "error_detail": False, "sale_order_id": False,
        })
        return job

    def _worker_finish(self, error=None, sale_order=None):
        job = self._worker_lock_job()
        if job.state != "processing":
            raise AccessError(_("The job is not processing."))
        if error is not None:
            if not isinstance(error, ImportBusinessError) or sale_order is not None:
                raise AccessError(_("A failed job requires a safe processing diagnostic."))
            values = {"state": "failed", "error_code": error.code, "error_detail": error.detail,
                      "sale_order_id": False}
        else:
            if getattr(sale_order, "_name", None) != "sale.order" or len(sale_order) != 1:
                raise AccessError(_("A completed job requires its native quotation."))
            quote = job.env["sale.order"].browse(sale_order.id)
            quote.check_access("read")
            if quote.company_id != job.company_id or quote.create_uid.id != job.env.uid or quote.state != "draft":
                raise AccessError(_("The native quotation does not belong to this processing actor."))
            job._worker_validate_quote(quote)
            values = {"state": "done", "sale_order_id": quote.id,
                      "error_code": False, "error_detail": False}
        super(ImportAccess, job).write(values)
        return job

    def _worker_payload(self):
        job = self._worker_lock_job()
        if job.state != "processing":
            raise AccessError(_("Payload resolution requires a processing job."))
        try:
            data = json.loads(job.raw_payload)
            normalized = normalize_order_payload(data)
        except (PayloadValidationError, ValueError, TypeError):
            raise ImportBusinessError("audit_integrity", "payload", "The reception audit is invalid.") from None
        if (
            normalized.payload_hash != job.payload_hash or normalized.canonical_json != job.canonical_payload
            or data["external_order_id"] != job.external_order_id
            or data["company_code"] != job.source_id.company_code or data["source_code"] != job.source_id.code
        ):
            raise ImportBusinessError("audit_integrity", "payload", "The reception audit identity does not match.")
        # Raw audit retains the submitted row order; canonical sorting is only an
        # identity operation and must not change diagnostic paths or line order.
        return data

    def _worker_configuration(self, data, snapshot):
        job = self._worker_lock_job()
        _actor, source = job._worker_authorized_source(job.source_id)
        if job.state != "processing":
            raise AccessError(_("Configuration resolution requires a processing job."))
        snapshot["source_defaults"] = {
            "source_id": source.id, "company_id": source.company_id.id,
            "warehouse_id": source.warehouse_id.id, "pricelist_id": source.pricelist_id.id,
            "sales_user_id": source.sales_user_id.id, "processor_user_id": source.processor_user_id.id,
        }
        source.company_id.invalidate_recordset(["currency_id"])
        source.pricelist_id.invalidate_recordset(["active", "currency_id", "company_id"])
        source.warehouse_id.invalidate_recordset(["active", "company_id"])
        users = source.sales_user_id | source.processor_user_id | source.integration_user_ids
        users.invalidate_recordset(["active", "share", "groups_id", "company_id", "company_ids"])
        try:
            source._validate_configuration()
        except ValidationError:
            raise ImportBusinessError("invalid_configuration", "source", "Current source defaults are invalid.") from None
        currency = source.company_id.currency_id
        if data["currency"] != "CNY" or data["currency"] != currency.name or source.pricelist_id.currency_id != currency:
            raise ImportBusinessError("currency_mismatch", "currency", "The current source currency does not match.")
        check_precision(data, currency.rounding, job.env["decimal.precision"].precision_get("Discount"))
        return source

    def _worker_resolve(self, data, snapshot):
        job = self._worker_lock_job()
        if job.state != "processing":
            raise AccessError(_("Mapping resolution requires a processing job."))
        source = job._worker_configuration(data, snapshot)
        mappings = snapshot.setdefault("mapping", {"lines": []})
        customer_map = job.env["portfolio.import.customer.map"].with_context(active_test=False).search([
            ("source_id", "=", source.id), ("external_ref", "=", data["customer_ref"]),
        ], limit=2)
        if len(customer_map) != 1 or not customer_map.active:
            raise ImportBusinessError("customer_mapping", "customer_ref", "An active customer mapping is required.")
        partner = customer_map.partner_id.with_context(active_test=False)
        mappings["customer"] = {"mapping_id": customer_map.id, "partner_id": partner.id}
        if not partner.active or (partner.company_id and partner.company_id != job.company_id):
            raise ImportBusinessError("customer_target", "customer_ref", "The mapped customer is inactive or incompatible.")
        try:
            job.env["portfolio.master.data"]._partner(partner, job.company_id, "customer")
        except UserError:
            raise ImportBusinessError("customer_target", "customer_ref", "The commercial customer ref is inactive, invalid or ambiguous.") from None
        lines = []
        for index, line in enumerate(data["lines"]):
            path = f"lines[{index}]"
            product_map = job.env["portfolio.import.product.map"].with_context(active_test=False).search([
                ("source_id", "=", source.id), ("external_sku", "=", line["sku"]),
            ], limit=2)
            if len(product_map) != 1 or not product_map.active:
                raise ImportBusinessError("product_mapping", f"{path}.sku", "An active product mapping is required.")
            product = product_map.product_id.with_context(active_test=False)
            unit = product.uom_id.with_context(active_test=False)
            mappings["lines"].append({
                "line_index": index, "mapping_id": product_map.id, "product_id": product.id,
                "uom_id": unit.id,
            })
            if (
                not product.active or not product.product_tmpl_id.active
                or (product.company_id and product.company_id != job.company_id)
                or not unit.active or not product.uom_po_id.active
                or unit.category_id != product.uom_po_id.category_id
            ):
                raise ImportBusinessError("product_target", f"{path}.sku", "The mapped product or unit is inactive or incompatible.")
            code = product.default_code
            candidates = job.env["product.product"].with_context(active_test=False).search([
                ("active", "=", True), ("company_id", "in", [False, job.company_id.id]),
                ("default_code", "=", code),
            ], limit=2)
            if not code or code != code.strip() or candidates.ids != product.ids:
                raise ImportBusinessError("product_target", f"{path}.sku", "The native product SKU is invalid or ambiguous.")
            with localcontext() as context:
                context.prec = 64
                if Decimal(line["quantity"]) % Decimal(str(unit.rounding)):
                    raise ImportBusinessError("quantity_precision", f"{path}.quantity", "Quantity must fit the product base unit rounding.")
            lines.append(Command.create({
                "product_id": product.id, "product_uom": unit.id,
                "product_uom_qty": exact_float(line["quantity"], f"{path}.quantity"),
                "price_unit": exact_float(line["unit_price"], f"{path}.unit_price"),
                "discount": exact_float(line["discount"], f"{path}.discount"),
                "tax_id": [Command.clear()],
            }))
        return {
            "partner_id": partner.id, "partner_invoice_id": partner.id, "partner_shipping_id": partner.id,
            "company_id": job.company_id.id, "warehouse_id": source.warehouse_id.id,
            "pricelist_id": source.pricelist_id.id, "user_id": source.sales_user_id.id,
            "state": "draft", "client_order_ref": f"{source.code}:{job.external_order_id}",
            "origin": f"{source.code}:{job.external_order_id}", "order_line": lines,
        }

    def _worker_validate_quote(self, quote):
        job = self._worker_lock_job()
        if job.state != "processing" or getattr(quote, "_name", None) != "sale.order" or len(quote) != 1:
            raise AccessError(_("Native validation requires this processing job's quotation."))
        quote = job.env["sale.order"].browse(quote.id)
        quote.check_access("read")
        data = job._worker_payload()
        snapshot = {"mapping": {"lines": []}, "source_defaults": {}}
        job._worker_resolve(data, snapshot)
        defaults = snapshot["source_defaults"]
        mapping = snapshot["mapping"]
        if (
            quote.company_id != job.company_id or quote.create_uid.id != job.env.uid or quote.state != "draft"
            or quote.partner_id.id != mapping["customer"]["partner_id"]
            or quote.warehouse_id.id != defaults["warehouse_id"]
            or quote.pricelist_id.id != defaults["pricelist_id"]
            or quote.user_id.id != defaults["sales_user_id"] or quote.currency_id != job.company_id.currency_id
            or len(quote.order_line) != len(data["lines"])
            or quote.client_order_ref != f"{job.source_id.code}:{job.external_order_id}"
            or quote.origin != f"{job.source_id.code}:{job.external_order_id}"
        ):
            raise ImportBusinessError("native_values", "quotation", "The native quotation changed the resolved commercial values.")
        with localcontext() as context:
            context.prec = 64
            expected_total = Decimal(0)
            for index, (native_line, line, resolved) in enumerate(zip(
                quote.order_line.sorted("id"), data["lines"], mapping["lines"], strict=True,
            )):
                if (
                    native_line.product_id.id != resolved["product_id"]
                    or native_line.product_uom.id != resolved["uom_id"]
                    or Decimal(str(native_line.product_uom_qty)) != Decimal(line["quantity"])
                    or Decimal(str(native_line.price_unit)) != Decimal(line["unit_price"])
                    or Decimal(str(native_line.discount)) != Decimal(line["discount"])
                    or native_line.tax_id or native_line.display_type or native_line.is_downpayment
                ):
                    raise ImportBusinessError("native_values", f"lines[{index}]", "The native line changed the submitted commercial values.")
                expected = rounded_amount(line, quote.currency_id.rounding)
                if Decimal(str(native_line.price_subtotal)) != expected or Decimal(str(native_line.price_total)) != expected:
                    raise ImportBusinessError("native_precision", f"lines[{index}]", "The native amount cannot represent the exact rounded submitted amount.")
                expected_total += expected
            if Decimal(str(quote.amount_untaxed)) != expected_total or Decimal(str(quote.amount_total)) != expected_total:
                raise ImportBusinessError("native_precision", "quotation.amount_total", "The native total cannot represent the exact submitted amount.")
        job.env["portfolio.master.data"]._validate_orders(quote, "customer")

    def _process_job(self):
        actor = self._worker_authorized_job()
        with actor.env.cr.savepoint():
            job = actor._worker_lock_job()
            if job.state != "pending":
                return False
            job = job._worker_start()
            snapshot = {"mapping": {"lines": []}, "source_defaults": {}}
            try:
                with job.env.cr.savepoint():
                    data = job._worker_payload()
                    values = job._worker_resolve(data, snapshot)
                    # Native business writes always run under the actual operator.
                    quote = job.env["sale.order"].create(values)
                    job = job._worker_finish(sale_order=quote)
            except ImportBusinessError as error:
                job = job._worker_finish(error=error)
                job.env["portfolio.import.attempt"]._append_event(job, "failed", snapshot, error=error)
            except (UserError, ValidationError) as error:
                code = "native_access" if isinstance(error, AccessError) else "native_validation"
                safe_error = ImportBusinessError(code, "quotation", "Native quotation access or business validation rejected the submitted order.")
                job = job._worker_finish(error=safe_error)
                job.env["portfolio.import.attempt"]._append_event(job, "failed", snapshot, error=safe_error)
            else:
                job.env["portfolio.import.attempt"]._append_event(job, "done", snapshot)
            return True

    @api.model
    def _process_pending(self):
        actor = self._worker_actor()
        with actor.env.cr.savepoint():
            actor.env.company._portfolio_lock_policy()
            actor.flush_model()
            actor.env["portfolio.import.source"].flush_model()
            actor.env.cr.execute("""
                SELECT job.id
                  FROM portfolio_import_job AS job
                  JOIN portfolio_import_source AS source ON source.id = job.source_id
                 WHERE job.state = 'pending' AND job.company_id = %s
                   AND source.company_id = %s AND source.active
                   AND source.processor_user_id = %s
                 ORDER BY job.id
                 LIMIT 5
                   FOR UPDATE OF job SKIP LOCKED
            """, [actor.env.company.id, actor.env.company.id, actor.env.uid])
            job_ids = [row[0] for row in actor.env.cr.fetchall()]
            for job in actor.browse(job_ids):
                job._process_job()
            return len(job_ids)

    def action_retry(self, reason):
        actor = self._worker_authorized_job()
        try:
            reason = retry_reason(reason)
        except ImportBusinessError as invalid:
            raise UserError(_("%(code)s: %(path)s", code=invalid.code, path=invalid.path)) from None
        with actor.env.cr.savepoint():
            job = actor._worker_lock_job()
            if job.state != "failed":
                raise UserError(_("Only a failed import job can be retried manually."))
            previous = job.env["portfolio.import.attempt"].search([
                ("job_id", "=", job.id), ("attempt_no", "=", job.attempt_count),
                ("event_type", "=", "failed"),
            ], limit=1)
            if not previous:
                raise AccessError(_("The failed attempt audit is missing."))
            snapshot = {
                "mapping": json.loads(previous.mapping_snapshot),
                "source_defaults": json.loads(previous.source_defaults_snapshot),
            }
            job.env["portfolio.import.attempt"]._append_event(job, "retry", snapshot, reason=reason)
            super(ImportAccess, job).write({"state": "pending", "error_code": False, "error_detail": False})
            return True
