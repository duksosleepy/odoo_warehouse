import logging
import time
from collections import defaultdict
from datetime import date

from psycopg2 import Error as Psycopg2Error

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
        selection=[
            ("stores", "Warehouses"),
            ("products", "Products"),
            ("inventory", "Inventory"),
        ],
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
    unique_product_records = fields.Integer(readonly=True)
    total_product_quantity = fields.Float(readonly=True)
    created_store_count = fields.Integer(string="Created Warehouses", readonly=True)
    updated_store_count = fields.Integer(string="Updated Warehouses", readonly=True)
    created_location_count = fields.Integer(string="Created Locations", readonly=True)
    created_product_count = fields.Integer(string="Created Product Templates", readonly=True)
    created_variant_count = fields.Integer(readonly=True)
    updated_variant_count = fields.Integer(readonly=True)
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
    def cron_sync_products(self):
        log = self.search(
            [("sync_type", "=", "products"), ("state", "=", "running")],
            order="started_at desc, id desc",
            limit=1,
        )
        if log:
            log._run_product_sync()

    @api.model
    def action_run_store_sync(self):
        log = self.create({"sync_type": "stores", "name": _("Warehouse Feed Sync")})
        log._run_sync(stores_only=True)
        return log._action_open()

    @api.model
    def action_run_product_sync(self):
        log = self.search(
            [("sync_type", "=", "products"), ("state", "=", "running")],
            order="started_at desc, id desc",
            limit=1,
        )
        if not log:
            log = self.create({"sync_type": "products", "name": _("Product Feed Sync")})
            self._set_config("product_sync_offset", 0)
        log._run_product_sync()
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
            if isinstance(exc, Psycopg2Error):
                raise
            _logger.exception("Inventory feed synchronization failed")
            self.write(
                {
                    "state": "failed",
                    "finished_at": fields.Datetime.now(),
                    "error_message": str(exc),
                    "error_count": 1,
                }
            )

    def _run_product_sync(self):
        self.ensure_one()
        try:
            stats = self._run_product_sync_batch()
            state = stats.pop("state")
            finished_at = stats.pop("finished_at", False)
            message = self._summary_message(stats, products_only=True)
            stats.pop("product_sync_incomplete", None)
            self.write(
                {
                    **stats,
                    "state": state,
                    "finished_at": finished_at,
                    "message": message,
                }
            )
        except Exception as exc:  # noqa: BLE001 - this is a cron/manual boundary.
            if isinstance(exc, Psycopg2Error):
                raise
            _logger.exception("Product feed synchronization failed")
            self.write(
                {
                    "state": "failed",
                    "finished_at": fields.Datetime.now(),
                    "error_message": str(exc),
                    "error_count": 1,
                }
            )
            self._set_config("product_sync_offset", 0)

    def _summary_message(self, stats, *, stores_only=False, products_only=False):
        if products_only:
            if stats.get("product_sync_incomplete"):
                return _(
                    "Processed %(processed)s of %(total)s distinct SKUs so far with total quantity %(quantity)s. Created %(templates)s product templates, created %(variants)s variants, updated %(updated)s variants, skipped %(skipped)s. The background cron will continue the remaining batches.",
                    processed=stats.get("unique_product_records", 0),
                    total=stats.get("total_feed_records", 0),
                    quantity=stats.get("total_product_quantity", 0.0),
                    templates=stats.get("created_product_count", 0),
                    variants=stats.get("created_variant_count", 0),
                    updated=stats.get("updated_variant_count", 0),
                    skipped=stats.get("skipped_count", 0),
                )
            return _(
                "Processed %(total)s feed rows into %(unique)s distinct SKUs with total quantity %(quantity)s. Created %(templates)s product templates, created %(variants)s variants, updated %(updated)s variants, skipped %(skipped)s.",
                total=stats.get("total_feed_records", 0),
                unique=stats.get("unique_product_records", 0),
                quantity=stats.get("total_product_quantity", 0.0),
                templates=stats.get("created_product_count", 0),
                variants=stats.get("created_variant_count", 0),
                updated=stats.get("updated_variant_count", 0),
                skipped=stats.get("skipped_count", 0),
            )
        if stores_only:
            return _(
                "Processed %(total)s feed rows. Created %(created)s warehouses and updated %(updated)s warehouses.",
                total=stats.get("total_feed_records", 0),
                created=stats.get("created_store_count", 0),
                updated=stats.get("updated_store_count", 0),
            )
        return _(
            "Processed %(total)s feed rows into %(unique)s warehouse/SKU balances. Updated %(quants)s quants, left %(unchanged)s unchanged, skipped %(skipped)s.",
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
                params={
                    "sort": "Ma_Kho,Ma_Vt,-Ngay_Ct",
                    "filter[Ma_Kho][_nempty]": "true",
                    "filter[Ma_Vt][_nempty]": "true",
                    "filter[Ton_Cuoi][_nnull]": "true",
                },
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
    def _fetch_product_rows(self):
        rows = []
        total_count = None
        skipped = 0
        offset = 0
        collection = self._config("collection", default="tmdt_inventory_status")
        page_size = self._config_int("page_size", default=500)
        page_size = max(int(page_size or 500), 1)
        params = {
            "aggregate[sum]": "Ton_Cuoi",
            "groupBy[]": "Ma_Vt",
            "filter[Ma_Vt][_nempty]": "true",
            "filter[Ton_Cuoi][_nnull]": "true",
            "sort": "Ma_Vt",
        }

        with self._api_client() as client:
            while True:
                request_params = {
                    **params,
                    "limit": page_size,
                    "offset": offset,
                }
                if total_count is None:
                    request_params["meta"] = "total_count"

                payload = client.get_items(collection, params=request_params)
                if total_count is None:
                    meta = payload.get("meta") or {}
                    total_count = meta.get("total_count")

                page_rows = payload.get("data") or []
                if not isinstance(page_rows, list):
                    raise UserError(_("Directus product aggregate payload is not a list."))

                for row in page_rows:
                    if not isinstance(row, dict):
                        skipped += 1
                        continue
                    sku = self._clean(row.get("Ma_Vt"))
                    quantity = self._parse_aggregate_quantity(row)
                    if not sku or quantity is None:
                        skipped += 1
                        continue
                    row["_sync_sku"] = sku
                    row["_sync_quantity"] = quantity
                    rows.append(row)

                row_count = len(page_rows)
                offset += row_count
                if not row_count or row_count < page_size:
                    break
                if total_count is not None and offset >= int(total_count):
                    break

        return rows, {
            "total_feed_records": total_count or len(rows),
            "unique_product_records": len(rows),
            "skipped_count": skipped,
        }

    def _run_product_sync_batch(self):
        offset = self._config_int("product_sync_offset", default=0)
        page_size = self._config_int("page_size", default=500)
        batch_pages = max(self._config_int("product_sync_batch_pages", default=1), 1)
        deadline = time.monotonic() + max(
            self._config_int("product_sync_time_budget", default=45), 5
        )

        total_count = self.total_feed_records or None
        processed = self.unique_product_records or 0
        total_quantity = self.total_product_quantity or 0.0
        created_templates = self.created_product_count or 0
        created_variants = self.created_variant_count or 0
        updated_variants = self.updated_variant_count or 0
        skipped = self.skipped_count or 0
        pages_done = 0
        done = False

        while pages_done < batch_pages and time.monotonic() < deadline:
            rows, page_stats = self._fetch_product_rows_page(
                offset,
                page_size,
                include_meta=total_count is None,
            )
            if total_count is None:
                total_count = page_stats.get("total_feed_records") or 0

            row_count = page_stats.get("page_row_count", 0)
            skipped += page_stats.get("skipped_count", 0)
            if not row_count:
                done = True
                break

            product_stats = self._sync_products(rows)
            processed += product_stats.get("unique_product_records", 0)
            total_quantity += product_stats.get("total_product_quantity", 0.0)
            created_templates += product_stats.get("created_product_count", 0)
            created_variants += product_stats.get("created_variant_count", 0)
            updated_variants += product_stats.get("updated_variant_count", 0)
            skipped += product_stats.get("skipped_count", 0)

            offset += row_count
            pages_done += 1
            if total_count and offset >= total_count:
                done = True
                break

        if done:
            self._set_config("product_sync_offset", 0)
        else:
            self._set_config("product_sync_offset", offset)

        return {
            "state": "done" if done else "running",
            "finished_at": fields.Datetime.now() if done else False,
            "total_feed_records": total_count or processed,
            "unique_product_records": processed,
            "total_product_quantity": total_quantity,
            "created_product_count": created_templates,
            "created_variant_count": created_variants,
            "updated_variant_count": updated_variants,
            "skipped_count": skipped,
            "product_sync_incomplete": not done,
        }

    @api.model
    def _fetch_product_rows_page(self, offset, page_size, *, include_meta=False):
        rows = []
        skipped = 0
        collection = self._config("collection", default="tmdt_inventory_status")
        request_params = {
            "aggregate[sum]": "Ton_Cuoi",
            "groupBy[]": "Ma_Vt",
            "filter[Ma_Vt][_nempty]": "true",
            "filter[Ton_Cuoi][_nnull]": "true",
            "sort": "Ma_Vt",
            "limit": max(int(page_size or 500), 1),
            "offset": max(int(offset or 0), 0),
        }
        if include_meta:
            request_params["meta"] = "total_count"

        with self._api_client() as client:
            payload = client.get_items(collection, params=request_params)

        page_rows = payload.get("data") or []
        if not isinstance(page_rows, list):
            raise UserError(_("Directus product aggregate payload is not a list."))

        for row in page_rows:
            if not isinstance(row, dict):
                skipped += 1
                continue
            sku = self._clean(row.get("Ma_Vt"))
            quantity = self._parse_aggregate_quantity(row)
            if not sku or quantity is None:
                skipped += 1
                continue
            row["_sync_sku"] = sku
            row["_sync_quantity"] = quantity
            rows.append(row)

        total_count = None
        if include_meta:
            meta = payload.get("meta") or {}
            total_count = meta.get("total_count")

        return rows, {
            "total_feed_records": total_count,
            "page_row_count": len(page_rows),
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

        existing_stores = {
            store.code: store
            for store in Store.with_context(active_test=False).search(
                [("code", "in", list(stores_payload))]
            )
        }
        existing_warehouses = self._get_existing_warehouses(stores_payload)
        created = updated = 0

        for code, payload in stores_payload.items():
            store = existing_stores.get(code)
            if not store and not auto_create_stores:
                continue

            vals = {
                "name": payload["name"] or code,
                "address": payload["address"] or False,
                "last_seen_at": now,
                "last_inventory_date": payload["last_inventory_date"],
                "last_source_id": payload["last_source_id"],
            }
            warehouse = self._sync_store_warehouse(code, existing_warehouses)
            if warehouse:
                vals["warehouse_id"] = warehouse.id
                vals["location_id"] = warehouse.lot_stock_id.id
            if store:
                store.write(vals)
                updated += 1
            elif auto_create_stores:
                store = Store.create({"code": code, **vals})
                existing_stores[code] = store
                created += 1

        return {
            "created_store_count": created,
            "updated_store_count": updated,
            "created_location_count": 0,
        }

    def _get_existing_warehouses(self, stores_payload):
        Warehouse = self.env["stock.warehouse"].sudo().with_context(active_test=False)
        codes = list(stores_payload)
        warehouses = Warehouse.search([
            ("company_id", "=", self.env.company.id),
            "|",
            ("code", "in", codes),
            ("name", "in", codes),
        ])
        existing = {}
        for warehouse in warehouses:
            if warehouse.code in stores_payload:
                existing[warehouse.code] = warehouse
            if warehouse.name in stores_payload:
                existing.setdefault(warehouse.name, warehouse)
        return existing

    def _sync_store_warehouse(self, code, existing_warehouses):
        Warehouse = self.env["stock.warehouse"].sudo().with_context(active_test=False)
        warehouse = existing_warehouses.get(code)
        vals = {
            "name": code,
            "code": code,
        }
        if warehouse:
            warehouse.write(vals)
            return warehouse
        warehouse = Warehouse.create({
            **vals,
            "company_id": self.env.company.id,
        })
        existing_warehouses[code] = warehouse
        return warehouse

    def _sync_inventory(self, rows):
        Product = self.env["product.product"].sudo().with_context(active_test=False)
        Quant = self.env["stock.quant"].sudo()

        codes = sorted({row["_sync_code"] for row in rows})
        skus = sorted({row["_sync_sku"] for row in rows})
        Warehouse = self.env["stock.warehouse"].sudo().with_context(active_test=False)
        warehouses_by_code = {
            warehouse.code: warehouse
            for warehouse in Warehouse.search([
                ("company_id", "=", self.env.company.id),
                ("code", "in", codes),
            ])
            if warehouse.active and warehouse.lot_stock_id
        }
        products_by_sku, created_products = self._get_products_by_sku(skus, rows)
        target_by_key = {}
        skipped = 0

        for row in rows:
            warehouse = warehouses_by_code.get(row["_sync_code"])
            product = products_by_sku.get(row["_sync_sku"])
            if not warehouse or not product:
                skipped += 1
                continue
            if product.tracking != "none" or not product.product_tmpl_id.is_storable:
                skipped += 1
                continue
            target_by_key[(product.id, warehouse.lot_stock_id.id)] = (
                product,
                warehouse.lot_stock_id,
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
            "created_variant_count": created_products,
            "updated_quant_count": len(to_apply),
            "unchanged_quant_count": unchanged,
            "skipped_count": self.skipped_count + skipped,
        }

    def _get_products_by_sku(self, skus, rows):
        Product = self.env["product.product"].sudo().with_context(active_test=False)
        products_by_sku = {}
        for sku_chunk in self._chunks(skus, 1000):
            for product in Product.search([("default_code", "in", list(sku_chunk))]):
                products_by_sku.setdefault(product.default_code, product)

        missing_skus = [sku for sku in skus if sku not in products_by_sku]
        if not missing_skus or not self._config_bool("auto_create_products", default=True):
            return products_by_sku, 0

        product_stats = self._sync_products(rows, target_skus=set(missing_skus))
        for sku_chunk in self._chunks(missing_skus, 1000):
            for product in Product.search([("default_code", "in", list(sku_chunk))]):
                products_by_sku.setdefault(product.default_code, product)
        return products_by_sku, product_stats.get("created_variant_count", 0)

    def _sync_products(self, rows, target_skus=None):
        specs, skipped = self._prepare_product_specs(rows, target_skus=target_skus)
        stats = self._sync_product_specs(specs)
        return {
            **stats,
            "unique_product_records": len(specs),
            "total_product_quantity": sum(spec["quantity"] for spec in specs.values()),
            "skipped_count": skipped + stats.get("skipped_count", 0),
        }

    def _prepare_product_specs(self, rows, target_skus=None):
        target_skus = set(target_skus or [])
        specs = {}
        skipped = 0
        for row in rows:
            sku = row.get("_sync_sku") or self._clean(row.get("Ma_Vt"))
            if target_skus and sku not in target_skus:
                continue

            quantity = row.get("_sync_quantity")
            if quantity is None:
                quantity = self._parse_aggregate_quantity(row)
            if quantity is None:
                quantity = self._parse_quantity(row.get("Ton_Cuoi")) or 0.0

            parsed = self._parse_feed_sku(sku)
            if not parsed:
                skipped += 1
                continue

            spec = specs.setdefault(
                sku,
                {
                    **parsed,
                    "sku": sku,
                    "source_name": self._clean(row.get("Ten_Vt")),
                    "quantity": 0.0,
                },
            )
            spec["quantity"] += quantity
            if not spec["source_name"]:
                spec["source_name"] = self._clean(row.get("Ten_Vt"))
        return specs, skipped

    def _sync_product_specs(self, specs):
        Product = self.env["product.product"].sudo().with_context(active_test=False)
        Template = self.env["product.template"].sudo().with_context(active_test=False)

        skus = list(specs)
        existing_products_by_sku = {}
        for sku_chunk in self._chunks(skus, 1000):
            for product in Product.search([("default_code", "in", list(sku_chunk))]):
                existing_products_by_sku.setdefault(product.default_code, product)

        template_codes = sorted({spec["template_code"] for spec in specs.values()})
        templates_by_code = {
            template.name: template
            for template in Template.search([("name", "in", template_codes)])
        }
        for code_chunk in self._chunks(template_codes, 1000):
            for product in Product.search([("default_code", "in", list(code_chunk))]):
                templates_by_code.setdefault(product.default_code, product.product_tmpl_id)

        created_templates = created_variants = updated_variants = 0
        skipped = 0

        specs_by_template = defaultdict(list)
        for spec in specs.values():
            specs_by_template[spec["template_code"]].append(spec)

        for template_code, template_specs in specs_by_template.items():
            existing_template = templates_by_code.get(template_code)
            if not existing_template:
                existing_template = Template.create(
                    self._prepare_product_template_values(
                        template_specs[0],
                        specs=template_specs,
                    )
                )
                templates_by_code[template_code] = existing_template
                created_templates += 1

            created, updated, skipped_for_template = self._sync_template_variants(
                existing_template,
                template_specs,
                existing_products_by_sku,
            )
            created_variants += created
            updated_variants += updated
            skipped += skipped_for_template

        return {
            "created_product_count": created_templates,
            "created_variant_count": created_variants,
            "updated_variant_count": updated_variants,
            "skipped_count": skipped,
        }

    def _prepare_product_template_values(self, spec, specs=None):
        vals = {
            "name": spec["template_code"],
            "type": "consu",
            "is_storable": True,
            "sale_ok": True,
            "purchase_ok": True,
        }
        attribute_lines = self._prepare_initial_attribute_lines(specs or [spec])
        if attribute_lines:
            vals["attribute_line_ids"] = attribute_lines
        return vals

    def _prepare_initial_attribute_lines(self, specs):
        lines = []
        size_values = sorted({spec["size"] for spec in specs if spec["size"]})
        color_values = sorted({spec["color"] for spec in specs if spec["color"]})
        if size_values:
            size_attribute = self._get_variant_attribute("Size", display_type="select")
            values = self._ensure_attribute_values(size_attribute, size_values)
            lines.append(
                (
                    0,
                    0,
                    {
                        "attribute_id": size_attribute.id,
                        "value_ids": [(6, 0, [value.id for value in values.values()])],
                    },
                )
            )
        if color_values:
            color_attribute = self._get_variant_attribute("Color", display_type="color")
            values = self._ensure_attribute_values(color_attribute, color_values)
            lines.append(
                (
                    0,
                    0,
                    {
                        "attribute_id": color_attribute.id,
                        "value_ids": [(6, 0, [value.id for value in values.values()])],
                    },
                )
            )
        return lines

    def _sync_template_variants(self, template, specs, existing_products_by_sku):
        created = updated = skipped = 0
        size_values = sorted({spec["size"] for spec in specs if spec["size"]})
        color_values = sorted({spec["color"] for spec in specs if spec["color"]})

        attribute_values_by_key = {}
        if size_values:
            size_attribute = self._get_variant_attribute("Size", display_type="select")
            attribute_values_by_key["size"] = self._ensure_attribute_values(
                size_attribute, size_values
            )
            self._ensure_template_attribute_line(
                template, size_attribute, attribute_values_by_key["size"].values()
            )
        if color_values:
            color_attribute = self._get_variant_attribute("Color", display_type="color")
            attribute_values_by_key["color"] = self._ensure_attribute_values(
                color_attribute, color_values
            )
            self._ensure_template_attribute_line(
                template, color_attribute, attribute_values_by_key["color"].values()
            )

        template.invalidate_recordset(["attribute_line_ids"])
        for spec in specs:
            product = existing_products_by_sku.get(spec["sku"])
            if product:
                self._ensure_product_is_storable(product)
                continue

            if not spec["size"] and not spec["color"]:
                product = template.product_variant_id
                if product:
                    if product.default_code != spec["sku"]:
                        product.default_code = spec["sku"]
                        updated += 1
                    existing_products_by_sku[spec["sku"]] = product
                    continue
                skipped += 1
                continue

            combination = self.env["product.template.attribute.value"].sudo()
            if spec["size"]:
                size_value = attribute_values_by_key["size"].get(spec["size"])
                combination |= self._get_template_attribute_value(template, size_value)
            if spec["color"]:
                color_value = attribute_values_by_key["color"].get(spec["color"])
                combination |= self._get_template_attribute_value(template, color_value)
            if not combination:
                skipped += 1
                continue

            product = template._get_variant_for_combination(combination)
            if not product:
                product = template._create_product_variant(combination)
            if not product:
                skipped += 1
                continue

            if not product.default_code:
                created += 1
            elif product.default_code != spec["sku"]:
                updated += 1
            product.default_code = spec["sku"]
            self._ensure_product_is_storable(product)
            existing_products_by_sku[spec["sku"]] = product

        return created, updated, skipped

    def _get_variant_attribute(self, name, *, display_type):
        Attribute = self.env["product.attribute"].sudo().with_context(active_test=False)
        attribute = Attribute.search(
            [("name", "=", name), ("create_variant", "=", "dynamic")],
            limit=1,
        )
        if attribute:
            if not attribute.active:
                attribute.active = True
            return attribute
        return Attribute.create(
            {
                "name": name,
                "create_variant": "dynamic",
                "display_type": display_type,
            }
        )

    def _ensure_attribute_values(self, attribute, names):
        Value = self.env["product.attribute.value"].sudo().with_context(active_test=False)
        values_by_name = {
            value.name: value
            for value in Value.search(
                [("attribute_id", "=", attribute.id), ("name", "in", list(names))]
            )
        }
        for name in names:
            value = values_by_name.get(name)
            if not value:
                value = Value.create({"attribute_id": attribute.id, "name": name})
                values_by_name[name] = value
            elif not value.active:
                value.active = True
        return values_by_name

    def _ensure_template_attribute_line(self, template, attribute, values):
        Line = self.env["product.template.attribute.line"].sudo().with_context(
            active_test=False
        )
        values = self.env["product.attribute.value"].sudo().browse(
            [value.id for value in values]
        )
        line = Line.search(
            [
                ("product_tmpl_id", "=", template.id),
                ("attribute_id", "=", attribute.id),
            ],
            limit=1,
        )
        commands = [(4, value_id) for value_id in values.ids]
        if line:
            vals = {"value_ids": commands}
            if not line.active:
                vals["active"] = True
            line.write(vals)
        else:
            Line.create(
                {
                    "product_tmpl_id": template.id,
                    "attribute_id": attribute.id,
                    "value_ids": [(6, 0, values.ids)],
                }
            )

    def _get_template_attribute_value(self, template, value):
        line = template.attribute_line_ids.filtered(
            lambda item: item.attribute_id == value.attribute_id
        )
        return line.product_template_value_ids.filtered(
            lambda item: item.product_attribute_value_id == value and item.ptav_active
        )[:1]

    def _ensure_product_is_storable(self, product):
        vals = {}
        if product.type != "consu":
            vals["type"] = "consu"
        if not product.product_tmpl_id.is_storable:
            vals["is_storable"] = True
        if vals:
            product.write(vals)

    @api.model
    def _parse_feed_sku(self, sku):
        sku = self._clean(sku)
        if not sku:
            return False

        if "-" in sku:
            template_code, color = sku.rsplit("-", 1)
            template_code = self._clean(template_code)
            color = self._clean(color)
            if template_code and color:
                return {
                    "template_code": template_code,
                    "size": False,
                    "color": color,
                }

        parts = sku.split("_")
        if len(parts) >= 3 and self._looks_like_size(parts[-2]):
            return {
                "template_code": "_".join(parts[:-2]),
                "size": parts[-2],
                "color": parts[-1],
            }
        if len(parts) >= 2:
            return {
                "template_code": "_".join(parts[:-1]),
                "size": False,
                "color": parts[-1],
            }
        return {
            "template_code": sku,
            "size": False,
            "color": False,
        }

    @staticmethod
    def _looks_like_size(value):
        value = str(value or "").strip()
        if not value:
            return False
        if value.replace(".", "", 1).isdigit():
            return True
        return value.upper() in {
            "XXS",
            "XS",
            "S",
            "M",
            "L",
            "XL",
            "XXL",
            "XXXL",
            "2XL",
            "3XL",
            "4XL",
            "5XL",
        }

    @api.model
    def _config(self, key, default=False):
        return (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param(f"warehouse_inventory_feed.{key}", default)
        )

    @api.model
    def _set_config(self, key, value):
        return (
            self.env["ir.config_parameter"]
            .sudo()
            .set_param(f"warehouse_inventory_feed.{key}", value)
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

    @classmethod
    def _parse_aggregate_quantity(cls, row):
        aggregate = row.get("sum") if isinstance(row, dict) else None
        if isinstance(aggregate, dict):
            return cls._parse_quantity(aggregate.get("Ton_Cuoi"))
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
