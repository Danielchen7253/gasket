"""Runtime patch for the deployed nameplate app.

Render starts nameplate_web_app.py directly. This module is imported by Python
before that script runs, so it installs a tiny trace hook and replaces the
customer-facing render helpers after the script finishes defining them but before
main() starts the server.
"""

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

    def patched_page(title, body):
        html = old_page(title, body).decode("utf-8")
        if "function startLoadingTimers" not in html:
            html = html.replace(
                "<script>function updateTotal(){",
                "<script>function fmt(s){let m=Math.floor(s/60),r=s%60;return String(m).padStart(2,'0')+':'+String(r).padStart(2,'0')}function startLoadingTimers(){let start=Date.now();setInterval(()=>{let s=Math.floor((Date.now()-start)/1000);document.querySelectorAll('[data-loading-label]').forEach(el=>{el.textContent=el.getAttribute('data-loading-label')+' '+fmt(s)})},1000)}function updateTotal(){",
                1,
            )
            html = html.replace(
                "window.addEventListener('load',updateTotal);",
                "window.addEventListener('load',updateTotal);window.addEventListener('load',startLoadingTimers);",
                1,
            )
        return html.encode("utf-8")

    def patched_render_no_match(brand, model, upload_url, nameplate_data):
        plate = f"<img class='plate' src='{esc(upload_url)}' alt='Uploaded nameplate'>" if upload_url else ""
        return g["page"]("No Match", f"""
<section><h2>&#25105;&#20204;&#27491;&#22312;&#21152;&#36733;&#36164;&#26009;</h2>
<p class="muted">&#24050;&#25910;&#21040;&#35813;&#20912;&#31665;&#22411;&#21495;&#65292;&#31995;&#32479;&#27491;&#22312;&#21305;&#37197;&#20135;&#21697;&#22270;&#29255;&#12289;&#38376;&#20301;&#21644;&#23494;&#23553;&#26465;&#36164;&#26009;&#12290;</p>
{plate}<div class="facts"><div>Brand read</div><div><strong>{esc(brand or 'Not found')}</strong></div><div>Model read</div><div><strong>{esc(model or 'Not found')}</strong></div><div>Serial</div><div>{esc(nameplate_data.get('serial_number') or 'Not found')}</div><div>Raw text</div><div>{esc(nameplate_data.get('raw_text') or '')}</div></div>
<p><a class="button" href="/">Try another nameplate</a></p></section>""")

    def patched_render_result(product, quote_items, request, upload_url):
        nameplate_data = (request or {}).get("nameplate_data") or {}
        pending_new = (
            product.get("data_status") == "customer_requested"
            and not product.get("door_layout_source")
            and not product.get("door_positions")
        )
        positions = [] if pending_new else g["infer_door_positions"](product)
        quantity = 0 if pending_new else (len(positions) or g["estimated_gasket_quantity"](product, quote_items))
        g["trigger_background_refresh"](product["id"], not product.get("product_image_url"), not quote_items)
        product_img = product.get("product_image_url")
        needs_image = not bool(product_img)
        needs_gasket = not bool(quote_items)
        product_loading = "&#22270;&#29255;&#27491;&#22312;&#21152;&#36733;"
        gasket_loading = "&#23494;&#23553;&#26465;&#36164;&#26009;&#27491;&#22312;&#21152;&#36733;"
        loading_banner = "<section><h2>&#25105;&#20204;&#27491;&#22312;&#21152;&#36733;&#36164;&#26009;</h2></section>" if needs_image or needs_gasket else ""
        product_html = f"<img class='photo' src='{esc(product_img)}' alt='Refrigerator product image'>" if product_img else f"<div class='photo loading'><span data-loading-label='{product_loading}'>{product_loading} 00:00</span></div>"
        plate_html = f"<img class='plate' src='{esc(upload_url)}' alt='Uploaded nameplate'>" if upload_url else "<div class='plate muted'>Nameplate photo</div>"

        rows = []
        primary_item = quote_items[0] if quote_items else None
        if pending_new and not primary_item:
            rows.append(f"""<div class="item"><input type="checkbox" disabled><div class="loading" style="width:98px;height:78px;border:1px solid #dbe2ea;border-radius:6px"><span data-loading-label="{gasket_loading}">{gasket_loading} 00:00</span></div><div><strong>{gasket_loading}</strong></div><div class="price"><strong>Loading</strong></div><div></div></div>""")

        for index, position in enumerate(positions or g["door_positions_for_count"](quantity), start=1):
            item = primary_item
            door_label = position.get("label") or f"Door {index}"
            door_key = position.get("key") or f"door_{index}"
            if not item:
                rows.append(f"""<div class="item"><input type="checkbox" disabled><div class="loading" style="width:98px;height:78px;border:1px solid #dbe2ea;border-radius:6px"><span data-loading-label="{gasket_loading}">{gasket_loading} 00:00</span></div><div><strong>{esc(door_label)} Gasket</strong></div><div class="price"><strong>Loading</strong></div><div><small class="muted">Door</small><br><strong>{esc(door_key)}</strong></div></div>""")
                continue
            price = float(item.get("final_price_usd") or 0)
            image = item.get("gasket_image_url")
            image_html = f"<img src='{esc(image)}' alt='Gasket image'>" if image else "<div class='muted'>No gasket image</div>"
            dims = item.get("dimensions_text") or f"{item.get('width_in') or '-'} x {item.get('height_in') or '-'} in"
            perimeter = item.get("perimeter_in")
            perimeter_html = f"<br>Perimeter: {esc(perimeter)} in" if perimeter not in (None, "") else ""
            part_number = item.get("part_number") or item.get("universal_part_number")
            part_html = f"<div><small class='muted'>Part</small><br><strong>{esc(part_number)}</strong></div>" if part_number else "<div></div>"
            rows.append(f"""<label class="item"><input type="checkbox" name="door_position" value="{esc(door_key)}" data-price="{price}" checked>{image_html}<div><strong>{esc(door_label)} Gasket</strong><p>{esc(dims)}{perimeter_html}<br>Source: {esc(item.get('source_name'))}</p></div><div class="price"><strong>{g['money'](price)}</strong><small>each selected door</small></div>{part_html}</label>""")

        summary_html = "" if pending_new else f"""<div class="summary"><div class="metric"><span>Required gaskets</span><strong>{quantity}</strong></div><div class="metric"><span>Selected</span><strong id="selected-count">0</strong></div><div class="metric"><span>Total</span><strong id="selected-total">$0.00</strong></div></div>"""
        rows_html = "".join(rows) if rows else f"""<div class="item"><input type="checkbox" disabled><div class="loading" style="width:98px;height:78px;border:1px solid #dbe2ea;border-radius:6px"><span data-loading-label="{gasket_loading}">{gasket_loading} 00:00</span></div><div><strong>{gasket_loading}</strong></div><div class="price"><strong>Loading</strong></div><div></div></div>"""
        return g["page"]("Matched Gasket Quote", f"""
<div data-refresh-product="{esc(product['id'])}" data-needs-image="{1 if needs_image else 0}" data-needs-gasket="{1 if needs_gasket else 0}" hidden></div>
{loading_banner}<section><h2>Matched refrigerator</h2><div class="result-grid"><div><h3>Refrigerator image</h3>{product_html}</div><div><h3>Nameplate</h3>{plate_html}</div><div><h3>Nameplate summary</h3><div class="facts"><div>OpenAI brand</div><div><strong>{esc(nameplate_data.get('brand') or product.get('brand'))}</strong></div><div>OpenAI model</div><div><strong>{esc(nameplate_data.get('model') or product.get('equipment_model'))}</strong></div><div>Serial</div><div>{esc(nameplate_data.get('serial_number') or 'Not found')}</div><div>Brand</div><div><strong>{esc(product.get('brand'))}</strong></div><div>Model</div><div><strong>{esc(product.get('equipment_model'))}</strong></div></div></div></div></section>
<section><h2>Gasket quote</h2>{summary_html}<div>{rows_html}</div></section>
<div class="checkout"><strong>Ready to order?</strong><br><span class="muted">Select the gasket solution for this refrigerator.</span></div>""")

    g["page"] = patched_page
    g["render_no_match"] = patched_render_no_match
    g["render_result"] = patched_render_result
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
