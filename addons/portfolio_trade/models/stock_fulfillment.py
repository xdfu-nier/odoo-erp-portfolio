from odoo import _, api, models
from odoo.exceptions import AccessError, UserError
from odoo.tools.float_utils import float_compare

from .stock_policy import (
    companies_for,
    controlled,
    effective_values,
    include_native_records,
    lock_stock,
    physical_capability,
    run_physical,
)

MOVE_FACTS = {
    "state", "quantity", "product_uom_qty", "product_qty", "product_id", "product_uom", "company_id",
    "location_id", "location_dest_id", "location_final_id", "picking_id", "picking_type_id", "sale_line_id",
    "purchase_line_id", "origin_returned_move_id", "returned_move_ids", "to_refund", "is_inventory", "scrap_id",
    "move_line_ids", "move_line_ids_without_package", "move_orig_ids", "move_dest_ids", "picked",
    "restrict_partner_id", "date",
}
LINE_FACTS = {
    "state", "quantity", "quantity_product_uom", "product_id", "product_uom_id", "company_id",
    "location_id", "location_dest_id", "move_id", "picking_id", "lot_id", "lot_name", "package_id",
    "result_package_id", "owner_id", "quant_id", "picked", "date", "package_level_id",
}
PICKING_FACTS = {
    "state", "date_done", "company_id", "location_id", "location_dest_id", "picking_type_id", "sale_id",
    "move_ids", "move_ids_without_package", "move_line_ids", "move_line_ids_without_package",
    "owner_id", "partner_id", "group_id", "return_id", "package_level_ids",
}


def _guard_mutation(records, values, facts, deleting=False):
    companies = companies_for(records, values)
    if not controlled(companies):
        return
    if physical_capability(records, companies):
        return
    lock_stock(records, companies, values)
    if deleting or facts.intersection(values):
        if any(record.state == "done" for record in records):
            raise UserError(_("Completed stock facts are immutable. Use a standard return or inventory adjustment."))
    if values.get("state") == "done":
        raise UserError(_("Use the standard stock completion actions."))
    candidate_move = records.env["stock.move"].browse(values.get("move_id") or []).sudo()
    candidate_picking = records.env["stock.picking"].browse(values.get("picking_id") or []).sudo()
    if candidate_move.state == "done" or candidate_picking.state == "done":
        raise UserError(_("Additional operations cannot be attached to completed stock records."))


def _guard_create(model, values):
    candidate = effective_values(model, values)
    companies = companies_for(model, candidate)
    if controlled(companies):
        # Creation is still pending business input, so no warehouse gate is
        # needed; native Sales confirmation must create and reserve stock moves.
        lock_stock(model, companies, candidate)
        if candidate.get("state") == "done":
            raise UserError(_("Create a pending stock operation, then use the standard completion action."))
        move = model.env["stock.move"].browse(candidate.get("move_id") or []).sudo()
        picking = model.env["stock.picking"].browse(candidate.get("picking_id") or []).sudo()
        if move.state == "done" or picking.state == "done":
            raise UserError(_("Additional operations cannot be attached to completed stock records."))


def _check_line_product(model, values, line=None):
    move_id = values.get("move_id", line.move_id.id if line else False)
    product_id = values.get("product_id", line.product_id.id if line else False)
    if values.get("quant_id"):
        # Native create/write copies the selected quant's product over the
        # caller's explicit product, so validate that effective identity too.
        product_id = model.env["stock.quant"].browse(values["quant_id"]).sudo().product_id.id
    move = model.env["stock.move"].browse(move_id or []).sudo()
    if move and move.product_id.id != product_id:
        raise UserError(_("A move line must use the product of its parent stock movement."))


class StockMove(models.Model):
    _inherit = "stock.move"

    @api.model_create_multi
    def create(self, vals_list):
        for values in vals_list:
            _guard_create(self, values)
        moves = super().create(vals_list)
        include_native_records(moves)
        return moves

    def write(self, values):
        if MOVE_FACTS.intersection(values):
            _guard_mutation(self, values, MOVE_FACTS)
        return super().write(values)

    def unlink(self):
        _guard_mutation(self, {}, MOVE_FACTS, deleting=True)
        return super().unlink()

    def _action_done(self, cancel_backorder=False):
        return run_physical(self, lambda: super(StockMove, self)._action_done(cancel_backorder=cancel_backorder))


class StockMoveLine(models.Model):
    _inherit = "stock.move.line"

    @api.model_create_multi
    def create(self, vals_list):
        configured = self.env["ir.default"]._get_model_defaults(self._name)
        product_defaults = self.default_get(["product_id"])
        for values in vals_list:
            _guard_create(self, values)
            candidate = {**product_defaults, **effective_values(self, values)}
            if controlled(companies_for(self, candidate)):
                if (
                    "quantity_product_uom" in values or "quantity_product_uom" in configured
                    or "default_quantity_product_uom" in self.env.context
                ):
                    raise UserError(_("The product-unit stock quantity is derived from the native move-line quantity."))
                _check_line_product(self, candidate)
        lines = super().create(vals_list)
        include_native_records(lines)
        return lines

    def write(self, values):
        if LINE_FACTS.intersection(values):
            _guard_mutation(self, values, LINE_FACTS)
            if controlled(companies_for(self, values)):
                if "quantity_product_uom" in values:
                    raise UserError(_("The product-unit stock quantity is derived from the native move-line quantity."))
                if {"product_id", "move_id", "quant_id"}.intersection(values):
                    for line in self:
                        _check_line_product(self, values, line)
        return super().write(values)

    def unlink(self):
        _guard_mutation(self, {}, LINE_FACTS, deleting=True)
        return super().unlink()

    def _action_done(self):
        companies = companies_for(self)
        if controlled(companies):
            if not physical_capability(self, companies):
                raise AccessError(_("Complete move lines through their parent stock operation."))
            # Check actual stored facts immediately before native quant deltas.
            # A legacy/defaulted computed cache must not replace the canonical
            # native UoM conversion of quantity for the real line product.
            fields = ["quantity", "quantity_product_uom", "product_id", "product_uom_id", "move_id"]
            self.flush_recordset(fields)
            self.invalidate_recordset(fields)
            for line in self:
                _check_line_product(self, {}, line)
                expected = line.product_uom_id._compute_quantity(
                    line.quantity, line.product_id.uom_id, rounding_method="HALF-UP",
                )
                if float_compare(line.quantity_product_uom, expected, precision_rounding=line.product_id.uom_id.rounding):
                    raise UserError(_("The move-line stock quantity does not match its native unit conversion."))
        return super()._action_done()


class StockPicking(models.Model):
    _inherit = "stock.picking"

    @api.model_create_multi
    def create(self, vals_list):
        for values in vals_list:
            _guard_create(self, values)
        pickings = super().create(vals_list)
        include_native_records(pickings)
        return pickings

    def write(self, values):
        if PICKING_FACTS.intersection(values):
            _guard_mutation(self, values, PICKING_FACTS)
        return super().write(values)

    def unlink(self):
        _guard_mutation(self, {}, PICKING_FACTS, deleting=True)
        return super().unlink()

    def button_validate(self):
        # Native validation partitions a batch into two completion cohorts.
        # Keep both inside one physical frame and rollback boundary, including
        # when a later cohort fails after an earlier one has already completed.
        return run_physical(self, lambda: super(StockPicking, self).button_validate())

    def _action_done(self):
        return run_physical(self, lambda: super(StockPicking, self)._action_done())
