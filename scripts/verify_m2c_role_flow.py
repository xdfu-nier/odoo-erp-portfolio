"""Verify the completed local fulfillment flow through seven private HTTP sessions."""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

import acceptance_credentials

ROOT = Path(__file__).resolve().parents[1]
ROLES = acceptance_credentials.ROLES
ROLE_GROUPS = {
    "sales": "sales_team.group_sale_salesman", "manager": "sales_team.group_sale_manager",
    "manager2": "sales_team.group_sale_manager", "buyer": "purchase.group_purchase_user",
    "warehouse": "stock.group_stock_user", "finance": "account.group_account_invoice",
    "other_sales": "sales_team.group_sale_salesman",
}
IDENTITIES = {
    "company_a": "res.company", "company_b": "res.company", "warehouse_a": "stock.warehouse",
    "customer": "res.partner", "vendor": "res.partner", "initial_bearing_move": "stock.move",
    **{f"user_{role}": "res.users" for role in ROLES},
    **{f"product_{key}": "product.product" for key in ("bearing", "sensor", "bolt")},
    **{f"supplier_{key}": "product.supplierinfo" for key in ("bearing", "sensor", "bolt")},
}
DOCUMENTS = {
    "sale_order": "sale.order", "purchase_order": "purchase.order",
    "receipt_first": "stock.picking", "receipt_second": "stock.picking",
    "delivery_first": "stock.picking", "delivery_second": "stock.picking",
    "return_picking": "stock.picking", "invoice_first": "account.move",
    "invoice_second": "account.move", "credit_note": "account.move",
}
MENUS = {"sales": "sale.order", "purchases": "purchase.order",
         "stock": "stock.picking", "invoices": "account.move"}
ROLE_MENUS = {"sales": ("sales",), "manager": ("sales",), "manager2": ("sales",),
              "buyer": ("purchases",), "warehouse": ("stock",),
              "finance": ("sales", "invoices"), "other_sales": ("sales",)}
PHYSICAL = {"gross_delivered_qty": 100.0, "returned_qty": 10.0, "net_delivered_qty": 90.0}
FINANCIAL = {"draft_invoice_qty": 0.0, "posted_invoice_qty": 100.0, "draft_credit_qty": 0.0,
             "posted_credit_qty": 10.0, "posted_signed_amount": 7650.0}
FINANCIAL_FIELDS = {*FINANCIAL, "invoice_ids", "credit_note_ids"}
TRACE_DOCUMENTS = {
    "purchase_order_ids": ("purchase_order",),
    "receipt_picking_ids": ("receipt_first", "receipt_second"),
    "delivery_picking_ids": ("delivery_first", "delivery_second"),
    "backorder_picking_ids": ("receipt_second", "delivery_second"),
    "return_picking_ids": ("return_picking",), "invoice_ids": ("invoice_first", "invoice_second"),
    "credit_note_ids": ("credit_note",),
}
SALES_BUTTONS = {"action_confirm", "action_quotation_send", "action_cancel", "action_draft",
                 "action_portfolio_submit", "action_portfolio_approve", "action_portfolio_reject_wizard"}


class VerificationError(ValueError):
    """A controlled failure whose public text never contains a remote response."""

    def __init__(self, role="input", operation="validation", reason="Verification failed."):
        super().__init__(reason)
        self.role, self.operation, self.reason = role, operation, reason


def require(condition, reason="Evidence does not satisfy the local acceptance contract."):
    if not condition:
        raise VerificationError(reason=reason)


def identifier(value):
    if isinstance(value, dict):
        value = value.get("id")
    elif isinstance(value, (list, tuple)):
        value = value[0] if value else None
    return value if type(value) is int and value > 0 else None


def runtime_path(path, *, existing=True):
    path = Path(path)
    if not path.is_absolute():
        path = ROOT / path
    runtime = ROOT / ".runtime"
    require(path.resolve().is_relative_to(runtime.resolve()), "Paths must stay in the project runtime directory.")
    require(not any(item.is_symlink() for item in (path, *path.parents) if item.is_relative_to(ROOT)),
            "Symlink input and output paths are not allowed.")
    if existing:
        require(path.is_file(), "An existing regular input file is required.")
    else:
        require(path.parent.is_dir() and (not path.exists() or path.is_file()), "The report destination is invalid.")
    return path


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        raise VerificationError(reason="An input report is malformed or unreadable.") from None


def validate_target(base_url, database):
    try:
        url = urllib.parse.urlsplit(base_url)
        valid = (url.scheme == "http" and url.hostname == "127.0.0.1" and url.port is not None
                 and 0 < url.port < 65536 and url.username is None and url.password is None
                 and not url.path and not url.query and not url.fragment)
    except (ValueError, TypeError):
        valid = False
    require(valid and database in acceptance_credentials.DATABASES, "Only an allowed database and loopback HTTP origin are accepted.")


def validate_reports(acceptance, flow, database):
    require(isinstance(acceptance, dict) and acceptance.get("namespace") == "portfolio_trade_acceptance")
    identities = acceptance.get("identities", {})
    require(set(identities) == set(IDENTITIES))
    for key, model in IDENTITIES.items():
        require(isinstance(identities[key], dict) and identities[key].get("model") == model
                and identifier(identities[key].get("id")))
    require(len({identities[f"user_{role}"]["id"] for role in ROLES}) == 7)
    require(identities["company_a"]["id"] != identities["company_b"]["id"])
    if "record_counts" in acceptance:
        require(acceptance["record_counts"] == dict(Counter(IDENTITIES.values())))
        require(acceptance.get("initial_bearing_move_id") == identities["initial_bearing_move"]["id"])
    require(isinstance(flow, dict) and flow.get("database") == database
            and flow.get("namespace") == "portfolio_trade_fulfillment"
            and flow.get("company_id") == identities["company_a"]["id"])
    docs = flow.get("identities", {})
    require(set(docs) == set(DOCUMENTS))
    for key, model in DOCUMENTS.items():
        item = docs[key]
        require(isinstance(item, dict) and item.get("model") == model and identifier(item.get("id"))
                and isinstance(item.get("name"), str) and item["name"].strip()
                and len(item["name"]) < 200 and not any(ord(char) < 32 for char in item["name"]))
    require(len({(item["model"], item["id"]) for item in docs.values()}) == 10)
    expected_actors = {role: identities[f"user_{role}"]["id"] for role in ("sales", "manager", "buyer", "warehouse", "finance")}
    require(flow.get("actors") == expected_actors)
    require(flow.get("physical") == {"delivered": 100.0, "returned": 10.0, "net": 90.0, "stock": 10.0})
    require(flow.get("financial") == {"invoice_posted": 100.0, "credit_posted": 10.0,
                                      "signed_posted": 90.0, "net_amount": 7650.0, "currency": "CNY"})


def load_inputs(*, base_url, database, credentials_path, acceptance_report_path, flow_report_path, output_path):
    validate_target(base_url, database)
    paths = [runtime_path(path) for path in (credentials_path, acceptance_report_path, flow_report_path)]
    output = runtime_path(output_path, existing=False)
    acceptance, flow = read_json(paths[1]), read_json(paths[2])
    validate_reports(acceptance, flow, database)
    credentials = acceptance_credentials._read_credentials(paths[0], database)
    require(output not in paths, "The output cannot replace an input file.")
    return {"base_url": base_url, "database": database, "credentials": credentials,
            "acceptance": acceptance, "flow": flow, "output": output}


def xml_view(view):
    try:
        return ET.fromstring(view["arch"])
    except (KeyError, TypeError, ET.ParseError):
        raise VerificationError(reason="A native view could not be verified.") from None


def readonly_sales(view):
    node = xml_view(view)
    require(node.get("create") in {"0", "false", "False"}
            and node.get("edit") in {"0", "false", "False"}, "Source sales must be read-only for this role.")
    require(not any(button.get("name") in SALES_BUTTONS for button in node.iter("button")),
            "A source sales view exposes Sales write controls.")


def validate_role_evidence(role, evidence, inputs):
    require(role in ROLES)
    acceptance, flow = inputs["acceptance"], inputs["flow"]
    identities = acceptance["identities"]
    company = identities["company_b" if role == "other_sales" else "company_a"]["id"]
    uid = identities[f"user_{role}"]["id"]
    require(evidence.get("http_status") == 200 and evidence.get("uid") == uid)
    require(evidence.get("company_id") == company and evidence.get("allowed_company_ids") == [company]
            and evidence.get("active_company_ids") == [company], "The actual user company scope is incorrect.")
    groups = evidence.get("groups", {})
    require(groups.get("base.group_user") is True and groups.get(ROLE_GROUPS[role]) is True
            and groups.get("base.group_system") is False, "The actual user groups are incorrect.")
    menus, actions, views = evidence.get("menus", {}), evidence.get("actions", {}), evidence.get("views", {})
    safe_menus = {}
    for key in ROLE_MENUS[role]:
        menu, action = menus.get(key, {}), actions.get(key, {})
        require(identifier(menu.get("id")) and identifier(action.get("id")) and identifier(menu.get("parent_id")))
        require(menu.get("xmlid") == f"portfolio_trade.menu_portfolio_{key}"
                and menu.get("action") == f"ir.actions.act_window,{action.get('id')}"
                and action.get("res_model") == MENUS[key]
                and {"list", "form"}.issubset(set(action.get("view_mode", "").split(","))))
        xml_view(views.get(MENUS[key], {}))
        safe_menus[key] = {"id": menu["id"], "xmlid": menu["xmlid"], "name": menu["name"],
                           "action_id": action["id"], "model": MENUS[key]}
    if role in {"finance", "buyer"}:
        readonly_sales(views.get("sale.order", {}))
        require(evidence.get("source_readonly") is True)
    documents, safe_docs = evidence.get("documents", {}), {}
    require(set(documents) == set(DOCUMENTS))
    mandatory = {"sale_order"} if role not in {"warehouse", "other_sales"} else set()
    if role == "buyer":
        mandatory.add("purchase_order")
    if role == "warehouse":
        mandatory |= {key for key, model in DOCUMENTS.items() if model == "stock.picking"}
    if role == "finance":
        mandatory |= {"invoice_first", "invoice_second", "credit_note"}
    for key, model in DOCUMENTS.items():
        item, expected = documents[key], flow["identities"][key]
        require(item.get("model") == model and type(item.get("readable")) is bool)
        rows = item.get("records")
        if item["readable"]:
            require(isinstance(rows, list) and len(rows) == 1)
            row = rows[0]
            expected_state = "sale" if model == "sale.order" else "purchase" if model == "purchase.order" else "posted" if model == "account.move" else "done"
            require(row.get("id") == expected["id"] and row.get("name") == expected["name"]
                    and identifier(row.get("company_id")) == flow["company_id"] and row.get("state") == expected_state)
            safe_docs[key] = expected["id"]
        else:
            require(rows == [] and key not in mandatory)
    cross = evidence.get("cross_company", {})
    require(cross.get("search_ids") == [] and cross.get("read_denied") is True
            and cross.get("trace_denied") is True and type(cross.get("trace_count_before")) is int
            and cross.get("trace_count_before") == cross.get("trace_count_after"),
            "Cross-company denial or snapshot preservation could not be verified.")
    facts, trace_docs = {}, {}
    if role in {"warehouse", "other_sales"}:
        require(evidence.get("trace") is None and evidence.get("trace_denied") is True)
        if role == "other_sales":
            require(evidence.get("source_records") == [] and not safe_docs)
        else:
            require(evidence.get("source_records") == documents["sale_order"]["records"])
    else:
        require(evidence.get("source_records") == documents["sale_order"]["records"] and evidence["source_records"])
        trace = evidence.get("trace")
        require(isinstance(trace, dict))
        action = trace.get("action", {})
        require(action.get("res_model") == "portfolio.fulfillment.trace" and identifier(action.get("res_id"))
                and action.get("view_mode") == "form" and action.get("target") == "new")
        arch = xml_view(trace.get("view", {}))
        require(all(arch.get(key) in {"0", "false", "False"} for key in ("create", "edit", "delete")))
        rows = trace.get("records")
        require(isinstance(rows, list) and len(rows) == 1)
        row = rows[0]
        require(row.get("id") == action["res_id"] and identifier(row.get("sale_order_id")) == flow["identities"]["sale_order"]["id"]
                and identifier(row.get("company_id")) == company and identifier(row.get("quantity_uom_id")))
        require(all(row.get(key) == value for key, value in PHYSICAL.items()))
        lines = row.get("line_ids")
        require(isinstance(lines, list) and len(lines) == 1 and identifier(lines[0].get("product_uom_id"))
                and identifier(lines[0].get("source_line_id")) and all(lines[0].get(key) == value for key, value in PHYSICAL.items()))
        fields = set(trace.get("fields", []))
        arch_fields = {node.get("name") for node in arch.iter("field")}
        require(set(PHYSICAL).issubset(fields & arch_fields))
        if role == "finance":
            require(FINANCIAL_FIELDS.issubset(fields & arch_fields)
                    and all(row.get(key) == value for key, value in FINANCIAL.items()))
            facts |= FINANCIAL
        else:
            require(not FINANCIAL_FIELDS.intersection(fields | arch_fields | set(row) | set(lines[0])),
                    "A non-Billing trace exposes financial fields.")
        for field, keys in TRACE_DOCUMENTS.items():
            if field in FINANCIAL_FIELDS and role != "finance":
                continue
            expected_ids = {flow["identities"][key]["id"] for key in keys if key in safe_docs}
            require(set(row.get(field, [])) == expected_ids, "Trace lists differ from actual readable native documents.")
            trace_docs.update({key: safe_docs[key] for key in keys if key in safe_docs})
        facts |= PHYSICAL
    source_action = actions.get("sales", {}).get("id", evidence.get("source_action_id"))
    require(identifier(source_action) or role == "warehouse")
    result = {
        "uid": uid, "http_status": 200, "company_id": company,
        "allowed_company_ids": [company], "active_company_ids": [company],
        "groups": sorted(key for key, value in groups.items() if value is True),
        "menus": safe_menus,
        "actions": {key: {"id": item["id"], "res_model": item["res_model"], "view_mode": item["view_mode"]}
                    for key, item in actions.items()},
        "documents": safe_docs, "trace_documents": trace_docs, "facts": facts,
        "source_route": f"/odoo/action-{source_action}/{flow['identities']['sale_order']['id']}" if source_action else None,
        "checks": {"authenticated": True, "company_scope": True, "required_groups": True,
                   "menus_and_views": True, "native_records_read": True, "cross_company_denied": True,
                   "snapshot_preserved": True, "trace_read": role not in {"warehouse", "other_sales"},
                   "source_readonly": role in {"buyer", "finance"}, "financial_fields_scoped": True},
    }
    return result


def validate_evidence(evidence_by_role, inputs):
    require(set(evidence_by_role) == set(ROLES))
    return {"schema_version": 1, "ok": True, "database": inputs["database"],
            "roles": {role: validate_role_evidence(role, evidence_by_role[role], inputs) for role in ROLES}}


class RejectRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise VerificationError(reason="Unexpected HTTP redirect.")


class JsonRpcClient:
    def __init__(self, base_url, role, opener=None):
        self.base_url, self.role = base_url, role
        self.opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), RejectRedirect(),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        )

    def rpc(self, path, params):
        operation = params.get("method", "authentication" if path.endswith("authenticate") else "navigation")
        try:
            request = urllib.request.Request(self.base_url + path, data=json.dumps({
                "jsonrpc": "2.0", "method": "call", "params": params, "id": 1,
            }).encode(), headers={"Content-Type": "application/json"})
            with self.opener.open(request, timeout=30) as response:
                if response.status != 200:
                    raise ValueError
                data = json.loads(response.read(8_000_001))
            if not isinstance(data, dict) or "error" in data or "result" not in data:
                raise ValueError
            return data["result"]
        except (OSError, ValueError, TypeError, urllib.error.URLError):
            raise VerificationError(self.role, operation, "The authenticated HTTP operation failed.") from None

    def call(self, model, method, args=None, kwargs=None):
        return self.rpc(f"/web/dataset/call_kw/{model}/{method}", {
            "model": model, "method": method, "args": args or [], "kwargs": kwargs or {},
        })


def denied(operation):
    try:
        operation()
    except VerificationError:
        return True
    return False


def collect_role_evidence(client, role, inputs):
    account = inputs["credentials"]["accounts"][role]
    session = client.rpc("/web/session/authenticate", {
        "db": inputs["database"], "login": account["login"], "password": account["password"],
    })
    expected_uid = inputs["acceptance"]["identities"][f"user_{role}"]["id"]
    require(session.get("uid") == expected_uid, "Authentication returned an unexpected user.")
    company = inputs["acceptance"]["identities"]["company_b" if role == "other_sales" else "company_a"]["id"]
    companies = session.get("user_companies", {})
    allowed = sorted(int(key) for key in companies.get("allowed_companies", {}))
    active = session.get("user_context", {}).get("allowed_company_ids", [companies.get("current_company")])
    require(allowed == [company] and active == [company] and companies.get("current_company") == company,
            "The authenticated company scope is incorrect.")
    context = {"allowed_company_ids": [company], "lang": "en_US"}

    def call(model, method, args=None, *, extra_context=None, **kwargs):
        return client.call(model, method, args, {"context": context | (extra_context or {}), **kwargs})

    groups = {name: call("res.users", "has_group", [[expected_uid], name]) for name in sorted({
        "base.group_user", "base.group_system", *ROLE_GROUPS.values(),
    })}
    menus_raw = call("ir.ui.menu", "load_menus", [False])
    require(isinstance(menus_raw, dict))
    root = next((item for item in menus_raw.values() if item.get("xmlid") == "portfolio_trade.menu_portfolio_root"), None)
    require(root and not root.get("parent_id"))
    menus, actions, views = {}, {}, {}
    for key in ROLE_MENUS[role]:
        menu = next((item for item in menus_raw.values() if item.get("xmlid") == f"portfolio_trade.menu_portfolio_{key}"), None)
        require(menu and identifier(menu.get("parent_id")) == root["id"])
        menus[key] = menu
        actions[key] = client.rpc("/web/action/load", {"action_id": f"portfolio_trade.action_portfolio_{key}", "context": context})
        views[MENUS[key]] = call(MENUS[key], "get_view", [], view_type="form")
        # Load the native list too: an action's view_mode string is not view evidence.
        xml_view(call(MENUS[key], "get_view", [], view_type="list"))
    source_action = actions.get("sales")
    if role != "warehouse" and source_action is None:
        source_action = client.rpc("/web/action/load", {"action_id": "portfolio_trade.action_portfolio_sales", "context": context})
        require(source_action.get("res_model") == "sale.order")
    documents, model_reads = {}, {}
    for key, model in DOCUMENTS.items():
        identity = inputs["flow"]["identities"][key]
        if model not in model_reads:
            can_read = call(model, "check_access_rights", ["read"], raise_exception=False)
            ids = call(model, "search", [[["id", "in", [item["id"] for item in inputs["flow"]["identities"].values() if item["model"] == model]],
                                           ["company_id", "=", inputs["flow"]["company_id"]]]]) if can_read else []
            rows = call(model, "web_read", [ids], specification={name: {} for name in ("name", "company_id", "state")}) if ids else []
            model_reads[model] = {row["id"]: row for row in rows}
        row = model_reads[model].get(identity["id"])
        documents[key] = {"model": model, "readable": row is not None, "records": [row] if row else []}
    source_id = inputs["flow"]["identities"]["sale_order"]["id"]
    trace = None
    trace_denied = False
    if role not in {"warehouse", "other_sales"}:
        views["sale.order"] = views.get("sale.order") or call("sale.order", "get_view", [], view_type="form")
        action = call("sale.order", "action_portfolio_trace", [[source_id]])
        view = call("portfolio.fulfillment.trace", "get_view", [], view_id=action.get("view_id"), view_type="form")
        fields = call("portfolio.fulfillment.trace", "fields_get", [], attributes=["type", "string"])
        names = {"sale_order_id", "company_id", "quantity_uom_id", "line_ids", "return_move_ids", *PHYSICAL, *TRACE_DOCUMENTS}
        if role != "finance":
            names -= FINANCIAL_FIELDS
        else:
            names |= FINANCIAL_FIELDS
        line_names = {"source_line_id", "product_id", "product_uom_id", *PHYSICAL}
        if role == "finance":
            line_names |= set(FINANCIAL)
        specification = {name: {} for name in names}
        specification["line_ids"] = {"fields": {name: {} for name in line_names}}
        rows = call("portfolio.fulfillment.trace", "web_read", [[action["res_id"]]], specification=specification)
        trace = {"action": action, "view": view, "fields": sorted(fields), "records": rows}
    else:
        trace_denied = denied(lambda: call("sale.order", "action_portfolio_trace", [[source_id]]))
    # A users must also reject the source when its actual company is inactive.
    # B uses its own active company for the same source, search and direct-read negatives.
    other_company = inputs["acceptance"]["identities"]["company_b"]["id"]
    negative_context = {"allowed_company_ids": [other_company]}
    can_trace = role != "warehouse"
    before = call("portfolio.fulfillment.trace", "search_count", [[]]) if can_trace else 0
    search_ids = call("sale.order", "search", [[["id", "=", source_id]]], extra_context=negative_context) if role == "other_sales" else []
    cross_read = denied(lambda: call("sale.order", "web_read", [[source_id]],
                                    extra_context=negative_context, specification={"name": {}, "company_id": {}}))
    cross_trace = denied(lambda: call("sale.order", "action_portfolio_trace", [[source_id]], extra_context=negative_context))
    after = call("portfolio.fulfillment.trace", "search_count", [[]]) if can_trace else 0
    return {
        "http_status": 200, "uid": session["uid"], "company_id": companies["current_company"],
        "allowed_company_ids": allowed, "active_company_ids": active, "groups": groups,
        "menus": menus, "actions": actions, "views": views, "documents": documents,
        "source_records": documents["sale_order"]["records"], "trace": trace,
        "source_action_id": source_action.get("id") if source_action else None,
        "source_readonly": role in {"buyer", "finance"}, "trace_denied": trace_denied,
        "cross_company": {"search_ids": search_ids, "read_denied": cross_read, "trace_denied": cross_trace,
                          "trace_count_before": before, "trace_count_after": after},
    }


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message):
        raise VerificationError(reason="Invalid verifier arguments.")


def main(argv=None):
    role, operation = "input", "validation"
    try:
        parser = SafeArgumentParser(description=__doc__, add_help=False)
        for name in ("base-url", "database", "credentials", "acceptance-report", "flow-report", "output"):
            parser.add_argument(f"--{name}", required=True)
        args = parser.parse_args(argv)
        inputs = load_inputs(base_url=args.base_url, database=args.database, credentials_path=args.credentials,
                             acceptance_report_path=args.acceptance_report, flow_report_path=args.flow_report,
                             output_path=args.output)
        evidence = {}
        for role in ROLES:
            operation = "role verification"
            evidence[role] = collect_role_evidence(JsonRpcClient(inputs["base_url"], role), role, inputs)
            validate_role_evidence(role, evidence[role], inputs)
        report = validate_evidence(evidence, inputs)
        serialized = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        inputs["output"].write_text(serialized, encoding="utf-8")
        print(serialized, end="")
        return 0
    except (ValueError, OSError, KeyError, TypeError, AttributeError) as error:
        if isinstance(error, VerificationError) and error.operation in {
            "validation", "authentication", "navigation", "has_group", "load_menus", "get_view",
            "fields_get", "check_access_rights", "search", "web_read", "search_count", "action_portfolio_trace",
        }:
            operation = error.operation
        print(json.dumps({"ok": False, "role": role, "operation": operation,
                          "reason": "Local role verification failed."}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
