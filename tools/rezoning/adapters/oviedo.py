#!/usr/bin/env python3
"""City of Oviedo (Seminole County) rezoning / PUD / future land use map amendment adapter.

Source: the city's PrimeGov public portal (cityofoviedo.primegov.com), no login:
 * /api/v2/PublicPortal/ListArchivedMeetings?year=YYYY and /ListUpcomingMeetings list meetings
   (committee 1 = City Council, committee 2 = Local Planning Agency / P&Z board).
 * HTML agendas (/Portal/Meeting?meetingTemplateId=...) give each item's title, agenda section
   ("PUBLIC HEARINGS", "FIRST READING OF ORDINANCES") and attachments. Meetings published only
   as a PDF agenda are read with pypdf (titles only, no attachments).
 * Each case is identified by its ordinance number ("Ordinance No. 1766, Zoning Map Amendment
   from Planned Unit Development to Planned Unit Development for ..."), which stays the same from
   the LPA hearing through Council adoption.
 * Details come from the item's attachments (pypdf, first pages only): the agenda memorandum
   (acres, location, applicant, LPA recommendation) and, at the Council stage, "Supporting Data,
   Inventory, and Analysis" (city file number such as 25-0166-PUD/ZMA, owner, applicant, acreage,
   Seminole County parcel IDs).
 * Hearing outcomes come from the DRAFT minutes the clerk attaches to a later agenda
   ("6.15.26 Draft Minutes", "LPA Minutes 5.5.26 draft"); the final minutes posted with each
   meeting are scanned images with no text, so outcomes lag the hearing by 2 to 6 weeks.
Cases surface only when they reach an LPA or Council agenda (no public application list);
filed_date = publish date of the first agenda that lists the case.
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

JURISDICTIONS = ["City of Oviedo"]

BASE = "https://cityofoviedo.primegov.com"
SOURCE = "Oviedo PrimeGov (LPA + City Council agendas, staff memos, draft minutes)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
COMMITTEES = {1: "City Council", 2: "Local Planning Agency"}
LOOKBACK_DAYS = 270
MAX_PDF_BYTES = 25 * 1024 * 1024
MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                          "nov", "dec"])}

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


def log(*a):
    print("[oviedo]", *a, file=sys.stderr)


def get(url, params=None, tries=3, sleep=0.5, **kw):
    last = None
    for i in range(tries):
        try:
            r = _s.get(url, params=params, timeout=120, **kw)
            r.raise_for_status()
            time.sleep(sleep)
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


def clean(s):
    s = _html.unescape(re.sub(r"\s+", " ", s or "")).strip(" .,;")
    return s or None


_pdf_cache = {}


def pdf_text(url, pages=None):
    key = (url, pages)
    if key in _pdf_cache:
        return _pdf_cache[key]
    r = _s.get(url, timeout=180, stream=True)
    r.raise_for_status()
    buf = io.BytesIO()
    for chunk in r.iter_content(256 * 1024):
        buf.write(chunk)
        if buf.tell() > MAX_PDF_BYTES:
            r.close()
            raise RuntimeError("PDF too large")
    time.sleep(0.5)
    rdr = pypdf.PdfReader(buf)
    pg = rdr.pages if pages is None else rdr.pages[:pages]
    t = "\n".join((p.extract_text() or "") for p in pg)
    _pdf_cache[key] = t
    return t


def att_url(uid):
    return f"{BASE}/api/compilemeetingattachmenthistory/historyattachment/?historyId={uid}"


# ------------------------------------------------------------------ meetings / agendas
def list_meetings(start):
    ms = []
    for y in range(int(start[:4]), dt.date.today().year + 1):
        ms += get(f"{BASE}/api/v2/PublicPortal/ListArchivedMeetings", {"year": y}).json()
    ms += get(f"{BASE}/api/v2/PublicPortal/ListUpcomingMeetings").json()
    out = {}
    for m in ms:
        if m.get("committeeId") not in COMMITTEES:
            continue
        if re.search(r"work ?session|\bWS\b|worksession|attorney.client", m.get("title") or "", re.I):
            continue
        if (m.get("dateTime") or "")[:10] < start:
            continue
        out[m["id"]] = m
    return sorted(out.values(), key=lambda m: m["dateTime"])


DATE_TXT = re.compile(r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+"
                      r"(January|February|March|April|May|June|July|August|September|October|November|December)"
                      r"\s+(\d{1,2}),?\s+(\d{4})")


def text_date(t):
    m = DATE_TXT.search(t or "")
    if not m:
        return None
    try:
        return dt.datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y").date().isoformat()
    except ValueError:
        return None


ITEM_RE = re.compile(r"class='section-row'><td colspan='2'>(.*?)</td>|"
                     r"class='meeting-item' data-itemid='(\d+)'.*?class='agenda-item' id='AgendaItem_\d+'>(.*?)</table>"
                     r"(.*?)(?=class='meeting-item'|class='section-row'|$)", re.S)
ATT_RE = re.compile(r"historyattachment/\?historyId=([0-9a-f-]+)\"[^>]*>.*?</span>\s*(?:&nbsp;)*\s*([^<]+?)\s*</a>", re.S)


def parse_html_agenda(h):
    items, section = [], None
    for m in ITEM_RE.finditer(h):
        if m.group(1) is not None:
            section = clean(re.sub(r"<[^>]+>", " ", m.group(1)))
            continue
        title = clean(re.sub(r"<[^>]+>", " ", m.group(3)))
        atts = [(uid, clean(nm)) for uid, nm in ATT_RE.findall(m.group(4) or "")]
        items.append({"item_id": m.group(2), "title": title, "section": section, "atts": atts})
    return items


def parse_pdf_agenda(t):
    t = re.sub(r"\s+", " ", t)
    items = []
    for m in re.finditer(r"(?:^|\s)\d{1,2}\.\s+(Ordinance No\.?\s*\d{3,5}.*?)(?=\s\d{1,2}\.\s|Local Planning Agency \w+day|"
                         r"City Council \w+day|DISCUSSION ITEMS|FUTURE MEETING|FIRST READING|PUBLIC HEARINGS|"
                         r"RESOLUTIONS|ADJOURNMENT|$)", t):
        before = t[:m.start()]
        secs = re.findall(r"(PUBLIC HEARINGS|FIRST READING OF ORDINANCES|CONSENT AGENDA|PUBLIC MEETING ITEMS)", before)
        items.append({"item_id": None, "title": clean(m.group(1)), "section": secs[-1] if secs else None, "atts": []})
    return items


def agenda_items(m):
    docs = m.get("documentList") or []
    hdoc = next((d for d in docs if d.get("compileOutputType") == 3 and "agenda" in (d.get("templateName") or "").lower()), None)
    pdoc = next((d for d in docs if d.get("compileOutputType") == 1 and (d.get("templateName") or "").lower() == "agenda"), None)
    pub = min([d["publishDate"][:10] for d in docs if d.get("publishDate") and
               "agenda" in (d.get("templateName") or "").lower()] or [m["dateTime"][:10]])
    if hdoc:
        url = f"{BASE}/Portal/Meeting?meetingTemplateId={hdoc['templateId']}"
        h = get(url).text
        items = parse_html_agenda(h)
        txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h))
        hd = re.search(r"AGENDA.{0,80}?(" + DATE_TXT.pattern + ")", txt)
        date = (text_date(hd.group(1)) if hd else None) or m["dateTime"][:10]
        return items, url, date, pub
    if pdoc and pypdf is not None:
        url = f"{BASE}/Public/CompiledDocument?meetingTemplateId={pdoc['templateId']}&compileOutputType=1"
        t = pdf_text(url, pages=4)
        ft = re.search(r"(?:Local Planning Agency|City Council)\s+(" + DATE_TXT.pattern + r")\s+Page", re.sub(r"\s+", " ", t))
        return parse_pdf_agenda(t), url, (text_date(ft.group(1)) if ft else None) or m["dateTime"][:10], pub
    return [], None, m["dateTime"][:10], pub


# ------------------------------------------------------------------ scope
IN_SCOPE = re.compile(r"Zoning Map Amendment|Rezon|Future Land Use Map|\bFLUM\b|Small[- ]Scale|Large[- ]Scale|"
                      r"Planned Unit Development|\bPUD\b|Comprehensive Plan Map", re.I)
OUT_SCOPE = re.compile(r"Text Amendment|Land Development Code|\bLDC\b|Charter|Evaluation and Appraisal|"
                       r"Chapter \d|Code of Ordinances", re.I)
ORD_RE = re.compile(r"Ordinance No\.?\s*(\d{3,5})", re.I)


def in_scope(title):
    t = title or ""
    return bool(ORD_RE.search(t)) and bool(IN_SCOPE.search(t)) and not OUT_SCOPE.search(t)


# ------------------------------------------------------------------ minutes
def minutes_date(name, agenda_date):
    n = (name or "").lower()
    ay, am = int(agenda_date[:4]), int(agenda_date[5:7])
    m = re.search(r"\b(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})\b", n)
    if m:
        mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y = y + 2000 if y < 100 else y
    else:
        m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})\b", n)
        if not m:
            return None
        mo, d = MONTHS[m.group(1)], int(m.group(2))
        y = ay if mo <= am else ay - 1
    try:
        return dt.date(y, mo, d).isoformat()
    except ValueError:
        return None


def minutes_section(text, ordno):
    t = re.sub(r"[ \t]+", " ", text)
    hits = [m.start() for m in re.finditer(rf"Ordinance\s+No\.?\s*{ordno}\b", t)]
    if not hits:
        return None
    # the agenda item heading is the occurrence preceded by an item number
    start = next((h for h in hits if re.search(r"(?:^|\n)\s*\d{1,2}\.\s*$", t[max(0, h - 8):h])), hits[0])
    rest = t[start + 10:]
    end = re.search(r"\n\s*\d{1,2}\.\s+[A-Z]|\n\s*(?:DISCUSSION ITEMS|FUTURE MEETING|ADJOURNMENT|RESOLUTIONS|"
                    r"FIRST READING|CITY MANAGER|PUBLIC HEARINGS)\b", rest)
    return re.sub(r"\s+", " ", t[start:start + 10 + (end.start() if end else 4000)])


def classify_minutes(sec, body, first_reading):
    s = (sec or "").lower()
    if not s:
        return None
    lpa = body == "Local Planning Agency"
    if re.search(r"applicant (?:has )?withdr|withdrawn by the applicant|(?:application|item|request|case) (?:was |has been )?"
                 r"withdrawn|request(?:ed)? to withdraw", s):
        return "Withdrawn"
    # walk the motions in order; the last one that carried decides
    last, prev = None, 0
    for m in re.finditer(r"motion (carried|passed|failed)|(carried|passed|failed) unanimously", s):
        seg = s[prev:m.start()]
        prev = m.end()
        ok = (m.group(1) or m.group(2)) != "failed"
        mm = list(re.finditer(r"motion to (\w+)|motions? (?:was |were )?made by .{0,80}? to (\w+)", seg))
        verb = (mm[-1].group(1) or mm[-1].group(2)) if mm else ""
        if not verb:
            verb = "deny" if re.search(r"\bden(?:y|ial)\b", seg) else ("continue" if re.search(r"continu|postpon|table", seg)
                                                                       else "approve")
        if ok:
            last = verb
        elif last is None:
            last = "failed:" + verb
    if last is None:
        return "Continued" if re.search(r"postpone|continued|tabled|defer", s) else None
    if last.startswith("failed:"):
        return f"Motion to {last[7:]} failed"
    if re.match(r"contin|postpon|table|defer", last):
        return "Continued"
    if re.match(r"den", last):
        return "Recommended Denial" if lpa else "Denied"
    if lpa:
        return "Recommended Approval"
    return "Approved on first reading" if first_reading else "Approved"


def memo_results(tt):
    """'At its Tuesday May 19, 2026 meeting, the Local Planning Agency ... recommended its approval.'"""
    out = {}
    for m in re.finditer(r"At its (?:[A-Z][a-z]+day,?\s+)?((?:January|February|March|April|May|June|July|August|September|"
                         r"October|November|December)\s+\d{1,2},?\s+\d{4}),?\s+(?:regular |special )?meeting,?\s+"
                         r"(?:the\s+)?(Local Planning Agency|LPA|City Council)(.{0,500}?(?<!No)(?<!\b[A-Z])\.)(?=\s+[A-Z]|\s*$)", tt):
        try:
            d = dt.datetime.strptime(re.sub(r",", "", m.group(1)), "%B %d %Y").date().isoformat()
        except ValueError:
            continue
        body = "City Council" if m.group(2) == "City Council" else "Local Planning Agency"
        x = m.group(3).lower()
        if re.search(r"recommended (?:its )?denial|recommend(?:ed)? to deny", x):
            r = "Recommended Denial" if body != "City Council" else "Denied"
        elif re.search(r"recommended (?:its )?approval|recommended approv|recommend(?:ed)? to approve|"
                       r"approved (?:the )?first reading|passed (?:on )?first reading", x):
            r = "Recommended Approval" if body != "City Council" else "Approved on first reading"
        elif re.search(r"continu|postpon|tabled", x):
            r = "Continued"
        else:
            continue
        out[(body, d)] = r
    return out


# ------------------------------------------------------------------ details from attachments
PID_RE = re.compile(r"\b\d{2}\s?-\s?\d{2}\s?-\s?\d{2}\s?-\s?[0-9A-Z]{3}\s?-\s?[0-9A-Z]{4}\s?-\s?[0-9A-Z]{4}\b")


def to_float(s):
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


def details(appearances):
    """Pull fields from the memo / supporting-data attachments (latest appearance first)."""
    info = {}
    if pypdf is None:
        return info
    memo_done = data_done = False
    for ap in sorted(appearances, key=lambda a: a["date"], reverse=True):
        for uid, nm in ap["atts"]:
            nm = nm or ""
            is_data = bool(re.search(r"Supporting Data|Data,? Inventory", nm, re.I))
            is_memo = bool(re.search(r"\bMemo$|Staff Report$|^Memo\b|Memorandum$", nm, re.I)) and "Atch" not in nm
            if is_data and not data_done:
                try:
                    t = pdf_text(att_url(uid), pages=4)
                except Exception as e:  # noqa: BLE001
                    log(f"{nm}: {e}")
                    continue
                data_done = True
                g = lambda rx: (lambda mm: clean(mm.group(1)) if mm else None)(re.search(rx, t))  # noqa: E731
                info.setdefault("file_no", g(r"File Number:\s*(\S+)"))
                info.setdefault("app_type", g(r"Application Type:\s*([^\n]+)"))
                info.setdefault("owner", g(r"Owners?:\s*([^\n]+)"))
                info.setdefault("applicant", g(r"Applicant:\s*([^\n]+)"))
                pids = []
                for p in PID_RE.findall(t):
                    p = re.sub(r"\s", "", p)
                    if p not in pids:
                        pids.append(p)
                if pids:
                    info["parcels"] = pids
                a = re.search(r"Total Acreage is\s*([\d,]*\.?\d+)\s*acres", t, re.I) or \
                    re.search(r"approximately\s*([\d,]*\.?\d+)\s*acres", t, re.I)
                if a:
                    info.setdefault("acres", to_float(a.group(1)))
                loc = re.search(r"(?:are|is) located (?:at|on) (.+?)\.\s", re.sub(r"\s+", " ", t))
                if loc:
                    info.setdefault("address", clean(loc.group(1)))
                info["data_url"] = att_url(uid)
            elif is_memo and not memo_done:
                try:
                    t = pdf_text(att_url(uid), pages=30)
                except Exception as e:  # noqa: BLE001
                    log(f"{nm}: {e}")
                    continue
                memo_done = True
                tt = re.sub(r"\s+", " ", t)
                info["memo_results"] = memo_results(tt)
                a = re.search(r"\(([\d,]*\.?\d+)\)\s*acres", tt) or re.search(r"approximately\s*([\d,]*\.?\d+)\s*acres", tt)
                if a:
                    info.setdefault("acres", to_float(a.group(1)))
                m = re.search(r"located (?:on|at) (.+?)(?: from | to | and approve|\. )", tt)
                if m:
                    info.setdefault("address", clean(m.group(1)))
                m = re.search(r"The applicant is ([^.]+(?:\.\s*[A-Z][a-z]?\.)?[^.]*)\.", tt)
                if m:
                    info.setdefault("memo_applicant", clean(m.group(1)))
                m = re.search(r"Introduction:\s*(.+?)(?:A location map| Discussion:)", tt)
                if m:
                    info.setdefault("intro", clean(m.group(1)))
                info.setdefault("memo_url", att_url(uid))
        if memo_done and data_done:
            break
    return info


# ------------------------------------------------------------------ fetch
def fetch(since, refresh):
    today = dt.date.today().isoformat()
    start = (dt.date.fromisoformat(since) - dt.timedelta(days=LOOKBACK_DAYS)).isoformat()
    refresh_set = {re.sub(r"\D", "", r) for r in (refresh or []) if r and re.search(r"\d", r)}
    meetings = list_meetings(start)
    log(f"{len(meetings)} LPA / Council meetings since {start}")

    cases, minutes = {}, []  # ordno -> appearances ; minutes attachments
    for m in meetings:
        body = COMMITTEES[m["committeeId"]]
        try:
            items, url, date, pub = agenda_items(m)
        except Exception as e:  # noqa: BLE001
            log(f"meeting {m['id']} {m['dateTime'][:10]}: {e}")
            continue
        for it in items:
            for uid, nm in it["atts"]:
                if re.search(r"minutes", nm or "", re.I) and not re.search(r"bid|evaluation|proposal|memo|atch|"
                                                                             r"committee minutes", nm or "", re.I):
                    md = minutes_date(nm, date)
                    if md:
                        minutes.append({"body": body, "date": md, "uid": uid, "name": nm})
            om = ORD_RE.search(it["title"] or "")
            if not om or OUT_SCOPE.search(it["title"]):
                continue
            ordno = om.group(1)
            cases.setdefault(ordno, []).append({
                "body": body, "date": date, "pub": pub, "title": it["title"], "section": it["section"] or "",
                "atts": it["atts"], "url": url, "item_id": it["item_id"],
            })
    cases = {k: v for k, v in cases.items() if any(in_scope(a["title"]) for a in v)}
    log(f"{len(cases)} in-scope ordinances; {len(minutes)} draft minutes attachments")

    out = []
    for ordno, aps in sorted(cases.items()):
        first = min(a["pub"] for a in aps)
        if first < since and ordno not in refresh_set:
            continue
        try:
            out.append(build(ordno, aps, minutes, today))
        except Exception as e:  # noqa: BLE001
            log(f"Ordinance {ordno}: skipped ({e})")
    for r in refresh_set - set(cases):
        log(f"refresh id {r} not found on LPA / Council agendas since {start}")
    return out


def build(ordno, aps, minutes, today):
    aps = sorted(aps, key=lambda a: a["date"])
    info = details(aps)
    # hearings
    hmap = {}
    for a in aps:
        first_reading = bool(re.search(r"FIRST READING", a["section"], re.I))
        res = None
        if a["date"] <= today and pypdf is not None:
            for mn in [x for x in minutes if x["body"] == a["body"] and x["date"] == a["date"]]:
                try:
                    sec = minutes_section(pdf_text(att_url(mn["uid"])), ordno)
                except Exception as e:  # noqa: BLE001
                    log(f"minutes {mn['name']}: {e}")
                    continue
                res = classify_minutes(sec, a["body"], first_reading)
                if res:
                    break
        k = (a["body"], a["date"])
        res = res or (info.get("memo_results") or {}).get(k)
        h = hmap.get(k) or {"body": a["body"], "date": a["date"], "result": None, "_first": first_reading}
        h["result"] = h["result"] or res
        hmap[k] = h
    hearings = sorted(hmap.values(), key=lambda h: (h["date"], h["body"] != "Local Planning Agency"))

    past = [h for h in hearings if h["date"] <= today]
    future = [h for h in hearings if h["date"] > today]
    final = [h for h in past if h["body"] == "City Council" and not h["_first"] and h["result"] in ("Approved", "Denied")]
    decision = None
    if any(h["result"] == "Withdrawn" for h in hearings) or re.search(r"withdrawn", aps[-1]["title"], re.I):
        status, raw = "Withdrawn", "Withdrawn (agenda / minutes)"
    elif final and not future:
        status, decision = final[-1]["result"], final[-1]["date"]
        raw = f"City Council {decision}: {final[-1]['result']} (draft minutes)"
    elif past and past[-1]["result"] == "Continued":
        status, raw = "Continued", f"{past[-1]['body']} {past[-1]['date']}: continued"
        if future:
            raw += f"; next {future[0]['body']} {future[0]['date']}"
    elif future:
        status, raw = "Hearing Scheduled", f"next {future[0]['body']} {future[0]['date']}"
        if past and past[-1]["result"]:
            raw = f"{past[-1]['body']} {past[-1]['date']}: {past[-1]['result']}; " + raw
    elif past and past[-1]["result"] is None:
        status = "Hearing Scheduled"
        raw = f"{past[-1]['body']} {past[-1]['date']}: outcome not yet in published draft minutes"
    elif past:
        status, raw = "Filed", f"{past[-1]['body']} {past[-1]['date']}: {past[-1]['result']}; next hearing not yet scheduled"
    else:
        status, raw = "Filed", "On agenda"

    title = aps[-1]["title"]
    body_t = re.sub(r"^Ordinance No\.?\s*\d+\s*[,.]?\s*", "", title)
    is_flu = bool(re.search(r"Future Land Use|FLUM|Comprehensive Plan Map|Small[- ]Scale|Large[- ]Scale", title, re.I))
    frm = to = None
    m = re.search(r"(?:Amendment|Rezoning)\b(?:\s+for\s+[\d.,]+\s+Acres)?\s+from\s+(.+?)\s+to\s+(.+?)"
                  r"(?=\s+(?:for|and|on|located)\b|[;]|,\s+(?!Residential|Commercial|Industrial)|$)", title, re.I)
    if m:
        frm, to = clean(m.group(1)), clean(m.group(2))
    else:
        m = re.search(r"from\s+(.+?)\s+to\s+(.+?)(?=\s+(?:and approve|and approving|for|on)\b|[,;.]|$)",
                      info.get("intro") or "")
        if m:
            frm, to = clean(m.group(1)), clean(m.group(2))
    is_pd = bool(re.search(r"Planned Unit Development|\bPUD\b|Planned Development", (to or "") + " " + title, re.I))
    category = "Comp Plan Amendment" if is_flu else ("PD" if is_pd else "Rezoning")
    acres = info.get("acres")
    if acres is None:
        a = re.search(r"([\d,]*\.?\d+)\s*Acres", title, re.I)
        acres = to_float(a.group(1)) if a else None
    name = None
    m = re.search(r"\bfor (?:the )?(.+?)(?: Planned Unit Development| Conceptual Development Plan| and |\.|$)", body_t)
    if m:
        name = clean(m.group(1))
    applicant = info.get("applicant") or info.get("memo_applicant") or info.get("owner")
    desc = clean(body_t)
    if info.get("file_no"):
        desc = f"{desc} [file {info['file_no']}]"
    if desc and len(desc) > 400:
        desc = desc[:397] + "..."
    last = aps[-1]
    return {
        "jurisdiction": "City of Oviedo", "county": "Seminole", "app_num": f"Ordinance No. {ordno}",
        "category": category, "app_type": info.get("app_type") or clean(re.split(r"\s+from\s+", body_t)[0]),
        "project_name": name, "description": desc, "applicant": applicant,
        "parcel_ids": info.get("parcels") or [], "address": info.get("address"), "acres": acres,
        "from_zoning": None if is_flu else frm, "to_zoning": None if is_flu else to,
        "from_flu": frm if is_flu else None, "to_flu": to if is_flu else None,
        "filed_date": min(a["pub"] for a in aps), "status": status, "status_raw": raw,
        "hearings": [{"body": h["body"], "date": h["date"], "result": h["result"]} for h in hearings],
        "decision_date": decision, "url": last["url"],
        "docs_url": info.get("data_url") or info.get("memo_url"),
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
