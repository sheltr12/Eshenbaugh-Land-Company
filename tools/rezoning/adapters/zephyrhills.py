#!/usr/bin/env python3
"""City of Zephyrhills (Pasco County) rezoning / PUD / future land use map amendment adapter.

Source: CivicClerk public API (zephyrhillsfl.api.civicclerk.com), portal zephyrhillsfl.portal.civicclerk.com.
 * A petition first shows up on the Site Plan Review Committee (SPRC) agenda, then the Planning
   Commission, then City Council (first and second reading of an ordinance). The city writes the
   petition number many ways ("RZ0018-25", "RZ 17-25", "PUD000014-22", "PUD-0016-22", "ANX 21-23",
   "FLU0005-24"); app_num is normalized to "<PREFIX> <n>-<yy>" ("RZ 18-25", "PUD 14-22") so all
   spellings of one petition land on one record. Kept: RZ, PUD, FLU / FLUM / FLUMA / CPA / LSCPA /
   SSCPA, and ANX petitions that include a rezoning or future land use change. Skipped: CU
   (conditional use), SP site plans, variances, text amendments (no petition number).
 * Council items read "Advent Health Rezoning First Reading Ordinance No. 1508-25 ...". They are tied
   to the petition by the petition number in the item text, else by Pasco parcel numbers shared with
   an SPRC / Planning Commission item; otherwise the ordinance stands alone ("Ordinance No. 1508-25").
 * Details come from item text (parcels, acreage, from/to) and page 1-2 of an attached staff report
   ("Applicant | Owner:", "Parcel ID Number(s):", "Acreage:", "Existing / Proposed Zoning:").
 * Outcomes: CivicClerk minutes votes on the item ("approve Ordinance No. 1500-25 on the Second
   Reading", pass/fail).
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

JURISDICTIONS = ["City of Zephyrhills"]
JUR = "City of Zephyrhills"
COUNTY = "Pasco"
TENANT = "zephyrhillsfl"
API = f"https://{TENANT}.api.civicclerk.com/v1"
PORTAL = f"https://{TENANT}.portal.civicclerk.com"
SOURCE = "Zephyrhills CivicClerk (SPRC, Planning Commission and City Council agendas, staff reports, votes)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
LOOKBACK_DAYS = 150
SMALL_PDF = 1_500_000

SPRC, PC, CC = "Site Plan Review Committee", "Planning Commission", "City Council"
SKIP_EVENT = re.compile(r"Workshop|Budget|Youth|Cancel|Joint|Visioning", re.I)

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


def log(*a):
    print("[zephyrhills]", *a, file=sys.stderr)


def get(url, params=None, tries=4, sleep=0.35, **kw):
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
    s = _html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    s = s.replace("\u00a0", " ").replace("\u200b", "").replace("\u2013", "-").replace("\u2014", "-")
    return sq(s)


class RangeFile(io.RawIOBase):
    """Seekable read-only file over HTTP range requests (pypdf reads a few pages of a big PDF)."""

    def __init__(self, url, size, chunk=1024 * 1024):
        self.url, self.size, self.chunk = url, size, chunk
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
            piece = self._blk(i)[self.pos - i * self.chunk: self.pos - i * self.chunk + n]
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


_pdf_cache = {}


def att_text(att, max_pages=2):
    url = att.get("pdfVersionFullPath")
    if not url or pypdf is None:
        return ""
    key = (att.get("id"), max_pages)
    if key in _pdf_cache:
        return _pdf_cache[key]
    txt = ""
    try:
        size = att.get("fileSize") or 0
        if size > SMALL_PDF:
            head = _s.head(url, timeout=60)
            size = int(head.headers.get("Content-Length") or 0) or size
            rdr = pypdf.PdfReader(RangeFile(url, size))
        else:
            rdr = pypdf.PdfReader(io.BytesIO(get(url).content))
        txt = "\n".join((p.extract_text() or "") for p in rdr.pages[:max_pages])
    except Exception as e:  # noqa: BLE001
        log(f"attachment {att.get('fileName')}: {e}")
    _pdf_cache[key] = txt
    return txt


# ------------------------------------------------------------------ CivicClerk
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


def body_of(ev):
    cat, name = ev.get("categoryName") or "", ev.get("eventName") or ""
    if SKIP_EVENT.search(name):
        return None
    txt = cat + " " + name
    if re.search(r"Site Plan Review", txt, re.I):
        return SPRC
    if re.search(r"Planning Commission", txt, re.I):
        return PC
    if re.search(r"City Council|Council Meeting", txt, re.I) or (cat == "City Council"):
        return CC
    return None


# ------------------------------------------------------------------ classification
CASE_RE = re.compile(r"\b(RZ|PUD|FLUMA|FLUM|FLU|LSCPA|SSCPA|CPA|ANX)\s?-?\s?0*(\d{1,6})\s?-\s?(\d{2})\b")
ORD_RE = re.compile(r"\bOrdinance\s*(?:No\.?\s*)?(\d{3,4})\s*-\s*(\d{2})\b", re.I)
PIN_RE = re.compile(r"\b(\d{2})-(\d{2})-(\d{2})-([0-9A-Z]{4})-([0-9A-Z]{5})-([0-9A-Z]{4})\b")
RZ_TXT = re.compile(r"Rezon|zoning map|zoning designation|\bPUD\b|Planned Unit Development", re.I)
FLU_TXT = re.compile(r"Future Land Use(?! Element)|\bFLUM?A?\b|land use map|Comprehensive Plan Amendment|Comp Plan Amendment", re.I)
MAP_TXT = re.compile(r"FUTURE LAND USE MAP|LAND USE MAP AMENDMENT|\bFLUMA?\b|SMALL-SCALE|LARGE-SCALE", re.I)
TEXT_TXT = re.compile(r"TEXT AMENDMENT|ELEMENT|Evaluation and Appraisal|Classification|Subarea", re.I)
OUT_TXT = re.compile(r"Land Development Code|\bLDC\b|Moratorium|Text Amendment|Millage|Budget|Rates", re.I)


def case_key(m):
    pre = m.group(1).upper()
    if pre in ("FLUM", "FLUMA"):
        pre = "FLU"
    return f"{pre} {int(m.group(2))}-{m.group(3)}"


def kind_of(pre, text):
    """'RZ' (rezoning / PUD) or 'CPA' or None."""
    if pre in ("RZ", "PUD"):
        return "RZ"
    if pre in ("FLU", "CPA", "LSCPA", "SSCPA"):
        return "RZ" if RZ_TXT.search(text) else "CPA"
    if pre == "ANX":
        if RZ_TXT.search(text):
            return "RZ"
        if FLU_TXT.search(text):
            return "CPA"
    return None


def pins_in(text):
    t = re.sub(r"(\d)\s*-\s*\n?\s*([0-9A-Z])", r"\1-\2", text or "")
    return list(dict.fromkeys("-".join(g) for g in PIN_RE.findall(t)))


def reading_of(text):
    t = (text or "").lower()
    if re.search(r"\b(second|2nd)\b[^.;)]{0,20}reading|final reading|adoption", t):
        return "second"
    if re.search(r"\btransmittal\b", t):
        return "transmittal"
    if re.search(r"\b(first|1st)\b[^.;)]{0,20}reading", t):
        return "first"
    return None


def vote_result(item):
    res = None
    for v in item.get("minutesItemVotes") or []:
        name = (v.get("motionName") or "").lower()
        if re.search(r"extend|agenda|minutes|recess|adjourn|reconsider|consent", name):
            continue
        passed = v.get("passFail") == 1
        if re.search(r"withdr", name):
            r = "Withdrawn"
        elif re.search(r"continu|table|postpone|defer", name):
            r = "Continued"
        elif re.search(r"\bden(y|ial)", name):
            r = "Denied" if passed else None
        elif passed:
            r = "Approved"
        else:
            r = "Denied" if re.search(r"approv|adopt", name) else None
        if r:
            res = r
    return res


_minutes = {}


def minutes_text(fid):
    if fid not in _minutes:
        try:
            r = get(f"{API}/Meetings/GetMeetingFileStream(fileId={fid},plainText=false)")
            _minutes[fid] = "\n".join((p.extract_text() or "") for p in pypdf.PdfReader(io.BytesIO(r.content)).pages)
        except Exception as e:  # noqa: BLE001
            log(f"minutes {fid}: {e}")
            _minutes[fid] = ""
    return _minutes[fid]


def key_pattern(key):
    m = re.match(r"([A-Z]+) (\d+)-(\d{2})$", key)
    if m:
        return rf"{m.group(1)}\s?-?\s?0*{m.group(2)}\s?-\s?{m.group(3)}\b"
    m = re.search(r"(\d{3,4})-(\d{2})$", key)
    return rf"{m.group(1)}\s?-\s?{m.group(2)}\b" if m else None


def minutes_result(text, pats):
    """Motion outcome for the first of `pats` (regexes) found in minutes text."""
    t = re.sub(r"[ \t]+", " ", text or "")
    for p in filter(None, pats):
        for m in re.finditer(p, t, re.I):
            seg = t[m.end(): m.end() + 3500]
            nxt = re.search(r"\n\s*\d{1,2}\.\d{1,2}\s+[A-Z]", seg[50:])
            if nxt:
                seg = seg[:50 + nxt.start()]
            mm = re.search(r"(?:motion(?:ed)?|moved)\s+(?:was\s+made\s+)?to\s+(approve|adopt|recommend\s+approval|"
                           r"recommend\s+denial|deny|table|continue|postpone|defer)(.{0,400}?)Motion\s+(passed|carried|failed|died)",
                           seg, re.I | re.S)
            if not mm:
                if re.search(r"\bwithdr[ae]w", seg[:1500], re.I):
                    return "Withdrawn"
                m2 = re.search(r"(?:Recommendation\s*:\s*)?Motion(?:ed)?\s+(?:for\s+)?(approval|denial|to\s+table|to\s+continue)\s+"
                               r"(?:by|was)\s+(?:all|unanimous|the)", seg, re.I)
                if m2:
                    w = m2.group(1).lower()
                    return "Approved" if w == "approval" else ("Denied" if w == "denial" else "Continued")
                continue
            if mm.group(3).lower() in ("failed", "died"):
                v = mm.group(1).lower()
                return "Denied" if v in ("approve", "adopt") else None
            v = mm.group(1).lower()
            if v in ("table", "continue", "postpone", "defer"):
                return "Continued"
            if "denial" in v or v == "deny":
                return "Denied"
            return "Approved"
    return None


# ------------------------------------------------------------------ text parsing
def label(text, *names, stop=r"\n"):
    for n in names:
        m = re.search(rf"{n}\s*:\s*(.+?)(?:{stop}|$)", text or "", re.I)
        if m and sq(m.group(1)):
            return sq(m.group(1)).strip(" .;")
    return None


def parse_acres(text):
    t = sq(text)
    for pat in (r"([\d,]*\.?\d+)\s*\(?\s*(?:\+/-|±|MOL)?\s*\)?\s*acres", r"\(([\d,]*\.?\d+)\s*ACRES\)"):
        m = re.search(pat, t, re.I)
        if m:
            try:
                v = float(m.group(1).replace(",", ""))
                if 0 < v < 50000:
                    return v
            except ValueError:
                pass
    return None


def from_to(text, kind):
    t = sq(text)
    if kind == "CPA":
        pats = [r"FUTURE LAND USE(?: MAP)?(?: DESIGNATION)?(?: CHANGING)? FROM (.+?) TO (.+?)(?: AND THE ZONING| FOR | ON |;|\.\s|,|$)",
                r"(?:change|changing) (?:in )?FLU(?:M)? (?:\([^)]*\) )?from (.+?) to (.+?)(?: and | for |;|\.|,|$)",
                r"FLUMA? from (.+?) to (.+?)(?: and | for |;|\.|$)"]
    else:
        pats = [r"ZONING DESIGNATION FROM (.+?) TO (.+?)(?: FOR | ON |;|\.\s|$)",
                r"(?:REZONING|rezone|zoning)\b[^;]{0,120}? FROM (.+?) TO (.+?)(?:,? LOCATED| FOR | ON |;|\.\s|\. |$)",
                r"Rezon\w* from (.+?) to (.+?)(?: and | for |;|\.|$)"]
    for p in pats:
        m = re.search(p, t, re.I)
        if m:
            f_, t_ = sq(m.group(1)).strip(" ,.;"), sq(m.group(2)).strip(" ,.;")
            if 0 < len(f_) < 150 and 0 < len(t_) < 150:
                return f_, t_
    return None, None


def project_name(name):
    n = CASE_RE.sub(" ", name, count=1)
    n = re.split(r"\s*[:;]\s|\s-\s|\bFIRST READING|\bSECOND READING|\bOrdinance\b", n, flags=re.I)[0]
    n = re.sub(r"\((.*?)\)", r"\1", n)
    n = re.sub(r"\b(Annexation|Rezoning|Rezone|Future Land Use|FLU|FLUMA|Comp Plan Amendment to|and|&|,|Map Amendment|"
               r"Property|Planning Report)\b", " ", n, flags=re.I)
    n = sq(n).strip(" -:,&")
    return n if 2 < len(n) < 80 else None


# ------------------------------------------------------------------ fetch
def fetch(since, refresh):
    if pypdf is None:
        raise RuntimeError("pypdf is required for the Zephyrhills adapter")
    today = dt.date.today()
    refresh_ids = {}
    for r in refresh or []:
        m = CASE_RE.search((r or "").upper())
        if m:
            refresh_ids[case_key(m)] = r
        elif ORD_RE.search(r or ""):
            om = ORD_RE.search(r)
            refresh_ids[f"Ordinance No. {om.group(1)}-{om.group(2)}"] = r
    start_d = dt.date.fromisoformat(since) - dt.timedelta(days=LOOKBACK_DAYS)
    if refresh_ids:
        yrs = [2000 + int(k[-2:]) for k in refresh_ids]
        start_d = min(start_d, max(dt.date(min(yrs), 1, 1), today - dt.timedelta(days=730)))
    end_d = today + dt.timedelta(days=75)
    try:
        events = list_events(start_d.isoformat(), end_d.isoformat())
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Zephyrhills CivicClerk unreachable: {e}")
    log(f"{len(events)} events {start_d}..{end_d}")

    cases, ords = {}, []
    for ev in events:
        body = body_of(ev)
        if not body or not ev.get("agendaId"):
            continue
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
        minutes = next((f.get("fileId") for f in files if f.get("type") == "Minutes"), None)
        for it in walk(mt.get("items")):
            name = strip_html(it.get("agendaObjectItemName"))
            desc = strip_html(it.get("agendaObjectItemDescription"))
            txt = name + " " + desc
            if not name or OUT_TXT.search(name):
                continue
            app = {"date": mdate, "first_seen": first_seen, "body": body, "name": name, "desc": desc, "item": it,
                   "event_id": ev.get("id"), "packet": packet, "minutes": minutes}
            cm = CASE_RE.search(txt)
            om = ORD_RE.search(txt)
            if body == CC and om:
                kind = None
                if RZ_TXT.search(name):
                    kind = "RZ"
                elif MAP_TXT.search(name) and not TEXT_TXT.search(name):
                    kind = "CPA"
                if not kind or (re.search(r"\bANNEX", name, re.I) and not re.search(r"ZONING|LAND USE", name, re.I)):
                    continue
                app["ono"] = f"{om.group(1)}-{om.group(2)}"
                app["kind"] = kind
                app["case"] = case_key(cm) if cm else None
                ords.append(app)
                continue
            if not cm:
                continue
            kind = kind_of(case_key(cm).split()[0], txt)
            if not kind:
                continue
            c = cases.setdefault(case_key(cm), {"kind": kind, "apps": []})
            if kind == "RZ":
                c["kind"] = "RZ"
            c["apps"].append(app)
    log(f"{len(cases)} petitions, {len(ords)} Council ordinance items")

    ord_case = {}
    for a in ords:
        ono = a["ono"]
        key = ord_case.get(ono) or a.get("case")
        if not key:
            pins = set(pins_in(a["name"] + " " + a["desc"]))
            if pins:
                for k, c in cases.items():
                    if pins & set(pins_in(" ".join(x["name"] + " " + x["desc"] for x in c["apps"]))):
                        key = k
                        break
        if not key:   # staff report or ordinance PDF may carry the petition number / parcels
            for att in a["item"].get("attachmentsList") or []:
                t = att_text(att, 1)
                m = CASE_RE.search(t[:3000])
                if m:
                    key = case_key(m)
                    break
                pins = set(pins_in(t))
                for k, c in cases.items():
                    if pins and pins & set(pins_in(" ".join(x["name"] + " " + x["desc"] for x in c["apps"]))):
                        key = k
                        break
                if key:
                    break
        key = key or f"Ordinance No. {ono}"
        ord_case[ono] = key
        c = cases.setdefault(key, {"kind": a["kind"], "apps": []})
        if a["kind"] == "RZ":
            c["kind"] = "RZ"
        c["apps"].append(a)
        c.setdefault("onos", set()).add(ono)
    if ord_case:
        log("ordinance -> petition: " + ", ".join(f"{k}={v}" for k, v in sorted(ord_case.items())))

    out = []
    for key, c in sorted(cases.items()):
        apps = sorted(c["apps"], key=lambda x: (x["date"], [SPRC, PC, CC].index(x["body"])))
        first = min(x["first_seen"] for x in apps)
        older = apps[0]["body"] == CC and reading_of(apps[0]["name"]) == "second" and \
            apps[0]["date"] < (start_d + dt.timedelta(days=45)).isoformat()
        if (first < since or older) and key not in refresh_ids:
            continue
        try:
            out.append(build(key, c, apps, today.isoformat()))
        except Exception as e:  # noqa: BLE001
            log(f"{key}: skipped ({e})")
    for r in set(refresh_ids) - set(cases):
        log(f"refresh id {refresh_ids[r]} not found on any agenda since {start_d}")
    return out


def build(key, c, apps, today):
    kind = c["kind"]
    alltxt = " ".join(a["name"] + " " + a["desc"] for a in apps)
    staff_txt = ""
    for a in reversed(apps):
        for att in a["item"].get("attachmentsList") or []:
            if re.search(r"Staff Report|Planning Report", att.get("fileName") or "", re.I):
                staff_txt = att_text(att, 2)
                if staff_txt:
                    break
        if staff_txt:
            break
    pins = pins_in(alltxt) or pins_in(staff_txt)
    acres = parse_acres(label(staff_txt, r"Acreage", r"Total Acreage") or "") or parse_acres(alltxt) or parse_acres(staff_txt)
    f_z, t_z, f_f, t_f = None, None, None, None
    for txt in [a["name"] for a in apps if a["body"] == CC] + [a["desc"] for a in apps] + [a["name"] for a in apps]:
        if not f_z:
            f_z, t_z = from_to(txt, "RZ")
        if not f_f:
            f_f, t_f = from_to(txt, "CPA")
    f_z = f_z or label(staff_txt, r"Existing Zoning", r"Current Zoning")
    t_z = t_z or label(staff_txt, r"Proposed Zoning")
    f_f = f_f or label(staff_txt, r"Existing Future Land Use", r"Current Future Land Use")
    t_f = t_f or label(staff_txt, r"Proposed Future Land Use")
    applicant = label(staff_txt, r"Applicant\s*\|\s*Owner", r"Applicant", r"Owner")
    if not applicant:
        m = re.search(r"submitted by (.+?) for ", staff_txt, re.I) or re.search(r"presented to the City by (.+?)[.,] ", staff_txt)
        applicant = sq(m.group(1)) if m else None
    address = label(staff_txt, r"Location")
    if not address:
        m = re.search(r"LOCATED (?:AT |ON |ALONG )?(.+?)(?:,? (?:BEING|HAVING|AND HAVING)|;|\.\s|$)", alltxt, re.I)
        address = sq(m.group(1))[:150] if m else None
    pname = next((project_name(a["name"]) for a in apps if a["body"] != CC and project_name(a["name"])), None) or \
        next((project_name(a["name"]) for a in apps if project_name(a["name"])), None)
    is_pd = kind == "RZ" and (key.startswith("PUD") or bool(re.search(r"\bPUD\b|Planned Unit", t_z or "", re.I)))
    category = "Comp Plan Amendment" if kind == "CPA" else ("PD" if is_pd else "Rezoning")
    pre = key.split()[0]
    app_type = {"RZ": "Rezoning", "PUD": "PUD Rezoning", "ANX": "Annexation with rezoning / future land use",
                "FLU": "Future Land Use Map Amendment", "CPA": "Comprehensive Plan Amendment",
                "LSCPA": "Large-Scale Comprehensive Plan Amendment", "SSCPA": "Small-Scale Comprehensive Plan Amendment",
                "Ordinance": "Ordinance (zoning map / future land use map)"}.get(pre, pre)
    desc = next((a["desc"] for a in apps if a["desc"] and len(a["desc"]) > 20), None) or \
        next((a["name"] for a in apps if a["body"] != CC), apps[0]["name"])
    desc = sq(desc)
    if len(desc) > 300:
        desc = desc[:297] + "..."
    onos = sorted(c.get("onos") or [])
    if onos and not key.startswith("Ordinance"):
        desc = f"{desc} (Ordinance No. {', '.join(onos)})"

    hearings = []
    for a in apps:
        res = vote_result(a["item"])
        rd = reading_of(a["name"]) if a["body"] == CC else None
        if res is None and re.search(r"\bwithdrawn\b", a["name"] + " " + a["desc"], re.I):
            res = "Withdrawn"
        if res is None and a.get("minutes") and a["body"] != SPRC and a["date"] <= today:
            pats = [key_pattern(key)] + [key_pattern(f"x {o}") for o in onos]
            if a.get("ono"):
                pats.insert(0, key_pattern(f"x {a['ono']}"))
            res = minutes_result(minutes_text(a["minutes"]), pats)
        body = a["body"] if not rd else f"City Council ({rd} reading)"
        if a["body"] != CC and res in ("Approved", "Denied"):
            res = "Recommended " + ("Approval" if res == "Approved" else "Denial")
        h = {"body": body, "date": a["date"], "result": res, "_rd": rd, "_body": a["body"]}
        prev = next((x for x in hearings if x["body"] == h["body"] and x["date"] == h["date"]), None)
        if prev:
            prev["result"] = prev["result"] or h["result"]
        else:
            hearings.append(h)
    hearings.sort(key=lambda h: (h["date"], [SPRC, PC, CC].index(h["_body"])))
    ccs = [h for h in hearings if h["_body"] == CC]
    rank = {"first": 1, "transmittal": 1, "second": 2, None: 0}
    for i, h in enumerate(ccs):
        later = [x for x in ccs[i + 1:] if x["date"] > h["date"]]
        if h["result"] is not None or not later or h["date"] > today:
            continue
        if h["_rd"] and any(x["_rd"] == h["_rd"] for x in later):
            h["result"] = "Continued"
        elif any(rank.get(x["_rd"], 0) > rank.get(h["_rd"], 0) for x in later):
            h["result"] = "Approved" if h["_rd"] in ("first", "transmittal") else None
    for h in hearings:
        if h["_body"] == CC and h["_rd"] in ("first", "transmittal") and h["result"] == "Approved":
            h["result"] = "Approved on first reading" if h["_rd"] == "first" else "Transmitted"

    status, raw, decision = status_from(hearings, today)
    latest = apps[-1]
    return {
        "jurisdiction": JUR, "county": COUNTY, "app_num": key, "category": category, "app_type": app_type,
        "project_name": pname, "description": desc or None, "applicant": applicant or None,
        "parcel_ids": pins, "address": address or None, "acres": acres,
        "from_zoning": f_z, "to_zoning": t_z, "from_flu": f_f, "to_flu": t_f,
        "filed_date": min(a["first_seen"] for a in apps), "status": status, "status_raw": raw,
        "hearings": [{"body": h["body"], "date": h["date"], "result": h["result"]} for h in hearings],
        "decision_date": decision, "url": f"{PORTAL}/event/{latest['event_id']}/files",
        "docs_url": (f"{API}/Meetings/GetMeetingFileStream(fileId={latest['packet']},plainText=false)"
                     if latest.get("packet") else None),
        "lat": None, "lon": None, "source": SOURCE,
    }


def status_from(hearings, today):
    past = [h for h in hearings if h["date"] <= today]
    future = [h for h in hearings if h["date"] > today]
    if any(h["result"] == "Withdrawn" for h in past):
        h = [x for x in past if x["result"] == "Withdrawn"][-1]
        return "Withdrawn", f"{h['body']} {h['date']}: withdrawn", None
    den = [h for h in past if h["_body"] == CC and h["result"] == "Denied"]
    if den:
        return "Denied", f"{den[-1]['body']} {den[-1]['date']}: denied", den[-1]["date"]
    fin = [h for h in past if h["_body"] == CC and h["result"] == "Approved" and h["_rd"] not in ("first", "transmittal")]
    if fin:
        return "Approved", f"{fin[-1]['body']} {fin[-1]['date']}: approved", fin[-1]["date"]
    if future:
        raw = f"next {future[0]['body']} {future[0]['date']}"
        if past and past[-1]["result"]:
            raw = f"{past[-1]['body']} {past[-1]['date']}: {past[-1]['result']}; " + raw
        return "Hearing Scheduled", raw, None
    if past and past[-1]["result"] == "Continued":
        return "Continued", f"{past[-1]['body']} {past[-1]['date']}: continued", None
    if past:
        lp = past[-1]
        if lp["result"]:
            return "Filed", f"{lp['body']} {lp['date']}: {lp['result']}; next hearing not yet on an agenda", None
        if lp["_body"] == SPRC:
            return "Filed", f"{lp['body']} {lp['date']} (staff review); public hearing not yet on an agenda", None
        return "Hearing Scheduled", f"{lp['body']} {lp['date']} held; outcome not yet posted", None
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
