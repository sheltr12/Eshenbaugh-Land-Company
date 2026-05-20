#!/usr/bin/env python3
import csv
import datetime as dt
import json
import math
import urllib.parse
import urllib.request
from pathlib import Path

from dbf_tools import records


ROOT = Path(__file__).resolve().parents[1]
SALES_DBF = ROOT / "source_cache/allsales_05_15_2026/allsales.dbf"
PARCEL_DBF = ROOT / "source_cache/parcel_05_15_2026/parcel.dbf"
PARCEL_DOR_DBF = ROOT / "source_cache/parcel_05_15_2026/parcel_dor_names.dbf"
LATLON_DBF = ROOT / "source_cache/LatLon_Table_05_15_2026/latlon.dbf"
OUT_DIR = ROOT / "outputs/hillsborough_vacant_land_comps_2026-05-19"
TODAY = dt.date(2026, 5, 19)
START = TODAY - dt.timedelta(days=30)
END = TODAY
ROAD_LAYER = "https://gisdextweb1.hillsboroughcounty.org/arcgis/rest/services/Hosted/Road_Centerlines/FeatureServer/0/query"

DEED_TYPES = {
    "AA": "Assignment of Agreement",
    "AD": "Administrative Deed",
    "AS": "Assignment of Contract",
    "CD": "County Deed",
    "CT": "Certificate of Title",
    "DD": "Other Deed",
    "ED": "Executor Deed",
    "FD": "Fee Simple Deed",
    "GD": "Guardian Deed",
    "MD": "Master's Deed",
    "PR": "Personal Representative Deed",
    "QC": "Quit Claim Deed",
    "SD": "Sheriff's Deed",
    "TD": "Tax Deed",
    "TR": "Trustee's Deed",
    "WD": "Warranty Deed",
}


def clean(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = " ".join(value.split())
        return value or None
    return value


def load_latlon():
    coords = {}
    for row in records(LATLON_DBF, ["FOLIO", "lat", "lon"]):
        if row.get("FOLIO") and row.get("lat") and row.get("lon"):
            coords[str(row["FOLIO"]).zfill(10)] = (row["lat"], row["lon"])
    return coords


def load_dor_names():
    names = {}
    for row in records(PARCEL_DOR_DBF, ["DORCODE", "DORDESCR"]):
        if row.get("DORCODE"):
            names[row["DORCODE"]] = clean(row.get("DORDESCR"))
    return names


def candidate_sales():
    candidates = []
    fields = ["PIN", "FOLIO", "DOR_CODE", "S_DATE", "VI", "QU", "REA_CD", "S_AMT", "S_TYPE", "OR_BK", "OR_PG", "GRANTOR", "GRANTEE", "DOC_NUM"]
    for row in records(SALES_DBF, fields):
        sale_date = row.get("S_DATE")
        if not sale_date:
            continue
        sale_date = dt.date.fromisoformat(sale_date)
        amount = row.get("S_AMT") or 0
        if START <= sale_date <= END and amount > 500000 and row.get("VI") == "V":
            row["S_DATE"] = sale_date.isoformat()
            candidates.append(row)
    return candidates


def fetch_parcels(folios):
    needed = set(folios)
    parcels = {}
    fields = [
        "FOLIO", "TYPE", "PIN", "DOR_C", "OWNER", "SITE_ADDR", "SITE_CITY",
        "SITE_ZIP", "LEGAL1", "LEGAL2", "LEGAL3", "LEGAL4", "STRAP",
        "VI", "S_DATE", "S_AMT", "ACREAGE",
    ]
    for row in records(PARCEL_DBF, fields):
        folio = str(row.get("FOLIO") or "").zfill(10)
        if folio in needed:
            parcels[folio] = row
    return parcels


def pa_url(pin):
    return f"https://gis.hcpafl.org/propertysearch/#/parcel/basic/{pin}" if pin else None


def clerk_url(doc_num=None, book=None, page=None):
    if doc_num:
        return f"https://hover.hillsclerk.com/html/case/caseSearch.html?instrument={doc_num}"
    if book and page:
        return f"https://hover.hillsclerk.com/html/case/caseSearch.html?book={book}&page={page}"
    return "https://hover.hillsclerk.com/"


def title_address(attrs):
    site = clean(attrs.get("SITE_ADDR"))
    city = clean(attrs.get("SITE_CITY"))
    if site and site.upper() != "NO PHYSICAL ADDRESS" and site not in {"0", "0 0", "0 1"}:
        return f"{site}, {city}, FL" if city else site
    legal = " ".join(x for x in [attrs.get("LEGAL1"), attrs.get("LEGAL2"), attrs.get("LEGAL3")] if x)
    return f"Near {legal[:60].strip()}" if legal else "Near official parcel centroid"


def point_segment_distance(px, py, ax, ay, bx, by):
    dx = bx - ax
    dy = by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def road_distance(point, geometry):
    lon, lat = point
    # Equirectangular scale is plenty for ranking nearby road segments.
    scale_x = math.cos(math.radians(lat))
    px, py = lon * scale_x, lat
    best = None
    for path in geometry.get("paths") or []:
        for a, b in zip(path, path[1:]):
            ax, ay = a[0] * scale_x, a[1]
            bx, by = b[0] * scale_x, b[1]
            d = point_segment_distance(px, py, ax, ay, bx, by)
            best = d if best is None else min(best, d)
    return best if best is not None else float("inf")


def nearest_road_name(lat, lon):
    if lat is None or lon is None:
        return None
    params = {
        "f": "json",
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "distance": "1200",
        "units": "esriSRUnit_Foot",
        "outFields": "fullname,st_name,roadtype",
        "returnGeometry": "true",
        "outSR": "4326",
    }
    url = ROAD_LAYER + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=25) as response:
            data = json.load(response)
    except Exception:
        return None
    best = None
    for feature in data.get("features", []):
        attrs = feature.get("attributes", {})
        name = clean(attrs.get("fullname")) or " ".join(x for x in [attrs.get("st_name"), attrs.get("roadtype")] if x)
        if not name:
            continue
        distance = road_distance((lon, lat), feature.get("geometry") or {})
        if best is None or distance < best[0]:
            best = (distance, name)
    return best[1] if best else None


def build():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sales = candidate_sales()
    folios = sorted({str(row["FOLIO"]).zfill(10) for row in sales if row.get("FOLIO")})
    parcels = fetch_parcels(folios)
    dor_names = load_dor_names()
    latlon = load_latlon()
    comps = []
    seen = set()
    for row in sales:
        folio = str(row["FOLIO"]).zfill(10)
        attrs = parcels.get(folio)
        if not attrs:
            continue
        acreage = attrs.get("ACREAGE")
        if acreage is None or acreage <= 0.5:
            continue
        dor_code = clean(attrs.get("DOR_C") or row.get("DOR_CODE"))
        dor_desc = dor_names.get(dor_code) or clean(row.get("DOR_CODE"))
        parcel_type = clean(attrs.get("TYPE"))
        # Keep only parcels coded as land/vacant in official appraiser/GIS fields.
        text = " ".join(x for x in [dor_desc, parcel_type, dor_code] if x)
        if "vacant" not in text.lower() and row.get("VI") != "V":
            continue
        key = (folio, row["S_DATE"], row.get("OR_BK"), row.get("OR_PG"), row.get("DOC_NUM"))
        if key in seen:
            continue
        seen.add(key)
        lat = lon = None
        if folio in latlon:
            lat, lon = latlon[folio]
        sale_date = dt.date.fromisoformat(row["S_DATE"])
        price = int(round(row.get("S_AMT") or 0))
        address = title_address(attrs)
        no_real_address = address.startswith("Near ") or address.startswith("0,") or address.startswith("0 ")
        if no_real_address:
            road = nearest_road_name(lat, lon)
            if road:
                address = f"Near {road}"
        comp = {
            "parcel_id": attrs.get("PIN") or row.get("PIN") or folio,
            "folio": folio,
            "address": address,
            "sale_date": sale_date.strftime("%b %d %Y"),
            "sale_date_iso": sale_date.isoformat(),
            "price": price,
            "acreage": float(acreage),
            "price_per_acre": round(price / float(acreage)),
            "transaction_type": "Sale",
            "grantor": clean(row.get("GRANTOR")),
            "grantee": clean(row.get("GRANTEE")),
            "deed_type": DEED_TYPES.get(clean(row.get("S_TYPE")), clean(row.get("S_TYPE"))),
            "property_type": "Land",
            "zoning": None,
            "land_use": dor_desc,
            "source": "Hillsborough County Property Appraiser allsales_05_15_2026 + parcel_05_15_2026 + LatLon_Table_05_15_2026",
            "lat": round(float(lat), 7) if lat is not None else None,
            "lon": round(float(lon), 7) if lon is not None else None,
            "aerial_url": pa_url(attrs.get("PIN") or row.get("PIN")),
            "url": pa_url(attrs.get("PIN") or row.get("PIN")),
            "deed_url": clerk_url(row.get("DOC_NUM"), row.get("OR_BK"), row.get("OR_PG")),
            "book": clean(row.get("OR_BK")),
            "page": clean(row.get("OR_PG")),
            "instrument": clean(row.get("DOC_NUM")),
            "elc_deal": None,
            "comments": None,
        }
        comps.append(comp)
    comps.sort(key=lambda x: (x["sale_date_iso"], x["parcel_id"]))
    return comps


def write_outputs(comps):
    json_path = OUT_DIR / "hillsborough_new_comps.json"
    csv_path = OUT_DIR / "hillsborough_new_comps_summary.csv"
    json_path.write_text(json.dumps(comps, indent=2) + "\n")
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(comps[0].keys()) if comps else [])
        if comps:
            writer.writeheader()
            writer.writerows(comps)
    print(json.dumps({"count": len(comps), "json": str(json_path), "csv": str(csv_path)}, indent=2))


if __name__ == "__main__":
    comps = build()
    write_outputs(comps)
