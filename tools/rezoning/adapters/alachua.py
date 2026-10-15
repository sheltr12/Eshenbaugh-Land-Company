#!/usr/bin/env python3
"""Alachua County (unincorporated) rezoning / PD / comp plan amendment adapter.

Source: the public ArcGIS Online feature service behind the county's Development Projects map
(mapgenius.alachuacounty.us/development-projects -> Experience 969eb34cc7cd421a8ce5051c4c2ed7cc):
  PermitBoundariesWithDates/FeatureServer/0  application polygons (CitizenServe permit #, type,
      sub type, status, application date, acres, current/proposed zoning and FLU, parcels,
      applicant/agent, owner, project description, CitizenServe public link)
  PermitBoundariesWithDates/FeatureServer/1  MeetingDates table (Planning Commission / BOCC dates)
The layer is refreshed from CitizenServe daily. CitizenServe itself is only used as the
clickable record link.
"""
import datetime as _dt
import json
import re
import sys
import time

import requests

JURISDICTIONS = ["Alachua County"]

UA = "Mozilla/5.0 (X11; Linux x86_64) ELC-RezoningTracker/1.0 (+https://thedirtdog.com)"
SVC = ("https://services1.arcgis.com/MiBZ4u97DWldovjI/arcgis/rest/services/"
       "PermitBoundariesWithDates/FeatureServer")
SLEEP = 0.3
TODAY = _dt.date.today()

# Sub_Type -> category (None = out of scope)
SUBTYPES = {
    "rezoning- straight zoning district": "Rezoning",
    "rezoning - a or c-1 or a single family lot": "Rezoning",
    "rezoning - new planned development": "PD",
    "major amendment to planned development": "PD",
    "comprehensive plan amendment - small scale": "Comp Plan Amendment",
    "comprehensive plan amendment - large scale": "Comp Plan Amendment",
    "comprehensive plan amendment": "Comp Plan Amendment",
    "combined small scale cpa/zoning": "Comp Plan Amendment",
    "combined large scale cpa/zoning": "Comp Plan Amendment",
}
STATUS = {
    "approved": "Approved", "complete": "Approved", "denied": "Denied",
    "withdrawn": "Withdrawn", "void": "Withdrawn", "expired": "Withdrawn", "rescinded": "Withdrawn",
    "deferred": "Continued", "hearing scheduled": "Hearing Scheduled",
    "under review": "Filed", "online application received": "Filed", "submitted": "Filed",
    "in review": "Filed", "pending": "Filed",
}
BODY = {"planning commission": "Planning Commission",
        "board of county commissioners": "BOCC",
        "development review committee": "DRC", "drc hearing date": "DRC"}

_s = requests.Session()
_s.headers["User-Agent"] = UA


def log(*a):
    print("[alachua]", *a, file=sys.stderr)


def _get(url, params):
    last = None
    for i in range(3):
        try:
            r = _s.get(url, params=params, timeout=60)
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
    d = _dt.datetime.fromtimestamp(ms / 1000, _dt.timezone.utc).date()
    return d.isoformat() if 1990 <= d.year <= 2100 else None


def _clean(s):
    if s is None:
        return None
    s = re.sub(r"\s+", " ", str(s)).strip()
    return s or None


_domains = {}


def _load_domains():
    meta = _get(SVC + "/0", {"f": "json"})
    for f in meta.get("fields", []):
        dom = f.get("domain") or {}
        if dom.get("codedValues"):
            _domains[f["name"]] = {c["code"]: c["name"] for c in dom["codedValues"]}


def _decode(field, code):
    if not code:
        return None
    name = _domains.get(field, {}).get(code)
    if name:
        return name
    return re.sub(r"^0100", "", code)  # county codes are prefixed "0100"


def _parcels(main, extra):
    ids = []
    for s in (main, extra):
        ids += re.findall(r"\b\d{5}-\d{3}-\d{3}\b", s or "")
    return list(dict.fromkeys(ids))


def _query_apps(where):
    out, offset = [], 0
    while True:
        d = _get(SVC + "/0/query", {
            "where": where, "outFields": "*", "returnGeometry": "false",
            "returnCentroid": "true", "outSR": 4326, "orderByFields": "OBJECTID",
            "resultOffset": offset, "resultRecordCount": 1000, "f": "json"})
        feats = d.get("features", [])
        out += feats
        if not d.get("exceededTransferLimit") or not feats:
            return out
        offset += len(feats)


def _meetings(permits):
    res = {}
    for i in range(0, len(permits), 50):
        chunk = permits[i:i + 50]
        inlist = ",".join("'" + p.replace("'", "''") + "'" for p in chunk)
        d = _get(SVC + "/1/query", {"where": f"Permit IN ({inlist})",
                                     "outFields": "Permit,MeetingDate,MeetingType",
                                     "returnGeometry": "false", "f": "json"})
        for f in d.get("features", []):
            a = f["attributes"]
            res.setdefault(a["Permit"], []).append(a)
    return res


def _mdate(s):
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s or "")
    return _dt.date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat() if m else None


def fetch(since, refresh):
    refresh = [r for r in (refresh or []) if r]
    _load_domains()
    subs = " OR ".join(f"LOWER(Sub_Type) = '{s}'" for s in SUBTYPES)
    where = f"({subs}) AND (ApplicationDate >= DATE '{since}' OR FileOpenDate >= DATE '{since}')"
    if refresh:
        inlist = ",".join("'" + r.replace("'", "''") + "'" for r in refresh)
        where = f"({where}) OR Permit IN ({inlist})"
    feats = _query_apps(where)
    # several polygons can share one permit number; keep the first, merge parcels
    by_permit = {}
    for f in feats:
        a = f["attributes"]
        p = a.get("Permit")
        if not p:
            continue
        if p in by_permit:
            by_permit[p]["_extra"].append(f)
        else:
            f["_extra"] = []
            by_permit[p] = f
    meet = _meetings(list(by_permit)) if by_permit else {}
    out = []
    for p, f in by_permit.items():
        try:
            rec = _record(p, f, meet.get(p, []))
            if p not in refresh and (not rec["filed_date"] or rec["filed_date"] < since):
                continue  # bad/legacy dates (e.g. year 5383 in old records)
            out.append(rec)
        except Exception as e:
            log("skip", p, repr(e))
    return out


def _record(permit, f, meetings):
    a = f["attributes"]
    cat = SUBTYPES.get((a.get("Sub_Type") or "").lower())
    if cat is None:
        cat = "Rezoning"  # only reachable for refresh ids
    hearings, seen = [], set()
    for m in meetings:
        body = BODY.get((m.get("MeetingType") or "").lower())
        d = _mdate(m.get("MeetingDate"))
        if not body or not d or (body, d) in seen:
            continue
        seen.add((body, d))
        hearings.append({"body": body, "date": d, "result": None})
    hearings.sort(key=lambda h: h["date"])

    raw = _clean(a.get("Status"))
    status = STATUS.get((raw or "").lower(), "Filed")
    future = [h for h in hearings if h["date"] >= TODAY.isoformat()]
    if status == "Filed" and future:
        status = "Hearing Scheduled"
    decision = None
    if status in ("Approved", "Denied"):
        past_bocc = [h for h in hearings if h["body"] == "BOCC" and h["date"] <= TODAY.isoformat()]
        decision = past_bocc[-1]["date"] if past_bocc else (_iso(a.get("ApprovalDate")) or _iso(a.get("CloseDate")))
        if past_bocc:
            past_bocc[-1]["result"] = status
    elif status == "Continued" and hearings:
        past = [h for h in hearings if h["date"] <= TODAY.isoformat()]
        if past:
            past[-1]["result"] = "Continued"

    parcels = _parcels(a.get("Parcel"), a.get("AdditionalParcels"))
    for x in f.get("_extra", []):
        parcels += [q for q in _parcels(x["attributes"].get("Parcel"), x["attributes"].get("AdditionalParcels"))
                    if q not in parcels]
    lat, lon = a.get("Latitude"), a.get("Longitude")
    c = f.get("centroid")
    if c and c.get("y"):
        lat, lon = c["y"], c["x"]
    desc = _clean(a.get("ProjectDescription")) or _clean(a.get("WorkDescription"))
    acres = a.get("Acres")
    if desc:  # project description acreage is the whole application area; polygon acres can be partial
        m = re.search(r"approximately\s+([\d,.]+)\s*acres", desc, re.I)
        if m:
            try:
                acres = float(m.group(1).replace(",", "").rstrip("."))
            except ValueError:
                pass
    applicant = _clean(a.get("ApplicantCompanyName")) or _clean(a.get("ApplicanName"))
    if a.get("ApplicantCompanyName") and a.get("ApplicanName"):
        applicant = f"{_clean(a['ApplicanName'])} ({_clean(a['ApplicantCompanyName'])})"
    if not applicant and desc:
        m = re.match(r"A request by (.+?)(?:\s*\(|,| for )", desc, re.I)
        applicant = _clean(m.group(1)) if m else None
    filed = _iso(a.get("ApplicationDate")) or _iso(a.get("FileOpenDate"))
    fz, tz = _decode("ZoneCode", a.get("ZoneCode")), _decode("ZoneProposed", a.get("ZoneProposed"))
    ff, tf = _decode("FluCode", a.get("FluCode")), _decode("FluProposed", a.get("FluProposed"))
    if cat != "Comp Plan Amendment" and not tf:
        ff = None  # current FLU only matters for CPAs
    if cat == "PD" and not tz and fz and "PD" in fz.upper():
        tz = fz + " (amendment)"
    return {
        "jurisdiction": "Alachua County", "county": "Alachua", "app_num": permit,
        "category": cat, "app_type": _clean(a.get("Sub_Type")) or cat,
        "project_name": _clean(a.get("WorkDescription")),
        "description": desc[:600] if desc else None,
        "applicant": applicant,
        "parcel_ids": parcels,
        "address": _clean(a.get("FULLADDR")),
        "acres": round(acres, 2) if acres else None,
        "from_zoning": fz, "to_zoning": tz, "from_flu": ff, "to_flu": tf,
        "filed_date": filed, "status": status, "status_raw": raw,
        "hearings": hearings, "decision_date": decision,
        "url": _clean(a.get("CSpublicLink")) or
        f"https://www.citizenserve.com/Portal/PortalController?Action=showPermitDetails&permitNumber={permit}&installationID=318",
        "docs_url": _clean(a.get("KLakeURL")),
        "lat": round(lat, 6) if lat else None, "lon": round(lon, 6) if lon else None,
        "source": "Alachua Development Projects GIS (CitizenServe)",
    }


if __name__ == "__main__":
    since = sys.argv[1] if len(sys.argv) > 1 else (TODAY - _dt.timedelta(days=14)).isoformat()
    refresh = sys.argv[2].split(",") if len(sys.argv) > 2 else []
    t0 = time.time()
    recs = fetch(since, refresh)
    print(json.dumps(recs, indent=1))
    from collections import Counter
    log(f"{len(recs)} records since {since} in {time.time() - t0:.0f}s;",
        dict(Counter(r["category"] for r in recs)), dict(Counter(r["status"] for r in recs)))
