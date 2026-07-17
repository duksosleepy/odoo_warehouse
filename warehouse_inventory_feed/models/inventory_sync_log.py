import logging
from datetime import date

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from odoo.addons.directus_connector.directus import DirectusClient as ApiClient

_logger = logging.getLogger(__name__)


FEED_FIELDS = (
    "id",
    "Ngay_Ct",
    "Ma_Kho",
    "Ma_Vt",
    "Ten_Vt",
    "Ton_Cuoi",
    "Trang_Thai",
    "Dia_Chi",
    "Ten_Cua_Hang",
)


class InventoryFeedSyncLog(models.Model):
    _name = "inventory.feed.sync.log"
    _description = "Inventory Feed Sync Log"
    _order = "started_at desc, id desc"

    name = fields.Char(default=lambda self: _("Inventory Feed Sync"), required=True)
    sync_type = fields.Selection(
        selection=[("stores", "Stores"), ("inventory", "Inventory")],
        required=True,
        default="inventory",
    )
    state = fields.Selection(
        selection=[("running", "Running"), ("done", "Done"), ("failed", "Failed")],
        required=True,
        default="running",
        index=True,
    )
    started_at = fields.Datetime(default=fields.Datetime.now, required=True)
    finished_at = fields.Datetime(readonly=True)
    total_feed_records = fields.Integer(readonly=True)
    unique_inventory_records = fields.Integer(readonly=True)
    created_store_count = fields.Integer(readonly=True)
    updated_store_count = fields.Integer(readonly=True)
    created_location_count = fields.Integer(readonly=True)
    created_product_count = fields.Integer(readonly=True)
    updated_quant_count = fields.Integer(readonly=True)
    unchanged_quant_count = fields.Integer(readonly=True)
    skipped_count = fields.Integer(readonly=True)
    error_count = fields.Integer(readonly=True)
    message = fields.Text(readonly=True)
    error_message = fields.Text(readonly=True)

    @api.model
    def cron_sync_inventory(self):
        self.action_run_inventory_sync()

    @api.model
    def action_run_store_sync(self):
        log = self.create({"sync_type": "stores", "name": _("Store Feed Sync")})
        log._run_sync(stores_only=True)
        return log._action_open()

    @api.model
    def action_run_inventory_sync(self):
        log = self.create({"sync_type": "inventory", "name": _("Inventory Feed Sync")})
        log._run_sync(stores_only=False)
        return log._action_open()

    def _action_open(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.display_name,
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
        }

    def _run_sync(self, *, stores_only):
        self.ensure_one()
        try:
            rows, stats = self._fetch_rows()
            store_stats = self._sync_stores(rows)
            stats.update(store_stats)
            if not stores_only:
                inventory_stats = self._sync_inventory(rows)
                inventory_stats["skipped_count"] += stats.get("skipped_count", 0)
                stats.update(inventory_stats)
            self.write(
                {
                    **stats,
                    "state": "done",
                    "finished_at": fields.Datetime.now(),
                    "message": self._summary_message(stats, stores_only=stores_only),
                }
            )
        except Exception as exc:  # noqa: BLE001 - this is a cron boundary.
            _logger.exception("Inventory feed synchronization failed")
            self.write(
                {
                    "state": "failed",
                    "finished_at": fields.Datetime.now(),
                    "error_message": str(exc),
                    "error_count": 1,
                }
            )

    def _summary_message(self, stats, *, stores_only):
        if stores_only:
            return _(
                "Processed %(total)s feed rows. Created %(created)s stores and updated %(updated)s stores.",
                total=stats.get("total_feed_records", 0),
                created=stats.get("created_store_count", 0),
                updated=stats.get("updated_store_count", 0),
            )
        return _(
            "Processed %(total)s feed rows into %(unique)s store/SKU balances. Updated %(quants)s quants, left %(unchanged)s unchanged, skipped %(skipped)s.",
            total=stats.get("total_feed_records", 0),
            unique=stats.get("unique_inventory_records", 0),
            quants=stats.get("updated_quant_count", 0),
            unchanged=stats.get("unchanged_quant_count", 0),
            skipped=stats.get("skipped_count", 0),
        )

    @api.model
    def _fetch_rows(self):
        rows_by_key = {}
        total = 0
        skipped = 0
        collection = self._config("collection", default="tmdt_inventory_status")
        page_size = self._config_int("page_size", default=500)

        with self._api_client() as client:
            for row in client.iter_items(
                collection,
                fields=FEED_FIELDS,
                page_size=page_size,
                params={"sort": "Ma_Kho,Ma_Vt,-Ngay_Ct"},
            ):
                total += 1
                code = self._clean(row.get("Ma_Kho"))
                sku = self._clean(row.get("Ma_Vt"))
                quantity = self._parse_quantity(row.get("Ton_Cuoi"))
                if not code or not sku or quantity is None:
                    skipped += 1
                    continue
                row["_sync_code"] = code
                row["_sync_sku"] = sku
                row["_sync_quantity"] = quantity
                key = (code, sku)
                previous = rows_by_key.get(key)
                if previous is None or self._row_date_key(row) >= self._row_date_key(previous):
                    rows_by_key[key] = row

        return list(rows_by_key.values()), {
            "total_feed_records": total,
            "unique_inventory_records": len(rows_by_key),
            "skipped_count": skipped,
        }

    @api.model
    def _api_client(self):
        base_url = self._config("base_url", default="https://di.lug.info.vn")
        token = self._config("token")
        if not token:
            raise UserError(_("Set the inventory feed access token in Inventory settings."))
        return ApiClient(
            base_url,
            token,
            auth_mode=self._config("auth_mode", default="access_token"),
            timeout=60.0,
        )

    def _sync_stores(self, rows):
        Store = self.env["inventory.feed.store"].sudo()
        now = fields.Datetime.now()
        auto_create_stores = self._config_bool("auto_create_stores", default=True)
        auto_create_locations = self._config_bool("auto_create_locations", default=True)
        stores_payload = {}

        for row in rows:
            code = row["_sync_code"]
            payload = stores_payload.setdefault(
                code,
                {
                    "code": code,
                    "name": False,
                    "address": False,
                    "last_inventory_date": False,
                    "last_source_id": False,
                },
            )
            name = self._clean(row.get("Ten_Cua_Hang"))
            address = self._clean(row.get("Dia_Chi"))
            if name and not payload["name"]:
                payload["name"] = name
            if address and not payload["address"]:
                payload["address"] = address
            row_date = self._to_date(row.get("Ngay_Ct"))
            if row_date and (
                not payload["last_inventory_date"] or row_date > payload["last_inventory_date"]
            ):
                payload["last_inventory_date"] = row_date
                payload["last_source_id"] = row.get("id")

        existing = {
            store.code: store
            for store in Store.with_context(active_test=False).search(
                [("code", "in", list(stores_payload))]
            )
        }
        created = updated = locations = 0
        parent_location = None

        for code, payload in stores_payload.items():
            store = existing.get(code)
            vals = {
                "name": payload["name"] or code,
                "address": payload["address"] or False,
                "last_seen_at": now,
                "last_inventory_date": payload["last_inventory_date"],
                "last_source_id": payload["last_source_id"],
            }
            if store:
                store.write(vals)
                updated += 1
            elif auto_create_stores:
                store = Store.create({"code": code, **vals})
                existing[code] = store
                created += 1
            else:
                continue

            if auto_create_locations and store and not store.location_id:
                parent_location = parent_location or self._default_parent_location()
                if parent_location:
                    store.location_id = self._create_store_location(store, parent_location)
                    locations += 1

        return {
            "created_store_count": created,
            "updated_store_count": updated,
            "created_location_count": locations,
        }

    def _sync_inventory(self, rows):
        Product = self.env["product.product"].sudo().with_context(active_test=False)
        Quant = self.env["stock.quant"].sudo()
        Store = self.env["inventory.feed.store"].sudo().with_context(active_test=False)

        codes = sorted({row["_sync_code"] for row in rows})
        skus = sorted({row["_sync_sku"] for row in rows})
        stores_by_code = {
            store.code: store
            for store in Store.search([("code", "in", codes)])
            if store.active and store.location_id
        }
        products_by_sku, created_products = self._get_products_by_sku(skus, rows)
        target_by_key = {}
        skipped = 0

        for row in rows:
            store = stores_by_code.get(row["_sync_code"])
            product = products_by_sku.get(row["_sync_sku"])
            if not store or not product:
                skipped += 1
                continue
            if product.tracking != "none" or not product.product_tmpl_id.is_storable:
                skipped += 1
                continue
            target_by_key[(product.id, store.location_id.id)] = (
                product,
                store.location_id,
                row["_sync_quantity"],
            )

        quant_by_key = {}
        if target_by_key:
            product_ids = list({key[0] for key in target_by_key})
            location_ids = list({key[1] for key in target_by_key})
            quant_domain = [
                ("product_id", "in", product_ids),
                ("location_id", "in", location_ids),
                ("lot_id", "=", False),
                ("package_id", "=", False),
                ("owner_id", "=", False),
            ]
            for quant in Quant.search(quant_domain):
                quant_by_key.setdefault((quant.product_id.id, quant.location_id.id), quant)

        to_apply = Quant.browse()
        unchanged = 0
        for key, (product, location, quantity) in target_by_key.items():
            quant = quant_by_key.get(key)
            if quant and product.uom_id.compare(quant.quantity, quantity) == 0:
                unchanged += 1
                continue
            if quant:
                quant.with_context(inventory_mode=True).write({"inventory_quantity": quantity})
            else:
                quant = Quant.with_context(inventory_mode=True).create(
                    {
                        "product_id": product.id,
                        "location_id": location.id,
                        "inventory_quantity": quantity,
                    }
                )
            to_apply |= quant

        for quant_ids in self._chunks(to_apply.ids, 500):
            Quant.browse(quant_ids)._apply_inventory()

        return {
            "created_product_count": created_products,
            "updated_quant_count": len(to_apply),
            "unchanged_quant_count": unchanged,
            "skipped_count": self.skipped_count + skipped,
        }

    def _get_products_by_sku(self, skus, rows):
        Product = self.env["product.product"].sudo().with_context(active_test=False)
        Template = self.env["product.template"].sudo()
        products_by_sku = {}
        for sku_chunk in self._chunks(skus, 1000):
            for product in Product.search([("default_code", "in", list(sku_chunk))]):
                products_by_sku.setdefault(product.default_code, product)

        missing_skus = [sku for sku in skus if sku not in products_by_sku]
        if not missing_skus or not self._config_bool("auto_create_products", default=True):
            return products_by_sku, 0

        name_by_sku = {}
        for row in rows:
            sku = row["_sync_sku"]
            name = self._clean(row.get("Ten_Vt"))
            if sku and name and sku not in name_by_sku:
                name_by_sku[sku] = name

        vals_list = [
            {
                "name": name_by_sku.get(sku) or sku,
                "default_code": sku,
                "type": "consu",
                "is_storable": True,
            }
            for sku in missing_skus
        ]
        templates = Template.create(vals_list)
        for template, sku in zip(templates, missing_skus):
            product = template.product_variant_id
            if product.default_code != sku:
                product.default_code = sku
            products_by_sku[sku] = product
        return products_by_sku, len(missing_skus)

    def _default_parent_location(self):
        warehouse = self.env["stock.warehouse"].sudo().search(
            [("company_id", "=", self.env.company.id)], limit=1
        )
        if warehouse:
            return warehouse.lot_stock_id
        location = self.env.ref("stock.stock_location_stock", raise_if_not_found=False)
        if location:
            return location
        return self.env["stock.location"].sudo().search([("usage", "=", "internal")], limit=1)

    def _create_store_location(self, store, parent_location):
        name = store.display_name or store.code
        return self.env["stock.location"].sudo().create(
            {
                "name": name,
                "usage": "internal",
                "location_id": parent_location.id,
                "company_id": parent_location.company_id.id or self.env.company.id,
            }
        )

    @api.model
    def _config(self, key, default=False):
        return (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param(f"warehouse_inventory_feed.{key}", default)
        )

    @api.model
    def _config_bool(self, key, default=False):
        value = self._config(key, default="1" if default else "0")
        return value in (True, "1", "True", "true", "yes", "on")

    @api.model
    def _config_int(self, key, default=0):
        value = self._config(key, default=str(default))
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _clean(value):
        if value is None:
            return False
        value = str(value).strip()
        return value or False

    @staticmethod
    def _parse_quantity(value):
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_date(value):
        if not value:
            return False
        return fields.Date.to_date(value)

    def _row_date_key(self, row):
        row_date = self._to_date(row.get("Ngay_Ct")) or date.min
        return row_date, row.get("id") or ""

    @staticmethod
    def _chunks(values, size):
        values = list(values)
        for index in range(0, len(values), size):
            yield values[index : index + size]
