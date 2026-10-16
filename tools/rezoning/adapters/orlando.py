#!/usr/bin/env python3
"""City of Orlando rezoning / PD / future land use map amendment adapter.

Sources (all public, no login):
  1. Orlando open data (Socrata) "Planning Applications" (data.cityoforlando.net, dataset bhxy-4rji),
     a daily export of the city's planning case system: every application with number, type,
     application date, status, applicant, address, parcel ID and comments.
       ZONyyyy-nnnnn = Zoning (rezoning, PD, PD amendment, initial zoning after annexation)
       GMPyyyy-nnnnn = Growth Management Plan amendment (kept only when it changes a future
                       land use designation; text amendments and reports are skipped)
     Cases appear here when the application is filed.
  2. Municipal Planning Board (MPB) records in the city's Laserfiche WebLink portal
     (portal.laserfiche.com, repo r-b158fc8c): one folder per MPB meeting with the agenda and one
     staff report per case (case numbers in the file names), plus the MPB minutes. The agenda /
     minutes give the hearing date, applicant, owner, location with acreage (+/- acres), the
     request ("Rezoning from R-1A to R-3A ...") and the board's action (recommend approval,
     denial, deferral).
  3. City Council agendas and minutes on eScribe (pub-orlando.escribemeetings.com): acceptance of
     the MPB actions, ordinance first reading and second reading / adoption (the final decision),
     with motion results. Ordinance titles name the case numbers and often the acreage.
Notes:
  * Parcel IDs are returned exactly as the open-data record shows them. Orlando's export uses
    range-township-section order ("29-22-25-1536-03-070" = Orange County Property Appraiser
    parcel 292225153603070), which is the reverse of the section-township-range order printed
    in staff reports and used by Orange County ("25-22-29-1536-03-070").
  * The open-data record has no public detail page; `url` points to the MPB staff report when
    there is one, otherwise to the open-data record (JSON).
Requires pypdf.
"""
import datetime as dt
import html as _html
import io
import json
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

try:
    import pypdf
except ImportError:  # pragma: no cover
    pypdf = None

JURISDICTIONS = ["City of Orlando"]

UA = "Mozilla/5.0 (X11; Linux x86_64) ELC-RezoningTracker/1.0 (+https://thedirtdog.com)"
SODA = "https://data.cityoforlando.net/resource/bhxy-4rji.json"
LF = "https://portal.laserfiche.com/Portal"
LF_REPO = "r-b158fc8c"
MPB_AGENDA_ROOT = 1032838      # "Municipal Planning Board (MPB) - Meeting Agenda" (year / meeting folders)
MPB_MINUTES_ROOT = 1032842     # "Municipal Planning Board (MPB) - Meeting Minutes" (year folders)
ESC = "https://pub-orlando.escribemeetings.com"
SOURCE = "Orlando open data Planning Applications + MPB records (Laserfiche) + City Council eScribe"
SLEEP = 0.4
HEARING_LOOKBACK_DAYS = 45     # MPB / Council records scanned this far before the earliest case date
ESCRIBE_START = "2025-02-01"   # City Council moved to eScribe in Feb 2025

_s = requests.Session()
_s.headers.update({"User-Agent": UA})

MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
CASE_RX = re.compile(r"\b(ZON|GMP)\s?(\d{4})\s?-\s?(\d{5})\b")
ANY_CASE_RX = re.compile(r"\b(?:ZON|GMP|CUP|MPL|VAR|LDC|SNC|ABN|ANX|SUB|MOD|DRI|HPB|ARB|DET)\d{4}-\d{5}\b")



def _sec_twp_rng(pid):
    m = re.fullmatch(r"(\d{2})-(\d{2})-(\d{2})-(\w{4})-(\w{2})-(\w{3})", pid)
    if not m:
        return pid
    rng, twp, sec, sub, blk, lot = m.groups()
    return f"{sec}-{twp}-{rng}-{sub}-{blk}-{lot}"

def log(*a):
    print("[orlando]", *a, file=sys.stderr)


# ------------------------------------------------------------------ http
def _req(method, url, tries=3, timeout=90, sleep=SLEEP, **kw):
    last = None
    for i in range(tries):
        try:
            r = _s.request(method, url, timeout=timeout, **kw)
            if r.status_code >= 500 or r.status_code == 429:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            time.sleep(sleep)
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"{method} failed {url}: {last}")


def clean(v):
    if v is None:
        return None
    v = re.sub(r"\s+", " ", str(v)).strip(" ,;")
    return v or None


def short(v, n=300):
    v = clean(v)
    if v and len(v) > n:
        v = v[:n - 3].rstrip() + "..."
    return v


def long_date(s):
    m = re.search(rf"({MONTHS})\s+(\d{{1,2}}),?\s*(\d{{4}})", s or "")
    if not m:
        return None
    return dt.datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y").date().isoformat()


def cases_in(text):
    out = []
    for m in CASE_RX.finditer(text or ""):
        c = f"{m.group(1)}{m.group(2)}-{m.group(3)}"
        if c not in out:
            out.append(c)
    return out


def acres_in(text):
    t = text or ""
    m = re.search(r"\(\s*(?:±|\+/-)\s*([\d,]*\.?\d+)\s*(?:acres?|ac\.?)\s*\)", t, re.I) or \
        re.search(r"Comprised of (?:Approximately |About )?([\d,]*\.?\d+)\s+Acres", t, re.I) or \
        re.search(r"(?:±|\+/-)\s*([\d,]*\.?\d+)\s*acres?", t, re.I)
    if not m:
        return None
    try:
        return round(float(m.group(1).replace(",", "")), 2)
    except ValueError:
        return None


# ------------------------------------------------------------------ 1. Socrata
def soda(where):
    out, off = [], 0
    while True:
        d = _req("GET", SODA, params={"$where": where, "$order": "application_date", "$limit": 1000,
                                      "$offset": off}).json()
        out.extend(d)
        if len(d) < 1000:
            return out
        off += 1000


GMP_MAP_RX = re.compile(r"future land use|land use designation|\bFLU\b|FLUM", re.I)


def in_scope(row):
    num = row.get("application_number") or ""
    text = " ".join(filter(None, [row.get("application_name"), row.get("application_comments")]))
    if num.startswith("ZON"):
        return True
    if num.startswith("GMP"):
        if re.search(r"Growth Indicators|Capacity Availability|Capital Improvement|\bCIE\b|Community Development "
                     r"District|\bCDD\b|\bCRA\b|Outlook Plan|Annual report|Urbanized Disturbed|Subarea Policy|"
                     r"Policy [A-Z]{1,3}[ -]?\d", text, re.I) and not GMP_MAP_RX.search(text):
            return False
        if GMP_MAP_RX.search(text):
            return True
        # site-specific GMP amendment (has a parcel or address), e.g. "GMP Amendment to PUB REC INST Rezone P"
        return bool(row.get("parcel_id") or re.match(r"\d", row.get("address") or ""))
    return False


# ------------------------------------------------------------------ 2. Laserfiche (MPB)
def lf_list(folder_id):
    r = _req("POST", f"{LF}/FolderListingService.aspx/GetFolderListing2",
             json={"repoName": LF_REPO, "folderId": folder_id, "getNewListing": True, "start": 0, "end": 500,
                   "sortColumn": "", "sortAscending": True})
    d = r.json().get("data") or {}
    return [{"id": e["entryId"], "name": e.get("name") or "", "type": e.get("type"),
             "ext": e.get("extension")} for e in d.get("results") or []]


def lf_pdf_text(doc_id):
    r = _req("GET", f"{LF}/ElectronicFile.aspx", params={"docid": doc_id, "dbid": 0, "repo": LF_REPO}, timeout=180)
    rdr = pypdf.PdfReader(io.BytesIO(r.content))
    return "\n".join((p.extract_text() or "") for p in rdr.pages)


def lf_url(doc_id):
    return f"{LF}/DocView.aspx?id={doc_id}&repo={LF_REPO}"


ITEM_HDR = re.compile(r"^\s*(\d{1,2})\.\s+([A-Z0-9][A-Z0-9 .,&'’/()#\-–:]{3,})\s*$", re.M)


def parse_mpb_items(text):
    """Split an MPB agenda / minutes PDF into numbered item blocks.
    Returns [{num, title, section, text}] (an item moved to the regular agenda appears twice)."""
    t = text.replace("\r", "")
    fl = re.M | re.I
    sec_marks = [(m.start(), "consent") for m in re.finditer(r"^\s*Consent Agenda\s*$", t, fl)] + \
                [(m.start(), "regular") for m in re.finditer(r"^\s*Regular Agenda\s*$", t, fl)] + \
                [(m.start(), "end") for m in re.finditer(r"^\s*(?:Other Business|Adjournment)\s*$", t, fl)]
    sec_marks.sort()
    heads = [m for m in ITEM_HDR.finditer(t)]
    out = []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(t)
        sec = None
        for pos, name in sec_marks:
            if pos <= m.start():
                sec = name
        nxt = [pos for pos, _ in sec_marks if pos > m.start()]
        if nxt:
            end = min(end, nxt[0])
        if sec in (None, "end"):
            continue
        out.append({"num": m.group(1), "title": clean(m.group(2)), "section": sec, "text": t[m.end():end]})
    return out


def item_details(blk):
    t = blk["text"]
    flat = re.sub(r"\s+", " ", t)
    d = {"title": blk["title"], "acres": acres_in(flat)}
    m = re.search(r"Applicant(?:/Owner)?:\s*(.+?)(?:\s+Owner:|\s+Location:)", flat)
    d["applicant"] = clean(m.group(1)) if m else None
    m = re.search(r"Owner:\s*(.+?)\s+Location:", flat)
    d["owner"] = clean(m.group(1)) if m else None
    m = re.search(r"Location:\s*(.+?)(?:\(\s*(?:±|\+/-)|\s+District:)", flat)
    d["location"] = clean(m.group(1)) if m else None
    reqs = {}
    for cm in re.finditer(r"\b((?:ZON|GMP)\d{4}-\d{5})\s*\*{0,2}\s*(.+?)(?=\b(?:ZON|GMP|CUP|MPL|VAR|LDC|SNC|ABN|ANX|SUB|"
                          r"MOD|DRI)\d{4}-\d{5}|Recommended Action|Project Planner|Note:|$)", flat):
        reqs[cm.group(1)] = clean(cm.group(2))
    d["requests"] = reqs
    d["moved_to_regular"] = bool(re.search(r"moved to the Regular Agenda", flat, re.I))
    m = re.search(r"Recommended Action:\s*(.+?)(?:\.|$)", flat)
    d["staff_rec"] = clean(m.group(1)) if m else None
    return d


def mpb_action(blk, consent_passed):
    """Board action for one item block of the minutes."""
    flat = re.sub(r"\s+", " ", blk["text"])
    title = blk.get("title") or ""
    if blk["section"] == "consent":
        if re.search(r"moved to the Regular Agenda|PULLED TO THE REGULAR", title + " " + flat, re.I):
            return None
        if re.search(r"WITHDRAW", title, re.I):
            return "Withdrawn"
        if re.search(r"DEFERRAL|DEFERRED|CONTINU", title, re.I):
            return "Continued"
        if re.search(r"\b(?:deferred|continued|withdrawn)\b", flat, re.I) and not re.search(r"Recommended Action", flat):
            return "Continued"
        if consent_passed:
            rec = re.search(r"Recommended Action:\s*(\w+)", flat)
            if re.search(r"RECOMMENDED DENIAL", title, re.I) or (rec and rec.group(1).lower().startswith("den")):
                return "Recommended Denial"
            return "Recommended Approval"
        return None
    m = None
    for m in re.finditer(r"moved\s+(?:to\s+)?(APPROVAL|DENIAL|DEFERRAL|CONTINUANCE|CONTINUE|DEFER|DENY|APPROVE|"
                         r"WITHDRAWAL)\b(.{0,400}?)(PASSED|FAILED|CARRIED)", flat, re.I):
        pass
    if not m:
        if re.search(r"withdr[ae]w", flat, re.I):
            return "Withdrawn"
        return None
    verb, res = m.group(1).upper(), m.group(3).upper()
    if res == "FAILED":
        return f"Motion for {verb.lower()} failed"
    if verb.startswith(("DEFER", "CONTIN")):
        return "Continued"
    if verb.startswith("DEN"):
        return "Recommended Denial"
    if verb.startswith("WITHDR"):
        return "Withdrawn"
    return "Recommended Approval"


def mpb_records(start, end):
    """{case: {"hearings": [...], "info": {...}, "staff_report": url}} from MPB folders between start and end."""
    out = {}
    years = range(int(start[:4]), int(end[:4]) + 1)
    try:
        ag_years = {e["name"]: e["id"] for e in lf_list(MPB_AGENDA_ROOT) if e["type"] == 0}
        mn_years = {e["name"]: e["id"] for e in lf_list(MPB_MINUTES_ROOT) if e["type"] == 0}
    except Exception as e:  # noqa: BLE001
        log(f"Laserfiche unavailable: {e}")
        return out
    meetings = {}
    for y in years:
        fid = ag_years.get(str(y))
        if not fid:
            continue
        for e in lf_list(fid):
            try:
                d = dt.datetime.strptime(e["name"].strip(), "%m/%d/%Y").date().isoformat()
            except ValueError:
                continue
            if start <= d <= end:
                meetings[d] = {"folder": e["id"]}
    minutes = {}
    for y in years:
        fid = mn_years.get(str(y))
        if not fid:
            continue
        for e in lf_list(fid):
            d = long_date(e["name"])
            if d:
                minutes[d] = e["id"]
    for d in sorted(meetings):
        try:
            ents = lf_list(meetings[d]["folder"])
        except Exception as e:  # noqa: BLE001
            log(f"MPB {d}: listing failed: {e}")
            continue
        agenda = next((e for e in ents if re.search(r"Meeting Agenda", e["name"])), None)
        reports = {}
        for e in ents:
            if re.search(r"Staff Report", e["name"], re.I):
                for c in cases_in(e["name"]):
                    reports[c] = e["id"]
        texts = []
        if agenda:
            try:
                texts.append(("agenda", lf_pdf_text(agenda["id"])))
            except Exception as e:  # noqa: BLE001
                log(f"MPB {d}: agenda PDF failed: {e}")
        mid = minutes.get(d)
        if mid:
            try:
                texts.append(("minutes", lf_pdf_text(mid)))
            except Exception as e:  # noqa: BLE001
                log(f"MPB {d}: minutes PDF failed: {e}")
        details, actions = {}, {}
        for kind, txt in texts:
            blocks = parse_mpb_items(txt)
            consent_passed = bool(re.search(r"moved\s+APPROVAL\s+of\s+the\s+CONSENT\s+AGENDA.{0,300}?PASSED",
                                            re.sub(r"\s+", " ", txt), re.I))
            for b in blocks:
                cs = cases_in(b["text"])
                if not cs:
                    continue
                det = item_details(b)
                for c in cs:
                    if c not in details or (det.get("requests") and not details[c].get("requests")):
                        details[c] = det
                if kind == "minutes":
                    act = mpb_action(b, consent_passed)
                    if act:
                        for c in cs:
                            actions[c] = act
        here = sorted(set(reports) | set(details))
        for c in here:
            rec = out.setdefault(c, {"hearings": [], "info": {}, "staff_report": None})
            rec["hearings"].append({"body": "Municipal Planning Board", "date": d, "result": actions.get(c)})
            if details.get(c):
                rec["info"] = details[c]
            if reports.get(c):
                rec["staff_report"] = lf_url(reports[c])
            rec["meeting_folder"] = meetings[d]["folder"]
        log(f"MPB {d}: ZON/GMP cases {', '.join(here) or 'none'}")
    return out


# ------------------------------------------------------------------ 3. eScribe (City Council)
def council_meetings(start, end):
    r = _req("POST", f"{ESC}/MeetingsCalendarView.aspx/GetCalendarMeetings",
             json={"calendarStartDate": start, "calendarEndDate": end},
             headers={"Content-Type": "application/json; charset=utf-8"})
    out = []
    for m in r.json().get("d") or []:
        if not re.search(r"City Council Meeting|Special City Council", m.get("MeetingType") or "", re.I):
            continue
        links = {x.get("Title"): x.get("Url") for x in m.get("MeetingDocumentLink") or []}
        out.append({"id": m["ID"], "date": m["StartDate"][:10].replace("/", "-"), "type": m.get("MeetingType"),
                    "minutes": any("Minutes (HTML)" in (t or "") for t in links),
                    "agenda": any("HTML" in (t or "") for t in links)})
    return sorted(out, key=lambda m: m["date"])


def parse_meeting(html):
    soup = BeautifulSoup(html, "lxml")
    items, section = [], None
    for it in soup.select("div.AgendaItem"):
        cnt = it.select_one(".AgendaItemCounter")
        ttl = it.select_one(".AgendaItemTitle")
        counter = clean(cnt.get_text(" ")) if cnt else ""
        title = clean(ttl.get_text(" ")) if ttl else ""
        if counter and "." not in counter.strip(".") and counter.strip(".").isdigit():
            section = title
        atts = [clean(a.get_text(" ")) for a in it.select(".AgendaItemAttachment a")]
        hrefs = [a.get("href") for a in it.select(".AgendaItemAttachment a")]
        motion = clean(" ".join(x.get_text(" ") for x in it.select(".MotionText")))
        result = clean(" ".join(x.get_text(" ") for x in it.select(".MotionResult")))
        minutes = clean(" ".join(x.get_text(" ") for x in it.select(".AgendaItemMinutes")))
        items.append({"counter": counter, "title": title, "section": section, "attachments": atts,
                      "hrefs": hrefs, "motion": motion, "result": result, "minutes": minutes})
    return items


def council_result(item, has_minutes):
    """(result, flag) for one council agenda item; flag 'final' marks the deciding vote, 'mpb' the MPB acceptance."""
    t, mo, res, mi = item["title"] or "", item["motion"] or "", item["result"] or "", item["minutes"] or ""
    sec = item["section"] or ""
    if re.match(r"\s*DELETED", t, re.I):
        return "Removed from agenda", None
    if not has_minutes or not (res or mi):
        return None, None
    # the clerk's disposition line: "Adopted, filed and documentary #...", "Deferred ...", "Denied, filed ..."
    fm = re.search(r"\b(Approved|Adopted|Accepted|Denied|Deferred|Continued|Withdrawn|Tabled|Postponed|Received)\b,?"
                   r"\s+(?:and\s+)?filed", mi)
    word = fm.group(1).lower() if fm else None
    if not word:
        if re.search(r"\b(?:deferred|continued|postponed|tabled)\s+(?:to|until)\b", mi, re.I):
            word = "continued"
        elif re.search(r"\bwithdrawn\b", mi, re.I):
            word = "withdrawn"
        elif re.search(r"\b(?:Defeated|Failed)\b", res) and not re.search(r"\bCarried\b", res):
            word = "denied"
        elif re.search(r"\bCarried\b", res):
            word = "approved"
    if not word:
        return None, None
    if word in ("deferred", "continued", "tabled", "postponed"):
        return "Continued", None
    if word == "withdrawn":
        return "Withdrawn", None
    if re.search(r"Minutes.*Municipal Planning Board|Municipal Planning Board.*Meeting of", t, re.I):
        return ("MPB actions accepted" if word != "denied" else "MPB actions not accepted"), "mpb"
    if re.search(r"Appeal", sec + " " + t, re.I):
        if word in ("received", "accepted"):
            return "Appeal received", None
        return ("Appeal denied" if word == "denied" else "Appeal granted"), None
    if re.search(r"Quasi-Judicial", sec, re.I):
        return ("Hearing officer recommended order adopted" if word in ("adopted", "approved", "accepted")
                else "Hearing officer recommended order rejected"), None
    second = bool(re.search(r"2nd Read|Second Read", sec, re.I) or re.match(r"\s*Adopt Ordinance", mo)
                  or word == "adopted")
    first = bool(re.search(r"1st Read|First Read", sec, re.I) or re.search(r"on first reading", mo, re.I))
    if word == "denied":
        return "Denied", ("final" if (first or second) else None)
    if second:
        return "Approved (ordinance adopted)", "final"
    if first:
        return "Approved on first reading", None
    return word.capitalize(), None


def council_records(start, end, today):
    out = {}
    try:
        meetings = council_meetings(start, end)
    except Exception as e:  # noqa: BLE001
        log(f"eScribe unavailable: {e}")
        return out
    for m in meetings:
        kind = "PostMinutes" if (m["minutes"] and m["date"] <= today) else "Agenda"
        url = f"{ESC}/Meeting.aspx?Id={m['id']}&Agenda={kind}&lang=English"
        try:
            items = parse_meeting(_req("GET", url, timeout=120).text)
        except Exception as e:  # noqa: BLE001
            log(f"Council {m['date']}: {e}")
            continue
        n = 0
        for it in items:
            if re.search(r"Municipal Planning Board", it["title"] or "") and long_date(it["title"]):
                # "Accept the Meeting Minutes and Approve the Actions of the MPB for the Meeting of <date>"
                res, flag = council_result(it, kind == "PostMinutes")
                exc = it["title"].split("EXCEPT", 1)[1] if "EXCEPT" in it["title"] else ""
                out.setdefault("_mpb", {})[long_date(it["title"])] = {
                    "date": m["date"], "title": it["title"], "section": it["section"], "result": res,
                    "flag": "mpb", "url": url, "docs": None, "except": cases_in(exc)}
                continue
            cs = cases_in(it["title"]) or cases_in(" ".join(it["attachments"]))
            if not cs:
                continue
            res, flag = council_result(it, kind == "PostMinutes")
            if res == "Removed from agenda":
                continue
            docs = [f"{ESC}/{h}" for h, a in zip(it["hrefs"], it["attachments"])
                    if h and re.search(r"Staff Report|Ordinance|1st Read|2nd Read|Reading|ZON|GMP", a or "", re.I)]
            for c in cs:
                rec = out.setdefault(c, [])
                rec.append({"date": m["date"], "title": it["title"], "section": it["section"], "result": res,
                            "flag": flag, "url": url, "docs": docs[0] if docs else None})
                n += 1
        log(f"Council {m['date']} ({kind}): {n} case references")
    return out


# ------------------------------------------------------------------ fetch
def fetch(since, refresh):
    if pypdf is None:
        raise RuntimeError("pypdf is required for the City of Orlando adapter")
    today = dt.date.today().isoformat()
    refresh = list(dict.fromkeys(c for r in (refresh or []) for c in cases_in(r.upper())))

    where = f"(application_number like 'ZON%' OR application_number like 'GMP%') AND application_date >= '{since}'"
    try:
        rows = soda(where)
        if refresh:
            ids = ",".join(f"'{r}'" for r in refresh)
            rows += soda(f"application_number in ({ids})")
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Orlando open data unreachable: {e}")
    by_num = {}
    for r in rows:
        by_num[r["application_number"]] = r
    cands = {n: r for n, r in by_num.items() if in_scope(r) and
             ((r.get("application_date") or "")[:10] >= since or n in refresh)}
    log(f"open data: {len(rows)} ZON/GMP rows, {len(cands)} in scope")
    if not cands:
        return []

    first = min((r.get("application_date") or today)[:10] for r in cands.values())
    start = (dt.date.fromisoformat(first) - dt.timedelta(days=HEARING_LOOKBACK_DAYS)).isoformat()
    end = (dt.date.today() + dt.timedelta(days=90)).isoformat()
    mpb = mpb_records(start, end)
    council = council_records(max(start, ESCRIBE_START), end, today)

    out = []
    for n, r in sorted(cands.items()):
        try:
            cc = list(council.get(n, []))
            for h in (mpb.get(n) or {}).get("hearings", []):
                acc = council.get("_mpb", {}).get(h["date"])
                if not acc or h["result"] == "Continued":
                    continue
                acc = dict(acc)
                if n in acc.get("except", []):
                    acc["result"], acc["flag"] = "MPB action appealed (excluded from acceptance)", None
                cc.append(acc)
            out.append(build(n, r, mpb.get(n), cc, today))
        except Exception as e:  # noqa: BLE001
            log(f"{n}: skipped ({e})")
    for r in refresh:
        if r not in by_num:
            log(f"refresh id {r}: not in open data")
    return out


def build(n, r, mp, cc, today):
    mp = mp or {}
    info = mp.get("info") or {}
    name = clean(r.get("application_name"))
    comments = clean(r.get("application_comments"))
    text = " ".join(filter(None, [name, comments]))
    kind = n[:3]
    if kind == "GMP":
        category, app_type = "Comp Plan Amendment", "Growth Management Plan Amendment (future land use map)"
    elif re.search(r"\bPD\b|Planned Development", text + " " + " ".join(x["title"] for x in cc), re.I):
        category, app_type = "PD", "Zoning (Planned Development / PD amendment)"
    else:
        category, app_type = "Rezoning", "Zoning (rezoning / initial zoning)"

    # hearings
    hmap = {}
    for h in mp.get("hearings", []):
        hmap[(h["body"], h["date"])] = dict(h)
    final = None
    accepted_mpb = None
    for x in sorted(cc, key=lambda x: x["date"]):
        key = ("City Council", x["date"])
        cur = hmap.get(key)
        res = x["result"]
        if cur is None or (res and (cur["result"] is None or x["flag"] == "final")):
            hmap[key] = {"body": "City Council", "date": x["date"], "result": res}
        if x["flag"] == "final" and res:
            final = ("Approved" if res.startswith("Approved") else "Denied", x["date"], f"City Council {x['date']}: {res}")
        if x["flag"] == "mpb":
            accepted_mpb = x["date"]
    hearings = sorted(hmap.values(), key=lambda h: (h["date"], h["body"] != "Municipal Planning Board"))
    past = [h for h in hearings if h["date"] <= today]
    future = [h for h in hearings if h["date"] > today]
    mpb_res = [h for h in hearings if h["body"] == "Municipal Planning Board" and h["result"]]

    st_raw = clean(r.get("application_status"))
    status, raw, decision = None, None, None
    if any(h["result"] == "Withdrawn" for h in hearings):
        status, raw = "Withdrawn", "Withdrawn (MPB / Council record)"
    elif final:
        status, decision, raw = final[0], final[1], final[2]
    elif mpb_res and mpb_res[-1]["result"] == "Recommended Denial" and accepted_mpb and accepted_mpb >= mpb_res[-1]["date"]:
        status, decision = "Denied", accepted_mpb
        raw = f"MPB {mpb_res[-1]['date']}: Recommended Denial; City Council {accepted_mpb}: MPB actions accepted"
    elif st_raw == "Closed" and past and past[-1]["result"] == "Continued" and not future:
        status = "Withdrawn"
        raw = f"{past[-1]['body']} {past[-1]['date']}: deferred, then application closed with no further hearing"
    elif future:
        status = "Continued" if past and past[-1]["result"] == "Continued" else "Hearing Scheduled"
        raw = f"next {future[0]['body']} {future[0]['date']}"
        if past and past[-1]["result"]:
            raw = f"{past[-1]['body']} {past[-1]['date']}: {past[-1]['result']}; " + raw
    elif past and past[-1]["result"] == "Continued":
        status, raw = "Continued", f"{past[-1]['body']} {past[-1]['date']}: deferred / continued"
    elif st_raw == "Closed" and not past:
        status, raw = "Withdrawn", "Closed with no MPB or City Council hearing on record"
    elif past and past[-1]["result"] is None:
        status, raw = "Hearing Scheduled", f"{past[-1]['body']} {past[-1]['date']}: result not yet posted"
    elif past:
        status, raw = "Filed", f"{past[-1]['body']} {past[-1]['date']}: {past[-1]['result']}; next hearing not yet scheduled"
    else:
        status, raw = "Filed", "Under review"
    raw = f"{raw} | open data status: {st_raw}" if st_raw else raw
    if st_raw == "Closed" and status in ("Filed", "Hearing Scheduled", "Continued") and past:
        raw += " (closed without a recorded final City Council vote)"

    # descriptive fields
    req = (info.get("requests") or {}).get(n)
    titles = " ".join(x["title"] for x in cc)
    frm = to = None
    for src in (req, titles):
        if not src:
            continue
        if kind == "GMP":
            m = re.search(r"(?:designation|Designations?)\s+from\s+(.+?)\s+to\s+(.+?)(?:\s+(?:to|for|in order)\b|[.;,(]|$)",
                          src, re.I) or re.search(r"\bFrom\s+(.+?)\s+to\s+(.+?)(?:,|\s+and\s+From|\(|$)", src)
        else:
            ms = list(re.finditer(r"\bFrom\s+(.+?)\s+to\s+(.+?)(?:\s+(?:for|to allow|to|in order|with)\b|[.;,(]|$)",
                                  src, re.I))
            m = ms[-1] if ms and kind == "ZON" and src is titles and len(ms) > 1 else (ms[0] if ms else None)
        if m:
            frm, to = clean(m.group(1)), clean(m.group(2))
            break
    acres = info.get("acres")
    if acres is None:
        acres = acres_in(titles) or acres_in(comments)
    # The open data prints the OCPA key in range-township-section order ("29-22-25-1536-03-070").
    # Return Orange County's standard section-township-range order ("25-22-29-1536-03-070") so the
    # parcel lookup is unambiguous and the map shows the same format as Orange County cases.
    parcels = [_sec_twp_rng(p.strip()) for p in re.split(r"[,;]\s*", r.get("parcel_id") or "") if p.strip()]
    address = clean(r.get("address"))
    if address and re.fullmatch(r"No Address|None", address, re.I):
        address = None
    if not address and info.get("location"):
        address = clean(re.split(r"\s+and generally|\s*;\s*", info["location"])[0])
    applicant = clean(r.get("applicant")) or info.get("applicant")
    desc = comments or req or name
    url = mp.get("staff_report") or f"{SODA}?application_number={n}"
    docs = mp.get("staff_report") or next((x["docs"] for x in reversed(cc) if x.get("docs")), None)
    return {
        "jurisdiction": "City of Orlando", "county": "Orange", "app_num": n,
        "category": category, "app_type": app_type,
        "project_name": name, "description": short(desc), "applicant": applicant,
        "parcel_ids": parcels, "address": address, "acres": acres,
        "from_zoning": frm if kind == "ZON" else None, "to_zoning": to if kind == "ZON" else None,
        "from_flu": frm if kind == "GMP" else None, "to_flu": to if kind == "GMP" else None,
        "filed_date": (r.get("application_date") or "")[:10] or None,
        "status": status, "status_raw": raw, "hearings": hearings, "decision_date": decision,
        "url": url, "docs_url": docs, "lat": None, "lon": None, "source": SOURCE,
        "owner": info.get("owner"), "ordinance_number": clean(r.get("ordinance_number")),
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
