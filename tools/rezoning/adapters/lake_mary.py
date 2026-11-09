#!/usr/bin/env python3
"""City of Lake Mary (Seminole County) rezoning / PUD amendment / future land use map amendment adapter.

Source: CivicClerk public API (lakemaryfl.api.civicclerk.com), Planning & Zoning Board and City
Commission meetings.
 * Lake Mary numbers every land use case: "2025-RZ-04, Ordinance No. 1720. A recommendation to the
   Mayor and City Commission regarding a request for rezoning of the property located immediately south
   of 2695 W. Lake Mary Blvd. ... from A-1 (Agriculture) to PO (Professional Office). Applicant: ..."
   app_num = the case number (YYYY-RZ-NN rezonings and PUD amendments, YYYY-LU-NN future land use map
   amendments). Text-only LU amendments (Comprehensive Plan policies) and ZTA / CU / SP / VAR cases
   are skipped.
 * The agenda item text gives the ordinance number, location, from/to districts and applicant. Lake Mary
   does not attach documents to agenda items, so parcel numbers and acreage are usually unavailable.
 * Outcomes: City Commission minutes ("Commissioner X made a motion to deny 2025-RZ-03 ... the motion
   was carried 5-0"). P&Z minutes are scanned, so P&Z results are usually unknown.
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

JURISDICTIONS = ["City of Lake Mary"]
JUR = "City of Lake Mary"
COUNTY = "Seminole"
TENANT = "lakemaryfl"
API = f"https://{TENANT}.api.civicclerk.com/v1"
PORTAL = f"https://{TENANT}.portal.civicclerk.com"
SOURCE = "Lake Mary CivicClerk (P&Z Board + City Commission agendas and minutes)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
LOOKBACK_DAYS = 150
MAX_ATT = 3_000_000
PZ = "Planning & Zoning Board"
CC = "City Commission"

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


def log(*a):
    print("[lake_mary]", *a, file=sys.stderr)


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
CASE_RE = re.compile(r"\b(20\d\d)\s*-\s*(RZ|LU)\s*-\s*(\d{1,3})\b", re.I)
ORD_NO_RE = re.compile(r"\bOrdinance\s*No\.?\s*(\d{3,5})\b", re.I)
TEXT_LU_RE = re.compile(r"Polic(?:y|ies)|Element|Text\s+Amendment|Expedited\s+State\s+Review|Table\s+GOP|"
                        r"Evaluation\s+and\s+Appraisal", re.I)
ORD_RE = CASE_RE  # minutes parsing keys on the case number


def norm_key(y, t, n):
    return f"{y}-{t.upper()}-{int(n):02d}"


def key_of(m):
    return norm_key(m.group(1), m.group(2), m.group(3))


def classify(name, desc):
    """(key, kind) for in-scope case items, else None. kind 'CPA' or 'RZ'."""
    m = CASE_RE.search(name)
    if not m or m.start() > 40:
        return None
    if re.match(r"\s*(?:Approval of|Draft)", name, re.I):
        return None
    if m.group(2).upper() == "LU":
        if TEXT_LU_RE.search(name) and not re.search(r"Future\s+Land\s+Use\s+Map|Small[- ]Scale", name, re.I):
            return None
        return key_of(m), "CPA"
    return key_of(m), "RZ"


def parse_title(name):
    t = sq(name.replace("\u2014", "-").replace("\u2013", "-").replace("\u2019", "'"))
    out = {}
    m = ORD_NO_RE.search(t)
    if m:
        out["ord"] = m.group(1)
    m = re.search(r"([\d,]*\.?\d+)\s*(?:\+/-|\+|\u00b1)?\s*acres", t, re.I)
    if m:
        try:
            out["acres"] = float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    m = re.search(r"\bfrom\s+((?!the\b)[A-Z][^;]{0,70}?),?\s+to\s+([A-Z][^;]{0,70}?)(?:\.\s|\.$|,?\s+Applicant|\s+\(Quasi|;|$)", t)
    if m:
        out["from"] = sq(m.group(1)).strip(" ,.")
        out["to"] = sq(m.group(2)).strip(" ,.")
    m = re.search(r"\blocated\s+(?:at\s+)?(.+?)(?:\s+from\s+[A-Z]|,?\s+Applicant:|\.\s+Applicant|\s+\(Quasi|$)", t)
    if m:
        loc = m.group(1).strip(" ,.")
        out["location"] = loc if len(loc) < 160 else None
    if not out.get("location"):
        m = re.search(r"\bat\s+(\d{2,6}\s+[A-Z][^.]{2,50}?(?:Rd|Road|St|Street|Ave|Avenue|Blvd|Ln|Lane|Dr|Drive|Pkwy|Way|Ct|Cir|Loop|Pl|Trl|Hwy)\.?)(?=[\s.,]|$)", t)
        if m:
            out["location"] = m.group(1).strip(" ,.")
    m = re.search(r"Applicant:\s*(.+?)(?:\.?\s*\((?:Quasi|Public|Legislative)|$)", t)
    if m:
        out["applicant"] = m.group(1).strip(" ,.")
    m = re.search(r"(?:Amendment to (?:the )?|request for )(.+?(?:Planned Unit Development|PUD)(?: \(PUD\))?)", t, re.I)
    if m:
        out["project"] = re.sub(r"^(?:the\s+)?(?:(?:First|Second|Third|Fourth|Fifth|Sixth)\s+)?Amendment\s+to\s+(?:the\s+)?",
                                "", m.group(1), flags=re.I).strip()
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
        keys = [key_of(x) for x in ORD_RE.finditer(after[:min(o.start(), 300)])]
        v = m.group(1).lower()
        val = ("Continued" if v.startswith(("tabl", "contin", "postp", "defer")) else
               "Denied" if v.startswith("den") else "Approved")
        for k in keys:
            res[k] = val
    # fallback: the first motion after an ordinance heading
    for m in ORD_RE.finditer(d):
        key = key_of(m)
        if key in res:
            continue
        tail = d[m.end(): m.end() + 3000]
        for nm in ORD_RE.finditer(tail):
            if key_of(nm) != key:
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
    if re.search(r"CANCEL|Work\s*Shop|Workshop|Budget|Code Enforcement|Pension|Parks|Elder|Advisory", name, re.I):
        return None
    if re.search(r"Planning", name, re.I):
        return PZ
    if re.search(r"Commission|Special Called Meeting", name, re.I):
        return CC
    return None


def fetch(since, refresh):
    if pypdf is None:
        raise RuntimeError("pypdf is required for the Lake Mary adapter")
    today = dt.date.today()
    refresh_ids = {}
    for r in refresh or []:
        m = CASE_RE.search(r or "")
        if m:
            refresh_ids[key_of(m)] = r
    start_d = dt.date.fromisoformat(since) - dt.timedelta(days=LOOKBACK_DAYS)
    if refresh_ids:
        yr = min(int(k[:4]) for k in refresh_ids)
        start_d = min(start_d, max(dt.date(yr, 1, 1), today - dt.timedelta(days=900)))
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
            log(f"{key}: skipped ({e})")
    for r in set(refresh_ids) - set(cases):
        log(f"refresh id {refresh_ids[r]} not found on any agenda since {start_d}")
    return out


def build(key, c, results, today):
    apps = sorted(c["apps"], key=lambda a: (a["date"], a["body"] == CC))
    latest = apps[-1]
    kind = c["kind"]
    cc_names = [a["name"] for a in apps if a["body"] == CC]
    name = max(cc_names or [a["name"] for a in apps], key=len)
    f = {}
    for a in apps:
        for k2, v in parse_title(a["name"]).items():
            if v and k2 not in f:
                f[k2] = v
    ids = []
    for a in reversed(apps):
        for att in a["atts"]:
            if (att.get("fileSize") or 0) > MAX_ATT or not att.get("pdfVersionFullPath"):
                continue
            try:
                ids += ids_from_text(pdf_text(get(att["pdfVersionFullPath"]).content, 6))
            except Exception as e:  # noqa: BLE001
                log(f"{key}: attachment {att.get('fileName')}: {e}")
        if ids:
            break
    ids = list(dict.fromkeys(ids))
    alltxt = " ".join(a["name"] for a in apps)
    is_pd = bool(re.search(r"\bPUD\b|Planned\s+Unit\s+Development", alltxt + " " + (f.get("to") or ""), re.I))
    category = "Comp Plan Amendment" if kind == "CPA" else ("PD" if is_pd else "Rezoning")
    if kind == "CPA":
        app_type = "Large-Scale FLUM Amendment" if re.search(r"Large[- ]Scale", alltxt, re.I) else \
            "Small-Scale FLUM Amendment"
    elif is_pd and re.search(r"Amendment\s+to\s+(?:the\s+)?.{0,80}(?:Planned Unit Development|PUD)", alltxt, re.I):
        app_type = "PUD Amendment"
    else:
        app_type = "PUD Rezoning" if is_pd else "Rezoning"

    hearings = []
    for i, a in enumerate(apps):
        res = results.get((a["body"], a["date"]), {}).get(key)
        later = apps[i + 1:]
        if res is None and re.search(r"REQUEST TO CONTINUE|\bcontinue[d]? to\b", a["name"], re.I):
            res = "Continued"
        if res is None and a["body"] == CC and a["reading"] == "first" and \
                any(b["body"] == CC and b["reading"] in ("second", "final") for b in later):
            res = "Approved"
        if res is None and any(b["body"] == a["body"] and b["reading"] == a["reading"] for b in later) and \
                a["date"] <= today:
            res = "Continued"
        if a["body"] == CC and a["reading"] == "first" and res == "Approved":
            res = "Approved (first reading)"
        if a["body"] == PZ and res in ("Approved", "Denied"):
            res = "Recommended " + ("Approval" if res == "Approved" else "Denial")
        body = a["body"] + (f" ({a['reading']} reading)" if a["reading"] else "")
        h = {"body": body, "date": a["date"], "result": res}
        if h not in hearings:
            hearings.append(h)
    status, raw, decision = status_from(hearings, today)
    desc = re.sub(r"^\s*" + re.escape(key) + r"\s*,?\s*", "", sq(name))
    desc = re.sub(r"\s*\((?:Quasi|Public|Legislative)[^)]*\)\s*\([^)]*\)\s*$", "", desc)
    desc = re.sub(r"^Ordinance No\.?\s*\d+[.,]\s*", "", desc)
    desc = re.sub(r"^A\s+recommendation\s+to\s+the\s+Mayor\s+and\s+City\s+Commission\s+(?:regarding|to\s+consider)\s+(?:an?\s+)?",
                  "", desc, flags=re.I)
    desc = re.sub(r"^(?:A\s+)?request\s+(?:to\s+the\s+Mayor\s+and\s+City\s+Commission\s+)?(?:to\s+consider\s+|for\s+)?(?:the\s+|a\s+)?",
                  "", desc, flags=re.I)
    desc = (desc[:1].upper() + desc[1:]) if desc else None
    if f.get("ord") and desc:
        desc = f"{desc} (Ordinance No. {f['ord']})"
    if desc and len(desc) > 300:
        desc = desc[:297] + "..."
    return {
        "jurisdiction": JUR, "county": COUNTY, "app_num": key, "category": category,
        "app_type": app_type, "project_name": f.get("project"), "description": desc,
        "applicant": f.get("applicant"), "parcel_ids": ids, "address": f.get("location"), "acres": f.get("acres"),
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
