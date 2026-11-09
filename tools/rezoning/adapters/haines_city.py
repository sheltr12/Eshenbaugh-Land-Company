#!/usr/bin/env python3
"""City of Haines City (Polk County) rezoning / PUD / future land use map amendment adapter.

Source: the city's eScribe meeting portal https://pub-hainescity.escribemeetings.com
 * meeting list: POST /MeetingsCalendarView.aspx/GetCalendarMeetings (JSON, date range).
 * Planning Commission and City Commission agendas are published as HTML pages
   (Meeting.aspx?Id=...&Agenda=Agenda / PostAgenda / PostMinutes). Each agenda item has a title
   ("Ordinance No. 26-2148 - Small Scale Land Use Amendment for Crossroads Townhomes - Second
   Reading", "Zoning Map Amendment - Direct Countertops"), a description and the recommended
   motion ("... small-scale land use amendment of 4.92+/-acres for Crossroads Townhomes, from
   County RL-4 to City Medium Density Residential (MDR)"). Post-meeting pages add the motion
   result ("Carried", "Carried Unanimously", "Defeated").
 * Attachments (staff reports, ordinances) sit behind eScribe's browser-verification page, so
   they are not read: parcel ids are not available from this source (the address is).
Case numbers: the city publishes no application number; land use cases are numbered by
ordinance ("Ordinance No. 26-2148"), which the City Commission items always carry and Planning
Commission items sometimes do. app_num = "Ordinance No. 26-2148" when the case's first agenda
appearance shows the ordinance number, otherwise "<project> (<request type>)" from that first
appearance (e.g. "Direct Countertops (Small Scale Land Use Plan Amendment)"); later appearances
are linked by ordinance number or project name, so the key does not change once issued.
Annexations, land development regulation text amendments, plats and special uses are skipped.
"""
import datetime as dt
import html as _html
import json
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

JURISDICTIONS = ["City of Haines City"]
JUR = "City of Haines City"
BASE = "https://pub-hainescity.escribemeetings.com"
SOURCE = "Haines City eScribe (Planning Commission + City Commission agendas and minutes)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
LOOKBACK_DAYS = 365      # scan this far before `since` so a case's first appearance (and key) is stable
PC = "Planning Commission"
CC = "City Commission"
TYPES = {"Planning Commission": PC, "City Commission Meeting": CC, "City Commission Special Meeting": CC}

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


def log(*a):
    print("[haines_city]", *a, file=sys.stderr)


def req(method, url, tries=4, sleep=0.5, **kw):
    last = None
    for i in range(tries):
        try:
            r = _s.request(method, url, timeout=90, **kw)
            r.raise_for_status()
            time.sleep(sleep)
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"{method} failed {url}: {last}")


def sq(s):
    s = re.sub(r"\s+", " ", _html.unescape(s or "").replace("\u00a0", " ")).strip()
    return s or None


def nd(s):
    return (s or "").replace("\u2013", "-").replace("\u2014", "-").replace("\u2012", "-")


# ------------------------------------------------------------------ eScribe
def list_meetings(start, end):
    d = req("POST", BASE + "/MeetingsCalendarView.aspx/GetCalendarMeetings",
            json={"calendarStartDate": f"{start}T00:00:00", "calendarEndDate": f"{end}T00:00:00"}).json()["d"]
    out = []
    for m in d:
        body = TYPES.get(m.get("MeetingType") or "")
        if not body:
            continue
        docs = [x for x in m.get("MeetingDocumentLink") or [] if x.get("Format") == "HTML"]
        pick = None
        for pref in ("PostMinutes", "PostAgenda", "Agenda=Merged", "Agenda=Agenda"):
            c = [x for x in docs if pref in (x.get("Url") or "")]
            if c:
                pick = c[0]["Url"]
                break
        if not pick:
            continue
        date = (m.get("StartDate") or "")[:10].replace("/", "-")
        agenda_url = re.sub(r"Agenda=\w+", "Agenda=Agenda", pick)
        out.append({"id": m["ID"], "date": date, "body": body, "url": BASE + "/" + pick.lstrip("/"),
                    "link": BASE + "/" + agenda_url.lstrip("/")})
    return sorted(out, key=lambda x: x["date"])


def meeting_items(url):
    soup = BeautifulSoup(req("GET", url).text, "lxml")
    out = []
    for it in soup.select("div.AgendaItem"):
        ti = it.select_one(".AgendaItemTitle")
        if not ti:
            continue
        c = it.select_one(".AgendaItemCounter")
        desc = " ".join(x.get_text(" ", strip=True) for x in it.select(".AgendaItemDescription"))
        mins = " ".join(x.get_text(" ", strip=True) for x in it.select(".AgendaItemMinutes"))
        motions = " ".join(x.get_text(" ", strip=True) for x in it.select(".MotionText"))
        results = [x.get_text(" ", strip=True) for x in it.select(".MotionResult")]
        out.append({"no": c.get_text(strip=True) if c else None, "title": nd(sq(ti.get_text(" ", strip=True))),
                    "desc": nd(sq(desc)), "minutes": nd(sq(mins)), "motion": nd(sq(motions)),
                    "result": next((r for r in results if r), None)})
    return out


# ------------------------------------------------------------------ classification
IN_RE = re.compile(r"Land Use (?:Plan )?(?:Map )?Amendment|Comprehensive Plan (?:Map )?Amendment|Zoning Map Amendment|"
                   r"Rezon|\bPUD\b|Planned (?:Unit )?Development|Zoning Amendment|Future Land Use", re.I)
OUT_RE = re.compile(r"Text Amendment|Land Development Regulation|Code Review|\bLDR\b|Code of Ordinances|Annexation|"
                    r"Special Use|Variance|Plat\b|Site Plan|Interim Agreement|Development Agreement", re.I)
ORD_RE = re.compile(r"Ordinance\s*(?:No\.?)?\s*(\d{2})\s*-\s*(\d{3,5})", re.I)


def classify(title):
    """(kind, type_label, project) for an in-scope item title or None. kind CPA / ZONING."""
    t = title or ""
    if not IN_RE.search(t):
        return None
    if OUT_RE.search(t) and not re.search(r"Land Use (?:Plan )?Amendment|Zoning Map Amendment", t, re.I):
        return None
    if re.search(r"Land Use|Comprehensive Plan", t, re.I):
        kind = "CPA"
        m = re.search(r"((?:Small|Large)[- ]Scale\s+)?(?:Future\s+)?Land Use (?:Plan )?(?:Map )?Amendment|"
                      r"Comprehensive Plan (?:Map )?Amendment", t, re.I)
        label = sq(m.group(0)) if m else "Land Use Amendment"
    else:
        kind = "ZONING"
        m = re.search(r"Zoning Map Amendment|Zoning Amendment|Rezoning|PUD[^-]*|Planned (?:Unit )?Development", t, re.I)
        label = sq(m.group(0)) if m else "Zoning Map Amendment"
    proj = project_of(t)
    return kind, label, proj


def project_of(t):
    s = ORD_RE.sub(" ", t or "")
    s = re.sub(r"\s*-\s*(?:First|Second|1st|2nd|Final)\s+Read(?:ing)?\s*$", "", s, flags=re.I)
    s = re.sub(r"\b(?:First|Second|1st|2nd|Final)\s+Read(?:ing)?\s+of\s+", "", s, flags=re.I)
    m = re.search(r"\bfor\s+(.+)$", s, re.I)
    if m:
        s = m.group(1)
    else:
        parts = [p for p in re.split(r"\s+-\s+", s) if p.strip()]
        cand = [p for p in parts if not IN_RE.search(p)]
        s = cand[-1] if cand else (parts[-1] if parts else s)
    s = re.sub(r"^(?:the|property located at)\s+", "", s.strip(" -:"), flags=re.I)
    return sq(s.strip(" -:,"))


def pkey(p):
    p = (p or "").lower()
    p = re.sub(r"\b(townhomes|cottages|subdivision|property|project|the)\b", " ", p)
    return re.sub(r"[^a-z0-9]+", " ", p).strip()


def reading_of(title):
    if re.search(r"Second\s+Read|2nd\s+Read|Final\s+Read", title or "", re.I):
        return "second"
    if re.search(r"First\s+Read|1st\s+Read", title or "", re.I):
        return "first"
    return None


def norm_result(res, body, reading):
    if not res:
        return None
    w = res.lower()
    if "withdr" in w:
        return "Withdrawn"
    if "defeat" in w or "fail" in w or "deni" in w:
        return "Denied" if body == CC else "Recommended Denial"
    if "tabl" in w or "postpon" in w or "continu" in w or "defer" in w:
        return "Continued"
    if "carried" in w or "approv" in w or "pass" in w:
        if body == PC:
            return "Recommended Approval"
        return "Approved" if reading == "second" else "Approved (first reading)"
    return sq(res)


# ------------------------------------------------------------------ fetch
def fetch(since, refresh):
    today = dt.date.today()
    refresh_ids = {r.strip(): r for r in (refresh or []) if r and r.strip()}
    start = (dt.date.fromisoformat(since) - dt.timedelta(days=LOOKBACK_DAYS)).isoformat()
    end = (today + dt.timedelta(days=90)).isoformat()
    meetings = list_meetings(start, end)
    if not meetings:
        raise RuntimeError("no Haines City meetings returned by eScribe")
    log(f"{len(meetings)} Planning/City Commission meetings {start}..{end}")

    apps = []
    for m in meetings:
        try:
            items = meeting_items(m["url"])
        except Exception as e:  # noqa: BLE001
            log(f"meeting {m['date']} {m['body']}: {e}")
            continue
        for it in items:
            c = classify(it["title"])
            if not c:
                continue
            kind, label, proj = c
            om = ORD_RE.search(it["title"]) or ORD_RE.search(it["desc"] or "") or ORD_RE.search(it["motion"] or "")
            ono = f"{om.group(1)}-{om.group(2)}" if om else None
            apps.append({**it, "date": m["date"], "body": m["body"], "url": m["link"], "kind": kind,
                         "label": label, "proj": proj, "pk": pkey(proj), "ord": ono,
                         "reading": reading_of(it["title"])})
    apps.sort(key=lambda a: (a["date"], a["body"] != PC))

    # group appearances into cases
    cases = []
    by_ord = {}
    for a in apps:
        case = by_ord.get(a["ord"]) if a["ord"] else None
        if case is None:
            cands = [c for c in cases if c["kind"] == a["kind"] and a["pk"] and a["pk"] in c["pks"]
                     and (not a["ord"] or not c["ords"] or a["ord"] in c["ords"])]
            case = cands[-1] if cands else None
        if case is None:
            key = f"Ordinance No. {a['ord']}" if a["ord"] else f"{a['proj']} ({a['label']})"
            case = {"key": key, "kind": a["kind"], "apps": [], "pks": set(), "ords": set()}
            cases.append(case)
        case["apps"].append(a)
        if a["pk"]:
            case["pks"].add(a["pk"])
        if a["ord"]:
            case["ords"].add(a["ord"])
            by_ord[a["ord"]] = case
    log(f"{len(apps)} in-scope agenda items -> {len(cases)} cases")

    out, keys = [], set()
    for c in cases:
        first = min(a["date"] for a in c["apps"])
        if first < since and c["key"] not in refresh_ids:
            continue
        if c["key"] in keys:
            continue
        keys.add(c["key"])
        try:
            out.append(build(c, today.isoformat()))
        except Exception as e:  # noqa: BLE001
            log(f"{c['key']}: skipped ({e})")
    for r in set(refresh_ids) - {c["key"] for c in cases}:
        log(f"refresh id {r} not found on agendas since {start}")
    return out


def num(s):
    try:
        v = float(s.replace(",", ""))
        return v if 0 < v < 100000 else None
    except (ValueError, AttributeError):
        return None


def build(c, today):
    apps = sorted(c["apps"], key=lambda a: a["date"])
    text = " ".join(filter(None, [a["motion"] for a in apps] + [a["desc"] for a in apps]))
    hearings = []
    cc_seen = 0
    for a in apps:
        reading = a["reading"]
        if a["body"] == CC:
            if not reading:
                reading = "second" if cc_seen else "first"
            cc_seen += 1
            body = f"{CC} ({'second reading' if reading == 'second' else 'first reading'})"
        else:
            body = PC
        h = {"body": body, "date": a["date"], "result": norm_result(a["result"], a["body"], reading)}
        if not any(x["body"] == h["body"] and x["date"] == h["date"] for x in hearings):
            hearings.append(h)

    status, raw, decision = None, None, None
    past = [h for h in hearings if h["date"] <= today]
    future = [h for h in hearings if h["date"] > today]
    final = [h for h in hearings if h["body"].startswith(CC) and h["result"] in ("Approved", "Denied")]
    if any(h["result"] == "Withdrawn" for h in hearings):
        status, raw = "Withdrawn", "Withdrawn"
    elif final:
        status, decision = final[-1]["result"], final[-1]["date"]
        raw = f"{final[-1]['body']} {decision}: {final[-1]['result']}"
    elif future:
        status, raw = "Hearing Scheduled", f"{future[0]['body']} {future[0]['date']}"
    elif past and past[-1]["result"] == "Continued":
        status, raw = "Continued", f"{past[-1]['body']} {past[-1]['date']}: continued"
    elif past:
        lp = past[-1]
        if lp["body"].startswith(CC) and lp["result"] is None:
            status = "Hearing Scheduled"
            raw = f"{lp['body']} {lp['date']} held; outcome not yet published"
        else:
            status = "Filed"
            raw = f"{lp['body']} {lp['date']}" + (f": {lp['result']}" if lp["result"] else "") + \
                "; next hearing not yet on an agenda"
    else:
        status, raw = "Filed", "On agenda"

    acres = None
    m = re.search(r"([\d,]*\.?\d+)\s*\+?\s*/?\s*-?\s*acres", text, re.I)
    if m:
        acres = num(m.group(1))
    frm = to = None
    m = re.search(r"\bfrom\s+(.+?)\s+to\s+(.+?)(?:\.\s|\.$|$|;)", text)
    if m:
        frm, to = sq(m.group(1)), sq(m.group(2))
        to = re.split(r"\s+(?:in the|located|for)\s", to)[0].strip(" .,")
        to = re.sub(r"^City (?:Future Land Use )?Designation of\s+", "", to, flags=re.I)
        frm = re.sub(r"^County Land Use Designation$", "County designation", frm)
    elif re.search(r"\bto\s+(?:City\s+)?([A-Z][\w /()-]{2,60})", text):
        m2 = re.search(r"(?:Designation of|designation of|to)\s+((?:City\s+)?[A-Z][\w /()-]{2,60}?\([A-Z0-9-]+\))", text)
        to = sq(m2.group(1)) if m2 else None
    addr = None
    m = re.search(r"located at\s+(.+?)(?:\.|,| known as| on first| on second|$)", text, re.I)
    if m:
        addr = sq(m.group(1))
    elif re.match(r"^\d+\s+\w", c["apps"][0]["proj"] or ""):
        addr = c["apps"][0]["proj"]
    kind = c["kind"]
    labels = " ".join(a["title"] for a in apps)
    if kind == "CPA":
        category = "Comp Plan Amendment"
        app_type = "Large Scale Land Use Amendment" if re.search(r"Large", labels, re.I) else "Small Scale Land Use Amendment"
    else:
        is_pd = bool(re.search(r"\bPUD\b|Planned (?:Unit )?Development", labels + " " + (to or ""), re.I))
        category = "PD" if is_pd else "Rezoning"
        app_type = "PUD Rezoning" if is_pd else "Zoning Map Amendment"
    ords = sorted(c["ords"])
    desc = next((a["desc"] for a in apps if a["desc"]), None) or next((a["motion"] for a in apps if a["motion"]), None)
    if ords and not c["key"].startswith("Ordinance"):
        desc = (desc or "") + f" (Ordinance No. {', '.join(ords)})"
    desc = re.sub(r"\s*Staff contact:.*$", "", desc or "", flags=re.I).strip() or None
    if desc and len(desc) > 500:
        desc = desc[:497] + "..."
    latest = apps[-1]
    return {
        "jurisdiction": JUR, "county": "Polk", "app_num": c["key"], "category": category, "app_type": app_type,
        "project_name": apps[-1]["proj"], "description": desc, "applicant": None, "parcel_ids": [],
        "address": addr, "acres": acres,
        "from_zoning": frm if kind == "ZONING" else None, "to_zoning": to if kind == "ZONING" else None,
        "from_flu": frm if kind == "CPA" else None, "to_flu": to if kind == "CPA" else None,
        "filed_date": apps[0]["date"], "status": status, "status_raw": raw, "hearings": hearings,
        "decision_date": decision, "url": latest["url"], "docs_url": None, "lat": None, "lon": None,
        "source": SOURCE,
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
