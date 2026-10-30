"""Explicit minimal CNY accounting for the marked, local acceptance fixture.

This plain module has no installation hook or public model method. It does not
load a chart template or represent a legal or tax localization.
"""

from collections import Counter

from odoo import Command, _
from odoo.exceptions import AccessError, UserError

from .acceptance_data import EXPECTED_IDENTITIES, ROLE_GROUPS, _validate_local_target

ACCEPTANCE_NAMESPACE = "portfolio_trade_acceptance"
NAMESPACE = "portfolio_trade_finance"
ACCOUNTS = {
    "account_income": ("PF4100", "Portfolio local acceptance income", "income"),
    "account_expense": ("PF5100", "Portfolio local acceptance expense", "expense"),
    "account_receivable": ("PF1100", "Portfolio local acceptance receivable", "asset_receivable"),
    "account_payable": ("PF2100", "Portfolio local acceptance payable", "liability_payable"),
}
EXPECTED_FINANCE = {
    **dict.fromkeys(ACCOUNTS, "account.account"),
    "journal_sale": "account.journal", "pricelist_cny": "product.pricelist",
}
JOURNAL_CODE = "PFSAL"
JOURNAL_NAME = "Portfolio local acceptance sales"
PRICELIST_NAME = "Portfolio local acceptance CNY"
_LOCK = 762714392


def _conflict(message):
    raise UserError(_("Local finance profile conflict: %s", message))


def _records_for_namespace(env, namespace, expected, *, optional=False):
    markers = env["ir.model.data"].sudo().search([("module", "=", namespace)])
    if optional and not markers:
        return {}
    if len(markers) != len(expected) or set(markers.mapped("name")) != set(expected):
        _conflict("the namespace is missing, incomplete or contains unexpected identities.")
    records = {}
    for marker in markers:
        if marker.model != expected[marker.name]:
            _conflict("an identity has an inconsistent model.")
        record = env[marker.model].sudo().with_context(active_test=False).browse(marker.res_id).exists()
        if not record:
            _conflict("an identity refers to a missing record.")
        records[marker.name] = record
    if len({(record._name, record.id) for record in records.values()}) != len(records):
        _conflict("multiple identities refer to the same record.")
    return records


def _acceptance_preflight(env):
    records = _records_for_namespace(env, ACCEPTANCE_NAMESPACE, EXPECTED_IDENTITIES)
    company, other = records["company_a"], records["company_b"]
    if company == other or not company.active or not other.active:
        _conflict("the acceptance companies are invalid or inactive.")
    cny = env.ref("base.CNY")
    if company.currency_id != cny or not cny.active:
        _conflict("PARTS-A requires its existing active CNY currency.")
    warehouse = records["warehouse_a"]
    if warehouse.company_id != company:
        _conflict("the acceptance warehouse has a foreign owner.")
    for key in ("customer", "vendor"):
        if records[key].company_id != company or not records[key].active:
            _conflict("an acceptance partner has a foreign owner or is inactive.")
    for role in ROLE_GROUPS:
        user, owner = records[f"user_{role}"], other if role == "other_sales" else company
        if not user.active or user.company_id != owner or user.company_ids != owner:
            _conflict("an acceptance user has inconsistent company ownership.")
    for key in ("bearing", "sensor", "bolt"):
        product, supplier = records[f"product_{key}"], records[f"supplier_{key}"]
        if product.company_id != company or product.product_tmpl_id.company_id != company or not product.active:
            _conflict("an acceptance product has a foreign owner or is inactive.")
        if (
            supplier.company_id != company or supplier.product_id != product
            or supplier.product_tmpl_id != product.product_tmpl_id
            or supplier.partner_id != records["vendor"] or supplier.currency_id != cny
        ):
            _conflict("an acceptance supplier identity has inconsistent ownership.")
    move = records["initial_bearing_move"]
    if (
        not move.is_inventory or move.state != "done" or move.quantity != 40
        or move.product_id != records["product_bearing"] or move.company_id != company
        or move.location_id.usage != "inventory"
        or move.location_id.company_id not in (env["res.company"], company)
        or move.location_dest_id != warehouse.lot_stock_id
        or move.location_dest_id.usage != "internal" or move.location_dest_id.company_id != company
    ):
        _conflict("the original completed 40-unit acceptance inventory evidence is invalid.")
    return records


def _bindings(acceptance):
    yield acceptance["customer"], "property_account_receivable_id", "account_receivable"
    yield acceptance["vendor"], "property_account_payable_id", "account_payable"
    for key in ("bearing", "sensor", "bolt"):
        template = acceptance[f"product_{key}"].product_tmpl_id
        yield template, "property_account_income_id", "account_income"
        yield template, "property_account_expense_id", "account_expense"


def _validate_bindings(acceptance, finance):
    company = acceptance["company_a"]
    for record, field, key in _bindings(acceptance):
        current = record.with_company(company)[field]
        if (finance and current != finance[key]) or (not finance and current):
            _conflict("an acceptance account property is already set or inconsistent; it cannot be overwritten.")
    for key in ("bearing", "sensor", "bolt"):
        category = acceptance[f"product_{key}"].product_tmpl_id.categ_id.with_company(company)
        for field, account_key in (("property_account_income_categ_id", "account_income"),
                                   ("property_account_expense_categ_id", "account_expense")):
            if category[field] and category[field] != finance.get(account_key):
                _conflict("an unknown category account property cannot be overridden by this profile.")


def _validate_finance(env, acceptance, finance):
    company, currency = acceptance["company_a"], acceptance["company_a"].currency_id
    for key, (code, name, account_type) in ACCOUNTS.items():
        account = finance[key].with_company(company)
        if (
            account.code != code or account.name != name or account.account_type != account_type
            or account.company_ids != company or account.deprecated or account.currency_id or account.tax_ids
            or account.reconcile != (account_type in {"asset_receivable", "liability_payable"})
        ):
            _conflict("a marked account has altered code, name, type, company, tax, currency or reconciliation.")
    journal, pricelist = finance["journal_sale"], finance["pricelist_cny"]
    if (
        journal.name != JOURNAL_NAME or journal.code != JOURNAL_CODE or journal.type != "sale"
        or journal.company_id != company or journal.currency_id != currency
        or journal.default_account_id != finance["account_income"] or not journal.active
    ):
        _conflict("the marked sale journal is inconsistent.")
    if (
        pricelist.name != PRICELIST_NAME or pricelist.company_id != company
        or pricelist.currency_id != currency or not pricelist.active
    ):
        _conflict("the marked CNY pricelist is inconsistent.")
    _validate_bindings(acceptance, finance)


def _validate_outside_conflicts(env, acceptance, finance):
    # Include inactive records and all companies when looking for these fixed
    # names/codes. Elevation here only reads possible ownership conflicts.
    accounts = env["account.account"].sudo().with_context(active_test=False)
    companies = env["res.company"].sudo().with_context(active_test=False).search([])
    for key, (code, name, _account_type) in ACCOUNTS.items():
        matches = accounts.search([("name", "=", name)])
        # Odoo18 code is company-dependent. Search every existing company
        # context so a foreign company's code is not hidden by PARTS-A's value.
        for company in companies:
            matches |= accounts.with_company(company).search([("code", "=", code)])
        if matches - finance.get(key, accounts.browse()):
            _conflict("an account code or name exists outside this namespace.")
    for key, model, domain in (
        ("journal_sale", "account.journal", ["|", ("code", "=", JOURNAL_CODE), ("name", "=", JOURNAL_NAME)]),
        ("pricelist_cny", "product.pricelist", [("name", "=", PRICELIST_NAME)]),
    ):
        records = env[model].sudo().with_context(active_test=False)
        if records.search(domain) - finance.get(key, records.browse()):
            _conflict("a journal or pricelist code/name exists outside this namespace.")


def _own(env, key, record):
    env["ir.model.data"].create({
        "module": NAMESPACE, "name": key, "model": record._name, "res_id": record.id, "noupdate": True,
    })
    return record


def _create_finance(env, acceptance):
    company = acceptance["company_a"]
    finance = {}
    for key, (code, name, account_type) in ACCOUNTS.items():
        finance[key] = _own(env, key, env["account.account"].create({
            "code": code, "name": name, "account_type": account_type,
            "company_ids": [Command.set(company.ids)], "deprecated": False,
            "currency_id": False, "tax_ids": [Command.clear()],
            "reconcile": account_type in {"asset_receivable", "liability_payable"},
        }))
    finance["journal_sale"] = _own(env, "journal_sale", env["account.journal"].create({
        "name": JOURNAL_NAME, "code": JOURNAL_CODE, "type": "sale", "active": True,
        "company_id": company.id, "currency_id": company.currency_id.id,
        "default_account_id": finance["account_income"].id, "alias_name": False,
    }))
    finance["pricelist_cny"] = _own(env, "pricelist_cny", env["product.pricelist"].create({
        "name": PRICELIST_NAME, "company_id": company.id,
        "currency_id": company.currency_id.id, "active": True,
    }))
    for record, field, key in _bindings(acceptance):
        record.with_env(env).write({field: finance[key].id})
    return finance


def _prepare(env, *, check_only, enable_fulfillment):
    module = env["ir.module.module"].sudo().search([("name", "=", "portfolio_trade")])
    if len(module) != 1 or module.state != "installed" or not module.installed_version.startswith("18.0."):
        _conflict("the Odoo18 portfolio_trade addon must be installed.")
    if (
        not callable(getattr(env["sale.order"], "action_portfolio_create_invoice", None))
        or not callable(getattr(env["sale.order"], "action_portfolio_trace", None))
        or "portfolio.fulfillment.trace" not in env.registry.models
        or "portfolio.partial.credit" not in env.registry.models
    ):
        _conflict("the installed addon is missing its finance or fulfillment trace APIs.")
    acceptance = _acceptance_preflight(env)
    company, other = acceptance["company_a"], acceptance["company_b"]
    actor = env["res.users"].with_user(env.uid).env.user
    if company.id not in actor.company_ids.ids:
        raise AccessError(_("The local finance company is outside your actual allowed companies."))
    if company.id not in actor.env.companies.ids:
        raise AccessError(_("The local finance company is outside your active companies."))
    # Only the marked A records receive writes. Discard caller defaults and
    # presentation contexts after the actual actor/company checks above.
    scoped = env(context={
        "allowed_company_ids": company.ids, "tracking_disable": True,
        "mail_create_nolog": True, "mail_create_nosubscribe": True,
    })
    finance = _records_for_namespace(scoped, NAMESPACE, EXPECTED_FINANCE, optional=True)
    if not finance and company.portfolio_fulfillment_enabled:
        _conflict("an enabled company requires an already complete finance profile.")
    _validate_outside_conflicts(scoped, acceptance, finance)
    if finance:
        _validate_finance(scoped, acceptance, finance)
    else:
        _validate_bindings(acceptance, {})
    before_other = (other.portfolio_fulfillment_enabled, other.portfolio_approval_policy_version, other.portfolio_stock_revision)
    created = 0
    if not finance and not check_only:
        finance = _create_finance(scoped, acceptance)
        created = len(finance)
    if finance:
        # Re-read all identities and validate every postcondition before the
        # optional policy write; a late exception rolls back this entire frame.
        finance = _records_for_namespace(scoped, NAMESPACE, EXPECTED_FINANCE)
        _validate_finance(scoped, acceptance, finance)
        _acceptance_preflight(scoped)
        if enable_fulfillment and not company.portfolio_fulfillment_enabled:
            company.with_env(scoped).write({"portfolio_fulfillment_enabled": True})
    if before_other != (other.portfolio_fulfillment_enabled, other.portfolio_approval_policy_version, other.portfolio_stock_revision):
        _conflict("PARTS-B policy changed during preparation.")
    return {
        "database": env.cr.dbname, "module_version": module.installed_version,
        "installed_module_version": module.latest_version, "namespace": NAMESPACE,
        "company_id": company.id, "other_company_id": other.id, "currency": company.currency_id.name,
        "ready": bool(finance), "would_create": [] if finance else sorted(EXPECTED_FINANCE),
        "identities": {key: {"model": record._name, "id": record.id} for key, record in sorted(finance.items())},
        "record_counts": dict(sorted(Counter(record._name for record in finance.values()).items())),
        "created_count": created, "reused_count": len(finance) - created,
        "fulfillment_enabled": company.portfolio_fulfillment_enabled,
        "other_fulfillment_enabled": other.portfolio_fulfillment_enabled,
        "initial_bearing_move_id": acceptance["initial_bearing_move"].id,
        "initial_bearing_quantity": acceptance["initial_bearing_move"].quantity,
        "bearing_quantity": acceptance["product_bearing"].with_company(company).qty_available,
    }


def prepare_local_finance(env, *, allow_local=False, check_only=False, enable_fulfillment=False):
    """Preflight/prepare the local CNY profile; the caller owns the transaction."""
    if allow_local is not True:
        raise UserError(_("Local finance preparation requires explicit local opt-in."))
    if check_only not in (True, False) or enable_fulfillment not in (True, False) or (check_only and enable_fulfillment):
        raise UserError(_("Check-only and enable-fulfillment are mutually exclusive boolean options."))
    _validate_local_target(env)
    actor = env["res.users"].with_user(env.uid).env.user
    if not actor.has_group("base.group_system"):
        raise AccessError(_("Only actual Settings administrators can prepare local finance."))
    with env.cr.savepoint():
        env.cr.execute("SELECT pg_advisory_xact_lock(%s)", [_LOCK])
        return _prepare(env, check_only=check_only, enable_fulfillment=enable_fulfillment)
