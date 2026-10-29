"""Read-only checks at business gates; incomplete drafts remain ordinary Odoo data."""

from odoo import _, fields, models
from odoo.exceptions import UserError


class PortfolioMasterData(models.AbstractModel):
    _name = "portfolio.master.data"
    _description = "Industrial parts master-data validation"

    def _partner(self, partner, company, role):
        # Business users need no supplier/settings write rights. Elevation is
        # limited to catalog reads, with the order's company scope explicit.
        partner = partner.sudo().with_context(active_test=False)
        commercial = partner.commercial_partner_id
        if not partner or not commercial:
            raise UserError(_("The %(role)s is missing.", role=role))
        if not partner.active or not commercial.active:
            raise UserError(_("The %(role)s is inactive.", role=role))
        if any(record.company_id and record.company_id != company for record in partner | commercial):
            raise UserError(_("The %(role)s belongs to another company.", role=role))
        code = commercial.ref
        if not code or not code.strip() or code != code.strip():
            raise UserError(_("The %(role)s requires a ref code without surrounding whitespace.", role=role))
        matches = self.env["res.partner"].sudo().with_context(active_test=False).search([
            ("active", "=", True), ("company_id", "in", [False, company.id]), ("ref", "=", code),
        ]).filtered(lambda item: item.commercial_partner_id == item)
        if matches.ids != commercial.ids:
            raise UserError(_("The %(role)s ref code is ambiguous in this company and shared records.", role=role))
        return commercial

    def _has_supplier(self, product, company):
        today = fields.Date.today()
        sellers = self.env["product.supplierinfo"].sudo().search([
            ("product_tmpl_id", "=", product.product_tmpl_id.id),
            ("product_id", "in", [False, product.id]),
            ("company_id", "in", [False, company.id]),
            "|", ("date_start", "=", False), ("date_start", "<=", today),
            "|", ("date_end", "=", False), ("date_end", ">=", today),
        ])
        for seller in sellers:
            if not seller.currency_id or not seller.currency_id.active:
                continue
            try:
                self._partner(seller.partner_id, company, "supplier vendor")
            except UserError:
                continue
            return True
        return False

    def _line(self, line, company, number):
        product = line.product_id.sudo().with_context(active_test=False)
        label = _("Line %(number)s", number=number)
        if not product:
            raise UserError(_("%(line)s requires a product and SKU.", line=label))
        if not product.active or not product.product_tmpl_id.active:
            raise UserError(_("%(line)s product is inactive.", line=label))
        if product.company_id and product.company_id != company:
            raise UserError(_("%(line)s product belongs to another company.", line=label))
        code = product.default_code
        if not code or not code.strip() or code != code.strip():
            raise UserError(_("%(line)s requires a SKU without surrounding whitespace.", line=label))
        matches = self.env["product.product"].sudo().with_context(active_test=False).search([
            ("active", "=", True), ("company_id", "in", [False, company.id]),
            ("default_code", "=", code),
        ])
        if matches.ids != product.ids:
            raise UserError(_("%(line)s SKU is ambiguous in this company and shared products.", line=label))
        unit = line.product_uom.sudo()
        base, purchase = product.uom_id, product.uom_po_id
        if (
            not unit or not unit.active or not base.active or not purchase.active
            or unit.category_id != base.category_id or purchase.category_id != base.category_id
        ):
            raise UserError(_("%(line)s Unit of Measure must be active and in the product's unit category.", line=label))
        if product.type == "consu" and product.is_storable:
            if product.invoice_policy != "delivery":
                raise UserError(_("%(line)s stock product requires invoicing on delivery quantities.", line=label))
            if not self._has_supplier(product, company):
                raise UserError(_("%(line)s requires a current supplier with a valid vendor ref and currency.", line=label))

    def _validate_orders(self, orders, role):
        for order in orders:
            if not order.company_id.portfolio_masterdata_enabled:
                continue
            self._partner(order.partner_id, order.company_id, role)
            for number, line in enumerate(order.order_line.filtered(lambda item: not item.display_type), 1):
                self._line(line, order.company_id, number)
