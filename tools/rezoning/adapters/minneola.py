#!/usr/bin/env python3
"""City of Minneola (Lake County) rezoning / PUD / future land use map amendment adapter.

Source: CivicClerk public API (minneolafl.api.civicclerk.com). Minneola moved its agendas to
CivicClerk in October 2026 (first published agenda: City Council 2026-10-20). Older agendas exist
only as PDFs on www.minneola.us, which sits behind a Cloudflare browser challenge (HTTP 403
"Just a moment...") for non-browser clients, so they cannot be read by this pipeline.
 * Land use cases are ordinances: "Ordinance 2026-20 Grassy Lake Industrial Rezoning - Second
   Reading - Quasi-Judicial". app_num = "Ordinance YYYY-NN". The item description carries the
   ordinance title (acreage, location, from/to districts).
 * Parcel numbers ('06-22-26-0003-000-06300') / Alternate Keys are taken from the text of the
   small attachments (staff review memos, ordinance); scanned applications have no text.
 * Outcomes: CivicClerk "Minutes" files once published (parsed conservatively), otherwise the
   status reflects the latest agenda.
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

JURISDICTIONS = ["City of Minneola"]
JUR = "City of Minneola"
TENANT = "minneolafl"
API = f"https://{TENANT}.api.civicclerk.com/v1"
PORTAL = f"https://{TENANT}.portal.civicclerk.com"
SOURCE = "Minneola CivicClerk (P&Z + City Council agendas)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
LOOKBACK_DAYS = 150
MAX_ATT = 3_000_000

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


def log(*a):
    print("[minneola]", *a, file=sys.stderr)


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


def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = _html.unescape(s).replace("\u00a0", " ").replace("\u200b", "")
    return sq(s)


def pdf_text(content, max_pages=4):
    rdr = pypdf.PdfReader(io.BytesIO(content))
    out = []
    for p in rdr.pages[:max_pages]:
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


ORD_RE = re.compile(r"\bOrdinance\s*(?:No\.?\s*)?(\d{4})\s*-\s*(\d{1,3})\b", re.I)
IN_RE = re.compile(r"Rezon|Comp(?:rehensive)?\s+Plan\s+Amendment|Future\s+Land\s+Use|\bFLUM?\b|\bPUD\b|"
                   r"Planned\s+Unit|Large[- ]Scale|Small[- ]Scale", re.I)
OUT_RE = re.compile(r"Annex|Impact\s+Fee|Land\s+Development\s+(?:Code|Regulations)|\bLDR\b|Text\s+Amendment|"
                    r"Moratorium|Budget|Millage|Franchise", re.I)


def classify(name, desc):
    m = ORD_RE.search(name)
    if not m:
        return None
    rest = name[m.end():]
    if OUT_RE.search(rest) or not IN_RE.search(rest + " " + (desc or "")):
        return None
    if not IN_RE.search(rest) and OUT_RE.search(desc or ""):
        return None
    kind = "CPA" if re.search(r"Comp(?:rehensive)?\s+Plan|Future\s+Land\s+Use|\bFLUM?\b|Large[- ]Scale|Small[- ]Scale",
                              rest, re.I) else "RZ"
    return f"{m.group(1)}-{int(m.group(2)):02d}", kind


def parse_desc(desc):
    d = sq(desc).replace("+/-", "+")
    out = {}
    m = re.search(r"([\d,]*\.?\d+)\s*\+?\s*(?:\+/-)?\s*Acres", d, re.I)
    if m:
        try:
            out["acres"] = float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    m = re.search(r"\bFrom\s+(.+?)\s+to\s+(.+?)(?:\s+Within|\s+For|;|,\s*Providing|$)", d, re.I)
    if m:
        out["from"], out["to"] = (sq(re.sub(r"[\"\u201c\u201d]", "", g)).strip(" ,") for g in m.groups())
    m = re.search(r"Generally Located\s+(.+?)(?:\s+From\s|\s+Within|;|$)", d, re.I)
    if m:
        out["location"] = m.group(1).strip(" ,")
    return out


def ids_from_text(t):
    pids = re.findall(r"\b\d{2}-\d{2}-\d{2}-\d{4}-\d{3}-\d{5}\b", t)
    keys = []
    for m in re.finditer(r"Alt(?:ernate)?\.?\s*Keys?\s*(?:Nos?\.?|Numbers?|#)?\s*:?\s*", t, re.I):
        seg = re.split(r"[A-Za-z]{4,}", t[m.end(): m.end() + 150])[0]
        keys += re.findall(r"\b\d{7}\b", seg)
    return list(dict.fromkeys(pids + keys))


def minutes_results(text):
    """{ord_no: word} from minutes text (motion after the ordinance is named)."""
    d = sq(text)
    res = {}
    for m in ORD_RE.finditer(d):
        ono = f"{m.group(1)}-{int(m.group(2)):02d}"
        tail = d[m.end(): m.end() + 2500]
        nxt = ORD_RE.search(tail)
        if nxt:
            tail = tail[:nxt.start()]
        mm = re.search(r"motion\s+to\s+(approve|adopt|deny|table|continue|postpone)\b.{0,600}?"
                       r"(motion\s+(?:carried|passed|failed)|carried|passed|failed)", tail, re.I)
        if not mm:
            continue
        if re.search(r"failed", mm.group(2), re.I):
            continue
        v = mm.group(1).lower()
        res.setdefault(ono, "Continued" if v in ("table", "continue", "postpone") else
                       ("Denied" if v == "deny" else "Approved"))
    return res


def fetch(since, refresh):
    if pypdf is None:
        raise RuntimeError("pypdf is required for the Minneola adapter")
    today = dt.date.today()
    refresh_ids = {}
    for r in refresh or []:
        m = re.search(r"(\d{4})\s*-\s*(\d{1,3})", r or "")
        if m:
            refresh_ids[f"{m.group(1)}-{int(m.group(2)):02d}"] = r
    start_d = dt.date.fromisoformat(since) - dt.timedelta(days=LOOKBACK_DAYS)
    if refresh_ids:
        start_d = min(start_d, today - dt.timedelta(days=730))
    end_d = today + dt.timedelta(days=75)
    events = list_events(start_d.isoformat(), end_d.isoformat())
    log(f"{len(events)} events {start_d}..{end_d}")
    cases, minutes_cache = {}, {}
    for ev in events:
        name = (ev.get("eventName") or "") + " " + (ev.get("categoryName") or "")
        if not ev.get("agendaId") or re.search(r"Redevelopment|\bCRA\b|Workshop|Budget", name, re.I):
            continue
        body = "Planning & Zoning Commission" if re.search(r"Planning", name, re.I) else "City Council"
        try:
            mt = get(f"{API}/Meetings/{ev['agendaId']}").json()
        except Exception as e:  # noqa: BLE001
            log(f"meeting {ev['agendaId']}: {e}")
            continue
        mdate = ev["startDateTime"][:10]
        files = ev.get("publishedFiles") or []
        pub = [f.get("publishOn")[:10] for f in files if f.get("type") == "Agenda" and f.get("publishOn")]
        first_seen = min(pub + [mdate])
        packet = next((f.get("fileId") for f in files if f.get("type") == "Agenda Packet"), None)
        mins = next((f.get("fileId") for f in files if f.get("type") == "Minutes"), None)
        if mins and mins not in minutes_cache:
            try:
                minutes_cache[mins] = minutes_results(pdf_text(get(file_stream(mins)).content, 60))
            except Exception as e:  # noqa: BLE001
                log(f"minutes {mdate}: {e}")
                minutes_cache[mins] = {}
        for it in walk(mt.get("items")):
            iname = strip_html(it.get("agendaObjectItemName"))
            desc = strip_html(it.get("agendaObjectItemDescription"))
            c = classify(iname, desc)
            if not c:
                continue
            ono, kind = c
            rd = re.search(r"\b(First|Second|Final)\s+Reading", iname, re.I)
            rec = cases.setdefault(ono, {"kind": kind, "apps": []})
            rec["apps"].append({"date": mdate, "first_seen": first_seen, "body": body,
                                "reading": rd.group(1).lower() if rd else None, "name": iname, "desc": desc,
                                "atts": it.get("attachmentsList") or [], "event_id": ev.get("id"),
                                "packet": packet, "minutes": mins})
    log(f"{len(cases)} in-scope ordinances")
    out = []
    for ono, c in sorted(cases.items()):
        first = min(a["first_seen"] for a in c["apps"])
        if first < since and ono not in refresh_ids:
            continue
        try:
            out.append(build(ono, c, minutes_cache, today.isoformat()))
        except Exception as e:  # noqa: BLE001
            log(f"Ordinance {ono}: skipped ({e})")
    for r in set(refresh_ids) - set(cases):
        log(f"refresh id {refresh_ids[r]} not on any CivicClerk agenda (agendas before 2026-10 are not on CivicClerk)")
    return out


def build(ono, c, minutes_cache, today):
    apps = sorted(c["apps"], key=lambda a: a["date"])
    latest = apps[-1]
    kind = c["kind"]
    desc = next((a["desc"] for a in reversed(apps) if a["desc"]), None)
    f = parse_desc(desc or "")
    pname = re.sub(r"\s*-\s*(First|Second|Final)\s+Reading.*$", "", latest["name"][ORD_RE.search(latest["name"]).end():],
                   flags=re.I)
    pname = sq(re.sub(r"Comp(?:rehensive)?\s+Plan\s+Amendment|Rezoning|Future\s+Land\s+Use(\s+Map)?\s+Amendment|"
                      r"Quasi-Judicial", "", pname, flags=re.I)).strip(" -:") or None
    # parcel ids + applicant from small text attachments
    ids, applicant, txts = [], None, []
    for a in latest["atts"]:
        if (a.get("fileSize") or 0) > MAX_ATT or not a.get("pdfVersionFullPath"):
            continue
        try:
            t = pdf_text(get(a["pdfVersionFullPath"]).content, 4)
        except Exception as e:  # noqa: BLE001
            log(f"{ono}: attachment {a.get('fileName')}: {e}")
            continue
        txts.append(t)
        ids += ids_from_text(t)
        if not applicant:
            m = re.search(r"WHEREAS,\s+(.{3,80}?)\s+\(the\s+[\u201c\"]Owner", sq(t))
            if m:
                applicant = m.group(1).strip(" ,")
    ids = list(dict.fromkeys(ids))
    acres = f.get("acres")
    is_pd = bool(re.search(r"\bPUD\b|Planned\s+Unit", (f.get("to") or "") + " " + latest["name"], re.I))
    category = "Comp Plan Amendment" if kind == "CPA" else ("PD" if is_pd else "Rezoning")
    app_type = ("Comprehensive Plan Amendment (FLUM)" if kind == "CPA" else ("PUD Rezoning" if is_pd else "Rezoning"))

    hearings = []
    for a in apps:
        res = minutes_cache.get(a["minutes"], {}).get(ono) if a["minutes"] else None
        if res == "Approved" and a["body"] == "City Council" and a["reading"] == "first":
            res = "Approved (first reading)"
        body = a["body"] + (f" ({a['reading']} reading)" if a["body"] == "City Council" and a["reading"] else "")
        if a["body"] != "City Council" and res == "Approved":
            res = "Recommended Approval"
        if a["body"] != "City Council" and res == "Denied":
            res = "Recommended Denial"
        h = {"body": body, "date": a["date"], "result": res}
        if h not in hearings:
            hearings.append(h)
    past = [h for h in hearings if h["date"] <= today]
    future = [h for h in hearings if h["date"] > today]
    final = [h for h in past if h["result"] in ("Approved", "Denied") and h["body"].startswith("City Council")]
    decision = None
    if final:
        status, decision = final[-1]["result"], final[-1]["date"]
        raw = f"{final[-1]['body']} {decision}: {final[-1]['result']} (minutes)"
    elif future:
        status, raw = "Hearing Scheduled", f"{future[0]['body']} {future[0]['date']}"
    elif past and past[-1]["result"] == "Continued":
        status, raw = "Continued", f"{past[-1]['body']} {past[-1]['date']}: continued"
    elif past:
        lp = past[-1]
        if lp["body"].startswith("City Council") and lp["result"] is None:
            status, raw = "Hearing Scheduled", f"{lp['body']} {lp['date']} held; outcome not yet in published minutes"
        else:
            status, raw = "Filed", f"{lp['body']} {lp['date']}" + (f": {lp['result']}" if lp["result"] else "")
    else:
        status, raw = "Filed", "On agenda"
    if desc and len(desc) > 300:
        desc = desc[:297] + "..."
    return {
        "jurisdiction": JUR, "county": "Lake", "app_num": f"Ordinance {ono}", "category": category,
        "app_type": app_type, "project_name": pname, "description": desc, "applicant": applicant,
        "parcel_ids": ids, "address": f.get("location"), "acres": acres,
        "from_zoning": f.get("from") if kind == "RZ" else None, "to_zoning": f.get("to") if kind == "RZ" else None,
        "from_flu": f.get("from") if kind == "CPA" else None, "to_flu": f.get("to") if kind == "CPA" else None,
        "filed_date": min(a["first_seen"] for a in apps), "status": status, "status_raw": raw,
        "hearings": hearings, "decision_date": decision, "url": f"{PORTAL}/event/{latest['event_id']}/files",
        "docs_url": file_stream(latest["packet"]) if latest["packet"] else None,
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
