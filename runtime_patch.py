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
    original_is_customer_visible_gasket = g["is_customer_visible_gasket"]

    def patched_is_customer_visible_gasket(item):
        name = (item.get("gasket_name") or "").lower()
        image = (item.get("gasket_image_url") or "").lower()
        if "search result" in name or "logo" in image:
            return False
        if item.get("data_status") == "ai_structured" and item.get("door_position_display"):
            return True
        return original_is_customer_visible_gasket(item)

    def _product_prefill(brand, model):
        if not model:
            return {}
        try:
            with httpx.Client(timeout=15) as client:
                return g["find_product"](client, brand or "", model or "") or {}
        except Exception:
            return {}

    def _door_positions_text(product):
        positions = product.get("door_positions")
        if isinstance(positions, list) and positions:
            import json

            return json.dumps(positions, ensure_ascii=False)
        return ""

    def patched_render_confirm_nameplate(upload_url, customer, nameplate_data, fallback_brand="", fallback_model=""):
        brand = nameplate_data.get("brand") or fallback_brand
        model = nameplate_data.get("model") or fallback_model
        product = _product_prefill(brand, model)
        raw_text = nameplate_data.get("raw_text") or ""
        return g["page"]("Confirm Nameplate", f"""
<section><h2>Confirm refrigerator information</h2>
<p>Check the nameplate and product information. Correct anything wrong before matching gasket records.</p>
<div class="result-grid"><div><h3>Nameplate photo</h3><img class="photo" src="{esc(upload_url)}" alt="Uploaded nameplate"></div>
<form method="post" action="/match" enctype="multipart/form-data"><h3>Read information</h3>
<input type="hidden" name="upload_url" value="{esc(upload_url)}">
<input type="hidden" name="customer_name" value="{esc(customer.get('customer_name') or '')}">
<input type="hidden" name="customer_email" value="{esc(customer.get('customer_email') or '')}">
<input type="hidden" name="customer_phone" value="{esc(customer.get('customer_phone') or '')}">
<div class="grid">
<div><label>Brand</label><input name="brand" value="{esc(brand or product.get('brand') or '')}"></div>
<div><label>Model</label><input name="equipment_model" value="{esc(model or product.get('equipment_model') or '')}"></div>
<div><label>Serial</label><input name="serial_number" value="{esc(nameplate_data.get('serial_number') or '')}"></div>
<div><label>Manufacturer</label><input name="manufacturer" value="{esc(nameplate_data.get('manufacturer') or product.get('manufacturer') or '')}"></div>
<div><label>Voltage</label><input name="voltage" value="{esc(nameplate_data.get('voltage') or '')}"></div>
<div><label>Refrigerant</label><input name="refrigerant" value="{esc(nameplate_data.get('refrigerant') or '')}"></div>
<div><label>Manufacture date</label><input name="manufacture_date" value="{esc(nameplate_data.get('manufacture_date') or product.get('manufacture_date_text') or '')}"></div>
<div><label>Product type</label><input name="product_type" value="{esc(product.get('product_type') or '')}"></div>
<div><label>Door count</label><input name="door_count" value="{esc(product.get('door_count') or '')}"></div>
<div><label>Door layout</label><input name="door_layout" value="{esc(product.get('door_layout') or '')}"></div>
<div><label>Product image URL</label><input name="product_image_url" value="{esc(product.get('product_image_url') or '')}"></div>
<div><label>Active / discontinued status</label><input name="lifecycle_status" value="{esc(product.get('lifecycle_status') or '')}"></div>
</div>
<input type="hidden" name="raw_text" value="{esc(raw_text)}">
<p><button id="confirm-match" type="submit" disabled>Please wait 10s</button> <a class="button" href="/">Upload another</a></p>
</form></div></section>""")

    def patched_render_home(message=""):
        warning = f"<p style='color:#9f4b12'>{esc(message)}</p>" if message else ""
        upload_style = """
<style>
body>header{display:none}
main{max-width:none;padding:0}
.site-header{background:#0f1d24;color:#fff;border:0;border-radius:0;margin:0;padding:0}
.site-header .wrap,.home-footer .wrap,.work-zone{max-width:1180px;margin:0 auto}
.site-header .wrap{display:grid;grid-template-columns:auto 1fr auto;gap:24px;align-items:center;padding:18px 22px}
.brand-lockup{display:flex;align-items:center;gap:12px}
.brand-mark{width:38px;height:38px;border-radius:8px;background:#0a6f78;display:flex;align-items:center;justify-content:center;font-weight:900;letter-spacing:.02em}
.brand-name{font-size:20px;font-weight:800;line-height:1}
.brand-tag{display:block;margin-top:4px;color:#b8c8d2;font-size:12px;font-weight:500}
.promo-line{display:flex;gap:10px;flex-wrap:wrap;justify-content:center}
.promo-line span{border:1px solid rgba(255,255,255,.18);background:rgba(255,255,255,.06);border-radius:999px;padding:8px 12px;color:#d8e5eb;font-size:13px}
.header-cta{color:#fff;background:#0a6f78;border-radius:6px;padding:10px 14px;text-decoration:none;font-weight:800;white-space:nowrap}
.work-shell{background:#eef3f6;padding:26px 22px 30px}
.work-zone{display:grid;grid-template-columns:minmax(0,1.05fr) minmax(360px,.95fr);gap:18px;align-items:stretch}
.work-panel{background:white;border:1px solid #dbe2ea;border-radius:8px;padding:22px;margin:0}
.work-panel h1{font-size:38px;line-height:1.05;margin-bottom:12px}
.workflow{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:18px}
.flow-step{border:1px solid #dbe2ea;border-radius:8px;background:#fbfdfe;padding:14px;min-height:112px}
.flow-step span{display:inline-flex;width:26px;height:26px;border-radius:50%;background:#0f1d24;color:#fff;align-items:center;justify-content:center;font-weight:800;font-size:12px;margin-bottom:10px}
.flow-step strong{display:block;margin-bottom:6px}
.flow-step p{font-size:13px;line-height:1.35;margin:0;color:#687385}
.home-form{background:#fff;border:1px solid #dbe2ea;border-radius:8px;padding:22px;margin:0}
.upload-working{margin-top:14px;border:1px solid #c9e7ea;background:#eefbfc;color:#0f1d24;border-radius:8px;padding:12px;line-height:1.45}
.upload-working span{color:#687385}
.upload-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px;align-items:end;margin-bottom:12px}
.upload-row button{width:auto;white-space:nowrap}
.home-form .grid{grid-template-columns:1fr 1fr 1fr;margin-top:12px}
.trust-strip{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:14px}
.trust-strip div{border:1px solid #dbe2ea;border-radius:8px;background:#fbfdfe;padding:12px}
.trust-strip strong{display:block}
.home-footer{background:#0f1d24;color:#d8e5eb;border:0;border-radius:0;margin:0;padding:24px 22px}
.home-footer .wrap{display:grid;grid-template-columns:1.2fr 1fr 1fr;gap:22px}
.home-footer h3{color:#fff;margin:0 0 8px;font-size:15px}
.home-footer p,.home-footer li{color:#b8c8d2;font-size:13px;margin:0 0 6px;line-height:1.45}
.home-footer ul{list-style:none;margin:0;padding:0}
@media(max-width:900px){.site-header .wrap,.work-zone,.home-footer .wrap{grid-template-columns:1fr}.promo-line{justify-content:flex-start}.workflow,.trust-strip,.home-form .grid{grid-template-columns:1fr}.upload-row{grid-template-columns:1fr}.upload-row button,.header-cta{width:100%;justify-content:center;text-align:center}.work-panel h1{font-size:32px}}
</style>"""
        return g["page"]("Gasket Match", f"""
{upload_style}
<section class="site-header"><div class="wrap">
<div class="brand-lockup"><div class="brand-mark">GM</div><div><div class="brand-name">Gasket Match</div><span class="brand-tag">Refrigerator door gasket identification</span></div></div>
<div class="promo-line"><span>Free nameplate fit check</span><span>Custom gaskets from $45</span><span>Commercial and residential models</span></div>
<a class="header-cta" href="#upload">Start match</a>
</div></section>
<div class="work-shell"><section class="work-zone">
<div class="work-panel"><h1>Find the Right Refrigerator Door Gasket Fast</h1>
<p>Upload the equipment nameplate. We read the model, ask you to confirm it, then match the door gasket options for ordering.</p>
<div class="workflow">
<div class="flow-step"><span>1</span><strong>Upload</strong><p>Take a clear photo of the refrigerator nameplate.</p></div>
<div class="flow-step"><span>2</span><strong>Confirm</strong><p>Check the brand, model, serial, voltage, and refrigerant.</p></div>
<div class="flow-step"><span>3</span><strong>Match</strong><p>See the product image and gasket records by door position.</p></div>
<div class="flow-step"><span>4</span><strong>Order</strong><p>Select one gasket or all required door gaskets.</p></div>
</div>
<div class="trust-strip"><div><strong>Need help?</strong><span class="muted">No gasket part number required.</span></div><div><strong>Price bands</strong><span class="muted">Clear pricing by gasket size.</span></div><div><strong>Fit focused</strong><span class="muted">Built to reduce wrong orders.</span></div></div>
</div>
<form id="upload" class="home-form" method="post" action="/read-nameplate" enctype="multipart/form-data"><h2>Upload nameplate</h2>{warning}
<div class="upload-row"><div><label>Nameplate photo</label><input type="file" name="nameplate" accept="image/*"></div><button type="submit">Read nameplate</button></div>
<div class="grid"><div><label>Brand fallback</label><input name="brand"></div><div><label>Model fallback</label><input name="equipment_model"></div><div><label>Customer name</label><input name="customer_name"></div></div>
<p class="muted">If the photo is hard to read, type the brand or model here before submitting.</p></form>
</section></div>
<section class="home-footer"><div class="wrap">
<div><h3>Gasket Match</h3><p>Door gasket identification and quote workflow for refrigerators, freezers, prep tables, merchandisers, and reach-in equipment.</p></div>
<div><h3>Service scope</h3><ul><li>Commercial refrigeration</li><li>Residential refrigerators</li><li>OEM cross reference</li><li>Custom size gasket quoting</li></ul></div>
<div><h3>Order support</h3><ul><li>Nameplate-based matching</li><li>Door-position gasket selection</li><li>Customer confirmation before production</li><li>Online payment workflow</li></ul></div>
</div></section>""")

    def patched_get_quote_items(client, product_id):
        response = client.get(
            f"{g['SUPABASE_URL']}/rest/v1/refrigerator_product_gaskets"
            f"?select=*&refrigerator_product_id=eq.{product_id}&order=door_index.asc",
            headers=g["supabase_headers"](),
        )
        response.raise_for_status()
        rows = response.json()
        return [row for row in rows if g["is_customer_visible_gasket"](row)]

    def patched_render_result(product, quote_items, request, upload_url):
        nameplate_data = (request or {}).get("nameplate_data") or {}
        pending_new = g["is_unconfirmed_new_product"](product)
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
</div>"""

        rows = []
        if pending_new and not quote_items:
            rows.append(f"""<div class="item"><input type="checkbox" disabled><div class="loading" style="width:98px;height:78px;border:1px solid #dbe2ea;border-radius:6px"><span data-loading-label="{gasket_loading}">{gasket_loading} 00:00</span></div><div><strong>{gasket_loading}</strong></div><div class="price"><strong>Loading</strong></div><div></div></div>""")

        for index, item in enumerate(quote_items, start=1):
            door_label = item.get("door_position_display") or "Door position loading"
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
            rows.append(f"""<label class="item"><input type="checkbox" name="door_position" value="{esc(door_key)}" data-price="{price}" checked>{image_html}<div><strong>{esc(door_label)}</strong><p>{esc(dims)}{size_note}{part_line}{color_line}{install_line}{confidence_line}{confirm_line}{evidence}</p></div><div class="price"><strong>{g['money'](price)}</strong><small>each selected door</small></div><div></div></label>""")

        if not quote_items and not pending_new:
            rows.append(f"""<div class="item"><input type="checkbox" disabled><div class="loading" style="width:98px;height:78px;border:1px solid #dbe2ea;border-radius:6px"><span data-loading-label="{gasket_loading}">{gasket_loading} 00:00</span></div><div><strong>{gasket_loading}</strong></div><div class="price"><strong>Loading</strong></div><div></div></div>""")

        summary_html = "" if pending_new else f"""<div class="summary"><div class="metric"><span>Door positions</span><strong>{len(quote_items)}</strong></div><div class="metric"><span>Selected</span><strong id="selected-count">0</strong></div><div class="metric"><span>Total</span><strong id="selected-total">$0.00</strong></div></div>"""
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
        if "canonical_model_for_brand" in g:
            model = g["canonical_model_for_brand"](brand, model)
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
            prefill_brand = nameplate_data.get("brand") or brand
            prefill_model = nameplate_data.get("model") or model
            if prefill_brand and prefill_model:
                try:
                    from instant_enrichment import start_instant_enrichment, upsert_known_product_from_nameplate

                    with httpx.Client(timeout=20) as client:
                        product = upsert_known_product_from_nameplate(client, prefill_brand, prefill_model, nameplate_data)
                        start_instant_enrichment(product["id"], nameplate_data)
                except Exception as exc:
                    print(f"instant pre-enrichment failed for {prefill_brand} {prefill_model}: {exc}")
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
        nameplate_data["model"] = model or nameplate_data.get("model")
        if not brand or not model:
            self.send_html(g["render_confirm_nameplate"](upload_url or "", customer, nameplate_data, brand, model), HTTPStatus.BAD_REQUEST)
            return

        with httpx.Client(timeout=30) as client:
            product = g["find_product"](client, brand, model)
            if not product:
                product = g["create_product_from_confirmed_model"](client, brand, model)
            update_payload = {
                "manufacturer": fields.get("manufacturer", {}).get("text") or product.get("manufacturer"),
                "product_type": fields.get("product_type", {}).get("text") or product.get("product_type"),
                "door_layout": fields.get("door_layout", {}).get("text") or product.get("door_layout"),
                "product_image_url": fields.get("product_image_url", {}).get("text") or product.get("product_image_url"),
                "lifecycle_status": fields.get("lifecycle_status", {}).get("text") or product.get("lifecycle_status"),
                "data_status": product.get("data_status") or "customer_confirmed",
                "data_confidence": product.get("data_confidence") or 70,
                "data_source_summary": product.get("data_source_summary") or "Customer-confirmed nameplate and product information.",
                "updated_at": g["datetime"].now(g["timezone"].utc).isoformat(),
                "last_enriched_at": g["datetime"].now(g["timezone"].utc).isoformat(),
            }
            manufacture_date_text = fields.get("manufacture_date", {}).get("text") or ""
            if manufacture_date_text:
                if __import__("re").match(r"^\d{4}-\d{2}-\d{2}$", manufacture_date_text):
                    update_payload["manufacture_date"] = manufacture_date_text
                else:
                    update_payload["manufacture_date_text"] = manufacture_date_text
            door_count_text = fields.get("door_count", {}).get("text") or ""
            if door_count_text.strip().isdigit():
                update_payload["door_count"] = int(door_count_text.strip())
            clean_payload = {key: value for key, value in update_payload.items() if value not in (None, "")}
            if clean_payload:
                response = client.patch(
                    f"{g['SUPABASE_URL']}/rest/v1/refrigerator_products?id=eq.{product['id']}",
                    headers=g["supabase_headers"]("return=representation"),
                    json=clean_payload,
                )
                response.raise_for_status()
                updated_rows = response.json()
                if updated_rows:
                    product = updated_rows[0]
            try:
                from instant_enrichment import start_instant_enrichment, upsert_known_product_from_nameplate, wait_for_core_result

                product = upsert_known_product_from_nameplate(client, brand, model, nameplate_data, status="customer_confirmed")
                start_instant_enrichment(product["id"], nameplate_data)
                waited = wait_for_core_result(product["id"], max_seconds=10)
                if waited.get("product"):
                    product = waited["product"]
            except Exception as exc:
                print(f"instant enrichment start failed for {brand} {model}: {exc}")
            request = g["create_request"](client, customer, upload_url, brand, model, product, nameplate_data)
            self.send_html(g["render_result"](product, g["get_quote_items"](client, product["id"]), request, upload_url))

    old_do_GET = g["Handler"].do_GET

    def patched_do_GET(self):
        parsed = g["urlparse"](self.path)
        if parsed.path == "/product-status":
            product_id = int(g["parse_qs"](parsed.query).get("product_id", ["0"])[0])
            if product_id:
                with httpx.Client(timeout=30) as client:
                    product = g["get_product"](client, product_id)
                    quote_items = g["get_quote_items"](client, product_id) if product else []
                if product:
                    g["trigger_background_refresh"](product_id, not product.get("product_image_url"), not quote_items)
                data = {
                    "product_image_url": product.get("product_image_url") if product else None,
                    "quote_item_count": len(quote_items),
                }
                payload = g["json"].dumps(data).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
        old_do_GET(self)

    g["is_customer_visible_gasket"] = patched_is_customer_visible_gasket
    g["get_quote_items"] = patched_get_quote_items
    g["render_home"] = patched_render_home
    g["render_confirm_nameplate"] = patched_render_confirm_nameplate
    g["render_result"] = patched_render_result
    g["Handler"].do_GET = patched_do_GET
    g["Handler"].do_POST = patched_do_POST


sitecustomize._install_patch = _patched_install
