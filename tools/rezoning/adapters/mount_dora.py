#!/usr/bin/env python3
"""City of Mount Dora (Lake County) rezoning / PUD / future land use map amendment adapter.

Source: CivicClerk public API (mountdorafl.api.civicclerk.com).
 * A case is first listed on a Development Review Committee (DRC) agenda, then the Planning & Zoning
   Commission, then City Council (first reading, final reading/adoption):
     DRC / P&Z: "Request for Rezoning; Round Lake Charter K-8 (Project Name); Z25-03 (Project Number);
                 31333 Round Lake Rd. (Project Address); Lake County Schools (Applicant); ... (Owner)"
     Council:   "Request Approval First Reading of Ordinance No. 2025-24, Change in Zoning, School Board
                 of Lake County, Round Lake Charter School (28.68 +/- Acres)"
   app_num = the city project number (Z25-03, FLUM25-03, PUD..); Council ordinance items carry no project
   number, so they are matched to a case by kind plus project / owner name words; an ordinance that
   cannot be matched becomes its own record ("Ordinance No. YYYY-NN").
 * Kept: Rezoning / Change in Zoning, PUD, Future Land Use Map / Land Use Amendment (small or large
   scale). Skipped: annexation-only, site plans, plats, conditional use, LDC text.
 * Parcel numbers are not in the agenda items; when a Council item has small text attachments they are
   searched for Lake County Alternate Keys / parcel numbers.
 * Outcomes: City Council minutes ("MOTION BY ... TO ADOPT ORDINANCE NO. 2025-26 ...; MOTION PASSED").
   P&Z minutes are scanned images, so P&Z results are usually unknown.
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

JURISDICTIONS = ["City of Mount Dora"]
JUR = "City of Mount Dora"
COUNTY = "Lake"
TENANT = "mountdorafl"
API = f"https://{TENANT}.api.civicclerk.com/v1"
PORTAL = f"https://{TENANT}.portal.civicclerk.com"
SOURCE = "Mount Dora CivicClerk (DRC, P&Z Commission and City Council agendas and minutes)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
LOOKBACK_DAYS = 200
MAX_ATT = 3_000_000
DRC = "DRC"
PZ = "Planning & Zoning Commission"
CC = "City Council"

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


def log(*a):
    print("[mount_dora]", *a, file=sys.stderr)


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
    s = _html.unescape(s).replace(" ", " ").replace("​", "").replace("­", "")
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
ORD_RE = re.compile(r"\bOrdinance\s*(?:No\.?|Number|#)?\s*:?\s*(\d{4})\s*-\s*(\d{1,3})\b", re.I)
PROJ_RE = re.compile(r"\b([A-Z]{1,6}\d{2}-\d{2,3}[A-Za-z]*)\s*\(Project\s+Number\)|"
                     r"Project\s+(?:Number|No\.?|No,|#)\s*:?\s*([A-Z]{1,6}\d{2}-\d{2,3}[A-Za-z]*)", re.I)
REQ_RE = re.compile(r"^\s*Re\w{0,2}est\s+for\s+(?:a\s+|the\s+)?(.+?)\s*[;:]", re.I)
CPA_RE = re.compile(r"Future\s+Land\s+Use|Land\s+Use\s+(?:Map\s+)?Amendment|\bFLUM?\b|Comprehensive\s+Plan\s+Amendment|"
                    r"Small[- ]Scale|Large[- ]Scale", re.I)
RZ_RE = re.compile(r"Rezon|Change\s+in\s+Zoning|Zoning\s+(?:Change|Map\s+Amendment)|\bPUD\b|Planned\s+Unit\s+Development", re.I)
OUT_RE = re.compile(r"Land\s+Development\s+Code|\bLDC\b|Site\s+Plan|Plat|Conditional\s+Use|Variance|Text\s+Amendment|"
                    r"Historic|Landscap|Sign", re.I)
STOP = set("""request approval first final reading adoption ordinance change zoning rezoning future land use map amendment
scale large property city mount dora project name number owner applicant site address location acres with
from into lake county florida planned unit development school board llc inc corp of and the for""".split())


def kind_of(text):
    if OUT_RE.search(text):
        return None
    if CPA_RE.search(text):
        return "CPA"
    if RZ_RE.search(text):
        return "RZ"
    return None


def fields_semicolon(name):
    """Parse 'Request for X; A (Project Name); Z25-03 (Project Number); ... (Owner)' items."""
    out = {}
    for part in re.split(r"\s*;\s*", name):
        m = re.match(r"(.+?)\s*\((Project Name|Project Number|Project Address|Site Address|Project Location|"
                     r"Site Location|Owner|Applicant|Agent)\)", part.strip(), re.I)
        if m:
            out.setdefault(m.group(2).lower(), m.group(1).strip(" ,"))
    return out


def tokens(s):
    s = re.sub(r"\b(?:small|large)[- ]scale\b", " ", (s or "").lower())
    out = set()
    for w in re.findall(r"[a-z0-9]{3,}", s):
        if w in STOP or w.isdigit():
            continue
        out.add(w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w)
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
    """{ordinance key or project number: result} from a minutes PDF."""
    d = sq(text)
    res = {}
    for m in MOTION_RE.finditer(d):
        after = d[m.end(): m.end() + 700]
        o = re.search(r"\b(passed|carried|failed)\b", after, re.I)
        if not o or o.group(1).lower() == "failed":
            continue
        span = after[:min(o.start(), 300)]
        keys = [f"{x.group(1)}-{int(x.group(2)):02d}" for x in ORD_RE.finditer(span)]
        keys += [(x.group(1) or x.group(2)).upper() for x in PROJ_RE.finditer(span)]
        keys += [x.upper() for x in re.findall(r"\b([A-Z]{1,6}\d{2}-\d{2,3})\b", span)]
        v = m.group(1).lower()
        val = ("Continued" if v.startswith(("tabl", "contin", "postp", "defer")) else
               "Denied" if v.startswith("den") else "Approved")
        for k in keys:
            res[k] = val
    return res


def date_words(s):
    for fmt in ("%B %d, %Y", "%B %d %Y"):
        try:
            return dt.datetime.strptime(sq(s), fmt).date().isoformat()
        except ValueError:
            pass
    return None


# ------------------------------------------------------------------ fetch
def body_of(ev):
    name = (ev.get("eventName") or "")
    if re.search(r"CANCEL|Workshop|Budget|Special Magistrate|Library|Parks|Arts|Historic|CRA|Committee\b(?<!Review Committee)",
                 name, re.I) and not re.search(r"Development Review", name, re.I):
        return None
    if re.search(r"Development Review|\bDRC\b", name, re.I):
        return DRC
    if re.search(r"Planning", name, re.I):
        return PZ
    if re.search(r"City Council", name, re.I):
        return CC
    return None


def fetch(since, refresh):
    if pypdf is None:
        raise RuntimeError("pypdf is required for the Mount Dora adapter")
    today = dt.date.today()
    refresh_set = {sq(r).upper() for r in refresh or [] if r}
    start_d = dt.date.fromisoformat(since) - dt.timedelta(days=LOOKBACK_DAYS)
    if refresh_set:
        start_d = min(start_d, today - dt.timedelta(days=730))
    end_d = today + dt.timedelta(days=75)
    events = list_events(start_d.isoformat(), end_d.isoformat())
    log(f"{len(events)} events {start_d}..{end_d}")

    entries = []   # one per agenda appearance
    min_files = {}
    n_meet = 0
    for ev in sorted(events, key=lambda e: e["startDateTime"]):
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
        for it in walk(mt.get("items")):
            name = strip_html(it.get("agendaObjectItemName"))
            base = {"date": mdate, "first_seen": first_seen, "body": body, "event_id": ev.get("id"),
                    "packet": packet, "name": name, "atts": it.get("attachmentsList") or []}
            rq = REQ_RE.match(name)
            if rq and body in (DRC, PZ):
                kind = kind_of(rq.group(1))
                if not kind:
                    continue
                f = fields_semicolon(name)
                pm = PROJ_RE.search(name)
                proj_no = (pm.group(1) or pm.group(2)).upper() if pm else None
                entries.append(dict(base, kind=kind, req=rq.group(1), proj_no=proj_no, ord=None,
                                    pname=f.get("project name"), owner=f.get("owner"), applicant=f.get("applicant"),
                                    address=f.get("project address") or f.get("site address") or
                                    f.get("project location") or f.get("site location"), reading=None))
                continue
            om = ORD_RE.search(name)
            if om and body == CC:
                rest = name[om.end():]
                kind = kind_of(rest)
                if not kind or (re.search(r"Annex", rest, re.I) and not RZ_RE.search(rest)):
                    continue
                rd = re.search(r"\b(First|Final|Second)\s+Reading", name, re.I)
                reading = rd.group(1).lower() if rd else ("final" if re.search(r"Adopt", name, re.I) else None)
                if reading == "second":
                    reading = "final"
                am = re.search(r"\(\s*([\d,]*\.?\d+)\s*(?:\+/-?|±)?\s*Acres?\s*\)", name.replace("­", ""), re.I)
                entries.append(dict(base, kind=kind, req=sq(rest).strip(" ,:"), proj_no=None,
                                    ord=f"{om.group(1)}-{int(om.group(2)):02d}", pname=None, owner=None, applicant=None,
                                    address=None, reading=reading,
                                    acres=float(am.group(1).replace(",", "")) if am else None))
    log(f"{n_meet} meetings read; {len(entries)} in-scope agenda entries")

    # group entries into cases
    cases = []
    for e in entries:
        tgt = None
        for c in cases:
            if c["kind"] != e["kind"]:
                continue
            if e["proj_no"] and e["proj_no"] in c["proj_nos"]:
                tgt = c
                break
            if e["ord"] and e["ord"] in c["ords"]:
                tgt = c
                break
        if tgt is None:
            et = tokens(e["pname"] or "") | (tokens(e["req"]) if e["ord"] else set())
            for c in cases:
                if c["kind"] != e["kind"] or (dt.date.fromisoformat(e["date"]) -
                                              dt.date.fromisoformat(c["last"])).days > 400:
                    continue
                if e["ord"] and c["ords"] and e["ord"] not in c["ords"]:
                    continue
                ok = len(et & c["tokens"]) >= min(2, len(c["tokens"]) or 99)
                if not ok and e["ord"] and c["owner"] and c["owner"].lower() in e["name"].lower():
                    ok = True
                if not ok and not e["ord"] and c["tokens"] and et and len(et & c["tokens"]) >= 1 and \
                        (e["address"] or "")[:8].lower() == (c["address"] or "")[:8].lower():
                    ok = True
                if ok:
                    tgt = c
                    break
        if tgt is None:
            tgt = {"kind": e["kind"], "proj_nos": [], "ords": [], "apps": [], "tokens": set(), "owner": None,
                   "address": None, "last": e["date"]}
            cases.append(tgt)
        tgt["apps"].append(e)
        tgt["last"] = max(tgt["last"], e["date"])
        if e["proj_no"] and e["proj_no"] not in tgt["proj_nos"]:
            tgt["proj_nos"].append(e["proj_no"])
        if e["ord"] and e["ord"] not in tgt["ords"]:
            tgt["ords"].append(e["ord"])
        tgt["tokens"] |= tokens(e["pname"] or "")
        tgt["owner"] = tgt["owner"] or e["owner"]
        tgt["address"] = tgt["address"] or e["address"]
    log(f"{len(cases)} cases")

    results = {}
    for k in sorted({(a["body"], a["date"]) for c in cases for a in c["apps"]}):
        if k in min_files:
            try:
                results[k] = minutes_results(pdf_text(get(min_files[k]).content))
            except Exception as e:  # noqa: BLE001
                log(f"minutes {k}: {e}")

    out, seen = [], set()
    for c in cases:
        try:
            rec = build(c, results, today.isoformat())
        except Exception as e:  # noqa: BLE001
            log(f"case {c['proj_nos'] or c['ords']}: skipped ({e})")
            continue
        ids = {rec["app_num"].upper()} | {p.upper() for p in c["proj_nos"]} | \
              {f"ORDINANCE NO. {o}" for o in c["ords"]} | {o for o in c["ords"]}
        first = rec["filed_date"]
        if first < since and not (ids & refresh_set or any(o in r for r in refresh_set for o in c["ords"])):
            continue
        if rec["app_num"] in seen:
            continue
        seen.add(rec["app_num"])
        out.append(rec)
    # zoning and FLUM cases of one project share the parcels; fill gaps from the sibling case
    for r in out:
        if not r["parcel_ids"]:
            sib = next((o for o in out if o is not r and o["parcel_ids"] and o["project_name"] == r["project_name"]
                        and o["address"] == r["address"] and r["project_name"]), None)
            if sib:
                r["parcel_ids"] = list(sib["parcel_ids"])
    return out


def build(c, results, today):
    apps = sorted(c["apps"], key=lambda a: (a["date"], {DRC: 0, PZ: 1, CC: 2}[a["body"]]))
    latest = apps[-1]
    kind = c["kind"]
    pref = [p for p in c["proj_nos"] if (kind == "CPA" and re.match(r"(FLUM?|LU|CPA|SSCPA|LSCPA)", p)) or
            (kind == "RZ" and re.match(r"(Z|RZ|PUD|PD)\d", p))]
    if pref:
        app_num = pref[-1]
    elif c["ords"]:
        app_num = f"Ordinance No. {c['ords'][0]}"
    elif c["proj_nos"]:
        app_num = c["proj_nos"][-1]
    else:
        raise ValueError(f"no project or ordinance number: {apps[0]['name'][:120]}")
    pz = [a for a in apps if a["body"] in (DRC, PZ)]
    pname = next((a["pname"] for a in reversed(pz) if a.get("pname")), None)
    owner = next((a["owner"] for a in pz if a.get("owner")), None)
    applicant = next((a["applicant"] for a in reversed(pz) if a.get("applicant")), None) or owner
    address = next((a["address"] for a in reversed(pz) if a.get("address")), None)
    acres = next((a.get("acres") for a in reversed(apps) if a.get("acres")), None)
    req = next((a["req"] for a in reversed(apps) if a["body"] == CC), None) or (pz[-1]["req"] if pz else None)
    ids = []
    for a in reversed(apps):
        for att in a["atts"]:
            if (att.get("fileSize") or 0) > MAX_ATT or not att.get("pdfVersionFullPath"):
                continue
            try:
                ids += ids_from_text(pdf_text(get(att["pdfVersionFullPath"]).content, 6))
            except Exception as e:  # noqa: BLE001
                log(f"{app_num}: attachment {att.get('fileName')}: {e}")
        if ids:
            break
    ids = list(dict.fromkeys(ids))
    is_pd = bool(re.search(r"\bPUD\b|Planned\s+Unit", " ".join(a["name"] for a in apps), re.I))
    category = "Comp Plan Amendment" if kind == "CPA" else ("PD" if is_pd else "Rezoning")
    if kind == "CPA":
        app_type = "Large-Scale FLUM Amendment" if re.search(r"Large", " ".join(a["name"] for a in apps), re.I) \
            else "Small-Scale FLUM Amendment"
    else:
        app_type = "PUD" if is_pd else "Rezoning"

    hearings = []
    for i, a in enumerate(apps):
        r = results.get((a["body"], a["date"]), {})
        res = None
        for k in ([a["ord"]] if a["ord"] else []) + c["proj_nos"] + c["ords"]:
            if k and k in r:
                res = r[k]
                break
        later = apps[i + 1:]
        if res is None and a["body"] == CC and a["reading"] == "first" and \
                any(b["body"] == CC and b["reading"] == "final" for b in later):
            res = "Approved"
        if a["body"] == CC and a["reading"] == "first" and res == "Approved":
            res = "Approved (first reading)"
        if a["body"] == PZ and res in ("Approved", "Denied"):
            res = "Recommended " + ("Approval" if res == "Approved" else "Denial")
        if a["body"] == DRC:
            res = None
        body = a["body"] + (f" ({a['reading']} reading)" if a["body"] == CC and a["reading"] else "")
        if a["body"] == CC and a["ord"]:
            body += f" Ord. {a['ord']}"
        h = {"body": body, "date": a["date"], "result": res}
        if h not in hearings:
            hearings.append(h)
    status, raw, decision = status_from(hearings, today)
    desc = req
    if c["ords"]:
        desc = f"{req} (Ordinance No. {', '.join(c['ords'])})" if req else f"Ordinance No. {', '.join(c['ords'])}"
    if desc and len(desc) > 300:
        desc = desc[:297] + "..."
    return {
        "jurisdiction": JUR, "county": COUNTY, "app_num": app_num, "category": category,
        "app_type": app_type, "project_name": pname, "description": desc,
        "applicant": applicant, "parcel_ids": ids, "address": address, "acres": acres,
        "from_zoning": None, "to_zoning": None, "from_flu": None, "to_flu": None,
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
