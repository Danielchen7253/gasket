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
        door_positions_value = _door_positions_text(product)
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
<input type="hidden" name="door_positions_json" value="{esc(door_positions_value)}">
<input type="hidden" name="raw_text" value="{esc(raw_text)}">
<p><button type="submit">Confirm and match gasket records</button> <a class="button" href="/">Upload another</a></p>
</form></div></section>""")

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
        if not product.get("product_image_url"):
            try:
                with httpx.Client(timeout=15) as client:
                    from fast_image_patch import quick_promote_product_image

                    if quick_promote_product_image(client, product):
                        refreshed = g["get_product"](client, product["id"])
                        if refreshed:
                            product = refreshed
            except Exception as exc:
                print(f"fast product image render refresh failed for {product['id']}: {exc}")
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
        door_positions = product.get("door_positions") if isinstance(product.get("door_positions"), list) else []
        door_text = ", ".join([item.get("label") or item.get("key") or "" for item in door_positions if item]) or "Loading"
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

    old_do_GET = g["Handler"].do_GET

    def patched_do_GET(self):
        parsed = g["urlparse"](self.path)
        if parsed.path == "/product-status":
            product_id = int(g["parse_qs"](parsed.query).get("product_id", ["0"])[0])
            if product_id:
                with httpx.Client(timeout=30) as client:
                    product = g["get_product"](client, product_id)
                    quote_items = g["get_quote_items"](client, product_id) if product else []
                    if product and not product.get("product_image_url"):
                        try:
                            from fast_image_patch import quick_promote_product_image

                            if quick_promote_product_image(client, product):
                                product = g["get_product"](client, product_id)
                        except Exception as exc:
                            print(f"fast product image refresh failed for {product_id}: {exc}")
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

    g["get_quote_items"] = patched_get_quote_items
    g["render_confirm_nameplate"] = patched_render_confirm_nameplate
    g["render_result"] = patched_render_result
    g["Handler"].do_GET = patched_do_GET


sitecustomize._install_patch = _patched_install
