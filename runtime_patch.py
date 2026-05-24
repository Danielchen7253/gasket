"""Additional runtime patch for the nameplate web app."""

from http import HTTPStatus
from pathlib import Path
import uuid

import httpx

import sitecustomize


_old_install_patch = sitecustomize._install_patch


def _patched_install(g):
    _old_install_patch(g)
    esc = g["esc"]

    def patched_get_quote_items(client, product_id):
        response = client.get(
            f"{g['SUPABASE_URL']}/rest/v1/refrigerator_product_quote_items"
            f"?select=*&refrigerator_product_id=eq.{product_id}&order=door_index.asc",
            headers=g["supabase_headers"](),
        )
        response.raise_for_status()
        rows = response.json()
        return [row for row in rows if g["is_customer_visible_gasket"](row)]

    def patched_render_result(product, quote_items, request, upload_url):
        nameplate_data = (request or {}).get("nameplate_data") or {}
        pending_new = g["is_unconfirmed_new_product"](product)
        positions = [] if pending_new else g["infer_door_positions"](product)
        quantity = 0 if pending_new else (len(positions) or g["estimated_gasket_quantity"](product, quote_items))
        g["trigger_background_refresh"](product["id"], not product.get("product_image_url"), not quote_items)
        product_img = product.get("product_image_url")
        needs_image = not bool(product_img)
        needs_gasket = not bool(quote_items)
        product_loading = "&#22270;&#29255;&#27491;&#22312;&#21152;&#36733;"
        gasket_loading = "&#23494;&#23553;&#26465;&#36164;&#26009;&#27491;&#22312;&#21152;&#36733;"
        product_name = f"{product.get('brand') or ''} {product.get('equipment_model') or ''}".strip()
        loading_banner = (
            f"<section><h2>&#31995;&#32479;&#27491;&#22312;&#20026; {esc(product_name)} &#21305;&#37197;&#38376;&#23553;&#26465;&#36164;&#26009;</h2></section>"
            if needs_image or needs_gasket else ""
        )
        product_html = (
            f"<img class='photo' src='{esc(product_img)}' alt='Refrigerator product image'>"
            if product_img
            else f"<div class='photo loading'><span data-loading-label='{product_loading}'>{product_loading} 00:00</span></div>"
        )
        plate_html = f"<img class='plate' src='{esc(upload_url)}' alt='Uploaded nameplate'>" if upload_url else "<div class='plate muted'>Nameplate photo</div>"
        source_summary = product.get("data_source_summary") or ""
        door_positions = product.get("door_positions") if isinstance(product.get("door_positions"), list) else []
        door_text = ", ".join([item.get("label") or item.get("key") or "" for item in door_positions if item]) or "Loading"
        confidence = product.get("data_confidence") or product.get("door_layout_confidence") or ""
        product_facts = f"""
<div class="facts">
<div>Product type</div><div><strong>{esc(product.get('product_type') or 'Loading')}</strong></div>
<div>Door layout</div><div>{esc(door_text)}</div>
<div>Status</div><div>{esc(product.get('lifecycle_status') or 'unknown')}</div>
<div>Data confidence</div><div>{esc(confidence)}{('%' if confidence != '' else '')}</div>
<div>Source summary</div><div>{esc(source_summary or 'Loading')}</div>
</div>"""

        rows = []
        if pending_new and not quote_items:
            rows.append(f"""<div class="item"><input type="checkbox" disabled><div class="loading" style="width:98px;height:78px;border:1px solid #dbe2ea;border-radius:6px"><span data-loading-label="{gasket_loading}">{gasket_loading} 00:00</span></div><div><strong>{gasket_loading}</strong></div><div class="price"><strong>Loading</strong></div><div></div></div>""")

        for index, item in enumerate(quote_items, start=1):
            door_label = item.get("door_position_display") or item.get("door_position") or f"Door {index}"
            door_key = item.get("door_position") or f"door_{index}"
            price = float(item.get("final_price_usd") or 0)
            image = item.get("gasket_image_url")
            image_html = f"<img src='{esc(image)}' alt='Gasket image'>" if image else "<div class='muted'>Gasket</div>"
            dims = item.get("dimensions_text") or f"{item.get('width_in') or '-'} x {item.get('height_in') or '-'} in"
            size_status = item.get("size_status")
            size_note = f" ({esc(size_status)})" if size_status else ""
            part_number = item.get("part_number") or item.get("universal_part_number")
            part_line = f"<br>OEM: <strong>{esc(part_number)}</strong>" if part_number else ""
            color_line = f"<br>Color: {esc(item.get('gasket_color'))}" if item.get("gasket_color") else ""
            install_line = f"<br>Type: {esc(item.get('gasket_install_type'))}" if item.get("gasket_install_type") else ""
            confirm_line = ""
            if item.get("needs_customer_confirmation") is True or item.get("needs_customer_confirmation") == "true":
                confirm_line = f"<br><span class='muted'>{esc(item.get('customer_confirmation_note') or 'Confirm before production.')}</span>"
            confidence_line = f"<br>Confidence: {esc(item.get('confidence_score'))}%" if item.get("confidence_score") is not None else ""
            evidence = f"<br><span class='muted'>{esc(item.get('evidence_summary'))}</span>" if item.get("evidence_summary") else ""
            rows.append(f"""<label class="item"><input type="checkbox" name="door_position" value="{esc(door_key)}" data-price="{price}" checked>{image_html}<div><strong>{esc(door_label)} gasket</strong><p>{esc(dims)}{size_note}{part_line}{color_line}{install_line}{confidence_line}{confirm_line}{evidence}</p></div><div class="price"><strong>{g['money'](price)}</strong><small>each selected door</small></div><div></div></label>""")

        if not quote_items and not pending_new:
            for index, position in enumerate(positions or g["door_positions_for_count"](quantity), start=1):
                door_label = position.get("label") or f"Door {index}"
                door_key = position.get("key") or f"door_{index}"
                rows.append(f"""<div class="item"><input type="checkbox" disabled><div class="loading" style="width:98px;height:78px;border:1px solid #dbe2ea;border-radius:6px"><span data-loading-label="{gasket_loading}">{gasket_loading} 00:00</span></div><div><strong>{esc(door_label)} gasket</strong></div><div class="price"><strong>Loading</strong></div><div><small class="muted">Door</small><br><strong>{esc(door_key)}</strong></div></div>""")

        summary_html = "" if pending_new else f"""<div class="summary"><div class="metric"><span>Required gaskets</span><strong>{quantity}</strong></div><div class="metric"><span>Selected</span><strong id="selected-count">0</strong></div><div class="metric"><span>Total</span><strong id="selected-total">$0.00</strong></div></div>"""
        rows_html = "".join(rows) if rows else f"""<div class="item"><input type="checkbox" disabled><div class="loading" style="width:98px;height:78px;border:1px solid #dbe2ea;border-radius:6px"><span data-loading-label="{gasket_loading}">{gasket_loading} 00:00</span></div><div><strong>{gasket_loading}</strong></div><div class="price"><strong>Loading</strong></div><div></div></div>"""
        return g["page"]("Matched Gasket Quote", f"""
<div data-refresh-product="{esc(product['id'])}" data-needs-image="{1 if needs_image else 0}" data-needs-gasket="{1 if needs_gasket else 0}" hidden></div>
{loading_banner}<section><h2>Matched refrigerator</h2><div class="result-grid"><div><h3>Refrigerator image</h3>{product_html}</div><div><h3>Nameplate</h3>{plate_html}</div><div><h3>Nameplate summary</h3><div class="facts"><div>OpenAI brand</div><div><strong>{esc(nameplate_data.get('brand') or product.get('brand'))}</strong></div><div>OpenAI model</div><div><strong>{esc(nameplate_data.get('model') or product.get('equipment_model'))}</strong></div><div>Serial</div><div>{esc(nameplate_data.get('serial_number') or 'Not found')}</div><div>Brand</div><div><strong>{esc(product.get('brand'))}</strong></div><div>Model</div><div><strong>{esc(product.get('equipment_model'))}</strong></div></div></div></div>{product_facts}</section>
<section><h2>Gasket quote</h2>{summary_html}<div>{rows_html}</div></section>
<div class="checkout"><strong>Ready to order?</strong><br><span class="muted">Select the gasket solution for this refrigerator.</span></div>""")

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

    g["get_quote_items"] = patched_get_quote_items
    g["render_result"] = patched_render_result
    g["Handler"].do_POST = patched_do_POST


sitecustomize._install_patch = _patched_install
