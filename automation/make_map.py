import json, sys, base64, re

county, run_date, window, src, infile, outfile, mode = sys.argv[1:8]
# mode: 'artifact' -> body-only fragment (Artifact tool wraps skeleton)
#       'standalone' -> full html doc (for local file / device delivery)

comps = json.load(open(infile))
title = f"{county} County Vacant Land Comps"
sub = f"Run date {run_date} &middot; Sale dates {window} &middot; &gt;0.5 ac &amp; &gt;$500k &middot; {len(comps)} new comps &middot; Source: {src}"

import os
libdir = os.environ.get('MAP_LIBS', 'libs') .rstrip('/') + '/'
leaflet_css = open(libdir+'leaflet.css', encoding='utf-8').read()
leaflet_js = open(libdir+'leaflet.js', encoding='utf-8').read()
esri_js = open(libdir+'esri-leaflet.js', encoding='utf-8').read()

def b64(path, mime):
    data = open(path, 'rb').read()
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"

marker_icon = b64(libdir+'marker-icon.png', 'image/png')
marker_icon_2x = b64(libdir+'marker-icon-2x.png', 'image/png')
marker_shadow = b64(libdir+'marker-shadow.png', 'image/png')

# strip the two remaining raster url() refs from leaflet.css (layers control -- unused, avoid 404s)
leaflet_css = re.sub(r"url\(images/layers[^)]*\)", "none", leaflet_css)
leaflet_css = re.sub(r"url\(images/marker-icon\.png\)", f"url({marker_icon})", leaflet_css)

BODY = """<title>__COUNTY__ Land Comps</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600&family=Source+Sans+3:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#f5f2ec; --surface:#ffffff; --surface-2:#efe9de; --line:#ddd4c2;
  --ink:#241a16; --ink-2:#5b5049; --accent:#8a1f2b; --accent-ink:#ffffff;
  --accent-2:#2f4a3c; --shadow:0 1px 3px rgba(36,26,22,.08);
  color-scheme: light;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --bg:#171310; --surface:#211b17; --surface-2:#2a221d; --line:#3a3029;
    --ink:#efe7de; --ink-2:#b6a99b; --accent:#e0555f; --accent-ink:#1a0f0f;
    --accent-2:#7fae95; --shadow:0 1px 3px rgba(0,0,0,.4);
    color-scheme: dark;
  }
}
:root[data-theme="dark"]{
  --bg:#171310; --surface:#211b17; --surface-2:#2a221d; --line:#3a3029;
  --ink:#efe7de; --ink-2:#b6a99b; --accent:#e0555f; --accent-ink:#1a0f0f;
  --accent-2:#7fae95; --shadow:0 1px 3px rgba(0,0,0,.4);
  color-scheme: dark;
}
*{box-sizing:border-box}
body{margin:0;padding-inline:0;font-family:'Source Sans 3',-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:var(--ink);background:var(--bg)}
header{background:var(--surface);border-bottom:1px solid var(--line);padding:18px 22px;display:flex;flex-direction:column;gap:4px}
header h1{margin:0;font-family:'Fraunces',Georgia,serif;font-weight:600;font-size:22px;letter-spacing:.01em;color:var(--ink)}
header p{margin:0;font-size:13px;color:var(--ink-2)}
header .eyebrow{font-family:'JetBrains Mono',monospace;font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--accent)}
#map{height:56vh;min-height:380px;width:100%;border-bottom:1px solid var(--line)}
.wrap{padding:16px clamp(16px,3vw,22px) 40px;}
.legend{font-size:12.5px;color:var(--ink-2);margin:0 0 12px}
.tablewrap{overflow-x:auto;border:1px solid var(--line);border-radius:8px;box-shadow:var(--shadow);background:var(--surface)}
table{border-collapse:collapse;width:100%;font-size:12.5px;min-width:1180px}
th,td{border-bottom:1px solid var(--line);padding:7px 9px;text-align:left;vertical-align:top;color:var(--ink)}
th{background:var(--surface-2);position:sticky;top:0;font-family:'JetBrains Mono',monospace;font-size:10.5px;letter-spacing:.05em;text-transform:uppercase;color:var(--ink-2);font-weight:500}
tbody tr:hover{background:var(--surface-2)}
a{color:var(--accent)}
.num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
.pop{font-size:12.5px;line-height:1.55;font-family:'Source Sans 3',sans-serif;color:#1a1a1a}
.pop b{color:#8a1f2b}
""" + leaflet_css + """
</style>
<header>
<span class="eyebrow">Eshenbaugh Land Company &middot; Weekly Land Comps</span>
<h1>__TITLE__</h1>
<p>__SUB__</p>
</header>
<div id="map"></div>
<div class="wrap">
<p class="legend">Click any marker for full detail. Coordinates are exact parcel centroids from the __COUNTY__ County PA GIS endpoint, not geocoded addresses, unless noted otherwise per record.</p>
<div class="tablewrap">
<table id="tbl"><thead><tr>
<th>#</th><th>Sale Date</th><th>Address</th><th>Grantor (Seller)</th><th>Grantee (Buyer)</th>
<th class="num">Price</th><th class="num">Acres</th><th class="num">$/Acre</th><th>Deed</th><th>Land Use</th><th>Zoning</th><th>Parcel ID</th><th>Book/Page</th><th>PA Page</th><th>Notes</th>
</tr></thead><tbody></tbody></table>
</div>
</div>
<script>""" + leaflet_js + """</script>
<script>""" + esri_js + """</script>
<script>
L.Icon.Default.mergeOptions({
  iconUrl: '""" + marker_icon + """',
  iconRetinaUrl: '""" + marker_icon_2x + """',
  shadowUrl: '""" + marker_shadow + """'
});
var comps=__DATA__;
var map=L.map('map');
L.esri.basemapLayer('Imagery').addTo(map);
L.esri.basemapLayer('ImageryLabels').addTo(map);
var money=function(n){return n==null?'\\u2014':'$'+Number(n).toLocaleString();};
var esc=function(s){return s==null?'\\u2014':String(s).replace(/[&<>]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;'}[c];});};
var grp=L.featureGroup().addTo(map);
comps.forEach(function(c,i){
  var pop='<div class="pop"><b>#'+(i+1)+' \\u00b7 '+money(c.price)+'</b><br/>'+esc(c.address)+'<br/><br/>'
   +'<b>Grantor:</b> '+esc(c.grantor)+'<br/>'
   +'<b>Grantee:</b> '+esc(c.grantee)+'<br/>'
   +'<b>Sale date:</b> '+esc(c.sale_date)+'<br/>'
   +'<b>Acreage:</b> '+c.acreage+' ac<br/>'
   +'<b>$/acre:</b> '+money(c.price_per_acre)+'<br/>'
   +'<b>Deed type:</b> '+esc(c.deed_type)+'<br/>'
   +'<b>Land use:</b> '+esc(c.land_use)+'<br/>'
   +'<b>Zoning:</b> '+esc(c.zoning)+'<br/>'
   +'<b>Parcel:</b> '+esc(c.parcel_id)+'<br/>'
   +'<b>Book/Page:</b> '+esc(c.book_page)+'<br/>'
   +(c.comments?('<b>Notes:</b> '+esc(c.comments)+'<br/>'):'')
   +'<a href="'+c.aerial+'" target="_blank" rel="noopener">Open PA parcel page \\u2192</a></div>';
  if(c.lat==null||c.lon==null) return;
  var m=L.marker([c.lat,c.lon]).addTo(grp).bindPopup(pop,{maxWidth:340});
  m.bindTooltip('#'+(i+1)+' '+money(c.price));
});
if(grp.getLayers().length) map.fitBounds(grp.getBounds().pad(0.25)); else map.setView([28.3,-82.4],10);
var tb=document.querySelector('#tbl tbody');
comps.forEach(function(c,i){
  var tr=document.createElement('tr');
  tr.innerHTML='<td>'+(i+1)+'</td><td>'+esc(c.sale_date)+'</td><td>'+esc(c.address)+'</td>'
   +'<td>'+esc(c.grantor)+'</td><td>'+esc(c.grantee)+'</td>'
   +'<td class="num">'+money(c.price)+'</td><td class="num">'+c.acreage+'</td>'
   +'<td class="num">'+money(c.price_per_acre)+'</td><td>'+esc(c.deed_type)+'</td>'
   +'<td>'+esc(c.land_use)+'</td><td>'+esc(c.zoning)+'</td><td>'+esc(c.parcel_id)+'</td>'
   +'<td>'+esc(c.book_page)+'</td>'
   +'<td><a href="'+c.aerial+'" target="_blank" rel="noopener">link</a></td>'
   +'<td>'+esc(c.comments)+'</td>';
  tr.style.cursor='pointer';
  tr.onclick=function(){ if(c.lat!=null) map.setView([c.lat,c.lon],16); };
  tb.appendChild(tr);
});
</script>
"""

BODY = (BODY.replace('__TITLE__', title).replace('__SUB__', sub)
        .replace('__COUNTY__', county).replace('__DATA__', json.dumps(comps)))

if mode == 'standalone':
    body_no_title_tag = re.sub(r'^<title>.*?</title>\n', '', BODY, count=1)
    doc = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{title} &mdash; {run_date}</title>
</head><body>
{body_no_title_tag}
</body></html>"""
    open(outfile, 'w', encoding='utf-8').write(doc)
else:
    open(outfile, 'w', encoding='utf-8').write(BODY)

print('wrote', outfile, len(open(outfile, encoding="utf-8").read()), 'bytes,', len(comps), 'comps')
