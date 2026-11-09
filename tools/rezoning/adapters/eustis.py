#!/usr/bin/env python3
"""City of Eustis (Lake County) rezoning / design district / future land use map amendment adapter.

Source: CivicClerk public API (eustisfl.api.civicclerk.com), Local Planning Agency (LPA) and City
Commission meetings.
 * Land use cases are numbered ordinances: "First Reading of Ordinance Number 2026-37: Future Land
   Use Map Amendment for Parcels with Alternate Key Numbers 1197546 and 1197601". app_num =
   "Ordinance YYYY-NN". Eustis zones land with "Design District Assignment" ordinances (its form
   based code replaces conventional zoning districts), so those are kept as category Rezoning;
   PUD / rezoning ordinances (if any) are kept too. Annexation, conditional use, development
   agreement and LDR text ordinances are skipped.
 * Before August 2026 Eustis did not itemize agendas in CivicClerk (only an agenda PDF and a packet),
   so the agenda PDF of every meeting is parsed; it is short and lists each ordinance with its
   reading and the Lake County Alternate Key(s).
 * Acreage, from/to designations and the city case number (e.g. CPLUS-26-0003) come from the staff
   report attachment when the item is itemized, else from the ordinance title read into the
   minutes ("... approximately 10 acres ... from Urban Low in Lake County to General Industrial").
 * Outcomes: CivicClerk "Minutes" files ("Motion made by ..., Seconded by ... to approve Ordinance
   Number 2026-07 on second reading. The motion passed ..."). A later second reading implies the
   first reading passed.
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

try:
    import pypdf
except ImportError:  # pragma: no cover
    pypdf = None

JURISDICTIONS = ["City of Eustis"]
JUR = "City of Eustis"
COUNTY = "Lake"
TENANT = "eustisfl"
API = f"https://{TENANT}.api.civicclerk.com/v1"
PORTAL = f"https://{TENANT}.portal.civicclerk.com"
SOURCE = "Eustis CivicClerk (LPA + City Commission agendas, staff reports and minutes)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
LOOKBACK_DAYS = 150
MAX_ATT = 3_000_000
LPA = "Local Planning Agency"
CC = "City Commission"

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


def log(*a):
    print("[eustis]", *a, file=sys.stderr)


def get(url, params=None, tries=4, sleep=0.4, **kw):
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


def sq(s):
    return re.sub(r"\s+", " ", s or "").strip()


def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = _html.unescape(s).replace(" ", " ").replace("​", "")
    return sq(s)


def pdf_text(content, max_pages=None):
    rdr = pypdf.PdfReader(io.BytesIO(content))
    out = []
    for p in rdr.pages[:max_pages] if max_pages else rdr.pages:
        try:
            out.append(p.extract_text() or "")
        except Exception:  # noqa: BLE001
            out.append("")
    return "\n".join(out)


def file_stream(fid):
    return f"{API}/Meetings/GetMeetingFileStream(fileId={fid},plainText=false)"


def list_events(start, end):
    url = f"{API}/Events"
    params = {"$filter": f"startDateTime ge {start}T00:00:00Z and startDateTime lt {end}T00:00:00Z",
              "$orderby": "startDateTime"}
    out = []
    while url:
        d = get(url, params).json()
        out.extend(d.get("value", []))
        url, params = d.get("@odata.nextLink"), None
    return out


def walk(items):
    for it in items or []:
        yield it
        yield from walk(it.get("childItems"))


# ------------------------------------------------------------------ parsing
ORD_RE = re.compile(r"\bOrdinances?\s*(?:Numbers?|No\.?|#)?\s*(\d{4})\s*-\s*(\d{1,3})\b", re.I)
CPA_RE = re.compile(r"Comprehensive\s+Plan(?:\s+Map)?\s+Amendment|Future\s+Land\s+Use|\bFLUM?\b", re.I)
RZ_RE = re.compile(r"Design\s+District\s+Assignment|Rezon|Zoning\s+(?:Map\s+)?(?:Change|Amendment)|\bPUD\b|"
                   r"Planned\s+(?:Unit\s+)?Development", re.I)
OUT_RE = re.compile(r"Land\s+Development\s+Regulations|Evaluation\s+and\s+Appraisal|\bEAR\b|\bCPT\b|Text\s+Amendment|"
                    r"Conditional\s+Use|Development\s+Agreement|Pension|Budget|Millage|Impact\s+Fee|Chapter\s+\d", re.I)
ITEM_SPLIT = re.compile(r"(?:^|\n)\s*\d{1,2}\.\d{1,2}\.?\s+|(?=\b(?:First|Second)\s+Reading\s+(?:of|for)\s+Ordinance\s+Number\b)",
                        re.I)


def norm_key(y, n):
    return f"{y}-{int(n):02d}"


def classify(title):
    m = ORD_RE.search(title)
    if not m or re.match(r"\s*Explanation", title, re.I):
        return None
    rest = title[m.end():]
    if OUT_RE.search(rest):
        return None
    if CPA_RE.search(rest):
        kind = "CPA"
    elif RZ_RE.search(rest):
        kind = "RZ"
    else:
        return None
    return norm_key(m.group(1), m.group(2)), kind


def agenda_items(text):
    """[(title, reading)] for ordinance items on an agenda PDF."""
    t = text.replace("\u2013", "-").replace("\u2014", "-")
    t = re.split(r"This Agenda is provided", t)[0]
    out = []
    for seg in ITEM_SPLIT.split(t):
        seg = sq(seg)
        if not seg or not ORD_RE.search(seg):
            continue
        seg = re.split(r"\s\d{1,2}\.\s+[A-Z]", seg)[0]  # stop at next top-level heading
        rd = re.match(r"(First|Second)\s+Reading", seg, re.I)
        out.append((seg, rd.group(1).lower() if rd else None))
    return out


def alt_keys(t):
    keys = []
    for m in re.finditer(r"Alt(?:ernate)?\.?\s*Keys?\s*(?:Nos?\.?|Numbers?|#)?\s*:?\s*", t, re.I):
        seg = re.split(r"[A-Za-z]{4,}", t[m.end(): m.end() + 120])[0]
        keys += re.findall(r"\b\d{7}\b", seg)
    keys += re.findall(r"\bAK\s*#?\s*(\d{7})\b", t)
    return list(dict.fromkeys(keys))


def parse_acres(t):
    m = re.search(r"(?:approximately|approx\.?|\+/-|totaling)\s*([\d,]*\.?\d+)\s*(?:\+/-\s*)?(?:gross\s+)?acres", t, re.I) or \
        re.search(r"([\d,]*\.?\d+)\s*(?:\+/-\s*)?acres", t, re.I)
    if m:
        try:
            v = float(m.group(1).replace(",", ""))
            return v if 0 < v < 50000 else None
        except ValueError:
            return None
    return None


def from_to(t):
    t = sq(t)
    m = re.search(r"\bfrom\s+(.{3,90}?)\s+to\s+(.{3,90}?)(?:\s+in\s+the\s+City(?:\s+of\s+Eustis)?|;|,\s*(?:and|providing|for)\b|\.\s|"
                  r"\s+for\s+(?:the|approximately|property|a\s)|$)", t, re.I)
    if not m:
        return None, None
    f, to = (sq(x).strip(" ,.") for x in m.groups())
    if len(f) > 80 or len(to) > 80:
        return None, None
    return f, to


MOTION_RE = re.compile(r"(?:motion|moved)\b.{0,250}?\bto\s+(?:recommend\s+)?(?:the\s+)?(approv\w*|adopt\w*|den\w+|tabl\w*|"
                       r"continu\w*|postpon\w*|defer\w*|transmit\w*)\b", re.I)


def minutes_results(text):
    """({key: result}, {key: ordinance-title text}) from a minutes PDF."""
    d = sq(text)
    res, titles = {}, {}
    for m in MOTION_RE.finditer(d):
        after = d[m.end(): m.end() + 700]
        o = re.search(r"\b(passed|carried|failed)\b", after, re.I)
        if not o:
            continue
        keys = [norm_key(x.group(1), x.group(2)) for x in ORD_RE.finditer(after[:min(o.start(), 300)])]
        if not keys or o.group(1).lower() == "failed":
            continue
        v = m.group(1).lower()
        val = ("Continued" if v.startswith(("tabl", "contin", "postp", "defer")) else
               "Denied" if v.startswith("den") else "Approved")
        for k in keys:
            res[k] = val
    for m in re.finditer(r"introduced\s+Ordinance\s+Number\s+(\d{4})\s*-\s*(\d{1,3})\s+by\s+title,?\s*(.{0,900}?)"
                         r"(?=City Attorney \w+ opened|opened the public hearing|Motion made|$)", d, re.I):
        titles.setdefault(norm_key(m.group(1), m.group(2)), m.group(3))
    for sent in re.split(r"(?<=[a-z)])\.\s+(?=[A-Z0-9])", d):
        if re.search(r"(?:removed|pulled|withdrawn)\s+from\s+(?:the\s+)?agenda|continued\s+to|postponed|tabled", sent, re.I):
            for y, n in re.findall(r"\b(20\d\d)\s*-\s*(\d{1,3})\b", sent):
                res.setdefault(norm_key(y, n), "Continued")
    for m in re.finditer(r"withdr[ae]w\w*\s+(?:of\s+)?Ordinance\s+Number\s+(\d{4})\s*-\s*(\d{1,3})", d, re.I):
        res[norm_key(m.group(1), m.group(2))] = "Withdrawn"
    return res, titles


# ------------------------------------------------------------------ fetch
def body_of(ev):
    name = (ev.get("eventName") or "") + " | " + (ev.get("categoryName") or "")
    if re.search(r"CANCEL|Workshop|Budget|Redevelopment|Historic|Pension|Code Enforcement", name, re.I):
        return None
    if re.search(r"Local Planning|Planning", name, re.I):
        return LPA
    if re.search(r"Commission", name, re.I):
        return CC
    return None


def fetch(since, refresh):
    if pypdf is None:
        raise RuntimeError("pypdf is required for the Eustis adapter")
    today = dt.date.today()
    refresh_ids = {}
    for r in refresh or []:
        m = re.search(r"(\d{4})\s*-\s*(\d{1,3})\b", r or "")
        if m:
            refresh_ids[norm_key(m.group(1), m.group(2))] = r
    start_d = dt.date.fromisoformat(since) - dt.timedelta(days=LOOKBACK_DAYS)
    if refresh_ids:
        yr = min(int(k[:4]) for k in refresh_ids)
        start_d = min(start_d, max(dt.date(yr - 1, 9, 1), today - dt.timedelta(days=730)))
    end_d = today + dt.timedelta(days=75)
    events = list_events(start_d.isoformat(), end_d.isoformat())
    log(f"{len(events)} events {start_d}..{end_d}")

    cases, minutes = {}, {}
    n_meet = 0
    for ev in events:
        body = body_of(ev)
        files = ev.get("publishedFiles") or []
        ag = next((f for f in files if f.get("type") == "Agenda"), None)
        if not body or not ag:
            continue
        mdate = ev["startDateTime"][:10]
        try:
            text = pdf_text(get(file_stream(ag["fileId"])).content)
        except Exception as e:  # noqa: BLE001
            log(f"agenda {mdate} {body}: {e}")
            continue
        n_meet += 1
        first_seen = min(ag["publishOn"][:10], mdate) if ag.get("publishOn") else mdate
        packet = next((f.get("fileId") for f in files if f.get("type") == "Agenda Packet"), None)
        mins = next((f.get("fileId") for f in files if f.get("type") == "Minutes"), None)
        atts = {}
        if ev.get("agendaId"):
            try:
                mt = get(f"{API}/Meetings/{ev['agendaId']}").json()
                for it in walk(mt.get("items")):
                    nm = strip_html(it.get("agendaObjectItemName"))
                    om = ORD_RE.search(nm)
                    if om and it.get("attachmentsList"):
                        atts.setdefault(norm_key(om.group(1), om.group(2)), []).extend(it["attachmentsList"])
            except Exception as e:  # noqa: BLE001
                log(f"meeting items {mdate}: {e}")
        found = False
        for title, reading in agenda_items(text):
            c = classify(title)
            if not c:
                continue
            key, kind = c
            found = True
            rec = cases.setdefault(key, {"kind": kind, "apps": []})
            rec["apps"].append({"date": mdate, "first_seen": first_seen, "body": body,
                                "reading": reading if body == CC else None, "title": title,
                                "atts": atts.get(key, []), "event_id": ev.get("id"), "packet": packet})
        if found and mins:
            try:
                minutes[(body, mdate)] = minutes_results(pdf_text(get(file_stream(mins)).content))
            except Exception as e:  # noqa: BLE001
                log(f"minutes {mdate}: {e}")
    log(f"{n_meet} agendas read; {len(cases)} in-scope ordinances")

    out = []
    for key, c in sorted(cases.items()):
        first = min(a["first_seen"] for a in c["apps"])
        if first < since and key not in refresh_ids:
            continue
        try:
            out.append(build(key, c, minutes, today.isoformat()))
        except Exception as e:  # noqa: BLE001
            log(f"Ordinance {key}: skipped ({e})")
    for r in set(refresh_ids) - set(cases):
        log(f"refresh id {refresh_ids[r]} not found on any agenda since {start_d}")
    return out


def build(key, c, minutes, today):
    apps = sorted(c["apps"], key=lambda a: (a["date"], a["body"] == CC))
    latest = apps[-1]
    kind = c["kind"]
    title = max((a["title"] for a in apps), key=len)
    m = ORD_RE.search(title)
    subject = sq(title[m.end():]).strip(" :-") if m else title
    subject = re.sub(r"^\s*(?:for|:)\s*", "", subject)
    ids = alt_keys(" ".join(a["title"] for a in apps))

    # staff report (itemized meetings) and minutes title text
    staff = ""
    for a in reversed(apps):
        for att in a["atts"]:
            fn = att.get("fileName") or ""
            if not re.search(r"Staff\s*Report|Memo", fn, re.I) or not att.get("pdfVersionFullPath") or \
                    (att.get("fileSize") or 0) > MAX_ATT:
                continue
            try:
                staff = pdf_text(get(att["pdfVersionFullPath"]).content, 4)
            except Exception as e:  # noqa: BLE001
                log(f"{key}: staff report: {e}")
            break
        if staff:
            break
    mtitle = next((minutes.get((a["body"], a["date"]), ({}, {}))[1].get(key) for a in apps
                   if minutes.get((a["body"], a["date"]), ({}, {}))[1].get(key)), None) or ""
    staff_sq = re.sub(r"\s*\(Alternate\s+Key\s+Numbers?\s+[\d ,and]+\)", "", sq(staff), flags=re.I)
    # narrow the staff report to the part about this ordinance when it covers several
    seg = staff_sq
    sm = re.search(r"\(?Ordinance\s+Number\s+" + re.escape(key) + r"\)?", staff_sq)
    if sm and "Introduction" in staff_sq:
        seg = staff_sq[staff_sq.find("Introduction"):]
    acres = parse_acres(seg) or parse_acres(mtitle)
    case_no = None
    cm = re.search(r"\b([A-Z]{2,6}-\d{2}-\d{3,5})\b", staff_sq)
    if cm:
        case_no = cm.group(1)
    f_, t_ = None, None
    if kind == "CPA":
        fm = re.search(r"Ordinance\s+Number\s+" + re.escape(key) + r"\s+would\s+change\s+the\s+Future\s+Land\s+Use\s+"
                       r"designation\s+from\s+(.+?)\s+to\s+(.+?)(?:\s+in\s+the\s+City|,|\.\s)", staff_sq, re.I)
        if fm:
            f_, t_ = sq(fm.group(1)), sq(fm.group(2))
        if not f_:
            fm = re.search(r"Existing:\s*(.+?)\s*[•●]?\s*Proposed:\s*(.+?)\.", staff_sq)
            if fm:
                f_, t_ = sq(fm.group(1)), sq(fm.group(2))
        if not f_:
            f_, t_ = from_to(re.split(r"design\s+district", mtitle, flags=re.I)[0])
    else:
        fm = re.search(r"Ordinance\s+Number\s+" + re.escape(key) + r"\s+would\s+assign\s+the\s+subject\s+property\s+a\s+"
                       r"Design\s+District\s+designation\s+of\s+(.+?)\.", staff_sq, re.I)
        if fm:
            t_ = sq(fm.group(1))
        if not t_:
            fm = re.search(r"assigning\s+(?:the\s+)?(.{3,50}?)\s+design\s+district\s+designation", mtitle, re.I)
            if fm:
                t_ = sq(fm.group(1)).title().replace("(Sc)", "(SC)")
        if not t_:
            fm = re.search(r"design\s+district\s+(?:designation\s+)?(?:of|as)\s+([A-Z][\w /-]{3,40}?)(?:\s+for|,|\.|;|$)",
                           mtitle + " " + subject, re.I)
            if fm:
                t_ = sq(fm.group(1))
        if re.search(r"Rezon", subject, re.I):
            f_, t_ = from_to(mtitle) if from_to(mtitle)[0] else (f_, t_)
    if f_ and len(f_) > 90:
        f_ = None
    if t_ and len(t_) > 90:
        t_ = None
    applicant = None
    am = re.search(r"Applicant:\s*(.{3,80}?)\s{1,3}(?:Owner|Property|Location|Site|Agent)", staff_sq)
    if am:
        applicant = am.group(1).strip(" ,")
    addr = None
    am = re.search(r"\blocated at\s+(\d{2,6}\s+[^,(]{3,60}?)(?:\s*\(|,|\s+Eustis)", staff_sq + " " + mtitle, re.I)
    if am:
        addr = am.group(1).strip()
    is_pd = bool(re.search(r"\bPUD\b|Planned\s+(?:Unit\s+)?Development", subject + " " + (t_ or ""), re.I))
    category = "Comp Plan Amendment" if kind == "CPA" else ("PD" if is_pd else "Rezoning")
    if kind == "CPA":
        app_type = "Large-Scale Comprehensive Plan Amendment" if re.search(r"Large", subject + staff_sq[:600], re.I) \
            else "Comprehensive Plan (FLUM) Amendment"
    elif re.search(r"Design\s+District", subject, re.I):
        app_type = "Design District Assignment"
    else:
        app_type = "PUD Rezoning" if is_pd else "Rezoning"

    hearings = []
    for i, a in enumerate(apps):
        res = minutes.get((a["body"], a["date"]), ({}, {}))[0].get(key)
        later = apps[i + 1:]
        if res is None and a["body"] == CC and a["reading"] == "first" and \
                any(b["body"] == CC and b["reading"] == "second" for b in later):
            res = "Approved"
        if a["body"] == CC and a["reading"] == "first" and res == "Approved":
            res = "Approved (first reading)"
        if a["body"] == LPA and res in ("Approved", "Denied"):
            res = "Recommended " + ("Approval" if res == "Approved" else "Denial")
        body = a["body"] + (f" ({a['reading']} reading)" if a["reading"] else "")
        h = {"body": body, "date": a["date"], "result": res}
        if h not in hearings:
            hearings.append(h)
    status, raw, decision = status_from(hearings, today)
    desc = subject if not case_no else f"{subject} (case {case_no})"
    if len(desc) > 300:
        desc = desc[:297] + "..."
    pname = addr
    return {
        "jurisdiction": JUR, "county": COUNTY, "app_num": f"Ordinance {key}", "category": category,
        "app_type": app_type, "project_name": pname, "description": desc,
        "applicant": applicant, "parcel_ids": ids, "address": addr, "acres": acres,
        "from_zoning": f_ if kind == "RZ" else None, "to_zoning": t_ if kind == "RZ" else None,
        "from_flu": f_.rstrip(". ") if (kind == "CPA" and f_) else None, "to_flu": t_ if kind == "CPA" else None,
        "filed_date": min(a["first_seen"] for a in apps), "status": status, "status_raw": raw,
        "hearings": hearings, "decision_date": decision, "url": f"{PORTAL}/event/{latest['event_id']}/files",
        "docs_url": file_stream(latest["packet"]) if latest["packet"] else None,
        "lat": None, "lon": None, "source": SOURCE,
    }


def status_from(hearings, today):
    past = [h for h in hearings if h["date"] <= today]
    future = [h for h in hearings if h["date"] > today]
    if any(h["result"] == "Withdrawn" for h in hearings):
        h = next(h for h in hearings if h["result"] == "Withdrawn")
        return "Withdrawn", f"{h['body']} {h['date']}: withdrawn", None
    final = [h for h in past if h["result"] in ("Approved", "Denied") and h["body"].startswith(CC)]
    if final:
        h = final[-1]
        return h["result"], f"{h['body']} {h['date']}: {h['result']} (minutes)", h["date"]
    if future:
        return "Hearing Scheduled", f"{future[0]['body']} {future[0]['date']}", None
    if past and past[-1]["result"] == "Continued":
        return "Continued", f"{past[-1]['body']} {past[-1]['date']}: continued", None
    if past:
        lp = past[-1]
        if lp["body"].startswith(CC) and lp["result"] is None:
            return "Hearing Scheduled", f"{lp['body']} {lp['date']} held; outcome not yet in published minutes", None
        return "Filed", f"{lp['body']} {lp['date']}" + (f": {lp['result']}" if lp["result"] else "") + \
            "; next hearing not yet on an agenda", None
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
