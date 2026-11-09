#!/usr/bin/env python3
"""City of Tavares (Lake County) rezoning / PD / future land use map amendment adapter.

Source: CivicClerk public API (tavaresfl.api.civicclerk.com).
 * Every land use case is a numbered ordinance heard first by the Planning & Zoning Board (sitting
   as the LPA) and then by City Council (first and second reading):
   "Ordinance 2026-07 YMCA Expansion - Rezoning 15.50 Acres on the West Side of David Walker Drive,
   ... from "PD" Planned Development to "PFD" Public Facilities District (Community Development)".
   app_num = "Ordinance YYYY-NN" (the only case number the city publishes). Rezonings,
   annex-and-rezone ordinances and small/large scale FLUM amendments are kept; annexation-only,
   LDR text, impact fee and incentive ordinances are skipped.
 * The item title carries project name, acreage, location and from/to designations. Council
   items sit under "First Reading" / "Second Reading" headings.
 * Tavares does not post the ordinance, maps or parcel numbers ("Attachments available to the
   public upon request"), so parcel_ids are normally empty.
 * Outcomes: City Council minutes (CivicClerk "Minutes" file, or the draft minutes attached to the
   "Approval of the <date> City Council Meeting Minutes" item of a later agenda). P&Z minutes are
   scanned images, so P&Z results are usually unknown. A later second reading implies the first
   reading passed.
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

JURISDICTIONS = ["City of Tavares"]
JUR = "City of Tavares"
COUNTY = "Lake"
TENANT = "tavaresfl"
API = f"https://{TENANT}.api.civicclerk.com/v1"
PORTAL = f"https://{TENANT}.portal.civicclerk.com"
SOURCE = "Tavares CivicClerk (P&Z Board + City Council agendas, packets and minutes)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
LOOKBACK_DAYS = 150
MAX_ATT = 3_000_000
PZ = "Planning & Zoning Board"
CC = "City Council"

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


def log(*a):
    print("[tavares]", *a, file=sys.stderr)


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


def walk(items, parent=""):
    for it in items or []:
        yield it, parent
        yield from walk(it.get("childItems"), strip_html(it.get("agendaObjectItemName")))


# ------------------------------------------------------------------ city specific parsing
ORD_RE = re.compile(r"\bOrdinance\s*(?:No\.?\s*)?(\d{4})\s*-\s*(\d{1,3})\b", re.I)
IN_RE = re.compile(r"Rezon|Zoning\s+(?:Map\s+)?(?:Change|Amendment)|Comp(?:rehensive)?\s+Plan\s+(?:FLUM\s+)?Amendment|"
                   r"Future\s+Land\s+Use|\bFLUM?\b|\bPUD\b|Planned\s+Development|Large[- ]Scale|Small[- ]Scale", re.I)
OUT_RE = re.compile(r"Land\s+Development\s+Regulations|\bLDR\b|Chapter\s+\d|Impact\s+Fee|Incentive|Interlocal|"
                    r"Text\s+Amendment|Moratorium|Budget|Millage|Franchise|Recovery\s+Residence|Boundary", re.I)
CPA_RE = re.compile(r"Comp(?:rehensive)?\s+Plan|Future\s+Land\s+Use|\bFLUM?\b|Large[- ]Scale|Small[- ]Scale", re.I)


def norm_key(y, n):
    return f"{y}-{int(n):02d}"


def classify(name, desc):
    """(key, kind) for in-scope ordinance items, else None. kind 'CPA' or 'RZ'."""
    m = ORD_RE.search(name)
    if not m:
        return None
    rest = name[m.end():]
    if OUT_RE.search(rest) or not IN_RE.search(rest):
        return None
    if re.search(r"Annex", rest, re.I) and not re.search(r"Rezon|Zoning", rest, re.I):
        return None
    return norm_key(m.group(1), m.group(2)), ("CPA" if CPA_RE.search(rest) else "RZ")


def parse_title(name):
    t = sq(name.replace("\u2014", "-").replace("\u2013", "-"))
    out = {}
    m = ORD_RE.search(t)
    rest = t[m.end():] if m else t
    pm = re.match(r"\s*(.+?)\s+-\s+", rest)
    if pm:
        out["project"] = pm.group(1).strip(" -:")
    m = re.search(r"([\d,]*\.?\d+)\s*(?:\+/-|\+)?\s*Acres", t, re.I)
    if m:
        try:
            out["acres"] = float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    m = re.search(r"\bfrom\s+(.+?)\s+to\s+(.+?)(?:\s+Designation)?\s*(?:\((?:Community|Planning)[^)]*\))?\s*$",
                  t, re.I)
    if m:
        out["from"] = sq(m.group(1).replace('"', "")).strip(" ,")
        out["to"] = sq(m.group(2).replace('"', "")).strip(" ,")
    m = re.search(r"Acres\s+(?:Located\s+)?((?:on|at|along|near|east|west|north|south)\b.+?)(?:,?\s+from\s|\s*\(|$)", t, re.I)
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


MOTION_RE = re.compile(r"(?:motion|moved)\b.{0,250}?\bto\s+(?:recommend\s+)?(?:the\s+)?(approv\w*|adopt\w*|den\w+|tabl\w*|"
                       r"continu\w*|postpon\w*|defer\w*|transmit\w*)\b", re.I)


def minutes_results(text):
    """{key: 'Approved'|'Denied'|'Continued'|'Withdrawn'} from minutes text."""
    d = sq(text)
    res = {}
    # motions that name the ordinance ("... made a motion to approve Ordinance 2026-07 ... motion carried")
    for m in MOTION_RE.finditer(d):
        after = d[m.end(): m.end() + 700]
        o = re.search(r"\b(passed|carried|failed)\b", after, re.I)
        if not o or o.group(1).lower() == "failed":
            continue
        keys = [norm_key(x.group(1), x.group(2)) for x in ORD_RE.finditer(after[:min(o.start(), 300)])]
        v = m.group(1).lower()
        val = ("Continued" if v.startswith(("tabl", "contin", "postp", "defer")) else
               "Denied" if v.startswith("den") else "Approved")
        for k in keys:
            res[k] = val
    # fallback: the first motion after an ordinance heading
    for m in ORD_RE.finditer(d):
        key = norm_key(m.group(1), m.group(2))
        if key in res:
            continue
        tail = d[m.end(): m.end() + 3000]
        for nm in ORD_RE.finditer(tail):
            if norm_key(nm.group(1), nm.group(2)) != key:
                tail = tail[:nm.start()]
                break
        if re.search(r"\bwithdr[ae]wn\b", tail[:400], re.I):
            res[key] = "Withdrawn"
            continue
        mm = MOTION_RE.search(tail)
        if not mm:
            continue
        o = re.search(r"\b(passed|carried|failed)\b", tail[mm.end(): mm.end() + 700], re.I)
        if not o or o.group(1).lower() == "failed":
            continue
        v = mm.group(1).lower()
        res[key] = ("Continued" if v.startswith(("tabl", "contin", "postp", "defer")) else
                    "Denied" if v.startswith("den") else "Approved")
    return res


MIN_ITEM_RE = re.compile(r"Approv\w*\s+of\s+(?:the\s+)?(\w+\s+\d{1,2},?\s+\d{4}),?\s+(.*?)Minutes", re.I)


def date_words(s):
    for fmt in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y"):
        try:
            return dt.datetime.strptime(sq(s), fmt).date().isoformat()
        except ValueError:
            pass
    return None


# ------------------------------------------------------------------ fetch
def body_of(ev):
    name = (ev.get("eventName") or "") + " | " + (ev.get("categoryName") or "")
    if re.search(r"CANCEL|Workshop|Budget|Code Enforcement|Magistrate|Pension|Library|CRA|Redevelopment", name, re.I):
        return None
    if re.search(r"Planning", name, re.I):
        return PZ
    if re.search(r"Council", name, re.I):
        return CC
    return None


def fetch(since, refresh):
    if pypdf is None:
        raise RuntimeError("pypdf is required for the Tavares adapter")
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

    cases, min_files, min_items = {}, {}, {}
    n_meet = 0
    for ev in events:
        body = body_of(ev)
        if not body or not ev.get("agendaId"):
            continue
        try:
            mt = get(f"{API}/Meetings/{ev['agendaId']}").json()
        except Exception as e:  # noqa: BLE001
            log(f"meeting {ev['agendaId']}: {e}")
            continue
        n_meet += 1
        mdate = ev["startDateTime"][:10]
        files = ev.get("publishedFiles") or []
        pub = [f.get("publishOn")[:10] for f in files if f.get("type") == "Agenda" and f.get("publishOn")]
        first_seen = min(pub + [mdate])
        packet = next((f.get("fileId") for f in files if f.get("type") == "Agenda Packet"), None)
        mins = next((f.get("fileId") for f in files if f.get("type") == "Minutes"), None)
        if mins:
            min_files[(body, mdate)] = file_stream(mins)
        for it, parent in walk(mt.get("items")):
            iname = strip_html(it.get("agendaObjectItemName"))
            desc = strip_html(it.get("agendaObjectItemDescription"))
            mm = MIN_ITEM_RE.search(iname)
            if mm:
                d = date_words(mm.group(1))
                att = next((a for a in it.get("attachmentsList") or [] if a.get("pdfVersionFullPath")), None)
                if d and att:
                    mbody = PZ if re.search(r"Planning", mm.group(2) + iname, re.I) else (
                        CC if re.search(r"Council", mm.group(2) + iname, re.I) else body)
                    min_items.setdefault((mbody, d), att["pdfVersionFullPath"])
            c = classify(iname, desc)
            if not c:
                continue
            key, kind = c
            rd = re.search(r"\b(First|Second|Final)\s+Reading", iname + " " + parent, re.I)
            rec = cases.setdefault(key, {"kind": kind, "apps": []})
            rec["apps"].append({"date": mdate, "first_seen": first_seen, "body": body,
                                "reading": rd.group(1).lower() if (rd and body == CC) else None,
                                "name": iname, "desc": desc, "atts": it.get("attachmentsList") or [],
                                "event_id": ev.get("id"), "packet": packet})
    log(f"{n_meet} meetings read; {len(cases)} in-scope ordinances")

    results = {}
    needed = {(a["body"], a["date"]) for c in cases.values() for a in c["apps"]}
    for k in sorted(needed):
        url = min_files.get(k) or min_items.get(k)
        if not url:
            continue
        try:
            results[k] = minutes_results(pdf_text(get(url).content))
        except Exception as e:  # noqa: BLE001
            log(f"minutes {k}: {e}")

    out = []
    for key, c in sorted(cases.items()):
        first = min(a["first_seen"] for a in c["apps"])
        if first < since and key not in refresh_ids:
            continue
        try:
            out.append(build(key, c, results, today.isoformat()))
        except Exception as e:  # noqa: BLE001
            log(f"Ordinance {key}: skipped ({e})")
    for r in set(refresh_ids) - set(cases):
        log(f"refresh id {refresh_ids[r]} not found on any agenda since {start_d}")
    return out


def build(key, c, results, today):
    apps = sorted(c["apps"], key=lambda a: (a["date"], a["body"] == CC))
    latest = apps[-1]
    kind = c["kind"]
    name = max((a["name"] for a in apps), key=len)
    f = parse_title(name)
    ids, txt = [], ""
    for a in reversed(apps):
        for att in a["atts"]:
            if (att.get("fileSize") or 0) > MAX_ATT or not att.get("pdfVersionFullPath"):
                continue
            try:
                t = pdf_text(get(att["pdfVersionFullPath"]).content, 6)
            except Exception as e:  # noqa: BLE001
                log(f"{key}: attachment {att.get('fileName')}: {e}")
                continue
            txt += "\n" + t
            ids += ids_from_text(t)
        if ids:
            break
    ids = list(dict.fromkeys(ids))
    applicant = None
    m = re.search(r"\bapplicant(?:\s+is|,)?\s+(?!is\b)([A-Z][\w&.,' -]{2,60}?(?:LLC|Inc\.?|Corp\.?|Company|Trust|L\.?P\.?))", sq(txt))
    if m:
        applicant = m.group(1)
    is_pd = bool(re.search(r"\bPUD\b|\bPD\b|Planned\s+(?:Unit\s+)?Development", f.get("to") or "", re.I))
    category = "Comp Plan Amendment" if kind == "CPA" else ("PD" if is_pd else "Rezoning")
    if kind == "CPA":
        app_type = "Large-Scale FLUM Amendment" if re.search(r"Large", name, re.I) else "Small-Scale FLUM Amendment"
    else:
        app_type = ("Annexation and Rezoning" if re.search(r"Annex", name, re.I) else "Rezoning") + \
                   (" (PD)" if is_pd else "")

    hearings = []
    for i, a in enumerate(apps):
        res = results.get((a["body"], a["date"]), {}).get(key)
        later = apps[i + 1:]
        if res is None and a["body"] == CC and a["reading"] == "first" and \
                any(b["body"] == CC and b["reading"] in ("second", "final") for b in later):
            res = "Approved"
        if res is None and a["body"] == PZ and any(b["body"] == CC for b in later) and a["date"] <= today:
            res = None  # forwarded to Council; P&Z vote itself is not published as text
        if a["body"] == CC and a["reading"] == "first" and res == "Approved":
            res = "Approved (first reading)"
        if a["body"] == PZ and res in ("Approved", "Denied"):
            res = "Recommended " + ("Approval" if res == "Approved" else "Denial")
        body = a["body"] + (f" ({a['reading']} reading)" if a["reading"] else "")
        h = {"body": body, "date": a["date"], "result": res}
        if h not in hearings:
            hearings.append(h)
    status, raw, decision = status_from(hearings, today)
    desc = name[ORD_RE.search(name).end():].strip(" -:") if ORD_RE.search(name) else name
    desc = re.sub(r"\s*\((?:Community|Planning)[^)]*\)\s*$", "", desc)
    if len(desc) > 300:
        desc = desc[:297] + "..."
    return {
        "jurisdiction": JUR, "county": COUNTY, "app_num": f"Ordinance {key}", "category": category,
        "app_type": app_type, "project_name": f.get("project"), "description": desc or None,
        "applicant": applicant, "parcel_ids": ids, "address": f.get("location"), "acres": f.get("acres"),
        "from_zoning": f.get("from") if kind == "RZ" else None, "to_zoning": f.get("to") if kind == "RZ" else None,
        "from_flu": f.get("from") if kind == "CPA" else None, "to_flu": f.get("to") if kind == "CPA" else None,
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
