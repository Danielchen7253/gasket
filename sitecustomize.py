"""Runtime patch for the deployed nameplate app."""

import re
import sys

_PATCHED = False


def _install_patch(g):
    global _PATCHED
    if _PATCHED:
        return
    required = [
        "page",
        "esc",
        "trigger_background_refresh",
        "infer_door_positions",
        "estimated_gasket_quantity",
        "door_positions_for_count",
        "money",
    ]
    if any(name not in g for name in required):
        return

    old_page = g["page"]
    esc = g["esc"]

    def patched_trigger_background_refresh(product_id: int, need_image: bool, need_gaskets: bool) -> None:
        try:
            if need_image or need_gaskets:
                from instant_enrichment import start_instant_enrichment

                start_instant_enrichment(product_id)
        except Exception as exc:
            print(f"instant enrichment trigger failed for {product_id}: {exc}", flush=True)
        return
        refreshing = g["BACKGROUND_REFRESHING"]
        if product_id in refreshing:
            return
        if not need_image and not need_gaskets:
            return
        refreshing.add(product_id)

        def worker() -> None:
            try:
                with g["httpx"].Client(timeout=60) as client:
                    product = g["get_product"](client, product_id)
                    if not product:
                        return
                    if need_image and not product.get("product_image_url"):
                        try:
                            from product_image_search_crawler import (
                                get_existing_candidates,
                                promote_best_image,
                                search_google_cse,
                                search_public_web_images,
                                search_serpapi,
                                upsert_candidate,
                            )
                            from fast_image_patch import quick_promote_product_image

                            promoted = quick_promote_product_image(client, product)
                            if not promoted:
                                saved = get_existing_candidates(client, product_id)
                                promoted = promote_best_image(client, product, saved)
                            if not promoted:
                                raw = []
                                raw.extend(search_serpapi(client, product))
                                raw.extend(search_google_cse(client, product))
                                if not raw:
                                    raw.extend(search_public_web_images(client, product))
                                saved = [upsert_candidate(client, product, row) for row in raw[:20]]
                                promote_best_image(client, product, saved)
                        except Exception as exc:
                            print(f"background image refresh failed for {product_id}: {exc}")
                    if need_gaskets:
                        try:
                            from gasket_spec_refresher import refresh_product_gasket_spec

                            refresh_product_gasket_spec(client, product_id)
                        except Exception as exc:
                            print(f"background gasket refresh failed for {product_id}: {exc}")
            finally:
                refreshing.discard(product_id)

        g["threading"].Thread(target=worker, daemon=True).start()

    def patched_is_unconfirmed_new_product(product):
        return (
            not product.get("product_image_url")
            and not product.get("door_layout_source")
            and not product.get("door_positions")
        )

    def patched_page(title, body):
        html = old_page(title, body).decode("utf-8")
        if "function startLoadingTimers" not in html:
            html = html.replace(
                "<script>function updateTotal(){",
                "<script>function fmt(s){let m=Math.floor(s/60),r=s%60;return String(m).padStart(2,'0')+':'+String(r).padStart(2,'0')}function startLoadingTimers(){let start=Date.now();setInterval(()=>{let s=Math.floor((Date.now()-start)/1000);document.querySelectorAll('[data-loading-label]').forEach(el=>{el.textContent=el.getAttribute('data-loading-label')+' '+fmt(s)})},1000)}function updateTotal(){",
                1,
            )
        if "function startConfirmCountdown" not in html:
            html = html.replace(
                "function updateTotal(){",
                "function startConfirmCountdown(){let b=document.getElementById('confirm-match');if(!b)return;let left=10;b.disabled=true;b.textContent='System matching '+left+'s';let timer=setInterval(()=>{left--;if(left>0){b.textContent='System matching '+left+'s'}else{clearInterval(timer);b.disabled=false;b.textContent='Confirm and match gasket records'}},1000)}function updateTotal(){",
                1,
            )
            html = html.replace(
                "window.addEventListener('load',updateTotal);",
                "window.addEventListener('load',updateTotal);window.addEventListener('load',startConfirmCountdown);",
                1,
            )
            html = html.replace(
                "window.addEventListener('load',updateTotal);",
                "window.addEventListener('load',updateTotal);window.addEventListener('load',startLoadingTimers);",
                1,
            )
        html = html.replace("},5000)}window.addEventListener('load',pollProductStatus)", "},2000)}window.addEventListener('load',pollProductStatus)")
        return html.encode("utf-8")

    def patched_render_no_match(brand, model, upload_url, nameplate_data):
        plate = f"<img class='plate' src='{esc(upload_url)}' alt='Uploaded nameplate'>" if upload_url else ""
        return g["page"]("No Match", f"""
<section><h2>&#25105;&#20204;&#27491;&#22312;&#21152;&#36733;&#36164;&#26009;</h2>
<p class="muted">&#24050;&#25910;&#21040;&#35813;&#20912;&#31665;&#22411;&#21495;&#65292;&#31995;&#32479;&#27491;&#22312;&#21305;&#37197;&#20135;&#21697;&#22270;&#29255;&#12289;&#38376;&#20301;&#21644;&#23494;&#23553;&#26465;&#36164;&#26009;&#12290;</p>
{plate}<div class="facts"><div>Brand read</div><div><strong>{esc(brand or 'Not found')}</strong></div><div>Model read</div><div><strong>{esc(model or 'Not found')}</strong></div><div>Serial</div><div>{esc(nameplate_data.get('serial_number') or 'Not found')}</div><div>Raw text</div><div>{esc(nameplate_data.get('raw_text') or '')}</div></div>
<p><a class="button" href="/">Try another nameplate</a></p></section>""")

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

    def patched_render_home(message=""):
        warning = f"<p style='color:#9f4b12'>{esc(message)}</p>" if message else ""
        upload_style = """
<style>
main{max-width:none;padding:0}
.work-zone{max-width:1180px;margin:0 auto;display:flex;justify-content:center;align-items:flex-start}
.work-shell{background:#eef3f6;padding:34px 22px 38px}
.home-form{background:#fff;border:1px solid #dbe2ea;border-radius:8px;padding:22px;margin:0}
.upload-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px;align-items:end;margin-bottom:12px}
.upload-row button{width:auto;white-space:nowrap}
.home-form .grid{grid-template-columns:1fr 1fr;margin-top:12px}
@media(max-width:900px){.home-form .grid{grid-template-columns:1fr}.upload-row{grid-template-columns:1fr}.upload-row button{width:100%;justify-content:center;text-align:center}}
</style>"""
        return g["page"]("Gasket Match", f"""
{upload_style}
<div class="work-shell"><section class="work-zone">
<form id="upload" class="home-form" method="post" action="/read-nameplate" enctype="multipart/form-data"><h2>Upload nameplate</h2>{warning}
<div class="upload-row"><div><label>Nameplate photo</label><input type="file" name="nameplate" accept="image/*"></div><button type="submit">Read nameplate</button></div>
<div class="grid"><div><label>Brand fallback</label><input name="brand"></div><div><label>Model fallback</label><input name="equipment_model"></div></div>
<p class="muted">If the photo is hard to read, type the brand or model here before submitting.</p></form>
</section></div>""")

    def patched_render_result(product, quote_items, request, upload_url):
        nameplate_data = (request or {}).get("nameplate_data") or {}
        pending_new = g["is_unconfirmed_new_product"](product)
        g["trigger_background_refresh"](product["id"], not product.get("product_image_url"), not quote_items)
        product_img = product.get("product_image_url")
        needs_image = not bool(product_img)
        needs_gasket = not bool(quote_items)
        product_loading = "&#22270;&#29255;&#27491;&#22312;&#21152;&#36733;"
        gasket_loading = "&#23494;&#23553;&#26465;&#36164;&#26009;&#27491;&#22312;&#21152;&#36733;"
        loading_banner = "<section><h2>&#25105;&#20204;&#27491;&#22312;&#21152;&#36733;&#36164;&#26009;</h2></section>" if needs_image or needs_gasket else ""
        product_html = f"<img class='photo' src='{esc(product_img)}' alt='Refrigerator product image'>" if product_img else f"<div class='photo loading'><span data-loading-label='{product_loading}'>{product_loading} 00:00</span></div>"
        plate_html = f"<img class='plate' src='{esc(upload_url)}' alt='Uploaded nameplate'>" if upload_url else "<div class='plate muted'>Nameplate photo</div>"

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
            image_html = f"<img src='{esc(image)}' alt='Gasket image'>" if image else "<div class='muted'>No gasket image</div>"
            dims = gasket_size(item)
            rows.append(f"""<label class="item"><input type="checkbox" name="door_position" value="{esc(door_key)}" data-price="{price}" checked>{image_html}<div><strong>{esc(door_label)}</strong><p>{esc(dims)}</p></div><div class="price"><strong>{g['money'](price)}</strong><small>each selected door</small></div><div></div></label>""")

        if not quote_items and not pending_new:
            rows.append(f"""<div class="item"><input type="checkbox" disabled><div class="loading" style="width:98px;height:78px;border:1px solid #dbe2ea;border-radius:6px"><span data-loading-label="{gasket_loading}">{gasket_loading} 00:00</span></div><div><strong>{gasket_loading}</strong></div><div class="price"><strong>Loading</strong></div><div></div></div>""")

        summary_html = "" if pending_new else f"""<div class="summary"><div class="metric"><span>Door positions</span><strong>{len(quote_items)}</strong></div><div class="metric"><span>Selected</span><strong id="selected-count">0</strong></div><div class="metric"><span>Total</span><strong id="selected-total">$0.00</strong></div></div>"""
        rows_html = "".join(rows) if rows else f"""<div class="item"><input type="checkbox" disabled><div class="loading" style="width:98px;height:78px;border:1px solid #dbe2ea;border-radius:6px"><span data-loading-label="{gasket_loading}">{gasket_loading} 00:00</span></div><div><strong>{gasket_loading}</strong></div><div class="price"><strong>Loading</strong></div><div></div></div>"""
        return g["page"]("Matched Gasket Quote", f"""
<div data-refresh-product="{esc(product['id'])}" data-needs-image="{1 if needs_image else 0}" data-needs-gasket="{1 if needs_gasket else 0}" hidden></div>
{loading_banner}<section><h2>Matched refrigerator</h2><div class="result-grid"><div><h3>Refrigerator image</h3>{product_html}</div><div><h3>Nameplate</h3>{plate_html}</div><div><h3>Nameplate summary</h3><div class="facts"><div>OpenAI brand</div><div><strong>{esc(nameplate_data.get('brand') or product.get('brand'))}</strong></div><div>OpenAI model</div><div><strong>{esc(nameplate_data.get('model') or product.get('equipment_model'))}</strong></div><div>Serial</div><div>{esc(nameplate_data.get('serial_number') or 'Not found')}</div><div>Brand</div><div><strong>{esc(product.get('brand'))}</strong></div><div>Model</div><div><strong>{esc(product.get('equipment_model'))}</strong></div></div></div></div></section>
<section><h2>Gasket quote</h2><form method="post" action="/checkout"><input type="hidden" name="product_id" value="{esc(product['id'])}">{summary_html}<div>{rows_html}</div><p><button type="submit">Checkout selected gaskets</button></p></form></section>""")

    old_do_GET = g["Handler"].do_GET

    def patched_do_GET(self):
        parsed = g["urlparse"](self.path)
        if parsed.path == "/product-status":
            product_id = int(g["parse_qs"](parsed.query).get("product_id", ["0"])[0])
            if product_id:
                with g["httpx"].Client(timeout=30) as client:
                    product = g["get_product"](client, product_id)
                    quote_items = g["get_quote_items"](client, product_id) if product else []
                if product:
                    g["trigger_background_refresh"](product_id, not product.get("product_image_url"), not quote_items)
                data = {
                    "product_image_url": product.get("product_image_url") if product else None,
                    "quote_item_count": len(quote_items),
                }
                payload = g["json"].dumps(data).encode("utf-8")
                self.send_response(g["HTTPStatus"].OK)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
        old_do_GET(self)

    g["page"] = patched_page
    g["trigger_background_refresh"] = patched_trigger_background_refresh
    g["is_unconfirmed_new_product"] = patched_is_unconfirmed_new_product
    g["render_home"] = patched_render_home
    g["render_no_match"] = patched_render_no_match
    g["render_result"] = patched_render_result
    g["Handler"].do_GET = patched_do_GET
    _PATCHED = True


def _trace(frame, event, arg):
    if event == "line" and frame.f_globals.get("__name__") == "__main__":
        g = frame.f_globals
        if "Handler" in g and "main" in g and "render_result" in g:
            _install_patch(g)
            sys.settrace(None)
            return None
    return _trace


sys.settrace(_trace)
