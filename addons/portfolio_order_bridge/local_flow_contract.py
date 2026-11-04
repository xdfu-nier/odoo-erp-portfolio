"""Read-only identity and commercial contract shared by the two local helpers.

This module never prepares a profile or executes a job. In particular, static
profile validation cannot recurse into the local flow's preparation entry point.
"""

import json
from decimal import Decimal

from odoo import SUPERUSER_ID
from odoo.exceptions import AccessError, UserError
from odoo.tools import LazyTranslate

from .payload import normalize_order_payload

NAMESPACE = "portfolio_order_bridge_flow"
JOB = "portfolio.import.job"
ATTEMPT = "portfolio.import.attempt"
PRODUCT_MAP = "portfolio.import.product.map"
RETRY = "portfolio.import.retry"
REPAIR_SKU = "BRIDGE-ACCEPTANCE-REPAIR"
RETRY_REASON = "Acceptance SKU mapping repaired"
APPROVAL_MESSAGE = "This quotation requires current approval before confirmation. Submit it for approval."
EXPECTED_IDENTITIES = {
    "job_success": JOB, "quote_success": "sale.order", "attempt_success": ATTEMPT,
    "job_repaired": JOB, "quote_repaired": "sale.order", "attempt_failed": ATTEMPT,
    "attempt_retry": ATTEMPT, "attempt_repaired": ATTEMPT, "product_map_repair": PRODUCT_MAP,
}
_lt = LazyTranslate(__name__, default_lang="en_US")


def require(condition, message):
    if not condition:
        raise UserError(_lt("Local import flow conflict: %s", message))


def role_model(env, static, role, model, *, sales=None):
    user = sales if role == "sales" else static[f"user_{role}"]
    company = static["source"].company_id
    records = env[model].with_user(user).with_context({
        "allowed_company_ids": company.ids, "lang": "en_US",
    })
    if (
        records.env.uid == SUPERUSER_ID or records.env.su or records.env.uid != user.id
        or not user.active or user.share or user.company_id != company or user.company_ids != company
        or records.env.companies != company
    ):
        raise AccessError(_lt("Local import actions require the actual ordinary role in company A."))
    return records


def payloads(static):
    common = {
        "company_code": "PARTS-A", "source_code": "B2B-ACCEPTANCE",
        "customer_ref": static["customer_map"].external_ref, "currency": "CNY",
        "sent_at": "2026-11-04T00:00:00Z",
    }
    return {
        "success": {**common, "external_order_id": "BRIDGE-ACCEPTANCE-SUCCESS",
                    "request_id": "local-import-success",
                    "lines": [
                        {"external_line_id": "2", "sku": "BRIDGE-ACCEPTANCE-2",
                         "quantity": "3", "unit_price": "35", "discount": "0"},
                        {"external_line_id": "1", "sku": "BRIDGE-ACCEPTANCE-1",
                         "quantity": "2", "unit_price": "120", "discount": "15"},
                    ]},
        # Deliberately place the missing row second while canonical sorting
        # places it first: diagnostics must retain the reception's row index.
        "repaired": {**common, "external_order_id": "BRIDGE-ACCEPTANCE-RETRY",
                     "request_id": "local-import-repair",
                     "lines": [
                         {"external_line_id": "2", "sku": "BRIDGE-ACCEPTANCE-1",
                          "quantity": "1", "unit_price": "120", "discount": "15"},
                         {"external_line_id": "1", "sku": REPAIR_SKU,
                          "quantity": "2", "unit_price": "35", "discount": "15"},
                     ]},
    }


def namespace_records(env, *, optional=False):
    markers = env["ir.model.data"].sudo().search([("module", "=", NAMESPACE)])
    if not markers and optional:
        return {}
    require(
        len(markers) == len(EXPECTED_IDENTITIES)
        and set(markers.mapped("name")) == set(EXPECTED_IDENTITIES)
        and all(markers.mapped("noupdate")),
        "the flow namespace is missing, partial, altered or contains unknown identities.",
    )
    records = {}
    for marker in markers:
        require(marker.model == EXPECTED_IDENTITIES[marker.name], "a flow identity has the wrong model.")
        record = env[marker.model].with_context(active_test=False).browse(marker.res_id).exists()
        require(bool(record), "a flow identity is stale.")
        require(not env["ir.model.data"].sudo().search_count([
            ("model", "=", marker.model), ("res_id", "=", marker.res_id), ("module", "!=", NAMESPACE),
        ]), "a flow target also belongs to another namespace.")
        records[marker.name] = record
    require(len({(record._name, record.id) for record in records.values()}) == len(records),
            "flow identities were repointed to the same target.")
    return records


def validate_job(static, job, suffix, *, done=True):
    normalized = normalize_order_payload(payloads(static)[suffix])
    require(
        job.source_id.id == static["source"].id and job.company_id == static["source"].company_id
        and job.external_order_id == payloads(static)[suffix]["external_order_id"]
        and job.raw_payload == normalized.raw_json and job.canonical_payload == normalized.canonical_json
        and job.payload_hash == normalized.payload_hash
        and job.create_uid.id == static["user_integration"].id
        and job.submitted_by_id.id == static["user_integration"].id
        and job.received_at and job.last_attempt_at and job.last_attempt_at >= job.received_at,
        "an owned job's source, actor or reception audit was modified.",
    )
    expected_count = 2 if done and suffix == "repaired" else 1
    require(job.state == ("done" if done else "failed") and job.attempt_count == expected_count,
            "an owned job has an unexpected state or attempt count.")
    require(bool(job.sale_order_id) == done
            and (not job.error_code and not job.error_detail if done else job.error_code == "product_mapping"),
            "an owned job has an unexpected quotation or processing diagnostic.")


def validate_quote(static, job, quote, suffix, sales):
    source, company = static["source"], static["source"].company_id
    require(
        quote.id == job.sale_order_id.id and quote.state == "draft" and quote.company_id == company
        and quote.create_uid.id == static["user_operator"].id
        and quote.portfolio_creator_id.id == static["user_operator"].id and quote.user_id.id == sales.id
        and quote.partner_id.id == static["customer"].id
        and quote.partner_invoice_id.id == static["customer"].id
        and quote.partner_shipping_id.id == static["customer"].id
        and quote.warehouse_id.id == source.warehouse_id.id and quote.pricelist_id.id == source.pricelist_id.id
        and quote.currency_id == company.currency_id and quote.currency_id.name == "CNY"
        and quote.origin == f"{source.code}:{job.external_order_id}"
        and quote.client_order_ref == quote.origin
        and quote.portfolio_approval_enabled and quote._portfolio_requires_approval()
        and quote.portfolio_approval_state == "draft" and not quote.portfolio_approval_history_ids
        and not quote.portfolio_approval_submitter_id and not quote.portfolio_approval_fingerprint
        and not quote.picking_ids and not quote.invoice_ids,
        "an owned quotation's actor, commercial profile, draft state or approval was changed.",
    )
    lines = quote.order_line.sorted("id")
    expected = payloads(static)[suffix]["lines"]
    require(len(lines) == len(expected), "an owned quotation has extra or missing lines.")
    amount = Decimal(0)
    for line, item in zip(lines, expected, strict=True):
        index = 1 if item["sku"] == "BRIDGE-ACCEPTANCE-1" else 2
        product = static[f"product_item{index}"]
        require(
            line.product_id.id == product.id and line.product_uom.id == product.uom_id.id
            and line.product_uom_qty == float(item["quantity"])
            and line.price_unit == float(item["unit_price"]) and line.discount == float(item["discount"])
            and not line.tax_id and not line.display_type and not line.is_downpayment
            and not line.qty_delivered and not line.qty_invoiced and not line.invoice_lines and not line.move_ids,
            "an owned quotation line's product, quantity, price, tax or execution was modified.",
        )
        amount += Decimal(item["quantity"]) * Decimal(item["unit_price"]) * (
            1 - Decimal(item["discount"]) / 100
        )
    require(Decimal(str(quote.amount_untaxed)) == amount
            and quote.amount_total == quote.amount_untaxed and not quote.amount_tax,
            "an owned quotation's native amounts were modified.")


def _mapping_snapshot(static, suffix, *, failed=False):
    result = {"customer": {"mapping_id": static["customer_map"].id, "partner_id": static["customer"].id},
              "lines": []}
    for line_index, item in enumerate(payloads(static)[suffix]["lines"]):
        if failed and item["sku"] == REPAIR_SKU:
            break
        index = 1 if item["sku"] == "BRIDGE-ACCEPTANCE-1" else 2
        mapping = static["product_map_repair"] if item["sku"] == REPAIR_SKU else static[f"product_map_item{index}"]
        result["lines"].append({
            "line_index": line_index, "mapping_id": mapping.id,
            "product_id": static[f"product_item{index}"].id, "uom_id": static[f"product_item{index}"].uom_id.id,
        })
    return result


def validate_event(static, event, job, kind, ordinal, suffix):
    source = static["source"]
    require(
        event.job_id.id == job.id and event.company_id == source.company_id
        and event.actor_id.id == static["user_operator"].id and event.create_uid.id == static["user_operator"].id
        and event.event_type == kind and event.attempt_no == ordinal
        and event.started_at and event.finished_at and event.finished_at >= event.started_at
        and event.started_at >= job.received_at and event.write_date == event.create_date,
        "an immutable attempt's identity, actor, ordinal or times were modified.",
    )
    require(event.retry_reason == (RETRY_REASON if kind == "retry" else False),
            "an immutable attempt's repair reason was modified.")
    require(
        (event.error_code, event.error_path, event.error_detail)
        == (("product_mapping", "lines[1].sku", "An active product mapping is required.")
            if kind == "failed" else (False, False, False)),
        "an immutable attempt's safe processing diagnostic was modified.",
    )
    try:
        mapping, defaults = json.loads(event.mapping_snapshot), json.loads(event.source_defaults_snapshot)
    except (TypeError, ValueError):
        require(False, "an immutable attempt's saved mapping or source defaults are invalid.")
    require(mapping == _mapping_snapshot(static, suffix, failed=kind in ("failed", "retry")),
            "an immutable attempt's saved mapping was modified.")
    require(defaults == {
        "source_id": source.id, "company_id": source.company_id.id,
        "warehouse_id": source.warehouse_id.id, "pricelist_id": source.pricelist_id.id,
        "sales_user_id": source.sales_user_id.id, "processor_user_id": source.processor_user_id.id,
    }, "an immutable attempt's saved source defaults were modified.")
    if kind == "done":
        require(event.started_at == job.last_attempt_at, "a successful attempt lost its job's processing time.")


def validate_completed(env, static, records=None):
    """Read only the complete nine identities; never create or repair anything."""
    records = namespace_records(env) if records is None else records
    source, company = static["source"], static["source"].company_id
    sales = source.sales_user_id
    job_model = role_model(env, static, "operator", JOB)
    event_model = role_model(env, static, "operator", ATTEMPT)
    quote_model = role_model(env, static, "sales", "sale.order", sales=sales)
    mapping = role_model(env, static, "settings", PRODUCT_MAP).browse(records["product_map_repair"].id)
    require(
        mapping.active and mapping.source_id.id == source.id and mapping.company_id == company
        and mapping.external_sku == REPAIR_SKU and mapping.product_id.id == static["product_item2"].id
        and mapping.create_uid.id == static["user_settings"].id,
        "the completed flow's sole repair mapping was modified or has a foreign owner.",
    )
    mapping._validate_target()
    static = {**static, "product_map_repair": mapping}
    rebound = {"product_map_repair": mapping}
    for suffix in ("success", "repaired"):
        job = job_model.browse(records[f"job_{suffix}"].id)
        quote = quote_model.browse(records[f"quote_{suffix}"].id)
        validate_job(static, job, suffix)
        validate_quote(static, job, quote, suffix, sales)
        rebound.update({f"job_{suffix}": job, f"quote_{suffix}": quote})
    for key, kind, ordinal, suffix in (
        ("attempt_success", "done", 1, "success"), ("attempt_failed", "failed", 1, "repaired"),
        ("attempt_retry", "retry", 1, "repaired"), ("attempt_repaired", "done", 2, "repaired"),
    ):
        event = event_model.browse(records[key].id)
        validate_event(static, event, rebound[f"job_{suffix}"], kind, ordinal, suffix)
        rebound[key] = event
    require(rebound["attempt_failed"].finished_at <= rebound["attempt_retry"].started_at
            <= rebound["attempt_retry"].finished_at <= rebound["attempt_repaired"].started_at,
            "failed, retry and repaired events have inconsistent chronology.")
    require(event_model.search([
        ("job_id", "in", [rebound["job_success"].id, rebound["job_repaired"].id]),
    ]) == event_model.browse(sorted(rebound[key].id for key in EXPECTED_IDENTITIES if key.startswith("attempt_"))),
        "the flow jobs contain an unexpected attempt event.")
    return rebound
