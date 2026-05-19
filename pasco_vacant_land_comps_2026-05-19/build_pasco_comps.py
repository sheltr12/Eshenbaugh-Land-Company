import csv
import datetime as dt
import html
import json
import re
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUT = ROOT / "elc_pasco_vacant_land_comps_2026-05-19"
START = dt.date(2026, 4, 19)
END = dt.date(2026, 5, 19)


def read_csv_from_zip(zip_name, member):
    with zipfile.ZipFile(DATA / zip_name) as zf:
        with zf.open(member) as raw:
            text = (line.decode("latin1") for line in raw)
            yield from csv.DictReader(text)


def money_to_int(value):
    return int(re.sub(r"[^0-9]", "", value or "0") or 0)


def clean_text(value):
    value = html.unescape(value or "")
    value = value.replace("\xa0", " ")
    value = re.sub(r"<br\s*/?>", " | ", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip(" |")


def span_text(page, span_id):
    match = re.search(
        rf'<span id="{re.escape(span_id)}"[^>]*>(.*?)</span>',
        page,
        flags=re.I | re.S,
    )
    return clean_text(match.group(1)) if match else ""


def fetch(url, dest=None):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 ELC comps data pull"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read()
    if dest:
        dest.write_bytes(body)
    return body.decode("latin1", errors="replace")


def address_from_site(row):
    if not row:
        return ""
    parts = [
        row.get("ADDRESS_NUMBER", ""),
        row.get("STREET_NAME", ""),
        row.get("STREET_SUFFIX", ""),
    ]
    unit = " ".join(
        p for p in [row.get("UNIT_TYPE", ""), row.get("UNIT_IDENTIFIER", "")] if p
    )
    city = row.get("CITY", "")
    zip_code = row.get("ZIP_CODE", "")
    street = " ".join(p for p in parts if p).strip()
    if unit:
        street = f"{street} {unit}".strip()
    tail = ", ".join(p for p in [city, f"FL {zip_code}".strip()] if p)
    return ", ".join(p for p in [street, tail] if p)


def geocode(address):
    if not address or address.lower().startswith("no physical address"):
        return None, None
    params = urllib.parse.urlencode(
        {
            "f": "json",
            "SingleLine": address,
            "maxLocations": "1",
            "outFields": "Match_addr,Addr_type",
        }
    )
    url = (
        "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/"
        f"findAddressCandidates?{params}"
    )
    try:
        data = json.loads(fetch(url))
        candidates = data.get("candidates") or []
        if candidates and candidates[0].get("score", 0) >= 85:
            location = candidates[0]["location"]
            return location["y"], location["x"]
    except Exception:
        return None, None
    return None, None


def parcel_centroid(parcel_id):
    params = urllib.parse.urlencode(
        {
            "f": "json",
            "where": f"ParcelID='{parcel_id}'",
            "outFields": "ParcelID",
            "returnGeometry": "true",
            "outSR": "4326",
        }
    )
    url = (
        "https://mapping.pascopa.com/arcgis/rest/services/Parcels/MapServer/3/"
        f"query?{params}"
    )
    try:
        data = json.loads(fetch(url))
        features = data.get("features") or []
        rings = features[0].get("geometry", {}).get("rings") or []
        points = [point for ring in rings for point in ring]
        if points:
            lon = sum(point[0] for point in points) / len(points)
            lat = sum(point[1] for point in points) / len(points)
            return lat, lon
    except Exception:
        return None, None
    return None, None


def make_aerial(parcel_id):
    return f"https://search.pascopa.com/parcel.aspx?parcel={urllib.parse.quote(parcel_id)}"


def build_html(comps):
    payload = json.dumps(comps, indent=2)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Pasco County Vacant Land Comps | Apr 19-May 19 2026</title>
  <link rel="stylesheet" href="https://js.arcgis.com/4.31/esri/themes/light/main.css">
  <script src="https://js.arcgis.com/4.31/"></script>
  <style>
    html, body, #viewDiv {{ height: 100%; margin: 0; font-family: Arial, sans-serif; }}
    .panel {{
      position: absolute; z-index: 10; left: 16px; top: 16px; width: min(460px, calc(100% - 32px));
      max-height: calc(100% - 32px); overflow: auto; background: #fff; color: #1f2933;
      border: 1px solid #c8d1dc; border-radius: 6px; box-shadow: 0 8px 24px rgba(0,0,0,.18);
    }}
    .panel header {{ padding: 14px 16px; border-bottom: 1px solid #d9e0e8; }}
    h1 {{ font-size: 18px; line-height: 1.2; margin: 0 0 5px; }}
    .meta {{ font-size: 12px; color: #586575; }}
    table {{ border-collapse: collapse; width: 100%; font-size: 12px; }}
    th, td {{ padding: 7px 8px; border-bottom: 1px solid #e5e9ef; text-align: left; vertical-align: top; }}
    th {{ position: sticky; top: 0; background: #f7f9fb; z-index: 1; }}
    tr {{ cursor: pointer; }}
    tr:hover {{ background: #eef6ff; }}
    .pill {{ display: inline-block; padding: 2px 6px; border-radius: 999px; background: #e8f0f8; font-size: 11px; }}
    a {{ color: #155da8; }}
    .popup h2 {{ font-size: 16px; margin: 0 0 8px; }}
    .popup dl {{ display: grid; grid-template-columns: 110px 1fr; gap: 4px 8px; margin: 0; }}
    .popup dt {{ font-weight: 700; }}
    .popup dd {{ margin: 0; }}
  </style>
</head>
<body>
  <div id="viewDiv"></div>
  <section class="panel" aria-label="Comparable sales list">
    <header>
      <h1>Pasco County Vacant Land Comps</h1>
      <div class="meta">Sale dates Apr 19-May 19, 2026. Source: Pasco County PA weekly files dated May 17, 2026, enriched from parcel pages.</div>
    </header>
    <table>
      <thead><tr><th>Sale</th><th>Parties</th><th>Acres</th><th>$/Ac</th></tr></thead>
      <tbody id="rows"></tbody>
    </table>
  </section>
  <script>
    const comps = {payload};
    const money = new Intl.NumberFormat("en-US", {{ style: "currency", currency: "USD", maximumFractionDigits: 0 }});
    const rows = document.querySelector("#rows");
    comps.forEach((c, i) => {{
      const tr = document.createElement("tr");
      tr.innerHTML = `<td><b>${{money.format(c.price)}}</b><br>${{c.sale_date}}<br><span class="pill">${{c.deed_type}}</span></td>
        <td><b>Grantor:</b> ${{c.grantor || "Unknown"}}<br><b>Grantee:</b> ${{c.grantee || "Unknown"}}<br><a href="${{c.aerial}}" target="_blank" rel="noopener">Parcel ${{c.parcel_id}}</a></td>
        <td>${{c.acreage}}</td><td>${{money.format(c.price_per_acre)}}</td>`;
      tr.addEventListener("click", () => window.focusComp(i));
      rows.appendChild(tr);
    }});
    require(["esri/Map", "esri/views/MapView", "esri/Graphic", "esri/layers/GraphicsLayer"], (Map, MapView, Graphic, GraphicsLayer) => {{
      const map = new Map({{ basemap: "hybrid" }});
      const view = new MapView({{ container: "viewDiv", map, center: [-82.43, 28.28], zoom: 10 }});
      const layer = new GraphicsLayer();
      map.add(layer);
      const graphics = comps.filter(c => c.lat && c.lon).map((c, i) => new Graphic({{
        geometry: {{ type: "point", longitude: c.lon, latitude: c.lat }},
        symbol: {{ type: "simple-marker", style: "circle", size: 12, color: [11, 105, 170, .92], outline: {{ color: [255,255,255], width: 1.5 }} }},
        attributes: {{ ...c, index: i }},
        popupTemplate: {{
          title: `${{c.grantor || "Grantor unknown"}} to ${{c.grantee || "Grantee unknown"}}`,
          content: `<div class="popup"><h2>${{money.format(c.price)}} | ${{c.acreage}} ac | ${{money.format(c.price_per_acre)}}/ac</h2>
            <dl><dt>Sale date</dt><dd>${{c.sale_date}}</dd><dt>Deed type</dt><dd>${{c.deed_type}}</dd><dt>Parcel ID</dt><dd>${{c.parcel_id}}</dd>
            <dt>Address</dt><dd>${{c.address || "No site address"}}</dd><dt>Zoning</dt><dd>${{c.zoning || ""}}</dd><dt>Land use</dt><dd>${{c.land_use || ""}}</dd></dl>
            <p><a href="${{c.aerial}}" target="_blank" rel="noopener">Open Pasco PA property page</a></p></div>`
        }}
      }}));
      layer.addMany(graphics);
      if (graphics.length) {{
        view.when(() => view.goTo(graphics, {{ padding: {{ left: 480, top: 40, right: 40, bottom: 40 }} }}));
      }}
      window.focusComp = (i) => {{
        const g = graphics.find(item => item.attributes.index === i);
        if (g) view.goTo({{ target: g.geometry, zoom: 16 }}).then(() => view.openPopup({{ features: [g], location: g.geometry }}));
      }};
    }});
  </script>
</body>
</html>
"""


def main():
    OUT.mkdir(exist_ok=True)
    page_dir = OUT / "parcel_pages"
    page_dir.mkdir(exist_ok=True)

    parcels = {r["Parcel_Num"]: r for r in read_csv_from_zip("parcel.zip", "parcel.csv")}
    owners = {r["Parcel_Num"]: r for r in read_csv_from_zip("owners.zip", "owners.csv")}
    addresses = {r["PARCEL"]: r for r in read_csv_from_zip("site_addresses.zip", "site_addresses.csv")}
    land = {}
    for row in read_csv_from_zip("land.zip", "land.csv"):
        land.setdefault(row["Parcel_Num"], row)

    comps = []
    for row in read_csv_from_zip("sales.zip", "sales_all.csv"):
        sale_date = dt.datetime.strptime(row["Sale_Date"], "%m/%d/%Y").date()
        price = int(row["Sale_Price"] or 0)
        parcel = parcels.get(row["Parcel_Num"])
        if not parcel:
            continue
        acreage = float(parcel.get("Acres") or 0)
        if not (
            START <= sale_date <= END
            and price > 500000
            and acreage > 0.5
            and row.get("Sale_VacImp") == "V"
        ):
            continue

        parcel_id = row["Parcel_Num"]
        page_url = make_aerial(parcel_id)
        page_path = page_dir / f"{parcel_id}.html"
        try:
            page = page_path.read_text(encoding="latin1") if page_path.exists() else fetch(page_url, page_path)
        except Exception:
            page = ""
        if not page_path.exists() and page:
            time.sleep(0.15)

        owner = owners.get(parcel_id, {})
        grantee = clean_text(owner.get("Owner_Mail_Name1")) or span_text(page, "lblMailingAddress").split(" | ")[0]
        grantor = span_text(page, "lblPreviousOwnerName")
        address = span_text(page, "lblPhysicalAddress") or address_from_site(addresses.get(parcel_id))
        land_row = land.get(parcel_id, {})
        zoning = land_row.get("Land_Zoning") or None
        land_use = land_row.get("Land_Use_Code") or parcel.get("Prop_Use_Code") or None
        lat, lon = parcel_centroid(parcel_id)
        if lat is None:
            lat, lon = geocode(address)
        time.sleep(0.1)

        comps.append(
            {
                "parcel_id": parcel_id,
                "address": address,
                "sale_date": sale_date.strftime("%b %d %Y"),
                "sale_date_iso": sale_date.isoformat(),
                "price": price,
                "acreage": acreage,
                "price_per_acre": round(price / acreage),
                "transaction_type": "Sale",
                "grantor": grantor or None,
                "grantee": grantee or None,
                "deed_type": row["Sale_Deed_Type"],
                "property_type": "Land",
                "zoning": zoning,
                "land_use": land_use,
                "source": "Pasco County PA",
                "lat": lat,
                "lon": lon,
                "aerial": page_url,
                "elc_deal": None,
                "comments": None,
            }
        )

    comps.sort(key=lambda c: (c["sale_date_iso"], -c["price"]))
    (OUT / "pasco_vacant_land_comps_2026-04-19_to_2026-05-19.json").write_text(
        json.dumps(comps, indent=2) + "\n",
        encoding="utf-8",
    )
    (OUT / "pasco_vacant_land_comps_2026-04-19_to_2026-05-19.html").write_text(
        build_html(comps),
        encoding="utf-8",
    )
    csv_fields = [
        "parcel_id",
        "address",
        "sale_date",
        "price",
        "acreage",
        "price_per_acre",
        "grantor",
        "grantee",
        "deed_type",
        "zoning",
        "land_use",
        "lat",
        "lon",
        "aerial",
    ]
    with (OUT / "pasco_vacant_land_comps_2026-04-19_to_2026-05-19.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=csv_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(comps)
    (OUT / "README.md").write_text(
        "# Pasco County Vacant Land Comps\n\n"
        "Sale window: Apr 19 2026 through May 19 2026.\n\n"
        "Filters applied from Pasco County PA weekly data dated May 17 2026: "
        "Sale_VacImp = V, Sale_Price > 500000, parcel Acres > 0.5. "
        "Grantor/current grantee context was enriched from Pasco PA parcel detail pages. "
        "Map coordinates are centroids from the public Pasco PA ArcGIS parcel layer.\n\n"
        "Files:\n"
        "- `pasco_vacant_land_comps_2026-04-19_to_2026-05-19.json` - JSON array using the ELC fields requested.\n"
        "- `pasco_vacant_land_comps_2026-04-19_to_2026-05-19.csv` - readable summary table.\n"
        "- `pasco_vacant_land_comps_2026-04-19_to_2026-05-19.html` - standalone Esri interactive map.\n",
        encoding="utf-8",
    )
    print(json.dumps({"count": len(comps), "out": str(OUT)}, indent=2))


if __name__ == "__main__":
    main()
