#!/usr/bin/env python3
"""City of Palm Bay (Brevard County) rezoning / PUD / future land use map amendment adapter.

www.palmbayfl.gov answers 403 (Akamai) to non-browser clients; the city's agendas live on its
PrimeGov public portal (palmbayflorida.primegov.com), no login, same platform as oviedo.py:
 * /api/v2/PublicPortal/ListArchivedMeetings?year=YYYY and /ListUpcomingMeetings list meetings
   (committee 2 = Planning and Zoning Board / Local Planning Agency, committee 1 = City Council).
 * HTML agendas (/Portal/Meeting?meetingTemplateId=...) list each case by its city case number:
     CPyy-nnnnn  Small / large scale Comprehensive Plan Future Land Use Map amendment
     CPZyy-nnnnn Zoning amendment that accompanies a CP case
     Zyy-nnnnn   Zoning change
     PDyy-nnnnn  Preliminary Development Plan (PUD / PMU planned development approval)
   P&Z items carry applicant, request ("from RS-2 ... to RT-10"), acreage, location and either a
   Brevard parcel ID ("Parcel IDs 30-37-01-00-500") or the tax parcel description ("Tax Parcels
   753 and 755, Section 34, Township 28, Range 37", turned into 28-37-34-00-753). Council items
   reference "(Case CPZ26-00001)" with first / final reading. Skipped: FD (final development
   plans), CU, V, T (text amendments, incl. text-only CP cases), A (annexation), CDD, DA.
 * Outcomes: P&Z minutes (compiled "Minutes" document of each P&Z meeting: "Motion by ... to
   submit Case CP25-00004 to the City Council for approval ... Result: Carried 5 to 0") and City
   Council minutes, which are attached to a later Council agenda ("Adoption of Minutes: Regular
   Meeting 2026-27; October 1, 2026"), so Council outcomes lag the meeting by 2 to 6 weeks.
Cases surface when they first reach a P&Z agenda; filed_date = first agenda publish date.
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

JURISDICTIONS = ["City of Palm Bay"]
JUR = "City of Palm Bay"
BASE = "https://palmbayflorida.primegov.com"
SOURCE = "Palm Bay PrimeGov (P&Z Board + City Council agendas and minutes)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
COMMITTEES = {2: "Planning and Zoning Board", 1: "City Council"}
PZ, CC = "Planning and Zoning Board", "City Council"
LOOKBACK_DAYS = 150
SKIP_MEETING = re.compile(r"Workshop|Budget|CANCEL|Attorney.Client|Retreat", re.I)
CASE_RE = re.compile(r"\b(CPZ|CP|Z|PD|PUD)(\d{2})-(\d{5})\b")
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


def log(*a):
    print("[palm_bay]", *a, file=sys.stderr, flush=True)


def get(url, params=None, tries=3, sleep=0.4, **kw):
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


def clean(v):
    v = re.sub(r"\s+", " ", _html.unescape(v or "")).strip(" .,;:")
    return v or None


def to_float(s):
    try:
        v = float(str(s).replace(",", ""))
        return v if 0 < v < 100000 else None
    except (TypeError, ValueError):
        return None


def text_date(s):
    m = re.search(rf"({MONTHS})\s+(\d{{1,2}}),?\s+(\d{{4}})", s or "")
    if not m:
        return None
    try:
        return dt.datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y").date().isoformat()
    except ValueError:
        return None


def att_url(uid):
    return f"{BASE}/api/compilemeetingattachmenthistory/historyattachment/?historyId={uid}"


_pdf_cache = {}


def pdf_text(url, max_pages=None):
    key = (url, max_pages)
    if key in _pdf_cache:
        return _pdf_cache[key]
    rdr = pypdf.PdfReader(io.BytesIO(get(url).content))
    out = []
    for p in (rdr.pages if max_pages is None else rdr.pages[:max_pages]):
        try:
            out.append(p.extract_text() or "")
        except Exception:  # noqa: BLE001
            out.append("")
    t = "\n".join(out)
    _pdf_cache[key] = t
    return t


# ------------------------------------------------------------------ meetings / agendas
def list_meetings(start):
    ms = []
    for y in range(int(start[:4]), dt.date.today().year + 1):
        ms += get(f"{BASE}/api/v2/PublicPortal/ListArchivedMeetings", {"year": y}).json()
    ms += get(f"{BASE}/api/v2/PublicPortal/ListUpcomingMeetings").json()
    out = {}
    for m in ms:
        if m.get("committeeId") not in COMMITTEES or SKIP_MEETING.search(m.get("title") or ""):
            continue
        if (m.get("dateTime") or "")[:10] < start:
            continue
        out[m["id"]] = m
    return sorted(out.values(), key=lambda m: m["dateTime"])


def parse_agenda(h):
    """[{text, atts:[(uid, name)]}] from a PrimeGov HTML agenda."""
    items = []
    chunks = re.split(r'class="agenda-item"', h)
    for ch in chunks[1:]:
        head, _, rest = ch.partition('class="item_contents"')
        text = clean(re.sub(r"<[^>]*$", " ", re.sub(r"<[^>]+>", " ", "<" + head)))
        rest = rest.split('class="agenda-item"')[0]
        atts = re.findall(r'historyId=([0-9a-f-]{36})"[^>]*?title="Download ([^"]*)"', rest)
        atts = list(dict.fromkeys((u, clean(n)) for u, n in atts))
        if text:
            items.append({"text": text.lstrip('> ').strip(), "atts": atts})
    return items


def agenda_of(m):
    docs = m.get("documentList") or []
    hdoc = next((d for d in docs if d.get("compileOutputType") == 3 and "agenda" in (d.get("templateName") or "").lower()), None)
    pub = min([d["publishDate"][:10] for d in docs if d.get("publishDate") and "agenda" in (d.get("templateName") or "").lower()]
              or [m["dateTime"][:10]])
    mdoc = next((d for d in docs if d.get("compileOutputType") == 1 and (d.get("templateName") or "").lower() == "minutes"), None)
    if not hdoc:
        return [], None, pub, mdoc
    url = f"{BASE}/Portal/Meeting?meetingTemplateId={hdoc['templateId']}"
    return parse_agenda(get(url).text), url, pub, mdoc


# ------------------------------------------------------------------ scope / parsing
def case_keys(text):
    out = []
    for m in CASE_RE.finditer(text or ""):
        k = f"{m.group(1)}{m.group(2)}-{m.group(3)}"
        if k not in out:
            out.append(k)
    return out


def item_in_scope(key, text):
    if key.startswith("CP") and not key.startswith("CPZ"):
        return not re.search(r"Textual Amendment|Text Amendment", text, re.I) or \
            re.search(r"Future Land Use Map", text, re.I)
    return True


def pids_of(text):
    t = text or ""
    out = []
    for m in re.finditer(r"Parcel IDs?\s+((?:\d{2}-\d{2}-\d{2}-[0-9A-Z]{2}-[0-9A-Z*.\-]+(?:,?\s*(?:and\s+)?)?)+)", t):
        for p in re.findall(r"\d{2}-\d{2}-\d{2}-[0-9A-Z]{2}-[0-9A-Z*.]+(?:-[0-9A-Z*.]+)?", m.group(1)):
            p = p.rstrip(".")
            if p not in out:
                out.append(p)
    # "Tax Parcels 753 and 755, Section 34, Township 28, Range 37" (unplatted Brevard tax parcels)
    for m in re.finditer(r"Tax Parcels?\s+([\d.,\sand]+?),\s*(?:of\s+)?Section\s+(\d{1,2}),\s*Township\s+(\d{2}),\s*Range\s+(\d{2})", t):
        for p in re.findall(r"\d+(?:\.\d+)?", m.group(1)):
            pid = f"{m.group(3)}-{m.group(4)}-{int(m.group(2)):02d}-00-{p}"
            if pid not in out:
                out.append(pid)
    return out


def parse_item(text):
    t = text
    out = {}
    m = re.match(r"^\s*\d{0,2}\.?\s*\**\s*[A-Z]{1,4}\d{2}-\d{5}\s+[–\-]\s+(.+?)\s+[–\-]\s+(.+?)\s+[–\-]\s+(?:An?\s|A\s)", t)
    if m:
        out["project"] = clean(m.group(1))
        out["applicant"] = clean(m.group(2))
    m = re.search(r"\bfrom\s+(?:an?\s+)?(.+?)\s+to\s+(?:an?\s+)?(.+?)(?:\.\s|\s+for\s|\s+to allow|\s*$)", t)
    if m:
        out["from"], out["to"] = clean(m.group(1)), clean(m.group(2))
    m = re.search(r"containing approximately\s*([\d,]*\.?\d+)\s*acres", t, re.I) or \
        re.search(r"\(([\d,]*\.?\d+)\s*acres\)", t, re.I)
    if m:
        out["acres"] = to_float(m.group(1))
    m = re.search(r"Located\s+(.+?)(?:\s+[A-Z]{1,4}\d{2}-\d{5}\s+MEMORANDUM|\s+MEMORANDUM|\s+Legislative Memorandum|$)", t)
    if m:
        out["location"] = clean(m.group(1))
    else:
        m = re.search(r"which property is located\s+(.+?)(?:\s*\(|$)", t)
        out["location"] = clean(m.group(1)) if m else None
    out["pids"] = pids_of(t)
    rd = re.search(r"\b(first|final|second) (?:reading|hearing)\b", t, re.I)
    out["reading"] = rd.group(1).lower() if rd else None
    if out["reading"] == "second":
        out["reading"] = "final"
    return out


# ------------------------------------------------------------------ minutes
def minutes_results(text, body):
    """{case: result} from P&Z or Council minutes. Each 'Motion by ... Result: Carried' /
    'Motion carried' is credited to the case numbers in the motion, else to the case number(s) of the
    item heading that precedes it."""
    res = {}
    t = re.sub(r"(\d{4})-\s+(\d)", r"\1-\2", text)
    t = re.sub(r"\b([A-Z]{1,4}\d{2})\s?-\s+(\d{5})\b", r"\1-\2", t)
    flat = re.sub(r"\s+", " ", t)
    flat = re.sub(r"\ba\s+pprov", "approv", flat)
    heading = re.compile(r"\s\d{1,2}\.\s+(?:Ordinance|Resolution|Request|Consideration|Appointment|Adoption|Acknowledg|"
                         r"Contract|Award|Approval|\**[A-Z]{1,4}\d{2}-\d{5})|\b[A-Z]{4,}(?: [A-Z&/]{2,})+:")
    any_case = re.compile(r"\b([A-Z]{1,4})(\d{2})-(\d{5})\b")
    for m in re.finditer(r"Motion by .{0,80}?\bto\s+(.{0,1500}?)(?:Result:\s*(Carried|Failed)|Motion (carried|failed))", flat, re.I):
        verb = m.group(1).lower()
        ok = (m.group(2) or m.group(3) or "").lower() != "failed"
        if re.search(r"^(?:approve|adopt|accept) the (?:minutes|agenda|consent)", verb):
            continue
        keys = case_keys(m.group(1)[:300])
        if not keys:
            before = list(any_case.finditer(flat, max(0, m.start() - 8000), m.start()))
            if not before:
                continue
            last = before[-1]
            if heading.search(flat, last.end(), m.start()):
                continue   # a new agenda item started after the last case mention
            # all case numbers of that heading (e.g. two cases heard together)
            head = flat[max(0, last.start() - 400):last.end()]
            keys = [k for k in case_keys(head) if k in case_keys(flat[last.start():last.end()]) or
                    re.search(r"(?:and|&)\s*$", flat[last.start() - 40:last.start()])] or case_keys(flat[last.start():last.end()])
            if not keys:
                continue
        hs = list(heading.finditer(flat, max(0, m.start() - 6000), m.start()))
        ctx = flat[hs[-1].start() if hs else max(0, m.start() - 2500):m.start()]
        rd = list(re.finditer(r"\b(first|final|second) reading\b", ctx, re.I))
        reading = None
        if rd:
            reading = "final" if rd[-1].group(1).lower() in ("final", "second") else "first"
        if re.search(r"\b(continue|table|postpone|defer)", verb[:200]):
            r = "Continued" if ok else None
            nd = text_date(m.group(1)[:300])
            if ok and nd:
                for k in keys:
                    res[("next", k)] = nd
        elif re.search(r"\bfor denial\b|\bdeny\b|\bdenial\b", verb[:400]):
            r = ("Recommended Denial" if body == PZ else "Denied") if ok else None
        elif re.search(r"approv|adopt|submit", verb[:400]):
            if body == PZ:
                r = "Recommended Approval" if ok else "Motion to recommend approval failed"
            elif reading == "first" and not re.search(r"only one reading", ctx[-1500:], re.I):
                r = "Approved on first reading" if ok else "Denied"
            else:
                r = "Approved" if ok else "Denied"
        else:
            r = None
        if r:
            for k in keys:
                res[k] = r
    for m in re.finditer(r"rendered moot", flat, re.I):
        before = case_keys(flat[max(0, m.start() - 1200):m.start()])
        if before and not heading.search(flat, flat.rfind(before[-1], 0, m.start()), m.start()):
            res.setdefault(before[-1], "Denied (moot after companion denial)")
    for m in re.finditer(r"withdr[ae]wn? by the applicant|applicant (?:has )?withdr", flat, re.I):
        before = case_keys(flat[max(0, m.start() - 1500):m.start()])
        if before:
            res.setdefault(before[-1], "Withdrawn")
    return res


# ------------------------------------------------------------------ fetch
def fetch(since, refresh):
    if pypdf is None:
        raise RuntimeError("pypdf is required for the Palm Bay adapter")
    today = dt.date.today().isoformat()
    refresh = [r.strip().upper() for r in (refresh or []) if r and r.strip()]
    start = (dt.date.fromisoformat(since) - dt.timedelta(days=LOOKBACK_DAYS)).isoformat()
    if refresh:
        yrs = [2000 + int(m.group(2)) for r in refresh for m in [CASE_RE.search(r)] if m]
        if yrs:
            start = min(start, max(f"{min(yrs) - 1}-10-01", (dt.date.today() - dt.timedelta(days=730)).isoformat()))
    try:
        meetings = list_meetings(start)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Palm Bay PrimeGov unreachable: {e}") from e
    log(f"{len(meetings)} P&Z / Council meetings since {start}")

    cases, outcomes = {}, {}
    att_minutes = {}   # (body, meeting date) -> minutes attachment uid on a later agenda
    for m in meetings:
        body = COMMITTEES[m["committeeId"]]
        mdate = m["dateTime"][:10]
        try:
            items, url, pub, mdoc = agenda_of(m)
        except Exception as e:  # noqa: BLE001
            log(f"meeting {m['id']} {mdate}: {e}")
            continue
        if body == PZ and mdoc and mdate <= today:
            try:
                txt = pdf_text(f"{BASE}/Public/CompiledDocument?meetingTemplateId={mdoc['templateId']}&compileOutputType=1")
                for k, r in minutes_results(txt, PZ).items():
                    outcomes[(k, PZ, mdate)] = r
            except Exception as e:  # noqa: BLE001
                log(f"P&Z minutes {mdate}: {e}")
        for it in items:
            t = it["text"]
            if re.search(r"\bMinutes\b", t[:120], re.I) and not case_keys(t[:200]):
                d = text_date(t)
                for uid, nm in it["atts"]:
                    if re.search(r"Minutes", nm or "", re.I) and d:
                        att_minutes.setdefault((body, d), uid)
                continue
            for k in case_keys(t[:400] if body == PZ else t):
                if not item_in_scope(k, t):
                    continue
                cases.setdefault(k, []).append({"body": body, "date": mdate, "pub": pub, "text": t,
                                                "atts": it["atts"], "url": url})
    log(f"{len(cases)} in-scope cases; {len(att_minutes)} minutes attachments")

    for (body, d), uid in sorted(att_minutes.items()):
        if d < start or (body == PZ and any(b == PZ and dd == d for (_, b, dd) in outcomes)):
            continue
        try:
            for k, r in minutes_results(pdf_text(att_url(uid)), body).items():
                outcomes.setdefault((k, body, d), r)
        except Exception as e:  # noqa: BLE001
            log(f"{body} minutes {d}: {e}")

    out = []
    for k, aps in sorted(cases.items()):
        first = min(a["pub"] for a in aps)
        if first < since and k not in refresh:
            continue
        try:
            out.append(build(k, aps, outcomes, today))
        except Exception as e:  # noqa: BLE001
            log(f"{k}: skipped ({type(e).__name__}: {e})")
    for r in set(refresh) - set(cases):
        log(f"refresh id {r} not found on agendas since {start}")
    return out


def build(key, aps, outcomes, today):
    aps = sorted(aps, key=lambda a: (a["date"], a["body"] != PZ))
    pz = [a for a in aps if a["body"] == PZ]
    main = (pz or aps)[-1]
    info = parse_item(main["text"])
    pids = []
    for a in aps:
        for p in parse_item(a["text"])["pids"]:
            if p not in pids:
                pids.append(p)

    hmap = {}
    for a in aps:
        rd = parse_item(a["text"])["reading"] if a["body"] == CC else None
        body = f"City Council ({rd} reading)" if rd else a["body"]
        r = outcomes.get((key, a["body"], a["date"]))
        if r is None and re.search(r"\bcontinued\b", a["text"][:600], re.I) and a["date"] <= today and False:
            r = "Continued"
        h = hmap.setdefault((a["body"], a["date"]), {"body": body, "date": a["date"], "result": None})
        h["result"] = h["result"] or r
    for a in aps:   # "continued to the November 19, 2026, regular Council meeting"
        nd = outcomes.get((("next", key), a["body"], a["date"]))
        if nd and nd > a["date"] and (a["body"], nd) not in hmap:
            hmap[(a["body"], nd)] = {"body": a["body"], "date": nd, "result": None}
    hearings = sorted(hmap.values(), key=lambda h: (h["date"], not h["body"].startswith("Planning")))

    past = [h for h in hearings if h["date"] <= today]
    future = [h for h in hearings if h["date"] > today]
    decision = None
    if any(h["result"] == "Withdrawn" for h in hearings) or re.search(r"\bWITHDRAWN\b", " ".join(a["text"][:300] for a in aps)):
        status, raw = "Withdrawn", "Withdrawn (agenda / minutes)"
    elif past and past[-1]["body"].startswith(CC) and (past[-1]["result"] or "").startswith(("Approved", "Denied")) \
            and past[-1]["result"] != "Approved on first reading" and not future:
        status, decision = ("Approved" if past[-1]["result"] == "Approved" else "Denied"), past[-1]["date"]
        raw = f"{past[-1]['body']} {decision}: {past[-1]['result']} (minutes)"
    elif past and past[-1]["result"] == "Continued":
        status, raw = "Continued", f"{past[-1]['body']} {past[-1]['date']}: continued"
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

    typ = re.match(r"[A-Z]+", key).group(0)
    frm, to = info.get("from"), info.get("to")
    if typ == "CP":
        category = "Comp Plan Amendment"
        app_type = "Large-Scale Comprehensive Plan Future Land Use Map Amendment (CP)" \
            if re.search(r"Large[- ]Scale", main["text"], re.I) else "Small-Scale Comprehensive Plan Future Land Use Map Amendment (CP)"
    elif typ in ("PD", "PUD"):
        category, app_type = "PD", "Preliminary Development Plan (PD)"
    else:
        pd = re.search(r"\bPUD\b|\bPMU\b|Planned Unit Development|Parkway Mixed", to or "", re.I)
        category = "PD" if pd else "Rezoning"
        app_type = "Zoning Amendment (CPZ)" if typ == "CPZ" else "Zoning Change (Z)"
    desc = re.sub(r"^\s*\d{1,2}\.\s*\**\s*", "", main["text"])
    desc = re.split(r"\s+[A-Z]{1,4}\d{2}-\d{5}\s+MEMORANDUM|\s+MEMORANDUM\b|\s+Legislative Memorandum", desc)[0]
    desc = clean(desc)
    if desc and len(desc) > 400:
        desc = desc[:397] + "..."
    project = info.get("project")
    if not project:
        m = re.search(r"known as\s+['‘“\"]([^'’”\"]+)['’”\"]", main["text"])
        project = clean(m.group(1)) if m else None
    staff = next((u for a in reversed(aps) for u, n in a["atts"] if re.search(r"Staff Report", n or "", re.I)), None)
    return {
        "jurisdiction": JUR, "county": "Brevard", "app_num": key, "category": category, "app_type": app_type,
        "project_name": project, "description": desc, "applicant": info.get("applicant"),
        "parcel_ids": pids, "address": info.get("location"), "acres": info.get("acres"),
        "from_zoning": None if category == "Comp Plan Amendment" else frm,
        "to_zoning": None if category == "Comp Plan Amendment" else to,
        "from_flu": frm if category == "Comp Plan Amendment" else None,
        "to_flu": to if category == "Comp Plan Amendment" else None,
        "filed_date": min(a["pub"] for a in aps), "status": status, "status_raw": raw,
        "hearings": hearings, "decision_date": decision, "url": aps[-1]["url"],
        "docs_url": att_url(staff) if staff else None,
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
