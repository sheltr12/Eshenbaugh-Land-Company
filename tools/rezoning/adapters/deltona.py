#!/usr/bin/env python3
"""City of Deltona (Volusia County) rezoning / PUD / future land use map amendment adapter.

Sources (all public, no login):
 1. Tyler EnerGov Citizen Self Service (deltonafl-energovweb.tylerhost.net, tenant "Prod"), plan
    search by type and application date:
      plan type "Rezoning" (work classes COMMERCIAL / RESIDENTIAL REZONING -> Rezoning,
        BUSINESS / INDUSTRIAL / MIXED / RESIDENTIAL PUD -> PD), case numbers RZ26-0010;
      plan type "Comprehensive Plan" (COMP PLAN SMALL SCALE / LARGE SCALE, TEXT/MAP AMENDMENT)
        -> Comp Plan Amendment, case numbers "CP 26-0008" (TEXT AMENDMENT is skipped).
    EnerGov gives case number, status, apply date, main parcel and address (no description).
 2. Deltona Legistar Web API (webapi.legistar.com/v1/deltona): Planning and Zoning Board and City
    Commission agenda items ("Ordinance No. 17-2026 - Amending the Official Zoning Map to rezone
    ... 18.13 acres ... from ... to ..."). The case number is read from attachment names
    ("Staff Report - RZ26-0010") or from page 1 of the staff report ("Re: RZ26-0010, Ordinance
    No. 17-2026, ..."), which also lists parcel IDs (9206-01-03-0010) and the applicant. Items of
    the same ordinance number are linked. Hearing results come from the item action / passed flag.
Parcel IDs are Volusia parcel numbers, dashed (9206-01-03-0010); EnerGov's undashed main parcel is
converted to the same format.
"""
import datetime as dt
import io
import json
import re
import sys
import time

import requests

try:
    import pypdf
except ImportError:  # pragma: no cover
    pypdf = None

JURISDICTIONS = ["City of Deltona"]
JUR = "City of Deltona"
SOURCE = "Deltona EnerGov CSS + Legistar (Planning and Zoning Board / City Commission)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
ENERGOV = "https://deltonafl-energovweb.tylerhost.net/apps/selfservice"
EG_API = ENERGOV + "/api/energov"
EG_HDR = {"tenantId": "1", "tenantName": "Prod", "Tyler-TenantUrl": "Home",
          "Content-Type": "application/json;charset=UTF-8"}
LEGISTAR = "https://webapi.legistar.com/v1/deltona"
LEGISTAR_WEB = "https://deltona.legistar.com"
PLAN_TYPES = {
    "47d73166-4f56-43ff-afdc-efe5de883919": "Rezoning",
    "ea4d2d4c-4920-47c9-8156-2f6619c6c40f": "Comp Plan",
}
CASE_RX = re.compile(r"\b(RZ\s?\d{2}-\d{4}|CP\s?\d{2}-\d{4})\b", re.I)
ORD_RX = re.compile(r"Ordinance\s+No\.?\s*(\d{1,3}-\d{4})", re.I)
ZONING_ITEM = re.compile(r"Zoning Map|\brezon|Future Land Use Map|Land Use Map|Comprehensive Plan Amendment|"
                         r"\bPUD\b|Planned Unit Development", re.I)
TEXT_ITEM = re.compile(r"amending Section|Land Development Code|Chapter \d+|Workshop|moratorium", re.I)
SLEEP = 0.35
LOOKAHEAD_DAYS = 75

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


def log(*a):
    print("[deltona]", *a, file=sys.stderr, flush=True)


def req(method, url, tries=3, sleep=SLEEP, **kw):
    kw.setdefault("timeout", 90)
    last = None
    for i in range(tries):
        try:
            r = _s.request(method, url, **kw)
            if r.status_code >= 500 or r.status_code == 429:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            time.sleep(sleep)
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1) ** 2)
    raise RuntimeError(f"{method} failed {url}: {last}")


def clean(s):
    if s is None:
        return None
    s = re.sub(r"\s+", " ", str(s).replace("\xa0", " ")).strip()
    return s or None


def norm_case(c):
    c = re.sub(r"\s+", "", (c or "").upper())
    m = re.match(r"^(RZ|CP)(\d{2}-\d{4})$", c)
    if not m:
        return c
    return f"RZ{m.group(2)}" if m.group(1) == "RZ" else f"CP {m.group(2)}"


def fmt_parcel(p):
    p = re.sub(r"[\s\-]", "", p or "")
    if re.fullmatch(r"[0-9A-Z]{12}", p):
        return f"{p[0:4]}-{p[4:6]}-{p[6:8]}-{p[8:12]}"
    return p or None


# ------------------------------------------------------------------ EnerGov
_criteria = None


def criteria():
    global _criteria
    if _criteria is None:
        _criteria = req("GET", EG_API + "/search/criteria", headers=EG_HDR).json()["Result"]
    return json.loads(json.dumps(_criteria))


def eg_search_type(type_id, apply_from):
    out, page = [], 1
    while True:
        c = criteria()
        c.update({"Keyword": "", "SearchModule": 3, "FilterModule": 3, "PageNumber": page, "PageSize": 100,
                  "SortBy": "ApplyDate", "SortAscending": False})
        c["PlanCriteria"].update({"PlanTypeId": type_id, "PlanStatusId": "none",
                                  "ApplyDateFrom": apply_from + "T00:00:00.000Z", "PageNumber": page,
                                  "PageSize": 100, "SortBy": "ApplyDate", "SortAscending": False})
        res = req("POST", EG_API + "/search/search", headers=EG_HDR, json=c).json().get("Result") or {}
        rows = res.get("EntityResults") or []
        out += rows
        if len(rows) < 100 or page >= (res.get("TotalPages") or 1):
            return out
        page += 1


def eg_by_number(num):
    c = criteria()
    c.update({"Keyword": num, "ExactMatch": True, "SearchModule": 1, "FilterModule": 1, "PageNumber": 1,
              "PageSize": 20, "SortBy": None, "SortAscending": False})
    res = req("POST", EG_API + "/search/search", headers=EG_HDR, json=c).json().get("Result") or {}
    for e in res.get("EntityResults") or []:
        if norm_case(e.get("CaseNumber")) == norm_case(num):
            return e
    return None


def eg_plan(case_id):
    try:
        return req("GET", f"{EG_API}/plans/{case_id}", headers=EG_HDR).json().get("Result") or {}
    except Exception as e:  # noqa: BLE001
        log(f"plan detail {case_id}: {e}")
        return {}


def eg_applicant(case_id):
    body = {"EntityId": case_id, "ModuleId": 2, "PageNumber": 1, "PageSize": 50, "SortField": "",
            "IsSortedInAscendingOrder": True}
    try:
        res = req("POST", EG_API + "/entity/contacts/search/search", headers=EG_HDR, json=body).json()
    except Exception as e:  # noqa: BLE001
        log(f"contacts {case_id}: {e}")
        return None
    by = {}
    for c in res.get("Result") or []:
        org = clean(c.get("GlobalEntityName"))
        person = clean(f"{c.get('FirstName') or ''} {c.get('LastName') or ''}")
        by.setdefault((c.get("ContactTypeName") or "").lower(), org or person)
    for pat in ("applicant", "agent", "owner"):
        for k, v in by.items():
            if pat in k and v:
                return v
    return next((v for v in by.values() if v), None)


# ------------------------------------------------------------------ Legistar
def body_of(name):
    n = name or ""
    if re.search(r"Planning and Zoning", n, re.I):
        return "Planning and Zoning Board"
    if re.search(r"City Commission", n, re.I) and not re.search(r"Workshop", n, re.I):
        return "City Commission"
    return None


_pdf_cache = {}


def pdf_first_pages(url, pages=2):
    if not url or pypdf is None:
        return ""
    if url in _pdf_cache:
        return _pdf_cache[url]
    try:
        r = req("GET", url)
        rd = pypdf.PdfReader(io.BytesIO(r.content))
        txt = "\n".join((p.extract_text() or "") for p in rd.pages[:pages])
    except Exception as e:  # noqa: BLE001
        log(f"pdf {url}: {e}")
        txt = ""
    _pdf_cache[url] = txt
    return txt


def item_result(body, action, passed, title, minutes):
    a = (action or "").lower()
    p = (passed or "").lower()
    txt = (a + " " + re.sub(r"\\[a-z]+-?\d* ?|[{}]", " ", minutes or "")).lower()
    if not a and not p:
        return None
    if re.search(r"withdr", txt):
        return "Withdrawn"
    if re.search(r"continu|table|postpon|defer", a):
        return "Continued"
    if body == "Planning and Zoning Board":
        if "denial" in a or "deny" in a or (p == "fail" and "approv" in a):
            return "Recommended Denial"
        if p == "pass" and re.search(r"approv|accept|recommend", a):
            return "Recommended Approval"
        return None
    if p == "fail":
        return "Denied" if re.search(r"approv|adopt|accept", a) else None
    if re.search(r"\bden", a):
        return "Denied"
    if p == "pass" and re.search(r"approv|adopt|accept|pass", a):
        if re.search(r"first reading", title or "", re.I):
            return "Approved (first reading)"
        return "Approved"
    return None


def scan_legistar(start, end):
    """{case_or_ordinance_key: [appearance]} for zoning items on P&Z / Commission agendas."""
    evs = req("GET", LEGISTAR + "/events", params={
        "$filter": f"EventDate ge datetime'{start}' and EventDate lt datetime'{end}'",
        "$orderby": "EventDate"}).json()
    apps = []
    for ev in evs:
        body = body_of(ev.get("EventBodyName"))
        if not body:
            continue
        try:
            items = req("GET", f"{LEGISTAR}/events/{ev['EventId']}/eventitems",
                        params={"AgendaNote": 0, "MinutesNote": 1, "Attachments": 1}).json()
        except Exception as e:  # noqa: BLE001
            log(f"event {ev['EventId']}: {e}")
            continue
        for it in items:
            title = clean(it.get("EventItemTitle")) or ""
            if not it.get("EventItemMatterId") or not ZONING_ITEM.search(title) or TEXT_ITEM.search(title):
                continue
            atts = it.get("EventItemMatterAttachments") or []
            names = " ".join(a.get("MatterAttachmentName") or "" for a in atts)
            cases = [norm_case(c) for c in CASE_RX.findall(title + " " + names)]
            staff = [a.get("MatterAttachmentHyperlink") for a in atts
                     if re.search(r"staff\s*report", a.get("MatterAttachmentName") or "", re.I)]
            ordm = ORD_RX.search(title)
            apps.append({
                "date": ev["EventDate"][:10], "body": body, "title": title,
                "ordinance": ordm.group(1) if ordm else None, "cases": list(dict.fromkeys(cases)),
                "staff": staff[0] if staff else None, "event_id": ev["EventId"],
                "url": ev.get("EventInSiteURL") or f"{LEGISTAR_WEB}/Calendar.aspx",
                "result": item_result(body, it.get("EventItemActionName"), it.get("EventItemPassedFlagName"),
                                      title, it.get("EventItemMinutesNote")),
                "matter_id": it.get("EventItemMatterId"),
            })
    log(f"{len(evs)} Legistar events {start}..{end}; {len(apps)} zoning agenda items")
    # staff report page 1 gives case number, parcels, applicant
    for a in apps:
        a["parcels"], a["applicant"] = [], None
        if a["staff"]:
            txt = pdf_first_pages(a["staff"])
            head = txt[:1500]
            if not a["cases"]:
                a["cases"] = [norm_case(c) for c in dict.fromkeys(CASE_RX.findall(head))]
            flat = re.sub(r"\s+", " ", txt)
            a["parcels"] = list(dict.fromkeys(re.findall(r"\b\d{4}-\d{2}-\d{2}-\d{3}[0-9A-Z]\b", flat)))
            m = re.search(r"\((?:Applicant/Agent|Applicant|Agent)[,:]?\s*([^)]{3,120})\)", flat)
            if m:
                a["applicant"] = clean(m.group(1))
            if not a["ordinance"]:
                om = ORD_RX.search(head)
                a["ordinance"] = om.group(1) if om else None
    # link by ordinance number
    by_ord = {}
    for a in apps:
        if a["ordinance"] and a["cases"]:
            by_ord.setdefault(a["ordinance"], set()).update(a["cases"])
    out = {}
    for a in apps:
        cases = a["cases"] or sorted(by_ord.get(a["ordinance"], []))
        for c in cases:
            out.setdefault(c, []).append(a)
    return out


# ------------------------------------------------------------------ build
def category_of(e):
    t = " ".join(filter(None, [e.get("CaseType"), e.get("CaseWorkclass")])).upper()
    if "COMP PLAN" in t or "MAP AMENDMENT" in t:
        return "Comp Plan Amendment"
    if "TEXT AMENDMENT" in t and "MAP" not in t:
        return None
    if "PUD" in t:
        return "PD"
    if "REZON" in t:
        return "Rezoning"
    return None


def parse_title(title):
    t = title or ""
    out = {}
    m = re.search(r"([\d,]*\.?\d+)\s*(?:\+/-\s*)?acres", t, re.I)
    if m:
        try:
            out["acres"] = float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    m = re.search(r"\bfrom\s+(.+?)\s+to\s+(.+?)(?:,\s*(?:at|for)\b|\.\s*$|\.$|$)", t, re.I)
    if m:
        out["from"], out["to"] = clean(m.group(1)), clean(m.group(2).rstrip("."))
    m = re.search(r"located (?:at|along)\s+(.+?)(?:,\s*from|\s+from|\s+within|,\s*east|,\s*west|,\s*north|,\s*south)", t, re.I)
    if m:
        out["address"] = clean(m.group(1))
    return out


def status_of(raw, hearings, today):
    r = (raw or "").upper()
    finals = [h for h in hearings if h["body"] == "City Commission" and h["result"] in ("Approved", "Denied")
              and h["date"] <= today]
    if re.search(r"WITHDR|VOID|CANCEL|EXPIRED|CLOSED", r):
        return "Withdrawn"
    if re.search(r"DENIED", r) or (finals and finals[-1]["result"] == "Denied"):
        return "Denied"
    if re.search(r"APPROVED|COMPLETE|ISSUED|FINAL", r) or finals:
        return "Approved"
    if any(h["date"] > today for h in hearings):
        return "Hearing Scheduled"
    past = [h for h in hearings if h["date"] <= today]
    if past and past[-1]["result"] == "Continued":
        return "Continued"
    return "Filed"


def build(num, e, apps, today):
    cat = category_of(e)
    if cat is None:
        return None
    plan = eg_plan(e["CaseId"]) if e.get("CaseId") else {}
    applicant = eg_applicant(e["CaseId"]) if e.get("CaseId") else None
    apps = sorted(apps, key=lambda a: (a["date"], a["body"]))
    parcels = []
    mp = fmt_parcel(e.get("MainParcel") or plan.get("MainParcelNumber"))
    for a in apps:
        parcels += a["parcels"]
    if mp:
        parcels.insert(0, mp)
    parcels = list(dict.fromkeys(parcels))
    info = {}
    for a in apps:
        for k, v in parse_title(a["title"]).items():
            info.setdefault(k, v)
    hearings = []
    for a in apps:
        h = {"body": a["body"], "date": a["date"], "result": a["result"]}
        if a["result"] is None and a["date"] > today:
            h["result"] = None
        if not any(x["body"] == h["body"] and x["date"] == h["date"] for x in hearings):
            hearings.append(h)
    status = status_of(e.get("CaseStatus"), hearings, today)
    decision = None
    if status in ("Approved", "Denied"):
        fin = [h for h in hearings if h["body"] == "City Commission" and h["result"] in ("Approved", "Denied")]
        decision = fin[-1]["date"] if fin else (e.get("CompleteDate") or e.get("FinalDate") or "")[:10] or None
    addr = clean((e.get("Address") or {}).get("FullAddress") or e.get("AddressDisplay") or plan.get("MainAddress"))
    if addr:
        addr = re.sub(r"\s+", " ", addr).strip()
    titles = [a["title"] for a in apps]
    desc = None
    if titles:
        desc = re.sub(r"^(?:Public Hearing|Quasi-judicial Hearing)\s*-\s*", "", titles[0], flags=re.I)
    from_z = to_z = from_f = to_f = None
    if cat == "Comp Plan Amendment":
        from_f, to_f = info.get("from"), info.get("to")
    else:
        from_z, to_z = info.get("from"), info.get("to")
    ords = list(dict.fromkeys(a["ordinance"] for a in apps if a["ordinance"]))
    pname = None
    if addr:
        pname = re.sub(r"\s+(DELTONA|DEBARY|ORANGE CITY)?,?\s*FL\s*\d{5}.*$", "", addr, flags=re.I).title()
    latest = apps[-1] if apps else None
    staff = [a["staff"] for a in reversed(apps) if a["staff"]]
    app_type = clean(" - ".join(filter(None, [e.get("CaseType"), e.get("CaseWorkclass")
                                              if (e.get("CaseWorkclass") or "").upper() not in (e.get("CaseType") or "").upper() else None])))
    if ords:
        app_type = f"{app_type} (Ordinance No. {ords[0]})"
    if desc and len(desc) > 400:
        desc = desc[:397].rstrip() + "..."
    return {
        "jurisdiction": JUR, "county": "Volusia", "app_num": e.get("CaseNumber") or num, "category": cat,
        "app_type": app_type, "project_name": pname, "description": desc,
        "applicant": next((a["applicant"] for a in apps if a["applicant"]), None) or applicant,
        "parcel_ids": parcels, "address": addr.title() if addr else (info.get("address")), "acres": info.get("acres"),
        "from_zoning": from_z, "to_zoning": to_z, "from_flu": from_f, "to_flu": to_f,
        "filed_date": (e.get("ApplyDate") or "")[:10] or (apps[0]["date"] if apps else None),
        "status": status, "status_raw": clean(e.get("CaseStatus")), "hearings": hearings, "decision_date": decision,
        "url": f"{ENERGOV}#/plan/{e['CaseId']}" if e.get("CaseId") else (latest["url"] if latest else ENERGOV),
        "docs_url": staff[0] if staff else None,
        "lat": None, "lon": None, "source": SOURCE,
    }



# ------------------------------------------------------------------ parcel fallback by street address
VC_PARCELS = "https://maps5.vcgov.org/arcgis/rest/services/Basemap/MapServer/6/query"
_DIRS = {"N", "S", "E", "W", "NORTH", "SOUTH", "EAST", "WEST", "NE", "NW", "SE", "SW"}


def parcels_by_address(addr, city):
    """Volusia parcel numbers (dashed) for one street address via the county parcel layer
    (ADRNO + street name + city). Used only when the source posts no parcel number.
    Returns [] when the address is not specific or matches more than 5 parcels."""
    t = (addr or "").upper().replace(".", " ")
    m = re.match(r"\s*(\d{1,6})\s+(.+)$", t)
    if not m:
        return []
    words = [w for w in re.split(r"[\s,]+", m.group(2)) if w]
    direction = words[0][0] if words and words[0] in _DIRS else None
    while words and words[0] in _DIRS:
        words = words[1:]
    if not words or not re.match(r"^[A-Z0-9]+$", words[0]):
        return []
    street = words[0]
    if street in ("US", "U", "STATE", "SR"):
        street = "US" if street in ("US", "U") else "STATE"
    where = f"ADRNO={int(m.group(1))} AND ADRSTR LIKE '{street}%' AND CITYNAME='{city}'"
    try:
        r = _s.get(VC_PARCELS, params={"where": where, "outFields": "PID,ALTKEY,ADRDIR", "returnGeometry": "false",
                                       "f": "json"}, timeout=60)
        time.sleep(0.3)
        feats = r.json().get("features") or []
    except Exception as e:  # noqa: BLE001
        log(f"parcel lookup {addr}: {e}")
        return []
    pids = []
    for f in feats:
        at = f.get("attributes") or {}
        if direction and (at.get("ADRDIR") or "").strip() and not (at.get("ADRDIR") or "").strip().startswith(direction):
            continue
        p = re.sub(r"\D", "", str(at.get("PID") or ""))
        if len(p) == 12:
            pids.append(f"{p[0:4]}-{p[4:6]}-{p[6:8]}-{p[8:12]}")
    pids = list(dict.fromkeys(pids))
    return pids if 0 < len(pids) <= 5 else []


def fetch(since, refresh=None):
    refresh = [r.strip() for r in (refresh or []) if r and r.strip()]
    today = dt.date.today()
    lookback = (dt.date.fromisoformat(since) - dt.timedelta(days=7)).isoformat()
    found = {}
    ok = False
    for tid, name in PLAN_TYPES.items():
        try:
            rows = eg_search_type(tid, lookback)
            ok = True
        except Exception as ex:  # noqa: BLE001
            log(f"EnerGov search {name}: {ex}")
            continue
        log(f"EnerGov {name}: {len(rows)} cases applied since {lookback}")
        for e in rows:
            if (e.get("ApplyDate") or "")[:10] >= since:
                found[norm_case(e.get("CaseNumber"))] = e
    if not ok:
        raise RuntimeError("Deltona EnerGov unreachable")
    for r in refresh:
        k = norm_case(r)
        if k in found:
            continue
        try:
            e = eg_by_number(r)
        except Exception as ex:  # noqa: BLE001
            log(f"refresh lookup {r}: {ex}")
            e = None
        if e:
            found[k] = e
        else:
            log(f"refresh id not found in EnerGov: {r}")
    applies = [(e.get("ApplyDate") or "")[:10] for e in found.values() if e.get("ApplyDate")]
    start = min([dt.date.fromisoformat(since)] + [dt.date.fromisoformat(a) for a in applies if a])
    try:
        agenda = scan_legistar(start.isoformat(), (today + dt.timedelta(days=LOOKAHEAD_DAYS)).isoformat())
    except Exception as ex:  # noqa: BLE001
        log(f"Legistar scan failed ({ex}); EnerGov only")
        agenda = {}
    out = []
    for k, e in sorted(found.items()):
        try:
            rec = build(k, e, agenda.get(k, []), today.isoformat())
        except Exception as ex:  # noqa: BLE001
            log(f"{k}: skipped ({ex})")
            continue
        if rec:
            out.append(rec)
    for rec in out:
        if not rec["parcel_ids"] and rec.get("address"):
            rec["parcel_ids"] = parcels_by_address(rec["address"], "DELTONA")
    return out


if __name__ == "__main__":
    since = sys.argv[1] if len(sys.argv) > 1 else (dt.date.today() - dt.timedelta(days=14)).isoformat()
    refresh = sys.argv[2].split(",") if len(sys.argv) > 2 else []
    t0 = time.time()
    recs = fetch(since, refresh)
    print(json.dumps(recs, indent=2, ensure_ascii=False))
    from collections import Counter
    log(f"{len(recs)} records in {time.time() - t0:.0f}s; status={dict(Counter(r['status'] for r in recs))}; "
        f"category={dict(Counter(r['category'] for r in recs))}")
