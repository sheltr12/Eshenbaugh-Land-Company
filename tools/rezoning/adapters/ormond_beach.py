#!/usr/bin/env python3
"""City of Ormond Beach (Volusia County) rezoning / PD / future land use map amendment adapter.

Ormond Beach runs planning cases in Tyler EnerGov, but its public self-service portal only exposes
site plans and CDDs (no PZ cases), so the case data comes from published agendas:
 1. Planning Board agendas (CivicPlus Archive, www.ormondbeach.org/Archive.aspx?AMID=50, one text
    PDF per meeting). Public hearing items read "A. PZ-2026-309: Comprehensive Plan Amendment, Small
    Scale Future Land Use Map Amendment, 231 Hernandez Avenue" followed by a request paragraph.
    In scope: Official Zoning Map Amendment / rezoning (Rezoning), Planned Business / Residential /
    Mixed-Use / Industrial Development and their amendments (PD), small / large scale future land
    use map amendments (Comp Plan Amendment). Special exceptions, LDC text amendments, CIE updates
    and other text-only comprehensive plan amendments are skipped.
 2. Planning Board agenda packets (Archive AMID=75): the EnerGov "PLAN CORRECTIONS REPORT
    PZ-2026-000259" page in each packet gives the parcel number (324200000280 -> 3242-00-00-0280),
    application date and applicant. Only light text pages are read (plan sheets are skipped).
 3. City Commission agendas on CivicClerk (ormondbeachfl.api.civicclerk.com): ordinance items
    ("AN ORDINANCE AMENDING THE FUTURE LAND USE ELEMENT ... LOCATED AT 651 SOUTH RIDGEWOOD ...")
    are matched to the Planning Board case by kind (land use / zoning map / planned development) and
    street address. Each meeting's "Action Report" PDF gives the outcome ("APPROVED ON FIRST
    READING", "APPROVED", "DENIED", "CONTINUED").
Planning Board minutes are scanned images, so Planning Board results are not available.
app_num is the EnerGov case number in its full form (PZ-2026-000309; agendas sometimes shorten it
to PZ-2026-309).
"""
import datetime as dt
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

JURISDICTIONS = ["City of Ormond Beach"]
JUR = "City of Ormond Beach"
SITE = "https://www.ormondbeach.org"
AGENDA_AMID, PACKET_AMID = 50, 75
CC_API = "https://ormondbeachfl.api.civicclerk.com/v1"
CC_PORTAL = "https://ormondbeachfl.portal.civicclerk.com"
SOURCE = "Ormond Beach Planning Board agendas + packets (CivicPlus Archive) + City Commission CivicClerk"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
LOOKBACK_DAYS = 150
LOOKAHEAD_DAYS = 60
PACKET_PAGE_LIMIT = 700
PACKET_TIME_LIMIT = 90     # seconds of text extraction per packet
HEAVY_PAGE = 150_000       # content-stream bytes; bigger pages are plan sheets / scans

CASE_RX = re.compile(r"\bPZ[- ]?(\d{4})-(\d{1,6})\b")
ITEM_RX = re.compile(r"(?:^|\n)\s*([A-Z])\.\s+(PZ[- ]?\d{4}-\d{1,6})\s*[:,]\s*(.+?)(?=\n\s*[A-Z]\.\s+PZ[- ]?\d{4}-|"
                     r"\n\s*[IVX]+\.\s|\Z)", re.S)

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


def log(*a):
    print("[ormond_beach]", *a, file=sys.stderr, flush=True)


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
            time.sleep(2 * (i + 1) ** 2)
    raise RuntimeError(f"GET failed {url}: {last}")


def clean(v):
    if v is None:
        return None
    v = re.sub(r"\s+", " ", str(v).replace("\xa0", " ")).strip(" .,;:")
    return v or None


def norm_case(year, num):
    return f"PZ-{year}-{int(num):06d}"


def fmt_parcel(p):
    q = re.sub(r"[\s\-]", "", p or "")
    if re.fullmatch(r"[0-9A-Z]{12}", q):
        return f"{q[0:4]}-{q[4:6]}-{q[6:8]}-{q[8:12]}"
    return p


# ------------------------------------------------------------------ CivicPlus archive
def archive_list(amid):
    h = get(f"{SITE}/Archive.aspx", params={"AMID": amid}).text
    s = BeautifulSoup(h, "lxml")
    out = []
    for a in s.find_all("a", href=re.compile(r"ADID=\d+")):
        t = a.get_text(" ", strip=True)
        m = re.match(r"([A-Z][a-z]+ \d{1,2}, \d{4})", t)
        if not m:
            continue
        try:
            d = dt.datetime.strptime(m.group(1), "%B %d, %Y").date()
        except ValueError:
            continue
        adid = int(re.search(r"ADID=(\d+)", a["href"]).group(1))
        out.append((d, adid, t))
    return out


def archive_url(adid):
    return f"{SITE}/Archive.aspx?ADID={adid}"


def pdf_reader(url):
    r = get(url)
    if not r.content.startswith(b"%PDF"):
        raise RuntimeError("not a PDF")
    return pypdf.PdfReader(io.BytesIO(r.content))


def agenda_items(adid):
    rd = pdf_reader(archive_url(adid))
    txt = "\n".join((p.extract_text() or "") for p in rd.pages)
    if re.search(r"has been cancel", txt, re.I):
        return []
    i = txt.find("PUBLIC HEARINGS")
    body = txt[i:] if i >= 0 else txt
    body = re.sub(r"Planning Board Agenda\s+Page \d+\s*\n\[[^\]]*\]", "\n", body)
    out = []
    for m in ITEM_RX.finditer(body):
        cm = CASE_RX.search(m.group(2))
        block = m.group(3)
        lines = block.split("\n")
        # header runs until the first line starting a sentence ("This is ...")
        head, rest = [], []
        for j, ln in enumerate(lines):
            if re.match(r"\s*(This is|The applicant|A request)", ln):
                rest = lines[j:]
                break
            head.append(ln)
        fix = lambda x: re.sub(r"(\w) \.(\w)", r"\1.\2", clean(" ".join(x)) or "")  # noqa: E731
        out.append({"case": norm_case(cm.group(1), cm.group(2)), "header": fix(head), "desc": fix(rest)})
    return out


def kind_of(header, desc):
    h = header or ""
    t = h + " " + (desc or "")
    if re.search(r"Special Exception|Variance|Land Development Code|Text Amendment|Capital Improvement|"
                 r"Annexation(?!.*(?:Zoning|Land Use))|Plat\b|Site Plan|Waiver|Moratorium|Data Center", h, re.I):
        return None
    if re.search(r"Future Land Use Map|Land Use Map Amendment|Small Scale|Large Scale", h, re.I):
        return "Comp Plan Amendment"
    if re.search(r"Comprehensive Plan Amendment", h, re.I) and re.search(r"Future Land Use|land use designation|"
                                                                          r"from Volusia County", t, re.I):
        return "Comp Plan Amendment"
    if re.search(r"Planned (?:Business|Residential|Mixed[- ]Use|Industrial|Unit|Commercial) Development|\bPBD\b|"
                 r"\bPRD\b|\bPUD\b|\bPMUD\b|\bPID\b", h):
        return "PD"
    if re.search(r"Zoning Map Amendment|Rezon", h, re.I):
        return "Rezoning"
    return None


def packet_details(adid, wanted):
    """{case: {parcels, filed, applicant, owner, address}} from a Planning Board agenda packet.
    Two page kinds are used: EnerGov "PLAN CORRECTIONS REPORT PZ-..." pages (PARCEL, APPLICATION DATE,
    PLAN ADDRESS) and staff reports ("Project Number: PZ-..." on page 1; parcel numbers within the
    next pages)."""
    out = {}
    if not wanted:
        return out
    try:
        rd = pdf_reader(archive_url(adid))
    except Exception as e:  # noqa: BLE001
        log(f"packet {adid}: {e}")
        return out
    t0 = time.time()
    cur, cur_left = None, 0
    for i, p in enumerate(rd.pages[:PACKET_PAGE_LIMIT]):
        if time.time() - t0 > PACKET_TIME_LIMIT:
            break
        try:
            c = p.get_contents()
            n = len(c.get_data()) if c is not None else 0
        except Exception:  # noqa: BLE001
            continue
        if n > HEAVY_PAGE:
            cur_left -= 1
            continue
        try:
            t = p.extract_text() or ""
        except Exception:  # noqa: BLE001
            continue
        m = re.search(r"PLAN CORRECTIONS REPORT\s*\(?\s*PZ[- ]?(\d{4})-(\d{1,6})", t)
        if m and "PARCEL" in t:
            case = norm_case(m.group(1), m.group(2))
            if case in wanted:
                d = out.setdefault(case, {"parcels": [], "filed": None, "applicant": None, "owner": None,
                                          "address": None})
                for pm in re.finditer(r"PARCEL:\s*(\d{12})", t):
                    pid = fmt_parcel(pm.group(1))
                    if pid not in d["parcels"]:
                        d["parcels"].append(pid)
                am = re.search(r"APPLICATION DATE:\s*(\d{2}/\d{2}/\d{4})", t)
                if am and not d["filed"]:
                    d["filed"] = dt.datetime.strptime(am.group(1), "%m/%d/%Y").date().isoformat()
                ad = re.search(r"PLAN ADDRESS:\s*(.+?)\n", t)
                if ad and not d["address"]:
                    d["address"] = clean(ad.group(1))
                ow = re.search(r"\nProperty Owner\s+\S+\s+\S+\s+(.+?)\s*\n", t)
                if ow and not d["owner"]:
                    d["owner"] = clean(ow.group(1))
            continue
        m = re.search(r"Project Numbers?:\s*PZ[- ]?(\d{4})-(\d{1,6})", t)
        if m:
            cand = norm_case(m.group(1), m.group(2))
            cur, cur_left = (cand, 18) if cand in wanted else (None, 0)
            if cur:
                d = out.setdefault(cur, {"parcels": [], "filed": None, "applicant": None, "owner": None,
                                         "address": None})
                ap = re.search(r"APPLICANT(?:/PROPERTY\s*OWNER)?:\s*(.+?)(?:,\s*representing|\n)", t)
                if ap and not d["applicant"]:
                    d["applicant"] = clean(ap.group(1))
        if cur and cur_left > 0:
            flat = re.sub(r"\s+", " ", t)
            flat = re.sub(r"(\d)-\s+(\d)", r"\1-\2", flat)
            for pid in re.findall(r"\b\d{4}-\d{2}-\d{2}-\d{4}\b", flat):
                if pid not in out[cur]["parcels"]:
                    out[cur]["parcels"].append(pid)
            cur_left -= 1
    log(f"packet {adid}: details for {len(out)}/{len(wanted)} cases in {time.time() - t0:.0f}s")
    return out


# ------------------------------------------------------------------ City Commission (CivicClerk)
def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = s.replace("&nbsp;", " ").replace(" ", " ").replace("&amp;", "&")
    s = re.sub(r"&#?\w+;", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def walk(items):
    for it in items or []:
        yield it
        yield from walk(it.get("childItems"))


def cc_kind(name):
    n = name.upper()
    if not n.startswith("AN ORDINANCE"):
        return None
    if "FUTURE LAND USE MAP" in n or ("FUTURE LAND USE" in n and "COMPREHENSIVE PLAN" in n and "CAPITAL" not in n):
        return "Comp Plan Amendment"
    if "OFFICIAL ZONING MAP" in n or "REZON" in n:
        return "Rezoning"
    if re.search(r"PLANNED (?:BUSINESS|RESIDENTIAL|MIXED[- ]USE|INDUSTRIAL|UNIT|COMMERCIAL) DEVELOPMENT", n):
        return "PD"
    return None


def addr_key(text):
    """('890', 'US') / ('231', 'HERNANDEZ') from an address-like text."""
    t = (text or "").upper().replace(".", "")
    m = re.search(r"\b(\d{2,6})(?:\s*(?:AND|&|,)\s*\d{2,6})*\s+((?:(?:NORTH|SOUTH|EAST|WEST|N|S|E|W)\s+)?)"
                  r"([A-Z0-9]+)", t)
    if not m:
        return None
    word = m.group(3)
    if word in ("US", "U", "STATE", "SR"):
        word = "US"
    return m.group(1), word


def cc_scan(start, end):
    url = f"{CC_API}/Events"
    params = {"$filter": f"startDateTime ge {start}T00:00:00Z and startDateTime lt {end}T00:00:00Z",
              "$orderby": "startDateTime"}
    events = []
    while url:
        d = get(url, params, sleep=0.3).json()
        events.extend(d.get("value", []))
        url, params = d.get("@odata.nextLink"), None
    apps = []
    for ev in events:
        name = ev.get("eventName") or ""
        if not ev.get("agendaId") or not re.search(r"City Commission", name, re.I) or \
                re.search(r"Workshop|cancel", name, re.I):
            continue
        try:
            mt = get(f"{CC_API}/Meetings/{ev['agendaId']}", sleep=0.3).json()
        except Exception as e:  # noqa: BLE001
            log(f"meeting {ev['agendaId']}: {e}")
            continue
        files = {f.get("type"): f.get("fileId") for f in ev.get("publishedFiles") or []}
        section = None
        for it in walk(mt.get("items")):
            nm = strip_html(it.get("agendaObjectItemName"))
            if re.fullmatch(r"(FIRST|SECOND) READING OF ORDINANCES|PUBLIC HEARINGS?|CONSENT AGENDA|[A-Z ,&]{6,60}", nm):
                section = nm
                continue
            k = cc_kind(nm)
            if not k:
                continue
            fn = " ".join(a.get("fileName") or "" for a in it.get("attachmentsList") or [])
            om = re.search(r"\b(\d{2})-(\d{2,3})\b", fn)
            reading = "first" if section and "FIRST READING" in section else \
                ("second" if section and "SECOND READING" in section else None)
            lm = re.search(r"LOCATED (?:AT|ALONG)\s+(.{3,80})", nm, re.I)
            apps.append({"date": ev["startDateTime"][:10], "event_id": ev["id"], "name": nm, "kind": k,
                         "reading": reading, "addr": addr_key(lm.group(1) if lm else nm),
                         "ordinance": f"20{om.group(1)}-{int(om.group(2))}" if om else None,
                         "action": files.get("Other"), "files": fn})
    log(f"{len(events)} CivicClerk events {start}..{end}; {len(apps)} land use ordinance items")
    return apps


_action_cache = {}


def action_text(file_id):
    if file_id not in _action_cache:
        try:
            r = get(f"{CC_API}/Meetings/GetMeetingFileStream(fileId={file_id},plainText=false)", sleep=0.3)
            rd = pypdf.PdfReader(io.BytesIO(r.content))
            _action_cache[file_id] = re.sub(r"\s+", " ", "\n".join((p.extract_text() or "") for p in rd.pages))
        except Exception as e:  # noqa: BLE001
            log(f"action report {file_id}: {e}")
            _action_cache[file_id] = ""
    return _action_cache[file_id]


def action_result(a):
    if not a.get("action"):
        return None
    t = action_text(a["action"])
    if not t:
        return None
    pos = -1
    if a["ordinance"]:
        m = re.search(r"ORDINANCE NO\.?\s*" + re.escape(a["ordinance"]) + r"\b", t, re.I)
        pos = m.start() if m else -1
    if pos < 0:
        key = re.sub(r"[^A-Z0-9]", "", a["name"].upper())[12:70]
        flat = re.sub(r"[^A-Z0-9]", "", t.upper())
        j = flat.find(key)
        if j >= 0:   # map back approximately: count alnum chars
            cnt = 0
            for i, ch in enumerate(t):
                if ch.isalnum():
                    if cnt == j:
                        pos = i
                        break
                    cnt += 1
    if pos < 0:
        return None
    seg = t[pos: pos + 2500]
    m = re.search(r"\b(APPROVED ON FIRST READING|APPROVED ON SECOND READING|APPROVED AS AMENDED|APPROVED|ADOPTED|"
                  r"DENIED|FAILED|CONTINUED[^.]{0,40}|TABLED|POSTPONED|WITHDRAWN|DEFERRED)\b", seg)
    if not m:
        return None
    w = m.group(1)
    if "FIRST READING" in w:
        return "Approved (first reading)"
    if w.startswith(("APPROVED", "ADOPTED")):
        return "Approved"
    if w.startswith(("DENIED", "FAILED")):
        return "Denied"
    if w.startswith("WITHDRAWN"):
        return "Withdrawn"
    return "Continued"


# ------------------------------------------------------------------ build
def from_to(desc, kind):
    d = re.sub(r"\b([A-Z]) -\s?(\d)", r"\1-\2", desc or "")
    d = re.sub(r"(\w) -(\w)", r"\1-\2", d)
    m = re.search(r"to change the\s+(.+?)\s+(?:zoning district\s+)?portion\s+to\s+(?:the\s+)?(.+?)"
                  r"(?:\s+zoning district|,\s*resulting|\.\s|$)", d, re.I)
    if m:
        return clean(m.group(1).strip("“”\"")), clean(m.group(2).strip("“”\""))
    m = re.search(r"to change the\s+[“\"]?(.+?)[”\"]?\s+(?:portion\s+)?to\s+[“\"]?(.+?)[”\"]?(?:,|\s+resulting|\.\s|$)", d, re.I) or \
        re.search(r"\bfrom\s+(.{2,100}?)\s+to\s+(.{2,100}?)(?:\s+following|,|\.\s|$)", d, re.I)
    if not m:
        return None, None
    f_, t_ = clean(m.group(1)), clean(m.group(2))
    f_ = re.sub(r"\s+(?:zoning district\s+)?portion$", "", f_ or "", flags=re.I) or None
    t_ = re.sub(r"\s+zoning district$", "", t_ or "", flags=re.I) or None
    return f_, t_


def acres_of(desc):
    d = desc or ""
    m = re.search(r"(?:totaling|approximately|of)\s+(?:approximately\s+)?([\d,]*\.?\d+)\s*(?:±\s*)?acres", d, re.I) or \
        re.search(r"([\d,]*\.?\d+)\s*acres", d, re.I)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            return None
    m = re.search(r"approximately\s+([\d,]+)\s*-?\s*square\s*-?\s*f(?:oo|ee)t\s+parc\s?el", d, re.I)
    if m:
        return round(float(m.group(1).replace(",", "")) / 43560.0, 3)
    return None


def build(case, apps, details, cc_apps, today):
    apps = sorted(apps, key=lambda a: a["date"])
    main = apps[-1]
    kind = main["kind"]
    det = details.get(case, {})
    loc = None
    hm = re.search(r",\s*((?:\d[\d ,&and]*\s+)?[^,]*?(?:Avenue|Road|Street|Boulevard|Drive|Highway\s*\d*|Trail|Way|"
                   r"Lane|Court|Parkway|Circle|Place)[^,]*?)(?:,|$)", main["header"])
    if hm:
        loc = clean(hm.group(1))
    if not loc:
        lm = re.search(r"located at\s+(.+?)(?:\s+and is|,|\.\s|$)", main["desc"], re.I)
        loc = clean(lm.group(1)) if lm else None
    akey = addr_key(loc or det.get("address") or main["desc"])
    hearings = [{"body": "Planning Board", "date": a["date"], "result": None} for a in apps]
    hearings = [h for i, h in enumerate(hearings) if h not in hearings[:i]]
    # City Commission items: same kind, same address, on/after the first Planning Board hearing
    first_pb = apps[0]["date"]
    for c in cc_apps:
        if c["kind"] != kind or c["date"] < first_pb or not akey or c["addr"] != akey:
            continue
        if (dt.date.fromisoformat(c["date"]) - dt.date.fromisoformat(first_pb)).days > 400:
            continue
        res = action_result(c) if c["date"] <= today else None
        body = "City Commission" + (f" ({c['reading']} reading)" if c["reading"] else "")
        if res == "Approved (first reading)" and body == "City Commission":
            body = "City Commission (first reading)"
        h = {"body": body, "date": c["date"], "result": res, "_url": f"{CC_PORTAL}/event/{c['event_id']}/files"}
        if not any(x["body"] == h["body"] and x["date"] == h["date"] for x in hearings):
            hearings.append(h)
    hearings.sort(key=lambda h: h["date"])
    seen_first = False
    for h in hearings:
        if h["body"] == "City Commission (first reading)":
            seen_first = True
        elif h["body"] == "City Commission" and seen_first:
            h["body"] = "City Commission (second reading)"
    cc_url = next((h["_url"] for h in reversed(hearings) if h.get("_url")), None)
    for h in hearings:
        h.pop("_url", None)

    past = [h for h in hearings if h["date"] <= today]
    future = [h for h in hearings if h["date"] > today]
    decision = None
    finals = [h for h in past if h["body"].startswith("City Commission") and h["result"] in ("Approved", "Denied")]
    if any(h["result"] == "Withdrawn" for h in hearings):
        status, raw = "Withdrawn", "Withdrawn (City Commission action report)"
    elif finals:
        status, decision = finals[-1]["result"], finals[-1]["date"]
        raw = f"{finals[-1]['body']} {decision}: {status} (action report)"
    elif future:
        status, raw = "Hearing Scheduled", f"{future[0]['body']} {future[0]['date']}"
    elif past and past[-1]["result"] == "Continued":
        status, raw = "Continued", f"{past[-1]['body']} {past[-1]['date']}: continued"
    elif past and past[-1]["body"].startswith("City Commission") and past[-1]["result"] is None:
        status, raw = "Hearing Scheduled", f"{past[-1]['body']} {past[-1]['date']} held; action report not yet published"
    elif past:
        status = "Filed"
        raw = f"Last on agenda: {past[-1]['body']} {past[-1]['date']}" + \
            (f" ({past[-1]['result']})" if past[-1]["result"] else "") + "; City Commission hearing not yet on an agenda"
    else:
        status, raw = "Filed", "On agenda"

    f_, t_ = from_to(main["desc"], kind)
    m = re.search(r"(?:request|application submitted) by\s+(.+?)(?:,?\s+on behalf of|,?\s+for\b|,\s*property owner)",
                  main["desc"], re.I)
    applicant = clean(m.group(1)) if m else det.get("applicant")
    if applicant and re.fullmatch(r"(?i)administrative|city of ormond beach", applicant) or \
            (not applicant and re.search(r"administrative request", main["desc"], re.I)):
        applicant = "City of Ormond Beach (administrative)"
    pname = clean(re.sub(r",\s*$", "", main["header"]))
    desc = main["desc"]
    if len(desc) > 400:
        desc = desc[:397].rstrip() + "..."
    app_type = clean(main["header"].split(",")[0]) if "," in main["header"] else kind
    if kind == "Comp Plan Amendment":
        m = re.search(r"(Small|Large) Scale", main["header"], re.I)
        app_type = f"{m.group(1).title()} Scale Future Land Use Map Amendment" if m else "Future Land Use Map Amendment"
    elif kind == "PD":
        m = re.search(r"(Planned \w+ Development(?: Amendment)?)", main["header"])
        app_type = m.group(1) if m else "Planned Development"
    else:
        app_type = "Official Zoning Map Amendment"
    return {
        "jurisdiction": JUR, "county": "Volusia", "app_num": case, "category": kind, "app_type": app_type,
        "project_name": pname, "description": desc or None, "applicant": applicant or det.get("owner"),
        "parcel_ids": det.get("parcels") or [], "address": loc or det.get("address"), "acres": acres_of(main["desc"]),
        "from_zoning": f_ if kind != "Comp Plan Amendment" else None,
        "to_zoning": t_ if kind != "Comp Plan Amendment" else None,
        "from_flu": f_ if kind == "Comp Plan Amendment" else None,
        "to_flu": t_ if kind == "Comp Plan Amendment" else None,
        "filed_date": det.get("filed") or apps[0]["date"], "status": status, "status_raw": raw,
        "hearings": hearings, "decision_date": decision,
        "url": cc_url if status in ("Approved", "Denied") and cc_url else archive_url(main["adid"]),
        "docs_url": archive_url(main["packet"]) if main.get("packet") else None,
        "lat": None, "lon": None, "source": SOURCE,
    }


def norm_ref(r):
    m = CASE_RX.search((r or "").upper())
    return norm_case(m.group(1), m.group(2)) if m else (r or "").strip().upper()



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
    if pypdf is None:
        raise RuntimeError("pypdf is required for the Ormond Beach adapter")
    refresh = {norm_ref(r) for r in (refresh or []) if r and r.strip()}
    today = dt.date.today()
    start = dt.date.fromisoformat(since) - dt.timedelta(days=LOOKBACK_DAYS)
    if refresh:
        start = min(start, today - dt.timedelta(days=540))
    end = today + dt.timedelta(days=LOOKAHEAD_DAYS)
    try:
        agendas = archive_list(AGENDA_AMID)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Ormond Beach agenda archive unreachable: {e}") from e
    try:
        packets = {d: adid for d, adid, _ in archive_list(PACKET_AMID)}
    except Exception as e:  # noqa: BLE001
        log(f"packet archive: {e}")
        packets = {}
    cases = {}
    for d, adid, _t in sorted(agendas):
        if not (start <= d <= end):
            continue
        try:
            items = agenda_items(adid)
        except Exception as e:  # noqa: BLE001
            log(f"agenda {adid} ({d}): {e}")
            continue
        for it in items:
            k = kind_of(it["header"], it["desc"])
            if not k:
                continue
            cases.setdefault(it["case"], []).append({**it, "kind": k, "date": d.isoformat(), "adid": adid,
                                                     "packet": packets.get(d)})
    log(f"{len(cases)} in-scope Planning Board cases on agendas {start}..{end}")
    keep = {c: a for c, a in cases.items() if min(x["date"] for x in a) >= since or c in refresh}
    details = {}
    by_packet = {}
    for c, a in keep.items():
        for x in a:
            if x.get("packet"):
                by_packet.setdefault(x["packet"], set()).add(c)
    for pk, wanted in by_packet.items():
        missing = {c for c in wanted if c not in details}
        if missing:
            details.update(packet_details(pk, missing))
    cc_apps = []
    if keep:
        try:
            first = min(min(x["date"] for x in a) for a in keep.values())
            cc_apps = cc_scan(first, end.isoformat())
        except Exception as e:  # noqa: BLE001
            log(f"CivicClerk scan failed: {e}")
    out = []
    for c, a in sorted(keep.items()):
        try:
            out.append(build(c, a, details, cc_apps, today.isoformat()))
        except Exception as e:  # noqa: BLE001
            log(f"{c}: skipped ({type(e).__name__}: {e})")
    for r in refresh - set(cases):
        log(f"refresh id {r} not found on any Planning Board agenda since {start}")
    for rec in out:
        if not rec["parcel_ids"] and rec.get("address"):
            rec["parcel_ids"] = parcels_by_address(rec["address"], "ORMOND BEACH")
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
