#!/usr/bin/env python3
"""Hillsborough County (unincorporated) rezoning adapter.

Filings, hearing dates and geometry: DSD Zoning Hearings ArcGIS layer
  .../DSD_Zoning_Hearings/Zoning_Hearings/FeatureServer/0 (points), /1 (polygons)
Descriptions, outcomes and hearing results: Legistar web API (hillsboroughcounty).
"""
import datetime as dt
import json
import math
import re
import sys
import time

import requests

JURISDICTIONS = ["Hillsborough County"]

GIS = ("https://maps.hillsboroughcounty.org/arcgis/rest/services/"
       "DSD_Zoning_Hearings/Zoning_Hearings/FeatureServer")
LEG = "https://webapi.legistar.com/v1/hillsboroughcounty"
LEG_SITE = "https://hillsboroughcounty.legistar.com"
SOURCE = "Hillsborough Zoning Hearings GIS + Legistar"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"

# GIS Type_ -> category
TYPE_MAP = {
    "RZ-PD": "PD", "MM": "PD", "PD": "PD",
    "RZ-STD": "Rezoning", "RZ_STD": "Rezoning", "RZ": "Rezoning",
    "HC/CPA": "Comp Plan Amendment", "CPA": "Comp Plan Amendment",
}
BODY_MAP = {
    "Zoning Hearing Master": "Zoning Hearing Master",
    "BOCC Land Use": "BOCC",
    "Land Use Hearing Officer": "LUHO",
}

_session = requests.Session()
_session.headers.update({"User-Agent": UA, "Accept": "application/json"})


def log(*a):
    print("[hillsborough]", *a, file=sys.stderr)


def get_json(url, params=None, tries=3, sleep=0.3):
    last = None
    for i in range(tries):
        try:
            r = _session.get(url, params=params, timeout=60)
            r.raise_for_status()
            time.sleep(sleep)
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


def ms_to_date(v):
    if v in (None, ""):
        return None
    return dt.datetime.fromtimestamp(v / 1000, dt.timezone.utc).strftime("%Y-%m-%d")


def norm_folio(raw):
    """'36274.0302' / '036274.0302' -> '036274-0302' (HCPA display format)."""
    m = re.match(r"^\s*(\d{1,6})\.(\d{4})\s*$", raw)
    if not m:
        return None
    return f"{int(m.group(1)):06d}-{m.group(2)}"


def parse_folios(*texts):
    out = []
    for t in texts:
        if not t:
            continue
        for tok in re.findall(r"\d{1,6}\.\d{4}", t):
            f = norm_folio(tok)
            if f and f not in out:
                out.append(f)
    return out


def esc(s):
    return s.replace("'", "''")


# ---------------------------------------------------------------- GIS
def gis_query(layer, where, geometry=True):
    feats, offset = [], 0
    while True:
        params = {
            "where": where, "outFields": "*", "f": "json", "outSR": 4326,
            "returnGeometry": "true" if geometry else "false",
            "orderByFields": "OBJECTID", "resultOffset": offset,
            "resultRecordCount": 1000,
        }
        d = get_json(f"{GIS}/{layer}/query", params)
        if "error" in d:
            raise RuntimeError(f"GIS error: {d['error']}")
        fs = d.get("features", [])
        feats.extend(fs)
        if not d.get("exceededTransferLimit") or not fs:
            break
        offset += len(fs)
    return feats


def ring_area_acres(rings):
    """Approximate area (acres) of lon/lat rings via local equirectangular projection."""
    total = 0.0
    for ring in rings:
        if len(ring) < 3:
            continue
        lat0 = math.radians(sum(p[1] for p in ring) / len(ring))
        R = 6378137.0
        pts = [(math.radians(x) * R * math.cos(lat0), math.radians(y) * R) for x, y in ring]
        a = 0.0
        for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
            a += x1 * y2 - x2 * y1
        total += a / 2.0  # outer rings CW (negative in ESRI), holes CCW
    return abs(total) / 4046.8564224


def ring_centroid(rings):
    pts = [p for r in rings for p in r]
    if not pts:
        return None, None
    return (sum(p[1] for p in pts) / len(pts), sum(p[0] for p in pts) / len(pts))


# ---------------------------------------------------------------- Legistar
TITLE_LABELS = [
    ("appno", r"Application Number"), ("applicant", r"Applicant"), ("location", r"Location"),
    ("folio", r"Folio Numbers?"), ("acreage", r"Acreage\s*(?:\(\+/-\))?"),
    ("district", r"Commissioner District"), ("comp", r"Comprehensive Plan"),
    ("service", r"Service Area"), ("community", r"Community Plan"),
    ("existing", r"Existing Zoning"), ("request", r"Request"), ("recommendation", r"RECOMMENDATION"),
    ("zhm", r"Zoning Hearing Master"), ("ds", r"Development Services"), ("pc", r"Planning Commission"),
]
_LABEL_RE = re.compile(r"(?:^|\n)[ \t]*(" + "|".join(p for _, p in TITLE_LABELS) + r")[ \t]*:", re.I)


def parse_title(title):
    """Parse 'Label: value' blocks (value on same or following lines) from a MatterTitle."""
    out = {}
    if not title:
        return out
    t = title.replace("\r", "")
    ms = list(_LABEL_RE.finditer(t))
    for i, m in enumerate(ms):
        lab = m.group(1)
        key = next(k for k, p in TITLE_LABELS if re.fullmatch(p, lab, re.I))
        val = t[m.end(): ms[i + 1].start() if i + 1 < len(ms) else len(t)]
        val = re.sub(r"\s+", " ", val).strip(" .")
        if val and key not in out:
            out[key] = val
    return out


def find_matters(appnum):
    d = get_json(f"{LEG}/matters", {"$filter": f"substringof('{esc(appnum)}',MatterFile)"})
    return [m for m in d if re.search(rf"(^|\D){re.escape(appnum)}(\D|$)", m.get("MatterFile") or "")]


def matter_histories(mid):
    return get_json(f"{LEG}/matters/{mid}/histories")


def matter_attachments(mid):
    try:
        return get_json(f"{LEG}/matters/{mid}/attachments")
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------- build
def classify_action(name, text):
    s = f"{name or ''} {text or ''}".lower()
    if "withdr" in s:
        return "Withdrawn"
    if "remand" in s:
        return "Remanded"
    if "continu" in s or "defer" in s:
        return "Continued"
    if "denied" in s or "denial" in s or "failed" in s:
        return "Denied"
    if "approv" in s or "adopt" in s:
        return "Approved"
    return None


def build_record(appnum, pts, polys, today):
    a = (pts or polys)[0]["attributes"]
    # prefer an active GIS row when duplicates exist
    for f in (pts or []) + (polys or []):
        if f["attributes"].get("Inactive") != "1":
            a = f["attributes"]
            break
    typ = (a.get("Type_") or "").strip()
    category = TYPE_MAP.get(typ)
    folio_raw = a.get("FolioNumb")

    lat = lon = None
    if pts and pts[0].get("geometry"):
        g = pts[0]["geometry"]
        lat, lon = round(g["y"], 6), round(g["x"], 6)
    gis_acres = None
    if polys and polys[0].get("geometry", {}).get("rings"):
        rings = polys[0]["geometry"]["rings"]
        gis_acres = round(ring_area_acres(rings), 2)
        if lat is None:
            clat, clon = ring_centroid(rings)
            lat, lon = round(clat, 6), round(clon, 6)

    created = [ms_to_date(f["attributes"].get("created_date")) for f in (pts or []) + (polys or [])]
    created = [c for c in created if c]
    filed = min(created) if created else ms_to_date(a.get("Cut_Date"))

    rec = {
        "jurisdiction": "Hillsborough County", "county": "Hillsborough",
        "app_num": appnum, "category": category, "app_type": typ or None,
        "project_name": None, "description": None, "applicant": None,
        "parcel_ids": parse_folios(folio_raw), "parcel_ids_raw": folio_raw or None,
        "address": None, "acres": gis_acres,
        "from_zoning": None, "to_zoning": None, "from_flu": None, "to_flu": None,
        "filed_date": filed, "status": "Filed", "status_raw": None,
        "hearings": [], "decision_date": None,
        "url": f"https://hillsborough.maps.arcgis.com/apps/webappviewer/index.html?id=be7f0f9c93c0460984d1223f6b329fa8&find={appnum}",
        "docs_url": None, "lat": lat, "lon": lon, "source": SOURCE,
    }

    # ---- Legistar
    try:
        matters = find_matters(appnum)
    except Exception as e:  # noqa: BLE001
        log(f"{appnum}: legistar lookup failed: {e}")
        matters = []
    info, hist = {}, []
    matter_status = None
    main = None
    for m in sorted(matters, key=lambda m: (m.get("MatterAgendaDate") or "", m["MatterId"])):
        t = parse_title(m.get("MatterTitle"))
        if len(t) >= len(info):
            info = {**info, **t}
            main = m
        elif t:
            for k, v in t.items():
                info.setdefault(k, v)
        try:
            hist.extend(matter_histories(m["MatterId"]))
        except Exception as e:  # noqa: BLE001
            log(f"{appnum}: histories failed: {e}")
        matter_status = m.get("MatterStatusName") or matter_status
    if matters and main is None:
        main = matters[-1]

    if main:
        rec["url"] = (f"{LEG_SITE}/LegislationDetail.aspx?ID={main['MatterId']}"
                      f"&GUID={main['MatterGuid']}")
        atts = matter_attachments(main["MatterId"])
        pdfs = [x.get("MatterAttachmentHyperlink") for x in atts if x.get("MatterAttachmentHyperlink")]
        if pdfs:
            rec["docs_url"] = pdfs[0]
        first = re.match(r"^\s*([A-Z\-/]+\s*\d{2}-\d{4})\s+(.+?)\s*$",
                         (main.get("MatterTitle") or "").split("\n")[0])
        if first and not info.get("applicant"):
            info["applicant"] = first.group(2).strip()

    if info.get("applicant"):
        rec["applicant"] = info["applicant"]
    if info.get("location"):
        rec["address"] = info["location"].rstrip(".")
    if info.get("acreage"):
        m = re.search(r"([\d,]*\.?\d+)", info["acreage"])
        if m:
            try:
                rec["acres"] = float(m.group(1).replace(",", ""))
            except ValueError:
                pass
    if info.get("folio"):
        for f in parse_folios(info["folio"]):
            if f not in rec["parcel_ids"]:
                rec["parcel_ids"].append(f)
    if info.get("existing"):
        rec["from_zoning"] = info["existing"]
    if info.get("comp"):
        rec["from_flu"] = info["comp"]
    req = info.get("request")
    if req:
        m = re.search(r"\bto\s+(?:a\s+)?([A-Z][A-Za-z0-9\-\(\)\. ]*?)\s*$", req)
        if m and "Major Modification" not in req:
            rec["to_zoning"] = m.group(1).strip()
        elif "Major Modification" in req:
            rec["to_zoning"] = "PD (major modification)"
    elif category == "PD" and typ == "RZ-PD":
        rec["to_zoning"] = "PD"
    head = (req.rstrip(".") if req else
            {"RZ-PD": "Rezone to PD", "MM": "Major Modification to PD",
             "RZ-STD": "Standard rezoning", "RZ": "Rezoning"}.get(typ, typ or "Application"))
    if rec["from_zoning"] and "Major Modification" not in head:
        head += f" from {rec['from_zoning']}"
    elif rec["from_zoning"]:
        head += f" ({rec['from_zoning']})"
    parts = [head]
    if rec["acres"]:
        parts.append(f"{rec['acres']:g} ac")
    if info.get("comp"):
        parts.append(f"FLU {info['comp']}")
    rec["description"] = ", ".join(parts)

    # ---- hearings (past from Legistar histories, future from GIS)
    hearings = {}
    zhm_rec = (info.get("zhm") or "")
    for h in sorted(hist, key=lambda h: h.get("MatterHistoryActionDate") or ""):
        d = (h.get("MatterHistoryActionDate") or "")[:10]
        body = BODY_MAP.get(h.get("MatterHistoryActionBodyName"), h.get("MatterHistoryActionBodyName"))
        if not d or not body:
            continue
        res = h.get("MatterHistoryActionName")
        if body == "Zoning Hearing Master" and res and "Heard" in res:
            if re.search(r"denial", zhm_rec, re.I):
                res = "Heard - Recommended Denial"
            elif re.search(r"approv", zhm_rec, re.I):
                res = "Heard - Recommended Approval"
        hearings[(body, d)] = {"body": body, "date": d, "result": res,
                               "_cls": classify_action(h.get("MatterHistoryActionName"),
                                                       h.get("MatterHistoryActionText")),
                               "_text": h.get("MatterHistoryActionText")}
    for fld, body in (("ZHM_Date", "Zoning Hearing Master"), ("LUHO_Date", "LUHO"),
                      ("BOCC_Date", "BOCC"), ("LU_Date", "BOCC")):
        d = ms_to_date(a.get(fld))
        if d and (body, d) not in hearings and d >= today:
            hearings[(body, d)] = {"body": body, "date": d, "result": None, "_cls": None, "_text": None}
    hl = sorted(hearings.values(), key=lambda h: h["date"])

    # ---- status
    status, status_raw, decision = None, None, None
    acted = [h for h in hl if h["_cls"]]
    bocc_final = [h for h in acted if h["body"] == "BOCC" and h["_cls"] in ("Approved", "Denied")]
    wd = [h for h in acted if h["_cls"] == "Withdrawn"]
    if wd or (matter_status or "").lower() == "withdrawn":
        status = "Withdrawn"
        status_raw = (wd[-1]["result"] if wd else matter_status)
    elif bocc_final:
        status = bocc_final[-1]["_cls"]
        status_raw = f"BOCC {bocc_final[-1]['result']} (Legistar: {matter_status})"
        decision = bocc_final[-1]["date"]
    elif (matter_status or "") in ("Passed", "Failed") and main and main.get("MatterBodyName") == "BOCC Land Use" \
            and main.get("MatterPassedDate"):
        status = "Approved" if matter_status == "Passed" else "Denied"
        status_raw = f"Legistar: {matter_status}"
        decision = main["MatterPassedDate"][:10]
    else:
        future = [h for h in hl if h["date"] >= today]
        past_acted = [h for h in acted if h["date"] < today or h["date"] == today]
        last = past_acted[-1] if past_acted else None
        if a.get("Inactive") == "1":
            # GIS marks superseded / pulled cases inactive; often re-filed under a new number
            status, status_raw = "Withdrawn", "Inactive in GIS hearing layer (no Legistar outcome)"
        elif last and last["_cls"] == "Continued" and (last["_text"] or "").find("Heard") < 0:
            status = "Continued"
            status_raw = f"{last['body']} {last['result']}: {last['_text'] or ''}".strip()
        elif future:
            status = "Hearing Scheduled"
            status_raw = (f"Legistar: {matter_status}; " if matter_status else "") + \
                f"next hearing {future[0]['body']} {future[0]['date']}"
        else:
            status = "Filed"
            status_raw = f"Legistar: {matter_status}" if matter_status else "In GIS hearing layer"
    rec["status"], rec["status_raw"], rec["decision_date"] = status, status_raw, decision
    if status in ("Withdrawn", "Approved", "Denied"):
        # GIS still carries the originally scheduled (now moot) dates
        hl = [h for h in hl if h["_cls"] or h["date"] < today]
    rec["hearings"] = [{"body": h["body"], "date": h["date"], "result": h["result"]} for h in hl]
    return rec


def fetch(since, refresh):
    today = dt.date.today().isoformat()
    types = ",".join(f"'{t}'" for t in TYPE_MAP)
    where = f"Type_ IN ({types}) AND created_date >= date '{since}'"
    pts = gis_query(0, where)
    polys = gis_query(1, where)
    refresh = [r for r in (refresh or []) if r]
    if refresh:
        for i in range(0, len(refresh), 50):
            chunk = ",".join(f"'{esc(r)}'" for r in refresh[i:i + 50])
            w = f"AppNum IN ({chunk})"
            pts += gis_query(0, w)
            polys += gis_query(1, w)
    by_app = {}
    for f in pts:
        an = (f["attributes"].get("AppNum") or "").strip()
        if an:
            by_app.setdefault(an, {"pts": [], "polys": []})["pts"].append(f)
    for f in polys:
        an = (f["attributes"].get("AppNum") or "").strip()
        if an:
            by_app.setdefault(an, {"pts": [], "polys": []})["polys"].append(f)
    log(f"GIS: {len(by_app)} applications ({len(pts)} points, {len(polys)} polygons)")

    out = []
    for an in sorted(by_app):
        g = by_app[an]
        # de-duplicate identical GIS rows
        try:
            rec = build_record(an, g["pts"], g["polys"], today)
            if rec["category"] is None:
                continue
            out.append(rec)
        except Exception as e:  # noqa: BLE001
            log(f"{an}: skipped ({e})")
    for r in refresh:
        if r not in by_app:
            log(f"refresh id {r} not found in GIS layer")
    return out


if __name__ == "__main__":
    since = sys.argv[1] if len(sys.argv) > 1 else (dt.date.today() - dt.timedelta(days=14)).isoformat()
    refresh = sys.argv[2].split(",") if len(sys.argv) > 2 else []
    t0 = time.time()
    recs = fetch(since, refresh)
    print(json.dumps(recs, indent=2))
    from collections import Counter
    log(f"{len(recs)} records in {time.time() - t0:.0f}s; status={dict(Counter(r['status'] for r in recs))}; "
        f"category={dict(Counter(r['category'] for r in recs))}")
