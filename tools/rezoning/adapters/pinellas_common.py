#!/usr/bin/env python3
"""Shared helpers for the Pinellas city adapters (Clearwater, Pinellas Park, Tarpon Springs,
City of Seminole). Not an adapter itself (no fetch()), so the pipeline skips it.

  * http GET with retries, text cleanup, PDF text
  * Pinellas County parcel lookup by site address (PA ParcelPropertyInfo layer on ArcGIS Online)
  * Pinellas parcel numbers in free text ("33-30-16-40913-000-0020")
  * CivicClerk public API: events in a window, agenda items, file streams, minutes text
  * minutes outcome parsing and a common status rule
"""
import datetime as dt
import html as _html
import io
import re
import sys
import time

import requests

try:
    import pypdf
except ImportError:  # pragma: no cover
    pypdf = None

UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
PARCELS = ("https://services.arcgis.com/f5HgUpxURgEzTccH/arcgis/rest/services/"
           "Pinellas_ParcelPropertyInfo/FeatureServer/0/query")
PIN_RE = re.compile(r"\b\d{2}-\d{2}-\d{2}-\d{5}-\d{3}-\d{4}\b")

_s = requests.Session()
_s.headers.update({"User-Agent": UA})
_TAG = "pinellas"


def set_tag(tag):
    global _TAG
    _TAG = tag


def log(*a):
    print(f"[{_TAG}]", *a, file=sys.stderr)


def get(url, params=None, tries=4, sleep=0.35, timeout=90, **kw):
    last = None
    for i in range(tries):
        try:
            r = _s.get(url, params=params, timeout=timeout, **kw)
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
    return sq(_html.unescape(s).replace(" ", " ").replace("​", ""))


def pdf_text(content, max_pages=60):
    if pypdf is None:
        return ""
    try:
        rdr = pypdf.PdfReader(io.BytesIO(content))
    except Exception:  # noqa: BLE001
        return ""
    out = []
    for p in rdr.pages[:max_pages]:
        try:
            out.append(p.extract_text() or "")
        except Exception:  # noqa: BLE001
            out.append("")
    return "\n".join(out)


def pins_in(text):
    return list(dict.fromkeys(PIN_RE.findall(text or "")))


# ------------------------------------------------------------------ parcels
_parcel_memo = {}


def parcel_lookup(address, city=None):
    """Site address -> {"pin", "acres", "lat", "lon", "address"} when exactly one parcel matches."""
    a = sq(address).upper()
    a = re.sub(r"^(?:PARCEL\s+)?(?:GENERALLY\s+)?LOCATED\s+AT\s+", "", a)
    a = re.sub(r"^(\d+)\s+(NORTH|SOUTH|EAST|WEST)\s+", lambda mm: f"{mm.group(1)} {mm.group(2)[0]} ", a)
    m = re.match(r"(\d+)\s+(?:([NSEW])\.?\s+)?(\d+[A-Z]{0,2}|[A-Z][A-Z']+)", a)
    if not m:
        return None
    num, name = m.group(1), m.group(3)
    key = (num, name, city)
    if key in _parcel_memo:
        return _parcel_memo[key]
    where = f"STR_NUM = {int(num)} AND STR_NAME LIKE '{name.replace(chr(39), chr(39) * 2)}%'"
    if city:
        where += f" AND STR_CITY LIKE '{city.upper()}%'"
    res = None
    try:
        d = get(PARCELS, params={"where": where, "outFields": "PARCELID_DSP1,SITE_ADDRESS,ACREAGE,STR_CITY",
                                 "returnCentroid": "true", "returnGeometry": "false", "outSR": 4326,
                                 "f": "json"}).json()
        feats = d.get("features") or []
        if m.group(2):  # prefer the parcel whose address has the same direction prefix
            pre = [f for f in feats if re.match(rf"\d+\s+{m.group(2)}\b", (f["attributes"].get("SITE_ADDRESS") or "").upper())]
            feats = pre or feats
        if len(feats) == 1:
            at, c = feats[0]["attributes"], feats[0].get("centroid") or {}
            res = {"pin": at.get("PARCELID_DSP1"), "acres": at.get("ACREAGE"), "lat": c.get("y"),
                   "lon": c.get("x"), "address": sq(at.get("SITE_ADDRESS"))}
    except Exception as e:  # noqa: BLE001
        log("parcel lookup failed", address, e)
    _parcel_memo[key] = res
    return res


def valid_pins(pins):
    """Keep only parcel numbers that exist in the Pinellas PA layer (PDF text can carry stale or
    misread numbers). If the check itself fails, return the list unchanged."""
    pins = [p for p in dict.fromkeys(pins or []) if p]
    if not pins:
        return []
    try:
        lits = ",".join("'" + p + "'" for p in pins[:50])
        d = get(PARCELS, params={"where": f"PARCELID_DSP1 IN ({lits})", "outFields": "PARCELID_DSP1",
                                 "returnGeometry": "false", "f": "json"}).json()
        ok = {f["attributes"].get("PARCELID_DSP1") for f in d.get("features") or []}
        return [p for p in pins if p in ok]
    except Exception as e:  # noqa: BLE001
        log("parcel check failed", e)
        return pins


# ------------------------------------------------------------------ CivicClerk
class CivicClerk:
    def __init__(self, tenant):
        self.api = f"https://{tenant}.api.civicclerk.com/v1"
        self.portal = f"https://{tenant}.portal.civicclerk.com"
        self._minutes = {}

    def events(self, start, end):
        url, params, out = f"{self.api}/Events", {
            "$filter": f"startDateTime ge {start}T00:00:00Z and startDateTime lt {end}T00:00:00Z",
            "$orderby": "startDateTime"}, []
        while url:
            d = get(url, params).json()
            out.extend(d.get("value", []))
            url, params = d.get("@odata.nextLink"), None
        return out

    def items(self, agenda_id):
        mt = get(f"{self.api}/Meetings/{agenda_id}").json()

        def walk(items, parent=None):
            for it in items or []:
                yield it, parent
                yield from walk(it.get("childItems"), it)
        return list(walk(mt.get("items")))

    def file_url(self, fid):
        return f"{self.api}/Meetings/GetMeetingFileStream(fileId={fid},plainText=false)"

    def event_url(self, ev_id):
        return f"{self.portal}/event/{ev_id}/files"

    def text_of_file(self, fid):
        if fid not in self._minutes:
            try:
                self._minutes[fid] = pdf_text(get(self.file_url(fid)).content)
            except Exception as e:  # noqa: BLE001
                log(f"file {fid}: {e}")
                self._minutes[fid] = ""
        return self._minutes[fid]

    def text_of_url(self, url):
        if url not in self._minutes:
            try:
                self._minutes[url] = pdf_text(get(url).content)
            except Exception as e:  # noqa: BLE001
                log(f"attachment {url[:80]}: {e}")
                self._minutes[url] = ""
        return self._minutes[url]


MIN_RE = re.compile(r"minutes\s+(?:of|from|for)?\s*(?:the\s+)?(?:[A-Za-z ]+?\s+)?(?:meeting\s+(?:of|on)\s+)?"
                    r"((?:January|February|March|April|May|June|July|August|September|October|November|December)"
                    r"\s+\d{1,2},?\s+\d{4})", re.I)


def minutes_attachment(it):
    """For an 'approve the minutes of <date>' agenda item -> (meeting date ISO, attachment url) or None."""
    n = item_name(it) + " " + item_desc(it)
    m = MIN_RE.search(n)
    if not m:
        return None
    try:
        d = dt.datetime.strptime(re.sub(r",", "", m.group(1)), "%B %d %Y").date().isoformat()
    except ValueError:
        return None
    for a in it.get("attachmentsList") or []:
        url = a.get("pdfVersionFullPath") or a.get("fullPath")
        if url and (a.get("fileSize") or 0) < 15_000_000:
            return d, url, n
    return None


def implied_first_readings(hearings, council_prefix="City Council"):
    """A council first reading with no recorded result, followed by a later council reading of the same
    case, passed (the item could not have advanced otherwise)."""
    cc = [h for h in hearings if h["body"].startswith(council_prefix)]
    for i, h in enumerate(cc):
        if h.get("result") is None and h.get("reading") == "first" and any(x["date"] > h["date"] for x in cc[i + 1:]):
            h["result"] = "Approved"
    return hearings


def item_name(it):
    return strip_html(it.get("agendaObjectItemName"))


def item_desc(it):
    return strip_html(it.get("agendaObjectItemDescription"))


# ------------------------------------------------------------------ outcomes
def _ns(s):
    return re.sub(r"\s+", "", s or "").lower()


def outcome_near(text, keys, window=4000, gap=900, stop=None):
    """Find the first key (plain text, matched ignoring whitespace and case) in minutes text, then
    the first motion result after it. PDF text often loses its spaces ("toPASS"), so everything is
    compared with whitespace removed. -> 'Approved' | 'Denied' | 'Continued' | 'Withdrawn' | None"""
    if not text:
        return None
    t = _ns(text)
    pos = -1
    for k in keys:
        kk = _ns(k)
        if kk and kk in t:
            pos = t.index(kk) + len(kk)
            break
    if pos < 0:
        return None
    seg = t[pos:pos + window]
    me = re.search(r"\d{4}[-.]\d+", _ns(keys[0]) if keys else "")
    for nxt in re.finditer(r"ordinanceno\.?(\d{4}[-.]\d+)", seg[200:]):  # stop at a different ordinance
        if not me or nxt.group(1).replace(".", "-") != me.group(0).replace(".", "-"):
            seg = seg[:200 + nxt.start()]
            break
    if stop:
        sm = re.search(stop, seg[300:])
        if sm:
            seg = seg[:300 + sm.start()]
    if re.search(r"withdr[ae]wn|withdrew", seg[:800]):
        return "Withdrawn"
    mm = re.search(r"(motion|moved).{0,400}?to(pass|approve|adopt|recommendapproval|recommenddenial|deny|continue|table|"
                   r"postpone|defer).{0,%d}?(motioncarried|carried|passed|approved|motionfailed|failed|unanimous)" % gap, seg)
    if not mm:
        return None
    if "failed" in mm.group(3):
        return None
    v = mm.group(2)
    if v in ("continue", "table", "postpone", "defer"):
        return "Continued"
    if v in ("deny", "recommenddenial"):
        return "Denied"
    return "Approved"


def status_from_hearings(hearings, today, final_body="City Council", final_words=("second", "final", "third", "adopt")):
    """Common status rule. hearings: [{"body","date","result","reading"}] sorted by date.
    Final decision = Approved/Denied at final_body on a second/final reading (or with no reading info)."""
    past = [h for h in hearings if h["date"] <= today]
    future = [h for h in hearings if h["date"] > today]
    wd = [h for h in past if h.get("result") == "Withdrawn"]
    if wd:
        return "Withdrawn", f"{wd[-1]['body']} {wd[-1]['date']}: withdrawn", None
    fin = [h for h in past if h["body"].startswith(final_body) and h.get("result") in ("Approved", "Denied")
           and (h.get("reading") is None or any(w in (h.get("reading") or "") for w in final_words))]
    den = [h for h in past if h["body"].startswith(final_body) and h.get("result") == "Denied"]
    if den:
        return "Denied", f"{den[-1]['body']} {den[-1]['date']}: denied", den[-1]["date"]
    if fin:
        return fin[-1]["result"], f"{fin[-1]['body']} {fin[-1]['date']}: {fin[-1]['result']}", fin[-1]["date"]
    if future:
        raw = f"next {future[0]['body']} {future[0]['date']}"
        if past and past[-1].get("result"):
            raw = f"{past[-1]['body']} {past[-1]['date']}: {past[-1]['result']}; " + raw
        return "Hearing Scheduled", raw, None
    if past and past[-1].get("result") == "Continued":
        return "Continued", f"{past[-1]['body']} {past[-1]['date']}: continued", None
    if past:
        lp = past[-1]
        if lp.get("result"):
            return "Filed", f"{lp['body']} {lp['date']}: {lp['result']}; next hearing not yet scheduled", None
        return "Hearing Scheduled", f"{lp['body']} {lp['date']} held; outcome not yet posted", None
    return "Filed", "On agenda", None


def reading_of(text):
    t = (text or "").lower()
    if re.search(r"\b(second|2nd)\s*(and final\s*)?(reading|rdg)|final reading", t):
        return "second"
    if re.search(r"\bthird|3rd\s*(reading|rdg)", t):
        return "third"
    if re.search(r"\b(first|1st)\s*(reading|rdg)", t):
        return "first"
    return None


def iso_today():
    return dt.date.today().isoformat()
