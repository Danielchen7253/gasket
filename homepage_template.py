"""Shared homepage template for the customer upload page."""


def render_premium_home(page, esc, warning: str = "") -> bytes:
    return page("Gasket Match", f"""
<style>
body{{background:#f4f6f8}}
main{{max-width:none;padding:0}}
.app-header-inner{{border-color:#d6dce5;box-shadow:0 14px 32px rgba(5,24,57,.08)}}
.app-logo{{background:#082a62}}
button,.button{{background:#092b63;border-radius:7px;box-shadow:0 8px 18px rgba(9,43,99,.18)}}
button:hover,.button:hover{{background:#0d3a82}}
.home-wrap{{max-width:1180px;margin:0 auto;padding:26px 22px 34px}}
.home-hero{{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(340px,.9fr);gap:24px;align-items:stretch}}
.brand-card{{position:relative;overflow:hidden;border:1px solid #cfd7e3;border-radius:16px;background:linear-gradient(135deg,#fff 0%,#eef1f4 44%,#d8dde3 100%);box-shadow:0 22px 44px rgba(7,25,58,.12);min-height:540px;padding:28px}}
.brand-card:before{{content:"";position:absolute;left:-8%;top:0;width:64%;height:120px;background:repeating-linear-gradient(165deg,#c9222f 0 12px,#fff 12px 24px,#0b2d67 24px 38px);opacity:.95;clip-path:polygon(0 0,100% 0,82% 72%,0 100%)}}
.brand-card:after{{content:"";position:absolute;left:0;right:0;bottom:0;height:74px;background:#082a62;border-top:7px solid #c9222f}}
.brand-inner{{position:relative;z-index:1;height:100%;display:flex;flex-direction:column}}
.brand-top{{display:flex;justify-content:flex-end;min-height:82px}}
.stamp{{border:2px solid #092b63;color:#092b63;border-radius:999px;padding:10px 15px;font-weight:900;letter-spacing:.08em;background:rgba(255,255,255,.7)}}
.brand-name{{margin-top:26px;font-size:64px;line-height:.92;font-weight:900;letter-spacing:.01em;color:#081624;text-transform:uppercase}}
.brand-name span{{color:#092b63}}
.brand-sub{{font-size:19px;letter-spacing:.16em;color:#0b2d67;font-weight:800;text-transform:uppercase;margin-top:12px}}
.red-stars{{color:#c9222f;font-size:28px;letter-spacing:9px;margin:18px 0 10px}}
.brand-ribbon{{align-self:flex-start;background:linear-gradient(180deg,#123c82,#071f4d);color:white;border-radius:7px;padding:10px 22px;font-size:25px;font-weight:900;letter-spacing:.1em;text-transform:uppercase;box-shadow:0 10px 22px rgba(9,43,99,.25)}}
.brand-copy{{margin-top:34px;display:grid;grid-template-columns:1fr 1fr;gap:24px 34px;color:#101923}}
.brand-copy strong{{display:block;font-size:34px;color:#092b63;line-height:1}}
.brand-copy span{{font-weight:800;text-transform:uppercase;letter-spacing:.05em}}
.brand-footer{{margin-top:auto;position:relative;z-index:2;color:white;display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px;align-items:end;font-weight:800;padding-top:28px}}
.brand-footer div{{font-size:14px;line-height:1.3}}
.match-panel{{background:white;border:1px solid #cfd7e3;border-radius:16px;box-shadow:0 22px 44px rgba(7,25,58,.12);padding:28px;display:flex;flex-direction:column;gap:18px}}
.panel-kicker{{font-weight:900;color:#c9222f;letter-spacing:.08em;text-transform:uppercase;font-size:13px}}
.match-panel h1{{font-size:38px;line-height:1.05;color:#081624;margin:0}}
.match-panel p{{font-size:16px;margin:0}}
.trust-row{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}}
.trust-pill{{border:1px solid #dbe2ea;border-radius:10px;padding:12px;background:#f8fafc}}
.trust-pill strong{{display:block;color:#092b63;font-size:18px}}
.home-form{{margin-top:auto;border:1px solid #d6dce5;border-radius:14px;background:#fbfcfd;padding:18px}}
.home-form h2{{color:#081624;margin-bottom:8px}}
.home-form .grid{{grid-template-columns:1fr 1fr;margin-bottom:14px}}
.home-form input{{background:white;border-color:#cfd7e3}}
.upload-row{{grid-template-columns:minmax(0,1fr) auto;margin-top:8px}}
.note-strip{{border-left:5px solid #c9222f;background:#fff;padding:12px 14px;border-radius:8px;color:#445066}}
.home-bottom{{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-top:22px}}
.home-bottom section{{border-radius:14px;margin:0}}
.home-bottom strong{{display:block;color:#092b63;margin-bottom:6px}}
@media(max-width:960px){{.home-hero,.home-bottom{{grid-template-columns:1fr}}.brand-card{{min-height:480px}}.brand-name{{font-size:48px}}}}
@media(max-width:640px){{.brand-copy,.brand-footer,.trust-row,.home-form .grid,.upload-row{{grid-template-columns:1fr}}.brand-name{{font-size:40px}}.brand-card,.match-panel{{padding:20px}}}}
</style>
<div class="home-wrap">
<div class="home-hero">
<div class="brand-card">
<div class="brand-inner">
<div class="brand-top"><div class="stamp">AMERICAN BRAND</div></div>
<div class="brand-name">CoolFix <span>Pro</span></div>
<div class="brand-sub">Premium refrigerator gasket matching</div>
<div class="red-stars">*****</div>
<div class="brand-ribbon">Nameplate fit check</div>
<div class="brand-copy">
<div><strong>45+</strong><span>Starting gasket quote</span></div>
<div><strong>24/7</strong><span>Online model intake</span></div>
<div><strong>OEM</strong><span>Cross reference workflow</span></div>
<div><strong>PDF</strong><span>Quote file after match</span></div>
</div>
<div class="brand-footer">
<div>Built for reliability<br>Designed to perform</div>
<div>www.coolfixpro.com</div>
<div>Designed in USA<br>Quality you can trust</div>
</div>
</div>
</div>
<div class="match-panel">
<div class="panel-kicker">Refrigerator door gasket match</div>
<h1>Upload the nameplate. Get the right gasket options.</h1>
<p>We read the refrigerator model, let you confirm the exact number, then match the product and door gasket records for ordering.</p>
<div class="trust-row">
<div class="trust-pill"><strong>1</strong>Upload photo</div>
<div class="trust-pill"><strong>2</strong>Confirm model</div>
<div class="trust-pill"><strong>3</strong>Download quote</div>
</div>
<div class="note-strip">Check the model number carefully before matching. One wrong character can point to a different refrigerator.</div>
<form id="upload" class="home-form" method="post" action="/read-nameplate" enctype="multipart/form-data">
<h2>Start gasket match</h2>{warning}
<div class="grid"><div><label>Brand fallback</label><input name="brand" placeholder="Whirlpool, True, Sub-Zero"></div><div><label>Model fallback</label><input name="equipment_model" placeholder="WRF535SMHZ03"></div></div>
<div class="upload-row"><div><label>Nameplate photo</label><input type="file" name="nameplate" accept="image/*"></div><button type="submit">Read nameplate</button></div>
</form>
</div>
</div>
<div class="home-bottom">
<section><strong>For homeowners</strong><span class="muted">No part number required. Send the refrigerator nameplate first.</span></section>
<section><strong>For technicians</strong><span class="muted">Door-position gasket selection and online checkout flow.</span></section>
<section><strong>For custom gaskets</strong><span class="muted">Size-based pricing with PDF quote output after matching.</span></section>
</div>
</div>""")
