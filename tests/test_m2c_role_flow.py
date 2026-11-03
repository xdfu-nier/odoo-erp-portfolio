"""Pure contracts for the local seven-role HTTP verifier; never start Odoo."""

import contextlib
import copy
import http.cookiejar
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

ROLES = ("sales", "manager", "manager2", "buyer", "warehouse", "finance", "other_sales")
GROUPS = {
    "sales": "sales_team.group_sale_salesman",
    "manager": "sales_team.group_sale_manager", "manager2": "sales_team.group_sale_manager",
    "buyer": "purchase.group_purchase_user", "warehouse": "stock.group_stock_user",
    "finance": "account.group_account_invoice", "other_sales": "sales_team.group_sale_salesman",
}
MODELS = {
    "sale_order": "sale.order", "purchase_order": "purchase.order",
    "receipt_first": "stock.picking", "receipt_second": "stock.picking",
    "delivery_first": "stock.picking", "delivery_second": "stock.picking",
    "return_picking": "stock.picking", "invoice_first": "account.move",
    "invoice_second": "account.move", "credit_note": "account.move",
}
MENUS = {"sales": ("sale.order", "销售订单"), "purchases": ("purchase.order", "采购订单"),
         "stock": ("stock.picking", "收发货"), "invoices": ("account.move", "客户发票")}
FINANCIAL_FIELDS = {"draft_invoice_qty", "posted_invoice_qty", "draft_credit_qty",
                    "posted_credit_qty", "posted_signed_amount", "invoice_ids", "credit_note_ids"}


def reports():
    identity_models = {
        "company_a": "res.company", "company_b": "res.company", "warehouse_a": "stock.warehouse",
        "customer": "res.partner", "vendor": "res.partner", "initial_bearing_move": "stock.move",
        **{f"user_{role}": "res.users" for role in ROLES},
        **{f"product_{key}": "product.product" for key in ("bearing", "sensor", "bolt")},
        **{f"supplier_{key}": "product.supplierinfo" for key in ("bearing", "sensor", "bolt")},
    }
    identities = {key: {"model": model, "id": index + 101}
                  for index, (key, model) in enumerate(identity_models.items())}
    acceptance = {"namespace": "portfolio_trade_acceptance", "identities": identities,
                  "record_counts": {"res.company": 2, "stock.warehouse": 1, "res.partner": 2,
                                    "stock.move": 1, "res.users": 7, "product.product": 3,
                                    "product.supplierinfo": 3},
                  "bearing_quantity": 40.0, "bearing_move_count": 1,
                  "initial_bearing_move_id": identities["initial_bearing_move"]["id"]}
    flow = {
        "database": "odoo_portfolio", "namespace": "portfolio_trade_fulfillment",
        "company_id": identities["company_a"]["id"],
        "identities": {key: {"model": model, "id": index + 701, "name": f"M2C-{key}"}
                       for index, (key, model) in enumerate(MODELS.items())},
        "actors": {role: identities[f"user_{role}"]["id"]
                   for role in ("sales", "manager", "buyer", "warehouse", "finance")},
        "physical": {"delivered": 100.0, "returned": 10.0, "net": 90.0, "stock": 10.0},
        "financial": {"invoice_posted": 100.0, "credit_posted": 10.0, "signed_posted": 90.0,
                      "net_amount": 7650.0, "currency": "CNY"},
    }
    return acceptance, flow


def role_evidence(role, acceptance, flow):
    """Native response evidence, kept internal rather than copied to the safe report."""
    company = acceptance["identities"]["company_b" if role == "other_sales" else "company_a"]["id"]
    uid = acceptance["identities"][f"user_{role}"]["id"]
    keys = {"sales": ["sales"], "manager": ["sales"], "manager2": ["sales"],
            "buyer": ["purchases"], "warehouse": ["stock"],
            "finance": ["sales", "invoices"], "other_sales": ["sales"]}[role]
    menus, actions, views = {}, {}, {}
    for key in keys:
        model, label = MENUS[key]
        index = list(MENUS).index(key)
        menus[key] = {"id": 201 + index, "xmlid": f"portfolio_trade.menu_portfolio_{key}",
                      "name": label, "action": f"ir.actions.act_window,{301 + index}",
                      "parent_id": 200}
        actions[key] = {"id": 301 + index, "res_model": model, "view_mode": "list,form",
                        "name": label}
        views[model] = {"arch": '<form create="0" edit="0"><field name="name"/></form>'}
    source = flow["identities"]["sale_order"]
    documents = {}
    allowed_models = {"sale.order", "purchase.order", "stock.picking"}
    if role == "finance":
        allowed_models = {"sale.order", "account.move"}
    elif role == "buyer":
        allowed_models = {"sale.order", "purchase.order", "stock.picking"}
    elif role == "warehouse":
        allowed_models = {"stock.picking"}
    elif role == "other_sales":
        allowed_models = set()
    for key, identity in flow["identities"].items():
        documents[key] = {"model": identity["model"], "readable": identity["model"] in allowed_models,
                          "records": [{"id": identity["id"], "name": identity["name"],
                                       "company_id": [company, "PARTS-A"],
                                       "state": "sale" if key == "sale_order" else
                                       "purchase" if key == "purchase_order" else
                                       "posted" if identity["model"] == "account.move" else "done"}]
                          if identity["model"] in allowed_models else []}
    trace = None
    if role not in {"warehouse", "other_sales"}:
        views["sale.order"] = {"arch": '<form create="0" edit="0"><button name="action_portfolio_trace"/><field name="name"/></form>'}
        quantities = {"gross_delivered_qty": 100.0, "returned_qty": 10.0, "net_delivered_qty": 90.0}
        fields = {"sale_order_id", "company_id", "quantity_uom_id", "line_ids", *quantities,
                  "purchase_order_ids", "receipt_picking_ids", "delivery_picking_ids",
                  "backorder_picking_ids", "return_picking_ids", "return_move_ids"}
        record = {"id": 901, "sale_order_id": [source["id"], source["name"]],
                  "company_id": [company, "PARTS-A"], "quantity_uom_id": [1, "Units"],
                  **quantities, "line_ids": [{"id": 902, "product_uom_id": [1, "Units"],
                                            "source_line_id": [801, "Bearing"], **quantities}],
                  "purchase_order_ids": [], "receipt_picking_ids": [],
                  "delivery_picking_ids": [], "backorder_picking_ids": [],
                  "return_picking_ids": [], "return_move_ids": []}
        if role == "finance":
            fields |= FINANCIAL_FIELDS
            record.update(draft_invoice_qty=0, posted_invoice_qty=100, draft_credit_qty=0,
                          posted_credit_qty=10, posted_signed_amount=7650,
                          invoice_ids=[flow["identities"][key]["id"] for key in ("invoice_first", "invoice_second")],
                          credit_note_ids=[flow["identities"]["credit_note"]["id"]])
        trace = {"action": {"res_model": "portfolio.fulfillment.trace", "res_id": 901,
                            "view_mode": "form", "target": "new"},
                 "view": {"arch": '<form create="0" edit="0" delete="0">' +
                          "".join(f'<field name="{name}"/>' for name in sorted(fields)) + "</form>"},
                 "fields": sorted(fields), "records": [record]}
        for field, keys in {
            "purchase_order_ids": ("purchase_order",),
            "receipt_picking_ids": ("receipt_first", "receipt_second"),
            "delivery_picking_ids": ("delivery_first", "delivery_second"),
            "backorder_picking_ids": ("receipt_second", "delivery_second"),
            "return_picking_ids": ("return_picking",),
        }.items():
            record[field] = [flow["identities"][key]["id"] for key in keys if documents[key]["readable"]]
    return {
        "http_status": 200, "uid": uid, "company_id": company,
        "allowed_company_ids": [company], "active_company_ids": [company],
        "groups": {"base.group_user": True, "base.group_system": False, GROUPS[role]: True},
        "menus": menus, "actions": actions, "views": views, "documents": documents,
        "source_records": documents["sale_order"]["records"], "trace": trace,
        "source_readonly": role in {"buyer", "finance"},
        "source_action_id": 301 if role != "warehouse" else None,
        "trace_denied": role in {"warehouse", "other_sales"},
        "cross_company": {"search_ids": [], "read_denied": True, "trace_denied": True,
                          "trace_count_before": 0, "trace_count_after": 0},
    }


class M2cRoleFlowTests(unittest.TestCase):
    def setUp(self):
        script = ROOT / "scripts" / "verify_m2c_role_flow.py"
        self.assertTrue(script.is_file(), "Seven-role HTTP verifier is missing")
        spec = importlib.util.spec_from_file_location("m2c_role_flow_under_test", script)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        for name in ("load_inputs", "validate_role_evidence", "validate_evidence", "JsonRpcClient", "main"):
            self.assertTrue(callable(getattr(self.module, name, None)), f"Verifier API {name} is missing")
        base = ROOT / ".runtime" / "tests"
        base.mkdir(parents=True, exist_ok=True)
        directory = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.acceptance, self.flow = reports()
        self.credentials = {"schema_version": 1, "database": "odoo_portfolio",
                            "accounts": {role: {"login": f"portfolio.acceptance.{role}",
                                                "password": f"PRIVATE-{role}-" + "x" * 32} for role in ROLES}}
        self.paths = {key: self.directory / f"{key}.json" for key in ("credentials", "acceptance", "flow", "output")}
        for key in ("credentials", "acceptance", "flow"):
            self.paths[key].write_text(json.dumps(getattr(self, key)))
        self.paths["credentials"].chmod(0o600)

    def load(self, **overrides):
        kwargs = {"base_url": "http://127.0.0.1:8069", "database": "odoo_portfolio",
                  "credentials_path": self.paths["credentials"],
                  "acceptance_report_path": self.paths["acceptance"],
                  "flow_report_path": self.paths["flow"], "output_path": self.paths["output"]}
        return self.module.load_inputs(**(kwargs | overrides))

    def evidence(self, role):
        return role_evidence(role, self.acceptance, self.flow)

    def test_import_has_no_authentication_credential_or_server_side_effects(self):
        with patch("acceptance_credentials._read_credentials", side_effect=AssertionError("Read during import")), \
                patch("urllib.request.build_opener", side_effect=AssertionError("Network during import")):
            spec = importlib.util.spec_from_file_location("m2c_import_only", ROOT / "scripts" / "verify_m2c_role_flow.py")
            spec.loader.exec_module(importlib.util.module_from_spec(spec))

    def test_valid_inputs_use_existing_private_credentials_without_mutating_reports(self):
        before = {key: path.read_bytes() for key, path in self.paths.items() if path.exists()}
        with patch("acceptance_credentials.ensure_credentials", side_effect=AssertionError("Must never create credentials")):
            inputs = self.load()
        self.assertEqual(inputs["database"], "odoo_portfolio")
        self.assertEqual(inputs["acceptance"], self.acceptance)
        self.assertEqual(inputs["flow"], self.flow)
        self.assertEqual(before, {key: path.read_bytes() for key, path in self.paths.items() if path.exists()})

    def test_nonlocal_targets_are_rejected_before_reading_credentials(self):
        targets = ("https://127.0.0.1:8069", "http://localhost:8069", "http://remote.example:8069",
                   "http://127.0.0.1:8069@remote.example", "http://127.0.0.1:8069/path",
                   "http://127.0.0.1:8069?password=PRIVATE", "http://127.0.0.1:0")
        with patch("acceptance_credentials._read_credentials", side_effect=AssertionError("Read before target guard")):
            for base_url in targets:
                with self.subTest(base_url=base_url), self.assertRaises(ValueError):
                    self.load(base_url=base_url)
            with self.assertRaises(ValueError):
                self.load(database="production")

    def test_missing_unsafe_or_symlink_credentials_are_rejected_without_autocreation(self):
        path = self.paths["credentials"]
        content = path.read_bytes()
        path.unlink()
        with self.assertRaises(ValueError):
            self.load()
        self.assertFalse(path.exists())
        path.write_bytes(content)
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            self.load()
        target = self.directory / "private-target.json"
        path.rename(target)
        target.chmod(0o600)
        path.symlink_to(target)
        with self.assertRaises(ValueError):
            self.load()
        self.assertEqual(target.read_bytes(), content)

    def test_input_reports_must_match_namespace_identity_models_company_actors_and_facts(self):
        mutations = (
            ("acceptance", lambda value: value.update(namespace="foreign")),
            ("acceptance", lambda value: value["identities"].pop("user_manager2")),
            ("acceptance", lambda value: value["identities"]["company_a"].update(model="res.users")),
            ("flow", lambda value: value.update(database="odoo_portfolio_m1_test")),
            ("flow", lambda value: value.update(company_id=999)),
            ("flow", lambda value: value["actors"].update(finance=999)),
            ("flow", lambda value: value["identities"]["credit_note"].update(id=True)),
            ("flow", lambda value: value["identities"]["credit_note"].update(model="sale.order")),
            ("flow", lambda value: value["physical"].update(net=91)),
            ("flow", lambda value: value["financial"].update(net_amount=8500)),
        )
        for key, mutate in mutations:
            with self.subTest(key=key, mutation=mutate):
                value = copy.deepcopy(getattr(self, key))
                mutate(value)
                self.paths[key].write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    self.load()
                self.paths[key].write_text(json.dumps(getattr(self, key)))

    def test_report_and_output_paths_cannot_be_symlinks_or_escape_project_runtime(self):
        target = self.directory / "actual-flow.json"
        self.paths["flow"].rename(target)
        self.paths["flow"].symlink_to(target)
        with self.assertRaises(ValueError):
            self.load()
        self.paths["flow"].unlink()
        target.rename(self.paths["flow"])
        for output in (ROOT / "README.md", self.directory / ".." / ".." / ".." / "unsafe.json"):
            with self.subTest(output=output), self.assertRaises(ValueError):
                self.load(output_path=output)

    def test_dotdot_input_and_output_aliases_cannot_overwrite_any_private_or_safe_input(self):
        alias_directory = self.directory / "actual-subdirectory"
        alias_directory.mkdir()
        input_keys = {"credentials": "credentials_path", "acceptance": "acceptance_report_path",
                      "flow": "flow_report_path"}
        before = {key: self.paths[key].read_bytes() for key in input_keys}
        with patch("urllib.request.build_opener", side_effect=AssertionError("Aliased outputs must fail before network")), \
                patch.object(Path, "write_text", side_effect=AssertionError("Input alias validation must never write")):
            for key, argument in input_keys.items():
                canonical = self.paths[key]
                alias = alias_directory / ".." / canonical.name
                self.assertEqual(alias.resolve(), canonical.resolve())
                for overrides in ({"output_path": alias}, {argument: alias, "output_path": canonical}):
                    with self.subTest(input=key, aliased_input=argument in overrides), self.assertRaises(ValueError):
                        self.load(**overrides)
                self.assertEqual({name: self.paths[name].read_bytes() for name in input_keys}, before)
        self.assertFalse(self.paths["output"].exists())

    def test_seven_role_report_requires_every_role_and_no_unknown_role(self):
        inputs = self.load()
        evidence = {role: self.evidence(role) for role in ROLES}
        for missing in ROLES:
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                self.module.validate_evidence({key: value for key, value in evidence.items() if key != missing}, inputs)
        with self.assertRaises(ValueError):
            self.module.validate_evidence(evidence | {"admin": self.evidence("sales")}, inputs)

    def test_actual_uid_allowed_active_and_default_companies_and_groups_are_exact(self):
        inputs = self.load()
        mutations = (lambda e: e.update(uid=999), lambda e: e.update(http_status=302),
                     lambda e: e.update(company_id=102), lambda e: e.update(allowed_company_ids=[101, 102]),
                     lambda e: e.update(active_company_ids=[102]),
                     lambda e: e["groups"].update({GROUPS["sales"]: False}),
                     lambda e: e["groups"].update({"base.group_system": True}))
        for mutate in mutations:
            evidence = self.evidence("sales")
            mutate(evidence)
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                self.module.validate_role_evidence("sales", evidence, inputs)

    def test_menu_action_model_and_both_native_view_modes_are_required(self):
        inputs = self.load()
        mutations = (lambda e: e["menus"].clear(),
                     lambda e: e["menus"]["sales"].update(xmlid="sale.menu_sale_order"),
                     lambda e: e["menus"]["sales"].update(action="ir.actions.act_window,999"),
                     lambda e: e["actions"]["sales"].update(res_model="res.users"),
                     lambda e: e["actions"]["sales"].update(view_mode="form"),
                     lambda e: e["views"].clear())
        for mutate in mutations:
            evidence = self.evidence("sales")
            mutate(evidence)
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                self.module.validate_role_evidence("sales", evidence, inputs)

    def test_finance_sales_view_cannot_expose_sales_write_or_approval_controls(self):
        inputs = self.load()
        for control in ('create="1"', 'edit="1"', '<button name="action_confirm"/>',
                        '<button name="action_portfolio_submit"/>', '<button name="action_portfolio_approve"/>'):
            evidence = self.evidence("finance")
            evidence["views"]["sale.order"]["arch"] = f"<form {control if '=' in control and '<' not in control else ''}>{control if '<' in control else ''}</form>"
            with self.subTest(control=control), self.assertRaises(ValueError):
                self.module.validate_role_evidence("finance", evidence, inputs)

    def test_nonbilling_trace_financial_fields_are_absent_from_view_schema_and_payload(self):
        inputs = self.load()
        for role in ("sales", "manager", "manager2", "buyer"):
            for location in ("view", "fields", "record", "line"):
                evidence = self.evidence(role)
                trace = evidence["trace"]
                if location == "view":
                    trace["view"]["arch"] = '<form><field name="posted_signed_amount"/></form>'
                elif location == "fields":
                    trace["fields"].append("posted_signed_amount")
                elif location == "record":
                    trace["records"][0]["posted_signed_amount"] = 7650
                else:
                    trace["records"][0]["line_ids"][0]["posted_invoice_qty"] = 100
                with self.subTest(role=role, location=location), self.assertRaises(ValueError):
                    self.module.validate_role_evidence(role, evidence, inputs)

    def test_finance_requires_actual_invoice_read_and_authoritative_trace_facts(self):
        inputs = self.load()
        mutations = (lambda e: e["documents"]["invoice_first"].update(records=[]),
                     lambda e: e["trace"]["records"][0].update(posted_signed_amount=8500),
                     lambda e: e["trace"]["records"][0].update(posted_credit_qty=0),
                     lambda e: e["trace"]["records"][0].update(net_delivered_qty=100))
        for mutate in mutations:
            evidence = self.evidence("finance")
            mutate(evidence)
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                self.module.validate_role_evidence("finance", evidence, inputs)

    def test_metadata_alone_cannot_replace_actual_source_trace_and_native_document_reads(self):
        inputs = self.load()
        for location in ("source", "trace", "documents"):
            evidence = self.evidence("buyer")
            if location == "source":
                evidence["source_records"] = []
            elif location == "trace":
                evidence["trace"]["records"] = []
            else:
                evidence["documents"]["purchase_order"]["records"] = []
            with self.subTest(location=location), self.assertRaises(ValueError):
                self.module.validate_role_evidence("buyer", evidence, inputs)

    def test_native_document_reads_must_match_flow_ids_names_company_and_state(self):
        inputs = self.load()
        for field, value in (("id", 999), ("name", "Foreign document"), ("company_id", [102, "PARTS-B"]), ("state", "draft")):
            evidence = self.evidence("warehouse")
            evidence["documents"]["delivery_first"]["records"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.module.validate_role_evidence("warehouse", evidence, inputs)

    def test_trace_lists_must_match_the_actually_readable_native_flow_documents(self):
        evidence = self.evidence("buyer")
        evidence["trace"]["records"][0]["purchase_order_ids"] = []
        with self.assertRaises(ValueError):
            self.module.validate_role_evidence("buyer", evidence, self.load())

    def test_warehouse_native_source_reads_are_preserved_while_trace_stays_denied(self):
        inputs = self.load()
        evidence = self.evidence("warehouse")
        buyer_documents = self.evidence("buyer")["documents"]
        for key in ("sale_order", "purchase_order"):
            evidence["documents"][key] = copy.deepcopy(buyer_documents[key])
        evidence["source_records"] = copy.deepcopy(evidence["documents"]["sale_order"]["records"])
        try:
            report = self.module.validate_role_evidence("warehouse", evidence, inputs)
        except ValueError:
            self.fail("Warehouse native Sales/Purchase reads must be accepted when Trace is denied")
        self.assertEqual(report["documents"]["sale_order"], self.flow["identities"]["sale_order"]["id"])
        self.assertEqual(report["documents"]["purchase_order"], self.flow["identities"]["purchase_order"]["id"])
        self.assertFalse(report["checks"]["trace_read"])
        self.assertEqual(report["facts"], {})
        for mutate in (lambda e: e.update(trace_denied=False),
                       lambda e: e["source_records"][0].update(id=999),
                       lambda e: e["documents"]["purchase_order"]["records"][0].update(company_id=[102, "PARTS-B"])):
            invalid = copy.deepcopy(evidence)
            mutate(invalid)
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                self.module.validate_role_evidence("warehouse", invalid, inputs)

    def test_other_company_denial_requires_no_a_payload_and_no_trace_snapshot_mutation(self):
        inputs = self.load()
        mutations = (lambda e: e["cross_company"].update(search_ids=[701]),
                     lambda e: e["cross_company"].update(read_denied=False),
                     lambda e: e["cross_company"].update(trace_denied=False),
                     lambda e: e["cross_company"].update(trace_count_after=1),
                     lambda e: e.update(source_records=[{"id": 701}]),
                     lambda e: e["documents"].update(delivery_first=self.evidence("warehouse")["documents"]["delivery_first"]))
        for mutate in mutations:
            evidence = self.evidence("other_sales")
            mutate(evidence)
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                self.module.validate_role_evidence("other_sales", evidence, inputs)

    def test_valid_seven_roles_produce_safe_positive_report_with_native_ids_and_facts(self):
        evidence = {role: self.evidence(role) for role in ROLES}
        report = self.module.validate_evidence(evidence, self.load())
        self.assertTrue(report["ok"])
        self.assertEqual(set(report["roles"]), set(ROLES))
        self.assertEqual(report["roles"]["finance"]["facts"]["posted_signed_amount"], 7650)
        self.assertEqual(report["roles"]["finance"]["facts"]["net_delivered_qty"], 90)
        self.assertEqual(report["roles"]["buyer"]["documents"]["purchase_order"], 702)
        serialized = json.dumps(report)
        for secret in ("PRIVATE-", "password", "cookie", "token", "session_id", "traceback", "dsn"):
            self.assertNotIn(secret, serialized.lower())

    def test_jsonrpc_transport_requires_200_and_never_returns_raw_error_payload(self):
        for status, payload in ((302, {"result": True}), (200, {"error": {"message": "PRIVATE-ERROR", "data": {"debug": "PRIVATE-TOKEN"}}})):
            response = Mock(status=status)
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            response.read.return_value = json.dumps(payload).encode()
            opener = Mock()
            opener.open.return_value = response
            client = self.module.JsonRpcClient("http://127.0.0.1:8069", "sales", opener=opener)
            with self.subTest(status=status), self.assertRaises(ValueError) as raised:
                client.rpc("/web/session/authenticate", {"password": "PRIVATE-PASSWORD"})
            self.assertNotIn("PRIVATE", str(raised.exception))
        with patch("urllib.request.build_opener", return_value=Mock()) as build:
            self.module.JsonRpcClient("http://127.0.0.1:8069", "buyer")
        handlers = build.call_args.args
        self.assertTrue(any(isinstance(getattr(handler, "cookiejar", None), http.cookiejar.CookieJar) for handler in handlers))

    def test_cli_uses_separate_authenticated_http_sessions_and_actual_web_reads(self):
        calls, sessions = [], []
        acceptance, flow = self.acceptance, self.flow

        class Response:
            status = 200

            def __init__(self, payload):
                self.payload = json.dumps(payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, *_args):
                return self.payload

        class Opener:
            def __init__(self):
                self.role = None
                self.snapshots = 0
                sessions.append(self)

            def open(self, request, timeout=None):
                if isinstance(request, str) or request.data is None:
                    return Response({})
                message = json.loads(request.data)
                params = message["params"]
                endpoint = request.full_url.split("127.0.0.1:8069", 1)[1]
                if endpoint == "/web/session/authenticate":
                    self.role = params["login"].removeprefix("portfolio.acceptance.")
                    calls.append((self.role, "authenticate", None))
                    role = self.role
                    company = acceptance["identities"]["company_b" if role == "other_sales" else "company_a"]["id"]
                    result = {"uid": acceptance["identities"][f"user_{role}"]["id"],
                              "user_context": {"allowed_company_ids": [company]},
                              "user_companies": {"current_company": company,
                                                 "allowed_companies": {str(company): {"id": company}}},
                              "cache_hashes": {"load_menus": "safe-test-hash"}}
                    return Response({"result": result})
                if self.role is None:
                    raise AssertionError("Every native read requires an authenticated role session")
                evidence = role_evidence(self.role, acceptance, flow)
                if endpoint == "/web/action/load":
                    action = params["action_id"]
                    key = str(action).removeprefix("portfolio_trade.action_portfolio_")
                    result = evidence["actions"].get(key)
                    if result is None and key == "sales":
                        result = {"id": 301, "res_model": "sale.order", "view_mode": "list,form"}
                    if result is None:
                        result = next((item for item in evidence["actions"].values() if str(item["id"]) == str(action)), None)
                    return Response({"result": result})
                model, method = params.get("model"), params.get("method")
                args, kwargs = params.get("args", []), params.get("kwargs", {})
                calls.append((self.role, model, method))
                context = kwargs.get("context", {})
                inactive = context.get("allowed_company_ids") not in (None, evidence["allowed_company_ids"])
                result = None
                error = False
                if model == "res.users" and method == "has_group":
                    if len(args) != 2 or args[0] != [evidence["uid"]] or not isinstance(args[1], str):
                        return Response({"error": {"message": "PRIVATE-native record-method arity failure"}})
                    result = evidence["groups"].get(args[1], False)
                elif model == "ir.ui.menu" and method in {"load_menus", "load_web_menus"}:
                    result = {str(item["id"]): item for item in evidence["menus"].values()}
                    result["200"] = {"id": 200, "name": "Trade Portfolio / 进销存作品集", "parent_id": False,
                                     "xmlid": "portfolio_trade.menu_portfolio_root", "action": False}
                elif method in {"has_access", "check_access_rights"}:
                    result = any(item["model"] == model and item["readable"] for item in evidence["documents"].values())
                    if model.startswith("portfolio.fulfillment.trace"):
                        result = self.role != "warehouse"
                elif method == "get_view":
                    result = evidence["trace"]["view"] if model == "portfolio.fulfillment.trace" and evidence["trace"] else \
                        evidence["views"].get(model, {"arch": '<form create="0" edit="0"><button name="action_portfolio_trace"/></form>'})
                elif method == "fields_get":
                    names = evidence["trace"]["fields"] if model.startswith("portfolio.fulfillment.trace") and evidence["trace"] else ["id", "name", "company_id", "state"]
                    result = {name: {"type": "float" if name.endswith("_qty") else "char", "string": name} for name in names}
                elif method == "search_count" and model.startswith("portfolio.fulfillment.trace"):
                    result = self.snapshots
                elif model == "sale.order" and method == "action_portfolio_trace":
                    if self.role in {"warehouse", "other_sales"} or inactive:
                        error = True
                    else:
                        self.snapshots += 1
                        result = evidence["trace"]["action"]
                elif method in {"search", "search_read", "read", "web_read"}:
                    if model.startswith("portfolio.fulfillment.trace"):
                        result = evidence["trace"]["records"] if evidence["trace"] else []
                    else:
                        items = [item["records"][0] for item in evidence["documents"].values()
                                 if item["model"] == model and item["records"]]
                        if method in {"read", "web_read"}:
                            ids = args[0]
                            items = [item for item in items if item["id"] in ids]
                            if not items or inactive:
                                error = True
                        elif self.role == "other_sales" or inactive:
                            items = []
                        result = [item["id"] for item in items] if method == "search" else items
                else:
                    raise AssertionError(f"Unexpected or mutating HTTP operation: {model}.{method}")
                return Response({"error": {"message": "PRIVATE-REMOTE-ERROR"}} if error else {"result": result})

        args = ["--base-url", "http://127.0.0.1:8069", "--database", "odoo_portfolio",
                "--credentials", str(self.paths["credentials"]), "--acceptance-report", str(self.paths["acceptance"]),
                "--flow-report", str(self.paths["flow"]), "--output", str(self.paths["output"])]
        output = io.StringIO()
        with patch("urllib.request.build_opener", side_effect=lambda *_handlers: Opener()), \
                contextlib.redirect_stdout(output):
            status = self.module.main(args)
        self.assertEqual(status, 0, output.getvalue())
        self.assertEqual(len(sessions), 7)
        self.assertEqual({role for role, model, _method in calls if model == "authenticate"}, set(ROLES))
        for role, model in (("buyer", "sale.order"), ("buyer", "purchase.order"),
                            ("warehouse", "stock.picking"), ("finance", "account.move")):
            self.assertIn((role, model, "web_read"), calls)
        self.assertTrue(json.loads(self.paths["output"].read_text())["ok"])
        self.assertNotIn("PRIVATE", output.getvalue() + self.paths["output"].read_text())

    def test_group_record_rpc_scopes_the_authenticated_user_before_group_lookup(self):
        inputs = self.load()
        uid = self.acceptance["identities"]["user_sales"]["id"]
        company = self.acceptance["identities"]["company_a"]["id"]
        group_calls = []

        class StopAfterGroups(Exception):
            pass

        class NativeBoundary:
            def rpc(self, endpoint, params):
                if endpoint != "/web/session/authenticate":
                    raise AssertionError("Unexpected non-group RPC")
                return {"uid": uid, "user_context": {"allowed_company_ids": [company]},
                        "user_companies": {"current_company": company,
                                           "allowed_companies": {str(company): {"id": company}}}}

            def call(self, model, method, args=None, kwargs=None):
                if (model, method) == ("ir.ui.menu", "load_menus"):
                    raise StopAfterGroups
                if (model, method) != ("res.users", "has_group"):
                    raise AssertionError("Unexpected non-group native method")
                if len(args) != 2 or args[0] != [uid] or not isinstance(args[1], str):
                    raise AssertionError("Native has_group requires singleton record IDs and a group external ID")
                group_calls.append(args)
                return args[1] in {"base.group_user", GROUPS["sales"]}

        with self.assertRaises(StopAfterGroups):
            self.module.collect_role_evidence(NativeBoundary(), "sales", inputs)
        self.assertEqual({args[1] for args in group_calls},
                         {"base.group_user", "base.group_system", *GROUPS.values()})

    def test_cli_reports_controlled_operation_without_remote_error_payload(self):
        args = ["--base-url", "http://127.0.0.1:8069", "--database", "odoo_portfolio",
                "--credentials", str(self.paths["credentials"]), "--acceptance-report", str(self.paths["acceptance"]),
                "--flow-report", str(self.paths["flow"]), "--output", str(self.paths["output"])]
        output = io.StringIO()
        failure = self.module.VerificationError("sales", "has_group", "PRIVATE-REMOTE-PAYLOAD")
        with patch.object(self.module, "collect_role_evidence", side_effect=failure), \
                patch("urllib.request.build_opener", return_value=Mock()), contextlib.redirect_stdout(output):
            status = self.module.main(args)
        self.assertEqual(status, 1)
        report = json.loads(output.getvalue())
        self.assertEqual(report["role"], "sales")
        self.assertEqual(report["operation"], "has_group")
        self.assertNotIn("PRIVATE", output.getvalue())
        self.assertFalse(self.paths["output"].exists())

    def test_cli_invalid_credentials_or_unknown_options_emit_only_safe_json_before_network(self):
        args = ["--base-url", "http://127.0.0.1:8069", "--database", "odoo_portfolio",
                "--credentials", str(self.paths["credentials"]), "--acceptance-report", str(self.paths["acceptance"]),
                "--flow-report", str(self.paths["flow"]), "--output", str(self.paths["output"])]
        self.paths["credentials"].chmod(0o644)
        for extra in ([], ["--password", "PRIVATE-ARGUMENT"]):
            output, errors = io.StringIO(), io.StringIO()
            with patch("urllib.request.build_opener", side_effect=AssertionError("No network for invalid input")), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                status = self.module.main(args + extra)
            self.assertNotEqual(status, 0)
            self.assertFalse(json.loads(output.getvalue())["ok"])
            self.assertEqual(errors.getvalue(), "")
            self.assertNotIn("PRIVATE", output.getvalue())
            self.assertFalse(self.paths["output"].exists())
