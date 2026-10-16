#!/usr/bin/env python3
"""Orange County (unincorporated) rezoning / PD / future land use map amendment adapter.

Sources (all public, no login, no CAPTCHA):
  1. Orange County "Public_Notification" ArcGIS MapServer (ocgis4.ocfl.net), the layer behind the
     county's public-notice map. Layers 0 ("Development and Planning", open cases) and 6 (same,
     recently decided / pending). One point per case, straight out of the county's permitting
     database (the same folders FastTrack shows): case number, folder type, filing date (INDATE),
     next/last hearing date (ISSUEDATE), BCC hearing date, status, applicant, project name,
     parcel IDs, acreage, proposed zoning / FLU, request description, coordinates.
     The layer is a rolling window: a case drops out about two months after its last hearing.
       RZ   = conventional rezoning (also carries SS-yy-mm-nnn small-scale FLUM amendments and
              yyyy-n-A-n-n regular-cycle (large-scale) FLUM amendments)
       LUP  = Land Use Plan (PD) rezoning,  LUPA = PD Land Use Plan amendment
       CDR  = Change Determination Request (PD change); kept only when it goes to a BCC public
              hearing as a substantial change (otherwise it is an administrative DRC change)
  2. Orange County BCC Legistar (webapi.legistar.com/v1/occompt): public hearing matters whose
     titles carry the case numbers (BCC hearing dates, continuances, approvals, denials,
     withdrawals from matter histories), and the monthly "<date> Planning and Zoning Commission
     Recommendations" consent matter that the BCC approves to finalize conventional rezonings
     (cases pulled for a separate hearing are named in the motion text).
  3. The PZC "Rezoning Recommendation Book" PDF attached to each of those matters: one staff
     report per conventional rezoning heard by the PZC (applicant, owner, request, location,
     parcel IDs, acreage, PZC hearing date, PZC recommendation). Used for PZC outcomes and to
     backfill cases that already left the GIS window.
Notes:
  * FastTrack (fasttrack.ocfl.net) has the full case records, but every case search and case
    detail page is behind an image CAPTCHA, so it is only used as the clickable `url`.
  * Parcel IDs are returned as the county shows them: section-township-range format
    "18-22-31-0000-00-046" (GIS PARCELID_STR and the PZC books use the same format).
Requires pypdf.
"""
import datetime as dt
import io
import json
import re
import sys
import time
from urllib.parse import quote

import requests

try:
    import pypdf
except ImportError:  # pragma: no cover
    pypdf = None

try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("America/New_York")
except Exception:  # noqa: BLE001
    _TZ = None

JURISDICTIONS = ["Orange County"]

UA = "Mozilla/5.0 (X11; Linux x86_64) ELC-RezoningTracker/1.0 (+https://thedirtdog.com)"
GIS = "https://ocgis4.ocfl.net/arcgis/rest/services/Public_Notification/MapServer"
GIS_LAYERS = (0, 6)
LEG = "https://webapi.legistar.com/v1/occompt"
LEG_SITE = "https://occompt.legistar.com"
FASTTRACK = "https://fasttrack.ocfl.net/onlineservices/folderinformation.aspx?referencefile="
SOURCE = "Orange County Public Notification GIS + BCC Legistar (occompt) + PZC recommendation books"
SLEEP = 0.4
MATTER_LOOKBACK_DAYS = 420     # BCC matters scanned this far back (titles only, cheap)
BOOK_LOOKBACK_DAYS = 35        # PZC books with a hearing date >= since - this are parsed
MAX_EXTRA_BOOKS = 5            # extra books scanned to locate refresh ids missing elsewhere

_s = requests.Session()
_s.headers.update({"User-Agent": UA})

MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
CASE_RX = re.compile(r"\b(RZ|LUPA|LUP|CDR|SS)[\s-]?(\d{2})-(\d{2})-(\d{3})\b")
CASE_RX_SPACED = re.compile(r"\b(L\s?U\s?P\s?A|L\s?U\s?P|RZ|CDR|SS)[\s-]?(\d{2})-(\d{2})-(\d{3})\b")
LSCPA_RX = re.compile(r"\b(20\d\d)-(\d)-([AP])-(\d+)-(\d+)\b")
PARCEL_RX = re.compile(r"\b\d{2}-\d{2}-\d{2}-\d{4}-\d{2}-\d{3}\b")


def log(*a):
    print("[orange]", *a, file=sys.stderr)


# ------------------------------------------------------------------ http
def _get(url, params=None, tries=3, timeout=90, sleep=SLEEP, **kw):
    last = None
    for i in range(tries):
        try:
            r = _s.get(url, params=params, timeout=timeout, **kw)
            if r.status_code >= 500 or r.status_code == 429:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            time.sleep(sleep)
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


def _json(url, params=None, tries=5, **kw):
    """GET JSON; ArcGIS answers some queries with an intermittent {"error": 400} body, so retry those too."""
    last = None
    for i in range(tries):
        d = _get(url, params, **kw).json()
        if not (isinstance(d, dict) and d.get("error")):
            return d
        last = d["error"]
        time.sleep(2 * (i + 1))
    raise RuntimeError(f"{url}: {last}")


# ------------------------------------------------------------------ helpers
def norm_case(s):
    """Canonical case number: 'LU P-26-01-014' -> 'LUP-26-01-014', 'RZ 26-03-007' -> 'RZ-26-03-007'."""
    if not s:
        return None
    s = s.strip().upper()
    m = CASE_RX_SPACED.search(s)
    if m:
        return f"{m.group(1).replace(' ', '')}-{m.group(2)}-{m.group(3)}-{m.group(4)}"
    m = LSCPA_RX.search(s)
    if m:
        return m.group(0)
    return re.sub(r"\s+", " ", s)


def cases_in(text):
    out = []
    for m in CASE_RX_SPACED.finditer(text or ""):
        c = f"{m.group(1).replace(' ', '')}-{m.group(2)}-{m.group(3)}-{m.group(4)}"
        if c not in out:
            out.append(c)
    for m in LSCPA_RX.finditer(text or ""):
        if m.group(0) not in out:
            out.append(m.group(0))
    return out


def kind_of(case):
    if case.startswith("SS-"):
        return "SS"
    if LSCPA_RX.fullmatch(case):
        return "LSCPA" if "-A-" in case else "TEXT"
    return case.split("-", 1)[0]


CATEGORY = {"RZ": "Rezoning", "LUP": "PD", "LUPA": "PD", "CDR": "PD",
            "SS": "Comp Plan Amendment", "LSCPA": "Comp Plan Amendment"}
APP_TYPE = {"RZ": "Conventional Rezoning (RZ)", "LUP": "Land Use Plan (PD) rezoning (LUP)",
            "LUPA": "PD Land Use Plan Amendment (LUPA)",
            "CDR": "PD Change Determination Request, substantial change (CDR)",
            "SS": "Small-Scale Future Land Use Map Amendment (SS)",
            "LSCPA": "Regular-Cycle (Large-Scale) Future Land Use Map Amendment"}


def clean(v):
    if v is None:
        return None
    v = re.sub(r"\s+", " ", str(v)).strip(" ,;")
    return v or None


def short(v, n=300):
    v = clean(v)
    if v and len(v) > n:
        v = v[:n - 3].rstrip() + "..."
    return v


def epoch_date(ms):
    if not ms:
        return None
    t = dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc)
    if _TZ:
        t = t.astimezone(_TZ)
    return t.date().isoformat()


def mdy(s):
    """'10/07/2026' or 'Oct 13, 2026' or 'October 13, 2026' -> ISO."""
    if not s:
        return None
    s = s.strip()
    for f in ("%m/%d/%Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return dt.datetime.strptime(s, f).date().isoformat()
        except ValueError:
            pass
    return None


def long_date(s):
    m = re.search(rf"({MONTHS})\s+(\d{{1,2}}),\s*(\d{{4}})", s or "")
    if not m:
        return None
    return dt.datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y").date().isoformat()


def parse_acres(s):
    """'3.45 gross acres / 2.67 upland acres' -> 3.45; '.5197,.5197' (one value per parcel) -> sum."""
    if not s:
        return None
    t = str(s).strip()
    if re.fullmatch(r"\s*\d*\.?\d+(\s*,\s*\d*\.?\d+)+\s*", t):
        try:
            return round(sum(float(x) for x in t.split(",")), 2)
        except ValueError:
            return None
    m = re.search(r"(\d[\d,]*\.?\d*|\.\d+)", t)
    if not m:
        return None
    try:
        return round(float(m.group(1).replace(",", "")), 2)
    except ValueError:
        return None


def dedupe_applicant(s):
    """'PE Group, LLC (Robert Paymayesh) (Robert Paymayesh)' -> 'PE Group, LLC (Robert Paymayesh)'."""
    s = clean(s)
    if not s:
        return None
    parts = re.findall(r"\(([^()]*)\)", s)
    if len(parts) >= 2 and re.sub(r"\s+", " ", parts[-1]).strip() == re.sub(r"\s+", " ", parts[-2]).strip():
        s = s[: s.rfind("(")].strip()
    return re.sub(r"\s+", " ", s)


def from_to(text):
    """'To rezone from A-2 (Farmland Rural District) to R-3 (...)' -> ('A-2 (Farmland Rural District)', 'R-3 (...)')."""
    t = clean(text) or ""
    m = re.search(r"\bfrom\s+(.+?)\s+to\s+(?:an?\s+)?(.+?)(?:\s+(?:in order|(?:to\s+)?(?:allow|accommodate|construct|"
                  r"develop|permit|establish|create|split|bring|facilitate|support)|"
                  r"for (?:the|a|an|consistency|commercial|residential)|with a|which|consistent|that)\b|[.;]|$)", t, re.I)
    if not m:
        return None, None
    a, b = clean(m.group(1)), clean(m.group(2))
    if a:
        a = re.sub(r"^(?:the\s+)?(?:existing|current)\s+", "", a, flags=re.I)
    if a and len(a) > 120 or b and len(b) > 120:
        return None, None
    return a, b


def weekday(iso):
    try:
        return dt.date.fromisoformat(iso).weekday()
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------ 1. GIS
def gis_cases():
    out = {}
    ok = 0
    for lyr in GIS_LAYERS:
        try:
            d = _json(f"{GIS}/{lyr}/query", {
                "where": "FOLDERTYPE_ABBR IN ('RZ','LUP','LUPA','CDR')", "outFields": "*",
                "returnGeometry": "true", "outSR": "4326", "f": "json"})
        except Exception as e:  # noqa: BLE001
            log(f"GIS layer {lyr} failed: {e}")
            continue
        ok += 1
        if d.get("exceededTransferLimit"):
            log(f"GIS layer {lyr}: transfer limit exceeded, results truncated")
        for f in d.get("features", []):
            a = f["attributes"]
            case = norm_case(a.get("REFERENCEFILE"))
            if not case:
                continue
            g = f.get("geometry") or {}
            a["_lat"], a["_lon"] = g.get("y"), g.get("x")
            a["_layer"] = lyr
            prev = out.get(case)
            if prev is None or (prev["_layer"] == 6 and lyr == 0):
                out[case] = a
    if not ok:
        raise RuntimeError("all GIS layers failed")
    log(f"GIS: {len(out)} RZ/LUP/LUPA/CDR features (incl. SS / regular-cycle amendments)")
    return out


# ------------------------------------------------------------------ 2. Legistar
def leg_matters(start):
    out, skip = [], 0
    while True:
        d = _json(f"{LEG}/matters", {
            "$filter": f"MatterIntroDate ge datetime'{start}'",
            "$select": "MatterId,MatterGuid,MatterFile,MatterIntroDate,MatterAgendaDate,MatterTypeName,"
                       "MatterStatusName,MatterTitle",
            "$top": 1000, "$skip": skip})
        out.extend(d)
        if len(d) < 1000:
            return out
        skip += 1000


def matter_search(case):
    """Matters whose title mentions this case number (used for refresh ids)."""
    num = case.split("-", 1)[1] if not LSCPA_RX.fullmatch(case) else case
    try:
        return _json(f"{LEG}/matters", {"$filter": f"substringof('{num}',MatterTitle)",
                                        "$select": "MatterId,MatterGuid,MatterFile,MatterIntroDate,"
                                                   "MatterAgendaDate,MatterTypeName,MatterStatusName,MatterTitle"})
    except Exception as e:  # noqa: BLE001
        log(f"matter search {case}: {e}")
        return []


def histories(mid):
    try:
        hs = _json(f"{LEG}/matters/{mid}/histories")
    except Exception as e:  # noqa: BLE001
        log(f"histories {mid}: {e}")
        return []
    out = []
    for h in hs:
        if "Board of County Commissioners" not in (h.get("MatterHistoryActionBodyName") or "") \
                and h.get("MatterHistoryActionBodyName"):
            continue
        out.append({"date": (h.get("MatterHistoryActionDate") or "")[:10],
                    "action": h.get("MatterHistoryActionName") or "",
                    "passed": h.get("MatterHistoryPassedFlagName"),
                    "text": h.get("MatterHistoryActionText") or ""})
    return sorted(out, key=lambda x: x["date"])


def attachments(mid):
    try:
        return _json(f"{LEG}/matters/{mid}/attachments")
    except Exception as e:  # noqa: BLE001
        log(f"attachments {mid}: {e}")
        return []


def matter_url(m):
    return f"{LEG_SITE}/LegislationDetail.aspx?ID={m['MatterId']}&GUID={m['MatterGuid']}"


def bcc_result(action, passed, title):
    a = (action or "").lower()
    if "withdr" in a:
        return "Withdrawn"
    if passed and passed.lower().startswith("fail"):
        return f"Motion to {action} failed" if action else None
    if "contin" in a or "postpon" in a or "defer" in a or "table" in a:
        return "Continued"
    if "den" in a:
        return "Denied"
    if "approv" in a or "adopt" in a or "accept" in a:
        if re.match(r"\s*Transmittal", title or "", re.I):
            return "Transmitted"
        return "Approved"
    return action or None


# ------------------------------------------------------------------ 3. PZC recommendation books
LABELS = [("applicant", r"APPLICANTS?"), ("owner", r"OWNERS?"), ("hearing_type", r"HEARING TYPE"),
          ("request", r"REQUEST"), ("location", r"LOCATION"), ("parcels", r"PARCEL ID NUMBERS?"),
          ("size", r"SIZE\s*/\s*ACREAGE"), ("notice", r"PUBLIC\s*(?:\n\s*)?NOTIFICATION"),
          ("community", r"COMMUNITY MEETING"), ("use", r"PROPOSED USE"), ("staff", r"STAFF RECOMMENDATION")]
_LABEL_RX = re.compile(r"^\s*(" + "|".join(p for _, p in LABELS) + r")\s*:?", re.M)


def parse_cover(t):
    out = {}
    hits = list(_LABEL_RX.finditer(t))
    for i, m in enumerate(hits):
        lab = m.group(1)
        key = next((k for k, p in LABELS if re.fullmatch(p, lab.strip())), None)
        end = hits[i + 1].start() if i + 1 < len(hits) else len(t)
        if key and key not in out:
            out[key] = clean(t[m.end():end])
    return out


def pzc_result(block):
    b = re.sub(r"\s+", " ", block or "")
    if re.search(r"POSTPONED|CONTINUED", b):
        return "Continued"
    if re.search(r"WITHDRAWN", b):
        return "Withdrawn"
    m = re.search(r"\brecommend\s+(?:the\s+)?(APPROVAL|DENIAL|Approval|Denial)", b)
    if m:
        return "Recommended Denial" if m.group(1).lower() == "denial" else "Recommended Approval"
    if re.search(r"\bDENIAL\b|\bdeny\b", b):
        return "Recommended Denial"
    if re.search(r"\bAPPROVAL\b", b):
        return "Recommended Approval"
    return None


def parse_book(url):
    """Return {case: info} for every staff report in a PZC rezoning recommendation book."""
    r = _get(url, timeout=300)
    rdr = pypdf.PdfReader(io.BytesIO(r.content))
    pages = []
    for p in rdr.pages:
        try:
            pages.append(p.extract_text() or "")
        except Exception:  # noqa: BLE001
            pages.append("")
    out, cur = {}, None
    for i, t in enumerate(pages):
        m = re.search(r"CASE\s*#\s*([A-Z ]{2,6}-?\d{2}-\d{2}-\d{3})", t)
        if m and "GENERAL INFORMATION" in t:
            cur = norm_case(m.group(1))
            info = parse_cover(t[m.end():])
            hd = re.search(rf"(?:PZC|BCC) Hearing Date:\s*((?:{MONTHS})\s+\d{{1,2}},\s*\d{{4}})", t)
            info["pzc_date"] = long_date(hd.group(1)) if hd else None
            info["page"] = i + 1
            out[cur] = info
            continue
        m = re.search(r"Planning and Zoning Commission \(PZC\) Recommendation\s*.\s*"
                      rf"((?:{MONTHS})\s+\d{{1,2}},\s*\d{{4}})(.*?)(?:PLANNING AND ZONING COMMISSION \(PZC\) PUBLIC "
                      r"HEARING SYNOPSIS|$)", t, re.S)
        if m and cur and cur in out and "pzc_result" not in out[cur]:
            out[cur]["pzc_result"] = pzc_result(m.group(2))
            out[cur]["pzc_date"] = out[cur].get("pzc_date") or long_date(m.group(1))
    return out


# ------------------------------------------------------------------ 4. BCC request memos
def valid_parcels(ids):
    """Keep Orange County section-township-range parcel IDs (sec 01-36, twp 20-24, rng 25-34)."""
    out = []
    for p in ids:
        p = (p or "").strip()
        if not PARCEL_RX.fullmatch(p):
            continue
        s_, t_, r_ = (int(x) for x in p.split("-")[:3])
        if 1 <= s_ <= 36 and 20 <= t_ <= 24 and 25 <= r_ <= 34 and p not in out:
            out.append(p)
    return out


_REQ_CACHE = {}


def request_info(ms):
    """Parcel IDs, acreage text and advertising language from the planning division's
    'Request for BCC Public Hearing' memo attached to the BCC matter (small text PDF)."""
    for m in sorted(ms, key=lambda m: m.get("MatterIntroDate") or "", reverse=True):
        mid = m["MatterId"]
        if mid in _REQ_CACHE:
            if _REQ_CACHE[mid]:
                return _REQ_CACHE[mid]
            continue
        _REQ_CACHE[mid] = {}
        atts = [x for x in attachments(mid) if re.search(r"Request", x.get("MatterAttachmentName") or "", re.I)
                and (x.get("MatterAttachmentHyperlink") or "").lower().endswith(".pdf")]
        if not atts:
            continue
        try:
            r = _get(atts[0]["MatterAttachmentHyperlink"], timeout=120)
            rdr = pypdf.PdfReader(io.BytesIO(r.content))
            text = "\n".join((p.extract_text() or "") for p in rdr.pages[:8])
        except Exception as e:  # noqa: BLE001
            log(f"request memo {mid}: {e}")
            continue
        flat = re.sub(r"\s+", " ", text)
        adv = r"A\s?DVERTISING\s+LANGUAGE"
        ad = re.search(r"Advertising Language:?\s*(.+?)(?:Material Provided|Special Instructions|$)", flat)
        ad_am = re.search(adv + r"\s+FOR\s+AMENDMENT:?\s*(.+?)(?:" + adv + r"|MATERIALS|$)", flat)
        ad_rz = re.search(adv + r"\s+FOR\s+REZONING:?\s*(.+?)(?:" + adv + r"|MATERIALS|$)", flat)
        loc = re.search(r"General Location:\s*(.+?)(?:Parcel ID|S\s?i\s?z\s?e\s*/|BCC Public|ESTIMATED TIME|"
                        r"HEARING CONTROVERSIAL|Jason|Digitally|Received on|Legislative file|Case #|$)", flat, re.I)
        size = re.search(r"S\s?i\s?z\s?e\s*/\s*A\s?c\s?r\s?e\s?a\s?g\s?e\s*:?\s*(.{0,160})", flat)
        info = {"parcels": valid_parcels(PARCEL_RX.findall(text)),
                "ad": clean(ad.group(1)) if ad else None,
                "ad_am": clean(ad_am.group(1)) if ad_am else None,
                "ad_rz": clean(ad_rz.group(1)) if ad_rz else None,
                "location": clean(loc.group(1)) if loc else None, "text": flat,
                "acres": size_acres(size.group(1)) if size else None}
        _REQ_CACHE[mid] = info
        return info
    return {}


def size_acres(v):
    """'1,236.20 gross acres (overall PD) 10.05 gross acres (affected parcel)' -> 10.05 (the affected area)."""
    v = re.sub(r"(\d),\s+(\d{3})", r"\1,\2", v or "")
    m = re.search(r"(\d[\d,]*\.?\d*|\.\d+)\s*(?:gross\s+|net\s+)?acres?\s*\(affected", v, re.I) or \
        re.search(r"(\d[\d,]*\.?\d*|\.\d+)\s*(?:\+/-\s*)?(?:gross\s+|net\s+)?acres?\b", v, re.I)
    return parse_acres(m.group(1)) if m else None


FIRM_RX = re.compile(r"\b(LLC|L\.L\.C|Inc|P\.A|PLLC|LLP|Law|Engineer|Associates|Design|Group|Corp|Company|Consult|"
                     r"Planning|Development|Studio|Partners|Firm|Holdings|Trust|Habitat|Homes)\b", re.I)


def title_parties(ms):
    """Applicant and project name from BCC matter titles such as
    'Case # CDR-25-10-248  Kody Klonowski, THS National LLC, Epoch Aloma Planned Development (PD); District 5' or
    'Adoption of Future Land Use Map Amendment SS-26-03-008, and Ordinance, Joseph Mingot'."""
    for m in sorted(ms, key=lambda m: m.get("MatterIntroDate") or "", reverse=True):
        t = m.get("MatterTitle") or ""
        last = None
        for last in list(CASE_RX_SPACED.finditer(t)) + list(LSCPA_RX.finditer(t)):
            pass
        hits = sorted([x.end() for x in CASE_RX_SPACED.finditer(t)] + [x.end() for x in LSCPA_RX.finditer(t)])
        if not hits:
            continue
        rest = t[hits[-1]:]
        rest = re.sub(r"\((?:Continued|Withdrawn)[^)]*\)", "", rest)
        rest = re.sub(r";\s*Districts?\b.*$", "", rest, flags=re.S)
        rest = re.sub(r"^[\s,]*(?:and\s+)?(?:Ordinance)?[\s,]*", "", rest)
        rest = re.sub(r"^\W+", "", rest)
        parts = [clean(p) for p in rest.split(",") if clean(p)]
        parts = [p for p in parts if not long_date(p) and not re.fullmatch(r"\d{4}", p)]
        if not parts:
            continue
        proj = next((p for p in parts if re.search(r"Planned Development|\bPD\b|Land Use Plan", p)), None)
        app = [p for p in parts if p != proj]
        applicant = None
        if app:
            applicant = app[0]
            if len(app) > 1 and FIRM_RX.search(app[1]) and not re.search(r"amend plan|permit", app[1], re.I):
                applicant += ", " + app[1]
        if applicant and re.search(r"amend plan|^Ordinance$", applicant, re.I):
            applicant = None
        return applicant, proj
    return None, None


# ------------------------------------------------------------------ fetch
def fetch(since, refresh):
    if pypdf is None:
        raise RuntimeError("pypdf is required for the Orange County adapter")
    today = dt.date.today()
    tiso = today.isoformat()
    refresh = [norm_case(r) for r in (refresh or []) if r and r.strip()]

    # 1. GIS
    try:
        gis = gis_cases()
    except Exception as e:  # noqa: BLE001
        log(f"GIS unavailable: {e}")
        gis = {}

    # 2. Legistar matters (titles only)
    mstart = min(dt.date.fromisoformat(since), today - dt.timedelta(days=MATTER_LOOKBACK_DAYS)).isoformat()
    try:
        matters = leg_matters(mstart)
    except Exception as e:  # noqa: BLE001
        if not gis:
            raise RuntimeError(f"Orange County sources unreachable (GIS and Legistar): {e}")
        log(f"Legistar unavailable: {e}")
        matters = []
    log(f"Legistar: {len(matters)} matters since {mstart}")

    case_matters = {}     # case -> [matter]
    rec_matters = []      # PZC recommendation consent matters
    for m in matters:
        title = m.get("MatterTitle") or ""
        if re.search(r"Planning and Zoning Commission Recommendations", title, re.I):
            d = long_date(title)
            if d:
                m["_pzc_date"] = d
                rec_matters.append(m)
            continue
        if re.search(r"Special Magistrate", title, re.I):
            continue    # FLUEDRA / Bert Harris proceedings, not a zoning hearing
        for c in cases_in(title):
            case_matters.setdefault(c, []).append(m)
    for r in refresh:
        if r not in case_matters and kind_of(r) != "RZ":
            for m in matter_search(r):
                if r in cases_in(m.get("MatterTitle")):
                    case_matters.setdefault(r, [])
                    if m["MatterId"] not in {x["MatterId"] for x in case_matters[r]}:
                        case_matters[r].append(m)
    rec_by_date = {m["_pzc_date"]: m for m in rec_matters}

    # 3. PZC books: hearings since `since` (minus a margin), plus hearings of GIS RZ cases
    want_dates = {d for d in rec_by_date if d >= (dt.date.fromisoformat(since)
                                                   - dt.timedelta(days=BOOK_LOOKBACK_DAYS)).isoformat()}
    for c, a in gis.items():
        if kind_of(c) == "RZ":
            d = mdy(a.get("ISSUEDATE"))
            if d and d <= tiso and d in rec_by_date and (c in refresh or (epoch_date(a.get("INDATE")) or "") >= since):
                want_dates.add(d)
    books = {}            # case -> book info
    parsed_dates = set()

    def load_book(d):
        if d in parsed_dates:
            return
        parsed_dates.add(d)
        m = rec_by_date[d]
        atts = attachments(m["MatterId"])
        pdfs = [a for a in atts if (a.get("MatterAttachmentHyperlink") or "").lower().endswith(".pdf")]
        if not pdfs:
            log(f"PZC {d}: no book attached")
            return
        url = pdfs[0]["MatterAttachmentHyperlink"]
        try:
            info = parse_book(url)
        except Exception as e:  # noqa: BLE001
            log(f"PZC book {d}: {e}")
            return
        for c, v in info.items():
            v["book_url"] = url
            v["book_date"] = d
            v["pzc_date"] = d          # one book per PZC meeting; cover dates sometimes have typos
            if c not in books or (v.get("pzc_date") or "") > (books[c].get("pzc_date") or ""):
                books[c] = v
        log(f"PZC book {d}: {len(info)} cases")

    for d in sorted(want_dates):
        load_book(d)
    # refresh RZ ids we still know nothing about: scan the books for the months after the case's yy-mm
    missing = [r for r in refresh if kind_of(r) == "RZ" and r not in gis and r not in books]
    extra = 0
    for r in missing:
        if r in books:
            continue
        yy, mm = int(r[3:5]), int(r[6:8])
        lo = dt.date(2000 + yy, mm, 1)
        hi = lo + dt.timedelta(days=150)
        for d in sorted(x for x in rec_by_date if lo.isoformat() <= x <= hi.isoformat()):
            if r in books or extra >= MAX_EXTRA_BOOKS:
                break
            if d not in parsed_dates:
                extra += 1
                load_book(d)

    # candidate cases
    cands = set()
    for c, a in gis.items():
        k = kind_of(c)
        if k not in CATEGORY:
            continue
        cands.add(c)
    for c in case_matters:
        if kind_of(c) in CATEGORY:
            cands.add(c)
    for c in books:
        if kind_of(c) in CATEGORY:
            cands.add(c)
    for r in refresh:
        if kind_of(r) in CATEGORY:
            cands.add(r)

    # histories only for matters of cases that may be output
    hist_cache = {}
    out = []
    for c in sorted(cands):
        a = gis.get(c)
        ms = case_matters.get(c, [])
        filed = epoch_date(a.get("INDATE")) if a else None
        known = [(m.get("MatterIntroDate") or "")[:10] for m in ms if m.get("MatterIntroDate")]
        if c in books and books[c].get("pzc_date"):
            known.append(books[c]["pzc_date"])
        first_seen = filed or (min(known) if known else None)
        k = kind_of(c)
        if k == "CDR":
            # only substantial changes that go to a BCC public hearing
            bcc = bool(ms) or (a and (a.get("STATUS") == "BCC" or a.get("BCC_MEETING")))
            names = " ".join([a.get("FOLDERNAME") or "" if a else ""] + [m.get("MatterTitle") or "" for m in ms])
            if not bcc or not re.search(r"\bPD\b|Planned Development", names):
                continue
            mi =[(m.get("MatterIntroDate") or "")[:10] for m in ms if m.get("MatterIntroDate")]
            if mi:
                first_seen = min(mi)    # first appeared as a BCC substantial-change hearing
        if c not in refresh:
            if not first_seen:
                continue
            if (filed or first_seen) < since and not (k == "CDR" and first_seen >= since):
                continue
        try:
            for m in ms:
                if m["MatterId"] not in hist_cache:
                    hist_cache[m["MatterId"]] = histories(m["MatterId"])
            rec = build(c, a, ms, hist_cache, books.get(c), rec_by_date, tiso, filed, first_seen)
        except Exception as e:  # noqa: BLE001
            log(f"{c}: skipped ({e})")
            continue
        if rec:
            out.append(rec)
    for r in refresh:
        if r not in {x["app_num"] for x in out}:
            log(f"refresh id {r}: not found in GIS window, Legistar titles or scanned PZC books")
    return out


def build(c, a, ms, hist_cache, book, rec_by_date, today, filed, first_seen):
    k = kind_of(c)
    a = a or {}
    book = book or {}
    if not a and not book and not ms:
        return None     # nothing known about this case in any source
    title = " ".join(m.get("MatterTitle") or "" for m in ms)
    if k == "LSCPA" and not a and not re.search(r"Future Land Use Map Amendment", title, re.I):
        return None

    # ---- hearings
    hmap = {}

    def add(body, date, result=None, prefer=False):
        if not date:
            return
        key = (body, date)
        cur = hmap.get(key)
        if cur is None:
            hmap[key] = {"body": body, "date": date, "result": result}
        elif result and (cur["result"] is None or prefer):
            cur["result"] = result

    issue = mdy(a.get("ISSUEDATE"))
    status_g = (a.get("STATUS") or "").strip()
    if issue:
        wd = weekday(issue)
        if wd == 3:   # PZC / LPA meets on Thursdays
            res = None
            if issue <= today and k in ("RZ",) and status_g in ("Approved", "Denied"):
                res = "Recommended Approval" if status_g == "Approved" else "Recommended Denial"
            add("Planning and Zoning Commission", issue, res)
        elif wd == 2 and status_g not in ("TRG",) and k in ("LUP", "LUPA", "CDR"):
            add("DRC", issue, "Approved" if status_g == "Approved" and k == "CDR" else None)
        elif wd in (0, 1):
            add("BCC", issue)
    bccm = mdy(a.get("BCC_MEETING"))
    if bccm:
        add("BCC", bccm)
    if book.get("pzc_date"):
        add("Planning and Zoning Commission", book["pzc_date"], book.get("pzc_result"), prefer=True)

    final = None          # (status, date, raw)
    withdrawn = status_g == "Withdrawn"
    murl = None
    for m in sorted(ms, key=lambda m: (m.get("MatterIntroDate") or "")):
        t = m.get("MatterTitle") or ""
        murl = matter_url(m)
        # PZC date in BCC hearing titles: "Case # LUP-26-01-014 Tanner Kalebaugh, Stonemont PD, October 15, 2026"
        if re.search(r"Case\s*#", t) and k in ("RZ", "LUP", "LUPA"):
            pd_ = long_date(re.sub(r"\(Continued from.*?\)", "", t))
            if pd_:
                add("Planning and Zoning Commission", pd_)
        if re.search(r"\(Withdrawn\)", t, re.I):
            withdrawn = True
        hs = hist_cache.get(m["MatterId"], [])
        for h in hs:
            res = bcc_result(h["action"], h["passed"], t)
            add("BCC", h["date"], res, prefer=True)
            if res == "Withdrawn":
                withdrawn = True
            if res in ("Approved", "Denied"):
                if k == "LSCPA" and not re.match(r"\s*Adoption", t, re.I):
                    continue
                final = (res, h["date"], f"BCC {h['date']}: {h['action']}")
        ad = (m.get("MatterAgendaDate") or "")[:10]
        if ad and not any(h["date"] == ad for h in hs):
            add("BCC", ad)

    # conventional rezonings: BCC approves the monthly PZC recommendations on consent
    pzc_dates = sorted(h["date"] for h in hmap.values() if h["body"] == "Planning and Zoning Commission")
    if k == "RZ" and not final and pzc_dates:
        pd_ = pzc_dates[-1]
        rm = rec_by_date.get(pd_)
        pres = hmap[("Planning and Zoning Commission", pd_)]["result"]
        if rm and pres in ("Recommended Approval", "Recommended Denial") and not ms:
            hs = hist_cache.get(("rec", rm["MatterId"]))
            if hs is None:
                hs = histories(rm["MatterId"])
                hist_cache[("rec", rm["MatterId"])] = hs
            done = [h for h in hs if (h["action"] or "").lower().startswith(("approv", "accept"))
                    and (h["passed"] or "Pass") != "Fail"]
            if done:
                h = done[-1]
                excepted = c in cases_in(h["text"].split("with the exception of", 1)[1]) \
                    if "with the exception of" in h["text"] else False
                if not excepted:
                    res = "Approved" if pres == "Recommended Approval" else "Denied"
                    add("BCC", h["date"], f"{res} (PZC recommendations accepted)", prefer=True)
                    final = (res, h["date"], f"PZC {pd_}: {pres}; BCC {h['date']}: PZC recommendations approved")
            else:
                ad = (rm.get("MatterAgendaDate") or "")[:10]
                if ad:
                    add("BCC", ad, None)
            murl = murl or matter_url(rm)

    hearings = sorted(hmap.values(), key=lambda h: (h["date"], {"DRC": 0, "Planning and Zoning Commission": 1}
                                                    .get(h["body"], 2)))
    past = [h for h in hearings if h["date"] <= today]
    future = [h for h in hearings if h["date"] > today]

    status, raw, decision = None, None, None
    if withdrawn:
        status, raw = "Withdrawn", "Withdrawn" + (f" (GIS status {status_g})" if status_g else " (BCC agenda)")
    elif final:
        status, decision, raw = final[0], final[1], final[2]
    elif past and past[-1]["result"] == "Continued" and not future:
        status, raw = "Continued", f"{past[-1]['body']} {past[-1]['date']}: continued/postponed"
    elif status_g in ("Postponed", "Continued") and not future:
        status, raw = "Continued", f"GIS status {status_g}"
    elif future:
        status = "Continued" if past and past[-1]["result"] == "Continued" else "Hearing Scheduled"
        raw = f"next {future[0]['body']} {future[0]['date']}"
        if past and past[-1]["result"]:
            raw = f"{past[-1]['body']} {past[-1]['date']}: {past[-1]['result']}; " + raw
    elif past and past[-1]["result"] is None and past[-1]["body"] != "DRC":
        status, raw = "Hearing Scheduled", f"{past[-1]['body']} {past[-1]['date']}: result not yet posted"
    elif past:
        status, raw = "Filed", f"{past[-1]['body']} {past[-1]['date']}: {past[-1]['result'] or 'reviewed'}; " \
                               f"next hearing not yet scheduled"
    else:
        status, raw = "Filed", "Under review"
    if status_g:
        raw = f"{raw} | FastTrack status: {status_g}"

    # ---- descriptive fields
    req = request_info(ms) if (not a and not book and ms) else {}
    if req:
        req = dict(req)
        req["ad"] = (req.get("ad_rz") if k == "RZ" else req.get("ad_am") if k in ("SS", "LSCPA") else None) \
            or req.get("ad")
    desc_src = a.get("FOLDERDESCRIPTION") or book.get("request") or req.get("ad") or \
        (clean(re.sub(r"^\W+", "", ms[-1].get("MatterTitle") or "")) if ms else None)
    frm = to = None
    if k != "CDR":
        if book.get("request"):
            mm = re.match(r"(.+?)\s*(?<=[\s)])to\s+(.+)$", book["request"])
            if mm:
                frm, to = clean(mm.group(1)), clean(mm.group(2))
        if not frm:
            frm, to = from_to(a.get("FOLDERDESCRIPTION") or req.get("ad"))
        if not frm and ms:
            mm = re.search(r"([A-Z][A-Za-z/\- ]+?\s\([A-Z0-9/\-]+\))\s+to\s+([A-Z][A-Za-z/\- ]+?\s\([A-Z0-9/\-]+\))",
                           title)
            if mm:
                frm, to = clean(mm.group(1)), clean(mm.group(2))
    prop = clean(a.get("PROPOSED_ZONING"))
    if prop:
        prop = re.sub(r"^Proposed FLU:\s*", "", prop)
    is_cpa = CATEGORY.get(k) == "Comp Plan Amendment"
    from_z = to_z = from_flu = to_flu = None
    if is_cpa:
        from_flu, to_flu = frm, (to or prop)
    else:
        from_z, to_z = frm, to
        if k in ("RZ", "LUP") and prop and (not to_z or len(to_z) > 80):
            to_z = prop
        if k == "LUP" and not to_z:
            to_z = "PD"

    parcels = valid_parcels(re.split(r"[,;\s]+", a.get("PARCELID_STR") or ""))
    if not parcels:
        parcels = valid_parcels(PARCEL_RX.findall(book.get("parcels") or ""))
    if not parcels:
        parcels = req.get("parcels") or []
    acres = parse_acres(a.get("ACREAGE")) if a.get("ACREAGE") else None
    if acres is None:
        acres = parse_acres(book.get("size"))
    if acres is None:
        acres = req.get("acres")
    for src in (desc_src, req.get("ad"), req.get("text")):
        if acres is not None:
            break
        mm = re.search(r"(\d[\d,]*\.?\d*|\.\d+)\s*(?:\+/-\s*|\u00b1\s*)?(?:gross\s+|net\s+)?acres?\b", src or "", re.I)
        if mm:
            acres = parse_acres(mm.group(1))
    fname = clean(a.get("FOLDERNAME"))
    address = None
    project = None
    if fname and re.match(r"^\d+\s", fname) and not re.search(r"FLUMA|Land Use Plan|PD\b", fname):
        address = fname
    elif fname:
        project = fname
    if not address and book.get("location"):
        address = clean(book["location"].split(";")[0])
    if not address and req.get("location"):
        address = clean(req["location"].split(";")[0])
    applicant = dedupe_applicant(a.get("APPLICANT")) or clean(book.get("applicant"))
    t_app, t_proj = title_parties(ms)
    applicant = applicant or t_app
    project = project or t_proj
    docs = book.get("book_url")
    if docs and book.get("page"):
        docs = f"{docs}#page={book['page']}"
    if ms:
        docs = matter_url(sorted(ms, key=lambda m: m.get("MatterIntroDate") or "")[-1])
    url = a.get("FASTTRACK") or (FASTTRACK + quote(c))

    return {
        "jurisdiction": "Orange County", "county": "Orange", "app_num": c,
        "category": CATEGORY[k], "app_type": APP_TYPE[k],
        "project_name": project, "description": short(desc_src), "applicant": applicant,
        "parcel_ids": parcels, "address": address, "acres": acres,
        "from_zoning": from_z, "to_zoning": to_z, "from_flu": from_flu, "to_flu": to_flu,
        "filed_date": filed or first_seen,
        "status": status, "status_raw": raw, "hearings": hearings, "decision_date": decision,
        "url": url, "docs_url": docs,
        "lat": round(a["_lat"], 6) if a.get("_lat") else None,
        "lon": round(a["_lon"], 6) if a.get("_lon") else None,
        "source": SOURCE, "owner": clean(book.get("owner")),
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
