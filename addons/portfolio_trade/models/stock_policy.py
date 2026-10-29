"""Private execution capabilities for native stock operations, never RPC context."""
import contextvars
from dataclasses import dataclass, field

from odoo import _
from odoo.exceptions import AccessError, UserError
from odoo.tools.float_utils import float_compare

_PHYSICAL = contextvars.ContextVar("portfolio_physical_operation", default=None)
_QUANT = contextvars.ContextVar("portfolio_native_quant_update", default=None)
_ZERO_CLEANUP = contextvars.ContextVar("portfolio_zero_quant_cleanup", default=None)

IDENTITY = {
    "company_id", "location_id", "location_dest_id", "move_id", "picking_id", "picking_type_id", "quant_id",
}


def effective_values(model, values):
    """Inspect the same effective defaults used by ORM create, including ir.default."""
    names = (IDENTITY | {"state"}) & set(model._fields)
    return {**model.default_get(sorted(names)), **values}


def companies_for(records, values=None):
    """Bounded identity inspection includes old and candidate canonical parents.

    Related company_id values are not authoritative: locations, moves and
    picking types determine scope even when a caller supplies a different one.
    No business payload or unbounded search is elevated here.
    """
    env = records.env
    companies = env["res.company"]
    current = records.sudo()
    if current and "company_id" in current._fields:
        companies |= current.company_id
    locations = env["stock.location"]
    for name in ("location_id", "location_dest_id"):
        if name in current._fields:
            locations |= current[name]
    moves = env["stock.move"]
    pickings = env["stock.picking"]
    if records._name == "stock.move":
        moves |= current
    elif records._name == "stock.move.line":
        moves |= current.move_id
        pickings |= current.picking_id
    elif records._name == "stock.picking":
        pickings |= current
        moves |= current.move_ids
    if values:
        companies |= env["res.company"].browse(values.get("company_id") or [])
        for name in ("location_id", "location_dest_id"):
            locations |= env["stock.location"].browse(values.get(name) or [])
        moves |= env["stock.move"].browse(values.get("move_id") or [])
        pickings |= env["stock.picking"].browse(values.get("picking_id") or [])
        picking_type = env["stock.picking.type"].browse(values.get("picking_type_id") or []).sudo()
        companies |= picking_type.company_id
        quant = env["stock.quant"].browse(values.get("quant_id") or []).sudo()
        locations |= quant.location_id
    moves = moves.sudo()
    pickings |= moves.picking_id
    pickings = pickings.sudo()
    companies |= moves.company_id | pickings.company_id | pickings.picking_type_id.company_id
    locations |= moves.location_id | moves.location_dest_id | moves.move_line_ids.location_id | moves.move_line_ids.location_dest_id
    locations |= pickings.location_id | pickings.location_dest_id
    companies |= locations.sudo().company_id
    return companies.with_env(env)


def controlled(companies):
    return bool(companies.sudo().filtered("portfolio_fulfillment_enabled"))


def authorize_companies(companies):
    actor = companies.with_user(companies.env.uid)
    if set(companies.ids) - set(actor.env.user.company_ids.ids):
        raise AccessError(_("The stock company is outside your allowed companies."))
    actor.check_access("read")
    if set(companies.ids) - set(actor.env.companies.ids):
        raise AccessError(_("The stock company is outside your active companies."))


def require_warehouse(env, companies):
    authorize_companies(companies)
    if not env["res.users"].with_user(env.uid).env.user.has_group("stock.group_stock_user"):
        raise AccessError(_("Only warehouse users can complete physical stock operations."))


@dataclass
class PhysicalOperation:
    cursor: object
    uid: int
    company_ids: frozenset
    move_ids: set = field(default_factory=set)
    picking_ids: set = field(default_factory=set)
    product_ids: set = field(default_factory=set)
    location_ids: set = field(default_factory=set)
    touched: set = field(default_factory=set)

    def matches(self, env, companies):
        return self.cursor is env.cr and self.uid == env.uid and self.company_ids.issuperset(companies.ids)

    def allows(self, records):
        if records._name == "stock.move":
            return self.move_ids.issuperset(records.ids)
        if records._name == "stock.move.line":
            return all(line.move_id.id in self.move_ids for line in records.sudo())
        if records._name == "stock.picking":
            return self.picking_ids.issuperset(records.ids)
        return False

    def include(self, records):
        """Native splits may create more pending records during this exact call."""
        moves = records.env["stock.move"]
        pickings = records.env["stock.picking"]
        if records._name == "stock.move":
            moves |= records
        elif records._name == "stock.picking":
            pickings |= records
            moves |= records.move_ids
        elif records._name == "stock.move.line":
            moves |= records.move_id
        pickings |= moves.picking_id
        self.move_ids.update(moves.ids)
        self.picking_ids.update(pickings.ids)
        self.product_ids.update((moves.product_id | moves.move_line_ids.product_id).ids)
        self.location_ids.update((moves.location_id | moves.location_dest_id | moves.move_line_ids.location_id | moves.move_line_ids.location_dest_id).ids)


def physical_capability(records, companies=None):
    cap = _PHYSICAL.get()
    companies = companies if companies is not None else companies_for(records)
    if cap and cap.matches(records.env, companies) and cap.allows(records):
        return cap
    return None


def include_native_records(records):
    cap = _PHYSICAL.get()
    if cap and cap.matches(records.env, companies_for(records)):
        if records._name != "stock.move.line" or cap.move_ids.issuperset(records.move_id.ids):
            cap.include(records)


def lock_stock(records, companies):
    """Company revision first, then bounded read-only SO and native stock locks."""
    companies._portfolio_lock_fulfillment()
    env = records.env
    moves = env["stock.move"]
    pickings = env["stock.picking"]
    lines = env["stock.move.line"]
    if records._name == "stock.move":
        moves |= records
    elif records._name == "stock.picking":
        pickings |= records
        moves |= records.move_ids
    elif records._name == "stock.move.line":
        lines |= records
        moves |= records.move_id
    # Stock company authorization precedes this bounded source identity read.
    # A composite Own Sales + Stock actor needs no sales business write grant.
    sources = moves.sudo().sale_line_id.order_id
    if sources.company_id - companies:
        raise UserError(_("The stock movement and its sales source must share an authorized company."))
    if sources:
        env.cr.execute("SELECT id FROM sale_order WHERE id IN %s ORDER BY id FOR UPDATE", [tuple(sources.ids)])
    pickings |= moves.picking_id
    lines |= moves.move_line_ids
    for table, items in (("stock_picking", pickings), ("stock_move", moves), ("stock_move_line", lines)):
        if items:
            env.cr.execute(f"SELECT id FROM {table} WHERE id IN %s ORDER BY id FOR UPDATE", [tuple(items.ids)])
            items.invalidate_recordset()


def check_physical_stock(env, cap):
    """Sum complete identities across duplicate native quant fragments.

    Actual quantity includes this operation's own reservation. Free quantity
    would subtract that reservation twice and reject legitimate deliveries.
    """
    if not cap.touched:
        return
    products = {key[0] for key in cap.touched}
    locations = {key[1] for key in cap.touched}
    groups = env["stock.quant"].sudo()._read_group(
        [("product_id", "in", sorted(products)), ("location_id", "in", sorted(locations))],
        ["product_id", "location_id", "lot_id", "package_id", "owner_id"], ["quantity:sum"],
    )
    for product, location, lot, package, owner, quantity in groups:
        key = (product.id, location.id, lot.id, package.id, owner.id)
        if key in cap.touched and location.usage == "internal" and float_compare(
            quantity, 0, precision_rounding=product.uom_id.rounding,
        ) < 0:
            raise UserError(_("There is insufficient physical stock for %(product)s at %(location)s.",
                              product=product.display_name, location=location.display_name))


def run_physical(records, operation):
    companies = companies_for(records)
    if not controlled(companies):
        return operation()
    require_warehouse(records.env, companies)
    if physical_capability(records, companies):
        return operation()
    with records.env.cr.savepoint():
        lock_stock(records, companies)
        cap = PhysicalOperation(records.env.cr, records.env.uid, frozenset(companies.ids))
        cap.include(records)
        token = _PHYSICAL.set(cap)
        try:
            result = operation()
            records.env.flush_all()
            check_physical_stock(records.env, cap)
            return result
        finally:
            _PHYSICAL.reset(token)


def quant_key(product, location, lot=None, package=None, owner=None):
    return (product.id, location.id, lot.id if lot else False, package.id if package else False, owner.id if owner else False)
