# Odoo Warehouse Inventory Feed

This addons repo contains two Odoo 19 modules:

- `directus_connector`: reusable synchronous Directus client built on `httpx`.
- `warehouse_inventory_feed`: syncs inventory feed rows into Odoo store locations and stock quants.

Install the Python dependency before installing the addons:

```bash
pip install -r requirements.txt
```

After installing the Odoo modules, open Inventory > Configuration > Settings and set the feed access token. The LUG source expects this token through the `access_token` query parameter, so leave Authentication set to `Access Token Query Parameter`.

The scheduled action `Inventory Feed: daily sync` runs once per day at midnight. Stores are managed from Inventory > Configuration > Stores.
