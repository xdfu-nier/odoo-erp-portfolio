"""Short-lived read-only views of authorized native fulfillment records."""
from odoo import Command, _, api, fields, models
from odoo.exceptions import AccessError, UserError

from .financial_policy import _authorized_sources, financial_facts, physical_facts
from .stock_policy import authorize_companies

BILLING = "account.group_account_invoice"
TRACE_GROUPS = ("sales_team.group_sale_salesman", "purchase.group_purchase_user", BILLING)
PHYSICAL_QUANTITIES = ("gross_delivered_qty", "returned_qty", "net_delivered_qty")
FINANCIAL_QUANTITIES = (
    "draft_invoice_qty", "posted_invoice_qty", "draft_credit_qty", "posted_credit_qty",
)
FINANCIAL_FIELDS = {*FINANCIAL_QUANTITIES, "posted_signed_amount", "invoice_ids", "credit_note_ids"}
DOCUMENT_FIELDS = (
    "purchase_order_ids", "receipt_picking_ids", "delivery_picking_ids",
    "backorder_picking_ids", "return_picking_ids", "return_move_ids", "invoice_ids", "credit_note_ids",
)


def _trace_actor(records):
    actor = records.with_user(records.env.uid)
    if not any(actor.env.user.has_group(group) for group in TRACE_GROUPS):
        raise AccessError(_("Only Sales, Purchase or Billing users can read fulfillment traces."))
    return actor


def _trace_source(order):
    order = _trace_actor(order)
    order.ensure_one()
    order.check_access("read")
    if not order.exists():
        raise UserError(_("Select an existing sales order."))
    authorize_companies(order.company_id)
    # Query canonical identities directly: a One2many cache populated under a
    # restrictive rule can remain incomplete even after switching to sudo.
    canonical = order.env["sale.order.line"].sudo().search([("order_id", "=", order.id)], order="id")
    lines = _authorized_sources(canonical.with_user(order.env.uid))
    return order.with_env(lines.env), lines


def _readable_documents(records, env, company):
    model = env[records._name].with_user(env.uid)
    if not model.has_access("read"):
        return model.browse()
    return model.search([("id", "in", records.ids), ("company_id", "=", company.id)], order="id")


class ReadOnlyTrace:
    """Enforce the actual actor on public snapshot read and mutation paths."""

    def _authorize_trace_read(self, names=None):
        actor = _trace_actor(self)
        actor.check_access("read")
        actor._authorize_trace_source()
        if names and FINANCIAL_FIELDS.intersection(names) and not actor.env.user.has_group(BILLING):
            raise AccessError(_("Only Billing users can read financial trace details."))
        return actor

    def read(self, fields=None, load="_classic_read"):
        actor = self._authorize_trace_read(fields)
        return super(ReadOnlyTrace, actor).read(fields=fields, load=load)

    def web_read(self, specification):
        # Native web_read bypasses read() for an id-only specification.
        actor = self._authorize_trace_read(specification)
        return super(ReadOnlyTrace, actor).web_read(specification)

    @api.model
    def search_read(self, domain=None, fields=None, offset=0, limit=None, order=None, **read_kwargs):
        actor = _trace_actor(self)
        records = actor.search(domain or [], offset=offset, limit=limit, order=order)
        return records.read(fields=fields, **read_kwargs)

    @api.model
    def read_group(self, domain, fields, groupby, offset=0, limit=None, orderby=False, lazy=True):
        actor = _trace_actor(self)
        names = fields + ([groupby] if isinstance(groupby, str) else groupby)
        actor.search(domain)._authorize_trace_read([name.split(":", 1)[0] for name in names])
        return super(ReadOnlyTrace, actor).read_group(
            domain, fields, groupby, offset=offset, limit=limit, orderby=orderby, lazy=lazy,
        )

    def export_data(self, fields_to_export):
        actor = self._authorize_trace_read([part for name in fields_to_export for part in name.split("/")])
        return super(ReadOnlyTrace, actor).export_data(fields_to_export)

    def write(self, values):
        raise AccessError(_("Fulfillment trace snapshots are read-only. Open a new trace to refresh them."))

    def unlink(self):
        # Native autovacuum runs as the superuser; ordinary actors have no
        # unlink grant, including callers retaining their uid under sudo.
        actor = self.with_user(self.env.uid)
        actor.check_access("unlink")
        return super(ReadOnlyTrace, actor).unlink()


class FulfillmentTrace(ReadOnlyTrace, models.TransientModel):
    _name = "portfolio.fulfillment.trace"
    _description = "Read-only sales fulfillment trace"
    _rec_name = "sale_order_id"

    sale_order_id = fields.Many2one("sale.order", required=True, readonly=True, ondelete="cascade")
    company_id = fields.Many2one("res.company", required=True, readonly=True)
    currency_id = fields.Many2one("res.currency", required=True, readonly=True)
    quantity_uom_id = fields.Many2one("uom.uom", readonly=True, string="Quantity unit")
    has_mixed_uom = fields.Boolean(readonly=True)
    gross_delivered_qty = fields.Float(readonly=True, string="Gross delivered")
    returned_qty = fields.Float(readonly=True, string="Physically returned")
    net_delivered_qty = fields.Float(readonly=True, string="Net delivered")
    draft_invoice_qty = fields.Float(readonly=True, groups=BILLING, string="Draft invoiced")
    posted_invoice_qty = fields.Float(readonly=True, groups=BILLING, string="Posted invoiced")
    draft_credit_qty = fields.Float(readonly=True, groups=BILLING, string="Draft credited")
    posted_credit_qty = fields.Float(readonly=True, groups=BILLING, string="Posted credited")
    posted_signed_amount = fields.Monetary(readonly=True, groups=BILLING, string="Net posted product amount")
    line_ids = fields.One2many("portfolio.fulfillment.trace.line", "trace_id", readonly=True, string="Physical sources")
    purchase_order_ids = fields.Many2many("purchase.order", readonly=True, string="Source purchases")
    receipt_picking_ids = fields.Many2many(
        "stock.picking", "portfolio_trace_receipt_rel", readonly=True, string="Receipts",
    )
    delivery_picking_ids = fields.Many2many(
        "stock.picking", "portfolio_trace_delivery_rel", readonly=True, string="Deliveries",
    )
    backorder_picking_ids = fields.Many2many(
        "stock.picking", "portfolio_trace_backorder_rel", readonly=True, string="Backorders",
    )
    return_picking_ids = fields.Many2many(
        "stock.picking", "portfolio_trace_return_rel", readonly=True, string="Returns",
    )
    return_move_ids = fields.Many2many("stock.move", readonly=True, string="Return origins")
    invoice_ids = fields.Many2many(
        "account.move", "portfolio_trace_invoice_rel", readonly=True, groups=BILLING, string="Customer invoices",
    )
    credit_note_ids = fields.Many2many(
        "account.move", "portfolio_trace_credit_rel", readonly=True, groups=BILLING, string="Customer credits",
    )

    def _authorize_trace_source(self):
        for trace in self:
            order, _lines = _trace_source(trace.sudo().sale_order_id.with_user(self.env.uid))
            for name in DOCUMENT_FIELDS:
                if name in FINANCIAL_FIELDS and not self.env.user.has_group(BILLING):
                    continue
                documents = trace.sudo()[name].with_user(self.env.uid)
                if documents:
                    authorize_companies(documents.company_id)
                    documents.check_access("read")
                    if documents.company_id - order.company_id:
                        raise AccessError(_("Trace documents must belong to the authorized source company."))

    @api.model
    def _snapshot(self, sale_order_id):
        order, sources = _trace_source(self.env["sale.order"].browse(sale_order_id))
        env, company = order.env, order.company_id
        physical = physical_facts(sources)
        values = {
            "sale_order_id": order.id, "company_id": company.id, "currency_id": company.currency_id.id,
        }
        units = sources.filtered(lambda line: line.id in physical).product_uom
        values.update(has_mixed_uom=len(units) > 1, quantity_uom_id=units.id if len(units) == 1 else False)
        line_values = {
            line.id: {
                "source_line_id": line.id, "company_id": company.id, "currency_id": company.currency_id.id,
                "product_id": line.product_id.id, "product_uom_id": line.product_uom.id,
                "gross_delivered_qty": physical[line.id]["delivered"],
                "returned_qty": physical[line.id]["returned"],
                "net_delivered_qty": physical[line.id]["delivered"] - physical[line.id]["returned"],
                **dict.fromkeys(FINANCIAL_QUANTITIES, 0.0), "posted_signed_amount": 0.0,
            }
            for line in sources.sorted("id") if line.id in physical
        }

        purchases = env["purchase.order"].sudo().search([
            ("portfolio_source_sale_id", "=", order.id), ("company_id", "=", company.id),
        ], order="id")
        sales_moves = env["stock.move"].sudo().search([
            ("sale_line_id", "in", sources.ids), ("company_id", "=", company.id),
        ])
        receipt_moves = env["stock.move"].sudo().search([
            ("purchase_line_id.order_id", "in", purchases.ids), ("company_id", "=", company.id),
        ]) if purchases else env["stock.move"]
        moves = sales_moves | receipt_moves
        returns = moves.filtered("origin_returned_move_id")
        receipts = (receipt_moves - returns).picking_id
        deliveries = (sales_moves - returns).filtered(
            lambda move: move.location_dest_id._is_outgoing() and not move.location_id._is_outgoing(),
        ).picking_id
        pickings = receipts | deliveries | returns.picking_id
        documents = {
            "purchase_order_ids": purchases, "receipt_picking_ids": receipts,
            "delivery_picking_ids": deliveries, "backorder_picking_ids": pickings.filtered("backorder_id"),
            "return_picking_ids": returns.picking_id, "return_move_ids": returns,
        }
        for name, records in documents.items():
            values[name] = [Command.set(_readable_documents(records, env, company).ids)]

        if env.user.has_group(BILLING):
            # Billing and every discovered native record/source are authorized
            # before financial_facts or journal payloads are read with elevation.
            entries = env["account.move.line"].sudo().search([
                ("sale_line_ids", "in", sources.ids), ("display_type", "=", "product"),
                ("move_id.move_type", "in", ["out_invoice", "out_refund"]),
            ])
            entries.with_user(env.uid).check_access("read")
            invoices = entries.move_id | entries.move_id.reversed_entry_id
            invoices.with_user(env.uid).check_access("read")
            authorize_companies((entries.company_id | invoices.company_id).with_env(env))
            _authorized_sources(entries.sale_line_ids.with_env(env))
            if entries.company_id - company or invoices.company_id - company:
                raise UserError(_("Financial trace records must share the sales source company."))
            facts = financial_facts(sources)
            if any(fact["invalid_line_ids"] for fact in facts.values()):
                raise UserError(_("Malformed active invoice facts prevent a financial trace."))
            amounts = {source.id: 0.0 for source in sources}
            for entry in entries.filtered(lambda entry: entry.move_id.state != "cancel"):
                if len(entry.sale_line_ids) != 1 or entry.product_id != entry.sale_line_ids.product_id:
                    raise UserError(_("A financial trace requires one unambiguous source per product journal line."))
                if entry.move_id.state == "posted":
                    amounts[entry.sale_line_ids.id] -= entry.balance
            for source_id, line in line_values.items():
                fact = facts[source_id]
                line.update(
                    draft_invoice_qty=fact["invoice_reserved"] - fact["invoice_posted"],
                    posted_invoice_qty=fact["invoice_posted"],
                    draft_credit_qty=fact["credit_reserved"] - fact["credit_posted"],
                    posted_credit_qty=fact["credit_posted"], posted_signed_amount=amounts[source_id],
                )
            values["posted_signed_amount"] = sum(amounts.values())
            values["invoice_ids"] = [Command.set(_readable_documents(
                entries.move_id.filtered(lambda move: move.move_type == "out_invoice"), env, company,
            ).ids)]
            values["credit_note_ids"] = [Command.set(_readable_documents(
                entries.move_id.filtered(lambda move: move.move_type == "out_refund"), env, company,
            ).ids)]
        quantity_names = PHYSICAL_QUANTITIES + (FINANCIAL_QUANTITIES if env.user.has_group(BILLING) else ())
        for name in quantity_names:
            values[name] = 0.0 if values["has_mixed_uom"] else sum(line.get(name, 0.0) for line in line_values.values())
        return values, list(line_values.values())

    @api.model
    def default_get(self, fields_list):
        actor = _trace_actor(self)
        # Display defaults, including ir.default, are never snapshot inputs.
        values = {}
        source_id = actor.env.context.get("default_sale_order_id")
        if source_id:
            snapshot, _lines = actor._snapshot(source_id)
            values.update({name: value for name, value in snapshot.items() if name in fields_list})
        return values

    @api.model_create_multi
    def create(self, vals_list):
        actor = _trace_actor(self)
        actor.check_access("create")
        prepared = []
        for values in vals_list:
            if set(values) - {"sale_order_id"}:
                raise AccessError(_("The sales order is the only fulfillment trace input."))
            source_id = values.get("sale_order_id", actor.env.context.get("default_sale_order_id"))
            if not source_id:
                raise UserError(_("Select a sales order to trace."))
            prepared.append(actor._snapshot(source_id))
        actor = actor.with_context({"allowed_company_ids": actor.env.companies.ids})
        with actor.env.cr.savepoint():
            traces = super(FulfillmentTrace, actor).create([values for values, _lines in prepared])
            line_model = actor.env["portfolio.fulfillment.trace.line"].sudo()
            lines = [
                {**line, "trace_id": trace.id}
                for trace, (_values, line_values) in zip(traces, prepared) for line in line_values
            ]
            if lines:
                super(FulfillmentTraceLine, line_model).create(lines)
            return traces


class FulfillmentTraceLine(ReadOnlyTrace, models.TransientModel):
    _name = "portfolio.fulfillment.trace.line"
    _description = "Read-only physical sales source trace"
    _rec_name = "product_id"
    _order = "source_line_id, id"

    trace_id = fields.Many2one("portfolio.fulfillment.trace", required=True, readonly=True, ondelete="cascade")
    source_line_id = fields.Many2one("sale.order.line", required=True, readonly=True, ondelete="cascade")
    company_id = fields.Many2one("res.company", required=True, readonly=True)
    currency_id = fields.Many2one("res.currency", required=True, readonly=True)
    product_id = fields.Many2one("product.product", required=True, readonly=True)
    product_uom_id = fields.Many2one("uom.uom", required=True, readonly=True, string="Unit")
    gross_delivered_qty = fields.Float(readonly=True, string="Gross delivered")
    returned_qty = fields.Float(readonly=True, string="Physically returned")
    net_delivered_qty = fields.Float(readonly=True, string="Net delivered")
    draft_invoice_qty = fields.Float(readonly=True, groups=BILLING, string="Draft invoiced")
    posted_invoice_qty = fields.Float(readonly=True, groups=BILLING, string="Posted invoiced")
    draft_credit_qty = fields.Float(readonly=True, groups=BILLING, string="Draft credited")
    posted_credit_qty = fields.Float(readonly=True, groups=BILLING, string="Posted credited")
    posted_signed_amount = fields.Monetary(readonly=True, groups=BILLING, string="Net posted product amount")

    def _authorize_trace_source(self):
        parents = self.sudo().trace_id.with_user(self.env.uid)
        parents._authorize_trace_read()
        sources = _authorized_sources(self.sudo().source_line_id.with_user(self.env.uid))
        if sources.order_id - parents.sale_order_id:
            raise AccessError(_("Trace lines must belong to the authorized sales order."))

    @api.model_create_multi
    def create(self, vals_list):
        raise AccessError(_("Trace lines can only be derived when opening a sales order trace."))


class SaleOrder(models.Model):
    _inherit = "sale.order"

    def action_portfolio_trace(self):
        order, _lines = _trace_source(self)
        trace = order.env["portfolio.fulfillment.trace"].create({"sale_order_id": order.id})
        view = order.env.ref("portfolio_trade.fulfillment_trace_form")
        return {
            "type": "ir.actions.act_window", "name": _("Fulfillment trace"),
            "res_model": trace._name, "res_id": trace.id, "view_mode": "form",
            "view_id": view.id, "views": [(view.id, "form")], "target": "new",
            "context": {"allowed_company_ids": order.env.companies.ids, "create": False, "edit": False, "delete": False},
        }
