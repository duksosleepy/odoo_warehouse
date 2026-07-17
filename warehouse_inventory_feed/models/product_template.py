from odoo import fields, models


class ProductTemplate(models.Model):
    _inherit = "product.template"

    stock_warehouse_ids = fields.Many2many(
        "stock.warehouse",
        "product_template_stock_warehouse_rel",
        "product_tmpl_id",
        "warehouse_id",
        string="Stock Warehouses",
        help=(
            "Compatibility field for older module versions. Native stock is "
            "managed by stock.quant per warehouse location."
        ),
    )
