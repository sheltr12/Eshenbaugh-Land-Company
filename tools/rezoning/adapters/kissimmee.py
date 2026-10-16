#!/usr/bin/env python3
"""City of Kissimmee (Osceola County) rezoning / PUD / future land use map amendment adapter.

Sources (all public, no login):
  1. Tyler EnerGov Citizen Self Service, tenant EnerGov_Prod
     https://cityofkissimmeefl-energovweb.tylerhost.net/apps/selfservice
     Plan cases: ZMA-26-0002 "Zoning Map Amendment" -> Rezoning (PD when the target is a PUD),
     PUD-26-0003 "Planned Unit Development Review" (PUD Zoning / PUD Amendment) -> PD,
     LUPA-26-0001 "Land Use Plan Amendment - Small / Large" -> Comp Plan Amendment
     (the "Text" work class is skipped). Annexations (AX-) are not collected; their companion
     ZMA / LUPA cases are.
     The portal's typed plan search (SearchModule 3) answers HTTP 500 for this tenant, so cases
     are discovered with the global keyword search ("ZMA-26", filtered to plan records) plus
     exact-number probing of the next numbers in each series. Plan detail gives status, apply
     date, description, main parcel and address; the contacts endpoint gives the applicant.
  2. CivicClerk (kissimmeefl.api.civicclerk.com): Planning Advisory Board and City Commission
     agendas. Agenda items name the case number ("... Fortune Rd. Zoning Map Amendment -
     PUD-26-0003", attachments "PUD-26-0003 Site Data Table") and the item description gives
     acres and parcel IDs. Each meeting's "Action Summary" PDF gives the motion and result
     ("Motion to approve the Public Hearing and adopt Ordinance # 3144 Passed.").
Parcel IDs are Osceola PA parcel numbers as EnerGov shows them: "19-25-30-00U0-0050-0000".
"""
import datetime as dt
import html as htmlmod
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

JURISDICTIONS = ["City of Kissimmee"]
JUR, COUNTY = "City of Kissimmee", "Osceola"
SOURCE = "Kissimmee EnerGov CSS + CivicClerk (PAB / City Commission)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
ENERGOV = "https://cityofkissimmeefl-energovweb.tylerhost.net/apps/selfservice"
EG_API = ENERGOV + "/api/energov"
EG_HDR = {"tenantId": "1", "tenantName": "EnerGov_Prod", "Tyler-TenantUrl": "Home",
          "Content-Type": "application/json;charset=UTF-8"}
CC_API = "https://kissimmeefl.api.civicclerk.com/v1"
CC_PORTAL = "https://kissimmeefl.portal.civicclerk.com"
PREFIXES = ("ZMA", "PUD", "LUPA")
CASE_RX = re.compile(r"\b((?:ZMA|PUD|LUPA)-\d{2}-\d{4})\b")
SLEEP = 0.35
LOOKAHEAD_DAYS = 90
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


def log(*a):
    print("[kissimmee]", *a, file=sys.stderr, flush=True)


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
    s = htmlmod.unescape(re.sub(r"<[^>]+>", " ", str(s)))
    s = re.sub(r"\s+", " ", s).strip(" ,;")
    return s or None


def iso(s):
    return str(s)[:10] if s else None


def to_float(s):
    try:
        v = float(str(s).replace(",", ""))
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- EnerGov
_criteria = None


def criteria():
    global _criteria
    if _criteria is None:
        _criteria = req("GET", EG_API + "/search/criteria", headers=EG_HDR).json()["Result"]
    return json.loads(json.dumps(_criteria))


def eg_search(keyword, exact=False, page=1, size=100):
    c = criteria()
    c.update({"Keyword": keyword, "ExactMatch": exact, "SearchModule": 1, "FilterModule": 3 if not exact else 1,
              "PageNumber": page, "PageSize": size, "SortBy": "ApplyDate" if not exact else None,
              "SortAscending": False})
    res = req("POST", EG_API + "/search/search", headers=EG_HDR, json=c).json().get("Result") or {}
    return res.get("EntityResults") or [], res.get("TotalPages") or 1


def eg_by_number(num):
    rows, _ = eg_search(num, exact=True, size=20)
    for e in rows:
        if (e.get("CaseNumber") or "").upper() == num.upper() and e.get("ModuleName") == 3:
            return e
    return None


def eg_plan(case_id):
    return req("GET", f"{EG_API}/plans/{case_id}", headers=EG_HDR).json().get("Result") or {}


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
        if org and re.search(r"CITY OF KISSIMMEE", org, re.I):
            continue   # staff placeholder contact
        name = org or person
        if org and person and person.lower() not in org.lower():
            name = f"{org} ({person})"
        by.setdefault((c.get("ContactTypeName") or "").lower(), name)
    for pat in ("applicant", "agent", "contact", "owner"):
        for k, v in by.items():
            if pat in k and v:
                return v
    return next((v for v in by.values() if v), None)


def discover(since, refresh):
    """Plan cases in scope (by number), via keyword search + exact-number probing."""
    found = {}
    y0, y1 = int(since[:4]) % 100, dt.date.today().year % 100
    for pre in PREFIXES:
        for yy in range(y0, y1 + 1):
            rx = re.compile(rf"^{pre}-{yy:02d}-(\d{{4}})$")
            page, pages = 1, 1
            while page <= pages and page <= 10:
                try:
                    rows, pages = eg_search(f"{pre}-{yy:02d}", page=page)
                except Exception as e:  # noqa: BLE001
                    log(f"keyword search {pre}-{yy:02d}: {e}")
                    break
                for e in rows:
                    n = (e.get("CaseNumber") or "").upper()
                    if e.get("ModuleName") == 3 and rx.match(n):
                        found[n] = e
                page += 1
            nums = [int(rx.match(n).group(1)) for n in found if rx.match(n)]
            top = max(nums) if nums else 0
            misses = 0
            i = 1
            while i <= top + 2 or (misses < 2 and i <= top + 4):
                n = f"{pre}-{yy:02d}-{i:04d}"
                if n not in found:
                    try:
                        e = eg_by_number(n)
                    except Exception as ex:  # noqa: BLE001
                        log(f"probe {n}: {ex}")
                        e = None
                    if e:
                        found[n] = e
                        top = max(top, i)
                        misses = 0
                    elif i > top:
                        misses += 1
                i += 1
    for n in refresh:
        if n not in found:
            try:
                e = eg_by_number(n)
            except Exception as ex:  # noqa: BLE001
                log(f"refresh lookup {n}: {ex}")
                e = None
            if e:
                found[n] = e
            else:
                log(f"refresh id not found in EnerGov: {n}")
    out = {}
    for n, e in found.items():
        if n in refresh or (iso(e.get("ApplyDate")) or "9999") >= since:
            out[n] = e
    return out


# --------------------------------------------------------------------------- CivicClerk
def cc_events(start, end):
    url = f"{CC_API}/Events"
    params = {"$filter": f"startDateTime ge {start}T00:00:00Z and startDateTime lt {end}T00:00:00Z",
              "$orderby": "startDateTime"}
    out = []
    while url:
        d = req("GET", url, params=params).json()
        out.extend(d.get("value", []))
        url, params = d.get("@odata.nextLink"), None
    keep = []
    for e in out:
        name = e.get("eventName") or ""
        if re.search(r"cancel|workshop|budget", name, re.I):
            continue
        if re.search(r"Planning Advisory Board", name, re.I):
            e["_body"] = "Planning Advisory Board"
        elif re.search(r"City Commission", name, re.I):
            e["_body"] = "City Commission"
        else:
            continue
        keep.append(e)
    return keep


def walk(items):
    for it in items or []:
        yield it
        yield from walk(it.get("childItems"))


def pdf_text(content):
    rdr = pypdf.PdfReader(io.BytesIO(content))
    return "\n".join((p.extract_text() or "") for p in rdr.pages)


def parse_actions(text, body):
    """{case: (result, raw)} from a PAB / Commission action summary."""
    t = re.sub(r"[ \t]+", " ", text)
    # undo pypdf word splits inside case numbers ("PUD -26-0003")
    t = re.sub(r"\b(ZMA|PUD|LUPA)\s*-\s*(\d{2})\s*-\s*(\d{4})", r"\1-\2-\3", t)
    chunks = re.split(r"\n\s*(?=\d{1,2}\.[A-Z]\.?\s)", t)
    out = {}
    for ch in chunks:
        cases = CASE_RX.findall(ch[:700])
        if not cases:
            continue
        flat = re.sub(r"\s+", " ", ch)
        mots = []
        for m in re.finditer(r"Motion to (.{3,160}?)\s+(?:(Passed|Failed|Carried|Died)\b\s*(?:(\d+)\s*-\s*(\d+))?|"
                             r"(\d+)\s*-\s*(\d+)\b)", flat, re.I):
            word = m.group(2)
            if not word:   # "Motion to Approve 4 - 1." (outcome word missing in the source)
                ayes, nays = int(m.group(5)), int(m.group(6))
                word = "Passed" if ayes > nays else "Failed"
            mots.append((m.group(1), word))
        res = raw = None
        if re.search(r"\bwithdrawn\b", flat, re.I) and not mots:
            res, raw = "Withdrawn", "Withdrawn"
        for act, outcome in mots:
            raw = clean(f"Motion to {act} {outcome}")
            a = act.lower()
            if outcome.lower() in ("failed", "died"):
                res = f"Motion to {act} failed"
                continue
            if re.search(r"contin|table|defer|postpon", a):
                res = "Continued"
            elif re.search(r"\bden(y|ial)\b", a):
                res = "Recommended Denial" if body == "Planning Advisory Board" else "Denied"
            elif re.search(r"approv|adopt|recommend", a):
                if body == "Planning Advisory Board":
                    res = "Recommended Approval"
                elif re.search(r"first reading", a):
                    res = "Approved on First Reading"
                else:
                    res = "Approved"
            else:
                res = raw
        for c in dict.fromkeys(cases):
            if res:
                out[c] = (res, raw)
    return out


def scan_meetings(start, wanted):
    end = (dt.date.today() + dt.timedelta(days=LOOKAHEAD_DAYS)).isoformat()
    events = cc_events(start, end)
    apps = {}
    for ev in events:
        if not ev.get("agendaId"):
            continue
        try:
            mt = req("GET", f"{CC_API}/Meetings/{ev['agendaId']}").json()
        except Exception as e:  # noqa: BLE001
            log(f"meeting {ev['agendaId']}: {e}")
            continue
        date = ev["startDateTime"][:10]
        files = {f.get("type"): f.get("fileId") for f in ev.get("publishedFiles", [])}
        hits = []
        for it in walk(mt.get("items")):
            name = clean(it.get("agendaObjectItemName")) or ""
            desc = clean(it.get("agendaObjectItemDescription")) or ""
            atts = " ".join(a.get("fileName") or "" for a in it.get("attachmentsList") or [])
            cases = CASE_RX.findall(name) or CASE_RX.findall(desc) or CASE_RX.findall(atts)
            for c in dict.fromkeys(cases):
                if c not in wanted:
                    continue
                hits.append(c)
                apps.setdefault(c, []).append({
                    "date": date, "body": ev["_body"], "event_id": ev["id"], "name": name, "desc": desc,
                    "packet": files.get("Agenda Packet"), "result": None, "raw": None,
                })
        if hits and files.get("Action Summary") and date <= dt.date.today().isoformat() and pypdf:
            try:
                r = req("GET", f"{CC_API}/Meetings/GetMeetingFileStream(fileId={files['Action Summary']},"
                               f"plainText=false)")
                acts = parse_actions(pdf_text(r.content), ev["_body"]) if r.content else {}
                for c in hits:
                    if c in acts:
                        for a in apps[c]:
                            if a["event_id"] == ev["id"]:
                                a["result"], a["raw"] = acts[c]
            except Exception as e:  # noqa: BLE001
                log(f"action summary {date} {ev['_body']}: {e}")
    log(f"CivicClerk: {len(events)} PAB/Commission meetings {start}..{end}; "
        f"{sum(len(v) for v in apps.values())} agenda appearances of {len(apps)} cases")
    return apps


# --------------------------------------------------------------------------- build
def category_of(num, to_zoning):
    if num.startswith("LUPA"):
        return "Comp Plan Amendment"
    if num.startswith("PUD"):
        return "PD"
    if re.search(r"Planned Unit Development|\bPUD\b|[A-Z]PUD\b", to_zoning or ""):
        return "PD"
    return "Rezoning"


def from_to(text, kind):
    t = clean(text) or ""
    if kind == "flu":
        m = re.search(r"Future Land Use Map designation from\s+(.+?)\s+to\s+(.+?)(?::|;|\s+on approximately|$)", t, re.I)
    else:
        m = re.search(r"Zoning Map designation from\s+(.+?)\s+to\s+(.+?)(?::|;|\s+on approximately|$)", t, re.I)
    if not m:
        m = re.search(r"\b(?:rezone|change)\b.{0,40}?\bfrom\s+(.{2,60}?)\s+to\s+(.{2,60}?)(?:\.|,|;|$)", t, re.I)
    if m:
        return clean(m.group(1)), clean(m.group(2))
    return None, None


def acres_of(*texts):
    for t in texts:
        for p in (r"approximately\s+([\d,]*\.?\d+)\s*acres?", r"\+/-\s*([\d,]*\.?\d+)\s*acres?",
                  r"([\d,]*\.?\d+)\s*(?:\+/-\s*)?acres?\b"):
            m = re.search(p, t or "", re.I)
            if m and to_float(m.group(1)):
                return to_float(m.group(1))
    return None


PARCEL_RX = re.compile(r"\b\d{2}-\d{2}-\d{2}-[0-9A-Z]{4}-[0-9A-Z]{4}-[0-9A-Z]{4}\b")


def build(num, e, plan, apps, today):
    apps = sorted(apps, key=lambda a: a["date"])
    atext = " ".join(f"{a['name']} {a['desc']}" for a in apps)
    wc = plan.get("WorkClassName") or e.get("CaseWorkclass") or ""
    if num.startswith("LUPA") and re.search(r"text", wc, re.I):
        return None
    desc_eg = clean(plan.get("Description") or e.get("Description"))
    if desc_eg and re.fullmatch(r"te?s+z?t\w*", desc_eg, re.I):
        return None   # staff test record
    is_cpa = num.startswith("LUPA")
    cat = "Comp Plan Amendment" if is_cpa else None

    fz = tz = ff = tf = None
    for a in reversed(apps):
        if is_cpa:
            ff, tf = from_to(a["name"], "flu")
        else:
            fz, tz = from_to(a["name"], "zoning")
        if fz or tz or ff or tf:
            break
    if not (fz or tz or ff or tf) and desc_eg:
        x, y = from_to(desc_eg, "zoning")
        if is_cpa:
            ff, tf = x, y
        else:
            fz, tz = x, y
    cat = category_of(num, tz)

    parcels = []
    for p in [plan.get("MainParcelNumber"), e.get("MainParcel")] + PARCEL_RX.findall(atext) + \
            PARCEL_RX.findall(desc_eg or ""):
        p = (p or "").strip()
        if p and p not in parcels:
            parcels.append(p)
    acres = acres_of(*[a["desc"] for a in reversed(apps)], desc_eg)

    # hearings
    hmap = {}
    for a in apps:
        k = (a["body"], a["date"])
        if k not in hmap or (a["result"] and not hmap[k]["result"]):
            hmap[k] = {"body": a["body"], "date": a["date"], "result": a["result"],
                       "_final": bool(re.search(r"final reading|second (?:and final )?reading|adopt", a["name"] + " " +
                                                (a["raw"] or ""), re.I))}
    hearings = sorted(hmap.values(), key=lambda h: (h["date"], h["body"] != "Planning Advisory Board"))

    raw = clean(plan.get("PlanStatus") or e.get("CaseStatus"))
    r = (raw or "").lower()
    status, decision = None, None
    final = [h for h in hearings if h["body"] == "City Commission" and h["result"] in ("Approved", "Denied")]
    if re.search(r"withdr|void|expired|cancel", r):
        status = "Withdrawn"
    elif re.search(r"denied", r) and "commission" in r:
        status = "Denied"
    elif re.search(r"final.*approved|commission.*approved", r):
        status = "Approved"
    elif any(h["result"] == "Withdrawn" for h in hearings):
        status = "Withdrawn"
    elif final:
        status = final[-1]["result"]
    if status in ("Approved", "Denied"):
        decision = final[-1]["date"] if final else None
    if status is None:
        future = [h for h in hearings if h["date"] > today]
        past = [h for h in hearings if h["date"] <= today]
        if future:
            status = "Hearing Scheduled"
        elif past and past[-1]["result"] == "Continued":
            status = "Continued"
        elif past and past[-1]["result"] is None:
            status = "Hearing Scheduled"   # held; outcome not yet in a published action summary
        elif past and past[-1]["body"] == "Planning Advisory Board":
            status = "Filed"               # PAB done, Commission hearing not yet set
        elif past:
            status = "Hearing Scheduled" if past[-1]["result"] == "Approved on First Reading" else "Filed"
        else:
            status = "Filed"
    for h in hearings:
        h.pop("_final", None)

    title = None
    if apps:
        m = re.search(r"\):\s*(.+?)\s*(?:-|\u2014|\u2013)\s*(?:ZMA|PUD|LUPA)-", apps[-1]["name"])
        title = clean(m.group(1)) if m else None
    desc = desc_eg
    if apps and apps[-1]["desc"]:
        desc = apps[-1]["desc"] if not desc or len(desc) < 40 else desc
    if desc and len(desc) > 400:
        desc = desc[:397].rstrip() + "..."
    latest = apps[-1] if apps else None
    docs = (f"{CC_API}/Meetings/GetMeetingFileStream(fileId={latest['packet']},plainText=false)"
            if latest and latest.get("packet") else None)
    addr = clean((plan.get("MainAddress") or "").replace("\r\n", ", ")) or clean(e.get("AddressDisplay"))
    return {
        "jurisdiction": JUR, "county": COUNTY, "app_num": num, "category": cat,
        "app_type": clean(plan.get("PlanType") or e.get("CaseType")),
        "project_name": clean(plan.get("ProjectName") or e.get("ProjectName")) or title,
        "description": desc, "applicant": eg_applicant(e["CaseId"]),
        "parcel_ids": parcels, "address": addr, "acres": acres,
        "from_zoning": fz, "to_zoning": tz, "from_flu": ff, "to_flu": tf,
        "filed_date": iso(plan.get("ApplyDate") or e.get("ApplyDate")),
        "status": status, "status_raw": raw,
        "hearings": hearings, "decision_date": decision,
        "url": f"{ENERGOV}#/plan/{e['CaseId']}", "docs_url": docs,
        "lat": None, "lon": None, "source": SOURCE,
    }


def fetch(since, refresh):
    t0 = time.time()
    today = dt.date.today().isoformat()
    refresh = [r.strip().upper() for r in (refresh or []) if r and r.strip()]
    try:
        criteria()
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Kissimmee EnerGov unreachable: {e}") from e
    cases = discover(since, refresh)
    log(f"{len(cases)} candidate EnerGov plan cases: {sorted(cases)}")
    if not cases:
        return []
    plans = {}
    for n, e in cases.items():
        try:
            plans[n] = eg_plan(e["CaseId"])
        except Exception as ex:  # noqa: BLE001
            log(f"{n}: plan detail failed: {ex}")
            plans[n] = {}
    start = min([iso(p.get("ApplyDate") or cases[n].get("ApplyDate")) or since for n, p in plans.items()] + [since])
    try:
        apps = scan_meetings(start, set(cases))
    except Exception as e:  # noqa: BLE001
        log(f"CivicClerk unavailable ({e}); hearings left empty")
        apps = {}
    out = []
    for n in sorted(cases):
        try:
            rec = build(n, cases[n], plans.get(n) or {}, apps.get(n, []), today)
        except Exception as e:  # noqa: BLE001
            log(f"{n}: skipped ({type(e).__name__}: {e})")
            continue
        if rec:
            out.append(rec)
    log(f"{len(out)} records in {time.time() - t0:.0f}s")
    return out


if __name__ == "__main__":
    since = sys.argv[1] if len(sys.argv) > 1 else (dt.date.today() - dt.timedelta(days=14)).isoformat()
    refresh = sys.argv[2].split(",") if len(sys.argv) > 2 and sys.argv[2] else []
    t0 = time.time()
    recs = fetch(since, refresh)
    print(json.dumps(recs, indent=2, ensure_ascii=False))
    from collections import Counter
    log(f"{len(recs)} records in {time.time() - t0:.0f}s; status={dict(Counter(r['status'] for r in recs))}; "
        f"category={dict(Counter(r['category'] for r in recs))}")
