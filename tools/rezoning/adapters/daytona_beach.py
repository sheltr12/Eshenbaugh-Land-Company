#!/usr/bin/env python3
"""City of Daytona Beach (Volusia County) rezoning / PD / future land use map amendment adapter.

Source: iCompass / Diligent CivicWeb portal (codb.civicweb.net), the city's agenda system for the
Planning Board and City Commission (the old CivicPlus Archive only holds other boards).
 * Meeting lists: Portal/MeetingInformation.aspx?type=61 (Planning Board) and ?type=10 (City
   Commission) list every meeting id with its date.
 * Services/MeetingsService.svc/meetings/{id}/meetingDocuments returns the agenda as HTML. Each
   agenda item is a table row "4.B." + bold title "Cedar Street Local Industry (M1) Rezoning
   DEV2026-136 (Quasi-Judicial Hearing)" + description + attachment link (staff report PDF on the
   Planning Board agenda, "Agenda Summary" PDF on the Commission agenda).
 * Every application has its own DEV number (the SSCPA and the companion rezoning are separate
   numbers); app_num = "DEV2026-136". In scope: rezonings, Planned Development (PD-G / PD-RD / PUD)
   rezonings and PD amendments, small / large scale comprehensive plan (future land use map)
   amendments. Site plans, semi-public use permits, LDC text amendments, plats, annexation-only
   items, temporary parking etc. are skipped.
 * Staff report page 1-3: applicant, owner, "Parcel No(s): 5305-01-01-0050 & 5306-01-01-0040",
   property size, existing zoning / future land use.
 * Results: City Commission minutes PDFs ("DEV2025-656 Adopted/Ordinance No. 2026-264"),
   Planning Board minutes PDFs ("A motion was made ... to approve ... DEV2024-319 ... The motion
   carried (7-0)").
 * Hearing chain: Planning Board (recommendation) -> City Commission first reading (introduction,
   or public hearing for LSCPA transmittal) -> City Commission second reading (final action).
Parcel IDs: Volusia parcel numbers as the staff report prints them (5305-01-01-0050).
"""
import datetime as dt
import io
import json
import re
import sys
import time
import zipfile

import requests
from bs4 import BeautifulSoup

try:
    import pypdf
except ImportError:  # pragma: no cover
    pypdf = None

JURISDICTIONS = ["City of Daytona Beach"]
JUR = "City of Daytona Beach"
HOST = "https://codb.civicweb.net"
SOURCE = "Daytona Beach CivicWeb (Planning Board / City Commission agendas, staff reports, minutes)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
TYPES = {61: "Planning Board", 10: "City Commission"}
LOOKBACK_DAYS = 150     # scan this far before `since` to know when a case first appeared
LOOKAHEAD_DAYS = 60
MAX_PDF = 90_000_000
DEV_RX = re.compile(r"\bDEV\s?(\d{4})-(\d{2,4})\b")
IN_SCOPE = re.compile(r"Rezon|Planned Development|Planned District|\bPD-?[A-Z]{1,2}\b|\bPUD\b|\bPMD\b|"
                      r"Comprehensive Plan (?:Map )?Amendment|\bSSCPA\b|\bLSCPA\b|Future Land Use Map", re.I)
OUT_SCOPE = re.compile(r"Site Plan|Semi-Public|Special Use|Variance|\bPlat\b|Text Amendment|Temporary|"
                       r"Abandon|Vacat|Development Agreement Extension|Sign Plan|Waiver Request", re.I)

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


def log(*a):
    print("[daytona_beach]", *a, file=sys.stderr, flush=True)


def get(url, params=None, tries=3, sleep=0.4, **kw):
    if url.startswith("/"):
        url = HOST + url
    last = None
    for i in range(tries):
        try:
            r = _s.get(url, params=params, timeout=120, **kw)
            r.raise_for_status()
            time.sleep(sleep)
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1) ** 2)
    raise RuntimeError(f"GET failed {url}: {last}")


def clean(v):
    if v is None:
        return None
    v = re.sub(r"\s+", " ", str(v).replace("\xa0", " ")).strip(" .,;:")
    return v or None


def dev_key(m):
    return f"DEV{m.group(1)}-{int(m.group(2)):03d}"


_pdf_cache = {}


def pdf_text(url, max_pages=None):
    if not url or pypdf is None:
        return ""
    key = (url, max_pages)
    if key in _pdf_cache:
        return _pdf_cache[key]
    txt = ""
    try:
        r = get(url, stream=True)
        size = int(r.headers.get("content-length") or 0)
        if size and size > MAX_PDF:
            log(f"pdf too large ({size} bytes), skipped: {url[:80]}")
            r.close()
        else:
            rd = pypdf.PdfReader(io.BytesIO(r.content))
            pages = rd.pages if max_pages is None else rd.pages[:max_pages]
            out = []
            for p in pages:
                try:
                    out.append(p.extract_text() or "")
                except Exception:  # noqa: BLE001
                    out.append("")
            txt = "\n".join(out)
    except Exception as e:  # noqa: BLE001
        log(f"pdf unreadable {url[:80]}: {e}")
    _pdf_cache[key] = txt
    return txt


# ------------------------------------------------------------------ meetings
def list_meetings(type_id):
    h = get(f"{HOST}/Portal/MeetingInformation.aspx", params={"type": type_id}).text
    out = []
    for mid, d in re.findall(r"meetingClick\(this,\s*(\d+),\d+,\d+\);return false;\"[^>]*>"
                             r"<div class=\"meeting-list-item-button-date\">([^<]+)</div>", h):
        try:
            date = dt.datetime.strptime(d.strip(), "%b %d %Y").date()
        except ValueError:
            continue
        out.append((int(mid), date))
    return out


def meeting_docs(mid):
    return get(f"{HOST}/Services/MeetingsService.svc/meetings/{mid}/meetingDocuments").json() or []


def parse_agenda(html):
    """[{num, title, desc, links:[(text, href)]}] from a CivicWeb HTML agenda."""
    b = BeautifulSoup(html, "lxml")
    items = []
    for tr in b.find_all("tr"):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 3:
            continue
        num = None
        idx = None
        for i, td in enumerate(tds[:2]):
            t = td.get_text(" ", strip=True)
            if re.fullmatch(r"\d+\.[A-Z]\.?", t):
                num, idx = t.rstrip("."), i
                break
        if num is None or idx + 1 >= len(tds):
            continue
        body = tds[idx + 1]
        ps = body.find_all("p")
        title = clean(ps[0].get_text(" ", strip=True)) if ps else None
        desc = clean(" ".join(p.get_text(" ", strip=True) for p in ps[1:]))
        links = [(clean(a.get_text(" ", strip=True)), a["href"]) for a in body.find_all("a", href=True)
                 if a["href"].startswith("/document/")]
        items.append({"num": num, "title": title or "", "desc": desc or "", "links": links})
    return items


def scan(start, end):
    """{dev: [appearance]} plus {meeting_id: minutes_url}."""
    cases = {}
    minutes = {}
    n = 0
    for type_id, body in TYPES.items():
        try:
            meets = list_meetings(type_id)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"Daytona Beach CivicWeb meeting list unreachable: {e}") from e
        for mid, date in meets:
            if not (start <= date <= end):
                continue
            try:
                docs = meeting_docs(mid)
            except Exception as e:  # noqa: BLE001
                log(f"meeting {mid}: {e}")
                continue
            n += 1
            agenda = next((d for d in docs if d.get("Html") and d.get("Type") == 1), None) or \
                next((d for d in docs if d.get("Html")), None)
            mins = [d for d in docs if d.get("Type") == 2]
            if mins:
                minutes[mid] = f"{HOST}/document/{mins[0]['Id']}/"
            if not agenda:
                continue
            for it in parse_agenda(agenda["Html"]):
                txt = it["title"] + " " + it["desc"]
                # minutes of earlier Planning Board meetings are linked from later PB agendas
                for t, href in it["links"]:
                    m = re.match(r"Planning Board - (\w{3} \d{1,2} \d{4}) - Minutes", t or "")
                    if m:
                        minutes.setdefault(("PB", m.group(1)), HOST + href)
                m = DEV_RX.search(it["title"]) or DEV_RX.search(it["desc"][:200])
                if not m:
                    continue
                key = dev_key(m)
                if not IN_SCOPE.search(it["title"]) or (OUT_SCOPE.search(it["title"]) and not
                                                        re.search(r"Rezon|SSCPA|LSCPA", it["title"], re.I)):
                    continue
                reading = None
                if body == "City Commission":
                    if re.search(r"first reading|Introduction of Ordinance", it["desc"], re.I):
                        reading = "first"
                    elif re.search(r"second reading|final action", it["desc"], re.I):
                        reading = "second"
                    elif re.search(r"Transmittal", txt, re.I):
                        reading = "transmittal"
                staff = [h for t, h in it["links"] if not re.search(r"Minutes", t or "", re.I)]
                cases.setdefault(key, []).append({
                    "date": date.isoformat(), "body": body, "mid": mid, "num": it["num"],
                    "title": it["title"], "desc": it["desc"], "reading": reading,
                    "staff": HOST + staff[0] if staff else None,
                    "companions": [dev_key(x) for x in DEV_RX.finditer(txt) if dev_key(x) != key],
                })
    log(f"{n} Planning Board / City Commission meetings read {start}..{end}; {len(cases)} in-scope DEV numbers")
    return cases, minutes


# ------------------------------------------------------------------ minutes results
_minutes_cache = {}


def docx_text(url):
    try:
        r = get(url)
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
        xml = re.sub(r"</w:p>", "\n", xml)
        return re.sub(r"<[^>]+>", "", xml).replace("&amp;", "&")
    except Exception as e:  # noqa: BLE001
        log(f"docx unreadable {url[:80]}: {e}")
        return ""


def minutes_text(url):
    if url not in _minutes_cache:
        raw = docx_text(url) if re.search(r"\.docx(?:\?|$)", url, re.I) else pdf_text(url)
        _minutes_cache[url] = re.sub(r"\s+", " ", raw)
    return _minutes_cache[url]


def result_from_minutes(text, dev, body, reading):
    """Outcome for one DEV number in a minutes text."""
    if not text:
        return None
    rx = re.compile(r"DEV\s?" + dev[3:7] + r"-0*" + str(int(dev[8:])) + r"\b")
    hits = list(rx.finditer(text))
    if not hits:
        return None
    seg = text[hits[0].start(): hits[0].start() + 6000]
    nxt = re.search(r"\s\d{1,2}\.[A-Z]\.\s", seg[60:])
    if nxt:
        seg = seg[: 60 + nxt.start()]
    head = seg[:220]
    if body == "City Commission":
        if re.search(r"Adopted/(?:Ordinance|Resolution)", head, re.I):
            return "Approved"
        if re.search(r"\bDenied\b|Failed/", head, re.I):
            return "Denied"
        if re.search(r"Withdrawn", head, re.I):
            return "Withdrawn"
        if re.search(r"Continued|Tabled|Postponed|Deferred", head, re.I):
            return "Continued"
        if re.search(r"motion (?:failed|died)", seg, re.I):
            return "Denied" if reading != "first" else "Failed (first reading)"
        if reading in ("first", "transmittal"):
            if re.search(r"moved|motion carried|passed", seg, re.I):
                return "Approved (first reading)" if reading == "first" else "Transmitted"
            return "Introduced (first reading)"
        return None
    # Planning Board
    mo = re.search(r"motion was made.{0,200}?to (approve|recommend approval|deny|recommend denial|continue|"
                   r"table|postpone|defer)", seg, re.I)
    carried = re.search(r"motion (carried|passed|failed|died)", seg, re.I)
    if re.search(r"withdr", seg[:400], re.I) and not mo:
        return "Withdrawn"
    if not mo:
        if re.search(r"continued to|be continued", seg, re.I):
            return "Continued"
        return None
    verb = mo.group(1).lower()
    ok = not carried or carried.group(1).lower() in ("carried", "passed")
    if verb in ("continue", "table", "postpone", "defer"):
        return "Continued" if ok else None
    if "approv" in verb:
        return "Recommended Approval" if ok else "Recommended Denial"
    return "Recommended Denial" if ok else "Recommended Approval"


# ------------------------------------------------------------------ staff report
def parse_staff(txt):
    flat = re.sub(r"[ \t]+", " ", txt or "")
    one = re.sub(r"\s+", " ", flat)
    out = {}
    for lab, key in (("APPLICANT", "applicant"), ("OWNER", "owner"), ("Location", "location"),
                     ("Existing Zoning", "zoning"), ("Existing Future Land Use", "flu"),
                     ("Proposed Zoning", "pzoning"), ("Proposed Future Land Use", "pflu")):
        m = re.search(r"(?:^|\n)\s*" + lab + r"\s*:\s*(.{2,220}?)\n(?=\s*[A-Z][A-Za-z /()&]{2,40}:|\s*\n)",
                      flat + "\n\n", re.S)
        if m:
            out[key] = clean(m.group(1))
    m = re.search(r"Property Size\s*:\s*±?\s*([\d,]*\.?\d+)", one, re.I) or \
        re.search(r"(?:approximately|totaling)\s*([\d,]*\.?\d+)\s*±?\s*acres", one, re.I) or \
        re.search(r"([\d,]*\.?\d+)\s*±\s*acres?", one, re.I)
    if m:
        try:
            v = float(m.group(1).replace(",", ""))
            if 0 < v < 50000:
                out["acres"] = v
        except ValueError:
            pass
    pids = []
    m = re.search(r"Parcel (?:No|Number|ID)\(?s?\)?\.?\s*(?:\(s\))?\s*:\s*(.{0,400}?)(?:Property Size|Existing|Location|$)", one, re.I)
    if m:
        pids = re.findall(r"\b\d{4}-\d{2}-\d{2}-\d{3}[0-9A-Z]\b|\b\d{12}\b|\b\d{7}\b", m.group(1))
    if not pids:
        pids = re.findall(r"\b\d{4}-\d{2}-\d{2}-\d{3}[0-9A-Z]\b", one[:20000])
    if not pids:
        pids = re.findall(r"PID\s*#?\s*(\d{12})", one[:20000])
    out["parcels"] = list(dict.fromkeys(fmt_parcel(p) for p in pids))
    return out


def fmt_parcel(p):
    q = re.sub(r"[\s\-]", "", p or "")
    if re.fullmatch(r"[0-9A-Z]{12}", q):
        return f"{q[0:4]}-{q[4:6]}-{q[6:8]}-{q[8:12]}"
    return p


# ------------------------------------------------------------------ build
def category_of(title):
    if re.search(r"Comprehensive Plan|SSCPA|LSCPA|Future Land Use", title, re.I):
        return "Comp Plan Amendment"
    if re.search(r"Planned Development|Planned District|\bPD-?[A-Z]{0,2}\b|\bPUD\b|\bPMD\b", title):
        return "PD"
    return "Rezoning"


def project_name(title):
    t = re.sub(r"^Growth Management\s*&\s*Planning Department\s*[-–]\s*", "", title)
    t = DEV_RX.sub("", t)
    t = re.sub(r"\((?:Quasi[- ]Judicial|Legislative|Public)[^)]*\)?", "", t, flags=re.I)
    t = re.sub(r"Continued from .*$", "", t, flags=re.I)
    t = re.sub(r"(\([A-Z\-]{2,8}\))(\s*\1)+", r"\1", t)
    return clean(t.replace(" – ", " - ").strip(" -–"))


def trim_target(v):
    if not v:
        return v
    v = re.split(r",?\s+and\s+(?:to\s+)?(?:create|a new|establish)\b|,\s*(?:to\s+)?establish\b", v)[0]
    return clean(v)


def from_to(text):
    m = re.search(r"\bfrom\s+(.{2,160}?)\s+to\s+(?:the\s+)?(.{2,120}?)(?:\s+(?:for|to allow|to permit|in order)\b|"
                  r"\s+zoning|[.;]\s|$)", text or "", re.I)
    if not m:
        return None, None
    return clean(m.group(1)), trim_target(m.group(2))


def address_of(st, desc, title):
    if st.get("location"):
        return st["location"]
    m = re.search(r"(?:property|site|properties) (?:is|are) (?:generally )?located (?:at|on)\s+(\d[^;]{3,80}?)"
                  r"(?:\.\s|;|,\s*(?:and|which)\b|$)", desc or "", re.I)
    if m:
        return clean(m.group(1))
    t = re.sub(r"^Growth Management\s*&\s*Planning Department\s*[-\u2013]\s*", "", title or "")
    m = re.match(r"(\d{2,6}\s+(?:(?:North|South|East|West|N|S|E|W)\.?\s+)?[A-Z][A-Za-z ]+?)\s+(?:Planned|Rezoning|"
                 r"Small|Large|PD\b|SSCPA|LSCPA|Comprehensive)", t)
    return clean(m.group(1)) if m else None


def build(dev, apps, minutes, today):
    apps = sorted(apps, key=lambda a: (a["date"], a["body"]))
    pb = [a for a in apps if a["body"] == "Planning Board"]
    cc = [a for a in apps if a["body"] == "City Commission"]
    cat = category_of((cc[-1] if cc else (pb[0] if pb else apps[0]))["title"])
    # the Planning Board agenda sometimes repeats one DEV number for both companion items
    pb_match = [a for a in pb if category_of(a["title"]) == cat]
    main = pb_match[0] if pb_match else (cc[-1] if cc else apps[0])
    staff_url = next((a["staff"] for a in reversed(pb) if a["staff"]), None) or \
        next((a["staff"] for a in reversed(apps) if a["staff"]), None)
    st = parse_staff(pdf_text(staff_url, max_pages=4)) if staff_url else {"parcels": []}

    hearings = []
    for a in apps:
        mt = None
        if a["body"] == "City Commission":
            mt = minutes.get(a["mid"])
        else:
            d = dt.date.fromisoformat(a["date"])
            mt = minutes.get(a["mid"]) or minutes.get(("PB", d.strftime("%b %d %Y"))) or \
                minutes.get(("PB", f"{d.strftime('%b')} {d.day} {d.year}"))
        res = None
        if mt and a["date"] <= today:
            res = result_from_minutes(minutes_text(mt), dev, a["body"], a["reading"])
        if res is None and re.search(r"\bwithdrawn\b", a["title"] + " " + a["desc"][:200], re.I):
            res = "Withdrawn"
        body = a["body"]
        if body == "City Commission" and a["reading"]:
            body = f"City Commission ({ {'first': 'first reading', 'second': 'second reading', 'transmittal': 'transmittal'}[a['reading']]})"
        h = {"body": body, "date": a["date"], "result": res}
        if not any(x["body"] == h["body"] and x["date"] == h["date"] for x in hearings):
            hearings.append(h)
        elif res:
            for x in hearings:
                if x["body"] == h["body"] and x["date"] == h["date"] and not x["result"]:
                    x["result"] = res

    past = [h for h in hearings if h["date"] <= today]
    future = [h for h in hearings if h["date"] > today]
    decision, status_raw = None, None
    cc_final = [h for h in past if h["body"].startswith("City Commission") and h["result"] in ("Approved", "Denied")]
    if any(h["result"] == "Withdrawn" for h in hearings):
        status, status_raw = "Withdrawn", "Withdrawn (agenda / minutes)"
    elif cc_final:
        status, decision = cc_final[-1]["result"], cc_final[-1]["date"]
        status_raw = f"{cc_final[-1]['body']} {decision}: {status} (minutes)"
    elif future:
        status, status_raw = "Hearing Scheduled", f"{future[0]['body']} {future[0]['date']}"
    elif past and past[-1]["result"] == "Continued":
        status, status_raw = "Continued", f"{past[-1]['body']} {past[-1]['date']}: continued"
    elif past and past[-1]["body"].startswith("City Commission") and past[-1]["result"] is None:
        status = "Hearing Scheduled"
        status_raw = f"{past[-1]['body']} {past[-1]['date']} held; minutes not yet published"
    elif past:
        lp = past[-1]
        status = "Filed"
        status_raw = f"Last on agenda: {lp['body']} {lp['date']}" + (f" ({lp['result']})" if lp["result"] else "") \
            + "; next hearing not yet on an agenda"
    else:
        status, status_raw = "Filed", "On agenda"

    desc = main["desc"]
    desc = re.sub(r"^(?:Introduction of )?Ordinance on (?:first|second) reading\s*-?\s*(?:PUBLIC HEARING|QUASI[- ]JUDICIAL HEARING)?\s*-?\s*",
                  "", desc, flags=re.I)
    desc = re.split(r"\s+(?:Action:|Note:|Agenda Summary)", desc)[0]
    desc_full = desc
    if len(desc) > 400:
        desc = desc[:397].rstrip() + "..."
    applicant = st.get("applicant")
    if not applicant:
        m = re.search(r"A request by (.+?)(?:,\s*on behalf of|\s+for approval|\s+to\s)", main["desc"])
        applicant = clean(m.group(1)) if m else None
    f_, t_ = from_to(desc_full)
    if cat == "Comp Plan Amendment":
        from_flu, to_flu, from_z, to_z = f_ or st.get("flu"), t_ or st.get("pflu"), None, None
    else:
        from_z, to_z, from_flu, to_flu = f_ or st.get("zoning"), t_ or st.get("pzoning"), None, None
        if cat == "PD" and not to_z:
            m = re.search(r"Planned Development\s*[-–]\s*(General|Redevelopment)\s*\((PD-[A-Z]+)\)", main["title"])
            to_z = m.group(2) if m else None
    acres = st.get("acres")
    if acres is None:
        m = re.search(r"([\d,]*\.?\d+)\s*±?\s*acres", desc_full, re.I)
        if m:
            try:
                acres = float(m.group(1).replace(",", ""))
            except ValueError:
                pass
    parcels = st.get("parcels") or []
    if not parcels:
        parcels = [fmt_parcel(p) for p in re.findall(r"PID#?\s*(\d{12})", " ".join(a["desc"] for a in apps))]
    app_type = re.search(r"(Small[- ]Scale Comprehensive Plan (?:Map )?Amendment|Large[- ]Scale Comprehensive Plan "
                         r"(?:Map )?Amendment|Planned Development\s*[-–]\s*\w+(?:\s*\(PD-[A-Z]+\))?|"
                         r"PD-[A-Z]+ Rezoning|Rezoning|SSCPA|LSCPA|Amendment)", main["title"], re.I)
    app_type = clean(app_type.group(1)) if app_type else cat
    if re.search(r"\b(?:\d+(?:st|nd|rd|th)) Amendment\b", main["title"]):
        app_type = "PD Amendment"
    latest = apps[-1]
    return {
        "jurisdiction": JUR, "county": "Volusia", "app_num": dev, "category": cat, "app_type": app_type,
        "project_name": project_name(main["title"]), "description": clean(desc),
        "applicant": applicant or st.get("owner"), "parcel_ids": [p for p in parcels if p],
        "address": address_of(st, desc_full, main["title"]), "acres": acres,
        "from_zoning": from_z, "to_zoning": to_z, "from_flu": from_flu, "to_flu": to_flu,
        "filed_date": apps[0]["date"], "status": status, "status_raw": status_raw, "hearings": hearings,
        "decision_date": decision,
        "url": f"{HOST}/Portal/MeetingInformation.aspx?Id={latest['mid']}",
        "docs_url": staff_url, "lat": None, "lon": None, "source": SOURCE,
    }


def norm_ref(r):
    m = DEV_RX.search((r or "").upper().replace(" ", ""))
    return dev_key(m) if m else (r or "").strip().upper()



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
    refresh = {norm_ref(r): r for r in (refresh or []) if r and r.strip()}
    today = dt.date.today()
    start = dt.date.fromisoformat(since) - dt.timedelta(days=LOOKBACK_DAYS)
    if refresh:
        start = min(start, today - dt.timedelta(days=540))
    end = today + dt.timedelta(days=LOOKAHEAD_DAYS)
    cases, minutes = scan(start, end)
    out = []
    for dev, apps in sorted(cases.items()):
        first = min(a["date"] for a in apps)
        if first < since and dev not in refresh:
            continue
        try:
            rec = build(dev, apps, minutes, today.isoformat())
        except Exception as e:  # noqa: BLE001
            log(f"{dev}: skipped ({type(e).__name__}: {e})")
            continue
        if dev in refresh:
            rec["app_num"] = dev
        out.append(rec)
    for r in set(refresh) - set(cases):
        log(f"refresh id {refresh[r]} not found on any agenda since {start}")
    for rec in out:
        if not rec["parcel_ids"] and rec.get("address"):
            rec["parcel_ids"] = parcels_by_address(rec["address"], "DAYTONA BEACH")
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
