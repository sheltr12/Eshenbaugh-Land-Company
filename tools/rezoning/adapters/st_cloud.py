#!/usr/bin/env python3
"""City of St. Cloud (Osceola County) rezoning / PUD / future land use map amendment adapter.

St. Cloud has no public case portal we can use: its permit portal (stcloudflpermits.
selectpaytoday.com) sits behind a Cloudflare block, and Planning Commission agendas are posted
only as the current PDF on the city website. This adapter combines public sources:
  1. CivicClerk (stcloudfl.api.civicclerk.com): City Council agendas. Items are named
     "Ordinance No. 2026-35 MG3 Old Canoe Creek Project #ZMA26-00005"; the item description says
     "First Reading and Introduction" / "Final public hearing" / "public hearing AND TRANSMITTAL"
     and gives acres, from/to districts and location. The staff-report attachment is read
     (first and last page only, HTTP range requests, pypdf) for applicant, agent, parcel ID,
     total land area, the Planning Commission date and vote, and the first-reading vote.
  2. City Council minutes PDFs listed on https://www.stcloudfl.gov/1270/Minutes ("Action on
     Ordinance No. 2026-30 ... made a motion to approve the First Reading ... motion carried").
     Minutes are posted a few months after the meeting.
  3. The current Planning Commission agenda PDF linked from
     https://www.stcloudfl.gov/872/Boards-and-Committees-Agenda (only the latest one is linked).
  4. The city's Development Projects GIS layer
     arcgisweb.stcloud.org/arcgis/rest/services/Referenced_Layers/DevProj_Prod/FeatureServer/24
     (types ZONING and PUD, with applied date, parcel APN and a point): gives filing dates,
     locations and cases that have not reached a hearing yet. It does not hold comprehensive
     plan amendments.
Case numbers: ZMA26-00005 -> Rezoning (PD if the target district is a PUD), PUD26-00004 -> PD
(preliminary master plans and major amendments; final master plans only when adopted by
ordinance; minor amendments skipped), CPA26-00007 -> Comp Plan Amendment. Annexations (ANX) are
not collected; their companion CPA / ZMA cases are.
Parcel IDs: staff reports show the dashed Osceola PA format "16-26-30-0757-0001-00E2"; the GIS
layer shows the same PIN without dashes ("1626300757000100E2"). Returned as the source shows.
"""
import datetime as dt
import difflib
import html as htmlmod
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

JURISDICTIONS = ["City of St. Cloud"]
JUR, COUNTY = "City of St. Cloud", "Osceola"
SOURCE = "St. Cloud CivicClerk (City Council) + Council minutes + PC agenda + DevProj GIS"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
CC_API = "https://stcloudfl.api.civicclerk.com/v1"
CC_PORTAL = "https://stcloudfl.portal.civicclerk.com"
CITY = "https://www.stcloudfl.gov"
GIS = ("https://arcgisweb.stcloud.org/arcgis/rest/services/Referenced_Layers/DevProj_Prod/"
       "FeatureServer/24")
SLEEP = 0.4
LOOKBACK_DAYS = 120      # agenda scan starts this far before `since`
REFRESH_LOOKBACK_DAYS = 400
LOOKAHEAD_DAYS = 90
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
CASE_RX = re.compile(r"\b(CPA|ZMA|PUD|ANX)\s?-?\s?(\d{2,3})\s?-\s?(\d{4,5})\b", re.I)

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


def log(*a):
    print("[st_cloud]", *a, file=sys.stderr, flush=True)


def get(url, params=None, tries=4, sleep=SLEEP, **kw):
    """www.stcloudfl.gov intermittently resets connections: retry with backoff."""
    last = None
    for i in range(tries):
        try:
            r = _s.get(url, params=params, timeout=90, **kw)
            if r.status_code >= 500 or r.status_code == 429:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            time.sleep(sleep)
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1) ** 2)
    raise RuntimeError(f"GET failed {url}: {last}")


def clean(s):
    if s is None:
        return None
    s = htmlmod.unescape(re.sub(r"<[^>]+>", " ", str(s)))
    s = re.sub(r"\s+", " ", s).strip(" ,;")
    return s or None


def norm_case(pre, yy, seq):
    """('ZMA', '26', '0007') -> 'ZMA26-00007'. A 3-digit year (source typo 'ZMA256-00002') is
    returned as written."""
    pre = pre.upper()
    if len(yy) != 2:
        return f"{pre}{yy}-{seq}"
    return f"{pre}{yy}-{int(seq):05d}"


def cases_in(text):
    return list(dict.fromkeys(norm_case(*m) for m in CASE_RX.findall(text or "")))


def text_date(s):
    m = re.search(rf"({MONTHS})\s+(\d{{1,2}}),?\s+(\d{{4}})", s or "", re.I)
    if not m:
        return None
    try:
        return dt.datetime.strptime(f"{m.group(1).title()} {m.group(2)} {m.group(3)}", "%B %d %Y").date().isoformat()
    except ValueError:
        return None


def acres_of(*texts):
    for t in texts:
        for p in (r"Total Land Area\s*\+/-\s*([\d,]*\.?\d+)", r"approximately\s*\+/-\s*([\d,]*\.?\d+)\s*(?:ac|acres?)",
                  r"\+/-\s*([\d,]*\.?\d+)\s*(?:ac\b|acres?)", r"([\d,]*\.?\d+)\s*(?:\+/-\s*)?acres?\b"):
            m = re.search(p, t or "", re.I)
            if m:
                try:
                    v = float(m.group(1).replace(",", ""))
                    if v > 0:
                        return v
                except ValueError:
                    pass
    return None


# --------------------------------------------------------------------------- PDF over HTTP ranges
class RangeFile(io.RawIOBase):
    """Seekable read-only file over HTTP range requests (pypdf reads page 1 of a large PDF)."""

    def __init__(self, url, chunk=256 * 1024):
        self.url, self.chunk = url, chunk
        r = _s.head(url, timeout=60, allow_redirects=True)
        r.raise_for_status()
        self.size = int(r.headers["Content-Length"])
        self.pos, self.cache = 0, {}

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, off, whence=0):
        self.pos = off if whence == 0 else (self.pos + off if whence == 1 else self.size + off)
        return self.pos

    def _blk(self, i):
        if i not in self.cache:
            a = i * self.chunk
            b = min(self.size, a + self.chunk) - 1
            self.cache[i] = get(self.url, headers={"Range": f"bytes={a}-{b}"}, sleep=0.1).content
        return self.cache[i]

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        n = max(0, min(n, self.size - self.pos))
        out = bytearray()
        while n > 0:
            i = self.pos // self.chunk
            b = self._blk(i)
            piece = b[self.pos - i * self.chunk: self.pos - i * self.chunk + n]
            if not piece:
                break
            out += piece
            self.pos += len(piece)
            n -= len(piece)
        return bytes(out)

    def readinto(self, b):
        d = self.read(len(b))
        b[:len(d)] = d
        return len(d)


def staff_report(url):
    """First and last pages of a City Council staff report."""
    rdr = pypdf.PdfReader(RangeFile(url))
    n = len(rdr.pages)
    first = rdr.pages[0].extract_text() or ""
    last = "\n".join((rdr.pages[i].extract_text() or "") for i in range(max(1, n - 2), n)) if n > 1 else ""
    return parse_staff_report(first, last)


SR_LABELS = {  # normalized (lowercase, no spaces) label -> key
    "drccaseno./ord.no.": "case", "typeofapplication": "type", "casename": "name",
    "applicant/owner": "applicant", "applicant/owners": "applicant", "applicant/agent": "applicant",
    "applicant": "applicant", "applicants": "applicant", "owner": "owner", "owners": "owner",
    "owner(s)": "owner", "agent": "agent", "address/location": "location", "location": "location",
    "parcelid#": "parcel", "parcelid": "parcel", "parcelids": "parcel", "parcelid(s)": "parcel",
    "parcelid(s)#": "parcel", "parcelids#": "parcel", "totallandarea": "area", "projectplanner": "planner",
}
PARCEL_RX = re.compile(r"\b\d{2}-\d{2}-\d{2}-[0-9A-Z]{4}-[0-9A-Z]{4}-[0-9A-Z]{4}\b|\b\d{6}[0-9A-Z]{12}\b")


def label_key(norm):
    if norm in SR_LABELS:
        return SR_LABELS[norm]
    if re.fullmatch(r"parcel(?:\(s\)|s)?id(?:no|nos|number|numbers)?(?:\(s\))?\.?#?", norm):
        return "parcel"
    if re.fullmatch(r"applicant(?:\(s\))?(?:/(?:owner|agent)s?(?:\(s\))?)?", norm):
        return "applicant"
    if re.fullmatch(r"owner(?:\(s\)|s)?", norm):
        return "owner"
    if re.fullmatch(r"agent(?:\(s\)|s)?", norm):
        return "agent"
    return None


def fix_pdf_text(t):
    t = re.sub(r"(?m)^([A-Z])\s*\n(?=[a-z])", r"\1", t)          # "A\npplicant" -> "Applicant"
    t = re.sub(r"(\d{2}-\d{2}-\d{2}-[0-9A-Z]{4}-[0-9A-Z]{4}-)\s*\n\s*([0-9A-Z]{4})", r"\1\2", t)
    t = re.sub(r"(\d{2}-\d{2}-\d{2}-[0-9A-Z]{4}-)\s*\n\s*([0-9A-Z]{4}-[0-9A-Z]{4})", r"\1\2", t)
    return t


def parse_staff_report(first, last):
    first = fix_pdf_text(first)
    out, cur = {}, None
    lines = [x.strip() for x in first.split("\n")]
    for k, ln in enumerate(lines):
        if not ln:
            if cur and out.get(cur) and out[cur][-1] != "\x00":
                out[cur].append("\x00")
            continue
        key = label_key(re.sub(r"\s+", "", ln).lower())
        if key is None and k + 1 < len(lines):   # "DRC" + "Case No. /Ord. No." on two lines
            key = label_key(re.sub(r"\s+", "", ln + lines[k + 1]).lower())
            if key:
                lines[k + 1] = ""
        if key:
            cur = key
            out.setdefault(cur, [])
            continue
        if re.match(r"(Figure\s*\d|F\s*igure|Request\b|Background\b|Meeting Date|City Council Staff Report|"
                    r"Planning Commission Staff Report)", ln):
            cur = None
            continue
        if cur:
            out[cur].append(ln)
    info = {k: " ".join(x for x in v if x != "\x00") for k, v in out.items() if v}
    appl = [clean(x) for x in " ".join(out.get("applicant", [])).split("\x00")]
    appl = [x for x in appl if x]
    res = {
        "applicant": appl[0] if appl else None, "owner": clean(info.get("owner")),
        "agent": clean(info.get("agent")),
        "parcels": list(dict.fromkeys(PARCEL_RX.findall(info.get("parcel", "")) or PARCEL_RX.findall(first))),
        "acres": acres_of(info.get("area")), "type": clean(info.get("type")),
        "name": clean(info.get("name")), "location": clean(info.get("location")),
    }
    m = re.search(r"\bRequest\s+(The applicant is requesting.+?)(?:\n\s*\n|Background|The subject property is shown)",
                  first, re.S)
    if m:
        res["request"] = clean(m.group(1))
    flat = re.sub(r"\s+", " ", last)
    m = re.search(rf"On ((?:{MONTHS}) \d{{1,2}}, \d{{4}}),? the Planning Commission\W+(.{{0,160}}?)"
                  r"(Passed|Failed|carried|denied)\W*(\d+\s*-\s*\d+)?", flat, re.I)
    if m:
        res["pc"] = (text_date(m.group(1)), m.group(2) + " " + m.group(3))
    m = re.search(rf"On ((?:{MONTHS}) \d{{1,2}}, \d{{4}}),?\s*-?\s*Motion to (approve|deny|continue|table)"
                  r"[^.]{0,40}?First Reading.{0,120}?(Passed|Failed)", flat, re.I)
    if m:
        res["first"] = (text_date(m.group(1)), m.group(2), m.group(3))
    return res


# --------------------------------------------------------------------------- CivicClerk
def cc_events(start, end):
    url = f"{CC_API}/Events"
    params = {"$filter": f"startDateTime ge {start}T00:00:00Z and startDateTime lt {end}T00:00:00Z",
              "$orderby": "startDateTime"}
    out = []
    while url:
        d = get(url, params).json()
        out.extend(d.get("value", []))
        url, params = d.get("@odata.nextLink"), None
    return [e for e in out if re.search(r"Council", e.get("eventName") or "", re.I)
            and not re.search(r"workshop|cancel|budget", e.get("eventName") or "", re.I)]


def walk(items):
    for it in items or []:
        yield it
        yield from walk(it.get("childItems"))


def reading_of(desc):
    d = (desc or "").lower()
    if "transmittal" in d:
        return "transmittal"
    if re.search(r"final public hearing|second (?:and final )?reading|adoption", d):
        return "final"
    if re.search(r"first reading|introduction", d):
        return "first"
    return None


def scan_council(start, end):
    events = cc_events(start, end)
    apps = {}
    for ev in events:
        if not ev.get("agendaId"):
            continue
        try:
            mt = get(f"{CC_API}/Meetings/{ev['agendaId']}").json()
        except Exception as e:  # noqa: BLE001
            log(f"meeting {ev['agendaId']}: {e}")
            continue
        date = ev["startDateTime"][:10]
        files = {f.get("type"): f.get("fileId") for f in ev.get("publishedFiles", [])}
        for it in walk(mt.get("items")):
            name = clean(it.get("agendaObjectItemName")) or ""
            desc = clean(it.get("agendaObjectItemDescription")) or ""
            atts = it.get("attachmentsList") or []
            own = cases_in(re.search(r"Project\s*#\s*\S+", desc).group(0)) if re.search(r"Project\s*#\s*\S+", desc) \
                else []
            own = own or cases_in(name) or cases_in(" ".join(a.get("fileName") or "" for a in atts))[:1]
            own = [c for c in own if not c.startswith("ANX")]
            if not own:
                continue
            if not re.search(r"Ordinance", name, re.I) and not re.search(r"\bZMA|\bCPA", own[0]):
                continue   # e.g. a PUD final master plan approved without an ordinance
            ordm = re.search(r"Ordinance No\.?\s*(\d{4}-\d+)", name + " " + desc, re.I)
            sr = next((a.get("pdfVersionFullPath") for a in atts
                       if re.search(r"staff report", a.get("fileName") or "", re.I) and a.get("pdfVersionFullPath")), None)
            fph = re.search(rf"Final Public Hearing will be heard on ((?:{MONTHS})\s+\d{{1,2}},?\s+\d{{4}})", desc, re.I)
            for c in own[:1]:
                apps.setdefault(c, []).append({
                    "date": date, "body": "City Council", "event_id": ev["id"], "name": name, "desc": desc,
                    "reading": reading_of(desc), "ordinance": ordm.group(1) if ordm else None,
                    "staff_report": sr, "packet": files.get("Agenda Packet") or files.get("Agenda"),
                    "final_on": text_date(fph.group(1)) if fph else None, "result": None,
                })
    log(f"CivicClerk: {len(events)} Council meetings {start}..{end}; {len(apps)} CPA/ZMA/PUD cases on agendas")
    return apps


# --------------------------------------------------------------------------- minutes
def minutes_index():
    try:
        r = get(f"{CITY}/1270/Minutes")
    except Exception as e:  # noqa: BLE001
        log(f"minutes page unavailable: {e}")
        return {}
    idx = {}
    for m in re.finditer(r'href="(/DocumentCenter/View/\d+/[^"]+)"[^>]*>([^<]*Council[^<]*Minutes[^<]*)<', r.text, re.I):
        dm = re.search(r"(\d{1,2})[-./](\d{1,2})[-./](\d{2,4})", m.group(2)) or \
            re.search(r"(\d{1,2})(\d{2})(\d{2})-City", m.group(1))
        if not dm:
            continue
        mo, da, yr = int(dm.group(1)), int(dm.group(2)), int(dm.group(3))
        yr = yr + 2000 if yr < 100 else yr
        try:
            idx[dt.date(yr, mo, da).isoformat()] = CITY + m.group(1)
        except ValueError:
            continue
    return idx


def parse_minutes(text):
    """{ordinance no: (result, reading)} from Council minutes."""
    t = re.sub(r"\s+", " ", text)
    out = {}
    parts = re.split(r"(?=Action on Ordinance No\.?\s*\d{4}-\d+)", t, flags=re.I)
    for p in parts[1:]:
        om = re.match(r"Action on Ordinance No\.?\s*(\d{4}-\d+)", p, re.I)
        seg = p[:1500]
        nxt = re.search(r"\s\d{1,2}\.\s+(?:First Reading|Final Public Hearing|Public Hearing|Action on|Request)", seg[40:])
        if nxt:
            seg = seg[:40 + nxt.start()]
        mm = re.search(r"made a motion to (approve|adopt|deny|continue|table|postpone|withdraw)(.{0,400}?)"
                       r"(?:motion (carried|failed|passed)|carried|failed)", seg, re.I)
        if not mm:
            if re.search(r"withdrawn", seg, re.I):
                out[om.group(1)] = ("Withdrawn", None)
            continue
        verb, rest, outcome = mm.group(1).lower(), mm.group(2), (mm.group(3) or "carried").lower()
        reading = "first" if re.search(r"first reading|introduction", rest, re.I) else (
            "transmittal" if re.search(r"transmit", rest, re.I) else None)
        if outcome == "failed":
            res = f"Motion to {verb} failed"
        elif verb in ("continue", "table", "postpone"):
            res = "Continued"
        elif verb == "deny":
            res = "Denied"
        elif verb == "withdraw":
            res = "Withdrawn"
        else:
            res = {"first": "Approved on First Reading", "transmittal": "Transmitted"}.get(reading, "Approved")
        out[om.group(1)] = (res, reading)
    return out


# --------------------------------------------------------------------------- PC agenda
def current_pc_agenda():
    """[(case, date, ordinance, name, applicant, request)] from the latest PC agenda PDF."""
    try:
        r = get(f"{CITY}/872/Boards-and-Committees-Agenda")
    except Exception as e:  # noqa: BLE001
        log(f"boards agenda page unavailable: {e}")
        return []
    href = None
    for a in BeautifulSoup(r.text, "lxml").find_all("a", href=True):
        if re.fullmatch(r"\s*Planning Commission\s*", a.get_text(" ")) and "DocumentCenter" in a["href"]:
            href = a["href"]
            break
    if not href:
        log("current Planning Commission agenda link not found")
        return []
    url = href if href.startswith("http") else CITY + href
    try:
        pdf = get(url).content
        text = "\n".join((p.extract_text() or "") for p in pypdf.PdfReader(io.BytesIO(pdf)).pages)
    except Exception as e:  # noqa: BLE001
        log(f"PC agenda PDF: {e}")
        return []
    date = text_date(text[:600])
    out = []
    for blk in re.split(r"\n\s*[A-Z]\.\s+(?=(?:Ordinance|Resolution))", text):
        cs = [c for c in cases_in(blk[:300]) if not c.startswith("ANX")]
        if not cs or not date:
            continue
        lines = [x.strip() for x in blk.split("\n") if x.strip()]
        om = re.search(r"Ordinance No\.?\s*(\d{4}-\d+)", blk)
        name = clean(re.sub(r"\s*/\s*" + CASE_RX.pattern + ".*", "", lines[1], flags=re.I)) if len(lines) > 1 else None
        am = re.search(r"Applicant\s*/\s*Agent:\s*(.+)", blk)
        rq = re.search(r"(The applicant is requesting.+?)(?:\n\s*[A-Z]\.\s|III\.|$)", blk, re.S)
        out.append({"case": cs[0], "date": date, "ordinance": om.group(1) if om else None, "name": name,
                    "applicant": clean(am.group(1)) if am else None, "request": clean(rq.group(1)) if rq else None,
                    "url": url})
    log(f"PC agenda {date}: {len(out)} CPA/ZMA/PUD items")
    return out


# --------------------------------------------------------------------------- GIS
def gis_projects(start):
    ms = int(dt.datetime.fromisoformat(start).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    params = {"where": f"USER_PROJECTTYPE IN ('ZONING','PUD') AND USER_APPLIED >= {ms}",
              "outFields": "*", "returnGeometry": "true", "outSR": "4326", "f": "json"}
    try:
        d = get(f"{GIS}/query", params).json()
    except Exception as e:  # noqa: BLE001
        log(f"GIS layer unavailable: {e}")
        return {}
    if d.get("error"):   # some ArcGIS servers reject numeric dates in where clauses
        params["where"] = f"USER_PROJECTTYPE IN ('ZONING','PUD') AND USER_APPLIED >= DATE '{start}'"
        d = get(f"{GIS}/query", params).json()
    out = {}
    for f in d.get("features") or []:
        a = f["attributes"]
        cs = cases_in(a.get("USER_PROJECT_NO"))
        if not cs:
            continue
        g = f.get("geometry") or {}
        a["_lat"], a["_lon"] = g.get("y"), g.get("x")
        if a.get("USER_APPLIED"):
            a["_applied"] = dt.datetime.fromtimestamp(a["USER_APPLIED"] / 1000, dt.timezone.utc).date().isoformat()
        out[cs[0]] = a
    return out


def ms_date(v):
    return dt.datetime.fromtimestamp(v / 1000, dt.timezone.utc).date().isoformat() if v else None


# --------------------------------------------------------------------------- build
def category_of(case, to_zoning, gis):
    if case.startswith("CPA"):
        return "Comp Plan Amendment"
    if case.startswith("PUD"):
        return "PD"
    if re.search(r"PUD|Planned Unit Development", to_zoning or ""):
        return "PD"
    return "Rezoning"


def from_to(text, flu=False):
    """'... Zoning Map Amendment from "CCC" Community Center Core (County) to "CCC" Community Center
    Core (City) for approximately +/- 7.17 acres' -> (from, to)."""
    t = re.sub(r"[\"\u201c\u201d]", "", clean(text) or "")
    m = re.search(r"\bfrom\s+(.{1,90}?)\s+to\s+(.{1,90}?)(?:\s+for\s+(?:approximately|\+/-)|\s+on\s+approximately|"
                  r",|;|\s+generally|\s+providing|\.\s|\.$|$)", t, re.I)
    if m and not re.search(r"located|north|south", m.group(1), re.I):
        return fix_words(m.group(1)), fix_words(m.group(2))
    return None, None


def fix_words(v):
    """Undo PDF word splits: 'M xd' -> 'Mxd', 'R- 3' -> 'R-3', 'Multi -Family' -> 'Multi-Family'."""
    v = clean(v) or ""
    v = re.sub(r"\b([A-Z])\s(?=[a-z]{2,}\b)", r"\1", v)
    v = re.sub(r"(?<=\w)\s?-\s?(?=\w)", "-", v)
    return clean(v)


def build(case, apps, pc_items, gis, minutes_res, sr, today, gis_by_name):
    apps = sorted(apps, key=lambda a: a["date"])
    latest = apps[-1] if apps else None
    g = gis.get(case)
    if case.startswith("PUD") and g:
        sub = (g.get("USER_PROJECTSUBTYPE") or "").upper()
        if "MINOR" in sub:
            return None
        if "FINAL MASTER PLAN" in sub and not apps:
            return None
    descs = [a["desc"] for a in reversed(apps)] + [p.get("request") for p in pc_items]
    flu = case.startswith("CPA")
    fz = tz = ff = tf = None
    for d in [(sr or {}).get("request")] + descs:
        x, y = from_to(d, flu=flu)
        if x or y:
            if flu:
                ff, tf = x, y
            else:
                fz, tz = x, y
            break
    cat = category_of(case, tz, g)

    # hearings
    hs = {}
    for p in pc_items:
        hs[("Planning Commission", p["date"])] = {"body": "Planning Commission", "date": p["date"], "result": None}
    if sr and sr.get("pc") and sr["pc"][0]:
        d, txt = sr["pc"]
        res = "Recommended Denial" if re.search(r"den(y|ial)|failed", txt, re.I) else (
            "Continued" if re.search(r"contin|table", txt, re.I) else "Recommended Approval")
        hs[("Planning Commission", d)] = {"body": "Planning Commission", "date": d, "result": res}
    for a in apps:
        res = None
        if a["ordinance"] and (a["ordinance"], a["date"]) in minutes_res:
            res = minutes_res[(a["ordinance"], a["date"])][0]
            if res == "Approved" and a["reading"] == "first":
                res = "Approved on First Reading"
            elif res == "Approved" and a["reading"] == "transmittal":
                res = "Transmitted"
        hs[("City Council", a["date"])] = {"body": "City Council", "date": a["date"], "result": res,
                                           "_reading": a["reading"]}
        if a.get("final_on") and a["final_on"] > today:   # past planned dates may have moved
            hs.setdefault(("City Council", a["final_on"]), {"body": "City Council", "date": a["final_on"],
                                                            "result": None, "_reading": "final"})
    if sr and sr.get("first") and sr["first"][0]:
        d, verb, outc = sr["first"]
        k = ("City Council", d)
        if k in hs and not hs[k]["result"]:
            hs[k]["result"] = "Approved on First Reading" if verb.lower() == "approve" and outc.lower() == "passed" \
                else f"Motion to {verb} {outc.lower()}"
    hearings = sorted(hs.values(), key=lambda h: (h["date"], h["body"] != "Planning Commission"))

    # status
    status, decision, raw = None, None, None
    final = [h for h in hearings if h["body"] == "City Council" and h["result"] in ("Approved", "Denied")]
    past = [h for h in hearings if h["date"] <= today]
    future = [h for h in hearings if h["date"] > today]
    if any(h["result"] == "Withdrawn" for h in hearings):
        status, raw = "Withdrawn", "Withdrawn (Council minutes)"
    elif final:
        status, decision = final[-1]["result"], final[-1]["date"]
        raw = f"City Council {decision}: {final[-1]['result']} (minutes)"
    elif future:
        status = "Hearing Scheduled"
        raw = f"next {future[0]['body']} {future[0]['date']}"
        if future[0].get("_reading"):
            raw += f" ({future[0]['_reading']} reading)" if future[0]["_reading"] != "final" else " (final hearing)"
    elif past and past[-1]["result"] == "Continued":
        status, raw = "Continued", f"{past[-1]['body']} {past[-1]['date']}: continued"
    elif past and past[-1]["result"] is None:
        status = "Hearing Scheduled"
        raw = (f"{past[-1]['body']} {past[-1]['date']} "
               f"{'final hearing' if past[-1].get('_reading') == 'final' else 'hearing'} held; "
               f"outcome not yet in published minutes")
    elif past:
        status = "Filed"
        raw = f"{past[-1]['body']} {past[-1]['date']}: {past[-1]['result']}; next hearing not yet scheduled"
    else:
        status, raw = "Filed", "In review (no hearing on a published agenda yet)"
    for h in hearings:
        h.pop("_reading", None)

    # descriptive fields
    name = None
    if latest:
        m = re.match(r"Ordinance No\.?\s*\d{4}-\d+,?\s*(.+?)\s*(?:Project\s*#.*|\b(?:CPA|ZMA|PUD)\s*$|$)", latest["name"], re.I)
        name = clean(m.group(1)) if m else None
    name = (sr or {}).get("name") or name or next((p["name"] for p in pc_items if p.get("name")), None) \
        or (clean(g.get("USER_PROJECT_NAME")) if g else None)
    desc = (sr or {}).get("request") or next((p["request"] for p in pc_items if p.get("request")), None)
    if not desc and latest:
        m = re.search(r"(specifically changing.+?|assigning a Future Land Use.+?|amending.+?)(?:,\s*generally located|"
                      r";|providing for)", latest["desc"], re.I)
        desc = clean(m.group(1)) if m else clean(latest["desc"][:300])
    if desc and len(desc) > 400:
        desc = desc[:397].rstrip() + "..."
    applicant = (sr or {}).get("applicant") or next((p["applicant"] for p in pc_items if p.get("applicant")), None) \
        or (sr or {}).get("agent") or (sr or {}).get("owner")
    parcels = list(dict.fromkeys((sr or {}).get("parcels") or []))
    if not parcels and g and (g.get("USER_SITE_APN") or "").strip():
        parcels = [g["USER_SITE_APN"].strip()]
    acres = (sr or {}).get("acres") or acres_of(*descs) or ((g.get("USER_ACREAGE") or None) if g else None)
    filed = g.get("_applied") if g else None
    if not filed:   # CPAs are not in the GIS layer: earliest known date (first agenda appearance)
        firsts = [h["date"] for h in hearings] + [a["date"] for a in apps]
        filed = min(firsts) if firsts else None
    url = f"{CC_PORTAL}/event/{latest['event_id']}/files" if latest else (
        next((p["url"] for p in pc_items), None) or
        f"{GIS}/query?where=USER_PROJECT_NO%3D%27{g['USER_PROJECT_NO']}%27&outFields=*&f=html" if g else None)
    docs = (f"{CC_API}/Meetings/GetMeetingFileStream(fileId={latest['packet']},plainText=false)"
            if latest and latest.get("packet") else (pc_items[0]["url"] if pc_items else None))
    app_type = (sr or {}).get("type") or {"CPA": "Comprehensive Plan Amendment (Future Land Use Map)",
                                          "ZMA": "Zoning Map Amendment"}.get(case[:3])
    if case.startswith("PUD"):
        app_type = f"PUD - {g['USER_PROJECTSUBTYPE'].title()}" if g and g.get("USER_PROJECTSUBTYPE") else "PUD"
    if case.startswith("CPA") and latest and re.search(r"Large Scale", latest["desc"], re.I):
        app_type = "Large Scale Comprehensive Plan Amendment"
    return {
        "jurisdiction": JUR, "county": COUNTY, "app_num": case, "category": cat, "app_type": app_type,
        "project_name": name, "description": desc, "applicant": applicant, "parcel_ids": parcels,
        "address": (clean(g.get("USER_SITE_ADDR")) if g else None) or (sr or {}).get("location"),
        "acres": acres, "from_zoning": fz, "to_zoning": tz, "from_flu": ff, "to_flu": tf,
        "filed_date": filed, "status": status, "status_raw": raw, "hearings": hearings,
        "decision_date": decision, "url": url, "docs_url": docs,
        "lat": round(g["_lat"], 6) if g and g.get("_lat") else None,
        "lon": round(g["_lon"], 6) if g and g.get("_lon") else None,
        "source": SOURCE,
    }


def fetch(since, refresh):
    t0 = time.time()
    if pypdf is None:
        raise RuntimeError("pypdf is required for the St. Cloud adapter")
    today = dt.date.today().isoformat()
    refresh = [r.strip().upper() for r in (refresh or []) if r and r.strip()]
    rnorm = {}
    for r in refresh:
        cs = cases_in(r)
        rnorm[cs[0] if cs else r] = r
    start = (dt.date.fromisoformat(since) - dt.timedelta(days=LOOKBACK_DAYS))
    if refresh:
        start = min(start, dt.date.today() - dt.timedelta(days=REFRESH_LOOKBACK_DAYS))
    end = dt.date.today() + dt.timedelta(days=LOOKAHEAD_DAYS)
    try:
        apps = scan_council(start.isoformat(), end.isoformat())
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"St. Cloud CivicClerk unreachable: {e}") from e
    pc = {}
    for p in current_pc_agenda():
        pc.setdefault(p["case"], []).append(p)
    gis = gis_projects((start - dt.timedelta(days=365)).isoformat())
    gis_by_name = {}
    for c, a in gis.items():
        nm = re.sub(r"[^a-z0-9 ]", "", (a.get("USER_PROJECT_NAME") or "").lower()).strip()
        if nm and a.get("_applied"):
            gis_by_name[nm] = min(a["_applied"], gis_by_name.get(nm, "9999"))
    log(f"GIS DevProj: {len(gis)} ZONING/PUD projects applied since {start - dt.timedelta(days=365)}")

    candidates = set(apps) | set(pc) | {c for c, a in gis.items() if (a.get("_applied") or "") >= since}
    candidates |= set(rnorm)

    # minutes for Council meetings where candidate cases appeared
    midx = minutes_index()
    minutes_res = {}
    need = sorted({a["date"] for c in candidates for a in apps.get(c, []) if a["date"] <= today})
    for d in need:
        url = midx.get(d)
        if not url:
            continue
        try:
            txt = "\n".join((p.extract_text() or "") for p in pypdf.PdfReader(io.BytesIO(get(url).content)).pages)
            for o, v in parse_minutes(txt).items():
                minutes_res[(o, d)] = v
        except Exception as e:  # noqa: BLE001
            log(f"minutes {d}: {e}")
    log(f"minutes available for {sum(1 for d in need if d in midx)} of {len(need)} Council dates with cases")

    out = []
    for c in sorted(candidates):
        try:
            ca = apps.get(c, [])
            dates = [a["date"] for a in ca] + [p["date"] for p in pc.get(c, [])]
            if c in gis and gis[c].get("_applied"):
                dates.append(gis[c]["_applied"])
            first = min(dates) if dates else "9999"
            if c not in rnorm and first < since:
                continue
            sr = None
            for a in sorted(ca, key=lambda a: a["date"], reverse=True):
                if a.get("staff_report"):
                    try:
                        sr = staff_report(a["staff_report"])
                        break
                    except Exception as e:  # noqa: BLE001
                        log(f"{c}: staff report read failed: {e}")
            rec = build(c, ca, pc.get(c, []), gis, minutes_res, sr, today, gis_by_name)
            if rec is None:
                continue
            if c not in rnorm and rec["filed_date"] and rec["filed_date"] < since:
                continue
            out.append(rec)
        except Exception as e:  # noqa: BLE001
            log(f"{c}: skipped ({type(e).__name__}: {e})")
    for c in rnorm:
        if c not in apps and c not in pc and c not in gis:
            log(f"refresh id {c} not found on Council agendas, the PC agenda or the GIS layer")
    log(f"{len(out)} records in {time.time() - t0:.0f}s")
    return out


if __name__ == "__main__":
    since = sys.argv[1] if len(sys.argv) > 1 else (dt.date.today() - dt.timedelta(days=14)).isoformat()
    refresh = sys.argv[2].split(",") if len(sys.argv) > 2 and sys.argv[2] else []
    t0 = time.time()
    recs = fetch(since, refresh)
    print(json.dumps(recs, indent=2, ensure_ascii=False))
    from collections import Counter
    log(f"{len(recs)} records in {time.time() - t0:.0f}s; status={dict(Counter(r['status'] for r in recs))}; "
        f"category={dict(Counter(r['category'] for r in recs))}")
