#!/usr/bin/env python3
"""City of Port Orange (Volusia County) rezoning / PUD / future land use map amendment adapter.

Source: CivicClerk public API (portorangefl.api.civicclerk.com), same platform as clermont.py.
 * Port Orange numbers every land use application: REZONING-26-0002 (conventional / administrative
   rezoning -> Rezoning), PRZA-26-0001 (PUD / PCD rezoning or Master Development Agreement amendment
   -> PD), CPAM-26-0001 (comprehensive plan amendment -> Comp Plan Amendment; capital improvements
   element / text-only amendments are skipped). DCAM (code text), VARC (variance), CUSE (special
   exception), SUBD / SPIN (plats, site plans) are out of scope.
 * A case first appears on a Staff Development Review Committee (SDRC) agenda (item name = case
   number only), then on a Planning Commission agenda ("APPLICATION: ... CASE NO.: PRZA-26-0001
   APPLICANT: ..." with the staff report attached), then City Council "First Reading - Ordinance
   No. 2026-10 ... (Case No. PRZA-26-0001)" and "Second Reading".
 * The staff report (first pages) gives applicant, location, acreage and sometimes parcel numbers.
 * Results: the Council item summary carries the Planning Commission vote ("Planning Commission
   Recommendation (2/26/26): Recommended Approval 3-2"). A first reading followed by a second
   reading means it passed first reading. Port Orange minutes are scanned images (no text), so the
   final (second reading) outcome is inferred: once the minutes of that meeting are published and
   the item does not come back on a later agenda, the ordinance is treated as adopted (status_raw
   says so). Items titled withdrawn / continued / postponed are handled explicitly.
app_num is the case number. Parcel IDs: Volusia parcel numbers as posted (dashed 12-digit form).
"""
import datetime as dt
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

JURISDICTIONS = ["City of Port Orange"]
JUR = "City of Port Orange"
TENANT = "portorangefl"
API = f"https://{TENANT}.api.civicclerk.com/v1"
PORTAL = f"https://{TENANT}.portal.civicclerk.com"
SOURCE = "Port Orange CivicClerk (SDRC, Planning Commission, City Council agendas + staff reports)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
LOOKBACK_DAYS = 180     # scan this far before `since` to know when a case first appeared
LOOKAHEAD_DAYS = 75
CASE_RX = re.compile(r"\b((?:REZONING|PRZA|PRZ|PUD|CPAM|SSCPA|LSCPA)-\d{2,4}-\d{4})\b")
TEXT_CPA = re.compile(r"Capital Improvements? Element|Element Update|text amendment|EAR[- ]based|"
                      r"Evaluation and Appraisal", re.I)

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


def log(*a):
    print("[port_orange]", *a, file=sys.stderr, flush=True)


def get(url, params=None, tries=3, sleep=0.35, **kw):
    last = None
    for i in range(tries):
        try:
            r = _s.get(url, params=params, timeout=90, **kw)
            r.raise_for_status()
            time.sleep(sleep)
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1) ** 2)
    raise RuntimeError(f"GET failed {url}: {last}")


def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = s.replace("&nbsp;", " ").replace(" ", " ").replace("&amp;", "&").replace("​", "")
    s = s.replace("&ndash;", "-").replace("&mdash;", "-").replace("&rsquo;", "'").replace("&lsquo;", "'")
    s = re.sub(r"&#?\w+;", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def clean(v):
    v = re.sub(r"\s+", " ", v or "").strip(" .,;:")
    return v or None


def norm_case(c):
    m = re.match(r"^([A-Z]+)-(\d{2,4})-(\d{4})$", c.upper())
    if not m:
        return c.upper()
    yy = m.group(2)[-2:]
    return f"{m.group(1)}-{yy}-{m.group(3)}"


def walk(items):
    for it in items or []:
        yield it
        yield from walk(it.get("childItems"))


_pdf_cache = {}


def pdf_text(url, max_pages=6):
    if not url or pypdf is None:
        return ""
    if url in _pdf_cache:
        return _pdf_cache[url]
    try:
        r = get(url)
        rd = pypdf.PdfReader(io.BytesIO(r.content))
        out = []
        for p in rd.pages[:max_pages]:
            try:
                out.append(p.extract_text() or "")
            except Exception:  # noqa: BLE001
                out.append("")
        txt = "\n".join(out)
    except Exception as e:  # noqa: BLE001
        log(f"pdf unreadable: {e}")
        txt = ""
    _pdf_cache[url] = txt
    return txt


# ------------------------------------------------------------------ agenda scan
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


def body_of(ev):
    name = ev.get("eventName") or ""
    cat = ev.get("categoryName") or ""
    if re.search(r"cancel|pension|CRA\b|Redevelopment|CDBG|workshop|budget", name, re.I):
        return None
    if re.search(r"Planning Commission", name + " " + cat, re.I):
        return "Planning Commission"
    if re.search(r"SDRC|Staff Development Review", name, re.I):
        return "DRC"
    if re.search(r"City Council", name, re.I):
        return "City Council"
    return None


def scan(start, end):
    events = list_events(start, end)
    cases = {}
    n = 0
    for ev in events:
        body = body_of(ev)
        if not body or not ev.get("agendaId"):
            continue
        try:
            mt = get(f"{API}/Meetings/{ev['agendaId']}").json()
        except Exception as e:  # noqa: BLE001
            log(f"meeting {ev['agendaId']}: {e}")
            continue
        n += 1
        date = ev["startDateTime"][:10]
        files = {f.get("type"): f.get("fileId") for f in ev.get("publishedFiles") or []}
        for it in walk(mt.get("items")):
            iname = strip_html(it.get("agendaObjectItemName"))
            desc = strip_html(it.get("agendaObjectItemDescription"))
            atts = it.get("attachmentsList") or []
            fnames = " ".join(a.get("fileName") or "" for a in atts)
            found = [norm_case(c) for c in CASE_RX.findall(iname)]
            if not found and body != "DRC":
                found = [norm_case(c) for c in CASE_RX.findall(fnames)]
            if not found:
                continue
            if body == "City Council" and not re.search(r"Ordinance|Reading|Rezon|Amendment", iname, re.I):
                continue
            reading = None
            m = re.search(r"\b(First|Second)\s+Reading", iname, re.I)
            if m:
                reading = m.group(1).lower()
            staff = [a for a in atts if re.search(r"staff\s*report", a.get("fileName") or "", re.I)]
            ordm = re.search(r"Ordinance\s+(?:No\.?\s*)?(\d{4}-\d{1,3})", iname, re.I)
            # the item's own case is the one named in the item title; others are companions
            for c in dict.fromkeys(found[:1] if body == "City Council" else found):
                cases.setdefault(c, []).append({
                    "date": date, "body": body, "reading": reading, "name": iname, "desc": desc,
                    "event_id": ev["id"], "staff": staff[0].get("pdfVersionFullPath") if staff else None,
                    "ordinance": ordm.group(1) if ordm else None, "minutes": bool(files.get("Minutes")),
                    "packet": files.get("Agenda Packet"), "agenda": files.get("Agenda"),
                })
    log(f"{n} SDRC / Planning Commission / City Council agendas read {start}..{end}; {len(cases)} case numbers")
    return cases


# ------------------------------------------------------------------ details
def category_of(case, texts):
    pre = case.split("-")[0]
    t = " ".join(texts)
    if pre in ("CPAM", "SSCPA", "LSCPA"):
        if TEXT_CPA.search(t) and not re.search(r"Future Land Use Map|FLUM|Future Land Use Amendment|"
                                                 r"Future Land Amendment|map amendment", t, re.I):
            return None
        return "Comp Plan Amendment"
    if pre in ("PRZA", "PRZ", "PUD"):
        return "PD"
    if pre == "REZONING":
        return "PD" if re.search(r"\bPUD\b|Planned Unit Development|Planned Commercial Development|\bPCD\b", t) else "Rezoning"
    return None


def parse_staff(txt):
    flat = re.sub(r"[ \t]+", " ", txt or "")
    out = {}
    m = re.search(r"APPLICANTS?(?:/PROPERTY OWNER)?:?\s+(.+?)\n", flat)
    if m:
        out["applicant"] = clean(m.group(1))
    m = re.search(r"(?:PROPERTY )?OWNER:?\s+(.+?)\n", flat)
    if m:
        out["owner"] = clean(m.group(1))
    m = re.search(r"LOCATIONS?:?\s+(.+?)\n", flat)
    if m:
        out["location"] = clean(m.group(1))
    one = re.sub(r"\s+", " ", flat)
    m = re.search(r"(?:approximately|totaling|±|\+/-)\s*([\d,]*\.?\d+)\s*(?:-|\s)?acres?", one, re.I) or \
        re.search(r"([\d,]*\.?\d+)\s*-?\s*acre\b", one, re.I)
    if m:
        try:
            v = float(m.group(1).replace(",", ""))
            if 0 < v < 50000:
                out["acres"] = v
        except ValueError:
            pass
    pids = re.findall(r"\b(\d{4}-\d{2}-\d{2}-\d{3}[0-9A-Z])\b", one)
    for m in re.finditer(r"Parcel(?:\s+(?:ID|Identification|Number)s?)?(?:\s+No\.?)?[:#]?\s*((?:\d{12}[,;\s and]*)+)", one, re.I):
        pids += re.findall(r"\d{12}", m.group(1))
    keys = []
    for m in re.finditer(r"Alternate\s+Keys?\s*(?:No\.?|#|:)?\s*((?:\d{7}[,;\s and]*)+)", one, re.I):
        keys += re.findall(r"\d{7}", m.group(1))
    out["parcels"] = list(dict.fromkeys([fmt_parcel(p) for p in pids] + keys))
    return out


def fmt_parcel(p):
    p = re.sub(r"[\s\-]", "", p or "")
    if re.fullmatch(r"[0-9A-Z]{12}", p):
        return f"{p[0:4]}-{p[4:6]}-{p[6:8]}-{p[8:12]}"
    return p


def from_to(text):
    k = re.search(r"rezon\w*|zoning|Future Land Use|land use designation", text or "", re.I)
    if not k:
        return None, None
    text = text[k.start():k.start() + 600]
    m = re.search(r"\bfrom\s+(?:their\s+existing\s+zoning\s+classifications|(?:Volusia County\s+|City of Port Orange\s+)?"
                  r"(.{2,120}?))\s+to\s+(?:the\s+)?(?:City of Port Orange\s+)?(.{2,90}?)(?:\s+zoning|\s+district|\s+for\b|"
                  r"\s+on\b|\s+to\s+(?:allow|permit)|[.;]|$)", text or "", re.I)
    if not m:
        return None, None
    return clean(m.group(1)), clean(m.group(2))


PC_RES = re.compile(r"PLANNING COMMISSION (?:RECOMMENDATION|ACTION)\s*\(([^)]+)\)\s*:?\s*"
                    r"((?:Recommended\s+)?(?:Approval|Denial|Approved|Denied|Continu\w*|Tabled|Postponed)[^0-9]{0,40}?"
                    r"(?:\d-\d)?)", re.I)


def pc_result_from(desc):
    m = PC_RES.search(desc or "")
    if not m:
        return None, None
    word = m.group(2).strip()
    w = word.lower()
    if "denial" in w or "denied" in w:
        res = "Recommended Denial"
    elif "contin" in w or "tabled" in w or "postpon" in w:
        res = "Continued"
    else:
        res = "Recommended Approval"
    vote = re.search(r"\d-\d", word)
    return res + (f" ({vote.group(0)})" if vote else ""), m.group(1)


def mdy_any(s):
    s = (s or "").strip()
    for fmt in ("%m/%d/%y", "%m/%d/%Y", "%m-%d-%y", "%m-%d-%Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def file_stream(file_id):
    return f"{API}/Meetings/GetMeetingFileStream(fileId={file_id},plainText=false)"


def docs_url(apps):
    """Stable link to the agenda packet that holds the staff report (attachment links expire)."""
    for body in ("Planning Commission", "City Council"):
        for a in reversed(apps):
            if a["body"] == body and a.get("packet"):
                return file_stream(a["packet"])
    return None


def build(case, apps, today):
    apps = sorted(apps, key=lambda a: (a["date"], {"DRC": 0, "Planning Commission": 1, "City Council": 2}[a["body"]]))
    texts = [a["name"] + " " + a["desc"] for a in apps]
    cat = category_of(case, texts)
    if cat is None:
        return None
    staff_url = next((a["staff"] for a in reversed(apps) if a["staff"] and a["body"] == "Planning Commission"), None) \
        or next((a["staff"] for a in reversed(apps) if a["staff"]), None)
    st = parse_staff(pdf_text(staff_url)) if staff_url else {"parcels": []}
    pc = [a for a in apps if a["body"] == "Planning Commission"]
    cc = [a for a in apps if a["body"] == "City Council"]

    # project name / description from the Planning Commission item ("APPLICATION: X CASE NO.: ...")
    pname, desc, applicant = None, None, st.get("applicant")
    if pc:
        nm = pc[-1]["name"]
        m = re.search(r"APPLICATION:\s*(.+?)\s+CASE NO", nm, re.I)
        if m:
            pname = clean(m.group(1))
        m = re.search(r"APPLICANTS?:\s*(.+?)\s+STAFF CONTACT", nm, re.I)
        if m and not applicant:
            applicant = clean(m.group(1))
        m = re.search(r"STAFF CONTACT:.+?(?:\.org|\d{4})\s+(.+)$", nm, re.I)
        desc = clean(m.group(1)) if m else None
    if cc and not pname:
        m = re.search(r"Ordinance No\.?\s*\d{4}-\d+\s*[-:]\s*(.+?)(?:\(Case No|$)", cc[-1]["name"], re.I)
        pname = clean(m.group(1)) if m else None
    if not desc and cc:
        desc = clean(re.sub(r"^.*?(?:Recommended\s+\w+\s+\d-\d|ACTION[^:]*:\s*\S+\s+\S+\s+\d-\d)\s*", "", cc[-1]["desc"]))
    if desc and len(desc) > 400:
        desc = desc[:397].rstrip() + "..."

    f_, t_ = None, None
    for a in pc + cc:
        f_, t_ = from_to(a["name"] + " " + a["desc"])
        if f_ or t_:
            break
    if not (f_ or t_):
        f_, t_ = from_to(pdf_text(staff_url)[:3000] if staff_url else "")

    # hearings
    hearings = []
    pc_res = None
    for a in cc:
        r_, d_ = pc_result_from(a["desc"])
        if r_:
            pc_res = (r_, mdy_any(d_))
    status_raw = None
    for a in apps:
        res = None
        word = a["name"] + " " + (a["desc"][:300] if a["body"] != "City Council" else "")
        if re.search(r"\bwithdr", a["name"], re.I):
            res = "Withdrawn"
        elif re.search(r"\b(continu|postpon|tabled|deferr)", a["name"], re.I):
            res = "Continued"
        if a["body"] == "Planning Commission" and pc_res and not res:
            if pc_res[1] is None or abs((dt.date.fromisoformat(a["date"]) - dt.date.fromisoformat(pc_res[1])).days) <= 10 \
                    or a is pc[-1]:
                res = pc_res[0]
        body = a["body"]
        if body == "City Council" and a["reading"]:
            body = f"City Council ({a['reading']} reading)"
        if a["body"] == "City Council" and a["date"] <= today and not res:
            later = [b for b in cc if b["date"] > a["date"]]
            if a["reading"] == "first" and any(b["reading"] == "second" for b in later):
                res = "Approved (first reading)"
            elif a["reading"] == "second" and not later and a["minutes"]:
                res = "Approved"
                status_raw = (f"Second reading {a['date']} held, minutes published, no later agenda appearance: "
                              f"adoption inferred (minutes are scanned images)")
        del word
        h = {"body": body, "date": a["date"], "result": res}
        if not any(x["body"] == h["body"] and x["date"] == h["date"] for x in hearings):
            hearings.append(h)

    past = [h for h in hearings if h["date"] <= today]
    future = [h for h in hearings if h["date"] > today]
    decision = None
    if any(h["result"] == "Withdrawn" for h in hearings):
        status, status_raw = "Withdrawn", "Withdrawn (agenda item)"
    elif any(h["result"] == "Denied" for h in hearings if h["body"].startswith("City Council")):
        status = "Denied"
        decision = [h for h in hearings if h["result"] == "Denied"][-1]["date"]
        status_raw = f"City Council denied {decision}"
    elif any(h["result"] == "Approved" for h in hearings if h["body"].startswith("City Council")):
        status = "Approved"
        decision = [h for h in hearings if h["result"] == "Approved"][-1]["date"]
    elif future:
        status, status_raw = "Hearing Scheduled", f"{future[0]['body']} {future[0]['date']}"
    elif past and past[-1]["result"] == "Continued":
        status, status_raw = "Continued", f"{past[-1]['body']} {past[-1]['date']}: continued"
    elif past and past[-1]["body"].startswith("City Council") and past[-1]["result"] is None:
        status = "Hearing Scheduled"
        status_raw = f"{past[-1]['body']} {past[-1]['date']} held; outcome not yet published"
    elif past:
        lp = past[-1]
        status = "Filed"
        status_raw = f"Last on agenda: {lp['body']} {lp['date']}" + (f" ({lp['result']})" if lp["result"] else "")
    else:
        status, status_raw = "Filed", "On agenda"

    ords = list(dict.fromkeys(a["ordinance"] for a in apps if a["ordinance"]))
    pre = case.split("-")[0]
    app_type = {"REZONING": "Rezoning", "PRZA": "PUD Rezoning / Master Development Agreement Amendment",
                "PRZ": "PUD Rezoning", "PUD": "PUD", "CPAM": "Comprehensive Plan Amendment"}.get(pre, pre)
    if cat == "Comp Plan Amendment":
        t = " ".join(texts)
        if re.search(r"Large[- ]Scale", t, re.I):
            app_type = "Large-Scale Comprehensive Plan Amendment"
        elif re.search(r"Small[- ]Scale", t, re.I):
            app_type = "Small-Scale Comprehensive Plan Amendment"
    if ords:
        app_type += f" (Ordinance No. {ords[0]})"
    latest = apps[-1]
    address = st.get("location")
    if address and re.search(r"^Various", address, re.I):
        address = None
    return {
        "jurisdiction": JUR, "county": "Volusia", "app_num": case, "category": cat, "app_type": app_type,
        "project_name": pname, "description": desc, "applicant": applicant or st.get("owner"),
        "parcel_ids": st.get("parcels") or [], "address": address, "acres": st.get("acres"),
        "from_zoning": f_ if cat != "Comp Plan Amendment" else None,
        "to_zoning": t_ if cat != "Comp Plan Amendment" else None,
        "from_flu": f_ if cat == "Comp Plan Amendment" else None,
        "to_flu": t_ if cat == "Comp Plan Amendment" else None,
        "filed_date": apps[0]["date"], "status": status, "status_raw": status_raw, "hearings": hearings,
        "decision_date": decision, "url": f"{PORTAL}/event/{latest['event_id']}/files",
        "docs_url": docs_url(apps),
        "lat": None, "lon": None, "source": SOURCE,
    }



# ------------------------------------------------------------------ parcel fallback by street address
VC_PARCELS = "https://maps5.vcgov.org/arcgis/rest/services/Basemap/MapServer/6/query"
_DIRS = {"N", "S", "E", "W", "NORTH", "SOUTH", "EAST", "WEST", "NE", "NW", "SE", "SW"}


def parcels_by_address(addr, city):
    """Volusia parcel numbers (dashed) for one street address via the county parcel layer
    (ADRNO + street name + city). Used only when the source posts no parcel number.
    Returns [] when the address is not specific or matches more than 5 parcels."""
    t = (addr or "").upper().replace(".", " ")
    m = re.match(r"\s*(\d{1,6})\s+(.+)$", t)
    if not m:
        return []
    words = [w for w in re.split(r"[\s,]+", m.group(2)) if w]
    direction = words[0][0] if words and words[0] in _DIRS else None
    while words and words[0] in _DIRS:
        words = words[1:]
    if not words or not re.match(r"^[A-Z0-9]+$", words[0]):
        return []
    street = words[0]
    if street in ("US", "U", "STATE", "SR"):
        street = "US" if street in ("US", "U") else "STATE"
    where = f"ADRNO={int(m.group(1))} AND ADRSTR LIKE '{street}%' AND CITYNAME='{city}'"
    try:
        r = _s.get(VC_PARCELS, params={"where": where, "outFields": "PID,ALTKEY,ADRDIR", "returnGeometry": "false",
                                       "f": "json"}, timeout=60)
        time.sleep(0.3)
        feats = r.json().get("features") or []
    except Exception as e:  # noqa: BLE001
        log(f"parcel lookup {addr}: {e}")
        return []
    pids = []
    for f in feats:
        at = f.get("attributes") or {}
        if direction and (at.get("ADRDIR") or "").strip() and not (at.get("ADRDIR") or "").strip().startswith(direction):
            continue
        p = re.sub(r"\D", "", str(at.get("PID") or ""))
        if len(p) == 12:
            pids.append(f"{p[0:4]}-{p[4:6]}-{p[6:8]}-{p[8:12]}")
    pids = list(dict.fromkeys(pids))
    return pids if 0 < len(pids) <= 5 else []


def fetch(since, refresh=None):
    refresh = {norm_case(r.strip()) for r in (refresh or []) if r and r.strip()}
    today = dt.date.today()
    start = dt.date.fromisoformat(since) - dt.timedelta(days=LOOKBACK_DAYS)
    if refresh:
        yrs = [int(m.group(1)) for r in refresh for m in [re.search(r"-(\d{2})-\d{4}$", r)] if m]
        if yrs:
            start = min(start, max(dt.date(2000 + min(yrs) - 1, 7, 1), today - dt.timedelta(days=730)))
    end = today + dt.timedelta(days=LOOKAHEAD_DAYS)
    cases = scan(start.isoformat(), end.isoformat())
    out = []
    for case, apps in sorted(cases.items()):
        first = min(a["date"] for a in apps)
        if first < since and case not in refresh:
            continue
        try:
            rec = build(case, apps, today.isoformat())
        except Exception as e:  # noqa: BLE001
            log(f"{case}: skipped ({e})")
            continue
        if rec:
            out.append(rec)
    for r in refresh - set(cases):
        log(f"refresh id {r} not found on any agenda since {start}")
    for rec in out:
        if not rec["parcel_ids"] and rec.get("address"):
            rec["parcel_ids"] = parcels_by_address(rec["address"], "PORT ORANGE")
    return out


if __name__ == "__main__":
    since = sys.argv[1] if len(sys.argv) > 1 else (dt.date.today() - dt.timedelta(days=14)).isoformat()
    refresh = sys.argv[2].split(",") if len(sys.argv) > 2 else []
    t0 = time.time()
    recs = fetch(since, refresh)
    print(json.dumps(recs, indent=2, ensure_ascii=False))
    from collections import Counter
    log(f"{len(recs)} records in {time.time() - t0:.0f}s; status={dict(Counter(r['status'] for r in recs))}; "
        f"category={dict(Counter(r['category'] for r in recs))}")
