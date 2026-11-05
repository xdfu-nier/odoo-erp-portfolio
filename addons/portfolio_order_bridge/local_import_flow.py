"""Explicit real-role local bridge demonstration; callers own the transaction.

No model method, install hook, RPC endpoint or implicit profile creation calls
this helper. Completed reruns validate live native facts without business writes.
"""

import hashlib

from odoo import SUPERUSER_ID
from odoo.addons.portfolio_trade.acceptance_data import _validate_local_target
from odoo.addons.portfolio_trade.local_finance import _acceptance_preflight
from odoo.exceptions import AccessError, UserError

from . import local_acceptance
from .local_flow_contract import (
    APPROVAL_MESSAGE,
    ATTEMPT,
    EXPECTED_IDENTITIES,
    JOB,
    NAMESPACE,
    PRODUCT_MAP,
    REPAIR_SKU,
    RETRY,
    RETRY_REASON,
    namespace_records,
    payloads,
    require,
    role_model,
    validate_completed,
    validate_event,
    validate_job,
    validate_quote,
)

_LOCK = 762714395


def _scope(env, actor, static):
    company = static["source"].company_id
    scope = env.context.get("allowed_company_ids")
    if company.id not in actor.company_ids.ids or (
        scope is not None and (
            not isinstance(scope, (list, tuple)) or company.id not in scope
            or any(type(value) is not int or value not in actor.company_ids.ids for value in scope)
        )
    ):
        raise AccessError(env._("The local import company must be in the maintainer's actual active scope."))
    return env(context={"allowed_company_ids": company.ids, "lang": "en_US"})


def _fact_ids(env, model, where="TRUE", parameters=()):
    # Read-only, fixed native ownership probes must also detect history hidden
    # by another company's rules. No elevated business CRUD/service is invoked.
    table = env[model]._table
    env.cr.execute(f'SELECT id FROM "{table}" WHERE {where} ORDER BY id', parameters)
    return [row[0] for row in env.cr.fetchall()]


def _history(env, static, flow):
    source, operator = static["source"], static["user_operator"]
    settings = role_model(env, static, "settings", local_acceptance.SOURCE)
    assigned = settings.with_context(active_test=False).search([("processor_user_id", "=", operator.id)])
    require(assigned.ids == source.ids,
            "the dedicated operator is assigned another source; the shared batch cannot run safely.")
    # Include archived and foreign-company sources in the same bounded yes/no
    # check. No source fields or foreign business data enter the report.
    env.cr.execute("SELECT EXISTS (SELECT 1 FROM portfolio_import_source WHERE processor_user_id = %s AND id != %s)",
                   [operator.id, source.id])
    require(not env.cr.fetchone()[0], "the dedicated operator is assigned an unexpected source.")
    jobs = role_model(env, static, "operator", JOB).search([("source_id", "=", source.id)])
    expected_jobs = {flow[f"job_{suffix}"].id for suffix in ("success", "repaired")} if flow else set()
    require(set(jobs.ids) == expected_jobs, "the owned source has unexpected, missing or unmarked jobs.")
    env.cr.execute("SELECT EXISTS (SELECT 1 FROM portfolio_import_job WHERE source_id = %s AND NOT (id = ANY(%s)))",
                   [source.id, sorted(expected_jobs)])
    require(not env.cr.fetchone()[0], "the owned source has hidden unmarked job history.")
    attempts = role_model(env, static, "operator", ATTEMPT).search([("job_id.source_id", "=", source.id)])
    require(set(attempts.ids) == {record.id for key, record in flow.items() if key.startswith("attempt_")},
            "the owned source has unmarked attempt history.")
    products = [static[f"product_item{index}"].id for index in (1, 2)]
    expected_quotes = {flow[f"quote_{suffix}"].id for suffix in ("success", "repaired")} if flow else set()
    quotes = _fact_ids(env, "sale.order",
                      "partner_id = %s OR origin LIKE %s OR id IN "
                      "(SELECT order_id FROM sale_order_line WHERE product_id = ANY(%s))",
                      [static["customer"].id, f"{source.code}:%", products])
    require(set(quotes) == expected_quotes, "the owned customer or products have unexpected sale history.")
    lines = _fact_ids(env, "sale.order.line", "product_id = ANY(%s)", [products])
    expected_lines = {line.id for key, record in flow.items() if key.startswith("quote_") for line in record.order_line}
    require(set(lines) == expected_lines, "the owned products have unmarked sale lines.")
    maps = role_model(env, static, "settings", PRODUCT_MAP).with_context(active_test=False).search([
        ("product_id", "in", products),
    ])
    expected_maps = {static[f"product_map_item{index}"].id for index in (1, 2)}
    if flow:
        expected_maps.add(flow["product_map_repair"].id)
    require(set(maps.ids) == expected_maps, "the owned products are used by unexpected source mappings.")
    env.cr.execute("SELECT EXISTS (SELECT 1 FROM portfolio_import_product_map WHERE product_id = ANY(%s) "
                   "AND NOT (id = ANY(%s)))", [products, sorted(expected_maps)])
    require(not env.cr.fetchone()[0], "the owned products have hidden foreign mapping history.")
    for model in ("stock.move", "stock.move.line", "stock.quant", "purchase.order.line", "account.move.line"):
        require(not _fact_ids(env, model, "product_id = ANY(%s)", [products]),
                "the owned products have unexpected stock, purchase or accounting history.")
    require(not _fact_ids(env, "purchase.order", "partner_id = %s", [static["vendor"].id]),
            "the owned vendor has unexpected purchase history.")
    require(not _fact_ids(env, "account.move", "partner_id = ANY(%s)",
                         [[static["customer"].id, static["vendor"].id]]),
            "the owned partners have unexpected accounting history.")


def _preservation(env, profile_report, acceptance):
    """Digest fixed existing M1/finance rows and their native history, read only."""
    env.flush_all()
    grouped = {}
    for namespace in ("portfolio_trade_acceptance", "portfolio_trade_finance", "portfolio_trade_fulfillment"):
        for marker in env["ir.model.data"].sudo().search([("module", "=", namespace)], order="name"):
            grouped.setdefault(marker.model, set()).add(marker.res_id)
    product_ids = [acceptance[f"product_{key}"].id for key in ("bearing", "sensor", "bolt")]
    for model in ("sale.order.line", "purchase.order.line", "stock.move", "stock.move.line", "stock.quant", "account.move.line"):
        grouped.setdefault(model, set()).update(_fact_ids(env, model, "product_id = ANY(%s)", [product_ids]))
    for model in ("res.groups", "res.currency", "ir.default", "product.pricelist"):
        grouped.setdefault(model, set()).update(_fact_ids(env, model))
    facts = []
    for model, ids in sorted(grouped.items()):
        # Table names come only from the fixed native registry models above;
        # supplied credentials and caller text never become SQL identifiers.
        table = env[model]._table
        env.cr.execute(f'SELECT to_jsonb(t)::text FROM "{table}" AS t WHERE id = ANY(%s) ORDER BY id', [sorted(ids)])
        facts.append((model, env.cr.fetchall()))
    company, bearing = acceptance["company_a"], acceptance["product_bearing"]
    env.cr.execute("SELECT COALESCE(SUM(quant.quantity), 0) FROM stock_quant AS quant "
                   "JOIN stock_location AS location ON location.id = quant.location_id "
                   "WHERE quant.product_id = %s AND quant.company_id = %s "
                   "AND location.usage IN ('internal', 'transit')", [bearing.id, company.id])
    stock = float(env.cr.fetchone()[0])
    return {
        "fingerprint": hashlib.sha256(repr(facts).encode()).hexdigest(),
        "borrowed_m1_identities": profile_report["borrowed_m1_identities"],
        "bearing_stock": stock,
        "bearing_move_ids": _fact_ids(env, "stock.move", "product_id = %s", [bearing.id]),
    }


def _own(env, key, record):
    require(key in EXPECTED_IDENTITIES and record._name == EXPECTED_IDENTITIES[key] and len(record) == 1,
            "an unexpected flow identity cannot be marked.")
    env["ir.model.data"].sudo().create({
        "module": NAMESPACE, "name": key, "model": record._name, "res_id": record.id, "noupdate": True,
    })


class _UndoConfirmation(Exception):
    pass


def _approval_guard(env, static, quote):
    """Exercise the responsible Sales user's exact public business rejection."""
    quote = role_model(env, static, "sales", "sale.order", sales=static["source"].sales_user_id).browse(quote.id)
    blocked = False
    try:
        with env.cr.savepoint():
            try:
                quote.action_confirm()
            except UserError as error:
                require(type(error) is UserError and str(error) == APPROVAL_MESSAGE,
                        "the quotation did not produce the expected current-approval business rejection.")
                blocked = True
            # Always undo the probe's native locks, revisions and any accidental
            # side effects, even when the expected error was caught locally.
            raise _UndoConfirmation()
    except _UndoConfirmation:
        pass
    require(blocked, "the quotation unexpectedly passed its current-approval confirmation guard.")


def _execute(env, static):
    incoming = payloads(static)
    integration = role_model(env, static, "integration", JOB)
    operator = role_model(env, static, "operator", JOB)
    event_model = role_model(env, static, "operator", ATTEMPT)
    sales = static["source"].sales_user_id
    quote_model = role_model(env, static, "sales", "sale.order", sales=sales)
    first = integration.submit_order(incoming["success"])
    repeated = integration.submit_order(incoming["success"])
    require(not first["reused"] and repeated["reused"] and first["job_id"] == repeated["job_id"],
            "the success reception did not preserve native idempotency.")
    operator.action_process_pending()
    success = operator.browse(first["job_id"])
    validate_job(static, success, "success")
    quote = quote_model.browse(success.sale_order_id.id)
    validate_quote(static, success, quote, "success", sales)
    event = event_model.search([("job_id", "=", success.id)])
    require(len(event) == 1, "success processing did not produce exactly one outcome event.")
    validate_event(static, event, success, "done", 1, "success")
    for key, record in (("job_success", success), ("quote_success", quote), ("attempt_success", event)):
        _own(env, key, record)
    _approval_guard(env, static, quote)
    second = integration.submit_order(incoming["repaired"])
    require(not second["reused"], "an unmarked retry reception cannot be adopted.")
    operator.action_process_pending()
    repaired = operator.browse(second["job_id"])
    validate_job(static, repaired, "repaired", done=False)
    failure = event_model.search([("job_id", "=", repaired.id)])
    require(len(failure) == 1, "failed processing did not produce exactly one outcome event.")
    validate_event(static, failure, repaired, "failed", 1, "repaired")
    failure_facts = failure.read()
    _own(env, "job_repaired", repaired)
    _own(env, "attempt_failed", failure)
    mapping = role_model(env, static, "settings", PRODUCT_MAP).create({
        "source_id": static["source"].id, "external_sku": REPAIR_SKU,
        "product_id": static["product_item2"].id,
    })
    _own(env, "product_map_repair", mapping)
    static = {**static, "product_map_repair": mapping}
    action = repaired.action_open_retry()
    require(isinstance(action, dict) and action.get("res_model") == RETRY and action.get("target") == "new",
            "the public retry action did not open its native reason wizard.")
    wizard = role_model(env, static, "operator", RETRY).create({"job_id": repaired.id, "reason": RETRY_REASON})
    wizard.action_apply()
    require(repaired.state == "pending" and repaired.attempt_count == 1 and not repaired.sale_order_id,
            "the native retry wizard did not preserve the first failed attempt ordinal.")
    retry = event_model.search([("job_id", "=", repaired.id), ("event_type", "=", "retry")])
    require(len(retry) == 1, "the native retry wizard did not produce exactly one retry event.")
    validate_event(static, retry, repaired, "retry", 1, "repaired")
    require(failure.read() == failure_facts, "repair or retry modified the original immutable failure.")
    _own(env, "attempt_retry", retry)
    operator.action_process_pending()
    validate_job(static, repaired, "repaired")
    quote = quote_model.browse(repaired.sale_order_id.id)
    validate_quote(static, repaired, quote, "repaired", sales)
    repaired_event = event_model.search([("job_id", "=", repaired.id), ("event_type", "=", "done")])
    require(len(repaired_event) == 1, "repaired processing did not produce exactly one success event.")
    validate_event(static, repaired_event, repaired, "done", 2, "repaired")
    require(failure.read() == failure_facts, "repaired processing modified the original immutable failure.")
    _own(env, "quote_repaired", quote)
    _own(env, "attempt_repaired", repaired_event)


def _report(env, static, records, preservation):
    return {
        "schema_version": 1, "database": env.cr.dbname, "namespace": NAMESPACE,
        "company_id": static["source"].company_id.id, "source_id": static["source"].id,
        "identities": {key: {"model": record._name, "id": record.id} for key, record in sorted(records.items())},
        "jobs": {
            suffix: {"id": records[f"job_{suffix}"].id, "state": records[f"job_{suffix}"].state,
                     "attempt_count": records[f"job_{suffix}"].attempt_count,
                     "submitted_by_id": records[f"job_{suffix}"].submitted_by_id.id,
                     "processed_by_id": static["user_operator"].id}
            for suffix in ("success", "repaired")
        },
        "quotes": {
            suffix: {"id": records[f"quote_{suffix}"].id, "name": records[f"quote_{suffix}"].name,
                     "state": records[f"quote_{suffix}"].state, "currency": records[f"quote_{suffix}"].currency_id.name,
                     "amount_untaxed": records[f"quote_{suffix}"].amount_untaxed,
                     "amount_total": records[f"quote_{suffix}"].amount_total,
                     "owner_id": records[f"quote_{suffix}"].user_id.id,
                     "created_by_id": records[f"quote_{suffix}"].create_uid.id, "approval_required": True}
            for suffix in ("success", "repaired")
        },
        "events": {
            key: {"id": record.id, "job_id": record.job_id.id, "event_type": record.event_type,
                  "attempt_no": record.attempt_no, "actor_id": record.actor_id.id,
                  "retry_reason": record.retry_reason or None}
            for key, record in sorted(records.items()) if key.startswith("attempt_")
        },
        "m1_preservation": preservation,
    }


def run_local_import(env, passwords_by_role, allow_local=False):
    """Run/validate only the already prepared, bounded local flow; never commit."""
    if allow_local is not True:
        raise UserError("Local import flow requires explicit local opt-in.")
    _validate_local_target(env)
    # has_group() can fetch translated group fields before the later profile
    # scope exists. Drop caller language/defaults/stock filters at this entry
    # boundary, retaining the raw company scope for explicit validation below.
    context = {"lang": "en_US"}
    if "allowed_company_ids" in env.context:
        context["allowed_company_ids"] = env.context["allowed_company_ids"]
    env = env(context=context)
    actor = env["res.users"].with_user(env.uid).env.user
    bootstrap = env.uid == SUPERUSER_ID and env.su
    if (not bootstrap and not actor.active) or not actor.has_group("base.group_system"):
        raise AccessError(env._("Only actual Settings administrators can run the local import flow."))
    local_acceptance._passwords(passwords_by_role)
    with env.cr.savepoint():
        env.cr.execute("SELECT pg_advisory_xact_lock(%s)", [_LOCK])
        # Read namespace identities before calling the static helper so a
        # missing static profile can never enter its bootstrap creation branch.
        clean = env(context={"lang": "en_US"})
        static = local_acceptance._namespace(clean)
        require(bool(static), "prepare the complete static bridge profile before running the flow.")
        clean = _scope(env, actor, static)
        static = {key: record.with_env(clean) for key, record in static.items()}
        profile = local_acceptance.prepare_local_import(clean, passwords_by_role, allow_local=True)
        acceptance = _acceptance_preflight(clean)
        require(acceptance["company_a"].portfolio_approval_enabled,
                "the existing company A approval policy must already be enabled.")
        # All profile references are now validated and the caller's stock/date
        # filters were discarded before any quantity or business role is read.
        acceptance = {key: record.with_context(clean.context) for key, record in acceptance.items()}
        # Use the existing native lock order (company then source), holding the
        # source against concurrent ingress until the caller ends its transaction.
        source = role_model(clean, static, "settings", local_acceptance.SOURCE).browse(static["source"].id)
        source.company_id._portfolio_lock_policy()
        source.flush_recordset()
        clean.cr.execute("SELECT id FROM portfolio_import_source WHERE id = %s FOR UPDATE", [source.id])
        flow = namespace_records(clean, optional=True)
        if flow:
            flow = validate_completed(clean, static, flow)
        _history(clean, static, flow)
        before = _preservation(clean, profile, acceptance)
        if not flow:
            _execute(clean, static)
            flow = validate_completed(clean, static)
            _history(clean, static, flow)
            # The exact nine markers now permit the sole repair mapping in
            # static profile validation. Do not validate a partial flow midway.
            require(local_acceptance.prepare_local_import(clean, passwords_by_role, allow_local=True) == profile,
                    "the static profile changed while executing the local flow.")
        after = _preservation(clean, profile, acceptance)
        require(after == before, "the existing M1 or finance profile/history changed during the local flow.")
        return _report(clean, static, flow, after)
