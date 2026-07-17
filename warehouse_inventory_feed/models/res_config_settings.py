from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    inventory_feed_base_url = fields.Char(
        string="API URL",
        config_parameter="warehouse_inventory_feed.base_url",
    )
    inventory_feed_collection = fields.Char(
        string="Inventory Collection",
        config_parameter="warehouse_inventory_feed.collection",
    )
    inventory_feed_token = fields.Char(
        string="Access Token",
        config_parameter="warehouse_inventory_feed.token",
    )
    inventory_feed_auth_mode = fields.Selection(
        selection=[
            ("access_token", "Access Token Query Parameter"),
            ("bearer", "Bearer Header"),
        ],
        string="Authentication",
        default="access_token",
        config_parameter="warehouse_inventory_feed.auth_mode",
    )
    inventory_feed_page_size = fields.Integer(
        string="Page Size",
        default=500,
        config_parameter="warehouse_inventory_feed.page_size",
    )
    inventory_feed_auto_create_stores = fields.Boolean(
        string="Create Missing Warehouses",
        default=True,
        config_parameter="warehouse_inventory_feed.auto_create_stores",
    )
    inventory_feed_auto_create_products = fields.Boolean(
        string="Create Missing Products",
        default=True,
        config_parameter="warehouse_inventory_feed.auto_create_products",
    )

    def action_sync_stores(self):
        self.ensure_one()
        self.set_values()
        return self.env["inventory.feed.sync.log"].action_run_store_sync()

    def action_sync_inventory(self):
        self.ensure_one()
        self.set_values()
        return self.env["inventory.feed.sync.log"].action_run_inventory_sync()
