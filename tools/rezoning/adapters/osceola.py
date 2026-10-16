#!/usr/bin/env python3
"""Osceola County (unincorporated) rezoning / PD / comp plan amendment adapter.

Sources (all public, no login):
  1. Accela Citizen Access, agency OSCEOLA (aca-prod.accela.com/OSCEOLA), via accela_core:
       Zoning module   Zoning/Application/Zoning Map Amendment/NA          ZMA26-0037  -> Rezoning
                       Zoning/Application/Planned Development/Public Hearing PD26-00024 -> PD
                       (new PDs and major / boundary PD amendments; the "Administrative"
                        PD minor-amendment type is out of scope and not searched)
       Planning module Planning/Application/Comprehensive Plan Amendment/NA  CPA26-0003 -> Comp Plan
                       Amendment (ASI "Map Change" = Yes; text-only CPAs are skipped)
     Gives filing date, status, parcel numbers, acreage (ASI), project description and the
     workflow, whose "Planning Commission" / "Board of County Commissioners" tasks carry the
     scheduled hearing date (due date of a "Scheduled" entry) and the outcome.
     Unsubmitted online drafts (OSCTEMP26-xxxxx) are skipped.
  2. OnBase Agenda Online (docs.osceola.org/OnBaseAgendaOnline), meeting types 120 (Planning
     Commission) and 102 (BCC Meeting). Agenda items carry the case number, ordinance title
     (acres, from/to districts), applicant / owner lines, the PC recommendation (on BCC agendas)
     and "Meeting Dates"; BCC "Summary" documents add the action (ADOPTED / APPROVED /
     CONTINUED ...). Item views give the staff report PDF link (docs_url).
Parcel IDs are returned as Accela shows them: Osceola 18-character PIN without dashes,
e.g. "012530000000610000" (= 01-25-30-0000-0061-0000, section-township-range-sub-block-lot).
"""
import datetime as dt
import html as htmlmod
import json
import os
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import accela_core as C  # noqa: E402

JURISDICTIONS = ["Osceola County"]
JUR, COUNTY = "Osceola County", "Osceola"
SOURCE = "Osceola Accela + OnBase Agenda Online (PC / BCC)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
AGENCY = "OSCEOLA"
ONBASE = "https://docs.osceola.org/OnBaseAgendaOnline"
MT_PC, MT_BCC = "120", "102"
SLEEP = 0.4
LOOKBACK_DAYS = 60     # agenda scan starts this far before `since`
LOOKAHEAD_DAYS = 120
REFRESH_LOOKBACK_DAYS = 365

# (module, record type value) -> category
TYPES = [
    ("Zoning", "Zoning/Application/Zoning Map Amendment/NA", "Rezoning"),
    ("Zoning", "Zoning/Application/Planned Development/Public Hearing", "PD"),
    ("Planning", "Planning/Application/Comprehensive Plan Amendment/NA", "Comp Plan Amendment"),
]
CASE_RX = re.compile(r"\b(ZMA\d{2}-\d{4}|PD\d{2}-\d{5}|CPA\d{2}-\d{4})\b")
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


def log(*a):
    print("[osceola]", *a, file=sys.stderr, flush=True)


def get(url, params=None, tries=3, sleep=SLEEP, **kw):
    last = None
    for i in range(tries):
        try:
            r = _s.get(url, params=params, timeout=90, **kw)
            if r.status_code >= 500 or r.status_code == 429:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            time.sleep(sleep)
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1) ** 2)
    raise RuntimeError(f"GET failed {url}: {last}")


def clean(s):
    if s is None:
        return None
    s = re.sub(r"\s+", " ", htmlmod.unescape(str(s))).strip(" ,;")
    return s or None


def module_of(num):
    return "Planning" if num.upper().startswith("CPA") else "Zoning"


def category_of(num):
    n = num.upper()
    if n.startswith("PD"):
        return "PD"
    if n.startswith("CPA"):
        return "Comp Plan Amendment"
    return "Rezoning"


def text_date(s):
    m = re.search(r"\b(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{4})\b", s or "")
    if m:
        try:
            return dt.date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
        except ValueError:
            return None
    m = re.search(rf"({MONTHS})\s+(\d{{1,2}}),?\s+(\d{{4}})", s or "", re.I)
    if not m:
        return None
    try:
        return dt.datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y").date().isoformat()
    except ValueError:
        return None


def acres_of(*texts):
    """Site acreage from ordinance titles / descriptions. Ignores district names like
    'Rural Development (one acre) (R-1)' or 'Estate Development (1 acre)'."""
    pats = [r"comprised of approximately\s+([\d,]*\.?\d+)\s+acres?",
            r"approximately\s+([\d,]*\.?\d+)\s*(?:\+/-\s*)?acres?",
            r"(?<![(\w])([\d,]*\.?\d+)\s*(?:\+/-|±)?\s*(?:total\s+|gross\s+)?(?:acres?|ac\.?)(?![\w)])"]
    for p in pats:
        for t in texts:
            for m in re.finditer(p, t or "", re.I):
                pre = (t or "")[max(0, m.start() - 1):m.start()]
                if pre == "(":
                    continue
                v = C.to_float(m.group(1))
                if v:
                    return v
    return None


ZDIST = r"[A-Z][A-Za-z0-9 ,/&\-\u2014\u2013()']{1,90}?\([A-Z][A-Z0-9\-/ ]{0,12}\)"


def from_to(text):
    """'changing the zoning designation from Rural Development (two-acres) (R-2) to Commercial
    Neighborhood (CN) Zoning District' / 'Rezone from AC to IR' -> (from, to)."""
    t = clean(text) or ""
    m = re.search(rf"from\s+({ZDIST})\s+to\s+(?:an?\s+)?({ZDIST})", t)
    if m:
        return clean(m.group(1)), clean(m.group(2))
    m = re.search(r"(?:rezon\w*|request\w*|zoning)\s+(?:from\s+)?([A-Z][A-Z0-9\-]{0,6}(?: ?[A-Z0-9]{1,3})?)"
                  r"\s+(?:to|-+>)\s+([A-Z][A-Z0-9\-]{0,6})\b", t)
    if m:
        return clean(m.group(1)), clean(m.group(2))
    m = re.search(r"\b([A-Z]{1,3}-?\d?[A-Z]?)\s+to\s+([A-Z]{1,3}(?:-[A-Z0-9]{1,2})?)\b", t)
    if m and m.group(1) not in ("TO", "A") and len(m.group(2)) <= 5:
        return clean(m.group(1)), clean(m.group(2))
    return None, None


def flu_from_to(text):
    t = clean(text) or ""
    m = re.search(r"Future Land Use designation from\s+(.+?)\s+to\s+(.+?)(?:\s+on certain|\s+for|;|,|$)", t, re.I)
    if m:
        return clean(m.group(1)), clean(m.group(2))
    m = re.search(r"\bfrom\s+(.{2,60}?)\s*(?:FLU|future land use)?\s+to\s+(.{2,60}?)\s*(?:FLU\b|future land use|\.|;|,|$)",
                  t, re.I)
    if m:
        return clean(m.group(1)), clean(m.group(2))
    return None, None


# --------------------------------------------------------------------------- OnBase agendas
def onbase_meetings(start, end):
    """Meetings (PC + BCC) between two dates, from the JSON embedded in the search page."""
    url = f"{ONBASE}/Meetings/Search"
    params = {"dropid": "11", "mtids": f"{MT_BCC},{MT_PC}",
              "dropsv": start.strftime("%m/%d/%Y"), "dropev": end.strftime("%m/%d/%Y")}
    r = get(url, params)
    m = re.search(r"new SearchResults\((\{.*?\})\)\);", r.text, re.S)
    if not m:
        raise RuntimeError("OnBase meeting search: results JSON not found")
    data = json.loads(m.group(1))
    return data.get("Meetings") or []


RESULT_RX = re.compile(r"^(ADOPTED|APPROVED|DENIED|CONTINUED|WITHDRAWN|TABLED|DEFERRED|POSTPONED|"
                       r"REMOVED|PULLED|FAILED|RECOMMENDED|NO ACTION)\b", re.I)


def parse_agenda(html):
    """Split an OnBase agenda / summary document into items:
    [{item_id, text, result}] (result = bold action line in summaries)."""
    s = BeautifulSoup(html, "lxml")
    for a in s.find_all("a", href=re.compile(r"loadAgendaItem\(")):
        m = re.search(r"loadAgendaItem\((\d+),(true|false)\)", a["href"])
        if m:
            a.insert_before(f"\n@@ITEM {m.group(1)} {m.group(2)}@@\n")
    for b in s.find_all("span", style=re.compile(r"font-weight:\s*bold")):
        t = b.get_text(" ", strip=True)
        if RESULT_RX.match(t):
            b.insert_before("\n@@RESULT ")
            b.insert_after("@@\n")
    txt = s.get_text("\n")
    items = []
    for chunk in re.split(r"\n@@ITEM ", txt)[1:]:
        m = re.match(r"(\d+) (true|false)@@", chunk)
        if not m or m.group(2) == "true":
            continue
        body = chunk[m.end():]
        res = re.findall(r"@@RESULT\s*(.+?)@@", body, re.S)
        body = re.sub(r"@@RESULT\s*(.+?)@@", " ", body, flags=re.S)
        lines = [clean(x) for x in body.split("\n")]
        lines = [x for x in lines if x]
        items.append({"item_id": m.group(1), "lines": lines, "text": " ".join(lines),
                      "result": clean(res[-1]) if res else None})
    return items


def item_info(lines):
    out = {}
    for ln in lines:
        if re.search(r"\((?:Applicants?|Owners?|Agents?|Applicants?/Owners?|Owner/Applicant)\)\s*$", ln, re.I) \
                or re.match(r"Applicants?\s*/\s*Owners?\s*:", ln, re.I):
            pm = re.search(r"\(([^()]*)\)\s*$", ln)
            tag = (pm.group(1) if pm else ln.split(":")[0]).lower()
            role = "applicant" if "applicant" in tag else ("agent" if "agent" in tag else "owner")
            name = re.sub(r"\s*\((?:[^()]*)\)\s*$", "", ln)
            name = re.sub(r"^Applicants?\s*/\s*Owners?\s*:\s*", "", name, flags=re.I)
            out.setdefault(role, clean(name))
        m = re.match(r"Planning Commission Recommendation:\s*(.*)", ln, re.I)
        if m:
            out["pc_rec"] = clean(m.group(1)) or out.get("pc_rec")
        elif out.get("pc_rec") == "" and ln:
            out["pc_rec"] = ln
        m = re.match(r"(?:Public Hearing|Meeting) Dates?:\s*(.*)", ln, re.I)
        if m:
            out["meeting_dates"] = m.group(1)
        m = re.match(r"Staff Recommendation:\s*(.*)", ln, re.I)
        if m:
            out["staff_rec"] = clean(m.group(1))
    if out.get("pc_rec") is None:
        for i, ln in enumerate(lines):
            if re.match(r"Planning Commission Recommendation:\s*$", ln, re.I) and i + 1 < len(lines):
                out["pc_rec"] = lines[i + 1]
    return out


def classify(res, body):
    r = (res or "").lower()
    if not r:
        return None
    if "withdr" in r:
        return "Withdrawn"
    if re.search(r"contin|table|defer|postpon", r):
        return "Continued"
    if "den" in r:
        return "Recommended Denial" if body == "Planning Commission" else "Denied"
    if re.search(r"adopt|approv", r):
        return "Recommended Approval" if body == "Planning Commission" else "Approved"
    return clean(res)


def pc_rec_result(rec):
    r = (rec or "").lower()
    if not r:
        return None
    if re.search(r"contin|table|defer", r):
        return "Continued"
    if re.search(r"\bden(y|ial|ied)\b", r):
        return "Recommended Denial"
    if re.search(r"approv|adopt", r):
        return "Recommended Approval"
    return None


def scan_agendas(since, refresh=()):
    start = dt.date.fromisoformat(since) - dt.timedelta(days=LOOKBACK_DAYS)
    if refresh:   # open cases from earlier runs: scan a year of agendas for their history
        start = min(start, dt.date.today() - dt.timedelta(days=REFRESH_LOOKBACK_DAYS))
    end = dt.date.today() + dt.timedelta(days=LOOKAHEAD_DAYS)
    meetings = onbase_meetings(start, end)
    apps = {}   # case -> [appearance]
    n = 0
    for mt in sorted(meetings, key=lambda m: m.get("Time") or ""):
        typ = str(mt.get("MeetingTypeName") or "")
        name = mt.get("Name") or ""
        if "workshop" in name.lower() or "budget" in name.lower():
            continue
        body = "Planning Commission" if "planning" in typ.lower() else "BOCC"
        date = (mt.get("Time") or "")[:10]
        if mt.get("IsSummaryAvailable"):
            doctype, typ_s = 3, "summary"
        elif mt.get("IsAgendaAvailable"):
            doctype, typ_s = 1, "agenda"
        else:
            continue
        try:
            r = get(f"{ONBASE}/Documents/ViewAgenda",
                    {"meetingId": mt["ID"], "type": typ_s, "doctype": doctype})
            items = parse_agenda(r.text)
            n += 1
        except Exception as e:  # noqa: BLE001
            log(f"agenda {mt.get('ID')} {date}: {e}")
            continue
        for it in items:
            cases = list(dict.fromkeys(CASE_RX.findall(it["text"])))
            if not cases:
                continue
            # an item may mention a companion case ("has a companion item ZMA26-0012");
            # the item's own case is the one in the title (first line)
            head = " ".join(it["lines"][:2])
            own = CASE_RX.findall(it["lines"][0]) if it["lines"] else []
            own = own or CASE_RX.findall(head)[:1] or cases[:1]
            info = item_info(it["lines"])
            res_raw = it["result"]
            m = re.match(r"\s*(Withdrawn|Continued|Tabled|Deferred)\b[^:]{0,80}:", head, re.I)
            if m and not res_raw:
                res_raw = clean(head[:m.end()].rstrip(":"))
            for c in own:
                apps.setdefault(c, []).append({
                    "date": date, "body": body, "meeting_id": mt["ID"], "item_id": it["item_id"],
                    "doc": typ_s, "result": classify(res_raw, body), "result_raw": res_raw,
                    "title": re.sub(r"^\s*(?:Withdrawn|Continued|Tabled|Deferred)\b[^:]{0,80}:\s*", "", head),
                    "text": it["text"], "info": info,
                })
    log(f"OnBase: {len(meetings)} PC/BCC meetings {start}..{end}, {n} documents read, "
        f"{len(apps)} in-scope case numbers on agendas")
    return apps


def staff_report_link(meeting_id, item_id):
    try:
        r = get(f"{ONBASE}/Meetings/ViewMeetingAgendaItem",
                {"meetingId": meeting_id, "itemId": item_id, "isSection": "false", "type": "agenda"})
    except Exception as e:  # noqa: BLE001
        log(f"item view {meeting_id}/{item_id}: {e}")
        return None
    s = BeautifulSoup(r.text, "lxml")
    links = [(a.get_text(" ", strip=True), a["href"]) for a in s.find_all("a", href=True)
             if "DownloadFile" in a["href"]]
    if not links:
        return None
    pick = [h for t, h in links if re.search(r"staff report", t, re.I)] or [links[0][1]]
    return "https://docs.osceola.org" + pick[0] if pick[0].startswith("/") else pick[0]


# --------------------------------------------------------------------------- Accela
def wf_hearings(d):
    """Hearings from the Accela workflow tasks 'Planning Commission' / 'Board of County Commissioners'."""
    out = []
    for t in d.get("workflow") or []:
        name = t.get("task") or ""
        if re.fullmatch(r"\s*Planning Commission\s*", name, re.I):
            body = "Planning Commission"
        elif re.search(r"Board of County Commissioners|^\s*BCC|^\s*BOCC", name, re.I):
            body = "BOCC"
        else:
            continue
        for e in t.get("entries") or []:
            st = e.get("status")
            due, done = e.get("due"), e.get("date")
            if not st and not due:
                continue
            if st is None or re.search(r"scheduled", st or "", re.I):
                if due:
                    out.append({"body": body, "date": due, "result": None, "_src": "wf-sched"})
                continue
            # the "marked" date lags the meeting by a day or so; the due date is the meeting
            # date when it is not later than the marked date
            date = done or due
            if due and done and due <= done and (dt.date.fromisoformat(done) - dt.date.fromisoformat(due)).days <= 7:
                date = due
            res = classify(st, body)
            out.append({"body": body, "date": date, "result": res, "_src": "wf", "_raw": st})
    return out


def merge_hearings(agenda_hs, wf_hs):
    hs = [dict(h) for h in agenda_hs]
    for w in wf_hs:
        near = [h for h in hs if h["body"] == w["body"] and
                abs((dt.date.fromisoformat(h["date"]) - dt.date.fromisoformat(w["date"])).days) <= 10]
        if near:
            h = min(near, key=lambda h: abs((dt.date.fromisoformat(h["date"]) -
                                             dt.date.fromisoformat(w["date"])).days))
            if not h.get("result") and w.get("result"):
                h["result"] = w["result"]
            continue
        if w["_src"] == "wf-sched":
            # a scheduled date superseded by a later agenda appearance of the same body is stale
            if any(h["body"] == w["body"] and h["date"] > w["date"] for h in hs):
                continue
        else:
            # staff often mark the hearing task weeks after the meeting: attach the outcome to
            # the latest agenda hearing of that body held on or before the marked date
            prior = [h for h in agenda_hs if h["body"] == w["body"] and h["date"] <= w["date"]]
            if prior:
                h = next(x for x in hs if x["body"] == w["body"] and x["date"] == prior[-1]["date"])
                if not h.get("result") and w.get("result"):
                    h["result"] = w["result"]
                continue
        hs.append(dict(w))
    ded = {}
    for h in hs:
        k = (h["body"], h["date"])
        if k not in ded or (h.get("result") and not ded[k].get("result")):
            ded[k] = h
    out = sorted(ded.values(), key=lambda h: (h["date"], h["body"] != "Planning Commission"))
    return [{"body": h["body"], "date": h["date"], "result": h.get("result")} for h in out]


def status_of(raw, hearings, today):
    r = (raw or "").lower()
    bcc = [h for h in hearings if h["body"] == "BOCC" and h["date"] <= today and h.get("result")]
    final = [h for h in bcc if h["result"] in ("Approved", "Denied")]
    if re.search(r"withdr|void|expired|cancel|closed.*incomplete|incomplete.*closed|abandon", r):
        return "Withdrawn", None
    if re.search(r"\bdenied\b", r):
        return "Denied", (final[-1]["date"] if final else None)
    if re.fullmatch(r"\s*(approved|adopted|complete[d]?|closed)\s*", r):
        return "Approved", (final[-1]["date"] if final else None)
    if any(h.get("result") == "Withdrawn" for h in hearings):
        return "Withdrawn", None
    if final:
        return final[-1]["result"], final[-1]["date"]
    future = [h for h in hearings if h["date"] > today]
    past = [h for h in hearings if h["date"] <= today]
    if future:
        return "Hearing Scheduled", None
    if past and past[-1].get("result") == "Continued":
        return "Continued", None
    if past and past[-1].get("result") is None:
        return "Hearing Scheduled", None   # hearing held, outcome not yet posted
    return "Filed", None


def search_rows(aca, tval, start, end):
    rows, single = aca.search(rtype=tval, start=start, end=end)
    if single is not None:
        d = C.parse_detail(single)
        rows = [{"app_num": d.get("app_num"), "type": d.get("type"), "date": None,
                 "status": d.get("status_raw"), "url": aca.last_url, "_html": single}]
    return rows


def fetch(since, refresh):
    t0 = time.time()
    today = dt.date.today().isoformat()
    refresh = [r.strip().upper() for r in (refresh or []) if r and r.strip()]
    acas = {"Zoning": C.ACA(AGENCY, "Zoning"), "Planning": C.ACA(AGENCY, "Planning")}
    for a in acas.values():
        a.agency_code = AGENCY
    try:
        avail = {m: dict(a.record_types()) for m, a in acas.items()}
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Osceola Accela unreachable: {e}") from e

    rows = {}
    start, end = dt.date.fromisoformat(since), dt.date.today()
    for mod, tval, _cat in TYPES:
        if tval not in avail[mod]:
            log(f"record type no longer offered: {tval}")
            continue
        for a_, b_ in C.split_range(start, end, 400):
            try:
                rs = search_rows(acas[mod], tval, a_, b_)
            except C.Blocked:
                raise
            except Exception as e:  # noqa: BLE001
                log(f"search failed {tval}: {e}")
                continue
            log(f"Accela {avail[mod][tval]}: {len(rs)} rows {a_}..{b_}")
            for r in rs:
                num = (r.get("app_num") or "").upper()
                if not num or num.startswith("OSCTEMP") or re.match(r"^\d{2}TMP-", num):
                    continue
                if r.get("date") and r["date"] < since:
                    continue
                r["_mod"] = mod
                rows.setdefault(num, r)
    for num in refresh:
        if num in rows:
            continue
        mod = module_of(num)
        try:
            rs, single = acas[mod].search(permit_no=num)
        except C.Blocked:
            raise
        except Exception as e:  # noqa: BLE001
            log(f"refresh search failed {num}: {e}")
            continue
        if single is not None:
            rows[num] = {"app_num": num, "_html": single, "url": acas[mod].last_url, "date": None,
                         "_refresh": True, "_mod": mod}
        else:
            hit = next((r for r in rs if (r.get("app_num") or "").upper() == num), None)
            if hit:
                hit.update({"_refresh": True, "_mod": mod})
                rows[num] = hit
            else:
                log(f"refresh id not found in Accela: {num}")
    log(f"{len(rows)} candidate records from Accela")

    try:
        agenda = scan_agendas(since, refresh)
    except Exception as e:  # noqa: BLE001
        log(f"OnBase agendas unavailable ({e}); hearings from Accela workflow only")
        agenda = {}

    out = []
    for num, r in sorted(rows.items()):
        try:
            aca = acas[r["_mod"]]
            if r.get("url") and "CapDetail" in r["url"]:
                d = aca.detail(r["url"], workflow=False)
            elif r.get("_html"):
                d = aca.detail_from_html(r["_html"], r.get("url") or aca.home_url, workflow=False)
            else:
                log(f"{num}: no detail link")
                continue
            try:
                d["workflow"] = aca.processing(d["url"])
            except Exception as e:  # noqa: BLE001
                log(f"{num}: workflow failed: {e}")
                d["workflow"] = []
            rec = build(num, r, d, agenda.get(num, []), today)
            if rec is None:
                continue
            if not r.get("_refresh") and rec["filed_date"] and rec["filed_date"] < since:
                continue
            out.append(rec)
        except C.Blocked:
            raise
        except Exception as e:  # noqa: BLE001
            log(f"{num}: skipped ({type(e).__name__}: {e})")
    log(f"{len(out)} records in {time.time() - t0:.0f}s")
    return out


def build(num, row, d, apps, today):
    cat = category_of(num)
    rd = d.get("record_details") or {}
    pdesc = rd.get("Project Description") or ""
    plines = [x.strip() for x in pdesc.split("\n") if x.strip()]
    if cat == "Comp Plan Amendment":
        mapc = (C.asi_get(d, r"^Map Change") or "").lower()
        textc = (C.asi_get(d, r"^Text Change") or "").lower()
        if mapc == "no" and textc == "yes":
            return None   # text-only amendment
    apps = sorted(apps, key=lambda a: a["date"])
    latest = apps[-1] if apps else None
    title = latest["title"] if latest else ""
    agenda_text = " ".join(a["text"] for a in apps[-2:]) if apps else ""

    # project name / description
    pname = clean(row.get("project_name")) or (clean(plines[0]) if len(plines) > 1 else None)
    desc = clean(row.get("description"))
    if not desc and plines:
        desc = clean(" ".join(plines[1:] if len(plines) > 1 else plines))
    if title:
        t = re.sub(r"^Adoption of Ordinance #?\S+ for \S+:?\s*An Ordinance of the Board of County "
                   r"Commissioners of Osceola County, Florida,?\s*", "", title, flags=re.I)
        t = re.split(r";\s*(?:amending|providing)", t)[0]
        if not desc or len(desc) < 25 or desc.lower().startswith("adoption of ordinance"):
            desc = clean(t)
    if desc and desc.lower().startswith("adoption of ordinance"):
        t = re.sub(r"^Adoption of Ordinance #?\S+ for \S+:?\s*An Ordinance of the Board of County "
                   r"Commissioners of Osceola County, Florida,?\s*", "", desc, flags=re.I)
        desc = clean(re.split(r";\s*(?:amending|providing)", t)[0])
    if desc and len(desc) > 400:
        desc = desc[:397].rstrip() + "..."

    # applicant
    info = {}
    for a in apps:
        for k, v in a["info"].items():
            if v:
                info[k] = v
    applicant = None
    for ln in plines:
        m = re.match(r"(.+?)\s*\((?:Applicants?|Applicant/Owner|Applicants?/Owners?)\)\s*$", ln, re.I)
        if m:
            applicant = clean(m.group(1))
            break
    applicant = applicant or info.get("applicant") or C.first_contact(d, r"applicant") \
        or C.first_contact(d, r"agent") or info.get("agent") or info.get("owner")
    if not applicant:
        for ln in plines:
            m = re.match(r"(.+?)\s*\((?:Agent|Owners?)\)\s*$", ln, re.I)
            if m:
                applicant = clean(m.group(1))
                break

    # acres
    acres = C.to_float(C.asi_get(d, r"^Total Acre", r"Acreage", r"^Acres?$"))
    if acres is None:
        acres = acres_of(title, agenda_text, pdesc, row.get("description"), row.get("project_name"))

    # zoning / FLU
    fz = tz = ff = tf = None
    texts = [title, pdesc, row.get("description") or "", row.get("project_name") or ""]
    if cat == "Comp Plan Amendment":
        for t in texts:
            ff, tf = flu_from_to(t)
            if tf:
                break
        ff = ff or clean(C.asi_get(d, r"Current Land Use Designation", r"Current Future Land Use"))
        tf = tf or clean(C.asi_get(d, r"Proposed Land Use Designation", r"Proposed Future Land Use",
                                   r"Requested Land Use"))
    else:
        for t in texts:
            fz, tz = from_to(t)
            if fz or tz:
                break
        if cat == "PD" and not tz:
            tz = "PD"

    # hearings
    ag_hs = []
    for a in apps:
        ag_hs.append({"body": a["body"], "date": a["date"], "result": a["result"]})
        rec = pc_rec_result(a["info"].get("pc_rec")) if a["body"] == "BOCC" else None
        if rec:
            prior = [h for h in ag_hs if h["body"] == "Planning Commission" and h["date"] < a["date"]]
            if prior and not prior[-1]["result"]:
                prior[-1]["result"] = rec
        md = a["info"].get("meeting_dates")
        if md:   # "Planning Commission - October 01, 2026; Board of County Commissioners - October 19, 2026"
            for part in re.split(r"[;/:,]\s*(?=(?:DRC|Planning|PC|Board|BoCC|BCC))", md):
                dte = text_date(part)
                if not dte or dte <= today:
                    continue   # past dates come from the agendas actually held (cases get continued)
                if re.search(r"Planning|\bPC\b", part):
                    ag_hs.append({"body": "Planning Commission", "date": dte, "result": None})
                elif re.search(r"Board|BoCC|BCC", part):
                    ag_hs.append({"body": "BOCC", "date": dte, "result": None})
    hearings = merge_hearings(ag_hs, wf_hearings(d))

    raw = d.get("status_raw") or row.get("status")
    status, decision = status_of(raw, hearings, today)
    if status in ("Approved", "Denied") and not decision:
        bcc = [h for h in hearings if h["body"] == "BOCC" and h.get("result") in ("Approved", "Denied")]
        decision = bcc[-1]["date"] if bcc else None
    status_raw = raw
    if latest and latest.get("result_raw") and latest["body"] == "BOCC":
        status_raw = f"{raw} (BCC {latest['date']}: {latest['result_raw']})" if raw else latest["result_raw"]

    docs = None
    for a in reversed(apps):
        docs = staff_report_link(a["meeting_id"], a["item_id"])
        if docs:
            break

    filed = row.get("date") or C.earliest_workflow_date(d)
    if not filed and apps:
        filed = apps[0]["date"]
    rec = {
        "jurisdiction": JUR, "county": COUNTY, "app_num": num, "category": cat,
        "app_type": d.get("type") or row.get("type") or C.asi_get(d, r"^Type$"),
        "project_name": pname, "description": desc, "applicant": applicant,
        "parcel_ids": list(dict.fromkeys(p.strip() for p in d.get("parcels") or [] if p and p.strip())),
        "address": C.one_line(d.get("address") or C.clean_address(row.get("address"))),
        "acres": acres, "from_zoning": fz, "to_zoning": tz, "from_flu": ff, "to_flu": tf,
        "filed_date": filed, "status": status, "status_raw": status_raw, "hearings": hearings,
        "decision_date": decision, "url": d.get("url") or row.get("url"), "docs_url": docs,
        "lat": None, "lon": None, "source": SOURCE,
    }
    pdtype = C.asi_get(d, r"^Type$")
    if cat == "PD" and pdtype:
        rec["app_type"] = f"Planned Development ({pdtype})"
    scale = C.asi_get(d, r"^CP Scale")
    if cat == "Comp Plan Amendment" and scale:
        rec["app_type"] = f"Comprehensive Plan Amendment ({scale} Scale)"
    if rec["address"] and re.fullmatch(r"(?:0,?\s*)?(?:[A-Z ]+,?\s*)?(?:FL\s*)?(?:United States)?", rec["address"]):
        rec["address"] = None
    return C.finalize(rec, {"jurisdiction": JUR, "county": COUNTY, "source": SOURCE})


if __name__ == "__main__":
    since = sys.argv[1] if len(sys.argv) > 1 else (dt.date.today() - dt.timedelta(days=14)).isoformat()
    refresh = sys.argv[2].split(",") if len(sys.argv) > 2 and sys.argv[2] else []
    t0 = time.time()
    recs = fetch(since, refresh)
    print(json.dumps(recs, indent=2, ensure_ascii=False))
    from collections import Counter
    log(f"{len(recs)} records in {time.time() - t0:.0f}s; status={dict(Counter(r['status'] for r in recs))}; "
        f"category={dict(Counter(r['category'] for r in recs))}")
