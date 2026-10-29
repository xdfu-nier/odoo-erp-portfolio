"""Explicit local acceptance fixtures. Importing/installing the addon never seeds data."""

from collections import Counter

from odoo import Command, _
from odoo.exceptions import AccessError, UserError

NAMESPACE = "portfolio_trade_acceptance"
ROLE_GROUPS = {
    "sales": "sales_team.group_sale_salesman",
    "manager": "sales_team.group_sale_manager",
    "manager2": "sales_team.group_sale_manager",
    "buyer": "purchase.group_purchase_user",
    "warehouse": "stock.group_stock_user",
    "finance": "account.group_account_invoice",
    "other_sales": "sales_team.group_sale_salesman",
}


class _Profile:
    def __init__(self, env):
        self.env = env

    def marker(self, key):
        return self.env["ir.model.data"].search([
            ("module", "=", NAMESPACE), ("name", "=", key),
        ])

    def own(self, key, record):
        self.env["ir.model.data"].create({
            "module": NAMESPACE, "name": key, "model": record._name,
            "res_id": record.id, "noupdate": True,
        })
        return record

    def get_or_create(self, key, model, values, conflict_domain):
        marker = self.marker(key)
        records = self.env[model].with_context(active_test=False)
        if marker:
            record = records.browse(marker.res_id).exists()
            if marker.model != model or not record:
                raise UserError(_("Acceptance profile conflict: %(key)s has a missing or invalid identity.", key=key))
            return record
        if records.search_count(conflict_domain):
            raise UserError(_("Acceptance profile conflict: %(key)s already exists outside this profile.", key=key))
        return self.own(key, self.env[model].create(values))


def prepare_acceptance(env, passwords_by_role, allow_local=True):
    """Create the fake profile once, preserving all known records on reruns.

    The CLI separately verifies host/database. Direct developer callers explicitly
    invoke this function and must supply the local opt-in and private credentials.
    The transaction belongs to the caller; a failed preparation rolls back its
    entire savepoint, and this function never commits or returns credentials.
    """
    if allow_local is not True:
        raise UserError(_("Acceptance preparation requires explicit local opt-in."))
    if not env.user.has_group("base.group_system"):
        raise AccessError(_("Only settings administrators can prepare acceptance data."))
    if (
        not isinstance(passwords_by_role, dict) or set(passwords_by_role) != set(ROLE_GROUPS)
        or any(not isinstance(value, str) or len(value) < 24 or not value.strip() for value in passwords_by_role.values())
        or len(set(passwords_by_role.values())) != len(ROLE_GROUPS)
    ):
        raise UserError(_("Supply seven distinct acceptance credentials of at least24 characters."))
    with env.cr.savepoint():
        # Serialize explicit seed attempts so a rerun cannot duplicate adjustments.
        env.cr.execute("SELECT pg_advisory_xact_lock(%s)", [762714391])
        return _prepare(env(context={
            **env.context, "no_reset_password": True, "mail_create_nosubscribe": True,
            "mail_create_nolog": True, "tracking_disable": True,
        }), passwords_by_role)


def _prepare(env, passwords_by_role):
    profile = _Profile(env)
    established = bool(profile.marker("company_a"))
    if established and not profile.marker("initial_bearing_move"):
        raise UserError(_("Acceptance inventory evidence is missing; refusing a second inventory adjustment."))
    currency = env.ref("base.CNY")
    if not established and not currency.active:
        # This standard currency is reference data, not unknown business data.
        currency.active = True
    company = profile.get_or_create("company_a", "res.company", {
        "name": "PARTS-A", "currency_id": currency.id,
        "portfolio_masterdata_enabled": True, "portfolio_approval_enabled": True,
    }, [("name", "=", "PARTS-A")])
    other = profile.get_or_create("company_b", "res.company", {
        "name": "PARTS-B", "currency_id": currency.id,
    }, [("name", "=", "PARTS-B")])
    # Give only the invoking settings administrator the new companies explicitly.
    missing = (company | other) - env.user.company_ids
    if missing:
        env.user.write({"company_ids": [Command.link(item.id) for item in missing]})
    env = env(context={**env.context, "allowed_company_ids": (company | other).ids})
    env = env["res.company"].with_company(company).env
    profile.env = env
    warehouse_marker = profile.marker("warehouse_a")
    if not warehouse_marker and not established:
        # Some standard company-create hooks (notably test mode) already create
        # a warehouse. This company was created in this savepoint, so its hook's
        # output has known provenance and can receive our stable identity.
        warehouses = env["stock.warehouse"].search([("company_id", "=", company.id)])
        if len(warehouses) > 1:
            raise UserError(_("Acceptance warehouse conflict: multiple initial warehouses."))
        warehouse = warehouses or env["stock.warehouse"].create({
            "name": "PARTS-A Acceptance", "code": "PAUAT", "company_id": company.id,
        })
        profile.own("warehouse_a", warehouse)
    elif not warehouse_marker:
        raise UserError(_("Acceptance inventory warehouse evidence is missing."))
    warehouse_marker = profile.marker("warehouse_a")
    warehouse = env["stock.warehouse"].browse(warehouse_marker.res_id).exists()
    if warehouse_marker.model != "stock.warehouse" or not warehouse or warehouse.company_id != company:
        raise UserError(_("Acceptance inventory warehouse evidence is invalid."))
    users = {}
    for role, group in ROLE_GROUPS.items():
        owner = other if role == "other_sales" else company
        login = f"portfolio.acceptance.{role}"
        users[role] = profile.get_or_create(f"user_{role}", "res.users", {
            "name": f"Acceptance {role}", "login": login, "password": passwords_by_role[role],
            "company_id": owner.id, "company_ids": [Command.set(owner.ids)],
            "groups_id": [Command.set((env.ref("base.group_user") | env.ref(group)).ids)],
            "notification_type": "inbox", "email": False,
        }, ["|", ("login", "=", login), ("name", "=", f"Acceptance {role}")])
    partners = {}
    for key, name, ref in (("customer", "Acceptance customer", "CUST-001"), ("vendor", "Acceptance vendor", "VEND-001")):
        partners[key] = profile.get_or_create(key, "res.partner", {
            "name": name, "ref": ref, "is_company": True, "company_id": company.id,
            "customer_rank": int(key == "customer"), "supplier_rank": int(key == "vendor"),
        }, ["|", ("name", "=", name), ("ref", "=", ref)])
    unit = env.ref("uom.product_uom_unit")
    products = {}
    for key, code, name, tracking, price, cost in (
        ("bearing", "BRG-6205", "Acceptance bearing6205", "none", 100, 60),
        ("sensor", "SEN-M12", "Acceptance M12 sensor", "serial", 200, 120),
        ("bolt", "BOLT-M8", "Acceptance M8 bolt", "none", 1, 0.6),
    ):
        product = profile.get_or_create(f"product_{key}", "product.product", {
            "name": name, "default_code": code, "type": "consu", "is_storable": True,
            "tracking": tracking, "invoice_policy": "delivery", "list_price": price,
            "standard_price": cost, "company_id": company.id,
            "uom_id": unit.id, "uom_po_id": unit.id,
            "taxes_id": [Command.clear()], "supplier_taxes_id": [Command.clear()],
        }, ["|", ("name", "=", name), ("default_code", "=", code)])
        products[key] = product
        profile.get_or_create(f"supplier_{key}", "product.supplierinfo", {
            "product_id": product.id, "product_tmpl_id": product.product_tmpl_id.id,
            "partner_id": partners["vendor"].id, "company_id": company.id,
            "currency_id": currency.id, "price": cost, "min_qty": 0,
        }, [("product_id", "=", product.id), ("partner_id", "=", partners["vendor"].id)])
    if not established:
        _initial_inventory(profile, products["bearing"], company)
    marker = profile.marker("initial_bearing_move")
    move = env["stock.move"].browse(marker.res_id).exists()
    if (
        marker.model != "stock.move" or not move or not move.is_inventory or move.state != "done"
        or move.quantity != 40 or move.product_id != products["bearing"] or move.company_id != company
        or move.location_id.usage != "inventory" or move.location_id.company_id not in (env["res.company"], company)
        or move.location_dest_id != warehouse.lot_stock_id
        or move.location_dest_id.usage != "internal" or move.location_dest_id.company_id != company
    ):
        raise UserError(_("Acceptance inventory evidence is invalid; refusing a new adjustment."))
    identities = env["ir.model.data"].search([("module", "=", NAMESPACE)], order="name")
    return {
        "namespace": NAMESPACE,
        "identities": {item.name: {"model": item.model, "id": item.res_id} for item in identities},
        "record_counts": dict(sorted(Counter(identities.mapped("model")).items())),
        "bearing_quantity": products["bearing"].with_company(company).qty_available,
        "bearing_move_count": env["stock.move"].search_count([("product_id", "=", products["bearing"].id), ("company_id", "=", company.id)]),
        "initial_bearing_move_id": move.id,
    }


def _initial_inventory(profile, product, company):
    env = profile.env
    warehouse = env["stock.warehouse"].browse(profile.marker("warehouse_a").res_id)
    if not warehouse:
        raise UserError(_("Acceptance inventory requires the company's standard warehouse."))
    if env["stock.move"].search_count([("product_id", "=", product.id)]) or env["stock.quant"].search_count([("product_id", "=", product.id)]):
        raise UserError(_("Acceptance inventory conflict: the new product already has stock history."))
    before = env["stock.move"].search([("product_id", "=", product.id)])
    quant = env["stock.quant"].with_context(inventory_mode=True).create({
        "product_id": product.id, "location_id": warehouse.lot_stock_id.id, "inventory_quantity": 40,
    })
    action = quant.action_apply_inventory()
    moves = env["stock.move"].search([
        ("product_id", "=", product.id), ("company_id", "=", company.id),
        ("is_inventory", "=", True), ("state", "=", "done"), ("id", "not in", before.ids),
    ])
    if action or len(moves) != 1 or moves.quantity != 40:
        raise UserError(_("Acceptance inventory adjustment did not create its standard evidence."))
    profile.own("initial_bearing_move", moves)
