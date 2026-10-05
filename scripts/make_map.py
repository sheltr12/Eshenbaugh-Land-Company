import json, sys, html
county, run_date, window, src, infile, outfile = sys.argv[1:7]
comps=json.load(open(infile))
title=f"{county} County Vacant Land Comps"
sub=f"Run date {run_date} &middot; Sale dates {window} &middot; &gt;0.5 ac &amp; &gt;$500k &middot; {len(comps)} new comps &middot; Source: {src}"
tpl = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>__TITLE__ &mdash; __RUN__</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/esri-leaflet@3.0.12/dist/esri-leaflet.js"></script>
<style>
:root{--ink:#1a2b3c;--accent:#2e6b4f;--line:#dfe4e8;}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:var(--ink);background:#f6f8f9}
header{background:var(--accent);color:#fff;padding:16px 22px}
header h1{margin:0;font-size:19px}
header p{margin:4px 0 0;font-size:13px;opacity:.9}
#map{height:60vh;min-height:420px;width:100%}
.wrap{padding:14px 18px 40px;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:12.5px;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.08)}
th,td{border:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
th{background:#eef3f0;position:sticky;top:0}
tr:nth-child(even){background:#fafbfc}
a{color:var(--accent)}
.num{text-align:right;white-space:nowrap}
.pop b{color:var(--accent)}
.pop{font-size:12.5px;line-height:1.5}
.legend{font-size:12px;color:#5a6b7a;margin:8px 0}
</style></head><body>
<header><h1>__TITLE__</h1><p>__SUB__</p></header>
<div id="map"></div>
<div class="wrap">
<p class="legend">Click any marker for full detail. Coordinates are exact parcel centroids from the __COUNTY__ County PA GIS endpoint, not geocoded addresses.</p>
<table id="tbl"><thead><tr>
<th>#</th><th>Sale Date</th><th>Address</th><th>Grantor (Seller)</th><th>Grantee (Buyer)</th>
<th class="num">Price</th><th class="num">Acres</th><th class="num">$/Acre</th><th>Deed</th><th>Land Use</th><th>Zoning</th><th>Parcel ID</th><th>Book/Page</th><th>PA Page</th><th>Notes</th>
</tr></thead><tbody></tbody></table>
</div>
<script>
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
if(grp.getLayers().length) map.fitBounds(grp.getBounds().pad(0.25)); else map.setView([28.8,-81.2],9);
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
</script></body></html>"""
out=(tpl.replace('__TITLE__',title).replace('__RUN__',run_date).replace('__SUB__',sub)
        .replace('__COUNTY__',county).replace('__DATA__',json.dumps(comps)))
open(outfile,'w').write(out)
print('wrote',outfile,len(out),'bytes,',len(comps),'comps')
