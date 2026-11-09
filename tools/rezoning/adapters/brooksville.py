#!/usr/bin/env python3
"""City of Brooksville (Hernando County) rezoning / PD / future land use map amendment adapter.

Source: the city's Destiny AgendaQuick site (destinyhosted.com/agenda_publish.cfm?id=27665), which
publishes HTML agendas, item memoranda and HTML minutes for City Council, Local Planning Agency and
Planning & Zoning Commission meetings (the city's CivicPlus AgendaCenter only carries the advisory
boards).
 * Land use petitions are ordinances with a city petition number:
   "Ordinance Number 1016-RZ 2026-01-Mike Ung on behalf of Freedom Capital Alliance, LLC Requesting
   Rezoning from C-2 to Planned Development Project-Commercial (PDP-C)",
   "Ordinance No. 1011-CPA 2026-01-S1-Coastal Waste ... Comprehensive Plan Future Land Use Map".
   app_num = the petition number ("RZ 2026-01", "CPA 2026-01-S1"); ordinances without one are kept
   as "Ordinance No. 1016". Kept: RZ, CPA (map), PDP / PUD / PD. Skipped: annexation, LDC text
   amendments, fee ordinances, special exceptions, plats.
 * Hearings: Planning & Zoning Commission (sitting as LPA, advisory), City Council first and second
   ("Second and Final") reading. Outcomes come from the HTML minutes ("Motion made by ... to approve
   on Second and Final Reading - Ordinance Number 1016 ... Vote: 5 - 0").
 * Details (Hernando County Property Appraiser key numbers, acreage, location) come from the item
   memorandum / ordinance headnote ("BEARING HERNANDO COUNTY PROPERTY APPRAISER'S KEY NUMBER 858717").
"""
import datetime as dt
import html as _html
import json
import re
import sys
import time

import requests

JURISDICTIONS = ["City of Brooksville"]
JUR = "City of Brooksville"
COUNTY = "Hernando"
HOST = "https://destinyhosted.com"
PUB = f"{HOST}/agenda_publish.cfm"
SITE_ID = 27665
SOURCE = "Brooksville Destiny AgendaQuick (P&Z Commission + City Council agendas, memos and minutes)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
LOOKBACK_DAYS = 150
PZ, CC = "Planning & Zoning Commission", "City Council"
SKIP_TYPE = re.compile(r"Workshop|Redevelopment|Pension|Budget|Canvass", re.I)

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


def log(*a):
    print("[brooksville]", *a, file=sys.stderr)


def get(url, params=None, tries=4, sleep=0.4, **kw):
    last = None
    for i in range(tries):
        try:
            r = _s.get(url, params=params, timeout=90, **kw)
            r.raise_for_status()
            time.sleep(sleep)
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


def sq(s):
    return re.sub(r"\s+", " ", s or "").strip()


def text_of(h):
    h = re.sub(r"<script.*?</script>|<style.*?</style>", " ", h or "", flags=re.S)
    t = _html.unescape(re.sub(r"<[^>]+>", " ", h)).replace("\u00a0", " ")
    return sq(t.replace("\u2013", "-").replace("\u2014", "-").replace("\u2019", "'"))


# ------------------------------------------------------------------ Destiny
def month_meetings(year, month):
    h = get(PUB, params={"id": SITE_ID, "mt": "ALL", "get_month": month, "get_year": year}).text
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", h, re.S):
        a = re.search(r'href="([^"]*dsp=ag&(?:amp;)?seq=(\d+))"[^>]*title="View Agenda for ([^"(]+)\((\d{2})/(\d{2})/(\d{4})\)"',
                      _html.unescape(row))
        if not a:
            continue
        mtype = sq(a.group(3))
        d = f"{a.group(6)}-{a.group(4)}-{a.group(5)}"
        mins = re.search(r'dsp=min&(?:amp;)?seq=(\d+)', _html.unescape(row))
        out.append({"date": d, "type": mtype, "seq": a.group(2), "year": year, "month": month,
                    "minutes": bool(mins)})
    return out


def body_of(mtype):
    if SKIP_TYPE.search(mtype):
        return None
    if re.search(r"Planning|Local Planning Agency", mtype, re.I):
        return PZ
    if re.search(r"City Council", mtype, re.I):
        return CC
    return None


def meeting_url(mt, dsp="ag"):
    return f"{PUB}?id={SITE_ID}&mt=ALL&get_month={mt['month']}&get_year={mt['year']}&dsp={dsp}&seq={mt['seq']}"


def agenda_items(mt):
    h = _html.unescape(get(meeting_url(mt)).text)
    out = []
    for u, t in re.findall(r'href="([^"]*dsp=agm[^"]*)"[^>]*>(.*?)</a>', h, re.S):
        out.append({"title": text_of(t), "url": HOST + u if u.startswith("/") else f"{HOST}/{u}"})
    return out


# ------------------------------------------------------------------ classification
CASE_RE = re.compile(r"\b(RZ|CPA|LSCPA|SSCPA|PDP|PUD|PD|ZA|ZMA)\s?-?\s?(\d{4})\s?-\s?(\d{1,3})((?:-[A-Z]{1,2}\d{0,2})?)\b")
ORD_RE = re.compile(r"\bOrdinance\s*(?:Number|No\.?|#)?\s*(\d{3,4})\b", re.I)
IN_RE = re.compile(r"Rezon|Zoning Map|Future Land Use Map|Comprehensive Plan Amendment|Planned Development|"
                   r"\bPDP-?[A-Z]*\b|\bPUD\b|Master Plan Revision", re.I)
OUT_RE = re.compile(r"Annexation(?! and)|Land Development Code|text amendment|fees?\b|Fireworks|Appointment|"
                    r"Letter of|Special Exception|Notice of Public Hearing|Correspondence|Redevelopment Plan|"
                    r"Resolution 20\d\d", re.I)


def case_of(title):
    m = CASE_RE.search(title)
    if m:
        return f"{m.group(1)} {m.group(2)}-{int(m.group(3)):02d}{m.group(4) or ''}", m.group(1)
    return None, None


def kind_of(title, pre):
    if pre in ("CPA", "LSCPA", "SSCPA") or re.search(r"Future Land Use Map|Comprehensive Plan Amendment", title, re.I):
        if re.search(r"text", title, re.I) and not re.search(r"Map", title, re.I):
            return None
        return "CPA"
    if pre or re.search(r"Rezon|Zoning Map|Planned Development|\bPDP|\bPUD\b|Master Plan Revision", title, re.I):
        return "RZ"
    return None


def reading_of(title):
    t = title.lower()
    if re.search(r"second|final reading|2nd", t):
        return "second"
    if re.search(r"first reading|1st reading", t):
        return "first"
    if re.search(r"transmittal", t):
        return "transmittal"
    return None


_minutes = {}


def minutes_text(mt):
    k = mt["seq"]
    if k not in _minutes:
        try:
            _minutes[k] = text_of(get(meeting_url(mt, "min")).text)
        except Exception as e:  # noqa: BLE001
            log(f"minutes {mt['date']}: {e}")
            _minutes[k] = ""
    return _minutes[k]


def minutes_result(text, pats):
    for p in pats:
        if not p:
            continue
        for m in re.finditer(p, text or "", re.I):
            seg = text[m.end(): m.end() + 9000]
            # stop at the next numbered agenda item that names another ordinance / petition
            nxt = re.search(r"\s(?:\d{1,2}|[A-Z])\.\s+(?:First|Second|Ordinance|Resolution|\*\*|Quasi|[A-Z]{4,})", seg[10:])
            if nxt:
                seg = seg[:10 + nxt.start()]
            mm = None
            for mm in re.finditer(r"Motion (?:was )?made by.{0,250}?to (.{0,160}?)(?:Ordinance|petition|request|the|as|with|$)"
                                  r".{0,4000}?Vote:\s*(\d)\s*-\s*(\d)", seg, re.I | re.S):
                pass
            if not mm:
                if re.search(r"\bwithdr[ae]w", seg[:3000], re.I):
                    return "Withdrawn"
                continue
            what = mm.group(1).lower()
            yes, no = int(mm.group(2)), int(mm.group(3))
            passed = yes > no
            if re.search(r"continu|table|postpone|defer", what):
                return "Continued" if passed else None
            if re.search(r"\bden(y|ial)", what):
                return "Denied" if passed else None
            return "Approved" if passed else "Denied"
    return None


# ------------------------------------------------------------------ fetch
def fetch(since, refresh):
    today = dt.date.today()
    refresh_ids = {sq(r).upper(): r for r in (refresh or []) if r and sq(r)}
    start_d = dt.date.fromisoformat(since) - dt.timedelta(days=LOOKBACK_DAYS)
    if refresh_ids:
        yrs = [int(m.group(1)) for r in refresh_ids for m in [re.search(r"(20\d\d)-", r)] if m]
        start_d = min(start_d, max(dt.date(min(yrs), 1, 1) if yrs else start_d, today - dt.timedelta(days=730)))
    end_d = today + dt.timedelta(days=75)
    months, d = [], dt.date(start_d.year, start_d.month, 1)
    while d <= end_d:
        months.append((d.year, d.month))
        d = dt.date(d.year + (d.month // 12), d.month % 12 + 1, 1)
    meetings = []
    for y, mth in months:
        try:
            meetings += month_meetings(y, mth)
        except Exception as e:  # noqa: BLE001
            if not meetings and (y, mth) == months[0]:
                raise RuntimeError(f"Brooksville Destiny agenda site unreachable: {e}")
            log(f"month {y}-{mth}: {e}")
    meetings = [m for m in meetings if body_of(m["type"]) and start_d.isoformat() <= m["date"] <= end_d.isoformat()]
    meetings.sort(key=lambda m: m["date"])
    log(f"{len(meetings)} P&Z / Council meetings {start_d}..{end_d}")

    cases = {}
    for mt in meetings:
        body = body_of(mt["type"])
        try:
            items = agenda_items(mt)
        except Exception as e:  # noqa: BLE001
            log(f"agenda {mt['date']}: {e}")
            continue
        for it in items:
            t = it["title"]
            if OUT_RE.search(t) and not CASE_RE.search(t):
                continue
            key, pre = case_of(t)
            om = ORD_RE.search(t)
            if not key and not om:
                continue
            if not key and not IN_RE.search(t):
                continue
            kind = kind_of(t, pre)
            if not kind:
                continue
            key = key or f"Ordinance No. {om.group(1)}"
            c = cases.setdefault(key, {"kind": kind, "apps": [], "ono": om.group(1) if om else None})
            c["ono"] = c["ono"] or (om.group(1) if om else None)
            c["apps"].append({"date": mt["date"], "body": body, "title": t, "url": it["url"], "mt": mt,
                              "reading": reading_of(t) if body == CC else None})
    log(f"{len(cases)} in-scope petitions")

    out = []
    for key, c in sorted(cases.items()):
        apps = sorted(c["apps"], key=lambda a: (a["date"], a["body"] != PZ))
        first = apps[0]
        older = first["body"] == CC and first["reading"] == "second" and \
            first["date"] < (start_d + dt.timedelta(days=45)).isoformat()
        if (first["date"] < since or older) and key.upper() not in refresh_ids:
            continue
        try:
            out.append(build(key, c, apps, today.isoformat()))
        except Exception as e:  # noqa: BLE001
            log(f"{key}: skipped ({e})")
    for r in set(refresh_ids) - {k.upper() for k in cases}:
        log(f"refresh id {refresh_ids[r]} not on any agenda since {start_d}")
    return out


def build(key, c, apps, today):
    kind = c["kind"]
    title = max((a["title"] for a in apps), key=len)
    memo, keys, memos = "", [], []
    for a in reversed(apps):
        try:
            t = text_of(get(a["url"]).text)
        except Exception as e:  # noqa: BLE001
            log(f"{key}: memo {e}")
            continue
        memos.append(t)
        for m in re.finditer(r"KEY (?:NUMBER|NO\.?|#)S?\s*((?:\d{3,8}(?:\s*,\s*|\s+AND\s+|\s*&\s*)?)+)", t, re.I):
            keys += re.findall(r"\d{3,8}", m.group(1))
        if keys:
            break
    memo = " ".join(memos)
    if not keys:   # the ordinance headnote read into the minutes names the PA key numbers
        kp = re.escape(key).replace(r"\ ", r"\s?-?\s?") if not key.startswith("Ordinance") else \
            rf"Ordinance\s*(?:Number|No\.?)?\s*{c.get('ono')}\b"
        for a in apps:
            if not a["mt"]["minutes"] or a["date"] > today:
                continue
            mtxt = minutes_text(a["mt"])
            for m in re.finditer(kp, mtxt, re.I):
                seg = mtxt[m.end(): m.end() + 2500]
                for km in re.finditer(r"KEY (?:NUMBER|NO\.?|#)S?\s*((?:\d{3,8}(?:\s*,\s*|\s+AND\s+|\s*&\s*)?)+)", seg, re.I):
                    keys += re.findall(r"\d{3,8}", km.group(1))
                if keys:
                    break
            if keys:
                break
    keys = list(dict.fromkeys(keys))
    parcels = re.findall(r"\bR\d{2}\s\d{3}\s\d{2}\s[0-9A-Z]{4}\s[0-9A-Z]{4}\s[0-9A-Z]{4}\b", memo)
    pids = list(dict.fromkeys(parcels + keys))
    acres = None
    m = re.search(r"approximately\s*([\d,]*\.?\d+)\s*(?:\+/-\s*)?acres", memo, re.I) or \
        re.search(r"([\d,]*\.?\d+)\s*(?:\+/-|±)?\s*acres", memo, re.I)
    if m:
        try:
            v = float(m.group(1).replace(",", ""))
            acres = v if 0 < v < 50000 else None
        except ValueError:
            pass
    address = None
    m = re.search(r"(?:is |and )?located (?:on|at|along) (the [a-z]+ side of .+?|\d{2,6} .+?)(?:,? (?:in the City|and is|the subject)|;|(?<!Dr)(?<!Jr)(?<!St)(?<!Ave)\.\s)", memo, re.I)
    if m:
        address = sq(m.group(1))[:160]
    applicant = None
    m = re.search(r"(?:RZ|CPA|PDP|PUD|PD|ZA|ZMA)\s?\d{4}-\d{1,3}(?:-[A-Z]{1,2}\d{0,2})?\s*-\s*(.+?)\s+Requesting", title)
    if m:
        applicant = sq(m.group(1))
    f_ = t_ = None
    m = re.search(r"from (?:Zoning District Classification )?(.+?) to (.+?)(?:\s*\[|;|$)", re.sub(r"^.*?Requesting\s+", "", title), re.I)
    if m and kind == "RZ":
        f_, t_ = sq(m.group(1)), sq(m.group(2))
    if kind == "CPA":
        m = re.search(r"(?:presently has|currently has) (?:a |an )?(.+?) Future Land Use designation", memo, re.I)
        f_ = sq(m.group(1)) if m else None
        m = re.search(r"(?:to designate the parcel as|to change the Future Land Use (?:Map )?designation to|to amend .{0,60}? to) (.+?)(?:,| which|\.)", memo, re.I)
        t_ = sq(m.group(1)) if m else None
    pre = key.split()[0]
    is_pd = kind == "RZ" and (pre in ("PDP", "PUD", "PD") or bool(re.search(r"Planned Development|\bPDP|\bPUD\b", (t_ or "") + " " + title, re.I)))
    category = "Comp Plan Amendment" if kind == "CPA" else ("PD" if is_pd else "Rezoning")
    if kind == "CPA":
        app_type = "Large-Scale Comprehensive Plan Amendment" if re.search(r"-L\d|Large", key + title, re.I) else \
            "Small-Scale Comprehensive Plan Amendment (FLUM)"
    else:
        app_type = "Rezoning to Planned Development" if is_pd else "Rezoning"
    pname = None
    m = re.search(r"Requesting (.+)$", title)
    desc = sq(m.group(0)) if m else sq(title)
    if c.get("ono") and not key.startswith("Ordinance"):
        desc = f"{desc} (Ordinance No. {c['ono']})"
    desc = desc[:297] + "..." if len(desc) > 300 else desc
    if applicant:
        pname = re.split(r" on behalf of ", applicant)[-1]

    hearings = []
    for a in apps:
        res = None
        if a["mt"]["minutes"] and a["date"] <= today:
            pats = [re.escape(key).replace(r"\ ", r"\s?-?\s?")]
            if c.get("ono"):
                pats.append(rf"Ordinance\s*(?:Number|No\.?)?\s*{c['ono']}\b")
            res = minutes_result(minutes_text(a["mt"]), pats)
        if res is None and re.search(r"\bwithdrawn\b", a["title"], re.I):
            res = "Withdrawn"
        rd = a["reading"]
        if a["body"] == PZ and res in ("Approved", "Denied"):
            res = "Recommended " + ("Approval" if res == "Approved" else "Denial")
        body = PZ if a["body"] == PZ else (f"City Council ({rd} reading)" if rd else CC)
        h = {"body": body, "date": a["date"], "result": res, "_rd": rd, "_body": a["body"]}
        prev = next((x for x in hearings if x["body"] == h["body"] and x["date"] == h["date"]), None)
        if prev:
            prev["result"] = prev["result"] or h["result"]
        else:
            hearings.append(h)
    hearings.sort(key=lambda h: (h["date"], h["_body"] != PZ))
    ccs = [h for h in hearings if h["_body"] == CC]
    rank = {"first": 1, "transmittal": 1, "second": 2, None: 0}
    for i, h in enumerate(ccs):
        later = [x for x in ccs[i + 1:] if x["date"] > h["date"]]
        if h["result"] is not None or not later or h["date"] > today:
            continue
        if h["_rd"] and any(x["_rd"] == h["_rd"] for x in later):
            h["result"] = "Continued"
        elif any(rank.get(x["_rd"], 0) > rank.get(h["_rd"], 0) for x in later):
            h["result"] = "Approved" if h["_rd"] in ("first", "transmittal") else None
    for h in hearings:
        if h["_body"] == CC and h["_rd"] in ("first", "transmittal") and h["result"] == "Approved":
            h["result"] = "Approved on first reading" if h["_rd"] == "first" else "Transmitted"
    status, raw, decision = status_from(hearings, today)
    return {
        "jurisdiction": JUR, "county": COUNTY, "app_num": key, "category": category, "app_type": app_type,
        "project_name": pname, "description": desc or None, "applicant": applicant,
        "parcel_ids": pids, "address": address, "acres": acres,
        "from_zoning": f_ if kind == "RZ" else None, "to_zoning": t_ if kind == "RZ" else None,
        "from_flu": f_ if kind == "CPA" else None, "to_flu": t_ if kind == "CPA" else None,
        "filed_date": apps[0]["date"], "status": status, "status_raw": raw,
        "hearings": [{"body": h["body"], "date": h["date"], "result": h["result"]} for h in hearings],
        "decision_date": decision, "url": apps[-1]["url"], "docs_url": meeting_url(apps[-1]["mt"]),
        "lat": None, "lon": None, "source": SOURCE,
    }


def status_from(hearings, today):
    past = [h for h in hearings if h["date"] <= today]
    future = [h for h in hearings if h["date"] > today]
    if any(h["result"] == "Withdrawn" for h in past):
        h = [x for x in past if x["result"] == "Withdrawn"][-1]
        return "Withdrawn", f"{h['body']} {h['date']}: withdrawn", None
    den = [h for h in past if h["_body"] == CC and h["result"] == "Denied"]
    if den:
        return "Denied", f"{den[-1]['body']} {den[-1]['date']}: denied", den[-1]["date"]
    fin = [h for h in past if h["_body"] == CC and h["result"] == "Approved" and h["_rd"] not in ("first", "transmittal")]
    if fin:
        return "Approved", f"{fin[-1]['body']} {fin[-1]['date']}: approved (minutes)", fin[-1]["date"]
    if future:
        raw = f"next {future[0]['body']} {future[0]['date']}"
        if past and past[-1]["result"]:
            raw = f"{past[-1]['body']} {past[-1]['date']}: {past[-1]['result']}; " + raw
        return "Hearing Scheduled", raw, None
    if past and past[-1]["result"] == "Continued":
        return "Continued", f"{past[-1]['body']} {past[-1]['date']}: continued", None
    if past:
        lp = past[-1]
        if lp["result"]:
            return "Filed", f"{lp['body']} {lp['date']}: {lp['result']}; next hearing not yet on an agenda", None
        return "Hearing Scheduled", f"{lp['body']} {lp['date']} held; minutes not yet posted", None
    return "Filed", "On agenda", None


if __name__ == "__main__":
    since = sys.argv[1] if len(sys.argv) > 1 else (dt.date.today() - dt.timedelta(days=14)).isoformat()
    refresh = sys.argv[2].split(",") if len(sys.argv) > 2 else []
    t0 = time.time()
    recs = fetch(since, refresh)
    print(json.dumps(recs, indent=2, ensure_ascii=False))
    from collections import Counter
    log(f"{len(recs)} records in {time.time() - t0:.0f}s; status={dict(Counter(r['status'] for r in recs))}; "
        f"category={dict(Counter(r['category'] for r in recs))}")
