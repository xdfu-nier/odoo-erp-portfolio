"""Native customer returns must retain a genuine delivery and its physical cap."""
import math

from odoo import api, models
from odoo.exceptions import UserError
from odoo.tools.float_utils import float_compare

from .financial_policy import _stock_quantity
from .stock_policy import authorize_companies, companies_for, controlled, run_physical

RETURN_FIELDS = {
    "origin_returned_move_id", "sale_line_id", "company_id", "product_id", "product_uom",
    "location_id", "location_dest_id", "picking_type_id", "picking_id", "to_refund",
    "product_uom_qty", "quantity", "state",
}


def _candidate(model, values, move=None):
    defaults = {name: move[name].id if move._fields[name].type == "many2one" else move[name]
                for name in RETURN_FIELDS} if move else model.default_get(sorted(RETURN_FIELDS))
    return {**defaults, **values}


def _return_identity(model, values, move=None):
    """Validate only controlled customer returns; native receipts stay native."""
    env = model.env
    candidate = _candidate(model, values, move)
    source = env["sale.order.line"].browse(candidate.get("sale_line_id") or []).sudo()
    origin = env["stock.move"].browse(candidate.get("origin_returned_move_id") or []).sudo()
    old_sources = move.sudo().sale_line_id | move.sudo().origin_returned_move_id.sale_line_id if move else env["sale.order.line"]
    sources = source | origin.sale_line_id | old_sources
    if not controlled(sources.order_id.company_id):
        return False
    location = env["stock.location"].browse(candidate.get("location_id") or []).sudo()
    destination = env["stock.location"].browse(candidate.get("location_dest_id") or []).sudo()
    old_incoming = bool(move and move.location_id._is_outgoing() and not move.location_dest_id._is_outgoing())
    incoming = location._is_outgoing() and not destination._is_outgoing()
    returning = incoming or old_incoming or bool(origin and origin.location_dest_id._is_outgoing())
    if not returning:
        return False
    if move is None and "to_refund" not in values:
        configured = env["ir.default"]._get_model_defaults(model._name)
        if "default_to_refund" not in env.context and "to_refund" not in configured:
            values["to_refund"] = candidate["to_refund"] = True
    # Warehouse operations authorize the real stock actor's actual/active
    # companies and inspect bounded commercial identities, as M2a lock_stock
    # does. An Own Sales role must not remove otherwise valid Stock rights on
    # another salesperson's picking. The financial fact API retains its separate
    # original-user source-read contract without any broader Sales grants here.
    companies = companies_for(model, candidate) | sources.order_id.company_id | origin.company_id
    authorize_companies(companies.with_env(env))
    company = env["res.company"].browse(candidate.get("company_id") or [])
    product = env["product.product"].browse(candidate.get("product_id") or [])
    uom = env["uom.uom"].browse(candidate.get("product_uom") or [])
    picking = env["stock.picking"].browse(candidate.get("picking_id") or []).sudo()
    operation = env["stock.picking.type"].browse(candidate.get("picking_type_id") or []).sudo()
    if (
        not origin or origin.state != "done" or not origin.location_dest_id._is_outgoing()
        or origin.location_id._is_outgoing() or not source or source != origin.sale_line_id
        or company != source.order_id.company_id or origin.company_id != company
        or product != source.product_id or product != origin.product_id
        or not uom or uom.category_id != source.product_uom.category_id
        or location != origin.location_dest_id or not incoming
        or (picking and picking.company_id != company)
        or (operation and operation.company_id != company)
        or (location | destination).company_id - company
    ):
        raise UserError(env._("A customer return must reverse its genuine completed delivery with the same sales source, product, company and unit."))
    warehouse = origin.picking_type_id.warehouse_id
    if (
        not warehouse or destination.usage != "internal"
        or not destination.parent_path.startswith(warehouse.view_location_id.parent_path)
        or operation and operation.warehouse_id != warehouse
    ):
        raise UserError(env._("A customer return must enter the original delivery's warehouse."))
    if not candidate.get("to_refund"):
        raise UserError(env._("Controlled customer returns must update sales quantities."))
    for name in ("quantity", "product_uom_qty"):
        quantity = candidate.get(name, 0)
        if not math.isfinite(quantity) or quantity < 0:
            raise UserError(env._("Customer return quantities must be finite and nonnegative."))
    _stock_quantity(origin, source)
    return True


def _check_done_returns(moves):
    env = moves.env
    origins = env["stock.move"]
    for move in moves.sudo():
        if _return_identity(moves, {}, move):
            origins |= move.origin_returned_move_id
    for origin in origins:
        source = origin.sale_line_id
        maximum = _stock_quantity(origin, source)
        returned = env["stock.move"].sudo().search([
            ("origin_returned_move_id", "=", origin.id), ("state", "=", "done"),
        ])
        quantity = 0.0
        for move in returned:
            quantity += _stock_quantity(move, source, returning=True)
            warehouse = origin.picking_type_id.warehouse_id
            for detail in move.move_line_ids:
                if (detail.location_id != origin.location_dest_id or detail.location_dest_id.usage != "internal"
                    or not detail.location_dest_id.parent_path.startswith(warehouse.view_location_id.parent_path)):
                    raise UserError(env._("Completed return details must reverse the original customer delivery into its warehouse."))
        if float_compare(quantity, maximum, precision_rounding=source.product_uom.rounding) > 0:
            raise UserError(env._("Cumulative completed returns cannot exceed the original actual delivery quantity."))


def _check_actual_return_boundaries(moves):
    """Actual SML directions cannot conceal a customer return in an internal parent."""
    env = moves.env
    for move in moves.sudo():
        if not controlled(move.sale_line_id.order_id.company_id):
            continue
        incoming = move.move_line_ids.filtered(
            lambda detail: detail.location_id._is_outgoing() and not detail.location_dest_id._is_outgoing(),
        )
        if incoming:
            if not move.location_id._is_outgoing() or move.location_dest_id._is_outgoing():
                raise UserError(env._("Actual customer return details must belong to a genuine customer return movement."))
            _return_identity(moves, {}, move)


class StockMove(models.Model):
    _inherit = "stock.move"

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        returning = False
        for supplied in vals_list:
            values = dict(supplied)
            # Only controlled customer returns acquire the canonical flag;
            # native receipts, deliveries and inventory inputs stay unchanged.
            returning = _return_identity(self, values) or returning
            prepared.append(values)
        if not returning:
            return super().create(vals_list)
        with self.env.cr.savepoint():
            return super().create(prepared)

    def write(self, values):
        if not RETURN_FIELDS.intersection(values):
            return super().write(values)
        returning = False
        for move in self:
            returning = _return_identity(self, values, move) or returning
        if not returning:
            return super().write(values)
        with self.env.cr.savepoint():
            return super().write(values)

    def _action_done(self, cancel_backorder=False):
        def complete():
            _check_actual_return_boundaries(self)
            result = super(StockMove, self)._action_done(cancel_backorder=cancel_backorder)
            _check_done_returns(result)
            return result

        # The existing frame holds the shared company revision and parent stock
        # locks. Validate cumulative DONE facts before leaving its savepoint.
        return run_physical(self, complete)
