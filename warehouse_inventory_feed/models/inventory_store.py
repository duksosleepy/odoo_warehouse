from odoo import api, fields, models


class InventoryFeedStore(models.Model):
    _name = "inventory.feed.store"
    _description = "Inventory Feed Store"
    _order = "code"

    code = fields.Char(required=True, index=True)
    name = fields.Char()
    address = fields.Char()
    location_id = fields.Many2one(
        "stock.location",
        string="Stock Location",
        domain=[("usage", "=", "internal")],
        ondelete="restrict",
        help="Internal Odoo location whose on-hand quantity is synchronized from this store feed.",
    )
    warehouse_id = fields.Many2one(
        "stock.warehouse",
        string="Warehouse",
        ondelete="restrict",
        help="Odoo warehouse represented by this store feed code.",
    )
    active = fields.Boolean(default=True)
    last_seen_at = fields.Datetime(readonly=True)
    last_inventory_date = fields.Date(readonly=True)
    last_source_id = fields.Char(readonly=True)

    _sql_constraints = [
        ("code_unique", "unique(code)", "The store feed code must be unique."),
    ]

    @api.depends("code", "name")
    def _compute_display_name(self):
        for store in self:
            if store.name and store.name != store.code:
                store.display_name = f"{store.code} - {store.name}"
            else:
                store.display_name = store.code

    def action_open_location(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.location_id.display_name,
            "res_model": "stock.location",
            "res_id": self.location_id.id,
            "view_mode": "form",
        }

    def action_open_warehouse(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.warehouse_id.display_name,
            "res_model": "stock.warehouse",
            "res_id": self.warehouse_id.id,
            "view_mode": "form",
        }

    @api.model
    def action_sync_from_feed(self):
        return self.env["inventory.feed.sync.log"].action_run_store_sync()
