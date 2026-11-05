"""Authenticate five local bridge actors and verify the completed native flow.

Only safe summaries leave this process. Audit payloads and server error bodies
are inspected in memory and are never included in a report or exception text.
"""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import math
import os
import re
import stat
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from decimal import Decimal
from pathlib import Path

import acceptance_credentials
import order_bridge_credentials

ROOT = Path(__file__).resolve().parents[1]
LIMIT = 8 * 1024 * 1024
ROLES = ("settings", "operator", "integration", "sales", "other_sales")
SOURCE, JOB, ATTEMPT = "portfolio.import.source", "portfolio.import.job", "portfolio.import.attempt"
CUSTOMER_MAP, PRODUCT_MAP = "portfolio.import.customer.map", "portfolio.import.product.map"
SYSTEM, ERP, INTERNAL = "base.group_system", "base.group_erp_manager", "base.group_user"
OPERATOR, INTEGRATION = "portfolio_order_bridge.group_import_operator", "portfolio_order_bridge.group_import_integration"
SALES = "sales_team.group_sale_salesman"
GROUPS = (INTERNAL, SYSTEM, ERP, OPERATOR, INTEGRATION, SALES,
          "sales_team.group_sale_salesman_all_leads", "sales_team.group_sale_manager",
          "purchase.group_purchase_user", "stock.group_stock_user", "account.group_account_invoice")
M1_MODELS = {
    "company_a": "res.company", "company_b": "res.company", "warehouse_a": "stock.warehouse",
    "customer": "res.partner", "vendor": "res.partner", "initial_bearing_move": "stock.move",
    **{f"user_{role}": "res.users" for role in acceptance_credentials.ROLES},
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
ACCESS = "odoo.exceptions.AccessError"
USER = "odoo.exceptions.UserError"
VALIDATION = "odoo.exceptions.ValidationError"
SEMANTIC_ERRORS = frozenset((ACCESS, USER, VALIDATION))
APPROVAL_MESSAGE = "This quotation requires current approval before confirmation. Submit it for approval."
RETRY_REASON = "Acceptance SKU mapping repaired"
RETRY_MESSAGE = "Only a failed import job can be retried manually."
NATIVE_EXPORT_MESSAGE = "You don't have the rights to export data. Please contact an Administrator."
EVENTS = (("attempt_success", "success", "done", 1), ("attempt_failed", "repaired", "failed", 1),
          ("attempt_retry", "repaired", "retry", 1), ("attempt_repaired", "repaired", "done", 2))
REASONS = frozenset(("INVALID_INPUT", "INVALID_EVIDENCE", "AUTHENTICATION_FAILED", "RPC_TRANSPORT",
                     "RPC_PROTOCOL", "RPC_SESSION", "RPC_SERVER", "RPC_SEMANTIC", "EXPECTED_DENIAL",
                     "HTTP_REDIRECT", "BUSINESS_CHANGED", "UNEXPECTED_FAILURE", "OUTPUT_FAILED"))
OPERATIONS = frozenset(("validation", "authentication", "navigation", "role_verification", "output",
                        "has_group", "load_menus", "get_view", "read", "web_read", "search", "search_read",
                        "search_count", "read_group", "export_data", "exists", "create", "write", "unlink", "copy",
                        "submit_order", "action_process_pending", "action_retry", "action_open_retry", "action_confirm"))


class VerificationError(ValueError):
    """Controlled codes only; caller text and remote error bodies are discarded."""

    def __init__(self, role="input", operation="validation", reason="INVALID_EVIDENCE"):
        self.role = role if role in (*ROLES, "input") else "input"
        self.operation = operation if operation in OPERATIONS else "validation"
        self.reason = reason if reason in REASONS else "INVALID_EVIDENCE"
        super().__init__(self.reason)


class RpcSemanticError(VerificationError):
    """Only an actual allowlisted server exception can satisfy a denial probe."""

    def __init__(self, role, operation, name, message=None):
        super().__init__(role, operation, "RPC_SEMANTIC")
        self.name = name
        # Preserve only recognition of fixed public business messages.
        # Arbitrary server messages/debug/description are never retained.
        self.known_message = message if message in (APPROVAL_MESSAGE, RETRY_MESSAGE, NATIVE_EXPORT_MESSAGE) else None


def require(condition, reason="INVALID_EVIDENCE", *, role="input", operation="validation"):
    if not condition:
        raise VerificationError(role, operation, reason)


def identifier(value):
    return value if type(value) is int and value > 0 else None


def relation(value):
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return identifier(value[0])
    if isinstance(value, dict):
        return identifier(value.get("id"))
    return identifier(value)


def exact_keys(value, keys):
    require(isinstance(value, dict) and set(value) == set(keys))


def same_json(value, expected):
    """JSON booleans and IDs must not pass through Python numeric equality."""
    if type(value) is not type(expected):
        return False
    if isinstance(expected, dict):
        return set(value) == set(expected) and all(same_json(value[key], item) for key, item in expected.items())
    if isinstance(expected, list):
        return len(value) == len(expected) and all(same_json(left, right) for left, right in zip(value, expected))
    return value == expected


def clean_text(value, maximum=200):
    return (isinstance(value, str) and 0 < len(value) <= maximum and value == value.strip()
            and not any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in value))


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "INVALID_INPUT")
        result[key] = value
    return result


def parse_json(raw, *, limit=LIMIT):
    try:
        require(isinstance(raw, (bytes, str)) and len(raw if isinstance(raw, bytes) else raw.encode("utf-8")) <= limit)
        return json.loads(raw, object_pairs_hook=_unique_object,
                          parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise VerificationError(reason="INVALID_INPUT") from None


def validate_target(base_url, database):
    try:
        url = urllib.parse.urlsplit(base_url)
        valid = (isinstance(base_url, str) and re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}", base_url)
                 and url.scheme == "http" and url.hostname == "127.0.0.1"
                 and url.port is not None and 0 < url.port < 65536)
    except (TypeError, ValueError, AttributeError):
        valid = False
    require(valid and isinstance(database, str) and database in order_bridge_credentials.DATABASES, "INVALID_INPUT")


def runtime_path(path, *, existing=True):
    try:
        path = Path(path)
        if not path.is_absolute():
            path = ROOT / path
        path = order_bridge_credentials._safe_path(path)
        roots = {ROOT / ".runtime"}
        environment = ROOT / ".venv"
        main = ROOT.parent.parent if ROOT.parent.name == ".worktrees" else ROOT
        if environment.exists() and environment.resolve().parent == main:
            roots.add(main / ".runtime")
        require(any(path.is_relative_to(root) for root in roots), "INVALID_INPUT")
        info = path.lstat() if path.exists() else None
        if existing:
            require(info is not None and stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid(), "INVALID_INPUT")
        else:
            require(info is None or (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()), "INVALID_INPUT")
        parent = path.parent.lstat()
        require(stat.S_ISDIR(parent.st_mode) and parent.st_uid == os.getuid(), "INVALID_INPUT")
        return path
    except (OSError, ValueError, TypeError):
        raise VerificationError(reason="INVALID_INPUT") from None


def read_json(path, *, limit=LIMIT):
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(descriptor)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_size <= limit, "INVALID_INPUT")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            return parse_json(handle.read(limit + 1), limit=limit)
    except (OSError, ValueError, UnicodeError):
        raise VerificationError(reason="INVALID_INPUT") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _identities(value, models):
    exact_keys(value, models)
    for key, model in models.items():
        exact_keys(value[key], ("model", "id"))
        require(value[key]["model"] == model and identifier(value[key]["id"]))
    require(len({(item["model"], item["id"]) for item in value.values()}) == len(models))


def validate_reports(acceptance, profile, flow, database):
    exact_keys(acceptance, ("namespace", "identities", "record_counts", "bearing_quantity",
                            "bearing_move_count", "initial_bearing_move_id"))
    require(acceptance["namespace"] == "portfolio_trade_acceptance")
    _identities(acceptance["identities"], M1_MODELS)
    old = acceptance["identities"]
    require(acceptance["record_counts"] == dict(Counter(M1_MODELS.values()))
            and all(type(value) is int for value in acceptance["record_counts"].values())
            and number(acceptance["bearing_quantity"]) and acceptance["bearing_quantity"] == 40
            and type(acceptance["bearing_move_count"]) is int and acceptance["bearing_move_count"] == 1
            and type(acceptance["initial_bearing_move_id"]) is int
            and acceptance["initial_bearing_move_id"] == old["initial_bearing_move"]["id"])
    exact_keys(profile, ("schema_version", "database", "namespace", "company_id", "source_id", "identities",
                         "cron_active", "borrowed_m1_identities", "borrowed_finance_pricelist_id"))
    exact_keys(flow, ("schema_version", "database", "namespace", "company_id", "source_id", "identities",
                      "jobs", "quotes", "events", "m1_preservation"))
    _identities(profile["identities"], PROFILE_MODELS)
    _identities(flow["identities"], FLOW_MODELS)
    static, dynamic = profile["identities"], flow["identities"]
    company, source = old["company_a"]["id"], static["source"]["id"]
    for report, namespace in ((profile, "portfolio_order_bridge_acceptance"), (flow, "portfolio_order_bridge_flow")):
        require(type(report["schema_version"]) is int and report["schema_version"] == 1
                and report["database"] == database and report["namespace"] == namespace
                and type(report["company_id"]) is int and report["company_id"] == company
                and type(report["source_id"]) is int and report["source_id"] == source)
    require(profile["cron_active"] is False and same_json(profile["borrowed_m1_identities"], old)
            and identifier(profile["borrowed_finance_pricelist_id"]))
    all_ids = [*old.values(), *static.values(), *dynamic.values()]
    require(len({(item["model"], item["id"]) for item in all_ids}) == len(all_ids))
    operator, integration = (static[f"user_{role}"]["id"] for role in ("operator", "integration"))
    exact_keys(flow["jobs"], ("success", "repaired"))
    exact_keys(flow["quotes"], ("success", "repaired"))
    for suffix, amount, count in (("success", 309, 1), ("repaired", 161.5, 2)):
        job, quote = flow["jobs"][suffix], flow["quotes"][suffix]
        exact_keys(job, ("id", "state", "attempt_count", "submitted_by_id", "processed_by_id"))
        require(same_json(job, {"id": dynamic[f"job_{suffix}"]["id"], "state": "done", "attempt_count": count,
                               "submitted_by_id": integration, "processed_by_id": operator})
                and type(job["attempt_count"]) is int)
        exact_keys(quote, ("id", "name", "state", "currency", "amount_untaxed", "amount_total", "owner_id",
                           "created_by_id", "approval_required"))
        require(type(quote["id"]) is int and quote["id"] == dynamic[f"quote_{suffix}"]["id"] and clean_text(quote["name"])
                and quote["state"] == "draft" and quote["currency"] == "CNY"
                and number(quote["amount_untaxed"]) and quote["amount_untaxed"] == amount
                and number(quote["amount_total"]) and quote["amount_total"] == amount
                and type(quote["owner_id"]) is int and quote["owner_id"] == old["user_sales"]["id"]
                and type(quote["created_by_id"]) is int and quote["created_by_id"] == operator
                and quote["approval_required"] is True)
    exact_keys(flow["events"], (item[0] for item in EVENTS))
    for key, suffix, kind, ordinal in EVENTS:
        event = flow["events"][key]
        exact_keys(event, ("id", "job_id", "event_type", "attempt_no", "actor_id", "retry_reason"))
        require(same_json(event, {"id": dynamic[key]["id"], "job_id": dynamic[f"job_{suffix}"]["id"], "event_type": kind,
                                 "attempt_no": ordinal, "actor_id": operator,
                                 "retry_reason": RETRY_REASON if kind == "retry" else None})
                and type(event["attempt_no"]) is int)
    preserved = flow["m1_preservation"]
    exact_keys(preserved, ("fingerprint", "borrowed_m1_identities", "bearing_stock", "bearing_move_ids"))
    require(isinstance(preserved["fingerprint"], str) and re.fullmatch("[0-9a-f]{64}", preserved["fingerprint"])
            and same_json(preserved["borrowed_m1_identities"], old)
            and number(preserved["bearing_stock"]) and preserved["bearing_stock"] == 10
            and isinstance(preserved["bearing_move_ids"], list) and preserved["bearing_move_ids"]
            and all(identifier(value) for value in preserved["bearing_move_ids"])
            and len(set(preserved["bearing_move_ids"])) == len(preserved["bearing_move_ids"]))


def load_inputs(*, base_url, database, credentials_path, sales_credentials_path,
                acceptance_report_path, profile_report_path, flow_report_path, output_path):
    validate_target(base_url, database)
    paths = [runtime_path(path) for path in (credentials_path, sales_credentials_path, acceptance_report_path,
                                            profile_report_path, flow_report_path)]
    output = runtime_path(output_path, existing=False)
    require(len({(path.stat().st_dev, path.stat().st_ino) for path in paths}) == len(paths), "INVALID_INPUT")
    require(output not in paths and not any(output.exists() and os.path.samefile(output, path) for path in paths), "INVALID_INPUT")
    for path in paths[:2]:
        require(stat.S_IMODE(path.stat().st_mode) == 0o600, "INVALID_INPUT")
        data = read_json(path, limit=65536)
        require(isinstance(data, dict) and type(data.get("schema_version")) is int, "INVALID_INPUT")
    try:
        credentials = order_bridge_credentials._read(paths[0], database)
        sales_credentials = acceptance_credentials._read_credentials(paths[1], database)
    except (ValueError, OSError):
        raise VerificationError(reason="INVALID_INPUT") from None
    acceptance, profile, flow = (read_json(path) for path in paths[2:])
    validate_reports(acceptance, profile, flow, database)
    return {"base_url": base_url, "database": database, "credentials": credentials,
            "sales_credentials": sales_credentials, "acceptance": acceptance, "profile": profile,
            "flow": flow, "output": output, "input_paths": paths}


def expected_groups(role):
    active = {INTERNAL, *{"settings": (SYSTEM, ERP), "operator": (OPERATOR, SALES),
                           "integration": (INTEGRATION,), "sales": (SALES,), "other_sales": (SALES,)}[role]}
    return {group: group in active for group in GROUPS}


def _xml(view, kind):
    exact_keys(view, ("arch",))
    try:
        require(isinstance(view["arch"], str) and len(view["arch"].encode()) <= LIMIT)
        arch = ET.fromstring(view["arch"])
        require(arch.tag == kind and all("attrs" not in node.attrib and "states" not in node.attrib for node in arch.iter()))
        return arch
    except (ValueError, TypeError, ET.ParseError, RecursionError):
        raise VerificationError() from None


def validate_role_evidence(role, evidence, inputs):
    require(role in ROLES)
    exact_keys(evidence, ("uid", "company_id", "allowed_company_ids", "active_company_ids", "groups", "menus",
                          "actions", "views", "menu_roots", "source_id", "mapping_ids", "cron", "jobs", "quotes", "events", "checks"))
    old, static, flow = inputs["acceptance"]["identities"], inputs["profile"]["identities"], inputs["flow"]
    uid = (static if role in ROLES[:3] else old)[f"user_{role}"]["id"]
    company = old["company_b" if role == "other_sales" else "company_a"]["id"]
    require(type(evidence["uid"]) is int and evidence["uid"] == uid
            and type(evidence["company_id"]) is int and evidence["company_id"] == company
            and same_json(evidence["allowed_company_ids"], [company]) and same_json(evidence["active_company_ids"], [company]))
    exact_keys(evidence["groups"], GROUPS)
    require(all(type(value) is bool for value in evidence["groups"].values()) and evidence["groups"] == expected_groups(role))
    keys = ROLE_MENUS[role]
    roots = evidence["menu_roots"]
    exact_keys(roots, ("trade", "import", "config") if role == "settings" else
               ("trade", "import") if role in ROLES[:3] else ("trade",))
    for name, root in roots.items():
        exact_keys(root, ("id", "parent_id"))
        require(identifier(root["id"]))
        require(root["parent_id"] is False if name == "trade" else
                root["parent_id"] == roots["trade" if name == "import" else "import"]["id"])
    exact_keys(evidence["menus"], keys)
    exact_keys(evidence["actions"], keys)
    exact_keys(evidence["views"], (MENU_MODELS[key] for key in keys))
    menus, actions, views = {}, {}, {}
    for key in keys:
        menu, action, model = evidence["menus"][key], evidence["actions"][key], MENU_MODELS[key]
        exact_keys(menu, ("id", "xmlid", "parent_id", "action"))
        exact_keys(action, ("id", "res_model", "view_mode"))
        module, prefix = ("portfolio_trade", "portfolio_") if key == "sales" else ("portfolio_order_bridge", "import_")
        require(identifier(menu["id"]) and identifier(menu["parent_id"]) and identifier(action["id"])
                and menu["parent_id"] == roots["trade" if key == "sales" else "config" if role == "settings" else "import"]["id"]
                and menu["xmlid"] == f"{module}.menu_{prefix}{key}"
                and menu["action"] == f"ir.actions.act_window,{action['id']}"
                and action["res_model"] == model and isinstance(action["view_mode"], str)
                and {"list", "form"}.issubset(action["view_mode"].split(",")))
        exact_keys(evidence["views"][model], ("list", "form"))
        for kind in ("list", "form"):
            arch = _xml(evidence["views"][model][kind], kind)
            require(VIEW_FIELDS[model][kind].issubset({node.get("name") for node in arch.iter("field")}))
            if model == "sale.order" and kind == "form":
                totals = [node for node in arch.iter("field") if node.get("name") == "tax_totals"]
                require(len(totals) == 1 and totals[0].get("widget") == "account-tax-totals-field"
                        and totals[0].get("readonly") in {"1", "true", "True"})
            if model in {JOB, ATTEMPT}:
                require(all(arch.get(name) in {"0", "false", "False"} for name in ("create", "edit", "delete")))
            if model == JOB:
                buttons = {node.get("name"): node for node in arch.iter("button")}
                if role == "integration":
                    require(not {"action_process_pending", "action_open_retry", "action_open_sale_order"}.intersection(buttons))
                elif kind == "form":
                    require("action_open_retry" in buttons and buttons["action_open_retry"].get("invisible") == "state != 'failed'")
        menus[key] = {"id": menu["id"], "action_id": action["id"], "model": model}
        actions[key] = dict(action)
        views[model] = {"list": True, "form": True}
    require(evidence["source_id"] == (inputs["profile"]["source_id"] if role in ROLES[:3] else None))
    mappings = {"customer": [static["customer_map"]["id"]],
                "product": sorted([static["product_map_item1"]["id"], static["product_map_item2"]["id"],
                                   flow["identities"]["product_map_repair"]["id"]])}
    require(same_json(evidence["mapping_ids"], mappings if role in {"settings", "operator"} else {}))
    require(same_json(evidence["cron"], {"id": static["processing_cron"]["id"], "active": False,
                                  "processor_uid": static["user_operator"]["id"], "fixed_code": True, "source_link": True}
                                 if role == "settings" else None))
    for section, allowed in (("jobs", {"operator", "integration"}), ("events", {"operator", "integration"}),
                             ("quotes", {"operator", "sales"})):
        require(same_json(evidence[section], flow[section] if role in allowed else {}))
    checks = COMMON_CHECKS | ROLE_CHECKS[role]
    exact_keys(evidence["checks"], checks)
    require(all(value is True for value in evidence["checks"].values()))
    return {"uid": uid, "company_id": company, "allowed_company_ids": [company], "active_company_ids": [company],
            "groups": expected_groups(role), "menus": menus, "actions": actions, "views": views,
            "menu_roots": {key: dict(value) for key, value in roots.items()},
            "source_id": evidence["source_id"], "mapping_ids": mappings if role in {"settings", "operator"} else {},
            "cron": dict(evidence["cron"]) if role == "settings" else None,
            "jobs": {key: dict(value) for key, value in evidence["jobs"].items()},
            "quotes": {key: dict(value) for key, value in evidence["quotes"].items()},
            "events": {key: dict(value) for key, value in evidence["events"].items()},
            "checks": {key: True for key in sorted(checks)}}


def validate_evidence(evidence_by_role, inputs):
    exact_keys(evidence_by_role, ROLES)
    validate_reports(inputs["acceptance"], inputs["profile"], inputs["flow"], inputs["database"])
    return {"schema_version": 1, "ok": True, "database": inputs["database"],
            "profile_source_id": inputs["profile"]["source_id"],
            "roles": {role: validate_role_evidence(role, evidence_by_role[role], inputs) for role in ROLES},
            "business_facts": {"jobs": 2, "draft_quotes": 2, "attempt_events": 4,
                               "quote_totals_cny": [309, 161.5], "preserved_m1_identities": 19,
                               "bearing_stock": inputs["flow"]["m1_preservation"]["bearing_stock"]}}


class RejectRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise VerificationError(reason="HTTP_REDIRECT")


class JsonRpcClient:
    def __init__(self, base_url, role, opener=None):
        validate_target(base_url, "odoo_portfolio")
        require(role in ROLES, "INVALID_INPUT")
        self.base_url, self.role = base_url, role
        self.opener = opener if opener is not None else urllib.request.build_opener(
            urllib.request.ProxyHandler({}), RejectRedirect(),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def rpc(self, path, params):
        operation = params.get("method", "authentication" if path == "/web/session/authenticate" else "navigation") if isinstance(params, dict) else "validation"
        require(isinstance(path, str) and (path in ("/web/session/authenticate", "/web/action/load", "/web/dataset/call_kw")
                or re.fullmatch(r"/web/dataset/call_kw/[a-z][a-z0-9_.]*/[a-zA-Z_][a-zA-Z0-9_]*", path)),
                "INVALID_INPUT", role=self.role, operation=operation)
        try:
            request = urllib.request.Request(self.base_url + path, data=json.dumps({
                "jsonrpc": "2.0", "method": "call", "params": params, "id": 1}, allow_nan=False).encode(),
                headers={"Content-Type": "application/json"})
            with self.opener.open(request, timeout=30) as response:
                require(response.status == 200, "RPC_PROTOCOL", role=self.role, operation=operation)
                raw = response.read(LIMIT + 1)
            data = parse_json(raw)
            require(isinstance(data, dict) and set(data).issubset({"jsonrpc", "id", "result", "error"})
                    and ("jsonrpc" not in data or data["jsonrpc"] == "2.0")
                    and ("id" not in data or type(data["id"]) is int and data["id"] == 1)
                    and (("error" in data) != ("result" in data)), "RPC_PROTOCOL", role=self.role, operation=operation)
            if "error" in data:
                error = data["error"]
                require(isinstance(error, dict) and isinstance(error.get("data"), dict), "RPC_SERVER", role=self.role, operation=operation)
                name = error["data"].get("name")
                if name in ("odoo.http.SessionExpiredException", "odoo.exceptions.SessionExpiredException") or error.get("code") == 100:
                    raise VerificationError(self.role, operation, "RPC_SESSION")
                require(type(error.get("code")) is int and error["code"] == 200 and name in SEMANTIC_ERRORS,
                        "RPC_SERVER", role=self.role, operation=operation)
                raise RpcSemanticError(self.role, operation, name, error["data"].get("message"))
            return data["result"]
        except RpcSemanticError:
            raise
        except VerificationError as error:
            raise VerificationError(self.role, operation, error.reason if error.reason != "INVALID_INPUT" else "RPC_PROTOCOL") from None
        except (OSError, urllib.error.URLError, ValueError, TypeError, UnicodeError, RecursionError):
            raise VerificationError(self.role, operation, "RPC_TRANSPORT") from None

    def call(self, model, method, args=None, kwargs=None):
        return self.rpc(f"/web/dataset/call_kw/{model}/{method}", {
            "model": model, "method": method, "args": args if args is not None else [],
            "kwargs": kwargs if kwargs is not None else {}})


def expected_denial(operation, names, *, known_message=None):
    require(isinstance(names, (set, frozenset)) and names and names.issubset(SEMANTIC_ERRORS))
    try:
        operation()
    except RpcSemanticError as error:
        require(error.name in names and (known_message is None or error.known_message == known_message),
                "EXPECTED_DENIAL", role=error.role, operation=error.operation)
        return True
    raise VerificationError(reason="EXPECTED_DENIAL")


JOB_FIELDS = ("source_id", "company_id", "external_order_id", "payload_hash", "raw_payload", "canonical_payload",
              "submitted_by_id", "received_at", "state", "attempt_count", "last_attempt_at", "error_code",
              "error_detail", "sale_order_id", "create_uid", "create_date", "write_date")
EVENT_FIELDS = ("job_id", "company_id", "attempt_no", "event_type", "actor_id", "started_at", "finished_at",
                "error_code", "error_path", "error_detail", "retry_reason", "mapping_snapshot",
                "source_defaults_snapshot", "create_uid", "create_date", "write_date")
QUOTE_FIELDS = ("name", "company_id", "state", "currency_id", "amount_untaxed", "amount_total", "amount_tax",
                "user_id", "create_uid", "portfolio_creator_id", "partner_id", "partner_invoice_id",
                "partner_shipping_id", "warehouse_id", "pricelist_id", "origin", "client_order_ref", "order_line",
                "portfolio_approval_enabled", "portfolio_approval_current", "portfolio_approval_state",
                "portfolio_approval_history_ids", "portfolio_approval_submitter_id", "portfolio_approval_fingerprint",
                "portfolio_approval_policy_version", "portfolio_revision", "picking_ids", "invoice_ids", "write_date")
LINE_FIELDS = ("order_id", "product_id", "product_uom", "product_uom_qty", "price_unit", "discount", "tax_id",
               "display_type", "is_downpayment", "qty_delivered", "qty_invoiced", "invoice_lines", "move_ids",
               "create_uid", "create_date", "write_date")
SOURCE_FIELDS = ("name", "active", "company_id", "company_code", "code")
SOURCE_CONFIG = ("pricelist_id", "warehouse_id", "sales_user_id", "processor_user_id", "integration_user_ids")


def _payload(suffix):
    return {"company_code": "PARTS-A", "source_code": "B2B-ACCEPTANCE", "customer_ref": "BRIDGE-ACCEPTANCE-CUSTOMER",
            "currency": "CNY", "sent_at": "2026-11-04T00:00:00Z",
            "external_order_id": "BRIDGE-ACCEPTANCE-SUCCESS" if suffix == "success" else "BRIDGE-ACCEPTANCE-RETRY",
            "request_id": "local-import-success" if suffix == "success" else "local-import-repair",
            "lines": ([{"external_line_id": "2", "sku": "BRIDGE-ACCEPTANCE-2", "quantity": "3", "unit_price": "35", "discount": "0"},
                       {"external_line_id": "1", "sku": "BRIDGE-ACCEPTANCE-1", "quantity": "2", "unit_price": "120", "discount": "15"}]
                      if suffix == "success" else
                      [{"external_line_id": "2", "sku": "BRIDGE-ACCEPTANCE-1", "quantity": "1", "unit_price": "120", "discount": "15"},
                       {"external_line_id": "1", "sku": "BRIDGE-ACCEPTANCE-REPAIR", "quantity": "2", "unit_price": "35", "discount": "15"}])}


def _canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


class _RoleCollector:
    """One real session, fixed read sets and tightly bounded negative probes."""

    def __init__(self, client, role, inputs):
        self.client, self.role, self.inputs = client, role, inputs
        self.old, self.static = inputs["acceptance"]["identities"], inputs["profile"]["identities"]
        self.flow = inputs["flow"]
        self.uid = (self.static if role in ROLES[:3] else self.old)[f"user_{role}"]["id"]
        self.company = self.old["company_b" if role == "other_sales" else "company_a"]["id"]
        self.source = inputs["profile"]["source_id"]
        self.operator, self.integration = (self.static[f"user_{name}"]["id"] for name in ("operator", "integration"))
        self.context = {"allowed_company_ids": [self.company], "lang": "en_US"}
        self.checks = {}

    def call(self, model, method, args=None, *, extra_context=None, **kwargs):
        return self.client.call(model, method, args, {"context": self.context | (extra_context or {}), **kwargs})

    def rows(self, model, ids, fields, *, web=False):
        require(isinstance(ids, list) and ids and all(identifier(value) for value in ids))
        result = (self.call(model, "web_read", [ids], specification={name: {} for name in fields}) if web
                  else self.call(model, "read", [ids], fields=list(fields)))
        require(isinstance(result, list) and len(result) == len(ids)
                and all(isinstance(row, dict) and identifier(row.get("id")) for row in result)
                and sorted(row["id"] for row in result) == sorted(ids))
        # Explicit field selection is not enough: reject incomplete responses.
        for row in result:
            require(set(fields).issubset(row))
        return sorted(result, key=lambda row: row["id"])

    def search(self, model, domain, *, expected=None, **kwargs):
        result = self.call(model, "search", [domain], **kwargs)
        require(isinstance(result, list) and all(identifier(value) for value in result) and len(set(result)) == len(result))
        if expected is not None:
            require(sorted(result) == sorted(expected))
        return sorted(result)

    def authenticate(self):
        accounts = self.inputs["credentials" if self.role in ROLES[:3] else "sales_credentials"]["accounts"]
        account = accounts[self.role]
        session = self.client.rpc("/web/session/authenticate", {
            "db": self.inputs["database"], "login": account["login"], "password": account["password"]})
        require(isinstance(session, dict) and type(session.get("uid")) is int and session["uid"] == self.uid,
                "AUTHENTICATION_FAILED", role=self.role, operation="authentication")
        companies, context = session.get("user_companies"), session.get("user_context")
        require(isinstance(companies, dict) and isinstance(context, dict), "AUTHENTICATION_FAILED")
        allowed = companies.get("allowed_companies")
        require(isinstance(allowed, dict) and set(allowed) == {str(self.company)}
                and isinstance(allowed[str(self.company)], dict)
                and identifier(allowed[str(self.company)].get("id")) == self.company
                and identifier(companies.get("current_company")) == self.company
                and same_json(context.get("allowed_company_ids", [companies["current_company"]]), [self.company])
                and ("uid" not in context or type(context["uid"]) is int and context["uid"] == self.uid), "AUTHENTICATION_FAILED")
        self.checks["session"] = True
        groups = {name: self.call("res.users", "has_group", [[self.uid], name]) for name in GROUPS}
        require(all(type(value) is bool for value in groups.values()) and groups == expected_groups(self.role))
        self.checks["groups"] = True
        return groups

    def navigation(self):
        raw = self.call("ir.ui.menu", "load_menus", [False])
        require(isinstance(raw, dict))
        items = [item for item in raw.values() if isinstance(item, dict) and isinstance(item.get("xmlid"), str)]
        require(len({item["xmlid"] for item in items}) == len(items))
        indexed = {item["xmlid"]: item for item in items}
        roots = {}
        for key, xmlid in (("trade", "portfolio_trade.menu_portfolio_root"),
                           *(((("import", "portfolio_order_bridge.menu_import_root"),) if self.role in ROLES[:3] else ())),
                           *(((("config", "portfolio_order_bridge.menu_import_config"),) if self.role == "settings" else ()))):
            item = indexed.get(xmlid)
            require(isinstance(item, dict) and identifier(item.get("id")))
            roots[key] = {"id": item["id"], "parent_id": relation(item.get("parent_id")) or False}
        visible = {key for key in MENU_MODELS if self._menu_xmlid(key) in indexed}
        # A Settings user's native menu grants can also contain Sales, but it
        # must never gain the bridge audit pages through System privileges.
        require(visible.intersection({"sources", "customer_maps", "product_maps", "jobs", "attempts"})
                == set(ROLE_MENUS[self.role]).difference({"sales"}))
        if self.role in {"sales", "other_sales"}:
            require("portfolio_order_bridge.menu_import_root" not in indexed)
        menus, actions, views = {}, {}, {}
        for key in ROLE_MENUS[self.role]:
            item = indexed.get(self._menu_xmlid(key))
            require(isinstance(item, dict))
            menus[key] = {name: item.get(name) for name in ("id", "xmlid", "action")}
            menus[key]["parent_id"] = relation(item.get("parent_id"))
            action = self.client.rpc("/web/action/load", {"action_id": self._action_xmlid(key), "context": self.context})
            require(isinstance(action, dict))
            actions[key] = {name: action.get(name) for name in ("id", "res_model", "view_mode")}
            model = MENU_MODELS[key]
            views[model] = {}
            for kind in ("list", "form"):
                view = self.call(model, "get_view", [], view_type=kind)
                require(isinstance(view, dict) and isinstance(view.get("arch"), str))
                views[model][kind] = {"arch": view["arch"]}
                _xml(views[model][kind], kind)
        self.checks.update(menus=True, actions=True, views=True)
        return roots, menus, actions, views

    @staticmethod
    def _menu_xmlid(key):
        return f"portfolio_trade.menu_portfolio_{key}" if key == "sales" else f"portfolio_order_bridge.menu_import_{key}"

    @staticmethod
    def _action_xmlid(key):
        return f"portfolio_trade.action_portfolio_{key}" if key == "sales" else f"portfolio_order_bridge.action_import_{key}"

    def source_facts(self):
        if self.role == "settings":
            # The public queue processes every assigned source. Refuse it if
            # this processor has even an archived additional source.
            self.search(SOURCE, [["processor_user_id", "=", self.operator]], expected=[self.source],
                        extra_context={"active_test": False})
        fields = (*SOURCE_FIELDS, *SOURCE_CONFIG) if self.role in {"settings", "operator"} else SOURCE_FIELDS
        if self.role == "settings":
            fields = (*fields, "processing_cron_id")
        source = self.rows(SOURCE, [self.source], fields, web=True)[0]
        require(relation(source["company_id"]) == self.old["company_a"]["id"] and source["active"] is True
                and source["company_code"] == "PARTS-A" and source["code"] == "B2B-ACCEPTANCE"
                and source["name"] == "Portfolio bridge acceptance source")
        if self.role in {"settings", "operator"}:
            require(relation(source["warehouse_id"]) == self.old["warehouse_a"]["id"]
                    and relation(source["pricelist_id"]) == self.inputs["profile"]["borrowed_finance_pricelist_id"]
                    and relation(source["sales_user_id"]) == self.old["user_sales"]["id"]
                    and relation(source["processor_user_id"]) == self.operator
                    and source["integration_user_ids"] == [self.integration])
        return source

    def mapping_facts(self):
        domain = [["source_id", "=", self.source]]
        customer_id = self.static["customer_map"]["id"]
        product_ids = sorted([self.static[f"product_map_item{i}"]["id"] for i in (1, 2)] +
                             [self.flow["identities"]["product_map_repair"]["id"]])
        self.search(CUSTOMER_MAP, domain, expected=[customer_id])
        self.search(PRODUCT_MAP, domain, expected=product_ids)
        customer = self.rows(CUSTOMER_MAP, [customer_id], ("source_id", "company_id", "active", "external_ref", "partner_id"), web=True)[0]
        require(relation(customer["source_id"]) == self.source and relation(customer["company_id"]) == self.company
                and customer["active"] is True and customer["external_ref"] == "BRIDGE-ACCEPTANCE-CUSTOMER"
                and relation(customer["partner_id"]) == self.static["customer"]["id"])
        products = self.rows(PRODUCT_MAP, product_ids, ("source_id", "company_id", "active", "external_sku", "product_id"), web=True)
        for row in products:
            index = 1 if row["id"] == self.static["product_map_item1"]["id"] else 2
            repair = row["id"] == self.flow["identities"]["product_map_repair"]["id"]
            require(relation(row["source_id"]) == self.source and relation(row["company_id"]) == self.company
                    and row["active"] is True and relation(row["product_id"]) == self.static[f"product_item{index}"]["id"]
                    and row["external_sku"] == ("BRIDGE-ACCEPTANCE-REPAIR" if repair else f"BRIDGE-ACCEPTANCE-{index}"))
        return {"customer": [customer_id], "product": product_ids}, [customer, *products]

    def cron_facts(self, source):
        cron_id = self.static["processing_cron"]["id"]
        require(relation(source["processing_cron_id"]) == cron_id)
        cron = self.rows("ir.cron", [cron_id], ("active", "user_id", "state", "code", "model_id", "interval_number",
                                                  "interval_type", "ir_actions_server_id"))[0]
        require(cron["active"] is False and relation(cron["user_id"]) == self.operator and cron["state"] == "code"
                and cron["code"] == "model._process_pending()" and cron["interval_number"] == 1
                and cron["interval_type"] == "minutes"
                and relation(cron["ir_actions_server_id"]) == self.static["processing_server_action"]["id"])
        model = self.rows("ir.model", [relation(cron["model_id"])], ("model",))[0]
        require(model["model"] == JOB)
        self.checks["cron_configuration"] = True
        return cron

    def catalog_facts(self):
        products = self.rows("product.product", [self.static[f"product_item{i}"]["id"] for i in (1, 2)],
                             ("default_code", "active", "uom_id", "taxes_id", "supplier_taxes_id"))
        require({row["default_code"] for row in products} == {"BRIDGE-ACCEPTANCE-1", "BRIDGE-ACCEPTANCE-2"}
                and all(row["active"] is True and relation(row["uom_id"]) and not row["taxes_id"]
                        and not row["supplier_taxes_id"] for row in products))
        return products

    def audit_facts(self):
        ids = self.flow["identities"]
        job_ids = sorted(ids[f"job_{suffix}"]["id"] for suffix in ("success", "repaired"))
        event_ids = sorted(ids[key]["id"] for key, *_rest in EVENTS)
        self.search(JOB, [["source_id", "=", self.source]], expected=job_ids)
        self.search(ATTEMPT, [["job_id", "in", job_ids]], expected=event_ids)
        require(self.call(JOB, "search_count", [[["source_id", "=", self.source]]]) == 2
                and self.call(ATTEMPT, "search_count", [[["job_id", "in", job_ids]]]) == 4)
        jobs = self.rows(JOB, job_ids, JOB_FIELDS, web=True)
        events = self.rows(ATTEMPT, event_ids, EVENT_FIELDS, web=True)
        for suffix in ("success", "repaired"):
            row = next(row for row in jobs if row["id"] == ids[f"job_{suffix}"]["id"])
            expected = self.flow["jobs"][suffix]
            require(relation(row["source_id"]) == self.source and relation(row["company_id"]) == self.company
                    and row["external_order_id"] == _payload(suffix)["external_order_id"]
                    and relation(row["submitted_by_id"]) == self.integration and relation(row["create_uid"]) == self.integration
                    and row["state"] == expected["state"] and row["attempt_count"] == expected["attempt_count"]
                    and relation(row["sale_order_id"]) == ids[f"quote_{suffix}"]["id"]
                    and not row["error_code"] and not row["error_detail"]
                    and clean_text(row["received_at"]) and clean_text(row["last_attempt_at"])
                    and row["received_at"] <= row["last_attempt_at"])
            payload = parse_json(row["raw_payload"], limit=65536)
            require(payload == _payload(suffix))
            canonical = {key: value for key, value in payload.items() if key not in {"request_id", "sent_at"}}
            canonical["lines"] = sorted(payload["lines"], key=lambda line: line["external_line_id"])
            require(row["canonical_payload"] == _canonical_json(canonical)
                    and row["payload_hash"] == hashlib.sha256(_canonical_json(canonical).encode()).hexdigest())
        for key, suffix, kind, ordinal in EVENTS:
            row = next(row for row in events if row["id"] == ids[key]["id"])
            expected = self.flow["events"][key]
            require(relation(row["job_id"]) == expected["job_id"] and relation(row["company_id"]) == self.company
                    and row["event_type"] == kind and row["attempt_no"] == ordinal
                    and relation(row["actor_id"]) == self.operator and relation(row["create_uid"]) == self.operator
                    and (row["retry_reason"] or None) == expected["retry_reason"]
                    and clean_text(row["started_at"]) and clean_text(row["finished_at"])
                    and row["started_at"] <= row["finished_at"] and row["write_date"] == row["create_date"])
            job = next(job for job in jobs if job["id"] == expected["job_id"])
            require(row["started_at"] >= job["received_at"])
            require((row["error_code"], row["error_path"], row["error_detail"])
                    == (("product_mapping", "lines[1].sku", "An active product mapping is required.")
                        if kind == "failed" else (False, False, False)))
            defaults = parse_json(row["source_defaults_snapshot"], limit=65536)
            require(defaults == {"source_id": self.source, "company_id": self.company,
                                 "warehouse_id": self.old["warehouse_a"]["id"],
                                 "pricelist_id": self.inputs["profile"]["borrowed_finance_pricelist_id"],
                                 "sales_user_id": self.old["user_sales"]["id"], "processor_user_id": self.operator})
            mapping = {"customer": {"mapping_id": self.static["customer_map"]["id"], "partner_id": self.static["customer"]["id"]}, "lines": []}
            for line_index, item in enumerate(_payload(suffix)["lines"]):
                repair = item["sku"] == "BRIDGE-ACCEPTANCE-REPAIR"
                if repair and kind in {"failed", "retry"}:
                    break
                index = 1 if item["sku"] == "BRIDGE-ACCEPTANCE-1" else 2
                # Snapshot unit IDs are verified against the current native
                # product; Integration has read access to ordinary products.
                product_id = self.static[f"product_item{index}"]["id"]
                product = self.rows("product.product", [product_id], ("uom_id",))[0]
                mapping["lines"].append({"line_index": line_index,
                                         "mapping_id": ids["product_map_repair"]["id"] if repair else self.static[f"product_map_item{index}"]["id"],
                                         "product_id": product_id, "uom_id": relation(product["uom_id"])})
            require(parse_json(row["mapping_snapshot"], limit=65536) == mapping)
            if kind == "done":
                require(row["started_at"] == job["last_attempt_at"])
        chronological = [next(row for row in events if row["id"] == ids[key]["id"]) for key in
                         ("attempt_failed", "attempt_retry", "attempt_repaired")]
        require(chronological[0]["finished_at"] <= chronological[1]["started_at"]
                <= chronological[1]["finished_at"] <= chronological[2]["started_at"])
        return {"jobs": jobs, "events": events}

    def quote_facts(self):
        quote_ids = [self.flow["quotes"][suffix]["id"] for suffix in ("success", "repaired")]
        product_ids = [self.static[f"product_item{i}"]["id"] for i in (1, 2)]
        # Discover the complete owned commercial scope. Reading just supplied
        # IDs would miss a duplicate quotation created by a faulty replay.
        self.search("sale.order", ["|", "|", ["partner_id", "=", self.static["customer"]["id"]],
                                   ["origin", "=like", "B2B-ACCEPTANCE:%"],
                                   ["order_line.product_id", "in", product_ids]], expected=quote_ids)
        quotes = self.rows("sale.order", quote_ids, QUOTE_FIELDS, web=True)
        require(all(isinstance(row["order_line"], list) and len(row["order_line"]) == 2
                    and all(identifier(value) for value in row["order_line"]) for row in quotes))
        line_ids = [line_id for quote in quotes for line_id in quote["order_line"]]
        require(len(set(line_ids)) == 4)
        self.search("sale.order.line", ["|", "|", ["order_id.partner_id", "=", self.static["customer"]["id"]],
                                        ["order_id.origin", "=like", "B2B-ACCEPTANCE:%"],
                                        ["product_id", "in", product_ids]], expected=line_ids)
        products = {row["id"]: row for row in self.catalog_facts()}
        policy = self.rows("res.company", [self.company], ("portfolio_approval_enabled", "portfolio_approval_amount",
                                                           "portfolio_approval_discount", "portfolio_approval_policy_version"))[0]
        require(policy["portfolio_approval_enabled"] is True and number(policy["portfolio_approval_amount"])
                and number(policy["portfolio_approval_discount"]) and policy["portfolio_approval_discount"] < 15
                and type(policy["portfolio_approval_policy_version"]) is int)
        all_lines = []
        for suffix in ("success", "repaired"):
            expected = self.flow["quotes"][suffix]
            row = next(row for row in quotes if row["id"] == expected["id"])
            origin = "B2B-ACCEPTANCE:" + _payload(suffix)["external_order_id"]
            require(row["name"] == expected["name"] and row["state"] == "draft"
                    and relation(row["company_id"]) == self.company and relation(row["user_id"]) == self.old["user_sales"]["id"]
                    and relation(row["create_uid"]) == self.operator and relation(row["portfolio_creator_id"]) == self.operator
                    and row["amount_untaxed"] == expected["amount_untaxed"] and row["amount_total"] == expected["amount_total"]
                    and not row["amount_tax"] and row["origin"] == origin and row["client_order_ref"] == origin
                    and all(relation(row[name]) == self.static["customer"]["id"] for name in ("partner_id", "partner_invoice_id", "partner_shipping_id"))
                    and relation(row["warehouse_id"]) == self.old["warehouse_a"]["id"]
                    and relation(row["pricelist_id"]) == self.inputs["profile"]["borrowed_finance_pricelist_id"]
                    and row["portfolio_approval_enabled"] is True and row["portfolio_approval_current"] is False
                    and row["portfolio_approval_state"] == "draft" and not row["portfolio_approval_history_ids"]
                    and not row["portfolio_approval_submitter_id"] and not row["portfolio_approval_fingerprint"]
                    and not row["picking_ids"] and not row["invoice_ids"])
            currency = self.rows("res.currency", [relation(row["currency_id"])], ("name",))[0]
            require(currency["name"] == "CNY")
            require(isinstance(row["order_line"], list) and len(row["order_line"]) == 2)
            lines = self.rows("sale.order.line", row["order_line"], LINE_FIELDS)
            total = Decimal(0)
            for line, item in zip(lines, _payload(suffix)["lines"]):
                index = 1 if item["sku"] == "BRIDGE-ACCEPTANCE-1" else 2
                product_id = self.static[f"product_item{index}"]["id"]
                require(relation(line["order_id"]) == row["id"] and relation(line["product_id"]) == product_id
                        and relation(line["product_uom"]) == relation(products[product_id]["uom_id"])
                        and line["product_uom_qty"] == float(item["quantity"]) and line["price_unit"] == float(item["unit_price"])
                        and line["discount"] == float(item["discount"]) and relation(line["create_uid"]) == self.operator
                        and not any(line[name] for name in ("tax_id", "display_type", "is_downpayment", "qty_delivered",
                                                           "qty_invoiced", "invoice_lines", "move_ids")))
                total += Decimal(item["quantity"]) * Decimal(item["unit_price"]) * (1 - Decimal(item["discount"]) / 100)
            require(total == Decimal(str(expected["amount_total"])))
            all_lines.extend(lines)
        self.checks["quote_details"] = True
        return {"quotes": quotes, "lines": all_lines, "policy": policy}

    def healthy(self):
        # Every denial group must finish with an allowed read in the same
        # authenticated session, including B's own active user record.
        user = self.rows("res.users", [self.uid], ("active", "share", "company_id", "company_ids"))[0]
        require(user["active"] is True and user["share"] is False and relation(user["company_id"]) == self.company
                and user["company_ids"] == [self.company])

    def snapshot(self):
        if self.role in {"operator", "integration"}:
            facts = {"source": self.source_facts(), **self.audit_facts()}
            if self.role == "operator":
                _ids, mappings = self.mapping_facts()
                facts.update(mappings=mappings, **self.quote_facts())
            return facts
        if self.role == "settings":
            source = self.source_facts()
            _ids, mappings = self.mapping_facts()
            return {"source": source, "mappings": mappings, "cron": self.cron_facts(source), "catalog": self.catalog_facts()}
        if self.role == "sales":
            return self.quote_facts()
        ids = [self.flow["quotes"][suffix]["id"] for suffix in ("success", "repaired")]
        self.search("sale.order", [["id", "in", ids]], expected=[])
        return {"foreign_quotes": []}

    def unchanged(self, before):
        self.healthy()
        require(self.snapshot() == before, "BUSINESS_CHANGED", role=self.role)
        observers = self.inputs.get("_observers")
        if observers is not None:
            observers.verify(self.role)

    def deny(self, model, method, args=None, *, names=None, known_message=None, **kwargs):
        return expected_denial(lambda: self.call(model, method, args, **kwargs), names or {ACCESS}, known_message=known_message)

    def negative_batch(self, before, action):
        action()
        self.unchanged(before)

    def audit_crud(self):
        for model, key in ((JOB, "job_success"), (ATTEMPT, "attempt_success")):
            record_id = self.flow["identities"][key]["id"]
            for method, args in (("create", [[{}]]), ("write", [[record_id], {}]),
                                 ("unlink", [[record_id]]), ("copy", [[record_id]])):
                self.deny(model, method, args)
        job_id = self.flow["identities"]["job_success"]["id"]
        for values in ({"state": "pending"}, {"raw_payload": "{}"}, {"company_id": self.old["company_b"]["id"]}):
            self.deny(JOB, "write", [[job_id], values], extra_context={"uid": self.operator, "default_state": "done", "default_company_id": self.company})
        self.checks["audit_crud_denied"] = True

    def service_denials(self):
        ids = [self.flow["identities"][f"job_{suffix}"]["id"] for suffix in ("success", "repaired")]
        if self.role != "operator":
            self.deny(JOB, "action_process_pending", [[]])
            for job_id in ids:
                self.deny(JOB, "action_retry", [[job_id], RETRY_REASON])
                self.deny(JOB, "action_open_retry", [[job_id]])
            self.checks["service_denied"] = True
        else:
            for job_id in ids:
                self.deny(JOB, "action_open_retry", [[job_id]], names={USER}, known_message=RETRY_MESSAGE)
                self.deny(JOB, "action_retry", [[job_id], RETRY_REASON], names={USER}, known_message=RETRY_MESSAGE)
            self.checks["done_retry_denied"] = True

    def private_fields(self):
        fields = ["processing_cron_id"]
        if self.role == "integration":
            fields.extend((*SOURCE_CONFIG, "ingress_revision"))
        for field in fields:
            self.deny(SOURCE, "read", [[self.source]], fields=[field])
            self.deny(SOURCE, "search", [[[field, "!=", False]]])
            self.deny(SOURCE, "search_count", [[[field, "!=", False]]])
            self.deny(SOURCE, "read_group", [[], [field], [field]])
        self.checks["source_private_fields_denied"] = True

    def visibility_denials(self):
        job_ids = [self.flow["identities"][f"job_{suffix}"]["id"] for suffix in ("success", "repaired")]
        if self.role in {"sales", "other_sales"}:
            # A bad-company context proves the context guard, not this ACL.
            # Exercise these reads in each actual user's normal active scope.
            self.deny(SOURCE, "read", [[self.source]], fields=["id"])
            self.deny(SOURCE, "web_read", [[self.source]], specification={"id": {}})
            self.deny(SOURCE, "search", [[["id", "=", self.source]]])
        if self.role in {"settings", "sales", "other_sales"}:
            for model, ids in ((JOB, job_ids), (ATTEMPT, [self.flow["identities"][key]["id"] for key, *_rest in EVENTS])):
                for method, args, kwargs in (("read", [ids], {"fields": ["id"]}),
                                             ("web_read", [ids], {"specification": {"id": {}}}),
                                             ("search", [[]], {}), ("search_count", [[]], {}),
                                             ("read_group", [[], ["id:count"], []], {}),
                                             ("export_data", [ids, ["id"]], {})):
                    self.deny(model, method, args, **kwargs)
            self.checks["job_acl_denied"] = True
        if self.role == "integration":
            for model in (CUSTOMER_MAP, PRODUCT_MAP):
                self.deny(model, "search", [[]])
                self.deny(model, "read", [[self.static["customer_map" if model == CUSTOMER_MAP else "product_map_item1"]["id"]]], fields=["id"])
            quote_ids = [self.flow["quotes"][suffix]["id"] for suffix in ("success", "repaired")]
            self.deny("sale.order", "read", [quote_ids], fields=["id"])
            self.deny("sale.order", "web_read", [quote_ids], specification={"name": {}, "company_id": {}})
            self.checks.update(mappings_denied=True, native_so_denied=True)
        if self.role == "other_sales":
            quote_ids = [self.flow["quotes"][suffix]["id"] for suffix in ("success", "repaired")]
            for extra in ({}, {"allowed_company_ids": [self.old["company_a"]["id"]], "uid": self.old["user_sales"]["id"],
                              "default_company_id": self.old["company_a"]["id"], "default_user_id": self.old["user_sales"]["id"]}):
                self.deny("sale.order", "read", [quote_ids], fields=["name", "company_id"], extra_context=extra)
                self.deny("sale.order", "web_read", [quote_ids], specification={"name": {}, "company_id": {}}, extra_context=extra)
            self.checks["foreign_quotes_hidden"] = True

    def scope_denials(self):
        foreign = self.old["company_a" if self.role == "other_sales" else "company_b"]["id"]
        spoof = {"allowed_company_ids": [foreign], "uid": self.operator,
                 "default_company_id": self.company, "default_state": "done", "force_company": self.company}
        job_ids = [self.flow["identities"][f"job_{suffix}"]["id"] for suffix in ("success", "repaired")]
        for model, ids in ((SOURCE, [self.source]), (JOB, job_ids)):
            for method, args, kwargs in (("read", [ids], {"fields": ["id"]}),
                                         ("web_read", [ids], {"specification": {"id": {}}}),
                                         ("search", [[["id", "in", ids]]], {}),
                                         ("search_count", [[["id", "in", ids]]], {}),
                                         ("read_group", [[["id", "in", ids]], ["id:count"], []], {}),
                                         ("export_data", [ids, ["id"]], {})):
                self.deny(model, method, args, extra_context=spoof, **kwargs)
            self.deny(model, "exists", [ids])
        # A spoofed UID in a valid context must neither widen the record rules
        # nor turn an unauthorized public service/config write into success.
        fake = {"uid": self.operator, "default_company_id": self.company, "default_state": "done",
                "default_processing_cron_id": self.static["processing_cron"]["id"]}
        if self.role in {"operator", "integration"}:
            self.search(JOB, [["source_id", "=", self.source]], expected=job_ids, extra_context=fake)
            require(self.call(JOB, "search_count", [[["source_id", "=", self.source]]], extra_context=fake) == 2)
            self.deny(SOURCE, "write", [[self.source], {"active": True}], extra_context=fake)
        elif self.role == "settings":
            # These two fields reject caller writes before CRUD even for
            # Settings. No configuration-changing write is attempted.
            self.deny(SOURCE, "write", [[self.source], {"processing_cron_id": self.static["processing_cron"]["id"]}], extra_context=fake)
            self.deny(SOURCE, "write", [[self.source], {"ingress_revision": 0}], extra_context=fake)
        else:
            self.deny(SOURCE, "write", [[self.source], {"active": True}], extra_context=fake)
            self.deny(JOB, "web_read", [job_ids], specification={"id": {}}, extra_context=fake)
        # Authorized query APIs also need positive native reads. Export can be
        # denied independently by the native export group; that grants nothing.
        if self.role in ROLES[:3]:
            own = self.search(SOURCE, [["id", "=", self.source]], expected=[self.source])
            require(self.call(SOURCE, "search_count", [[["id", "=", self.source]]]) == 1)
            grouped = self.call(SOURCE, "read_group", [[["id", "=", self.source]], ["company_id"], ["company_id"]])
            require(isinstance(grouped, list) and len(grouped) == 1 and relation(grouped[0].get("company_id")) == self.company)
            try:
                exported = self.call(SOURCE, "export_data", [own, ["id"]])
                require(isinstance(exported, dict) and isinstance(exported.get("datas"), list) and len(exported["datas"]) == 1)
            except RpcSemanticError as error:
                if error.name == USER:
                    groups = self._prepared[0]
                    require(error.known_message == NATIVE_EXPORT_MESSAGE and groups[SYSTEM] is False and groups[ERP] is False)
                    require(self.call("res.users", "has_group", [[self.uid], "base.group_allow_export"]) is False)
                else:
                    require(error.name == ACCESS)
        self.checks.update(cross_company=True, private_rpc_denied=True, no_scope_spoof=True, scoped_queries=True)

    def safe_positive_probes(self, before):
        if self.role == "integration":
            for suffix in ("success", "repaired"):
                job = next(row for row in before["jobs"] if row["id"] == self.flow["jobs"][suffix]["id"])
                payload = parse_json(job["raw_payload"], limit=65536)
                require(payload == _payload(suffix))
                result = self.call(JOB, "submit_order", [payload])
                require(isinstance(result, dict) and identifier(result.get("job_id"))
                        and identifier(result.get("sale_order_id"))
                        and same_json(result, {"job_id": job["id"], "state": "done",
                                               "sale_order_id": self.flow["quotes"][suffix]["id"], "reused": True})
                        and result["reused"] is True)
                self.unchanged(before)
            self.checks["idempotent_reception"] = True
        elif self.role == "operator":
            result = self.call(JOB, "action_process_pending", [[]])
            require(isinstance(result, dict) and result.get("type") == "ir.actions.client"
                    and result.get("tag") == "display_notification" and isinstance(result.get("params"), dict)
                    and result["params"].get("type") == "success"
                    and result["params"].get("message") == "Processed 0 pending import jobs.")
            self.unchanged(before)
            self.checks["queue_empty"] = True
        elif self.role == "sales":
            for suffix in ("success", "repaired"):
                self.deny("sale.order", "action_confirm", [[self.flow["quotes"][suffix]["id"]]],
                          names={USER}, known_message=APPROVAL_MESSAGE)
                self.unchanged(before)
            self.checks["approval_guard"] = True

    def prepare(self):
        if not hasattr(self, "_prepared"):
            groups = self.authenticate()
            roots, menus, actions, views = self.navigation()
            self.healthy()
            self._prepared = groups, roots, menus, actions, views
        return self._prepared

    def collect(self):
        groups, roots, menus, actions, views = self.prepare()
        if "_observers" not in self.inputs:
            self.inputs["_observers"] = _BusinessObservers(self)
        self.healthy()
        before = self.snapshot()
        self.inputs["_observers"].verify(self.role)
        if self.role == "settings":
            self.checks["source_catalog"] = True
        for action in (self.audit_crud, self.service_denials, self.visibility_denials, self.scope_denials):
            self.negative_batch(before, action)
        if self.role in {"operator", "integration"}:
            self.negative_batch(before, self.private_fields)
        self.safe_positive_probes(before)
        self.unchanged(before)
        self.checks.update(healthy_after_negatives=True, business_unchanged=True)
        mapping_ids = self.mapping_facts()[0] if self.role in {"settings", "operator"} else {}
        evidence = {"uid": self.uid, "company_id": self.company, "allowed_company_ids": [self.company],
                    "active_company_ids": [self.company], "groups": groups, "menus": menus, "actions": actions,
                    "views": views, "menu_roots": roots, "source_id": self.source if self.role in ROLES[:3] else None,
                    "mapping_ids": mapping_ids, "cron": {"id": self.static["processing_cron"]["id"], "active": False,
                                                         "processor_uid": self.operator, "fixed_code": True, "source_link": True}
                    if self.role == "settings" else None,
                    "jobs": {key: dict(value) for key, value in self.flow["jobs"].items()} if self.role in {"operator", "integration"} else {},
                    "quotes": {key: dict(value) for key, value in self.flow["quotes"].items()} if self.role in {"operator", "sales"} else {},
                    "events": {key: dict(value) for key, value in self.flow["events"].items()} if self.role in {"operator", "integration"} else {},
                    "checks": self.checks}
        validate_role_evidence(self.role, evidence, self.inputs)
        return evidence


class _BusinessObservers:
    """Keep authorized read sessions alive across every role's probe batches."""

    def __init__(self, current):
        self.collectors = {}
        self.baselines = {}
        inputs = current.inputs
        collectors = inputs.setdefault("_collectors", {})
        collectors[current.role] = current
        clients = inputs.setdefault("_role_clients", {current.role: current.client})
        for role in ("settings", "operator", "sales"):
            if role not in clients:
                clients[role] = JsonRpcClient(inputs["base_url"], role)
            if role not in collectors:
                collectors[role] = _RoleCollector(clients[role], role, inputs)
            observer = collectors[role]
            observer.prepare()
            self.collectors[role] = observer
            self.baselines[role] = observer.snapshot()
        # Settings and Operator independently see the same mapping records.
        require(same_json(self.baselines["settings"]["mappings"], self.baselines["operator"]["mappings"]))
        # A faulty processor may assign a different creator while retaining
        # the native Sales owner. Both ordinary scopes must remain complete.
        for key in ("quotes", "lines", "policy"):
            require(same_json(self.baselines["sales"][key], self.baselines["operator"][key]))

    def verify(self, current_role):
        for role, observer in self.collectors.items():
            observer.healthy()
            try:
                actual = observer.snapshot()
            except VerificationError as error:
                if error.reason == "INVALID_EVIDENCE":
                    raise VerificationError(current_role, "validation", "BUSINESS_CHANGED") from None
                raise
            require(same_json(actual, self.baselines[role]), "BUSINESS_CHANGED", role=current_role)


def collect_role_evidence(client, role, inputs):
    require(role in ROLES, "INVALID_INPUT")
    collectors = inputs.setdefault("_collectors", {})
    if role not in collectors:
        collectors[role] = _RoleCollector(client, role, inputs)
    return collectors[role].collect()


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message):
        raise VerificationError(reason="INVALID_INPUT")


def atomic_output(inputs, serialized):
    path = runtime_path(inputs["output"], existing=False)
    require(path not in inputs["input_paths"]
            and not any(path.exists() and os.path.samefile(path, source) for source in inputs["input_paths"]), "OUTPUT_FAILED")
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".bridge-http-", dir=path.parent)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        runtime_path(path, existing=False)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            os.unlink(temporary)


def main(argv=None):
    role, operation = "input", "validation"
    try:
        parser = SafeArgumentParser(add_help=False, allow_abbrev=False)
        for name in ("base-url", "database", "credentials", "sales-credentials", "acceptance-report",
                     "profile-report", "flow-report", "output"):
            parser.add_argument(f"--{name}", required=True)
        args = parser.parse_args(argv)
        inputs = load_inputs(base_url=args.base_url, database=args.database, credentials_path=args.credentials,
                             sales_credentials_path=args.sales_credentials, acceptance_report_path=args.acceptance_report,
                             profile_report_path=args.profile_report, flow_report_path=args.flow_report, output_path=args.output)
        inputs["_role_clients"] = {role: JsonRpcClient(inputs["base_url"], role) for role in ROLES}
        evidence = {}
        for role in ROLES:
            operation = "role_verification"
            evidence[role] = collect_role_evidence(inputs["_role_clients"][role], role, inputs)
            validate_role_evidence(role, evidence[role], inputs)
        report = validate_evidence(evidence, inputs)
        serialized = json.dumps(report, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n"
        operation = "output"
        atomic_output(inputs, serialized)
        print(serialized, end="")
        return 0
    except Exception as error:
        # Never stringify an arbitrary exception: it may contain credentials,
        # cookies, HTML or a server traceback. KeyboardInterrupt is not caught.
        reason = "UNEXPECTED_FAILURE"
        if isinstance(error, VerificationError):
            role, operation, reason = error.role if error.role != "input" else role, error.operation, error.reason
        elif operation == "validation":
            reason = "INVALID_INPUT"
        print(json.dumps({"schema_version": 1, "ok": False, "role": role, "operation": operation, "reason": reason}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
