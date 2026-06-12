"""Additional runtime patch for the nameplate web app."""

from http import HTTPStatus
from pathlib import Path
import re
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

    def _model_check_notice():
        return """
<div class="model-check-notice">
<strong>Important: confirm the model number exactly.</strong>
AI can misread characters such as 8/S, 1/I, 0/O. The red model box must match the nameplate before you continue.
</div>"""

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
        recognition_error = nameplate_data.get("recognition_error")
        recognition_notice = (
            f"""<div class="model-check-notice"><strong>Please type the brand and model from the nameplate.</strong>
Automatic reading is unavailable right now. The uploaded photo is saved; enter the exact brand and model, then continue matching.</div>"""
            if recognition_error else ""
        )
        return g["page"]("Confirm Nameplate", f"""
<section><h2>Confirm refrigerator information</h2>
<p>Check the nameplate and product information. Correct anything wrong before matching gasket records.</p>{recognition_notice}
<div class="result-grid"><div><h3>Nameplate photo</h3><button class="image-open" type="button" data-image-viewer-src="{esc(upload_url)}"><img class="photo" src="{esc(upload_url)}" alt="Uploaded nameplate"></button><p class="muted">Click the nameplate photo to zoom and drag.</p></div>
<form method="post" action="/match" enctype="multipart/form-data"><h3>Read information</h3>
<input type="hidden" name="upload_url" value="{esc(upload_url)}">
<input type="hidden" name="customer_name" value="{esc(customer.get('customer_name') or '')}">
<input type="hidden" name="customer_email" value="{esc(customer.get('customer_email') or '')}">
<input type="hidden" name="customer_phone" value="{esc(customer.get('customer_phone') or '')}">
<div class="grid">
<div><label>Brand</label><input name="brand" value="{esc(brand or product.get('brand') or '')}"></div>
<div><label>Model</label><input class="model-confirm-input" name="equipment_model" value="{esc(model or product.get('equipment_model') or '')}">{_model_check_notice()}</div>
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
<p><button type="submit">Confirm and match gasket records</button> <a class="button" href="/">Upload another</a></p>
</form></div></section>
<div class="image-viewer" id="image-viewer" aria-hidden="true">
<div class="image-viewer-tools"><button type="button" data-zoom="out">-</button><button type="button" data-zoom="in">+</button><button type="button" data-close-viewer>Close</button></div>
<div class="image-viewer-stage"><img id="image-viewer-img" alt="Nameplate enlarged"></div>
</div>""")

    def patched_render_home(message=""):
        warning = f"<p style='color:#9f4b12'>{esc(message)}</p>" if message else ""
        upload_style = """
<style>
main{max-width:none;padding:0}
.work-zone{max-width:var(--page-max,1180px);margin:0 auto}
.work-shell{background:#eef3f6;padding:34px 22px 38px}
.work-zone{display:flex;justify-content:center;align-items:flex-start}
.work-panel{background:white;border:1px solid #dbe2ea;border-radius:8px;padding:22px;margin:0}
.home-form{width:100%;background:#fff;border:1px solid #dbe2ea;border-radius:8px;padding:28px;margin:0}
.model-confirm-input{border:2px solid #d93025!important;background:#fffafa!important;box-shadow:0 0 0 3px rgba(217,48,37,.12)}
.model-check-notice{margin-top:8px;border:2px solid #d93025;background:#fff1f0;color:#5f1410;border-radius:8px;padding:10px;font-size:13px;line-height:1.4}
.model-check-notice strong{display:block;margin-bottom:4px;color:#3b0906}
.image-open{display:block;width:100%;padding:0;border:0;background:transparent;cursor:zoom-in}
.image-viewer{position:fixed;inset:0;background:rgba(7,16,22,.88);display:none;z-index:9999}
.image-viewer.is-open{display:block}
.image-viewer-tools{position:absolute;top:18px;right:18px;display:flex;gap:8px;z-index:2}
.image-viewer-tools button{min-width:44px;background:#fff;color:#0f1d24;border-radius:6px}
.image-viewer-stage{height:100%;overflow:hidden;display:flex;align-items:center;justify-content:center;cursor:grab}
.image-viewer-stage:active{cursor:grabbing}
.image-viewer-stage img{max-width:none;max-height:none;transform-origin:center center;user-select:none;pointer-events:none}
.upload-working{margin-top:14px;border:1px solid #c9e7ea;background:#eefbfc;color:#0f1d24;border-radius:8px;padding:12px;line-height:1.45}
.upload-working span{color:#687385}
.upload-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px;align-items:end;margin-top:26px}
.upload-row button{width:auto;white-space:nowrap}
.home-form .grid{grid-template-columns:1fr 1fr;margin-top:8px}
@media(max-width:900px){.work-zone{grid-template-columns:1fr}.home-form .grid{grid-template-columns:1fr}.upload-row{grid-template-columns:1fr}.upload-row button{width:100%;justify-content:center;text-align:center}}
</style>"""
        return g["page"]("Gasket Match", f"""
{upload_style}
<div class="work-shell"><section class="work-zone">
<form id="upload" class="home-form" method="post" action="/read-nameplate" enctype="multipart/form-data"><h2>Upload nameplate</h2>{warning}
<div class="grid"><div><label>Brand fallback</label><input name="brand"></div><div><label>Model fallback</label><input name="equipment_model"></div></div>
<div class="upload-row"><div><label>Nameplate photo</label><input type="file" name="nameplate" accept="image/*"></div><button type="submit">Read nameplate</button></div></form>
</section></div>
""")

    def evidence_html(package):
        if not package:
            return ""
        missing = package.get("missing_fields") or []
        items = package.get("items") or []
        missing_text = ", ".join([item.get("label") or item.get("field_name") or "" for item in missing[:6]]) or "None"
        rows = []
        for item in sorted(items, key=lambda row: float(row.get("confidence_score") or 0), reverse=True)[:6]:
            rows.append(
                f"""<div class="metric"><span>{esc(item.get('source_name') or item.get('evidence_type') or 'Evidence')}</span><strong>{esc(item.get('confidence_score') or 0)}%</strong><p>{esc(item.get('supports_value') or item.get('field_name') or '')}</p></div>"""
            )
        rows_html = "".join(rows) if rows else "<p class='muted'>Evidence is being collected.</p>"
        return f"""
<section><h2>Product evidence package</h2>
<div class="summary"><div class="metric"><span>Status</span><strong>{esc(package.get('status') or 'collecting')}</strong></div><div class="metric"><span>Completeness</span><strong>{esc(package.get('completeness_score') or 0)}%</strong></div><div class="metric"><span>Confidence</span><strong>{esc(package.get('overall_confidence') or 0)}%</strong></div></div>
<p class="muted">Missing or still being enriched: {esc(missing_text)}</p>
<div class="grid">{rows_html}</div></section>"""

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
        product_img = product.get("product_image_url")
        if product_img:
            try:
                from product_image_search_crawler import is_displayable_image_url

                with httpx.Client(timeout=5) as image_client:
                    if not is_displayable_image_url(image_client, product_img, timeout=3.0):
                        product_img = None
            except Exception:
                product_img = None
        g["trigger_background_refresh"](product["id"], not product_img, not quote_items)
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

        def gasket_size(item):
            width = item.get("width_in")
            height = item.get("height_in")
            if width not in (None, "") and height not in (None, ""):
                return f'{float(width):g}" x {float(height):g}"'
            dimensions = (item.get("dimensions_text") or "").strip()
            if dimensions:
                match = re.search(
                    r'(\d+(?:\.\d+)?(?:-\d+/\d+|/\d+)?|\d+\s+\d+/\d+)\s*(?:"|in|inch|inches)?\s*[x×]\s*(\d+(?:\.\d+)?(?:-\d+/\d+|/\d+)?|\d+\s+\d+/\d+)\s*(?:"|in|inch|inches)?',
                    dimensions,
                    re.IGNORECASE,
                )
                if match:
                    return f'{match.group(1).strip()}" x {match.group(2).strip()}"'
                blocked = ["not publicly", "official", "partsdr", "partselect", "confirm", "oem"]
                if not any(token in dimensions.lower() for token in blocked):
                    return dimensions
            perimeter = item.get("perimeter_in")
            if perimeter not in (None, ""):
                return f'Perimeter {float(perimeter):g}"'
            return "Size to confirm"

        rows = []
        if pending_new and not quote_items:
            rows.append(f"""<div class="item"><input type="checkbox" disabled><div class="loading" style="width:98px;height:78px;border:1px solid #dbe2ea;border-radius:6px"><span data-loading-label="{gasket_loading}">{gasket_loading} 00:00</span></div><div><strong>{gasket_loading}</strong></div><div class="price"><strong>Loading</strong></div><div></div></div>""")

        for index, item in enumerate(quote_items, start=1):
            door_label = item.get("door_position_display") or "Door position loading"
            door_key = item.get("door_position") or f"door_{index}"
            price = float(item.get("final_price_usd") or 0)
            image = item.get("gasket_image_url")
            image_html = f"<img src='{esc(image)}' alt='Gasket image'>" if image else "<div class='muted'>Gasket</div>"
            dims = gasket_size(item)
            rows.append(f"""<label class="item"><input type="checkbox" name="door_position" value="{esc(door_key)}" data-price="{price}" checked>{image_html}<div><strong>{esc(door_label)}</strong><p>{esc(dims)}</p></div><div class="price"><strong>{g['money'](price)}</strong><small>each selected door</small></div><div></div></label>""")

        if not quote_items and not pending_new:
            rows.append(f"""<div class="item"><input type="checkbox" disabled><div class="loading" style="width:98px;height:78px;border:1px solid #dbe2ea;border-radius:6px"><span data-loading-label="{gasket_loading}">{gasket_loading} 00:00</span></div><div><strong>{gasket_loading}</strong></div><div class="price"><strong>Loading</strong></div><div></div></div>""")

        summary_html = "" if pending_new else f"""<div class="summary"><div class="metric"><span>Door positions</span><strong>{len(quote_items)}</strong></div><div class="metric"><span>Selected</span><strong id="selected-count">0</strong></div><div class="metric"><span>Total</span><strong id="selected-total">$0.00</strong></div></div>"""
        rows_html = "".join(rows) if rows else f"""<div class="item"><input type="checkbox" disabled><div class="loading" style="width:98px;height:78px;border:1px solid #dbe2ea;border-radius:6px"><span data-loading-label="{gasket_loading}">{gasket_loading} 00:00</span></div><div><strong>{gasket_loading}</strong></div><div class="price"><strong>Loading</strong></div><div></div></div>"""
        return g["page"]("Matched Gasket Quote", f"""
<style>
.checkout-actions{{display:grid;grid-template-columns:minmax(360px,1fr) auto;gap:14px;align-items:end;margin-top:18px}}
.shipping-panel{{display:none;margin-top:18px;border:1px solid #dbe2ea;background:#fbfdfe;border-radius:8px;padding:16px}}
.shipping-panel.is-open{{display:block}}
.shipping-panel h3{{margin-top:0}}
.shipping-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}}
.shipping-grid .wide{{grid-column:span 2}}
.checkout-error{{display:none;margin-top:12px;border:1px solid #f2b8b5;background:#fff1f0;color:#5f1410;border-radius:8px;padding:12px}}
@media(max-width:760px){{.checkout-actions{{grid-template-columns:1fr}}.shipping-grid{{grid-template-columns:1fr}}.shipping-grid .wide{{grid-column:auto}}}}
</style>
<div data-refresh-product="{esc(product['id'])}" data-needs-image="{1 if needs_image else 0}" data-needs-gasket="{1 if needs_gasket else 0}" hidden></div>
{loading_banner}<section><h2>Matched refrigerator</h2><div class="result-grid"><div><h3>Refrigerator image</h3>{product_html}</div><div><h3>Nameplate</h3>{plate_html}</div><div><h3>Nameplate summary</h3><div class="facts"><div>OpenAI brand</div><div><strong>{esc(nameplate_data.get('brand') or product.get('brand'))}</strong></div><div>OpenAI model</div><div><strong>{esc(nameplate_data.get('model') or product.get('equipment_model'))}</strong></div><div>Serial</div><div>{esc(nameplate_data.get('serial_number') or 'Not found')}</div><div>Brand</div><div><strong>{esc(product.get('brand'))}</strong></div><div>Model</div><div><strong>{esc(product.get('equipment_model'))}</strong></div></div></div></div>{product_facts}</section>
<section><h2>Gasket quote</h2><form class="checkout-form" method="post" action="/checkout?product_id={esc(product['id'])}" data-product-id="{esc(product['id'])}"><input type="hidden" name="product_id" value="{esc(product['id'])}">{summary_html}<div>{rows_html}</div><div class="shipping-panel" data-shipping-panel><h3>Shipping information</h3><div class="shipping-grid"><label>Email<input data-required-check type="email" name="customer_email" autocomplete="email"></label><label>Name<input data-required-check name="customer_name" autocomplete="name"></label><label>Phone<input data-required-check name="customer_phone" autocomplete="tel"></label><label class="wide">Shipping address<input data-required-check name="shipping_address1" autocomplete="shipping address-line1"></label><label>Apt / Suite<input name="shipping_address2" autocomplete="shipping address-line2"></label><label>City<input data-required-check name="shipping_city" autocomplete="shipping address-level2"></label><label>State<input data-required-check name="shipping_state" autocomplete="shipping address-level1"></label><label>ZIP code<input data-required-check name="shipping_zip" autocomplete="shipping postal-code"></label><label>Country<input data-required-check name="shipping_country" value="United States" autocomplete="shipping country"></label></div></div><div class="checkout-actions"><div><p class="muted">No account required. Shipping details are collected only when you are ready to pay.</p></div><button type="submit" data-checkout-button>Purchase selected gaskets</button></div><div class="checkout-error" data-checkout-error></div></form></section>
<script>
document.querySelectorAll('.checkout-form').forEach(form=>form.addEventListener('submit',async event=>{{
  if(!window.fetch)return;
  event.preventDefault();
  const button=form.querySelector('button[type="submit"]');
  const shippingPanel=form.querySelector('[data-shipping-panel]');
  const errorBox=form.querySelector('[data-checkout-error]');
  if(errorBox){{errorBox.style.display='none';errorBox.textContent='';}}
  if(shippingPanel&&!shippingPanel.classList.contains('is-open')){{
    shippingPanel.classList.add('is-open');
    if(button)button.textContent='Continue to payment';
    shippingPanel.scrollIntoView({{behavior:'smooth',block:'center'}});
    return;
  }}
  const missing=[...form.querySelectorAll('[data-required-check]')].find(input=>!input.value.trim()||!input.checkValidity());
  if(missing){{
    missing.focus();
    missing.reportValidity?.();
    if(errorBox){{errorBox.textContent='Please complete the shipping information before checkout.';errorBox.style.display='block';}}
    return;
  }}
  if(button){{button.disabled=true;button.textContent='Preparing checkout...';}}
  try{{
    const data=new FormData(form);
    data.set('ajax','1');
    if(!data.get('product_id'))data.set('product_id',form.dataset.productId||'');
    const response=await fetch(form.action,{{method:'POST',body:new URLSearchParams(data),headers:{{'X-Requested-With':'fetch'}}}});
    const payload=await response.json();
    if(!response.ok||!payload.ok)throw new Error(payload.error||'Checkout is not ready.');
    window.location.href=payload.checkout_url;
  }}catch(error){{
    if(errorBox){{errorBox.textContent=error.message;errorBox.style.display='block';}}
  }}finally{{
    if(button){{button.disabled=false;button.textContent='Purchase selected gaskets';}}
  }}
}}));
</script>""")

    def patched_do_POST(self):
        path = g["urlparse"](self.path).path
        if path.lower() == "/admin/login":
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8", errors="replace")
            fields = g["parse_qs"](body)
            password = (fields.get("password") or [""])[0]
            if g.get("ADMIN_PASSWORD") and __import__("hmac").compare_digest(password, g["ADMIN_PASSWORD"]):
                cookie = f"{g['ADMIN_COOKIE_NAME']}={g['make_admin_cookie']()}; Path=/; Max-Age={g['ADMIN_SESSION_SECONDS']}; HttpOnly; SameSite=Lax"
                self.redirect("/ADMIN", cookie)
                return
            self.send_html(g["render_admin_login"]("Wrong password."), HTTPStatus.UNAUTHORIZED)
            return
        if path == "/checkout":
            g["handle_checkout_post"](self, self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            return
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
                if "fallback_nameplate_data" in g:
                    nameplate_data = g["fallback_nameplate_data"](exc, brand, model)
                else:
                    nameplate_data = {
                        "brand": brand or None,
                        "model": model or None,
                        "raw_text": "",
                        "confidence": 0,
                        "recognition_error": str(exc),
                    }
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
                from instant_enrichment import run_quick_image_task, start_instant_enrichment, upsert_known_product_from_nameplate, wait_for_core_result

                product = upsert_known_product_from_nameplate(client, brand, model, nameplate_data, status="customer_confirmed")
                start_instant_enrichment(product["id"], nameplate_data)
                quick_product = run_quick_image_task(product["id"])
                if quick_product:
                    product = quick_product
                waited = wait_for_core_result(product["id"], max_seconds=10)
                if waited.get("product"):
                    product = waited["product"]
                if not product.get("product_image_url") and quick_product:
                    product["product_image_url"] = quick_product.get("product_image_url")
                    product["product_image_source_url"] = quick_product.get("product_image_source_url")
                    product["product_image_confidence"] = quick_product.get("product_image_confidence")
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
                    product_img = product.get("product_image_url") if product else None
                    if product_img:
                        try:
                            from product_image_search_crawler import is_displayable_image_url

                            if not is_displayable_image_url(client, product_img, timeout=3.0):
                                product_img = None
                        except Exception:
                            product_img = None
                if product:
                    g["trigger_background_refresh"](product_id, not product_img, not quote_items)
                data = {
                    "product_image_url": product_img,
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
