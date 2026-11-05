"""Pure authenticated bridge HTTP contracts: all wire access is mocked."""

import builtins
import contextlib
import copy
import hashlib
import http.cookiejar
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import unittest
import urllib.error
from collections import Counter
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
ROLES = ("settings", "operator", "integration", "sales", "other_sales")
OLD_ROLES = ("sales", "manager", "manager2", "buyer", "warehouse", "finance", "other_sales")
SOURCE = "portfolio.import.source"
JOB = "portfolio.import.job"
ATTEMPT = "portfolio.import.attempt"
CUSTOMER_MAP = "portfolio.import.customer.map"
PRODUCT_MAP = "portfolio.import.product.map"
M1_MODELS = {
    "company_a": "res.company", "company_b": "res.company", "warehouse_a": "stock.warehouse",
    "customer": "res.partner", "vendor": "res.partner", "initial_bearing_move": "stock.move",
    **{f"user_{role}": "res.users" for role in OLD_ROLES},
    **{f"product_{key}": "product.product" for key in ("bearing", "sensor", "bolt")},
    **{f"supplier_{key}": "product.supplierinfo" for key in ("bearing", "sensor", "bolt")},
}
PROFILE_MODELS = {
    **{f"user_{role}": "res.users" for role in ROLES[:3]},
    "customer": "res.partner", "vendor": "res.partner",
    **{f"product_item{i}": "product.product" for i in (1, 2)},
    **{f"template_item{i}": "product.template" for i in (1, 2)},
    **{f"supplier_item{i}": "product.supplierinfo" for i in (1, 2)},
    "source": SOURCE, "customer_map": CUSTOMER_MAP,
    **{f"product_map_item{i}": PRODUCT_MAP for i in (1, 2)},
    "processing_cron": "ir.cron", "processing_server_action": "ir.actions.server",
}
FLOW_MODELS = {
    "job_success": JOB, "quote_success": "sale.order", "attempt_success": ATTEMPT,
    "job_repaired": JOB, "quote_repaired": "sale.order", "attempt_failed": ATTEMPT,
    "attempt_retry": ATTEMPT, "attempt_repaired": ATTEMPT, "product_map_repair": PRODUCT_MAP,
}
GROUPS = ("base.group_user", "base.group_system", "base.group_erp_manager", "portfolio_order_bridge.group_import_operator",
          "portfolio_order_bridge.group_import_integration", "sales_team.group_sale_salesman",
          "sales_team.group_sale_salesman_all_leads", "sales_team.group_sale_manager",
          "purchase.group_purchase_user", "stock.group_stock_user", "account.group_account_invoice")
MENU_MODELS = {"sources": SOURCE, "customer_maps": CUSTOMER_MAP, "product_maps": PRODUCT_MAP,
               "jobs": JOB, "attempts": ATTEMPT, "sales": "sale.order"}
ROLE_MENUS = {"settings": ("sources", "customer_maps", "product_maps"),
              "operator": ("jobs", "attempts", "sales"), "integration": ("jobs", "attempts"),
              "sales": ("sales",), "other_sales": ("sales",)}
COMMON_CHECKS = {"session", "groups", "menus", "actions", "views", "healthy_after_negatives",
                 "business_unchanged", "cross_company", "private_rpc_denied", "no_scope_spoof",
                 "audit_crud_denied", "scoped_queries"}
ROLE_CHECKS = {
    "settings": {"service_denied", "job_acl_denied", "cron_configuration", "source_catalog"},
    "operator": {"source_private_fields_denied", "queue_empty", "done_retry_denied", "quote_details"},
    "integration": {"service_denied", "native_so_denied", "mappings_denied",
                    "source_private_fields_denied", "idempotent_reception"},
    "sales": {"service_denied", "job_acl_denied", "approval_guard", "quote_details"},
    "other_sales": {"service_denied", "job_acl_denied", "foreign_quotes_hidden"},
}
VIEW_FIELDS = {
    SOURCE: {"list": {"name", "company_id", "company_code", "code", "active"},
             "form": {"name", "company_id", "company_code", "code", "active", "pricelist_id", "warehouse_id",
                      "sales_user_id", "processor_user_id", "integration_user_ids", "processing_cron_id"}},
    CUSTOMER_MAP: {"list": {"source_id", "company_id", "external_ref", "partner_id", "active"},
                   "form": {"source_id", "company_id", "external_ref", "partner_id", "active"}},
    PRODUCT_MAP: {"list": {"source_id", "company_id", "external_sku", "product_id", "active"},
                  "form": {"source_id", "company_id", "external_sku", "product_id", "active"}},
    JOB: {"list": {"state", "source_id", "company_id", "external_order_id", "attempt_count", "sale_order_id"},
          "form": {"state", "source_id", "company_id", "external_order_id", "attempt_count", "sale_order_id",
                   "submitted_by_id", "attempt_ids", "raw_payload", "canonical_payload", "payload_hash"}},
    ATTEMPT: {"list": {"job_id", "company_id", "attempt_no", "event_type", "actor_id", "retry_reason"},
              "form": {"job_id", "company_id", "attempt_no", "event_type", "actor_id", "retry_reason",
                       "mapping_snapshot", "source_defaults_snapshot"}},
    "sale.order": {"list": {"name", "partner_id", "amount_total", "state"},
                   "form": {"name", "partner_id", "user_id", "order_line", "tax_totals", "state", "portfolio_approval_state"}},
}


def reports():
    identities = {key: {"model": model, "id": 101 + i} for i, (key, model) in enumerate(M1_MODELS.items())}
    acceptance = {"namespace": "portfolio_trade_acceptance", "identities": identities,
                  "record_counts": dict(Counter(M1_MODELS.values())), "bearing_quantity": 40.0,
                  "bearing_move_count": 1, "initial_bearing_move_id": identities["initial_bearing_move"]["id"]}
    static = {key: {"model": model, "id": 501 + i} for i, (key, model) in enumerate(PROFILE_MODELS.items())}
    company, source = identities["company_a"]["id"], static["source"]["id"]
    profile = {"schema_version": 1, "database": "odoo_portfolio", "namespace": "portfolio_order_bridge_acceptance",
               "company_id": company, "source_id": source, "identities": static, "cron_active": False,
               "borrowed_m1_identities": copy.deepcopy(identities), "borrowed_finance_pricelist_id": 600}
    flow_ids = {key: {"model": model, "id": 701 + i} for i, (key, model) in enumerate(FLOW_MODELS.items())}
    integration, operator = (static[f"user_{r}"]["id"] for r in ("integration", "operator"))
    flow = {"schema_version": 1, "database": "odoo_portfolio", "namespace": "portfolio_order_bridge_flow",
            "company_id": company, "source_id": source, "identities": flow_ids,
            "jobs": {suffix: {"id": flow_ids[f"job_{suffix}"]["id"], "state": "done",
                              "attempt_count": 1 if suffix == "success" else 2,
                              "submitted_by_id": integration, "processed_by_id": operator}
                     for suffix in ("success", "repaired")},
            "quotes": {suffix: {"id": flow_ids[f"quote_{suffix}"]["id"], "name": f"S{index}",
                                "state": "draft", "currency": "CNY", "amount_untaxed": amount,
                                "amount_total": amount, "owner_id": identities["user_sales"]["id"],
                                "created_by_id": operator, "approval_required": True}
                       for index, (suffix, amount) in enumerate((("success", 309.0), ("repaired", 161.5)))},
            "events": {key: {"id": flow_ids[key]["id"], "job_id": flow_ids[f"job_{suffix}"]["id"],
                             "event_type": kind, "attempt_no": ordinal, "actor_id": operator,
                             "retry_reason": "Acceptance SKU mapping repaired" if kind == "retry" else None}
                       for key, suffix, kind, ordinal in (("attempt_success", "success", "done", 1),
                                                         ("attempt_failed", "repaired", "failed", 1),
                                                         ("attempt_retry", "repaired", "retry", 1),
                                                         ("attempt_repaired", "repaired", "done", 2))},
            "m1_preservation": {"fingerprint": "a" * 64, "borrowed_m1_identities": copy.deepcopy(identities),
                                "bearing_stock": 10.0, "bearing_move_ids": [130, 131, 132]}}
    return acceptance, profile, flow


def role_evidence(role, acceptance, profile, flow):
    static = profile["identities"]
    company = acceptance["identities"]["company_b" if role == "other_sales" else "company_a"]["id"]
    uid = (static if role in ROLES[:3] else acceptance["identities"])[f"user_{role}"]["id"]
    groups = {group: False for group in GROUPS}
    groups["base.group_user"] = True
    for group in {"settings": ("base.group_system", "base.group_erp_manager"),
                  "operator": ("portfolio_order_bridge.group_import_operator", "sales_team.group_sale_salesman"),
                  "integration": ("portfolio_order_bridge.group_import_integration",),
                  "sales": ("sales_team.group_sale_salesman",),
                  "other_sales": ("sales_team.group_sale_salesman",)}[role]:
        groups[group] = True
    menus, actions, views = {}, {}, {}
    for key in ROLE_MENUS[role]:
        index, model = list(MENU_MODELS).index(key), MENU_MODELS[key]
        module, prefix = ("portfolio_trade", "portfolio_") if key == "sales" else ("portfolio_order_bridge", "import_")
        menus[key] = {"id": 801 + index, "xmlid": f"{module}.menu_{prefix}{key}",
                      "parent_id": 800 if key == "sales" else 811 if role == "settings" else 810,
                      "action": f"ir.actions.act_window,{901 + index}"}
        actions[key] = {"id": 901 + index, "res_model": model, "view_mode": "list,form"}
        views[model] = {}
        for kind in ("list", "form"):
            attributes = ' create="false" edit="false" delete="false"' if model in {JOB, ATTEMPT} else ""
            buttons = ('<button name="action_open_retry" invisible="state != &apos;failed&apos;"/>'
                       if model == JOB and role == "operator" and kind == "form" else "")
            fields = "".join('<field name="tax_totals" widget="account-tax-totals-field" readonly="1"/>'
                             if model == "sale.order" and kind == "form" and name == "tax_totals" else f'<field name="{name}"/>'
                             for name in sorted(VIEW_FIELDS[model][kind]))
            views[model][kind] = {"arch": f"<{kind}{attributes}>{buttons}{fields}</{kind}>"}
    return {"uid": uid, "company_id": company, "allowed_company_ids": [company], "active_company_ids": [company],
            "groups": groups, "menus": menus, "actions": actions, "views": views,
            "menu_roots": {"trade": {"id": 800, "parent_id": False},
                           **({"import": {"id": 810, "parent_id": 800}} if role in ROLES[:3] else {}),
                           **({"config": {"id": 811, "parent_id": 810}} if role == "settings" else {})},
            "source_id": profile["source_id"] if role in ROLES[:3] else None,
            "mapping_ids": {"customer": [static["customer_map"]["id"]],
                            "product": [static["product_map_item1"]["id"], static["product_map_item2"]["id"],
                                        flow["identities"]["product_map_repair"]["id"]]} if role in {"settings", "operator"} else {},
            "cron": {"id": static["processing_cron"]["id"], "active": False,
                     "processor_uid": static["user_operator"]["id"], "fixed_code": True, "source_link": True}
                    if role == "settings" else None,
            "jobs": copy.deepcopy(flow["jobs"]) if role in {"operator", "integration"} else {},
            "quotes": copy.deepcopy(flow["quotes"]) if role in {"operator", "sales"} else {},
            "events": copy.deepcopy(flow["events"]) if role in {"operator", "integration"} else {},
            "checks": {key: True for key in COMMON_CHECKS | ROLE_CHECKS[role]}}


class Response:
    def __init__(self, payload=None, *, status=200, raw=None):
        self.status = status
        self.payload = json.dumps({"jsonrpc": "2.0", "id": 1, "result": payload}).encode() if raw is None else raw

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size=-1):
        return self.payload[:size] if size >= 0 else self.payload


def native_records(acceptance, profile, flow):
    """Independent fixture for the documented completed native demonstration."""
    static, old, ids = profile["identities"], acceptance["identities"], flow["identities"]
    company, source = profile["company_id"], profile["source_id"]
    operator, integration, sales = (static["user_operator"]["id"], static["user_integration"]["id"], old["user_sales"]["id"])
    data = {model: {} for model in (SOURCE, JOB, ATTEMPT, CUSTOMER_MAP, PRODUCT_MAP, "ir.cron", "ir.model",
                                    "product.product", "sale.order", "sale.order.line", "res.company", "res.currency", "res.users")}
    data[SOURCE][source] = {"id": source, "name": "Portfolio bridge acceptance source", "active": True,
                            "company_id": company, "company_code": "PARTS-A", "code": "B2B-ACCEPTANCE",
                            "warehouse_id": old["warehouse_a"]["id"], "pricelist_id": profile["borrowed_finance_pricelist_id"],
                            "sales_user_id": sales, "processor_user_id": operator, "integration_user_ids": [integration],
                            "processing_cron_id": static["processing_cron"]["id"], "ingress_revision": 1}
    for role in ROLES:
        evidence = role_evidence(role, acceptance, profile, flow)
        data["res.users"][evidence["uid"]] = {"id": evidence["uid"], "active": True, "share": False,
                                              "company_id": evidence["company_id"], "company_ids": [evidence["company_id"]]}
    data["res.company"][company] = {"id": company, "portfolio_approval_enabled": True, "portfolio_approval_amount": 10000,
                                    "portfolio_approval_discount": 10, "portfolio_approval_policy_version": 1}
    data["res.currency"][1] = {"id": 1, "name": "CNY"}
    data["ir.model"][1] = {"id": 1, "model": JOB}
    cron_id = static["processing_cron"]["id"]
    data["ir.cron"][cron_id] = {"id": cron_id, "active": False, "user_id": operator, "state": "code",
                                "code": "model._process_pending()", "model_id": 1, "interval_number": 1,
                                "interval_type": "minutes", "ir_actions_server_id": static["processing_server_action"]["id"]}
    for i in (1, 2):
        product_id = static[f"product_item{i}"]["id"]
        data["product.product"][product_id] = {"id": product_id, "default_code": f"BRIDGE-ACCEPTANCE-{i}",
                                              "active": True, "uom_id": 1, "taxes_id": [], "supplier_taxes_id": []}
    customer_map = static["customer_map"]["id"]
    data[CUSTOMER_MAP][customer_map] = {"id": customer_map, "source_id": source, "company_id": company, "active": True,
                                      "external_ref": "BRIDGE-ACCEPTANCE-CUSTOMER", "partner_id": static["customer"]["id"]}
    for key, sku, index in (("product_map_item1", "BRIDGE-ACCEPTANCE-1", 1),
                            ("product_map_item2", "BRIDGE-ACCEPTANCE-2", 2),
                            ("product_map_repair", "BRIDGE-ACCEPTANCE-REPAIR", 2)):
        mapping_id = (ids if key == "product_map_repair" else static)[key]["id"]
        data[PRODUCT_MAP][mapping_id] = {"id": mapping_id, "source_id": source, "company_id": company,
                                         "active": True, "external_sku": sku, "product_id": static[f"product_item{index}"]["id"]}
    line_id = 1001
    payloads = {}
    for suffix, external, request_id, lines in (
        ("success", "BRIDGE-ACCEPTANCE-SUCCESS", "local-import-success",
         [("2", "BRIDGE-ACCEPTANCE-2", "3", "35", "0"), ("1", "BRIDGE-ACCEPTANCE-1", "2", "120", "15")]),
        ("repaired", "BRIDGE-ACCEPTANCE-RETRY", "local-import-repair",
         [("2", "BRIDGE-ACCEPTANCE-1", "1", "120", "15"), ("1", "BRIDGE-ACCEPTANCE-REPAIR", "2", "35", "15")]),
    ):
        payload = {"company_code": "PARTS-A", "source_code": "B2B-ACCEPTANCE", "customer_ref": "BRIDGE-ACCEPTANCE-CUSTOMER",
                   "currency": "CNY", "sent_at": "2026-11-04T00:00:00Z", "request_id": request_id,
                   "external_order_id": external, "lines": [dict(zip(("external_line_id", "sku", "quantity", "unit_price", "discount"), line)) for line in lines]}
        payloads[suffix] = payload
        canonical = {key: value for key, value in payload.items() if key not in {"request_id", "sent_at"}}
        canonical["lines"] = sorted(payload["lines"], key=lambda row: row["external_line_id"])
        canonical_text = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        job_id, quote_id = ids[f"job_{suffix}"]["id"], ids[f"quote_{suffix}"]["id"]
        processed_at = "2026-11-04 01:00:00" if suffix == "success" else "2026-11-04 03:00:00"
        data[JOB][job_id] = {"id": job_id, "source_id": source, "company_id": company, "external_order_id": external,
                             "payload_hash": hashlib.sha256(canonical_text.encode()).hexdigest(), "raw_payload": json.dumps(payload),
                             "canonical_payload": canonical_text, "submitted_by_id": integration, "create_uid": integration,
                             "received_at": "2026-11-04 00:00:00", "last_attempt_at": processed_at, "state": "done",
                             "attempt_count": flow["jobs"][suffix]["attempt_count"], "error_code": False, "error_detail": False,
                             "sale_order_id": quote_id, "create_date": "2026-11-04 00:00:00", "write_date": processed_at}
        quote = {"id": quote_id, "name": flow["quotes"][suffix]["name"], "company_id": company, "state": "draft",
                 "currency_id": 1, "amount_untaxed": flow["quotes"][suffix]["amount_untaxed"], "amount_total": flow["quotes"][suffix]["amount_total"],
                 "amount_tax": 0, "user_id": sales, "create_uid": operator, "portfolio_creator_id": operator,
                 "partner_id": static["customer"]["id"], "partner_invoice_id": static["customer"]["id"],
                 "partner_shipping_id": static["customer"]["id"], "warehouse_id": old["warehouse_a"]["id"],
                 "pricelist_id": profile["borrowed_finance_pricelist_id"], "origin": "B2B-ACCEPTANCE:" + external,
                 "client_order_ref": "B2B-ACCEPTANCE:" + external, "order_line": [], "portfolio_approval_enabled": True,
                 "portfolio_approval_current": False, "portfolio_approval_state": "draft", "portfolio_approval_history_ids": [],
                 "portfolio_approval_submitter_id": False, "portfolio_approval_fingerprint": False,
                 "portfolio_approval_policy_version": 0, "portfolio_revision": 1, "picking_ids": [], "invoice_ids": [], "write_date": processed_at}
        for _line, sku, quantity, price, discount in lines:
            product_index = 1 if sku == "BRIDGE-ACCEPTANCE-1" else 2
            data["sale.order.line"][line_id] = {"id": line_id, "order_id": quote_id, "product_id": static[f"product_item{product_index}"]["id"],
                                               "product_uom": 1, "product_uom_qty": float(quantity), "price_unit": float(price),
                                               "discount": float(discount), "tax_id": [], "display_type": False, "is_downpayment": False,
                                               "qty_delivered": 0, "qty_invoiced": 0, "invoice_lines": [], "move_ids": [], "create_uid": operator,
                                               "create_date": processed_at, "write_date": processed_at}
            quote["order_line"].append(line_id)
            line_id += 1
        data["sale.order"][quote_id] = quote
    for key, expected in flow["events"].items():
        kind, suffix = expected["event_type"], "success" if key == "attempt_success" else "repaired"
        time = "2026-11-04 01:00:00" if kind == "failed" or suffix == "success" else "2026-11-04 02:00:00" if kind == "retry" else "2026-11-04 03:00:00"
        mapping = {"customer": {"mapping_id": customer_map, "partner_id": static["customer"]["id"]}, "lines": []}
        for index, item in enumerate(payloads[suffix]["lines"]):
            repair = item["sku"] == "BRIDGE-ACCEPTANCE-REPAIR"
            if repair and kind in {"failed", "retry"}:
                break
            product_index = 1 if item["sku"] == "BRIDGE-ACCEPTANCE-1" else 2
            mapping["lines"].append({"line_index": index,
                                     "mapping_id": ids["product_map_repair"]["id"] if repair else static[f"product_map_item{product_index}"]["id"],
                                     "product_id": static[f"product_item{product_index}"]["id"], "uom_id": 1})
        row = {**expected, "retry_reason": expected["retry_reason"] or False, "company_id": company, "create_uid": operator,
               "started_at": time, "finished_at": time, "create_date": time, "write_date": time,
               "error_code": "product_mapping" if kind == "failed" else False,
               "error_path": "lines[1].sku" if kind == "failed" else False,
               "error_detail": "An active product mapping is required." if kind == "failed" else False,
               "mapping_snapshot": json.dumps(mapping), "source_defaults_snapshot": json.dumps({
                   "source_id": source, "company_id": company, "warehouse_id": old["warehouse_a"]["id"],
                   "pricelist_id": profile["borrowed_finance_pricelist_id"], "sales_user_id": sales, "processor_user_id": operator})}
        data[ATTEMPT][row["id"]] = row
    return data


class NativeWire:
    """Tiny wire fixture with native shapes; it never opens a socket."""

    def __init__(self, acceptance, profile, flow):
        self.acceptance, self.profile, self.flow = acceptance, profile, flow
        self.data = native_records(acceptance, profile, flow)
        self.sessions, self.calls = [], []
        self.break_on_denial = None
        self.native_context = True
        self.export_groups = {role: False for role in ROLES}

    def opener(self, *_handlers):
        wire = self

        class Opener:
            role = None

            def open(self, request, timeout):
                if timeout != 30:
                    raise AssertionError("HTTP timeout must be bounded")
                message = json.loads(request.data)
                params, endpoint = message["params"], request.full_url.removeprefix("http://127.0.0.1:8069")
                if endpoint == "/web/session/authenticate":
                    self.role = params["login"].split(".")[-1]
                    wire.sessions.append(self.role)
                    evidence = role_evidence(self.role, wire.acceptance, wire.profile, wire.flow)
                    company = evidence["company_id"]
                    context = {"uid": evidence["uid"], "lang": "en_US"} if wire.native_context else {"allowed_company_ids": [company]}
                    return Response({"uid": evidence["uid"], "user_context": context, "user_companies": {
                        "current_company": company, "allowed_companies": {str(company): {"id": company}}}})
                if self.role is None:
                    raise AssertionError("Native reads need a role's authenticated session")
                return wire.dispatch(self.role, endpoint, params)

        return Opener()

    def denied(self):
        if self.break_on_denial:
            self.break_on_denial()
        return Response(raw=json.dumps({"error": {"code": 200, "message": "PRIVATE-ERROR", "data": {
            "name": "odoo.exceptions.AccessError", "debug": "PRIVATE-DEBUG", "message": "PRIVATE-MESSAGE"}}}).encode())

    def matches(self, model, row, domain):
        """Evaluate ordinary Odoo OR/AND domains against the independent fixture."""
        def values(current_model, record, path):
            name, separator, rest = path.partition(".")
            value = record[name]
            if not separator:
                return value if isinstance(value, list) else [value]
            related = {("sale.order", "order_line"): "sale.order.line", ("sale.order.line", "order_id"): "sale.order"}[(current_model, name)]
            ids = value if isinstance(value, list) else [value]
            return [item for record_id in ids for item in values(related, self.data[related][record_id], rest)]

        def term(expression):
            field, operator, expected = expression
            actual = values(model, row, field)
            if operator == "=":
                return expected in actual
            if operator == "in":
                return any(value in expected for value in actual)
            if operator in {"like", "=like", "ilike", "=ilike"}:
                pattern = re.escape(expected).replace("%", ".*").replace("_", ".")
                return any(re.fullmatch(pattern, str(value), re.I if "ilike" in operator else 0) for value in actual)
            raise AssertionError("Unexpected fixture domain operator")

        tokens = iter(domain)

        def expression(token):
            if token in ("|", "&"):
                left, right = expression(next(tokens)), expression(next(tokens))
                return left or right if token == "|" else left and right
            if token == "!":
                return not expression(next(tokens))
            return term(token)

        return all([expression(token) for token in tokens])

    def dispatch(self, role, endpoint, params):
        evidence = role_evidence(role, self.acceptance, self.profile, self.flow)
        if endpoint == "/web/action/load":
            key = params["action_id"].split("action_")[-1].removeprefix("portfolio_").removeprefix("import_")
            return Response(evidence["actions"][key])
        model, method, args, kwargs = (params[name] for name in ("model", "method", "args", "kwargs"))
        self.calls.append((role, model, method, copy.deepcopy(args), copy.deepcopy(kwargs)))
        if (model, method) == ("res.users", "has_group"):
            if len(args) != 2 or args[0] != [evidence["uid"]]:
                raise AssertionError("Native has_group arity and actual UID")
            if args[1] == "base.group_allow_export":
                return Response(self.export_groups[role])
            return Response(evidence["groups"][args[1]])
        if (model, method) == ("ir.ui.menu", "load_menus"):
            menus = copy.deepcopy(evidence["menus"])
            for key, value in evidence["menu_roots"].items():
                menus[key] = {**value, "xmlid": {"trade": "portfolio_trade.menu_portfolio_root",
                                               "import": "portfolio_order_bridge.menu_import_root", "config": "portfolio_order_bridge.menu_import_config"}[key]}
            for menu in menus.values():
                if menu["parent_id"]:
                    menu["parent_id"] = [menu["parent_id"], "Native parent"]
            return Response(menus)
        if method == "get_view":
            return Response(evidence["views"][model][kwargs["view_type"]])
        context = kwargs["context"]
        if model.startswith("portfolio.import.") and (context["allowed_company_ids"] != [evidence["company_id"]] or method == "exists"):
            return self.denied()
        if method in {"create", "write", "unlink", "copy"}:
            return self.denied()
        if model == JOB and method in {"action_process_pending", "action_retry", "action_open_retry"}:
            if role != "operator":
                return self.denied()
            if method == "action_process_pending":
                return Response({"type": "ir.actions.client", "tag": "display_notification", "params": {
                    "type": "success", "message": "Processed 0 pending import jobs."}})
            return Response(raw=json.dumps({"error": {"code": 200, "data": {"name": "odoo.exceptions.UserError",
                "message": "Only a failed import job can be retried manually."}}}).encode())
        if model == JOB and method == "submit_order":
            suffix = "success" if args[0]["external_order_id"] == "BRIDGE-ACCEPTANCE-SUCCESS" else "repaired"
            if role != "integration" or args[0] != json.loads(self.data[JOB][self.flow["jobs"][suffix]["id"]]["raw_payload"]):
                raise AssertionError("Only exact idempotent reception is permitted")
            return Response({"job_id": self.flow["jobs"][suffix]["id"], "state": "done", "sale_order_id": self.flow["quotes"][suffix]["id"], "reused": True})
        if model == "sale.order" and method == "action_confirm":
            if role != "sales":
                raise AssertionError("Only responsible Sales confirmation guard")
            return Response(raw=json.dumps({"error": {"code": 200, "data": {"name": "odoo.exceptions.UserError",
                "message": "This quotation requires current approval before confirmation. Submit it for approval."}}}).encode())
        if model in {JOB, ATTEMPT} and role not in {"operator", "integration"}:
            return self.denied()
        if model in {CUSTOMER_MAP, PRODUCT_MAP} and role not in {"operator", "settings"}:
            return self.denied()
        if model == SOURCE:
            if role not in ROLES[:3]:
                return self.denied()
            private = {"processing_cron_id"} if role == "operator" else {
                "processing_cron_id", "ingress_revision", "pricelist_id", "warehouse_id", "sales_user_id", "processor_user_id", "integration_user_ids"} if role == "integration" else set()
            requested = set(kwargs.get("fields", kwargs.get("specification", {})))
            if method in {"search", "search_count"}:
                requested |= {term[0] for term in args[0]}
            elif method == "read_group":
                requested |= set(args[1]) | set(args[2])
            if requested & private:
                return self.denied()
        if model in {"sale.order", "sale.order.line"} and role not in {"sales", "operator"}:
            if method == "search" and role == "other_sales":
                return Response([])
            # The pinned native id-only web_read bypasses read rules. The
            # verifier must request a real protected field for native SOs.
            if method == "web_read" and set(kwargs["specification"]) == {"id"}:
                return Response([{"id": record_id} for record_id in args[0]])
            return self.denied()
        if method in {"read", "web_read"}:
            fields = kwargs.get("fields", list(kwargs.get("specification", {})))
            rows = []
            for record_id in args[0]:
                record = self.data[model][record_id]
                rows.append({"id": record_id, **{field: copy.deepcopy(record[field]) for field in fields}})
            return Response(rows)
        if method in {"search", "search_count"}:
            rows = list(self.data[model].values())
            rows = [row for row in rows if self.matches(model, row, args[0])]
            return Response(len(rows) if method == "search_count" else [row["id"] for row in rows])
        if method == "read_group" and model == SOURCE:
            return Response([{"company_id": [evidence["company_id"], "PARTS-A"], "company_id_count": 1}])
        if method == "export_data":
            if role == "settings":
                return Response({"datas": [["portfolio_order_bridge_acceptance.source"]]})
            return self.denied()  # Native group_allow_export is independent.
        raise AssertionError(f"Unexpected native wire call: {role} {model}.{method}")


class BridgeHttpTests(unittest.TestCase):
    def setUp(self):
        script = ROOT / "scripts" / "verify_order_bridge_roles.py"
        self.assertTrue(script.is_file(), "Authenticated order bridge HTTP verifier is missing")
        spec = importlib.util.spec_from_file_location("bridge_http_under_test", script)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        for name in ("load_inputs", "validate_reports", "validate_role_evidence", "validate_evidence",
                     "collect_role_evidence", "JsonRpcClient", "expected_denial", "main"):
            self.assertTrue(callable(getattr(self.module, name, None)), f"Bridge HTTP API {name} is missing")
        base = ROOT / ".runtime" / "tests"
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.acceptance, self.profile, self.flow = reports()
        self.credentials = {"schema_version": 1, "database": "odoo_portfolio", "accounts": {
            role: {"login": f"portfolio.bridge.acceptance.{role}", "password": f"PRIVATE-{role}-" + "x" * 32}
            for role in ROLES[:3]}}
        self.sales_credentials = {"schema_version": 1, "database": "odoo_portfolio", "accounts": {
            role: {"login": f"portfolio.acceptance.{role}", "password": f"PRIVATE-OLD-{role}-" + "y" * 32}
            for role in OLD_ROLES}}
        self.paths = {key: self.directory / f"{key}.json" for key in (
            "credentials", "sales_credentials", "acceptance", "profile", "flow", "output")}
        for key in self.paths.keys() - {"output"}:
            self.paths[key].write_text(json.dumps(getattr(self, key)))
        for key in ("credentials", "sales_credentials"):
            self.paths[key].chmod(0o600)

    def load(self, **overrides):
        kwargs = {"base_url": "http://127.0.0.1:8069", "database": "odoo_portfolio",
                  "credentials_path": self.paths["credentials"], "sales_credentials_path": self.paths["sales_credentials"],
                  "acceptance_report_path": self.paths["acceptance"], "profile_report_path": self.paths["profile"],
                  "flow_report_path": self.paths["flow"], "output_path": self.paths["output"]}
        return self.module.load_inputs(**(kwargs | overrides))

    def evidence(self, role):
        return role_evidence(role, self.acceptance, self.profile, self.flow)

    def argv(self):
        return ["--base-url", "http://127.0.0.1:8069", "--database", "odoo_portfolio",
                "--credentials", str(self.paths["credentials"]), "--sales-credentials", str(self.paths["sales_credentials"]),
                "--acceptance-report", str(self.paths["acceptance"]), "--profile-report", str(self.paths["profile"]),
                "--flow-report", str(self.paths["flow"]), "--output", str(self.paths["output"])]

    def test_standard_library_preflight_never_imports_odoo(self):
        original = builtins.__import__

        def guarded(name, *args, **kwargs):
            self.assertFalse(name == "odoo" or name.startswith("odoo."))
            return original(name, *args, **kwargs)

        with patch("builtins.__import__", guarded):
            inputs = self.load()
        self.assertEqual(inputs["profile"]["source_id"], self.profile["source_id"])
        self.assertEqual(set(inputs["credentials"]["accounts"]), set(ROLES[:3]))

    def test_only_exact_loopback_http_origin_and_two_databases_are_allowed(self):
        urls = ("https://127.0.0.1:8069", "http://localhost:8069", "http://[::1]:8069", "http://127.0.0.1",
                "http://127.0.0.1:0", "http://127.0.0.1:65536", "http://u:p@127.0.0.1:8069",
                "http://127.0.0.1:8069/", "http://127.0.0.1:8069?a=b", "http://127.0.0.1:8069#x",
                "http://127.0.0.1:8069?", "http://127.0.0.1:8069#", " HTTP://127.0.0.1:8069")
        with patch("urllib.request.build_opener", side_effect=AssertionError("Preflight must not open network")):
            for url in urls:
                with self.subTest(url=url), self.assertRaises(ValueError):
                    self.load(base_url=url)
            for database in ("production", "", None, True):
                with self.subTest(database=database), self.assertRaises(ValueError):
                    self.load(database=database)

    def test_private_credentials_require_mode_owner_role_schema_database_and_no_symlinks(self):
        for key in ("credentials", "sales_credentials"):
            path, original = self.paths[key], self.paths[key].read_bytes()
            path.chmod(0o644)
            with self.subTest(key=key, cause="mode"), self.assertRaises(ValueError):
                self.load()
            path.chmod(0o600)
            data = json.loads(original)
            for invalid in ({**data, "database": "odoo_portfolio_m1_test"},
                            {**data, "accounts": {}}, {**data, "schema_version": True}):
                path.write_text(json.dumps(invalid))
                with self.subTest(key=key, cause="schema"), self.assertRaises(ValueError):
                    self.load()
            path.write_bytes(original)
            linked = self.directory / f"linked-{key}.json"
            linked.symlink_to(path)
            with self.assertRaises(ValueError):
                self.load(**{f"{key}_path": linked})
        with patch("os.getuid", return_value=os.getuid() + 1), self.assertRaises(ValueError):
            self.load()

    def test_paths_reject_outside_runtime_symlink_parent_duplicate_inputs_and_output_aliases(self):
        for key in ("credentials_path", "sales_credentials_path", "acceptance_report_path",
                    "profile_report_path", "flow_report_path", "output_path"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.load(**{key: ROOT / "outside.json"})
        linked = self.directory / "linked"
        linked.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.load(profile_report_path=linked / "profile.json")
        for path in self.paths.values():
            if path == self.paths["output"]:
                continue
            with self.subTest(path=path.name), self.assertRaises(ValueError):
                self.load(output_path=path)
        hardlink = self.directory / "alias.json"
        os.link(self.paths["credentials"], hardlink)
        with self.assertRaises(ValueError):
            self.load(output_path=hardlink)
        with self.assertRaises(ValueError):
            self.load(flow_report_path=self.paths["profile"])

    def test_reports_enforce_models_identity_counts_namespace_and_actor_ownership(self):
        mutations = (
            ("acceptance", lambda r: r.update(namespace="wrong")),
            ("acceptance", lambda r: r["record_counts"].update({"res.users": 6})),
            ("acceptance", lambda r: r["identities"]["company_b"].update(id=r["identities"]["company_a"]["id"])),
            ("profile", lambda r: r.update(schema_version=True)),
            ("profile", lambda r: r.update(database="odoo_portfolio_m1_test")),
            ("profile", lambda r: r.update(source_id=999)),
            ("profile", lambda r: r.update(cron_active=True)),
            ("profile", lambda r: r["identities"]["user_operator"].update(id=r["identities"]["user_settings"]["id"])),
            ("profile", lambda r: r["identities"]["user_operator"].update(id=self.acceptance["identities"]["user_sales"]["id"])),
            ("profile", lambda r: r["identities"]["source"].update(model=JOB)),
            ("profile", lambda r: r["borrowed_m1_identities"]["user_sales"].update(id=999)),
            ("flow", lambda r: r.update(namespace="wrong")),
            ("flow", lambda r: r["identities"].pop("attempt_retry")),
            ("flow", lambda r: r["identities"]["job_repaired"].update(id=r["identities"]["job_success"]["id"])),
            ("flow", lambda r: r["identities"]["quote_success"].update(id=True)),
            ("flow", lambda r: r["jobs"]["success"].update(submitted_by_id=999)),
            ("flow", lambda r: r["quotes"]["success"].update(created_by_id=999)),
            ("flow", lambda r: r["quotes"]["repaired"].update(owner_id=999)),
        )
        for key, mutate in mutations:
            values = {name: copy.deepcopy(getattr(self, name)) for name in ("acceptance", "profile", "flow")}
            mutate(values[key])
            with self.subTest(key=key, mutate=mutate), self.assertRaises(ValueError):
                self.module.validate_reports(values["acceptance"], values["profile"], values["flow"], "odoo_portfolio")

    def test_reports_enforce_two_draft_cny_quotes_jobs_attempts_reason_and_preservation(self):
        mutations = (
            lambda r: r["jobs"]["success"].update(state="failed"),
            lambda r: r["jobs"]["repaired"].update(attempt_count=1),
            lambda r: r["quotes"]["success"].update(state="sale"),
            lambda r: r["quotes"]["success"].update(currency="USD"),
            lambda r: r["quotes"]["success"].update(amount_total=310),
            lambda r: r["quotes"]["repaired"].update(amount_untaxed=161.0),
            lambda r: r["quotes"]["success"].update(approval_required=False),
            lambda r: r["events"]["attempt_retry"].update(attempt_no=2),
            lambda r: r["events"]["attempt_retry"].update(retry_reason="wrong"),
            lambda r: r["events"]["attempt_failed"].update(event_type="done"),
            lambda r: r["events"]["attempt_repaired"].update(job_id=r["jobs"]["success"]["id"]),
            lambda r: r["m1_preservation"].update(fingerprint="invalid"),
            lambda r: r["m1_preservation"].update(bearing_stock=9),
            lambda r: r["m1_preservation"]["borrowed_m1_identities"].pop("user_sales"),
            lambda r: r["quotes"]["success"].update(raw_payload="PRIVATE-SOURCE"),
        )
        for mutate in mutations:
            flow = copy.deepcopy(self.flow)
            mutate(flow)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                self.module.validate_reports(self.acceptance, self.profile, flow, "odoo_portfolio")

    def test_reports_are_bounded_json_and_reject_duplicate_keys(self):
        for content in ('{"namespace":1,"namespace":2}', "PRIVATE-HTML", "[1,2]", "x" * (8 * 1024 * 1024 + 1)):
            self.paths["flow"].write_text(content)
            with self.subTest(size=len(content)), self.assertRaises(ValueError) as raised:
                self.load()
            self.assertNotIn("PRIVATE", str(raised.exception))

    def test_complete_five_role_evidence_produces_fixed_safe_summary(self):
        inputs = self.load()
        report = self.module.validate_evidence({role: self.evidence(role) for role in ROLES}, inputs)
        self.assertIs(report["ok"], True)
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(set(report["roles"]), set(ROLES))
        self.assertEqual(report["profile_source_id"], self.profile["source_id"])
        self.assertEqual(report["roles"]["sales"]["quotes"], self.flow["quotes"])
        text = json.dumps(report).lower()
        for forbidden in ("private-", "password", "cookie", "session_id", "payload", "debug", "fingerprint", "connection"):
            self.assertNotIn(forbidden, text)

    def test_role_validator_rejects_missing_checks_false_checks_partial_pages_or_raw_secrets(self):
        mutations = (
            lambda e: e["checks"].pop("business_unchanged"),
            lambda e: e["checks"].update(healthy_after_negatives=False),
            lambda e: e["checks"].update(fake=True),
            lambda e: e["checks"].update(session=1),
            lambda e: e["menus"].pop(next(iter(e["menus"]))),
            lambda e: e["menu_roots"].pop("trade"),
            lambda e: e["actions"].pop(next(iter(e["actions"]))),
            lambda e: e["views"].pop(next(iter(e["views"]))),
            lambda e: e.update(raw_payload="PRIVATE-RAW"),
            lambda e: e.update(company_id=float(e["company_id"])),
            lambda e: e.update(groups={"base.group_user": True}),
            lambda e: e["menus"][next(iter(e["menus"]))].update(raw_body="PRIVATE-BODY"),
        )
        for role in ROLES:
            for mutate in mutations:
                evidence = self.evidence(role)
                mutate(evidence)
                with self.subTest(role=role, mutate=mutate), self.assertRaises(ValueError):
                    self.module.validate_role_evidence(role, evidence, self.load())
        with self.assertRaises(ValueError):
            self.module.validate_evidence({"operator": self.evidence("operator")}, self.load())

    def test_role_authentication_company_groups_menus_actions_and_xml_must_match(self):
        mutations = (
            lambda e: e.update(uid=999), lambda e: e.update(company_id=999),
            lambda e: e.update(allowed_company_ids=[101, 102]), lambda e: e.update(active_company_ids=[]),
            lambda e: e["groups"].update({"base.group_user": False}),
            lambda e: e["groups"].update({"sales_team.group_sale_salesman_all_leads": True}),
            lambda e: e["groups"].update({"stock.group_stock_user": True}),
            lambda e: e["actions"][next(iter(e["actions"]))].update(res_model="res.users"),
            lambda e: e["menus"][next(iter(e["menus"]))].update(action="ir.actions.act_window,999"),
            lambda e: e["views"][next(iter(e["views"]))].update(form={"arch": "PRIVATE-MALFORMED"}),
            lambda e: e["views"][next(iter(e["views"]))].update(list={"arch": '<list attrs="{}"/>'}),
            lambda e: e["views"][next(iter(e["views"]))].update(form={"arch": '<form><field name="id"/></form>'}),
        )
        for role in ROLES:
            for mutate in mutations:
                evidence = self.evidence(role)
                mutate(evidence)
                with self.subTest(role=role, mutate=mutate), self.assertRaises(ValueError):
                    self.module.validate_role_evidence(role, evidence, self.load())

    def test_role_records_must_match_owned_flow_states_and_complete_history(self):
        for role, section, key, field, value in (
            ("operator", "jobs", "success", "attempt_count", 2),
            ("integration", "events", "attempt_retry", "retry_reason", "wrong"),
            ("sales", "quotes", "success", "state", "sale"),
            ("sales", "quotes", "repaired", "amount_total", 160),
            ("operator", "quotes", "success", "owner_id", 999),
        ):
            evidence = self.evidence(role)
            evidence[section][key][field] = value
            with self.subTest(role=role, field=field), self.assertRaises(ValueError):
                self.module.validate_role_evidence(role, evidence, self.load())
        evidence = self.evidence("settings")
        evidence["cron"]["active"] = True
        with self.assertRaises(ValueError):
            self.module.validate_role_evidence("settings", evidence, self.load())
        evidence["cron"]["active"] = 0
        with self.assertRaises(ValueError):
            self.module.validate_role_evidence("settings", evidence, self.load())

    def test_invalid_authenticated_identity_is_fatal_before_menu_or_business_operations(self):
        inputs = self.load()
        evidence = self.evidence("integration")
        company = evidence["company_id"]
        valid = {"uid": evidence["uid"], "user_context": {"allowed_company_ids": [company]},
                 "user_companies": {"current_company": company, "allowed_companies": {str(company): {"id": company}}}}
        for mutate in (lambda r: r.update(uid=999), lambda r: r.update(uid=False),
                       lambda r: r["user_context"].update(allowed_company_ids=[company, 102]),
                       lambda r: r["user_companies"].update(current_company=102),
                       lambda r: r["user_companies"]["allowed_companies"].update({"102": {"id": 102}})):
            result = copy.deepcopy(valid)
            mutate(result)
            client = Mock()
            client.rpc.return_value = result
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                self.module.collect_role_evidence(client, "integration", inputs)
            client.call.assert_not_called()

    def test_expected_denial_requires_the_specific_requested_semantic_class(self):
        raw = json.dumps({"error": {"code": 200, "data": {"name": "odoo.exceptions.UserError"}}}).encode()
        client, _opener = self.client(Response(raw=raw))
        with self.assertRaises(ValueError):
            self.module.expected_denial(lambda: client.call(JOB, "write", [[701], {}]), {"odoo.exceptions.AccessError"})
        with self.assertRaises(ValueError):
            self.module.expected_denial(lambda: (_ for _ in ()).throw(self.module.VerificationError()),
                                        {"odoo.exceptions.AccessError"})

    def test_rpc_cannot_send_credentials_or_cookies_to_unapproved_routes(self):
        client, opener = self.client()
        for route in ("https://remote.invalid/", "//remote.invalid/", "/../private", "/web/session/authenticate?x=1"):
            with self.subTest(route=route), self.assertRaises(ValueError):
                client.rpc(route, {"password": "PRIVATE-PASSWORD"})
        opener.open.assert_not_called()

    def client(self, response=None, error=None):
        opener = Mock()
        opener.open.side_effect = error
        opener.open.return_value = response or Response(True)
        return self.module.JsonRpcClient("http://127.0.0.1:8069", "integration", opener=opener), opener

    def test_rpc_has_independent_cookie_jars_no_proxy_no_redirect_bounded_reads_and_timeout(self):
        with patch("urllib.request.build_opener", return_value=Mock()) as build:
            self.module.JsonRpcClient("http://127.0.0.1:8069", "operator")
            first = build.call_args.args
            self.module.JsonRpcClient("http://127.0.0.1:8069", "sales")
            second = build.call_args.args
        jars = [[getattr(h, "cookiejar", None) for h in handlers if isinstance(getattr(h, "cookiejar", None), http.cookiejar.CookieJar)]
                for handlers in (first, second)]
        self.assertEqual([len(items) for items in jars], [1, 1])
        self.assertIsNot(jars[0][0], jars[1][0])
        self.assertTrue(any(getattr(h, "proxies", None) == {} for h in first))
        self.assertTrue(any(isinstance(h, self.module.RejectRedirect) for h in first))
        response = Mock(wraps=Response(True))
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.status = 200
        client, opener = self.client(response)
        self.assertIs(client.rpc("/web/dataset/call_kw", {}), True)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 30)
        response.read.assert_called_once_with(8 * 1024 * 1024 + 1)

    def test_only_allowlisted_actual_server_exceptions_count_as_expected_denials(self):
        for name in ("odoo.exceptions.AccessError", "odoo.exceptions.UserError", "odoo.exceptions.ValidationError"):
            raw = json.dumps({"error": {"code": 200, "message": "PRIVATE-ERROR", "data": {
                "name": name, "debug": "PRIVATE-DEBUG", "message": "PRIVATE-MESSAGE"}}}).encode()
            client, _opener = self.client(Response(raw=raw))
            self.assertIs(self.module.expected_denial(lambda: client.call(JOB, "write", [[701], {}]), {name}), True)

    def test_transport_expired_unknown_bad_json_html_oversize_redirect_never_count_as_denials(self):
        cases = [Response(raw=b"<html>PRIVATE-HTML</html>"), Response(raw=b"{PRIVATE-MALFORMED"),
                 Response(raw=b"x" * (8 * 1024 * 1024 + 1)), Response(True, status=302),
                 Response(raw=b'{"result":true,"result":false}'), Response(raw=b'{"unexpected":true}'),
                 Response(raw=json.dumps({"error": {"code": 100, "data": {"name": "odoo.http.SessionExpiredException",
                                                                              "debug": "PRIVATE-DEBUG"}}}).encode()),
                 Response(raw=json.dumps({"error": {"code": 200, "data": {"name": "builtins.KeyError",
                                                                              "message": "PRIVATE-EXCEPTION"}}}).encode()),
                 Response(raw=json.dumps({"error": {"code": 200, "message": "AccessError PRIVATE-FAKE"}}).encode())]
        for response in cases:
            client, _opener = self.client(response)
            with self.subTest(response=response.payload[:30]), self.assertRaises(ValueError) as raised:
                self.module.expected_denial(lambda: client.call(JOB, "write", [[701], {}]), {"odoo.exceptions.AccessError"})
            self.assertNotIn("PRIVATE", str(raised.exception))
        for error in (TimeoutError("PRIVATE-TIMEOUT"), urllib.error.URLError("PRIVATE-TRANSPORT"),
                      urllib.error.HTTPError("http://127.0.0.1:8069", 403, "PRIVATE-HTML", {}, None)):
            client, _opener = self.client(error=error)
            with self.subTest(error=type(error)), self.assertRaises(ValueError) as raised:
                self.module.expected_denial(lambda: client.call(JOB, "write", [[701], {}]), {"odoo.exceptions.AccessError"})
            self.assertNotIn("PRIVATE", str(raised.exception))
        with self.assertRaises(ValueError):
            self.module.expected_denial(lambda: True, {"odoo.exceptions.AccessError"})

    def test_group_rpc_binds_authenticated_actual_uid_and_checks_all_forbidden_groups(self):
        inputs, role = self.load(), "operator"
        evidence = self.evidence(role)
        group_calls = []

        class StopAfterGroups(Exception):
            pass

        class Boundary:
            def rpc(self, endpoint, params):
                if endpoint != "/web/session/authenticate":
                    raise AssertionError("Only authentication before group RPC")
                company = evidence["company_id"]
                return {"uid": evidence["uid"], "user_context": {"allowed_company_ids": [company]},
                        "user_companies": {"current_company": company, "allowed_companies": {str(company): {"id": company}}}}

            def call(self, model, method, args=None, kwargs=None):
                if (model, method) == ("ir.ui.menu", "load_menus"):
                    raise StopAfterGroups
                if (model, method) != ("res.users", "has_group"):
                    raise AssertionError("Expected group boundary")
                if len(args) != 2 or args[0] != [evidence["uid"]]:
                    raise AssertionError("has_group requires actual singleton user record")
                group_calls.append(args[1])
                return evidence["groups"][args[1]]

        with self.assertRaises(StopAfterGroups):
            self.module.collect_role_evidence(Boundary(), role, inputs)
        self.assertEqual(set(group_calls), set(GROUPS))

    def test_native_wire_collects_all_five_roles_and_business_probes_with_native_session_context(self):
        wire = NativeWire(self.acceptance, self.profile, self.flow)
        output = io.StringIO()
        with patch("urllib.request.build_opener", side_effect=wire.opener), contextlib.redirect_stdout(output):
            status = self.module.main(self.argv())
        self.assertEqual(status, 0, output.getvalue())
        report = json.loads(output.getvalue())
        self.assertEqual(set(wire.sessions), set(ROLES))
        self.assertEqual(len(wire.sessions), 5)
        self.assertEqual(set(report["roles"]), set(ROLES))
        for role, model, method in (("integration", JOB, "submit_order"), ("operator", JOB, "action_process_pending"),
                                     ("sales", "sale.order", "action_confirm"), ("settings", "ir.cron", "read"),
                                     ("other_sales", "sale.order", "web_read"), ("operator", SOURCE, "read_group")):
            self.assertTrue(any(call[:3] == (role, model, method) for call in wire.calls), (role, model, method))
        for role in ROLES:
            for model in (JOB, ATTEMPT):
                for method in ("create", "write", "unlink", "copy"):
                    self.assertTrue(any(call[:3] == (role, model, method) for call in wire.calls))
            self.assertTrue(any(call[:3] == (role, SOURCE, "exists") for call in wire.calls))
        for role in ("operator", "integration"):
            self.assertTrue(any(call[:3] == (role, JOB, "web_read") and set(call[4]["specification"]) == {"id"}
                                and call[4]["context"]["allowed_company_ids"] != [self.profile["company_id"]]
                                for call in wire.calls))
        self.assertNotIn("PRIVATE", output.getvalue())
        self.assertNotIn("raw_payload", output.getvalue())

    def test_full_collector_transport_failure_on_negative_probe_is_fatal(self):
        wire = NativeWire(self.acceptance, self.profile, self.flow)
        wire.break_on_denial = lambda: (_ for _ in ()).throw(TimeoutError("PRIVATE-PASSWORD-SESSION"))
        output = io.StringIO()
        with patch("urllib.request.build_opener", side_effect=wire.opener), contextlib.redirect_stdout(output):
            status = self.module.main(self.argv())
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(output.getvalue())["reason"], "RPC_TRANSPORT")
        self.assertFalse(self.paths["output"].exists())
        self.assertNotIn("PRIVATE", output.getvalue())

    def test_full_collector_detects_mutated_business_state_after_semantic_denial(self):
        wire = NativeWire(self.acceptance, self.profile, self.flow)
        job_id = self.flow["jobs"]["success"]["id"]
        wire.break_on_denial = lambda: wire.data[JOB][job_id].update(state="failed")
        output = io.StringIO()
        with patch("urllib.request.build_opener", side_effect=wire.opener), contextlib.redirect_stdout(output):
            status = self.module.main(self.argv())
        self.assertEqual(status, 1)
        self.assertFalse(self.paths["output"].exists())
        self.assertNotIn("PRIVATE", output.getvalue())

    def test_another_source_assigned_to_the_processor_blocks_queue_probe(self):
        wire = NativeWire(self.acceptance, self.profile, self.flow)
        source_id = self.profile["source_id"]
        extra = copy.deepcopy(wire.data[SOURCE][source_id])
        extra["id"] = source_id + 100
        extra["active"] = False
        wire.data[SOURCE][extra["id"]] = extra
        output = io.StringIO()
        with patch("urllib.request.build_opener", side_effect=wire.opener), contextlib.redirect_stdout(output):
            status = self.module.main(self.argv())
        self.assertEqual(status, 1)
        self.assertFalse(any(call[:3] == ("operator", JOB, "action_process_pending") for call in wire.calls))

    def test_every_role_batch_rechecks_audits_and_mappings_through_retained_observer_sessions(self):
        for faulty_role in ROLES:
            for target in ("audit", "mapping"):
                wire = NativeWire(self.acceptance, self.profile, self.flow)
                dispatch, changed = wire.dispatch, []

                def fault(role, endpoint, params):
                    if role == faulty_role and params.get("model") == JOB and params.get("method") == "create" and not changed:
                        changed.append(True)
                        if target == "audit":
                            wire.data[JOB][self.flow["jobs"]["success"]["id"]]["write_date"] = "2026-11-04 04:00:00"
                        else:
                            wire.data[CUSTOMER_MAP][self.profile["identities"]["customer_map"]["id"]]["external_ref"] = "PRIVATE-CHANGED-MAPPING"
                    return dispatch(role, endpoint, params)

                wire.dispatch = fault
                output = io.StringIO()
                with self.subTest(role=faulty_role, target=target), patch("urllib.request.build_opener", side_effect=wire.opener), \
                        contextlib.redirect_stdout(output):
                    status = self.module.main(self.argv())
                    self.assertEqual(changed, [True], "The fault must occur during the selected real role probe")
                    self.assertEqual(status, 1, "Every role must preserve facts it cannot read itself")
                    self.assertFalse(any(call[:3] == (faulty_role, JOB, "action_process_pending") for call in wire.calls),
                                     "Global preservation must be rechecked before the next probe batch")
                    self.assertNotIn("PRIVATE", output.getvalue())

    def test_idempotent_replay_creating_a_third_owned_quotation_fails_global_preservation(self):
        wire = NativeWire(self.acceptance, self.profile, self.flow)
        dispatch, changed = wire.dispatch, []

        def fault(role, endpoint, params):
            if role == "integration" and params.get("method") == "submit_order" and not changed:
                changed.append(True)
                extra = copy.deepcopy(wire.data["sale.order"][self.flow["quotes"]["success"]["id"]])
                extra.update(id=9901, name="PRIVATE-ROGUE-QUOTE")
                wire.data["sale.order"][9901] = extra
            return dispatch(role, endpoint, params)

        wire.dispatch = fault
        output = io.StringIO()
        with patch("urllib.request.build_opener", side_effect=wire.opener), contextlib.redirect_stdout(output):
            status = self.module.main(self.argv())
        self.assertEqual(changed, [True])
        self.assertEqual(status, 1, "Expected-ID reads cannot prove the complete owned quotation scope")
        self.assertNotIn("PRIVATE", output.getvalue())

    def test_idempotent_replay_creating_an_extra_owned_product_line_fails_global_preservation(self):
        wire = NativeWire(self.acceptance, self.profile, self.flow)
        dispatch, changed = wire.dispatch, []

        def fault(role, endpoint, params):
            if role == "integration" and params.get("method") == "submit_order" and not changed:
                changed.append(True)
                extra = copy.deepcopy(wire.data["sale.order.line"][1001])
                extra.update(id=9902, order_id=9903)
                wire.data["sale.order.line"][9902] = extra
                unrelated = copy.deepcopy(wire.data["sale.order"][self.flow["quotes"]["success"]["id"]])
                unrelated.update(id=9903, name="PRIVATE-EXTRA-LINE-PARENT", origin="OTHER-SOURCE",
                                 client_order_ref="OTHER-SOURCE", partner_id=self.acceptance["identities"]["customer"]["id"],
                                 order_line=[9902])
                wire.data["sale.order"][9903] = unrelated
            return dispatch(role, endpoint, params)

        wire.dispatch = fault
        output = io.StringIO()
        with patch("urllib.request.build_opener", side_effect=wire.opener), contextlib.redirect_stdout(output):
            status = self.module.main(self.argv())
        self.assertEqual(changed, [True])
        self.assertEqual(status, 1, "Own-product lines outside known order_line IDs must also be counted")
        self.assertNotIn("PRIVATE", output.getvalue())

    def test_sales_observer_detects_replay_quote_hidden_by_operator_creator_rule_before_next_probe(self):
        wire = NativeWire(self.acceptance, self.profile, self.flow)
        dispatch, changed = wire.dispatch, []

        def fault(role, endpoint, params):
            if role == "integration" and params.get("method") == "submit_order" and not changed:
                changed.append(True)
                extra = copy.deepcopy(wire.data["sale.order"][self.flow["quotes"]["success"]["id"]])
                extra.update(id=9901, name="PRIVATE-ROGUE-QUOTE", create_uid=self.acceptance["identities"]["user_sales"]["id"])
                wire.data["sale.order"][9901] = extra
            response = dispatch(role, endpoint, params)
            if role == "operator" and params.get("model") == "sale.order" and params.get("method") == "search":
                ids = json.loads(response.payload)["result"]
                return Response([record_id for record_id in ids if wire.data["sale.order"][record_id]["create_uid"]
                                 == self.profile["identities"]["user_operator"]["id"]])
            return response

        wire.dispatch = fault
        output = io.StringIO()
        with patch("urllib.request.build_opener", side_effect=wire.opener), contextlib.redirect_stdout(output):
            status = self.module.main(self.argv())
        self.assertEqual(changed, [True])
        self.assertEqual(status, 1)
        self.assertEqual(sum(call[:3] == ("integration", JOB, "submit_order") for call in wire.calls), 1,
                         "The union of ordinary Operator and Sales reads must be checked after each replay")
        self.assertNotIn("PRIVATE", output.getvalue())

    def test_idempotent_replay_rejects_float_ids_and_boolean_job_id_one(self):
        original = copy.deepcopy(self.flow)
        for malformed in ("float_job", "float_quote", "boolean_job_one"):
            self.flow = copy.deepcopy(original)
            if malformed == "boolean_job_one":
                self.flow["identities"]["job_success"]["id"] = 1
                self.flow["jobs"]["success"]["id"] = 1
                self.flow["events"]["attempt_success"]["job_id"] = 1
            self.paths["flow"].write_text(json.dumps(self.flow))
            wire = NativeWire(self.acceptance, self.profile, self.flow)
            dispatch, responses = wire.dispatch, []

            def fault(role, endpoint, params):
                response = dispatch(role, endpoint, params)
                if role == "integration" and params.get("method") == "submit_order":
                    result = json.loads(response.payload)["result"]
                    if malformed == "float_quote":
                        result["sale_order_id"] = float(result["sale_order_id"])
                    elif malformed == "float_job":
                        result["job_id"] = float(result["job_id"])
                    else:
                        result["job_id"] = True
                    responses.append(result)
                    return Response(result)
                return response

            wire.dispatch = fault
            output = io.StringIO()
            with self.subTest(malformed=malformed), patch("urllib.request.build_opener", side_effect=wire.opener), \
                    contextlib.redirect_stdout(output):
                status = self.module.main(self.argv())
                self.assertTrue(responses, "Malformed ID must reach a real idempotent replay response")
                self.assertEqual(status, 1, "Numeric equality must not conceal a malformed public replay result")
                self.assertNotIn("PRIVATE", output.getvalue())

    def test_native_export_usererror_requires_exact_message_and_actual_missing_export_group(self):
        message = "You don't have the rights to export data. Please contact an Administrator."
        for faulty_role in ("operator", "integration"):
            for server_message, allow_export, expected_status in ((message, False, 0),
                                                                  ("PRIVATE-UNRELATED-USERERROR", False, 1),
                                                                  (message, True, 1)):
                wire = NativeWire(self.acceptance, self.profile, self.flow)
                wire.export_groups[faulty_role] = allow_export
                dispatch, denials = wire.dispatch, []

                def fault(role, endpoint, params):
                    response = dispatch(role, endpoint, params)
                    if role == faulty_role and params.get("model") == SOURCE and params.get("method") == "export_data" \
                            and params["kwargs"]["context"]["allowed_company_ids"] == [self.evidence(role)["company_id"]]:
                        denials.append(True)
                        return Response(raw=json.dumps({"error": {"code": 200, "data": {
                            "name": "odoo.exceptions.UserError", "message": server_message, "debug": "PRIVATE-DEBUG"}}}).encode())
                    return response

                wire.dispatch = fault
                output = io.StringIO()
                with self.subTest(role=faulty_role, allow_export=allow_export, expected_status=expected_status), \
                        patch("urllib.request.build_opener", side_effect=wire.opener), contextlib.redirect_stdout(output):
                    status = self.module.main(self.argv())
                    self.assertEqual(denials, [True], "The normal-scope authorized export must reach the native denial")
                    self.assertEqual(status, expected_status, output.getvalue())
                    if expected_status == 0:
                        self.assertTrue(any(call[:3] == (faulty_role, "res.users", "has_group")
                                            and call[3] == [[self.evidence(faulty_role)["uid"]], "base.group_allow_export"]
                                            for call in wire.calls), "Global export denial needs a real group read in this session")
                    self.assertNotIn("PRIVATE", output.getvalue())

    def test_native_sale_form_requires_the_pinned_readonly_tax_totals_widget(self):
        native_field = '<field name="tax_totals" widget="account-tax-totals-field" readonly="1"/>'
        for replacement, expected_status in ((native_field, 0), ("", 1),
                                               ('<field name="tax_totals" widget="wrong-widget" readonly="1"/>', 1),
                                               ('<field name="tax_totals" widget="account-tax-totals-field" readonly="0"/>', 1)):
            wire = NativeWire(self.acceptance, self.profile, self.flow)
            dispatch = wire.dispatch

            def native_view(role, endpoint, params):
                response = dispatch(role, endpoint, params)
                if params.get("model") == "sale.order" and params.get("method") == "get_view" \
                        and params["kwargs"]["view_type"] == "form":
                    view = json.loads(response.payload)["result"]
                    self.assertNotIn('name="amount_total"', view["arch"])
                    self.assertIn(native_field, view["arch"])
                    view["arch"] = view["arch"].replace(native_field, replacement)
                    return Response(view)
                return response

            wire.dispatch = native_view
            output = io.StringIO()
            with self.subTest(replacement=replacement, expected_status=expected_status), \
                    patch("urllib.request.build_opener", side_effect=wire.opener), contextlib.redirect_stdout(output):
                status = self.module.main(self.argv())
                self.assertEqual(status, expected_status, output.getvalue())
                if expected_status == 0:
                    self.assertEqual(json.loads(output.getvalue())["roles"]["sales"]["quotes"], self.flow["quotes"])
                self.assertNotIn("PRIVATE", output.getvalue())

    def test_sales_roles_actually_probe_source_read_web_read_and_search_in_their_normal_scope(self):
        wire = NativeWire(self.acceptance, self.profile, self.flow)
        output = io.StringIO()
        with patch("urllib.request.build_opener", side_effect=wire.opener), contextlib.redirect_stdout(output):
            status = self.module.main(self.argv())
        self.assertEqual(status, 0, output.getvalue())
        for role in ("sales", "other_sales"):
            company = self.evidence(role)["company_id"]
            methods = {call[2] for call in wire.calls if call[:2] == (role, SOURCE)
                       and call[4]["context"]["allowed_company_ids"] == [company]}
            with self.subTest(role=role):
                self.assertTrue({"read", "web_read", "search"}.issubset(methods))

    def test_source_acl_regression_in_normal_scope_cannot_pass_bad_company_only_denials(self):
        for faulty_role in ("sales", "other_sales"):
            wire = NativeWire(self.acceptance, self.profile, self.flow)
            dispatch = wire.dispatch

            def fault(role, endpoint, params):
                if role == faulty_role and params.get("model") == SOURCE and params.get("method") in {"read", "web_read", "search"} \
                        and params["kwargs"]["context"]["allowed_company_ids"] == [self.evidence(role)["company_id"]]:
                    return Response([self.profile["source_id"]] if params["method"] == "search" else [{"id": self.profile["source_id"]}])
                return dispatch(role, endpoint, params)

            wire.dispatch = fault
            output = io.StringIO()
            with self.subTest(role=faulty_role), patch("urllib.request.build_opener", side_effect=wire.opener), contextlib.redirect_stdout(output):
                status = self.module.main(self.argv())
                self.assertEqual(status, 1, "Bad-company AccessError must not stand in for a normal-scope ACL check")
                self.assertNotIn("PRIVATE", output.getvalue())

    def test_authentication_rejects_boolean_string_null_company_ids_before_group_calls(self):
        self.acceptance["identities"]["company_a"]["id"] = 1
        for report in (self.profile, self.flow):
            report["company_id"] = 1
        self.profile["borrowed_m1_identities"]["company_a"]["id"] = 1
        self.flow["m1_preservation"]["borrowed_m1_identities"]["company_a"]["id"] = 1
        for key in ("acceptance", "profile", "flow"):
            self.paths[key].write_text(json.dumps(getattr(self, key)))
        inputs, uid = self.load(), self.profile["identities"]["user_integration"]["id"]
        valid = {"uid": uid, "user_context": {"allowed_company_ids": [1]},
                 "user_companies": {"current_company": 1, "allowed_companies": {"1": {"id": 1}}}}
        for invalid in (True, "1", None):
            for location in ("current", "allowed", "active"):
                result = copy.deepcopy(valid)
                if location == "current":
                    result["user_companies"]["current_company"] = invalid
                elif location == "allowed":
                    result["user_companies"]["allowed_companies"]["1"]["id"] = invalid
                else:
                    result["user_context"]["allowed_company_ids"] = [invalid]
                client = Mock()
                client.rpc.return_value = result
                client.call.side_effect = ValueError("Synthetic group boundary")
                with self.subTest(location=location, value=invalid):
                    with self.assertRaises(ValueError):
                        self.module.collect_role_evidence(client, "integration", inputs)
                    client.call.assert_not_called()

    def test_cli_all_inputs_are_validated_before_any_network_and_errors_are_safe(self):
        self.paths["credentials"].chmod(0o644)
        for extra in ([], ["--password", "PRIVATE-ARGUMENT"]):
            output, errors = io.StringIO(), io.StringIO()
            with patch("urllib.request.build_opener", side_effect=AssertionError("No network")), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                status = self.module.main(self.argv() + extra)
            self.assertNotEqual(status, 0)
            self.assertIs(json.loads(output.getvalue())["ok"], False)
            self.assertEqual(errors.getvalue(), "")
            self.assertNotIn("PRIVATE", output.getvalue())
            self.assertFalse(self.paths["output"].exists())

    def test_cli_uses_five_independent_sessions_and_atomic_safe_output(self):
        output = io.StringIO()
        sessions = []

        def collect(client, role, inputs):
            sessions.append((client, role))
            self.assertEqual(inputs["database"], "odoo_portfolio")
            return self.evidence(role)

        with patch("urllib.request.build_opener", side_effect=lambda *_handlers: Mock()), \
                patch.object(self.module, "collect_role_evidence", side_effect=collect), contextlib.redirect_stdout(output):
            status = self.module.main(self.argv())
        self.assertEqual(status, 0, output.getvalue())
        self.assertEqual([role for _client, role in sessions], list(ROLES))
        self.assertEqual(len({id(client) for client, _role in sessions}), 5)
        self.assertEqual(json.loads(output.getvalue()), json.loads(self.paths["output"].read_text()))
        self.assertNotIn("PRIVATE", output.getvalue())

    def test_cli_failure_only_reports_fixed_codes_and_never_exception_text(self):
        for failure in (TimeoutError("PRIVATE-PASSWORD-SESSION-DEBUG"),
                        self.module.VerificationError("integration", "submit_order", "PRIVATE-REMOTE-BODY")):
            output, errors = io.StringIO(), io.StringIO()
            with patch("urllib.request.build_opener", return_value=Mock()), \
                    patch.object(self.module, "collect_role_evidence", side_effect=failure), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                status = self.module.main(self.argv())
            self.assertEqual(status, 1)
            report = json.loads(output.getvalue())
            self.assertEqual(set(report), {"schema_version", "ok", "role", "operation", "reason"})
            self.assertRegex(report["reason"], "^[A-Z_]+$")
            self.assertNotIn("PRIVATE", output.getvalue() + errors.getvalue())
            self.assertFalse(self.paths["output"].exists())
