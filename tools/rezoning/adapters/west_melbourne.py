#!/usr/bin/env python3
"""City of West Melbourne (Brevard County) rezoning / PUD / future land use map amendment adapter.

Source: the city's CivicPlus AgendaCenter (www.westmelbourne.gov/AgendaCenter), category 2
"Planning and Zoning" and category 1 "City Council", same platform as groveland.py. Each agenda
is a single packet PDF (agenda pages + staff memos + exhibits, 5 to 30 MB; the server ignores
HTTP Range, so packets are downloaded whole, only the agenda pages and the pages that mention a
case are parsed). Minutes are separate PDFs on the same row.
 * P&Z public hearing items carry the city case number: "Small Scale Comprehensive Plan Map
   Amendment (SSA-2026-01) - Lamarre - 235 Park Hill Boulevard", "Rezoning (REZ-2026-01) Lamarre -
   235 Park Hill Boulevard", with acreage, from/to designations and applicant. In scope: SSA, LSA,
   CPA (map), REZ, PUD. Annexations, development agreements (DA), plats (PP/FP), code amendments
   are skipped.
 * City Council agendas group a project's items under one heading ("B. 235 Park Hill Boulevard /
   1.) Amend the Future Land Use Map ... (Case #SSA-2026-01) / 2.) Proposed Rezoning ..."); the
   rezoning sub-item usually has no case number, so it is linked to the REZ case of the same
   project (same address / project name) heard by P&Z.
 * Outcomes come from the minutes ("Council Member Adams moved to approve the first reading of
   Ordinance No. 2026-04, rezoning ... which passed (5 to 1 ...)"), credited to the case of the
   project heading that precedes the motion (future land use vs rezoning by the motion wording).
www.westmelbourne.gov intermittently drops connections, so requests are retried.
Cases surface when they first reach a P&Z agenda; filed_date = date the agenda was posted.
Requires pypdf.
"""
import datetime as dt
import html as _html
import io
import json
import logging
import re
import sys
import time

import requests

try:
    import pypdf
    logging.getLogger("pypdf").setLevel(logging.ERROR)
except ImportError:  # pragma: no cover
    pypdf = None

JURISDICTIONS = ["City of West Melbourne"]
JUR = "City of West Melbourne"
BASE = "https://www.westmelbourne.gov"
SOURCE = "West Melbourne AgendaCenter (P&Z Board + City Council packets and minutes)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
LOOKBACK_DAYS = 60
PZ, CC = "Planning and Zoning Board", "City Council"
CATS = {2: PZ, 1: CC}
CASE_RE = re.compile(r"\b(SSA|LSA|CPA|REZ|PUD)\s?-?\s?#?\s?(\d{4})\s?-\s?(\d{2,3})\b")
PID_RE = re.compile(r"\b(\d{2})\s?-\s?(\d{2})\s?-\s?(\d{2})\s?-\s?([0-9A-Z]{2})\s?-\s?([0-9A-Z*]{1,5}(?:\.\d+)?)"
                    r"(?:\s?-\s?([0-9A-Z*]{1,5}(?:\.\d+)?))?\b")
STREET = r"(?:Boulevard|Blvd|Avenue|Ave|Road|Rd|Street|St|Drive|Dr|Lane|Ln|Court|Ct|Way|Parkway|Pkwy|Place|Pl|Highway|Hwy|Circle|Trail)"

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


def log(*a):
    print("[west_melbourne]", *a, file=sys.stderr, flush=True)


def req(method, url, tries=6, sleep=0.6, **kw):
    last = None
    for i in range(tries):
        try:
            r = _s.request(method, url, timeout=180, **kw)
            r.raise_for_status()
            time.sleep(sleep)
            return r
        except Exception as e:  # noqa: BLE001  (the site drops connections now and then)
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"{method} failed {url}: {last}")


def sq(s):
    return re.sub(r"\s+", " ", s or "").strip()


def clean(v):
    v = sq(v).strip(" .,;:")
    return v or None


def to_float(s):
    try:
        v = float(str(s).replace(",", ""))
        return v if 0 < v < 100000 else None
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ AgendaCenter
def list_meetings(year, cat):
    t = req("POST", f"{BASE}/AgendaCenter/UpdateCategoryList",
            data={"year": year, "catID": cat, "startDate": "", "endDate": "", "term": "",
                  "prevVersionScreen": "false"}).text
    out = []
    for row in re.findall(r'<tr[^>]*class="catAgendaRow"[^>]*>(.*?)</tr>', t, re.S):
        a = re.search(r'href="(/AgendaCenter/ViewFile/Agenda/_(\d{2})(\d{2})(\d{4})-(\d+))"', row)
        if not a:
            continue
        title = sq(_html.unescape(re.sub(r"<[^>]+>", " ", row)))
        if re.search(r"Cancel", title, re.I) or (re.search(r"Workshop", title, re.I) and not re.search(r"Regular", title, re.I)):
            continue
        mn = re.search(r'href="(/AgendaCenter/ViewFile/Minutes/[^"]+)"', row)
        posted = re.search(r"Posted\s*<abbr[^>]*>(\w+)</abbr>\s*(\d{1,2}),\s*(\d{4})", row)
        pdate = None
        if posted:
            try:
                pdate = dt.datetime.strptime(f"{posted.group(1)} {posted.group(2)} {posted.group(3)}", "%b %d %Y").date().isoformat()
            except ValueError:
                pdate = None
        date = f"{a.group(4)}-{a.group(2)}-{a.group(3)}"
        out.append({"date": date, "title": title[:120], "url": BASE + a.group(1),
                    "minutes": BASE + mn.group(1) if mn else None, "posted": min(pdate or date, date),
                    "body": CATS[cat]})
    return out


_pages = {}


def pdf_pages(url):
    if url not in _pages:
        rdr = pypdf.PdfReader(io.BytesIO(req("GET", url).content))
        _pages[url] = rdr
    return _pages[url]


def page_texts(url, first=8, want=None):
    """Agenda pages (up to ADJOURN) plus, if want (regex), any later page that matches it."""
    rdr = pdf_pages(url)
    agenda, extra = [], []
    for i, p in enumerate(rdr.pages):
        if i < first:
            try:
                agenda.append(p.extract_text() or "")
            except Exception:  # noqa: BLE001
                agenda.append("")
            continue
        if want is None:
            break
        try:
            t = p.extract_text() or ""
        except Exception:  # noqa: BLE001
            t = ""
        if want.search(t):
            extra.append(t)
        if i > 120:
            break
    txt = "\n".join(agenda)
    cut = re.search(r"\n\s*\d{1,2}\.\s*ADJOURN", txt)
    return (txt[:cut.end()] if cut else txt), extra


# ------------------------------------------------------------------ parsing
def keys_in(text):
    out = []
    for m in CASE_RE.finditer(text or ""):
        k = f"{m.group(1)}-{m.group(2)}-{m.group(3).zfill(2)}"
        if k not in out:
            out.append(k)
    return out


def addr_token(text):
    m = re.search(rf"\b(\d{{2,6}})\s+((?:[NSEW]\.?\s+)?[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?)\s+{STREET}\b", text or "")
    return f"{m.group(1)} {m.group(2).lower()}" if m else None


def pids_of(text):
    out = []
    t = re.sub(r"(\d)\s+-", r"\1-", re.sub(r"-\s+(\d)", r"-\1", text or ""))
    for m in PID_RE.finditer(t):
        p = "-".join(x for x in m.groups() if x)
        if p not in out:
            out.append(p)
    return out


def pz_items(agenda):
    """[(heading, text)] public hearing items of a P&Z agenda."""
    t = re.sub(r"[ \t]+", " ", agenda)
    m = re.search(r"PUBLIC HEARINGS?", t)
    if not m:
        return []
    sec = t[m.end():]
    end = re.search(r"\n\s*\d{1,2}\.\s*(?:FINAL PLAT|P ?UBLIC COMMENTS|PLANNING DIRECTOR|BUSINESS ITEMS|ADJOURN|NEW BUSINESS|OLD BUSINESS)", sec)
    if end:
        sec = sec[:end.start()]
    parts = re.split(r"\n\s*([a-z])\.\s+", "\n" + sec)
    out = []
    for i in range(1, len(parts) - 1, 2):
        body = parts[i + 1]
        head = sq(body.split("\n \n")[0])[:250]
        out.append((head, sq(body)))
    return out


def cc_groups(agenda):
    """[(heading, text)] public hearing groups of a Council agenda ('A. 235 Park Hill Boulevard')."""
    t = re.sub(r"[ \t]+", " ", agenda)
    m = re.search(r"\n\s*\d{1,2}\.\s*PUBLIC HEARINGS?", t)
    if not m:
        return []
    sec = t[m.end():]
    end = re.search(r"\n\s*\d{1,2}\.\s*(?:PUBLIC FORUM|CONSENT AGENDA|ACTION AGENDA)", sec)
    if end:
        sec = sec[:end.start()]
    parts = re.split(r"\n\s*([A-Z])\.\s+", "\n" + sec)
    return [(sq(parts[i + 1])[:200], sq(parts[i + 1])) for i in range(1, len(parts) - 1, 2)]


def kind_of(text):
    t = text.lower()
    if re.search(r"future land use|comprehensive plan|\bflum\b", t):
        return "FLU"
    if re.search(r"rezon|zoning", t):
        return "ZON"
    return None


def kind_of_any(text, kind):
    t = text.lower()
    return bool(re.search(r"future land use|comprehensive plan", t)) if kind == "FLU" else bool(re.search(r"rezon", t))


def motion_results(text, body):
    """[(pos, verb, ok)] motions in minutes text."""
    flat = re.sub(r"\s+", " ", text)
    flat = re.sub(r"\ba\s+pprov", "approv", flat)
    out = []
    for m in re.finditer(r"\bmoved\s+to\s+|\bMoved by [^.]{0,120}?\bto\s+", flat, re.I):
        tail = flat[m.end():m.end() + 1500]
        mm = re.search(r"which (passed|failed|was approved|was denied)|[Mm]otion (passed|carried|failed)|"
                       r"(passed|carried) unanimously|All voted in favor", tail)
        if not mm:
            continue
        verb = tail[:mm.start()][:700]
        word = (mm.group(1) or mm.group(2) or mm.group(3) or "passed").lower()
        out.append((m.start(), verb, "fail" not in word and "denied" not in word))
    return flat, out


# ------------------------------------------------------------------ fetch
def fetch(since, refresh):
    if pypdf is None:
        raise RuntimeError("pypdf is required for the West Melbourne adapter")
    today = dt.date.today().isoformat()
    refresh = [r.strip().upper() for r in (refresh or []) if r and r.strip()]
    start = (dt.date.fromisoformat(since) - dt.timedelta(days=LOOKBACK_DAYS)).isoformat()
    for r in refresh:
        k = keys_in(r)
        if k:
            # packets are downloaded whole, so look back at most ~11 months for open cases
            start = min(start, max(f"{int(k[0].split('-')[1])}-01-01", (dt.date.today() - dt.timedelta(days=330)).isoformat()))
    end = (dt.date.today() + dt.timedelta(days=75)).isoformat()
    meetings = []
    try:
        for y in range(int(start[:4]), int(end[:4]) + 1):
            for cat in CATS:
                meetings += [m for m in list_meetings(y, cat) if start <= m["date"] <= end]
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"West Melbourne AgendaCenter unreachable: {e}") from e
    meetings.sort(key=lambda m: (m["date"], m["body"] != PZ))
    log(f"{len(meetings)} P&Z / Council meetings {start}..{end}")

    cases = {}
    want = re.compile(r"\b(?:SSA|LSA|CPA|REZ|PUD)\s?-?\s?#?\s?\d{4}\s?-\s?\d{2}")
    for mt in meetings:
        try:
            agenda, extra = page_texts(mt["url"], want=want if mt["body"] == PZ else None)
        except Exception as e:  # noqa: BLE001
            log(f"{mt['body']} {mt['date']}: {e}")
            continue
        if mt["body"] == PZ:
            for head, text in pz_items(agenda):
                ks = [k for k in keys_in(head)]
                if not ks or re.match(r"(Final|Preliminary) Plat|Development Agreement|Code Amendment", head, re.I):
                    continue
                for k in ks:
                    c = cases.setdefault(k, {"apps": [], "pz_text": text, "head": head, "staff": []})
                    c["apps"].append({"body": PZ, "date": mt["date"], "posted": mt["posted"], "url": mt["url"],
                                      "reading": None, "text": text})
                    c["staff"] += [x for x in extra if k in re.sub(r"\s", "", x).replace("#", "")]
        mt["agenda_text"] = agenda
    # project tokens
    for k, c in cases.items():
        c["token"] = addr_token(c["head"]) or addr_token(c["pz_text"])
        m = re.search(r"\)\s*[\u2013\-]?\s*(.+?)(?:\s+[\u2013\-]\s+|\s+A (?:request|staff)|\s+Planning and Zoning|$)", c["head"])
        nm = clean(m.group(1)) if m else None
        c["name"] = nm if nm and not re.match(r"\d", nm) and len(nm) <= 60 else None
        c["acres_key"] = (re.search(r"([\d,]*\.?\d+)\s*\+?\s*/?\s*-?\s*±?\s*acres", c["pz_text"], re.I) or [None, None])[1]
        c["kind"] = "FLU" if k.split("-")[0] in ("SSA", "LSA", "CPA") else "ZON"

    def companions(k):
        c = cases[k]
        d0 = min(a["date"] for a in c["apps"])
        return [o for o, oc in cases.items() if o != k and min(a["date"] for a in oc["apps"]) == d0 and
                ((c["token"] and oc["token"] == c["token"]) or (c["name"] and oc["name"] == c["name"]) or
                 (c["acres_key"] and oc["acres_key"] == c["acres_key"]))]

    def match_group(text):
        hits = [k for k in keys_in(text) if k in cases]
        for k in list(hits):
            for o in companions(k):
                if o not in hits and kind_of_any(text, cases[o]["kind"]):
                    hits.append(o)
        low = text.lower()
        for k, c in cases.items():
            if k in hits:
                continue
            if c["token"] and c["token"] in re.sub(r"\s+", " ", low):
                hits.append(k)
            elif c["name"] and len(c["name"]) > 4 and c["name"].lower() in low and any(h.split("-")[1] == k.split("-")[1] for h in hits):
                hits.append(k)
        return hits

    for mt in meetings:
        if mt["body"] != CC or not mt.get("agenda_text"):
            continue
        for head, text in cc_groups(mt["agenda_text"]):
            hits = match_group(text)
            if not hits:
                continue
            subs = re.split(r"\s\d\.\)\s", " " + text)
            for k in hits:
                kd = cases[k]["kind"]
                mine = [s_ for s_ in subs if kind_of(s_) == kd or k in keys_in(s_)] or [text]
                stext = " ".join(mine)
                rd = re.search(r"\b(first|second|final) reading|\btransmittal\b", stext, re.I)
                reading = None
                if rd:
                    reading = "transmittal" if "transmittal" in rd.group(0).lower() else \
                        ("first" if rd.group(1).lower() == "first" else "second")
                cases[k]["apps"].append({"body": CC, "date": mt["date"], "posted": mt["posted"], "url": mt["url"],
                                         "reading": reading, "text": stext})
    log(f"{len(cases)} in-scope cases")

    # outcomes from minutes
    outcomes = {}
    for mt in meetings:
        if not mt["minutes"] or mt["date"] > today or not cases:
            continue
        try:
            rdr = pdf_pages(mt["minutes"])
            txt = "\n".join((p.extract_text() or "") for p in rdr.pages)
        except Exception as e:  # noqa: BLE001
            log(f"minutes {mt['body']} {mt['date']}: {e}")
            continue
        flat, mots = motion_results(txt, mt["body"])
        low = flat.lower()
        markers = []
        for k, c in cases.items():
            for m in re.finditer(re.escape(k).replace("\\-", r"\s?-?\s?#?\s?"), flat):
                markers.append((m.start(), k))
            if c["token"]:
                for m in re.finditer(re.escape(c["token"]), low):
                    markers.append((m.start(), k))
            if c["name"] and len(c["name"]) > 4:
                for m in re.finditer(re.escape(c["name"].lower()), low):
                    markers.append((m.start(), k))
        markers.sort()
        for pos, verb, ok in mots:
            before = [k for p, k in markers if p < pos and p > pos - 30000]
            if not before:
                continue
            kd = kind_of(verb)
            if re.search(r"minutes|consent agenda|excuse|annex", verb[:120], re.I) and not kd:
                continue
            last = before[-1]
            if not kd and not any(x and x in verb.lower() for x in (last.lower(), (cases[last]["name"] or "").lower() or None,
                                                                      cases[last]["token"])):
                continue   # a motion on some other item (no land use wording, no case reference)
            group = {k for k in before[-6:]} | {k for k in cases if cases[k]["token"] and cases[k]["token"] == cases[last]["token"]} \
                | set(companions(last))
            targets = [k for k in group if cases[k]["kind"] == kd] if kd else [last]
            for k in targets:
                v = verb.lower()
                if re.search(r"\b(table|continue|postpone|defer)", v[:200]):
                    r = "Continued" if ok else None
                elif re.search(r"\bden(y|ial)\b", v[:200]):
                    r = ("Recommended Denial" if mt["body"] == PZ else "Denied") if ok else None
                elif re.search(r"approv|recommend|adopt|transmi", v[:300]):
                    if mt["body"] == PZ:
                        r = "Recommended Approval" if ok else "Motion to recommend approval failed"
                    elif re.search(r"first reading", v[:200]):
                        r = "Approved on first reading" if ok else "Denied"
                    elif re.search(r"transmi", v[:200]):
                        r = "Transmitted" if ok else None
                    else:
                        r = "Approved" if ok else "Denied"
                else:
                    r = None
                if r:
                    outcomes[(k, mt["body"], mt["date"])] = r

    out = []
    for k, c in sorted(cases.items()):
        first = min(a["posted"] for a in c["apps"])
        if first < since and k not in refresh:
            continue
        try:
            out.append(build(k, c, outcomes, today))
        except Exception as e:  # noqa: BLE001
            log(f"{k}: skipped ({type(e).__name__}: {e})")
    for r in set(refresh) - set(cases):
        log(f"refresh id {r} not found on agendas since {start}")
    return out


def build(key, c, outcomes, today):
    apps = sorted(c["apps"], key=lambda a: (a["date"], a["body"] != PZ))
    hmap = {}
    for a in apps:
        body = f"City Council ({a['reading']} reading)" if a["body"] == CC and a["reading"] in ("first", "second") else \
            ("City Council (transmittal)" if a["reading"] == "transmittal" else a["body"])
        h = hmap.setdefault((a["body"], a["date"]), {"body": body, "date": a["date"], "result": None})
        r = outcomes.get((key, a["body"], a["date"]))
        if r == "Approved" and a["reading"] == "first":
            r = "Approved on first reading"
        if r == "Approved" and a["reading"] == "transmittal":
            r = "Transmitted"
        h["result"] = h["result"] or r
    hearings = sorted(hmap.values(), key=lambda h: (h["date"], not h["body"].startswith("Planning")))
    past = [h for h in hearings if h["date"] <= today]
    future = [h for h in hearings if h["date"] > today]
    decision = None
    if any(h["result"] == "Withdrawn" for h in hearings):
        status, raw = "Withdrawn", "Withdrawn (minutes)"
    elif past and past[-1]["body"].startswith(CC) and past[-1]["result"] in ("Approved", "Denied") and not future:
        status, decision = past[-1]["result"], past[-1]["date"]
        raw = f"{past[-1]['body']} {decision}: {past[-1]['result']} (minutes)"
    elif past and past[-1]["result"] == "Continued":
        status, raw = "Continued", f"{past[-1]['body']} {past[-1]['date']}: tabled / continued"
        if future:
            raw += f"; next {future[0]['body']} {future[0]['date']}"
    elif future:
        status, raw = "Hearing Scheduled", f"next {future[0]['body']} {future[0]['date']}"
        if past and past[-1]["result"]:
            raw = f"{past[-1]['body']} {past[-1]['date']}: {past[-1]['result']}; " + raw
    elif past and past[-1]["result"] is None:
        status, raw = "Hearing Scheduled", f"{past[-1]['body']} {past[-1]['date']} held; outcome not yet in published minutes"
    elif past:
        status = "Filed"
        raw = f"{past[-1]['body']} {past[-1]['date']}: {past[-1]['result']}; next hearing not yet on an agenda"
    else:
        status, raw = "Filed", "On agenda"

    t = re.sub(r"\s*Planning and Zoning Board Meeting \w+ \d{1,2}, \d{4}\s+Page \d+\s*", " ", c["pz_text"])
    t = re.sub(r"\b([A-Z]{1,3})- ([A-Z0-9])", r"\1-\2", t)
    staff = " ".join(sq(x) for x in c.get("staff", []))
    frm = to = None
    m = re.search(r"\bfrom\s+(.+?)\s+to\s+(.+?)(?:\.\s|\s+for\s|\s+on\s+the|\s+future land use|\s*$)", t)
    if m:
        frm, to = clean(m.group(1)), clean(m.group(2))
    m = re.search(r"([\d,]*\.?\d+)\s*\+?\s*/?\s*-?\s*±?\s*acres", t, re.I)
    acres = to_float(m.group(1)) if m else None
    m = re.search(r"Applicants?:\s*(.+?)(?:\s+Location:|\s+The proposed|$)", t)
    applicant = clean(m.group(1)) if m else None
    m = re.search(r"Location:\s*(.+?)(?:\s+The proposed|\s+Applicant|$)", t)
    location = clean(m.group(1)) if m else None
    m = re.search(rf"\b(\d{{2,6}}\s+(?:[NSEW]\.?\s+)?[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?\s+{STREET})\b", c["head"] + " " + t)
    address = clean(m.group(1)) if m else location
    pids = pids_of(t) or pids_of(staff)
    accts = re.findall(r"Tax Account(?: No\.?| Number| #)?\s*:?\s*(\d{7})", t + " " + staff)
    for a in accts:
        if a not in pids:
            pids.append(a)
    typ = key.split("-")[0]
    if typ in ("SSA", "LSA", "CPA"):
        category = "Comp Plan Amendment"
        app_type = {"SSA": "Small Scale Comprehensive Plan Map Amendment (SSA)",
                    "LSA": "Large Scale Comprehensive Plan Map Amendment (LSA)",
                    "CPA": "Comprehensive Plan Map Amendment (CPA)"}[typ]
    else:
        pd = typ == "PUD" or re.search(r"\bPUD\b|Planned Unit Development", to or "", re.I)
        category = "PD" if pd else "Rezoning"
        app_type = "Planned Unit Development (PUD)" if typ == "PUD" else "Rezoning (REZ)"
    desc = clean(re.sub(r"\s*The proposed .*$", "", t))
    if desc and len(desc) > 400:
        desc = desc[:397] + "..."
    return {
        "jurisdiction": JUR, "county": "Brevard", "app_num": key, "category": category, "app_type": app_type,
        "project_name": c.get("name"), "description": desc, "applicant": applicant, "parcel_ids": pids,
        "address": address, "acres": acres,
        "from_zoning": None if category == "Comp Plan Amendment" else frm,
        "to_zoning": None if category == "Comp Plan Amendment" else to,
        "from_flu": frm if category == "Comp Plan Amendment" else None,
        "to_flu": to if category == "Comp Plan Amendment" else None,
        "filed_date": min(a["posted"] for a in apps), "status": status, "status_raw": raw, "hearings": hearings,
        "decision_date": decision, "url": apps[-1]["url"], "docs_url": apps[0]["url"],
        "lat": None, "lon": None, "source": SOURCE,
    }


if __name__ == "__main__":
    since = sys.argv[1] if len(sys.argv) > 1 else (dt.date.today() - dt.timedelta(days=14)).isoformat()
    refresh = sys.argv[2].split(",") if len(sys.argv) > 2 and sys.argv[2] else []
    t0 = time.time()
    recs = fetch(since, refresh)
    print(json.dumps(recs, indent=2, ensure_ascii=False))
    from collections import Counter
    log(f"{len(recs)} records in {time.time() - t0:.0f}s; status={dict(Counter(r['status'] for r in recs))}; "
        f"category={dict(Counter(r['category'] for r in recs))}")
