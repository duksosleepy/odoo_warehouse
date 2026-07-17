{
    "name": "Warehouse Inventory Feed Sync",
    "summary": "Synchronize store inventory quantities from an external inventory feed",
    "version": "19.0.1.0.0",
    "category": "Inventory/Inventory",
    "license": "LGPL-3",
    "author": "Custom",
    "depends": ["base_import", "directus_connector", "stock"],
    "data": [
        "security/ir.model.access.csv",
        "data/ir_config_parameter.xml",
        "data/ir_cron.xml",
        "views/inventory_store_views.xml",
        "views/inventory_sync_log_views.xml",
        "views/res_config_settings_views.xml",
    ],
    "installable": True,
    "application": False,
}
