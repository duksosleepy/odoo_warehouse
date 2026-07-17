from odoo import fields, models


class StockWarehouse(models.Model):
    _inherit = "stock.warehouse"

    code = fields.Char(
        "Short Name",
        required=True,
        size=64,
        help="Short name used to identify your warehouse",
    )
