import re
import unicodedata

from odoo import api, models


WAREHOUSE_LIST_OPTION = "warehouse_inventory_feed_warehouse_list"
WAREHOUSE_LIST_HEADERS = {
    "stt",
    "he thong",
    "mien",
    "ma kho",
    "ma bo phan",
    "ktdt phu trach",
    "note",
}


class BaseImport(models.TransientModel):
    _inherit = "base_import.import"

    def _read_file(self, options):
        file_length, rows = super()._read_file(options)
        if not self._is_stock_warehouse_import():
            return file_length, rows

        header_index = self._find_warehouse_list_header_index(rows)
        if header_index is not None:
            options[WAREHOUSE_LIST_OPTION] = True
            return self._trim_to_warehouse_list(rows, header_index)

        current_sheet = options.get("sheet")
        for sheet in options.get("sheets", []):
            if sheet == current_sheet:
                continue

            sheet_options = dict(options, sheet=sheet)
            sheet_options.pop(WAREHOUSE_LIST_OPTION, None)
            _sheet_file_length, sheet_rows = super()._read_file(sheet_options)
            header_index = self._find_warehouse_list_header_index(sheet_rows)
            if header_index is None:
                continue

            options.update(sheet_options)
            options[WAREHOUSE_LIST_OPTION] = True
            return self._trim_to_warehouse_list(sheet_rows, header_index)

        return file_length, rows

    @api.model
    def _get_mapping_suggestions(self, headers, header_types, fields_tree):
        suggestions = super()._get_mapping_suggestions(headers, header_types, fields_tree)
        if not self._is_stock_warehouse_import():
            return suggestions

        for index, header in enumerate(headers):
            if self._normalize_warehouse_header(header) == "ma kho":
                suggestions[(index, header)] = {
                    "field_path": ["code"],
                    "distance": 0,
                }
        return suggestions

    @api.model
    def _convert_import_data(self, fields, options):
        data, import_fields = super()._convert_import_data(fields, options)
        if not self._is_stock_warehouse_import() or not options.get(WAREHOUSE_LIST_OPTION):
            return data, import_fields

        return self._prepare_warehouse_list_import_data(data, import_fields)

    def _is_stock_warehouse_import(self):
        self.ensure_one()
        return self.res_model == "stock.warehouse"

    @api.model
    def _find_warehouse_list_header_index(self, rows):
        for index, row in enumerate(rows[:20]):
            normalized_headers = {
                self._normalize_warehouse_header(value)
                for value in row
                if self._normalize_warehouse_header(value)
            }
            if "ma kho" in normalized_headers and len(
                normalized_headers & WAREHOUSE_LIST_HEADERS
            ) >= 4:
                return index
        return None

    @api.model
    def _trim_to_warehouse_list(self, rows, header_index):
        header = list(rows[header_index])
        width = len(header)
        stt_index = self._warehouse_list_stt_index(header)
        trimmed_rows = [header]
        for row in rows[header_index + 1:]:
            row = list(row[:width])
            if len(row) < width:
                row.extend([""] * (width - len(row)))
            if stt_index is not None and not self._is_sequence_number(row[stt_index]):
                continue
            trimmed_rows.append(row)
        return len(trimmed_rows), trimmed_rows

    @api.model
    def _warehouse_list_stt_index(self, header):
        for index, value in enumerate(header):
            if self._normalize_warehouse_header(value) == "stt":
                return index
        return None

    @api.model
    def _prepare_warehouse_list_import_data(self, data, import_fields):
        if "code" not in import_fields and "name" not in import_fields:
            return data, import_fields

        import_fields = list(import_fields)
        data = [list(row) for row in data]

        source_field = "code" if "code" in import_fields else "name"
        source_index = import_fields.index(source_field)

        if "code" not in import_fields:
            import_fields.append("code")
            for row in data:
                row.append(row[source_index])

        if "name" not in import_fields:
            import_fields.append("name")
            for row in data:
                row.append(row[source_index])

        code_index = import_fields.index("code")
        name_index = import_fields.index("name")

        if ".id" not in import_fields:
            import_fields.insert(0, ".id")
            for row in data:
                row.insert(0, "")
            code_index += 1
            name_index += 1
        id_index = import_fields.index(".id")

        codes = []
        normalized_data = []
        seen_codes = set()
        for row in data:
            code = self._clean_import_value(row[code_index])
            if not code or code in seen_codes:
                continue
            seen_codes.add(code)
            codes.append(code)
            row[code_index] = code
            row[name_index] = code
            normalized_data.append(row)

        if not normalized_data:
            return normalized_data, import_fields

        Warehouse = self.env["stock.warehouse"].with_context(active_test=False)
        existing_warehouses = Warehouse.search([
            ("company_id", "=", self.env.company.id),
            ("code", "in", codes),
        ])
        existing_id_by_code = {
            warehouse.code: warehouse.id for warehouse in existing_warehouses
        }
        for row in normalized_data:
            row[id_index] = existing_id_by_code.get(row[code_index]) or row[id_index]

        return normalized_data, import_fields

    @api.model
    def _normalize_warehouse_header(self, value):
        value = self._clean_import_value(value).casefold()
        value = unicodedata.normalize("NFKD", value)
        value = "".join(char for char in value if not unicodedata.combining(char))
        return re.sub(r"\s+", " ", value).strip()

    @api.model
    def _clean_import_value(self, value):
        if value is None:
            return ""
        return str(value).strip()

    @api.model
    def _is_sequence_number(self, value):
        value = self._clean_import_value(value)
        if not value:
            return False
        try:
            return float(value).is_integer()
        except ValueError:
            return False
