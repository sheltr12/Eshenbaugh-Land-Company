#!/usr/bin/env python3
"""City of Largo (Pinellas) future land use map amendments and land-use annexations
(Tyler EnerGov Civic Access public search API: cityoflargofl-energovweb.tylerhost.net).

Largo has no separate zoning map: its Future Land Use Map sets the allowed uses, so land use map
amendments (case type "Future Land Use Map Amendment", case numbers FLUMA-26-0002) are its
rezonings. Also kept: "Multi-Family Residential & Non-Residential Annexation" cases (annexation
with an initial land use designation). Skipped: single-family annexations, site plans, plats,
development agreements, land use letters, and city-initiated area-wide map updates with no parcel.
EnerGov gives case number, status, apply/complete dates, address, parcel and description. Hearing
dates are not published there, so status follows the EnerGov case status (Submitted / In Review ->
Filed, Approved, Denied, Withdrawn).
"""
import copy
import datetime as dt
import json
import os
import re
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pinellas_common as P  # noqa: E402

JURISDICTIONS = ["City of Largo"]
JUR = "City of Largo"
HOST = "https://cityoflargofl-energovweb.tylerhost.net/apps/selfservice"
API = HOST + "/api"
SOURCE = "Largo EnerGov Civic Access (planning cases)"
P.set_tag("largo")
log = P.log
HDR = {"Content-Type": "application/json;charset=UTF-8", "tenantId": "1", "tenantName": "CityofLargoFLProd",
       "Tyler-TenantUrl": "CityofLargoFLProd", "Tyler-Tenant-Culture": "en-US", "User-Agent": P.UA}
TYPES = {  # plan type name -> (category, app_type)
    "Future Land Use Map Amendment": ("Comp Plan Amendment", "Future Land Use Map Amendment"),
    "Multi-Family Residential & Non-Residential Annexation": ("Rezoning", "Annexation with land use designation"),
}

_s = requests.Session()
_s.headers.update(HDR)


def call(method, path, body=None, tries=4):
    last = None
    for i in range(tries):
        try:
            r = _s.request(method, API + path, json=body, timeout=90)
            r.raise_for_status()
            time.sleep(0.4)
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"{method} {path} failed: {last}")


def type_ids():
    d = call("GET", "/SelfServiceSecondaryData/GlobalSearchSecondaryData")["Result"]
    out = {}
    for c in d.get("CaseTypes") or []:
        if c.get("CategoryObjectName") == "PLAN" and c.get("CaseTypeName") in TYPES:
            out[c["CaseTypeName"]] = c["CaseTypeId"].split("_")[0]
    return out


def search(type_id, since):
    crit = call("GET", "/energov/search/criteria")["Result"]
    out, page = [], 1
    while True:
        c = copy.deepcopy(crit)
        c.update(SearchModule=3, FilterModule=3, PageNumber=page, PageSize=100, SortBy="PlanNumber.keyword",
                 SortAscending=False)
        c["PlanCriteria"].update(PageNumber=page, PageSize=100, SortBy="PlanNumber.keyword", SortAscending=False,
                                 PlanTypeId=type_id, PlanStatusId="none",
                                 ApplyDateFrom=f"{since}T00:00:00.000Z" if since else None)
        r = call("POST", "/energov/search/search", c)["Result"]
        rows = r.get("EntityResults") or []
        out.extend(rows)
        if page >= (r.get("TotalPages") or 1) or not rows:
            return out
        page += 1


def status_of(s):
    t = (s or "").lower()
    if "approv" in t or "complete" in t or "issued" in t and "comment" not in t:
        return "Approved"
    if "denied" in t or "denial" in t:
        return "Denied"
    if any(w in t for w in ("withdr", "void", "expired", "cancel", "closed")):
        return "Withdrawn"
    return "Filed"


def parse_desc(d):
    out = {}
    u = P.sq(d)
    m = re.search(r"\bfrom\s+(.+?)\s+to\s+(.+?)(?:\s+to\s+(?:accommodate|allow|establish)|\s+and\s|[.;,]|$)", u, re.I)
    if m:
        out["from"], out["to"] = m.group(1).strip(" ,"), m.group(2).strip(" ,")
    else:
        m = re.search(r"\b(?:FLUMA?|FLUM)\s+(?:from\s+)?([A-Z/&]{1,8})\s+to\s+([A-Z/&]{1,8})\b", u)
        if m:
            out["from"], out["to"] = m.group(1), m.group(2)
    m = re.search(r"([\d,]*\.?\d+)\s*acres?\b", u, re.I)
    if m:
        try:
            out["acres"] = float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    return out


def fetch(since, refresh):
    refresh = {r.strip().upper() for r in (refresh or []) if r and r.strip()}
    ids = type_ids()
    log(f"plan types: {list(ids)}")
    lookback = (dt.date.fromisoformat(since) - dt.timedelta(days=30)).isoformat()
    if refresh:
        lookback = (dt.date.today() - dt.timedelta(days=720)).isoformat()
    out = []
    for name, tid in ids.items():
        try:
            rows = search(tid, lookback)
        except Exception as e:  # noqa: BLE001
            log(f"{name}: {e}")
            continue
        log(f"{name}: {len(rows)} cases since {lookback}")
        for e in rows:
            try:
                rec = build(e, name)
            except Exception as ex:  # noqa: BLE001
                log(f"{e.get('CaseNumber')}: skipped ({ex})")
                continue
            if not rec:
                continue
            if (rec["filed_date"] or "9999") < since and rec["app_num"].upper() not in refresh:
                continue
            out.append(rec)
    return out


def build(e, type_name):
    num = e.get("CaseNumber")
    pin = P.sq(e.get("MainParcel"))
    desc = P.sq(e.get("Description"))
    addr = P.sq((e.get("Address") or {}).get("FullAddress") or e.get("AddressDisplay"))
    if not pin and not re.match(r"\d", addr or ""):
        return None  # area-wide map update with no site
    if re.search(r"\b(?:CRD|overlay)\b.*\bupdate\b|\bupdate\b.*\b(?:CRD|overlay)\b|adopt the .* overlay", desc, re.I) and not pin:
        return None
    category, app_type = TYPES[type_name]
    info = parse_desc(desc)
    status = status_of(e.get("CaseStatus"))
    apply_d = (e.get("ApplyDate") or "")[:10] or None
    done = (e.get("CompleteDate") or e.get("FinalDate") or "")[:10] or None
    decision = done if status in ("Approved", "Denied") else None
    street = re.sub(r"\s+(?:LARGO|CLEARWATER|SEMINOLE|BELLEAIR\w*)\s+FL\s+\d{5}.*$", "", addr or "", flags=re.I).strip()
    return {
        "jurisdiction": JUR, "county": "Pinellas", "app_num": num, "category": category, "app_type": app_type,
        "project_name": P.sq(e.get("ProjectName")) or (street.title() if street else None),
        "description": desc[:300] or None, "applicant": None,
        "parcel_ids": [pin] if pin else [], "address": addr.title() if addr else None, "acres": info.get("acres"),
        "from_zoning": None, "to_zoning": None,
        "from_flu": info.get("from"), "to_flu": info.get("to"),
        "filed_date": apply_d, "status": status, "status_raw": e.get("CaseStatus"),
        "hearings": [], "decision_date": decision,
        "url": f"{HOST}/#/plan/{e.get('CaseId')}", "docs_url": None,
        "lat": None, "lon": None, "source": SOURCE,
    }


if __name__ == "__main__":
    since = sys.argv[1] if len(sys.argv) > 1 else (dt.date.today() - dt.timedelta(days=14)).isoformat()
    refresh = sys.argv[2].split(",") if len(sys.argv) > 2 else []
    t0 = time.time()
    recs = fetch(since, refresh)
    print(json.dumps(recs, indent=2, ensure_ascii=False))
    from collections import Counter
    log(f"{len(recs)} records in {time.time() - t0:.0f}s; status={dict(Counter(r['status'] for r in recs))}; "
        f"category={dict(Counter(r['category'] for r in recs))}")
