#!/usr/bin/env python3
"""Hernando County rezoning / PD (master plan) / comp plan amendment adapter.

Sources (all public, no login):
  1. Tyler EnerGov Citizen Self Service (anonymous search API) for applications filed in the
     county's new permitting system (case numbers REZ-000026-2026, CPAM-000008-2026; system went
     live for planning cases around March 2026). Gives apply date, status, address, parcel keys,
     applicant contact.
     https://hernandocountyfl-energovweb.tylerhost.net/apps/selfservice
  2. Hernando BOCC Legistar Web API (hernandocountyfl) for P&Z Commission and BOCC hearings,
     staff summaries (request, parcel keys), staff report links and outcomes (minutes notes).
     Legacy cases filed before EnerGov (H2616, CPAM2602 ...) exist only here; for those the
     filed_date is the first date the case appeared on a Legistar agenda.
  3. Hernando County Property Appraiser parcels layer (ArcGIS Online) to convert parcel KEYS to
     PA parcel numbers ("R06 123 21 0215 0000 0010"), acres, and a centroid.
"""
import datetime as _dt
import json
import re
import sys
import time

import requests

JURISDICTIONS = ["Hernando County"]

UA = "Mozilla/5.0 (X11; Linux x86_64) ELC-RezoningTracker/1.0 (+https://thedirtdog.com)"
ENERGOV = "https://hernandocountyfl-energovweb.tylerhost.net/apps/selfservice"
ENERGOV_API = ENERGOV + "/api"
ENERGOV_HDR = {"tenantId": "1", "tenantName": "hernandocountyflprod",
               "Tyler-TenantUrl": "hernandocountyflprod",
               "Content-Type": "application/json;charset=UTF-8"}
LEGISTAR = "https://webapi.legistar.com/v1/hernandocountyfl"
LEGISTAR_WEB = "https://hernandocountyfl.legistar.com"
PARCELS = ("https://services2.arcgis.com/x5zvhhxfUuRDntRe/arcgis/rest/services/"
           "Parcels/FeatureServer/0/query")
PLAN_TYPES = {  # EnerGov plan type ids in scope
    "47d73166-4f56-43ff-afdc-efe5de883919": "Rezoning",
    "84099529-5c3c-f368-68bb-5afab0972c62": "CPAM",
}
BOCC_ID, PZ_ID = 138, 140
SLEEP = 0.35
TODAY = _dt.date.today()

_s = requests.Session()
_s.headers["User-Agent"] = UA


def log(*a):
    print("[hernando]", *a, file=sys.stderr)


def _req(method, url, **kw):
    kw.setdefault("timeout", 60)
    last = None
    for i in range(3):
        try:
            r = _s.request(method, url, **kw)
            if r.status_code >= 500 or r.status_code == 429:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            time.sleep(SLEEP)
            return r
        except Exception as e:  # noqa
            last = e
            time.sleep(2 * (i + 1))
    raise last


def _json(method, url, **kw):
    return _req(method, url, **kw).json()


def _iso(s):
    if not s:
        return None
    if isinstance(s, (int, float)):
        return _dt.datetime.fromtimestamp(s / 1000, _dt.timezone.utc).date().isoformat()
    return str(s)[:10]


def _clean(s):
    if s is None:
        return None
    s = re.sub(r"\s+", " ", str(s)).strip()
    return s or None


# ----------------------------------------------------------------------------- EnerGov
_criteria = None


def _energov_criteria():
    global _criteria
    if _criteria is None:
        _criteria = _json("GET", ENERGOV_API + "/energov/search/criteria",
                          headers=ENERGOV_HDR)["Result"]
    return json.loads(json.dumps(_criteria))


def energov_search_type(type_id, apply_from):
    out, page = [], 1
    while True:
        c = _energov_criteria()
        c.update({"Keyword": "", "SearchModule": 3, "FilterModule": 3, "PageNumber": page,
                  "PageSize": 100, "SortBy": "ApplyDate", "SortAscending": False})
        c["PlanCriteria"].update({"PlanTypeId": type_id, "PlanStatusId": "none",
                                  "ApplyDateFrom": apply_from + "T00:00:00.000Z",
                                  "PageNumber": page, "PageSize": 100,
                                  "SortBy": "ApplyDate", "SortAscending": False})
        res = _json("POST", ENERGOV_API + "/energov/search/search", headers=ENERGOV_HDR,
                    json=c)["Result"] or {}
        rows = res.get("EntityResults") or []
        out += rows
        if len(rows) < 100 or page >= (res.get("TotalPages") or 1):
            break
        page += 1
    return out


def energov_by_number(num):
    c = _energov_criteria()
    c.update({"Keyword": num, "ExactMatch": True, "SearchModule": 1, "FilterModule": 1,
              "PageNumber": 1, "PageSize": 20, "SortBy": None, "SortAscending": False})
    res = _json("POST", ENERGOV_API + "/energov/search/search", headers=ENERGOV_HDR,
                json=c)["Result"] or {}
    for e in res.get("EntityResults") or []:
        if (e.get("CaseNumber") or "").upper() == num.upper():
            return e
    return None


def energov_applicant(case_id):
    try:
        body = {"EntityId": case_id, "ModuleId": 2, "PageNumber": 1, "PageSize": 20,
                "SortField": "", "IsSortedInAscendingOrder": True}
        res = _json("POST", ENERGOV_API + "/energov/entity/contacts/search/search",
                    headers=ENERGOV_HDR, json=body).get("Result") or []
    except Exception as e:
        log("contacts failed", case_id, e)
        return None
    def name(c):
        return _clean(c.get("GlobalEntityName")) or _clean(
            f"{c.get('FirstName') or ''} {c.get('LastName') or ''}")
    by = {}
    for c in res:
        by.setdefault((c.get("ContactTypeName") or "").lower(), name(c))
    for k in ("applicant", "agent", "property owner", "owner"):
        if by.get(k):
            return by[k]
    return next((v for v in by.values() if v), None)


# ----------------------------------------------------------------------------- Legistar
CASE_RE = re.compile(r"\(((?:H|CPAM|REZ)[- ]?\d[\d-]{2,14})\)\s*$", re.I)
TITLE_KEYS = ("Rezoning", "Master Plan", "Mater Plan", "Comprehensive Plan Amendment")


def legistar_matters(filter_expr):
    out, skip = [], 0
    while True:
        rows = _json("GET", LEGISTAR + "/matters",
                     params={"$filter": filter_expr, "$top": 1000, "$skip": skip})
        out += rows
        if len(rows) < 1000:
            return out
        skip += 1000


def _title_filter():
    return "(" + " or ".join(f"substringof('{k}',MatterTitle)" for k in TITLE_KEYS) + ")"


def norm_case(raw, known=()):
    """Legistar writes EnerGov numbers without dashes (REZ0000262026); normalise."""
    s = raw.upper().replace(" ", "")
    m = re.match(r"^(REZ|CPAM)-?(\d+)-?(\d{4})?$", s)
    if not m:
        return s
    pre, d, yr = m.group(1), m.group(2), m.group(3)
    if yr is None:
        if len(d) <= 4:  # legacy e.g. CPAM2602
            return pre + d
        d, yr = d[:-4], d[-4:]
    cand = f"{pre}-{int(d):06d}-{yr}"
    if known and cand not in known:
        for i in range(len(d)):  # typo like REZ00002102026 -> REZ-000021-2026
            if d[i] == "0":
                alt = f"{pre}-{int(d[:i] + d[i + 1:] or 0):06d}-{yr}"
                if alt in known:
                    return alt
    return cand


def _rtf_to_text(rtf):
    if not rtf:
        return ""
    t = re.sub(r"\\par[d]?", "\n", rtf)
    t = re.sub(r"\{\\(?:fonttbl|colortbl)[^}]*\}", "", t)
    t = re.sub(r"\\'[0-9a-f]{2}", "", t)
    t = re.sub(r"\\[a-z]+-?\d* ?", "", t)
    t = t.replace("{", "").replace("}", "")
    return re.sub(r"[ \t]+", " ", t).strip()


def classify(text):
    t = (text or "").lower()
    if not t:
        return None
    if "withdr" in t:
        return "Withdrawn"
    if re.search(r"postpon|continu|table[ds]? |defer|date certain", t):
        return "Continued"
    if re.search(r"\bden(y|ied|ial)", t):
        return "Denied"
    if re.search(r"approv|adopt", t):
        return "Approved"
    return None


def _effective_motion(minutes):
    """Drop motions that died / failed; return text of the remaining (last) motion(s)."""
    txt = _rtf_to_text(minutes)
    chunks = re.split(r"made the following Motion:", txt)
    keep = [c for c in chunks[1:] if not re.search(r"died for (a )?lack|motion failed|failed \d-\d", c, re.I)]
    return keep[-1] if keep else ""


_event_cache = {}


def _event_items(event_id):
    if event_id not in _event_cache:
        try:
            _event_cache[event_id] = _json(
                "GET", f"{LEGISTAR}/events/{event_id}/eventitems",
                params={"AgendaNote": 0, "MinutesNote": 1, "Attachments": 0})
        except Exception as e:
            log("event items failed", event_id, e)
            _event_cache[event_id] = []
    return _event_cache[event_id]


def _body_name(bid, name):
    if bid == BOCC_ID or "County Commissioners" in (name or ""):
        return "BOCC"
    if bid == PZ_ID or "Planning" in (name or ""):
        return "Planning & Zoning Commission"
    return name


def matter_hearings(m):
    """Return list of hearings for one Legistar matter."""
    hs = []
    try:
        hist = _json("GET", f"{LEGISTAR}/matters/{m['MatterId']}/histories")
    except Exception as e:
        log("histories failed", m["MatterId"], e)
        hist = []
    title = m.get("MatterTitle") or ""
    seen = set()
    for h in hist:
        date = _iso(h.get("MatterHistoryActionDate"))
        body = _body_name(h.get("MatterHistoryActionBodyId"), h.get("MatterHistoryActionBodyName"))
        if not date or (body, date) in seen:
            continue
        if (h.get("MatterHistoryPassedFlagName") or "").lower() == "fail" and any(
                (x.get("MatterHistoryPassedFlagName") or "").lower() == "pass" and
                _iso(x.get("MatterHistoryActionDate")) == date for x in hist):
            continue
        text = h.get("MatterHistoryActionText") or ""
        ev = h.get("MatterHistoryEventId")
        if ev:
            for it in _event_items(ev):
                if it.get("EventItemMatterId") == m["MatterId"]:
                    text = " ".join(filter(None, [it.get("EventItemActionText"),
                                                  _effective_motion(it.get("EventItemMinutesNote"))]))
                    break
        res = classify(" ".join([text, title if re.search(r"postpone|ratifying denial|withdraw", title, re.I) else ""]))
        if res is None and (h.get("MatterHistoryPassedFlagName") or "").lower() == "pass" and not text:
            res = None  # passed, but motion text unknown
        if body == "Planning & Zoning Commission" and res in ("Approved", "Denied"):
            res = "Recommended " + ("Approval" if res == "Approved" else "Denial")
        seen.add((body, date))
        hs.append({"body": body, "date": date, "result": res})
    if not hs and m.get("MatterAgendaDate"):
        body = _body_name(m.get("MatterBodyId"), m.get("MatterBodyName"))
        hs.append({"body": body, "date": _iso(m["MatterAgendaDate"]),
                   "result": classify(title) if re.search(r"postpone|ratifying denial|withdraw", title, re.I) else None})
    return hs


def matter_text(mid):
    try:
        vers = _json("GET", f"{LEGISTAR}/matters/{mid}/versions")
        if not vers:
            return ""
        key = vers[-1]["Key"]
        return _json("GET", f"{LEGISTAR}/matters/{mid}/texts/{key}").get("MatterTextPlain") or ""
    except Exception as e:
        log("text failed", mid, e)
        return ""


def matter_staff_report(mid):
    try:
        atts = _json("GET", f"{LEGISTAR}/matters/{mid}/attachments")
    except Exception:
        return None
    for pat in ("staff report", "application packet", "map"):
        for a in atts:
            if pat in (a.get("MatterAttachmentName") or "").lower():
                return a.get("MatterAttachmentHyperlink")
    return None


def _section(text, label):
    m = re.search(label + r"[^:\n]*:\s*\n?(.+?)(?:\n\s*\n|\n[A-Z][A-Za-z' ]+:\s*\n)", text + "\n\n", re.S)
    return _clean(m.group(1)) if m else None


# ----------------------------------------------------------------------------- parcels
def parcel_lookup(keys):
    keys = [int(k) for k in keys if str(k).isdigit()][:60]
    if not keys:
        return []
    try:
        d = _json("GET", PARCELS, params={
            "where": f"PARCEL_KEY IN ({','.join(map(str, keys))})",
            "outFields": "PARCEL_KEY,PARCEL_NUMBER,ACRES,SITUS_ADDRESS,SITUS_CITY",
            "returnCentroid": "true", "returnGeometry": "false", "outSR": 4326, "f": "json"})
    except Exception as e:
        log("parcel lookup failed", e)
        return []
    out = []
    for f in d.get("features", []):
        a = f["attributes"]
        c = f.get("centroid") or {}
        out.append({"key": a["PARCEL_KEY"], "pin": _clean(a.get("PARCEL_NUMBER")),
                    "acres": a.get("ACRES"), "addr": _clean(a.get("SITUS_ADDRESS")),
                    "city": _clean(a.get("SITUS_CITY")), "lat": c.get("y"), "lon": c.get("x")})
    return out


def _keys_from(text):
    return [str(int(k)) for k in re.findall(r"\b0*(\d{4,8})\b", text or "")]


# ----------------------------------------------------------------------------- build
def _zoning_from_request(req):
    if not req:
        return None, None
    m = re.search(r"\bfrom\s+(.+?)\s+to\s+(.+?)(?:\s+with\b|\s+on\b|[.;]|$)", req, re.I)
    if not m:
        return None, None
    return _clean(m.group(1)), _clean(m.group(2))


def _acres_from(text):
    m = re.search(r"(\d[\d,]*\.?\d*)\s*(?:\+/-|±)?\s*-?\s*(?:gross\s+)?acres?\b", text or "", re.I) or \
        re.search(r"±\s*(\d[\d,]*\.?\d*)\s*-?\s*acre", text or "", re.I)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            return None
    return None


def _category(title, workclass, to_zone, req):
    t = " ".join(filter(None, [title, workclass])).lower()
    if "comprehensive plan" in t or "cpam" in t:
        return "Comp Plan Amendment"
    if "master plan" in t or "mater plan" in t or "pdp" in t:
        return "PD"
    if to_zone and "PDP" in to_zone.upper():
        return "PD"
    if req and re.search(r"\bto\s+(C?PDP|PD)\b", req, re.I):
        return "PD"
    return "Rezoning"


def _energov_status(raw):
    r = (raw or "").lower()
    if any(k in r for k in ("void", "withdraw", "expired", "cancel")):
        return "Withdrawn"
    if "denied" in r:
        return "Denied"
    return None


def build_record(num, eg, matters, since):
    hearings, seen = [], set()
    for m in sorted(matters, key=lambda x: (x.get("MatterAgendaDate") or x.get("MatterIntroDate") or "")):
        for h in matter_hearings(m):
            k = (h["body"], h["date"])
            if k in seen:
                for x in hearings:
                    if (x["body"], x["date"]) == k and x["result"] is None:
                        x["result"] = h["result"]
                continue
            seen.add(k)
            hearings.append(h)
    hearings.sort(key=lambda h: h["date"])

    latest = max(matters, key=lambda x: x.get("MatterIntroDate") or "") if matters else None
    text = matter_text(latest["MatterId"]) if latest else ""
    req = _section(text, r"(?:Rezoning )?Request") if text else None
    loc = _section(text, "General Location") if text else None
    keytxt = _section(text, r"Parcel Key") if text else None
    keys = _keys_from(keytxt)
    if eg:
        keys = _keys_from(eg.get("MainParcel")) + keys
        desc = eg.get("Description") or ""
        if re.search(r"parcel", desc, re.I):
            keys += _keys_from(desc.split(":", 1)[-1] if ":" in desc else desc)
    keys = list(dict.fromkeys(keys))
    parcels = parcel_lookup(keys)

    title = latest.get("MatterTitle") if latest else None
    workclass = eg.get("CaseWorkclass") if eg else None
    fz, tz = _zoning_from_request(req) if req and "land use" not in req.lower() else (None, None)
    ff = tf = None
    cat = _category(title, workclass, tz, req)
    if cat == "Comp Plan Amendment" and req:
        ff, tf = _zoning_from_request(req)
    acres = _acres_from(req) or _acres_from(text[:3000]) or (_acres_from(eg.get("Description")) if eg else None)
    if acres is None and parcels and not re.search(
            r"portion", (keytxt or "") + " " + ((eg or {}).get("Description") or ""), re.I):
        acres = round(sum(p["acres"] or 0 for p in parcels), 2) or None

    applicant = None
    if eg:
        applicant = energov_applicant(eg["CaseId"])
    if not applicant and title:
        m = re.search(r"Submitted by (.+?)(?:,? for Property|,? On Behalf| \(|$)", title, re.I)
        applicant = _clean(m.group(1)) if m else None

    filed = _iso(eg.get("ApplyDate")) if eg else None
    if not filed and matters:
        filed = min(_iso(m.get("MatterIntroDate")) for m in matters if m.get("MatterIntroDate"))

    # status
    status_raw = eg.get("CaseStatus").strip() if eg and eg.get("CaseStatus") else None
    status, decision = _energov_status(status_raw), None
    bocc = [h for h in hearings if h["body"] == "BOCC" and h["result"] in ("Approved", "Denied")]
    if status is None:
        if any(h["result"] == "Withdrawn" for h in hearings):
            status = "Withdrawn"
        elif bocc:
            status, decision = bocc[-1]["result"], bocc[-1]["date"]
        elif any(h["date"] >= TODAY.isoformat() for h in hearings):
            status = "Hearing Scheduled"
        elif hearings and hearings[-1]["result"] == "Continued":
            status = "Continued"
        else:
            status = "Filed"
    if not status_raw and latest:
        status_raw = f"Legistar: {latest.get('MatterStatusName')}" + (
            f" ({bocc[-1]['result']} by BOCC {bocc[-1]['date']})" if bocc else "")

    addr = None
    if eg:
        addr = _clean(eg.get("AddressDisplay"))
    if not addr:
        good = [p for p in parcels if p["addr"] and not p["addr"].startswith("0 ")]
        addr = good[0]["addr"] + (", " + good[0]["city"] if good[0]["city"] else "") if good else loc
    pts = [p for p in parcels if p["lat"]]
    lat = round(sum(p["lat"] for p in pts) / len(pts), 6) if pts else None
    lon = round(sum(p["lon"] for p in pts) / len(pts), 6) if pts else None

    if eg:
        url = f"{ENERGOV}#/plan/{eg['CaseId']}"
    else:
        url = f"{LEGISTAR_WEB}/LegislationDetail.aspx?ID={latest['MatterId']}&GUID={latest['MatterGuid']}"
    docs = None
    for m in sorted(matters, key=lambda x: x.get("MatterIntroDate") or "", reverse=True)[:2]:
        docs = matter_staff_report(m["MatterId"])
        if docs:
            break
    if not docs and latest:
        docs = f"{LEGISTAR_WEB}/LegislationDetail.aspx?ID={latest['MatterId']}&GUID={latest['MatterGuid']}"

    descr = req or _clean(eg.get("Description") if eg else None) or _clean(title)
    if descr and acres and "acre" not in descr.lower():
        descr = f"{descr} ({acres:g} ac)"
    pname = None
    if title:
        m = re.search(r"for Property Located on (.+?) \(", title)
        pname = _clean(m.group(1)) if m else None
    return {
        "jurisdiction": "Hernando County", "county": "Hernando", "app_num": num,
        "category": cat,
        "app_type": (eg.get("CaseType") if eg else None) or _clean(re.sub(r"\s*Submitted by.*", "", title or "")) or cat,
        "project_name": _clean(eg.get("ProjectName")) if eg and eg.get("ProjectName") else pname,
        "description": descr[:500] if descr else None,
        "applicant": applicant,
        "parcel_ids": [p["pin"] for p in parcels if p["pin"]],
        "address": addr, "acres": acres,
        "from_zoning": fz, "to_zoning": tz, "from_flu": ff, "to_flu": tf,
        "filed_date": filed, "status": status, "status_raw": status_raw,
        "hearings": hearings, "decision_date": decision,
        "url": url, "docs_url": docs, "lat": lat, "lon": lon,
        "source": "Hernando EnerGov CSS + Legistar" if eg and matters else (
            "Hernando EnerGov CSS" if eg else "Hernando Legistar"),
    }


def fetch(since, refresh):
    refresh = [r for r in (refresh or []) if r]
    since_d = _dt.date.fromisoformat(since)
    # EnerGov applications
    eg_cases = {}
    try:
        for tid in PLAN_TYPES:
            for e in energov_search_type(tid, since):
                eg_cases[e["CaseNumber"].upper()] = e
    except Exception as e:
        log("EnerGov search failed:", e)
        eg_err = e
    else:
        eg_err = None
    for r in refresh:
        if re.match(r"^(REZ|CPAM)-\d{6}-\d{4}$", r, re.I) and r.upper() not in eg_cases:
            try:
                e = energov_by_number(r.upper())
                if e:
                    eg_cases[r.upper()] = e
            except Exception as ex:
                log("EnerGov refresh failed", r, ex)

    # Legistar matters (look back far enough to know a case's first appearance)
    lookback = (since_d - _dt.timedelta(days=540)).isoformat()
    try:
        ms = legistar_matters(f"MatterIntroDate ge datetime'{lookback}' and {_title_filter()}")
    except Exception as e:
        log("Legistar failed:", e)
        if eg_err:
            raise RuntimeError(f"both sources unreachable: {eg_err}; {e}")
        ms = []
    for r in refresh:  # legacy refresh ids older than the lookback
        if not any(r.upper() in (m.get("MatterTitle") or "").upper().replace("-", "") for m in ms) \
                and re.match(r"^(H\d{3,4}|CPAM\d{4})$", r, re.I):
            try:
                ms += legistar_matters(f"substringof('({r})',MatterTitle)")
            except Exception as e:
                log("Legistar refresh failed", r, e)
    known = set(eg_cases)
    by_case = {}
    for m in ms:
        t = m.get("MatterTitle") or ""
        mm = CASE_RE.search(t)
        if not mm or re.search(r"Notice of|Transmittal of Minutes|Special Exception|Conditional Use", t, re.I):
            continue
        num = norm_case(mm.group(1), known)
        by_case.setdefault(num, {})[m["MatterId"]] = m

    wanted = set()
    for num, e in eg_cases.items():
        if _iso(e.get("ApplyDate")) and _iso(e.get("ApplyDate")) >= since:
            wanted.add(num)
    for num, mm in by_case.items():
        first = min(_iso(m["MatterIntroDate"]) for m in mm.values())
        if num not in eg_cases and first >= since and not re.match(r"^(REZ|CPAM)-", num):
            wanted.add(num)
    for r in refresh:
        n = norm_case(r, known)
        if n in eg_cases or n in by_case:
            wanted.add(n)
        else:
            log("refresh id not found:", r)

    out = []
    for num in sorted(wanted):
        eg = eg_cases.get(num)
        if eg is None and re.match(r"^(REZ|CPAM)-", num):
            try:
                eg = energov_by_number(num)
            except Exception:
                eg = None
        if eg and re.search(r"minor modification", (eg.get("Description") or ""), re.I) \
                and num not in by_case:
            continue  # administrative minor modifications are out of scope
        try:
            out.append(build_record(num, eg, list(by_case.get(num, {}).values()), since))
        except Exception as e:
            log("skip", num, repr(e))
    return out


if __name__ == "__main__":
    since = sys.argv[1] if len(sys.argv) > 1 else (TODAY - _dt.timedelta(days=14)).isoformat()
    refresh = sys.argv[2].split(",") if len(sys.argv) > 2 else []
    t0 = time.time()
    recs = fetch(since, refresh)
    print(json.dumps(recs, indent=1))
    from collections import Counter
    log(f"{len(recs)} records since {since} in {time.time() - t0:.0f}s;",
        dict(Counter(r["category"] for r in recs)), dict(Counter(r["status"] for r in recs)))
