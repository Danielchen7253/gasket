"""Additional runtime patch for the nameplate web app."""

from http import HTTPStatus
from pathlib import Path
import uuid

import httpx

import sitecustomize


_old_install_patch = sitecustomize._install_patch


def _patched_install(g):
    _old_install_patch(g)

    def patched_do_POST(self):
        path = g["urlparse"](self.path).path
        if path not in {"/read-nameplate", "/match"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        fields = g["parse_multipart"](
            self.rfile.read(int(self.headers.get("Content-Length", "0"))),
            self.headers.get("Content-Type", ""),
        )
        brand = fields.get("brand", {}).get("text", "").strip()
        model = fields.get("equipment_model", {}).get("text", "").strip()
        upload_url = fields.get("upload_url", {}).get("text", "").strip() or None
        file_field = fields.get("nameplate")
        customer = {key: fields.get(key, {}).get("text") or None for key in ("customer_name", "customer_email", "customer_phone")}

        if path == "/read-nameplate":
            if not (file_field and file_field.get("filename") and file_field.get("data")):
                self.send_html(g["render_home"]("Please upload a nameplate photo first."), HTTPStatus.BAD_REQUEST)
                return
            saved_name = f"{uuid.uuid4().hex}{Path(file_field['filename']).suffix or '.jpg'}"
            (g["UPLOAD_DIR"] / saved_name).write_bytes(file_field["data"])
            upload_url = f"/uploads/customer_nameplates/{saved_name}"
            try:
                nameplate_data = g["identify_nameplate"](file_field["data"], file_field["filename"])
            except Exception as exc:
                self.send_html(g["render_home"](f"Nameplate recognition failed: {exc}"), HTTPStatus.BAD_REQUEST)
                return
            self.send_html(g["render_confirm_nameplate"](upload_url, customer, nameplate_data, brand, model))
            return

        nameplate_data = {
            "brand": brand or None,
            "model": model or None,
            "serial_number": fields.get("serial_number", {}).get("text") or None,
            "manufacturer": fields.get("manufacturer", {}).get("text") or None,
            "manufacture_date": fields.get("manufacture_date", {}).get("text") or None,
            "refrigerant": fields.get("refrigerant", {}).get("text") or None,
            "voltage": fields.get("voltage", {}).get("text") or None,
            "raw_text": fields.get("raw_text", {}).get("text") or "",
            "confidence": 100,
        }
        if not brand or not model:
            self.send_html(g["render_confirm_nameplate"](upload_url or "", customer, nameplate_data, brand, model), HTTPStatus.BAD_REQUEST)
            return

        with httpx.Client(timeout=30) as client:
            product = g["find_product"](client, brand, model)
            if not product:
                product = g["create_product_from_confirmed_model"](client, brand, model)
            request = g["create_request"](client, customer, upload_url, brand, model, product, nameplate_data)
            if not g["is_unconfirmed_new_product"](product):
                positions = g["infer_door_positions"](product)
                if positions:
                    g["save_inferred_door_layout"](client, product, positions)
                    product["door_positions"] = positions
                    product["door_count"] = len(positions)
            self.send_html(g["render_result"](product, g["get_quote_items"](client, product["id"]), request, upload_url))

    g["Handler"].do_POST = patched_do_POST


sitecustomize._install_patch = _patched_install
