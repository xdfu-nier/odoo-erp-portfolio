from odoo import _, api, models
from odoo.exceptions import AccessError, UserError
from odoo.tools.float_utils import float_is_zero

from .stock_policy import (
    _PHYSICAL,
    _QUANT,
    _ZERO_CLEANUP,
    authorize_companies,
    companies_for,
    controlled,
    quant_key,
    run_physical,
)

QUANT_IDENTITY = {"product_id", "location_id", "lot_id", "package_id", "owner_id"}
QUANT_FACTS = QUANT_IDENTITY | {"company_id", "quantity", "reserved_quantity", "in_date"}


def _native_quant_allowed(model, values, creating=False):
    cap = _QUANT.get()
    if not cap or cap[0] is not model.env.cr or cap[1] != model.env.uid:
        return False
    if creating:
        permitted = QUANT_IDENTITY | {"in_date"} | cap[3]
        key = tuple(values.get(name) or False for name in (
            "product_id", "location_id", "lot_id", "package_id", "owner_id",
        ))
        return not (set(values) - permitted) and key == cap[2]
    if set(values) - ({"in_date"} | cap[3]):
        return False
    return all(quant_key(q.product_id, q.location_id, q.lot_id, q.package_id, q.owner_id) == cap[2] for q in model)


class StockQuant(models.Model):
    _inherit = "stock.quant"

    @api.model_create_multi
    def create(self, vals_list):
        vals_list = [dict(values) for values in vals_list]
        names = sorted(QUANT_IDENTITY | {"company_id", "quantity", "reserved_quantity"})
        defaults = self.default_get(names)
        configured = self.env["ir.default"]._get_model_defaults(self._name)
        for values in vals_list:
            native_create = _native_quant_allowed(self, values, creating=True)
            if native_create:
                # The exact updater capability authorizes its explicit delta
                # only. Initialize the other physical column before ORM can
                # inject context/ir.default values into this NEW quant.
                values.setdefault("quantity", 0)
                values.setdefault("reserved_quantity", 0)
            effective = {**defaults, **values}
            companies = companies_for(self, effective)
            if not controlled(companies):
                continue
            authorize_companies(companies)
            raw_fields = {"quantity", "reserved_quantity"}
            supplied = raw_fields.intersection(values) | raw_fields.intersection(configured)
            supplied |= {name for name in raw_fields if f"default_{name}" in self.env.context}
            if supplied and not native_create:
                raise UserError(_("Physical stock quantities are maintained by native stock operations."))
        return super().create(vals_list)

    def write(self, values):
        if QUANT_FACTS.intersection(values):
            companies = companies_for(self, values)
            if controlled(companies):
                authorize_companies(companies)
                if not _native_quant_allowed(self, values):
                    raise UserError(_("Physical stock facts are immutable outside native stock operations."))
        return super().write(values)

    def unlink(self):
        companies = companies_for(self)
        if controlled(companies):
            precision = max(6, self.sudo().env.ref("product.decimal_product_uom").digits * 2)
            zero = all(float_is_zero(q.quantity, precision_digits=precision) and float_is_zero(
                q.reserved_quantity, precision_digits=precision,
            ) for q in self)
            cleanup = _ZERO_CLEANUP.get()
            native_cleanup = cleanup and cleanup[0] is self.env.cr and cleanup[1] == self.env.uid
            if not (zero and native_cleanup):
                authorize_companies(companies)
                if not zero and self.env.su:
                    raise UserError(_("Use a standard inventory adjustment to remove physical stock."))
        return super().unlink()

    def _unlink_zero_quants(self):
        # The native SQL selector is global, including companies outside the
        # invoking actor. Its bounded zero-only deletion changes no stock fact.
        token = _ZERO_CLEANUP.set((self.env.cr, self.env.uid))
        try:
            return super()._unlink_zero_quants()
        finally:
            _ZERO_CLEANUP.reset(token)

    def _apply_inventory(self):
        # Gate before native inventory creates its moves, and keep those moves
        # inside the same atomic physical operation as their completion.
        return run_physical(self, lambda: super(StockQuant, self)._apply_inventory())

    @api.model
    def _update_available_quantity(self, product_id, location_id, quantity=False, reserved_quantity=False,
                                   lot_id=None, package_id=None, owner_id=None, in_date=None):
        companies = companies_for(self.browse(), {"location_id": location_id.id})
        physical = _PHYSICAL.get()
        participating = physical and physical.matches(self.env, companies)
        if not controlled(companies) and not participating:
            return super()._update_available_quantity(
                product_id, location_id, quantity=quantity, reserved_quantity=reserved_quantity,
                lot_id=lot_id, package_id=package_id, owner_id=owner_id, in_date=in_date,
            )
        authorize_companies(companies)
        key = quant_key(product_id, location_id, lot_id, package_id, owner_id)
        if quantity:
            if not (
                physical and physical.matches(self.env, companies)
                and product_id.id in physical.product_ids and location_id.id in physical.location_ids
            ):
                raise AccessError(_("Change physical stock through a parent native stock operation."))
            physical.touched.add(key)
        # Reservation-only updates also originate from Sales confirmation. They
        # require real company membership, while the warehouse gate belongs to
        # the physical operation, not this native reservation primitive.
        fields_allowed = ({"quantity"} if quantity else set()) | ({"reserved_quantity"} if reserved_quantity else set())
        token = _QUANT.set((self.env.cr, self.env.uid, key, fields_allowed))
        try:
            return super()._update_available_quantity(
                product_id, location_id, quantity=quantity, reserved_quantity=reserved_quantity,
                lot_id=lot_id, package_id=package_id, owner_id=owner_id, in_date=in_date,
            )
        finally:
            _QUANT.reset(token)
