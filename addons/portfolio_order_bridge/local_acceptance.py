"""Explicit, bounded local bridge profile; importing this module never seeds data.

This is a plain Python maintenance helper, with no model/RPC or install-hook
entry. The caller owns the transaction and may commit its successful report.
"""

from odoo import SUPERUSER_ID, Command
from odoo.addons.portfolio_trade.acceptance_data import (
    EXPECTED_IDENTITIES as M1_IDENTITIES,
)
from odoo.addons.portfolio_trade.acceptance_data import _validate_local_target
from odoo.addons.portfolio_trade.local_finance import (
    EXPECTED_FINANCE,
    _acceptance_preflight,
    _records_for_namespace,
    _validate_finance,
)
from odoo.exceptions import AccessError, UserError
from odoo.tools import LazyTranslate

from .local_flow_contract import NAMESPACE as FLOW_NAMESPACE
from .local_flow_contract import validate_completed as _validate_completed_flow
from .models.access import FORBIDDEN_GROUPS, INTEGRATION, OPERATOR, SALES

NAMESPACE = "portfolio_order_bridge_acceptance"
ROLES = ("settings", "operator", "integration")
SOURCE = "portfolio.import.source"
CUSTOMER_MAP = "portfolio.import.customer.map"
PRODUCT_MAP = "portfolio.import.product.map"
EXPECTED_IDENTITIES = {
    **{f"user_{role}": "res.users" for role in ROLES},
    "customer": "res.partner", "vendor": "res.partner",
    **{f"product_item{index}": "product.product" for index in (1, 2)},
    **{f"template_item{index}": "product.template" for index in (1, 2)},
    **{f"supplier_item{index}": "product.supplierinfo" for index in (1, 2)},
    "source": SOURCE, "customer_map": CUSTOMER_MAP,
    **{f"product_map_item{index}": PRODUCT_MAP for index in (1, 2)},
    "processing_cron": "ir.cron", "processing_server_action": "ir.actions.server",
}
_LOCK = 762714394
_lt = LazyTranslate(__name__, default_lang="en_US")
_SOURCE_NAME = "Portfolio bridge acceptance source"
_SOURCE_CODE = "B2B-ACCEPTANCE"
_COMPANY_CODE = "PARTS-A"
_PARTNERS = {
    "customer": ("Portfolio bridge acceptance customer", "BRIDGE-ACCEPTANCE-CUSTOMER"),
    "vendor": ("Portfolio bridge acceptance vendor", "BRIDGE-ACCEPTANCE-VENDOR"),
}
_ITEMS = {
    1: ("Portfolio bridge acceptance item 1", "BRIDGE-ACCEPTANCE-1", 120.0, 60.0),
    2: ("Portfolio bridge acceptance item 2", "BRIDGE-ACCEPTANCE-2", 35.0, 15.0),
}
_FIELDS = {
    SOURCE: ("name", "active", "company_id", "company_code", "code", "pricelist_id",
             "warehouse_id", "sales_user_id", "processor_user_id", "processing_cron_id", "ingress_revision"),
    CUSTOMER_MAP: ("active", "source_id", "company_id", "external_ref", "partner_id"),
    PRODUCT_MAP: ("active", "source_id", "company_id", "external_sku", "product_id"),
    "portfolio.import.job": ("source_id", "company_id", "state", "attempt_count", "sale_order_id"),
    "portfolio.import.attempt": ("job_id", "company_id", "attempt_no", "event_type", "actor_id"),
    "portfolio.import.retry": ("job_id", "reason"),
}
_VIEWS = {
    "view_import_source_list": (SOURCE, "list"),
    "view_import_source_form": (SOURCE, "form"),
    "view_import_customer_map_list": (CUSTOMER_MAP, "list"),
    "view_import_customer_map_form": (CUSTOMER_MAP, "form"),
    "view_import_product_map_list": (PRODUCT_MAP, "list"),
    "view_import_product_map_form": (PRODUCT_MAP, "form"),
    "view_import_job_list": ("portfolio.import.job", "list"),
    "view_import_job_form": ("portfolio.import.job", "form"),
    "view_import_attempt_list": ("portfolio.import.attempt", "list"),
    "view_import_attempt_form": ("portfolio.import.attempt", "form"),
    "view_import_retry_form": ("portfolio.import.retry", "form"),
}


def _conflict(message):
    raise UserError(_lt("Local bridge profile conflict: %s", message))


def _passwords(passwords):
    if (
        not isinstance(passwords, dict) or set(passwords) != set(ROLES)
        or any(not isinstance(value, str) or len(value) < 24 or not value.strip()
               for value in passwords.values())
        or len(set(passwords.values())) != len(ROLES)
    ):
        _conflict("supply three distinct private role passwords of at least24 characters.")


def _installed_readiness(env):
    module = env["ir.module.module"].sudo().search([("name", "=", "portfolio_order_bridge")])
    if (
        len(module) != 1 or module.state != "installed" or not module.installed_version
        or not module.installed_version.startswith("18.0.")
        or module.latest_version != module.installed_version
    ):
        _conflict("the installed Odoo18 bridge must match its source version; upgrade the addon first.")
    markers = env["ir.model.data"].sudo()
    for model, names in _FIELDS.items():
        if model not in env.registry.models or any(name not in env[model]._fields for name in names):
            _conflict("a required bridge model or field is unavailable.")
        marker = markers.search([
            ("module", "=", "portfolio_order_bridge"),
            ("name", "=", f"model_{model.replace('.', '_')}"),
        ])
        metadata = env["ir.model"].browse(marker.res_id).exists() if len(marker) == 1 and marker.model == "ir.model" else env["ir.model"]
        if not metadata or metadata.model != model or metadata.state != "base":
            _conflict("installed bridge model metadata is unavailable.")
    for model, methods in {
        SOURCE: ("action_prepare_processing_cron", "_ui_validate_processing_cron"),
        "portfolio.import.job": ("submit_order", "_process_pending", "action_retry", "action_process_pending"),
        "portfolio.import.retry": ("action_apply",),
    }.items():
        if any(not callable(getattr(env[model], method, None)) for method in methods):
            _conflict("a required bridge service is unavailable.")
    for name, (model, view_type) in _VIEWS.items():
        marker = markers.search([("module", "=", "portfolio_order_bridge"), ("name", "=", name)])
        view = env["ir.ui.view"].browse(marker.res_id).exists() if len(marker) == 1 and marker.model == "ir.ui.view" else env["ir.ui.view"]
        if not view or not view.active or view.model != model or view.type != view_type:
            _conflict("a required installed bridge view is unavailable.")
    tables = [env[model]._table for model in _FIELDS]
    env.cr.execute(
        "SELECT count(*) FROM pg_class WHERE oid IN "
        "(SELECT to_regclass(name) FROM unnest(%s::text[]) AS name) AND relkind IN ('r', 'p')",
        [tables],
    )
    if env.cr.fetchone()[0] != len(tables):
        _conflict("required bridge tables are unavailable; upgrade the addon first.")
    for model, names in _FIELDS.items():
        env.cr.execute(
            "SELECT attname FROM pg_attribute WHERE attrelid = to_regclass(%s) "
            "AND attname = ANY(%s) AND attnum > 0 AND NOT attisdropped",
            [env[model]._table, list(names)],
        )
        if {row[0] for row in env.cr.fetchall()} != set(names):
            _conflict("required persisted bridge fields are unavailable; upgrade the addon first.")
    membership = env[SOURCE]._fields.get("integration_user_ids")
    if membership is None or membership.type != "many2many" or membership.comodel_name != "res.users":
        _conflict("the installed source integration membership is unavailable.")
    env.cr.execute("SELECT to_regclass(%s) IS NOT NULL", [membership.relation])
    if not env.cr.fetchone()[0]:
        _conflict("the installed source integration membership table is unavailable.")


def _namespace(env):
    markers = env["ir.model.data"].sudo().search([("module", "=", NAMESPACE)])
    if not markers:
        return {}
    if (
        len(markers) != len(EXPECTED_IDENTITIES)
        or set(markers.mapped("name")) != set(EXPECTED_IDENTITIES)
        or not all(markers.mapped("noupdate"))
    ):
        _conflict("the owned namespace is incomplete, altered or contains unexpected identities.")
    records = {}
    for marker in markers:
        if marker.model != EXPECTED_IDENTITIES[marker.name]:
            _conflict("an owned identity has the wrong model.")
        # exists() is a private native identity check. All public bridge reads
        # and configuration actions below use the actual owned Settings actor.
        record = env[marker.model].with_context(active_test=False).browse(marker.res_id).exists()
        if not record:
            _conflict("an owned identity is stale.")
        records[marker.name] = record
    if len({(record._name, record.id) for record in records.values()}) != len(records):
        _conflict("multiple owned identities point to the same record.")
    for record in records.values():
        foreign = env["ir.model.data"].sudo().search_count([
            ("model", "=", record._name), ("res_id", "=", record.id), ("module", "!=", NAMESPACE),
        ])
        if foreign:
            _conflict("an owned target also belongs to another namespace.")
    return records


def _groups(env, role):
    groups = env.ref("base.group_user")
    for xmlid in {
        "settings": ("base.group_system",), "operator": (OPERATOR, SALES),
        "integration": (INTEGRATION,),
    }[role]:
        groups |= env.ref(xmlid)
    return groups


def _validate_users(env, records, company, passwords):
    for role in ROLES:
        user = records[f"user_{role}"]
        groups = _groups(env, role)
        required = (groups | groups.trans_implied_ids) - env.ref("base.group_multi_company")
        # Native mail preference inversion adds this technical inbox group.
        required |= env.ref("mail.group_mail_notification_type_inbox")
        if (
            user.id == 1 or not user.active or user.share
            or user.name != f"Portfolio bridge acceptance {role}"
            or user.login != f"portfolio.bridge.acceptance.{role}"
            or user.email != f"bridge-{role}@example.invalid" or user.notification_type != "inbox"
            or user.lang != "en_US" or user.company_id != company or user.company_ids != company
            or set(user.groups_id.ids) != set(required.ids)
        ):
            _conflict("an owned actor's identity, company or permissions were modified.")
        if role != "settings" and any(user.has_group(group) for group in FORBIDDEN_GROUPS):
            _conflict("an owned business actor has excess permissions.")
    # Bounded, internal password-hash read. verify() never resets a password or
    # silently upgrades its hash, unlike native authentication's update path.
    users = [records[f"user_{role}"].id for role in ROLES]
    env.cr.execute("SELECT id, COALESCE(password, '') FROM res_users WHERE id = ANY(%s)", [users])
    hashes = dict(env.cr.fetchall())
    for role in ROLES:
        try:
            valid = env["res.users"]._crypt_context().verify(passwords[role], hashes[records[f"user_{role}"].id])
        except (KeyError, ValueError, TypeError):
            valid = False
        if not valid:
            _conflict("existing actor credentials do not match the supplied private profile.")


def _native_conflicts(env, records):
    domains = []
    for role in ROLES:
        domains.append((f"user_{role}", "res.users", [
            "|", ("login", "=", f"portfolio.bridge.acceptance.{role}"),
            ("name", "=", f"Portfolio bridge acceptance {role}"),
        ]))
    for key, (name, ref) in _PARTNERS.items():
        domains.append((key, "res.partner", ["|", ("name", "=", name), ("ref", "=", ref)]))
    for index, (name, sku, _price, _cost) in _ITEMS.items():
        for key, model in ((f"product_item{index}", "product.product"),
                           (f"template_item{index}", "product.template")):
            domains.append((key, model, ["|", ("name", "=", name), ("default_code", "=", sku)]))
    for key, model, domain in domains:
        native = env[model].sudo().with_context(active_test=False)
        if native.search(domain) - records.get(key, native.browse()):
            _conflict("a fixed user, partner or product identity exists outside this namespace.")
    # A yes/no, bounded existence check also detects global source-key conflicts
    # hidden by another company's record rule. It returns no business rows.
    env.cr.execute(
        "SELECT EXISTS (SELECT 1 FROM portfolio_import_source WHERE "
        "(name = %s OR (company_code = %s AND code = %s) OR (company_id = %s AND code = %s)) "
        "AND id != %s)",
        [_SOURCE_NAME, _COMPANY_CODE, _SOURCE_CODE, env.company.id, _SOURCE_CODE,
         records["source"].id if records else 0],
    )
    if env.cr.fetchone()[0]:
        _conflict("the fixed source identity exists outside this namespace.")


def _settings_env(env, records, company):
    return env["res.users"].with_user(records["user_settings"]).with_context(
        allowed_company_ids=company.ids, lang="en_US",
    ).env


def _bridge_conflicts(settings, records):
    sources = settings[SOURCE].with_context(active_test=False)
    matches = sources.search([
        "|", "|", ("name", "=", _SOURCE_NAME),
        "&", ("company_code", "=", _COMPANY_CODE), ("code", "=", _SOURCE_CODE),
        ("code", "=", _SOURCE_CODE),
    ])
    if matches - sources.browse(records["source"].id if "source" in records else []):
        _conflict("an existing source cannot be adopted by the local profile.")


def _validate_catalog(env, records, company):
    for key, (name, ref) in _PARTNERS.items():
        partner = records[key]
        if (
            not partner.active or partner.name != name or partner.ref != ref or not partner.is_company
            or partner.company_id != company or partner.parent_id
            or partner.email != f"bridge-{key}@example.invalid"
            or partner.customer_rank != int(key == "customer")
            or partner.supplier_rank != int(key == "vendor")
        ):
            _conflict("an owned partner was modified.")
    unit = env.ref("uom.product_uom_unit")
    if not unit.active:
        _conflict("the native unit of measure is unavailable.")
    for index, (name, sku, price, cost) in _ITEMS.items():
        product, template = records[f"product_item{index}"], records[f"template_item{index}"]
        if (
            not product.active or not template.active or product.product_tmpl_id != template
            or template.product_variant_ids != product or product.name != name or product.default_code != sku
            or product.company_id != company or template.company_id != company
            or product.type != "consu" or not product.is_storable or product.tracking != "none"
            or product.invoice_policy != "delivery" or product.uom_id != unit or product.uom_po_id != unit
            or product.taxes_id or product.supplier_taxes_id
            or product.list_price != price or product.with_company(company).standard_price != cost
            or not product.sale_ok or not product.purchase_ok
        ):
            _conflict("an owned product or template was modified.")
        supplier = records[f"supplier_item{index}"]
        if (
            supplier.product_id != product or supplier.product_tmpl_id != template
            or supplier.company_id != company or supplier.partner_id != records["vendor"]
            or supplier.currency_id != company.currency_id or supplier.price != cost
            or supplier.min_qty != 0 or supplier.date_start or supplier.date_end
            or product.seller_ids != supplier
        ):
            _conflict("an owned supplier price was modified or duplicated.")


def _validate_bridge(settings, records, acceptance, finance):
    source = settings[SOURCE].browse(records["source"].id)
    if not source.search([("id", "=", source.id)]):
        _conflict("the owned source is unavailable to its Settings actor.")
    if (
        not source.active or source.name != _SOURCE_NAME
        or (source.company_code, source.code) != (_COMPANY_CODE, _SOURCE_CODE)
        or source.company_id != acceptance["company_a"]
        or source.warehouse_id != acceptance["warehouse_a"]
        or source.pricelist_id != finance["pricelist_cny"]
        or source.sales_user_id != acceptance["user_sales"]
        or source.processor_user_id != records["user_operator"]
        or source.integration_user_ids != records["user_integration"]
        or source.create_uid != records["user_settings"]
    ):
        _conflict("the owned source configuration was modified.")
    source._validate_configuration()
    # A completed flow owns exactly one additional fixed repair mapping. Its
    # complete immutable identity/audit contract is read-only and independent
    # of prepare_local_import, so this cannot recurse into preparation.
    repair = None
    if settings["ir.model.data"].sudo().search_count([("module", "=", FLOW_NAMESPACE)]):
        repair = _validate_completed_flow(settings, records)["product_map_repair"]
    expected_maps = {CUSTOMER_MAP: ["customer_map"], PRODUCT_MAP: [f"product_map_item{index}" for index in (1, 2)]}
    for model, keys in expected_maps.items():
        maps = settings[model].with_context(active_test=False).search([("source_id", "=", source.id)])
        expected_ids = {records[key].id for key in keys}
        if model == PRODUCT_MAP and repair:
            expected_ids.add(repair.id)
        if set(maps.ids) != expected_ids:
            _conflict("the owned source mappings are missing or contain unexpected rows.")
        for key in keys:
            mapping = settings[model].browse(records[key].id)
            target = records["customer"] if model == CUSTOMER_MAP else records[key.replace("product_map", "product")]
            external = _PARTNERS["customer"][1] if model == CUSTOMER_MAP else _ITEMS[int(key[-1])][1]
            if (
                not mapping.active or mapping.source_id != source or mapping.company_id != source.company_id
                or mapping[mapping._target_field] != target or mapping[mapping._external_key] != external
                or mapping.create_uid != records["user_settings"]
            ):
                _conflict("an owned mapping was modified.")
            mapping._validate_target()
    cron = settings["ir.cron"].with_context(active_test=False).browse(records["processing_cron"].id)
    if (
        source.processing_cron_id != cron or cron.active
        or cron.ir_actions_server_id.id != records["processing_server_action"].id
        or cron.name != f"Order import / 订单导入: {_SOURCE_NAME}"
        or cron.create_uid != records["user_settings"]
    ):
        _conflict("the owned inactive schedule or server action was changed.")
    source._ui_validate_processing_cron(cron)
    settings.cr.execute(
        "SELECT EXISTS (SELECT 1 FROM portfolio_import_source "
        "WHERE processing_cron_id = %s AND id != %s)", [cron.id, source.id],
    )
    if settings.cr.fetchone()[0]:
        _conflict("the owned schedule is also linked to another source.")


def _own(env, key, record):
    env["ir.model.data"].create({
        "module": NAMESPACE, "name": key, "model": record._name,
        "res_id": record.id, "noupdate": True,
    })
    return record


def _create_users(env, company, passwords):
    return {
        f"user_{role}": _own(env, f"user_{role}", env["res.users"].create({
            "name": f"Portfolio bridge acceptance {role}", "login": f"portfolio.bridge.acceptance.{role}",
            "password": passwords[role], "active": True,
            "company_id": company.id, "company_ids": [Command.set(company.ids)],
            "groups_id": [Command.set(_groups(env, role).ids)],
            "email": f"bridge-{role}@example.invalid", "notification_type": "inbox", "lang": "en_US",
        })) for role in ROLES
    }


def _create_catalog(env, records, company):
    for key, (name, ref) in _PARTNERS.items():
        records[key] = _own(env, key, env["res.partner"].create({
            "name": name, "ref": ref, "active": True, "is_company": True,
            "company_id": company.id, "email": f"bridge-{key}@example.invalid",
            "customer_rank": int(key == "customer"), "supplier_rank": int(key == "vendor"),
        }))
    unit = env.ref("uom.product_uom_unit")
    for index, (name, sku, price, cost) in _ITEMS.items():
        product = _own(env, f"product_item{index}", env["product.product"].create({
            "name": name, "default_code": sku, "active": True, "company_id": company.id,
            "type": "consu", "is_storable": True, "invoice_policy": "delivery", "tracking": "none",
            "uom_id": unit.id, "uom_po_id": unit.id, "list_price": price, "standard_price": cost,
            "sale_ok": True, "purchase_ok": True,
            "taxes_id": [Command.clear()], "supplier_taxes_id": [Command.clear()],
        }))
        records[f"product_item{index}"] = product
        records[f"template_item{index}"] = _own(env, f"template_item{index}", product.product_tmpl_id)
        records[f"supplier_item{index}"] = _own(env, f"supplier_item{index}", env["product.supplierinfo"].create({
            "product_id": product.id, "product_tmpl_id": product.product_tmpl_id.id,
            "partner_id": records["vendor"].id, "company_id": company.id,
            "currency_id": company.currency_id.id, "price": cost, "min_qty": 0,
            "date_start": False, "date_end": False,
        }))


def _create_bridge(env, settings, records, acceptance, finance):
    source = _own(env, "source", settings[SOURCE].create({
        "name": _SOURCE_NAME, "active": True, "company_id": acceptance["company_a"].id,
        "company_code": _COMPANY_CODE, "code": _SOURCE_CODE,
        "warehouse_id": acceptance["warehouse_a"].id, "pricelist_id": finance["pricelist_cny"].id,
        "sales_user_id": acceptance["user_sales"].id, "processor_user_id": records["user_operator"].id,
        "integration_user_ids": [Command.set(records["user_integration"].ids)],
    }))
    records["source"] = source
    records["customer_map"] = _own(env, "customer_map", settings[CUSTOMER_MAP].create({
        "source_id": source.id, "external_ref": _PARTNERS["customer"][1], "partner_id": records["customer"].id,
    }))
    for index, (_name, sku, _price, _cost) in _ITEMS.items():
        records[f"product_map_item{index}"] = _own(env, f"product_map_item{index}", settings[PRODUCT_MAP].create({
            "source_id": source.id, "external_sku": sku, "product_id": records[f"product_item{index}"].id,
        }))
    source.action_prepare_processing_cron()
    records["processing_cron"] = _own(env, "processing_cron", source.processing_cron_id)
    records["processing_server_action"] = _own(env, "processing_server_action", source.processing_cron_id.ir_actions_server_id)


def _prepare(env, actor, passwords):
    _installed_readiness(env)
    acceptance = _acceptance_preflight(env)
    company = acceptance["company_a"]
    if company.id not in actor.company_ids.ids:
        raise AccessError(env._("The existing local company is outside the maintainer's actual companies."))
    scope = env.context.get("allowed_company_ids")
    if scope is not None and (
        not isinstance(scope, (list, tuple)) or company.id not in scope
        or any(type(value) is not int or value not in actor.company_ids.ids for value in scope)
    ):
        raise AccessError(env._("The existing local company must be active in the maintainer's actual scope."))
    if not acceptance["user_sales"].has_group(SALES) or acceptance["user_sales"].share:
        _conflict("the existing M1 Sales owner is invalid.")
    # Drop caller defaults, sudo flags and unrelated business options.
    env = env(context={
        "allowed_company_ids": company.ids, "lang": "en_US", "no_reset_password": True,
        "tracking_disable": True, "mail_create_nolog": True, "mail_create_nosubscribe": True,
    })
    finance = _records_for_namespace(env, "portfolio_trade_finance", EXPECTED_FINANCE)
    _validate_finance(env, acceptance, finance)
    if not env.ref("uom.product_uom_unit").active:
        _conflict("the native product unit must already be active.")
    records = _namespace(env)
    _native_conflicts(env, records)
    if records:
        _validate_users(env, records, company, passwords)
        settings = _settings_env(env, records, company)
        _bridge_conflicts(settings, records)
    else:
        records = _create_users(env, company, passwords)
        _validate_users(env, records, company, passwords)
        settings = _settings_env(env, records, company)
        _bridge_conflicts(settings, records)
        _create_catalog(env, records, company)
        _create_bridge(env, settings, records, acceptance, finance)
        records = _namespace(env)
    _validate_catalog(env, records, company)
    _validate_bridge(settings, records, acceptance, finance)
    return {
        "schema_version": 1, "database": env.cr.dbname, "namespace": NAMESPACE,
        "company_id": company.id, "source_id": records["source"].id,
        "identities": {key: {"model": record._name, "id": record.id} for key, record in sorted(records.items())},
        "cron_active": False,
        "borrowed_m1_identities": {
            key: {"model": M1_IDENTITIES[key], "id": acceptance[key].id} for key in sorted(M1_IDENTITIES)
        },
        "borrowed_finance_pricelist_id": finance["pricelist_cny"].id,
    }


def prepare_local_import(env, passwords_by_role, allow_local=False):
    """Validate/create only the static local profile; never commit or run jobs."""
    if allow_local is not True:
        raise UserError(env._("Local bridge preparation requires explicit local opt-in."))
    _validate_local_target(env)
    actor = env["res.users"].with_user(env.uid).env.user
    bootstrap = env.uid == SUPERUSER_ID and env.su
    if (not bootstrap and not actor.active) or not actor.has_group("base.group_system"):
        raise AccessError(env._("Only actual Settings administrators can prepare the local bridge."))
    _passwords(passwords_by_role)
    with env.cr.savepoint():
        env.cr.execute("SELECT pg_advisory_xact_lock(%s)", [_LOCK])
        return _prepare(env, actor, passwords_by_role)
