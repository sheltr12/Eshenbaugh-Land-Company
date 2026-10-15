#!/usr/bin/env python3
"""Plan Hillsborough (Hillsborough County City-County Planning Commission) future land use map
amendments for unincorporated Hillsborough County, City of Tampa, Plant City and Temple Terrace.

Source: the public ArcGIS MapServer layers behind Plan Hillsborough's "Plan Amendments
Interactive Map" pages (planhillsborough.org/*-plan-amendments-interactive-map/):
  https://gis.tpcmaps.org/arcgis/rest/services/pigi/pigiHC/MapServer/0  (HC/CPA-yy-nn)
  https://gis.tpcmaps.org/arcgis/rest/services/pigi/pigiTA/MapServer/0  (TA/CPA-yy-nn)
  https://gis.tpcmaps.org/arcgis/rest/services/pigi/pigiPC/MapServer/0  (PC/CPA-yy-nn)
  https://gis.tpcmaps.org/arcgis/rest/services/pigi/pigiTT/MapServer/0  (TT/CPA-yy-nn)
Each feature joins the amendment polygon with Plan Hillsborough's tracking table
(applicant/agent, location, acres, FLU from/to, folios, Planning Commission hearing and action,
elected-official hearings 1-3 and actions, adoption date, ordinance, documentation link).
Text-only amendments are skipped.
"""
import datetime as _dt
import json
import re
import sys
import time

import requests

JURISDICTIONS = ["Hillsborough County", "City of Tampa", "City of Plant City", "City of Temple Terrace"]

UA = "Mozilla/5.0 (X11; Linux x86_64) ELC-RezoningTracker/1.0 (+https://thedirtdog.com)"
BASE = "https://gis.tpcmaps.org/arcgis/rest/services/pigi/{svc}/MapServer/0/query"
SERVICES = {
    # svc: (jurisdiction, elected body, public map page)
    "pigiHC": ("Hillsborough County", "BOCC",
               "https://planhillsborough.org/hillsborough-county-plan-amendments-interactive-map/"),
    "pigiTA": ("City of Tampa", "City Council",
               "https://planhillsborough.org/city-of-tampa-plan-amendments-interactive-map/"),
    "pigiPC": ("City of Plant City", "City Commission",
               "https://planhillsborough.org/city-of-plant-city-plan-amendments-interactive-map/"),
    "pigiTT": ("City of Temple Terrace", "City Council",
               "https://planhillsborough.org/city-of-temple-terrace-plan-amendments-interactive-map/"),
}
P = "edt.landuse.PlanAmendData."
SLEEP = 0.4
TODAY = _dt.date.today().isoformat()

_s = requests.Session()
_s.headers["User-Agent"] = UA


def log(*a):
    print("[plan_hillsborough]", *a, file=sys.stderr)


def _get(url, params):
    last = None
    for i in range(3):
        try:
            r = _s.get(url, params=params, timeout=90)
            r.raise_for_status()
            d = r.json()
            if "error" in d:
                raise RuntimeError(d["error"])
            time.sleep(SLEEP)
            return d
        except Exception as e:  # noqa
            last = e
            time.sleep(2 * (i + 1))
    raise last


def _iso(ms):
    if ms is None:
        return None
    try:
        return _dt.datetime.fromtimestamp(ms / 1000, _dt.timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _clean(s):
    if s is None:
        return None
    s = re.sub(r"\s+", " ", str(s)).strip()
    return s or None


def _flu(s):
    """"'HC:RESIDENTIAL-1 (.25 FAR)'|'HC:RESIDENTIAL-4 (.25 FAR)'" -> "RESIDENTIAL-1 (.25 FAR); ..."""
    if not s:
        return None
    parts = [re.sub(r"^[A-Z]{2}:", "", p.strip().strip("'\"").strip()) for p in re.split(r"\|", s)]
    parts = [p for p in parts if p]
    return "; ".join(dict.fromkeys(parts)) or None


def _folios(s):
    """'62486.0200\\n62485.0004 & 61679.0000' -> ['062486-0200', ...] (Hillsborough PA folio format)"""
    out = []
    for a, b in re.findall(r"\b(\d{3,6})[.\-](\d{4})\b", s or ""):
        out.append(f"{int(a):06d}-{b}")
    return list(dict.fromkeys(out))


def _centroid(geom):
    rings = (geom or {}).get("rings") or []
    best, best_a = None, 0
    for ring in rings:
        a = cx = cy = 0.0
        for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
            c = x1 * y2 - x2 * y1
            a += c
            cx += (x1 + x2) * c
            cy += (y1 + y2) * c
        if a and abs(a) > best_a:
            best_a, best = abs(a), (cx / (3 * a), cy / (3 * a))
    return best


def _status(a, hearings):
    cur = (_clean(a.get("CurrentAction")) or "").lower()
    if cur in ("adopted", "closed", "approved"):
        return "Approved"
    if cur == "denied":
        return "Denied"
    if cur == "withdrawn":
        return "Withdrawn"
    acts = [(_clean(a.get(f"OffAction{i}")) or "").lower() for i in (1, 2, 3)]
    if "withdrawn" in acts:
        return "Withdrawn"
    if "denied" in acts:
        return "Denied"
    if any(h["date"] >= TODAY for h in hearings):
        return "Hearing Scheduled"
    if cur == "continued" or any(x in ("continued", "not heard", "deferred") for x in acts):
        return "Continued"
    if cur not in ("pending", "continued") and a.get("Adoption") and \
            [x for x in acts if x][-1:] == ["adopted"]:
        return "Approved"
    return "Filed"


def _record(svc, f):
    juris, elected, page = SERVICES[svc]
    a = {k.replace(P, "").split(".")[-1]: v for k, v in f["attributes"].items()}
    num = _clean(a.get("PlanAmend"))
    hearings = []
    if a.get("PlanBoard"):
        hearings.append({"body": "Planning Board", "date": _iso(a["PlanBoard"]), "result": None})
    if a.get("TPCPubHear"):
        hearings.append({"body": "Planning Commission", "date": _iso(a["TPCPubHear"]),
                         "result": _clean(a.get("TPCAction"))})
    for i in (1, 2, 3):
        if a.get(f"OffHear{i}"):
            hearings.append({"body": elected, "date": _iso(a[f"OffHear{i}"]),
                             "result": _clean(a.get(f"OffAction{i}"))})
    hearings = [h for h in hearings if h["date"]]
    hearings.sort(key=lambda h: h["date"])
    status = _status(a, hearings)
    decision = None
    if status in ("Approved", "Denied"):
        decision = _iso(a.get("Adoption")) if status == "Approved" else None
        hit = [h for h in hearings if h["body"] == elected and (h["result"] or "").lower() in
               (("adopted", "approved") if status == "Approved" else ("denied",))]
        if hit and (not decision or status == "Denied" or juris != "Hillsborough County"):
            decision = hit[-1]["date"]
        decision = decision or _iso(a.get("OrdinDate"))
        if not decision:
            past = [h for h in hearings if h["body"] == elected and h["date"] <= TODAY]
            decision = past[-1]["date"] if past else None
    acres = a.get("TotAcres") or a.get("Acreage")
    applicant = _clean(a.get("Applicant")) or _clean(a.get("AgentRep"))
    if applicant:
        applicant = re.split(r"\s+\d{2,6}\s+\w", applicant)[0]  # drop street address
        applicant = re.sub(r"\S+@\S+|\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}", "", applicant)  # emails/phones
        applicant = _clean(applicant.strip(" -&,;/"))[:120] if _clean(applicant.strip(" -&,;/")) else None
    loc = _clean(a.get("Location"))
    ff, tf = _flu(a.get("ChangeFrom")), _flu(a.get("ChangeTo"))
    desc = f"FLU map amendment {ff or '?'} -> {tf or '?'}"
    if acres:
        desc += f" on {acres:g} ac"
    if loc:
        desc += f" ({loc})"
    if _clean(a.get("AmendDesc")):
        desc = _clean(a["AmendDesc"]) + ": " + desc
    lat = lon = None
    c = _centroid(f.get("geometry"))
    if c:
        lon, lat = round(c[0], 6), round(c[1], 6)
    raw = _clean(a.get("CurrentAction"))
    last_act = [h for h in hearings if h["result"]]
    status_raw = raw or (f"{last_act[-1]['body']}: {last_act[-1]['result']}" if last_act else None)
    return {
        "jurisdiction": juris, "county": "Hillsborough", "app_num": num,
        "category": "Comp Plan Amendment",
        "app_type": " ".join(filter(None, [_clean(a.get("AppType")), _clean(a.get("AmendType")) or "Map",
                                           "Plan Amendment"])),
        "project_name": _clean(a.get("AmendDesc")),
        "description": desc[:500],
        "applicant": applicant,
        "parcel_ids": _folios(a.get("Folio")),
        "address": loc,
        "acres": round(float(acres), 2) if acres else None,
        "from_zoning": None, "to_zoning": None, "from_flu": ff, "to_flu": tf,
        "filed_date": _iso(a.get("ApplicationDate")) or _iso(a.get("CreateDate")),
        "status": status, "status_raw": status_raw,
        "hearings": hearings, "decision_date": decision,
        "url": page, "docs_url": _clean(a.get("DocumentationLink")),
        "lat": lat, "lon": lon,
        "source": "Plan Hillsborough plan amendments GIS (gis.tpcmaps.org pigi)",
    }


def fetch(since, refresh):
    refresh = [r.strip().upper() for r in (refresh or []) if r and r.strip()]
    yr = int(since[:4]) - 1
    out, ok = [], 0
    for svc in SERVICES:
        # date predicates fail on this joined layer; filter by year server-side, dates client-side
        where = f"{P}CycleYear >= {yr} OR {P}AmendYear >= {yr}"
        mine = [r for r in refresh if r.startswith(svc[-2:] + "/")]
        if mine:
            where += " OR " + f"{P}PlanAmend IN ({','.join(repr(r) for r in mine)})"
        try:
            d = _get(BASE.format(svc=svc), {"where": where, "outFields": "*",
                                            "returnGeometry": "true", "outSR": 4326,
                                            "geometryPrecision": 6, "f": "json"})
            ok += 1
        except Exception as e:
            log(svc, "failed:", e)
            continue
        seen = set()
        for f in d.get("features", []):
            try:
                a = {k.replace(P, "").split(".")[-1]: v for k, v in f["attributes"].items()}
                num = (_clean(a.get("PlanAmend")) or "").upper()
                if not num or num in seen:
                    continue
                if (a.get("AmendType") or "").strip().lower() == "text" and num not in refresh:
                    continue
                filed = _iso(a.get("ApplicationDate"))
                created = _iso(a.get("CreateDate"))
                if num not in refresh and not ((filed and filed >= since) or (created and created >= since)):
                    continue
                seen.add(num)
                out.append(_record(svc, f))
            except Exception as e:
                log("skip", f.get("attributes", {}).get(P + "PlanAmend"), repr(e))
    if ok == 0:
        raise RuntimeError("all Plan Hillsborough services unreachable")
    return out


if __name__ == "__main__":
    since = sys.argv[1] if len(sys.argv) > 1 else (_dt.date.today() - _dt.timedelta(days=14)).isoformat()
    refresh = sys.argv[2].split(",") if len(sys.argv) > 2 else []
    t0 = time.time()
    recs = fetch(since, refresh)
    print(json.dumps(recs, indent=1))
    from collections import Counter
    log(f"{len(recs)} records since {since} in {time.time() - t0:.0f}s;",
        dict(Counter(r["jurisdiction"] for r in recs)), dict(Counter(r["status"] for r in recs)))
