#!/usr/bin/env python3
"""City of Clermont (Lake County) rezoning / PUD / future land use map amendment adapter.

Source: CivicClerk public API (clermontfl.api.civicclerk.com), the same platform as lake.py.
 * Clermont processes every land use case as a numbered ordinance ("Ordinance No. 2026-019
   Louisa Grande SSCPA", "Ordinance No. 2026-020 Louisa Grande Rezoning"). The ordinance number
   is the only case number the city publishes, so app_num = "Ordinance No. YYYY-NNN". A
   project usually has two ordinances (SSCPA/LSCPA + REZ), plus an annexation ordinance that is
   skipped; each in-scope ordinance becomes its own record (category CPA or Rezoning/PD).
 * A case first appears on a Planning and Zoning Commission agenda, then City Council
   "Intro" (first reading / LSCPA transmittal) and "Final" (adoption).
 * Details come from the item attachments: the "Staff Analysis" report (applicant, owner,
   size, location with Lake County Alternate Keys, existing zoning / FLU) and the ordinance PDF
   ("From: ... To: ..." and acreage). Only a few pages are read (HTTP range requests for big files).
 * City Council outcomes come from the "Summary of Actions" document CivicClerk publishes for
   each Council meeting ("Item No. 9 - Ordinance No. 2026-020 Final ... STATUS: APPROVED").
   Planning and Zoning Commission minutes are scanned images, so P&Z results are not available.
www.clermontfl.gov intermittently resets connections; it is not needed (all data is on
civicclerk). Requires pypdf.
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

JURISDICTIONS = ["City of Clermont"]
JUR = "City of Clermont"
TENANT = "clermontfl"
API = f"https://{TENANT}.api.civicclerk.com/v1"
PORTAL = f"https://{TENANT}.portal.civicclerk.com"
SOURCE = "Clermont CivicClerk (P&Z + City Council agendas, staff analyses, Summary of Actions)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
LOOKBACK_DAYS = 150   # scan meetings this far before `since` to know when a case first appeared
SMALL_PDF = 2_500_000  # download whole PDFs below this size, else range-read the pages we need

PZ_CAT = "Planning and Zoning Commission"
CC_CATS = {"City Council Meeting", "Special Meeting"}
SKIP_EVENT = re.compile(r"Workshop|Committee|Budget|Redevelopment|\bCRA\b|Retirement|Pension|Canceled|Cancelled", re.I)

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


def log(*a):
    print("[clermont]", *a, file=sys.stderr)


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


class RangeFile(io.RawIOBase):
    """Seekable read-only file over HTTP range requests (lets pypdf read a few pages of a big PDF)."""

    def __init__(self, url, size, chunk=256 * 1024):
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


_pdf_cache = {}


def pdf_text(att, max_pages=8):
    """Text of the first `max_pages` pages of a CivicClerk attachment (dict with pdfVersionFullPath)."""
    url = att.get("pdfVersionFullPath")
    if not url:
        return ""
    key = (att.get("id"), max_pages)
    if key in _pdf_cache:
        return _pdf_cache[key]
    size = att.get("fileSize") or 0
    if size and size > SMALL_PDF:
        rdr = pypdf.PdfReader(RangeFile(url, size))
    else:
        rdr = pypdf.PdfReader(io.BytesIO(get(url).content))
    out = []
    for p in rdr.pages[:max_pages]:
        try:
            out.append(p.extract_text() or "")
        except Exception:  # noqa: BLE001
            out.append("")
    txt = "\n".join(out)
    _pdf_cache[key] = txt
    return txt


def file_stream(file_id):
    return f"{API}/Meetings/GetMeetingFileStream(fileId={file_id},plainText=false)"


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


def walk_items(items):
    for it in items or []:
        yield it
        yield from walk_items(it.get("childItems"))


def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = s.replace("&nbsp;", " ").replace("\u00a0", " ").replace("&amp;", "&").replace("\u200b", "")
    return re.sub(r"\s+", " ", s).strip()


ORD_RE = re.compile(r"\bOrdinance\s*(?:No\.?)?\s*(\d{4})\s*-\s*(\d{3})\b", re.I)
CPA_RE = re.compile(r"\bS\.?S\.?C\.?P\.?A\b|\bL\.?S\.?C\.?P\.?A\b|Comprehensive Plan Amendment|"
                    r"Small[- ]scale|Large[- ]scale|future land use|land use change", re.I)
RZ_RE = re.compile(r"\bRezon|\bREZ\b|\bPUD\b|Planned Unit Development|zoning change|zoning district", re.I)
OUT_RE = re.compile(r"Annex|Land Development Code|\bLDC\b|Development District|\bCDD\b|Vacat|Impact Fee|"
                    r"Moratorium|Franchise|Golf Cart|Code Enforcement|Stormwater|E-?Bikes|Dwelling Units|"
                    r"Recovery Residences|Fee Schedule|2045 Comprehensive Plan|Comprehensive Plan\s*$", re.I)
READING_LABEL = {"Intro": "first reading", "Final": "final hearing", "Transmittal": "transmittal",
                 "Adoption": "adoption"}
READING_RE = re.compile(r"\b(Intro(?:duction)?|Final|Transmittal|Adoption)\b", re.I)


def classify_item(name, desc):
    """Return (ord_no, kind) for in-scope ordinance items, else None. kind: 'CPA' or 'RZ'."""
    m = ORD_RE.search(name)
    if not m:
        return None
    rest = name[m.end():]
    if OUT_RE.search(rest) and not re.search(r"SSCPA|LSCPA|Rezon|\bPUD\b", rest, re.I):
        return None
    txt = rest + " " + (desc or "")
    if CPA_RE.search(rest):
        kind = "CPA"
    elif RZ_RE.search(rest):
        kind = "RZ"
    elif CPA_RE.search(desc or ""):
        kind = "CPA"
    elif RZ_RE.search(desc or ""):
        kind = "RZ"
    else:
        return None
    if re.search(r"Land Development Code|text amendment", txt, re.I):
        return None
    return f"{m.group(1)}-{m.group(2)}", kind


def project_name(name):
    m = ORD_RE.search(name)
    rest = name[m.end():] if m else name
    rest = re.sub(r"\b(Intro(?:duction)?|Final|Transmittal|Adoption|Hearing|Tabled from [A-Za-z]+ \d+, \d{4}|Table)\b"
                  r"\s*/?\s*", " ", rest, flags=re.I)
    rest = re.sub(r"\b(Small[- ]scale|Large[- ]scale)\s+Comprehensive Plan Amendment|Comprehensive Plan Amendment|"
                  r"\bS?S?L?SCPA\b|\bLSCPA\b|\bSSCPA\b|\bRezon(?:ing|e)\b|\bREZ\b|\bPUD\b", " ", rest, flags=re.I)
    rest = re.sub(r"\s+", " ", rest).strip(" -/:,")
    return rest or None


# ------------------------------------------------------------------ document parsing
LABELS = ["OWNER", "APPLICANT", "PROJECT NAME", "REQUESTED ACTION", "SIZE OF PARCEL", "LOCATION",
          "EXISTING ZONING", "PROPOSED ZONING", "EXISTING LAND USE", "FUTURE LAND USE CATEGORY",
          "FUTURE LAND USE", "PROPOSED FUTURE LAND USE", "EXISTING USE", "STAFF RECOMMENDATION", "ANALYSIS",
          "STAFF ANALYSIS", "Adjacent Property"]
LAB_RE = re.compile(r"^\s*(" + "|".join(re.escape(x) for x in LABELS) + r")\s*:?", re.I | re.M)


def parse_staff(text):
    """Label/value pairs from page 1 of a Clermont Staff Analysis Report."""
    out = {}
    ms = list(LAB_RE.finditer(text))
    for i, m in enumerate(ms):
        lab = m.group(1).upper()
        end = ms[i + 1].start() if i + 1 < len(ms) else min(len(text), m.end() + 400)
        val = re.sub(r"\s+", " ", text[m.end():end]).strip(" :.;")
        if val and lab not in out:
            out[lab] = val
    return out


def alt_keys(text):
    keys = []
    for m in re.finditer(r"Alternate\s+Key", text, re.I):
        seg = re.split(r"[A-Za-z]{4,}", text[m.end(): m.end() + 120])[0]
        keys += re.findall(r"\b\d{7}\b", seg)
    return list(dict.fromkeys(keys))


def parse_acres(text):
    t = re.sub(r"\s+", " ", text or "")
    for pat in (r"OR\s+([\d,]+\.?\d*)\s*(?:\+/-\s*)?ACRES", r"([\d,]*\.?\d+)\s*\+\s*/\s*-\s*Acres",
                r"([\d,]*\.?\d+)\s*(?:\+/-)?\s*(?:gross\s+)?acres"):
        m = re.search(pat, t, re.I)
        if m:
            try:
                v = float(m.group(1).replace(",", ""))
                if 0 < v < 100000:
                    return v
            except ValueError:
                pass
    return None


def from_to(text):
    m = re.search(r"^\s*From:\s*(.+?)\s*\n\s*To:\s*(.+?)\s*$", text or "", re.I | re.M)
    if not m:
        return None, None
    return clean(m.group(1)), clean(m.group(2))


def clean(v):
    v = re.sub(r"\s+", " ", v or "").strip(" .,;:")
    return v or None


# ------------------------------------------------------------------ summary of actions
def parse_summary(text):
    """{ord_no: (status_word, reading)} from a Council 'Summary of Actions' PDF."""
    t = re.sub(r"[ \t]+", " ", text)
    res = {}
    parts = re.split(r"\n\s*Item No\.\s*\d+\s*-\s*", "\n" + t)
    for p in parts[1:]:
        m = ORD_RE.search(p[:120])
        if not m:
            continue
        ono = f"{m.group(1)}-{m.group(2)}"
        st = re.search(r"STATUS:\s*([^\n]+)", p)
        word = st.group(1).strip() if st else None
        if not word:
            w2 = re.search(r"\b(WITHDRAWN|TABLED|CONTINUED|POSTPONED|DENIED)\b", p)
            word = w2.group(1) if w2 else None
        if word:
            res[ono] = word
    return res


def norm_result(word, reading, body):
    if not word:
        return None
    w = word.upper()
    if "WITHDR" in w:
        return "Withdrawn"
    if "TRANSMIT" in w:
        return "Transmitted"
    if "INTRO" in w:
        return "Introduced (first reading)"
    if "DENI" in w or "FAIL" in w:
        return "Denied"
    if "TABLE" in w or "CONTINU" in w or "POSTPON" in w or "DEFER" in w:
        return "Continued"
    if "APPRO" in w or "APRO" in w or "ADOPT" in w or "PASS" in w or "TRANSMIT" in w:
        if body == "City Council" and reading in ("Intro", "Transmittal"):
            return "Approved (first reading)" if reading == "Intro" else "Transmitted"
        return "Approved"
    if "NO ACTION" in w:
        return None
    return word.title()


# ------------------------------------------------------------------ fetch
def fetch(since, refresh):
    if pypdf is None:
        raise RuntimeError("pypdf is required for the Clermont adapter")
    today = dt.date.today()
    refresh_ids = {}
    for r in refresh or []:
        m = re.search(r"(\d{4})\s*-\s*(\d{3})", r or "")
        if m:
            refresh_ids[f"{m.group(1)}-{m.group(2)}"] = r
    start_d = dt.date.fromisoformat(since) - dt.timedelta(days=LOOKBACK_DAYS)
    if refresh_ids:   # make sure older open ordinances are covered (ordinance numbers carry the year)
        yr = min(int(k[:4]) for k in refresh_ids)
        start_d = min(start_d, max(dt.date(yr - 1, 9, 1), today - dt.timedelta(days=730)))
    end_d = today + dt.timedelta(days=75)
    events = list_events(start_d.isoformat(), end_d.isoformat())
    log(f"{len(events)} events {start_d}..{end_d}")

    cases = {}
    n_meet = 0
    for ev in events:
        cat = ev.get("categoryName") or ""
        name = ev.get("eventName") or ""
        if not ev.get("agendaId") or SKIP_EVENT.search(name):
            continue
        if cat == PZ_CAT:
            body = "Planning and Zoning Commission"
        elif cat in CC_CATS:
            body = "City Council"
        else:
            continue
        try:
            mt = get(f"{API}/Meetings/{ev['agendaId']}").json()
        except Exception as e:  # noqa: BLE001
            log(f"meeting {ev['agendaId']}: {e}")
            continue
        n_meet += 1
        mdate = ev["startDateTime"][:10]
        files = ev.get("publishedFiles") or []
        pub = [f.get("publishOn") for f in files if f.get("type") == "Agenda" and f.get("publishOn")]
        first_seen = min([p[:10] for p in pub] + [mdate])
        packet = [f.get("fileId") for f in files if f.get("type") == "Agenda Packet"]
        summary = [f.get("fileId") for f in files if f.get("type") == "Summary of Actions"]
        items = list(walk_items(mt.get("items")))
        for it in items:
            iname = strip_html(it.get("agendaObjectItemName"))
            desc = strip_html(it.get("agendaObjectItemDescription"))
            c = classify_item(iname, desc)
            if not c:
                continue
            ono, kind = c
            rm = READING_RE.search(iname[ORD_RE.search(iname).end():])
            reading = None
            if body == "City Council":
                reading = (rm.group(1).title() if rm else None)
                if reading and reading.startswith("Intro"):
                    reading = "Intro"
                if re.search(r"Transmittal", iname, re.I):
                    reading = "Transmittal"
            rec = cases.setdefault(ono, {"kind": kind, "apps": []})
            rec["apps"].append({
                "date": mdate, "first_seen": first_seen, "body": body, "reading": reading,
                "name": iname, "desc": desc, "atts": it.get("attachmentsList") or [],
                "event_id": ev.get("id"), "packet": packet[0] if packet else None,
                "summary": summary[0] if summary else None,
                "sibling_atts": [a for o in items if o is not it
                                 and project_name(strip_html(o.get("agendaObjectItemName"))) == project_name(iname)
                                 for a in (o.get("attachmentsList") or [])],
            })
    log(f"{n_meet} meetings read; {len(cases)} in-scope ordinances")

    # Council outcomes from Summary of Actions
    outcomes = {}
    for sid in sorted({a["summary"] for c in cases.values() for a in c["apps"] if a["summary"]}):
        try:
            r = get(file_stream(sid))
            txt = "\n".join((p.extract_text() or "") for p in pypdf.PdfReader(io.BytesIO(r.content)).pages)
            outcomes[sid] = parse_summary(txt)
        except Exception as e:  # noqa: BLE001
            log(f"summary {sid}: {e}")

    out = []
    for ono, c in sorted(cases.items()):
        first = min(a["first_seen"] for a in c["apps"])
        if first < since and ono not in refresh_ids:
            continue
        try:
            out.append(build(ono, c, outcomes, today.isoformat()))
        except Exception as e:  # noqa: BLE001
            log(f"{ono}: skipped ({e})")
    for r in set(refresh_ids) - set(cases):
        log(f"refresh id {refresh_ids[r]} not found on any agenda since {start_d}")
    return out


def build(ono, c, outcomes, today):
    apps = sorted(c["apps"], key=lambda a: a["date"])
    latest = apps[-1]
    pz = [a for a in apps if a["body"] == "Planning and Zoning Commission"]
    name_src = (pz[0] if pz else apps[0])["name"]
    pname = project_name(name_src)

    # documents: newest staff analysis + newest ordinance PDF for this ordinance
    staff, ordtxt, app_dates = {}, "", []
    staff_txt = ""
    for a in reversed(apps):
        if not staff_txt:
            sa = [x for x in a["atts"] if re.search(r"Staff\s*Analysis", x.get("fileName") or "", re.I)]
            if not sa:
                sa = [x for x in a["sibling_atts"] if re.search(r"Staff\s*Analysis", x.get("fileName") or "", re.I)]
            if sa:
                try:
                    staff_txt = pdf_text(sa[0], max_pages=1)
                except Exception as e:  # noqa: BLE001
                    log(f"{ono}: staff analysis unreadable ({e})")
        if not ordtxt:
            od = [x for x in a["atts"] if re.search(r"^\s*(?:Ord|\d{4}-\d{3})", x.get("fileName") or "", re.I)
                  and re.search(ono[-3:], x.get("fileName") or "")]
            if od:
                try:
                    ordtxt = pdf_text(od[0], max_pages=10)
                except Exception as e:  # noqa: BLE001
                    log(f"{ono}: ordinance unreadable ({e})")
        for x in a["atts"] + a["sibling_atts"]:
            fn = x.get("fileName") or ""
            if re.search(r"Applica", fn, re.I):
                for m in re.finditer(r"(?=(?<!\d)(\d{1,2})[.\-_](\d{1,2})[.\-_](\d{4}|\d{2})(?!\d))", fn):
                    y = int(m.group(3)) + (2000 if len(m.group(3)) == 2 else 0)
                    try:
                        app_dates.append(dt.date(y, int(m.group(1)), int(m.group(2))).isoformat())
                    except ValueError:
                        pass
    staff = parse_staff(staff_txt) if staff_txt else {}

    kind = c["kind"]
    descs = [a["desc"] for a in apps if a["desc"] and not re.fullmatch(r"(?i)withdrawn|consider tabling.*", a["desc"])]
    desc = descs[0] if descs else (staff.get("REQUESTED ACTION"))
    f_, t_ = from_to(ordtxt)
    from_z = to_z = from_flu = to_flu = None
    if kind == "CPA":
        from_flu, to_flu = f_, t_
        if not from_flu:
            m = re.search(r"future land use from (.+?) to (.+?)(?: for| on|\.|$)", " ".join(descs), re.I)
            if m:
                from_flu, to_flu = clean(m.group(1)), clean(m.group(2))
        from_flu = from_flu or staff.get("FUTURE LAND USE CATEGORY") or staff.get("FUTURE LAND USE")
    else:
        from_z, to_z = f_, t_
        if not from_z:
            m = re.search(r"(?:rezone|zoning (?:district )?)\s*from (.+?) to (.+?)(?: for| on|\.|$)", " ".join(descs), re.I)
            if m:
                from_z, to_z = clean(m.group(1)), clean(m.group(2))
        from_z = from_z or staff.get("EXISTING ZONING")
    is_pd = bool(re.search(r"\bPUD\b|Planned Unit Development", " ".join(filter(None, [to_z, name_src, latest["name"]]))))
    category = "Comp Plan Amendment" if kind == "CPA" else ("PD" if is_pd else "Rezoning")
    if kind == "CPA":
        app_type = "LSCPA" if re.search(r"LSCPA|Large", " ".join(a["name"] for a in apps), re.I) else "SSCPA"
    else:
        app_type = "Rezoning (PUD)" if is_pd else "Rezoning"

    acres = parse_acres(staff.get("SIZE OF PARCEL")) or parse_acres(ordtxt)
    keys = alt_keys(staff_txt) or alt_keys(ordtxt)
    applicant = staff.get("APPLICANT")
    owner = staff.get("OWNER")
    address = staff.get("LOCATION")
    if address:
        address = clean(re.sub(r"\(?\s*Alternate\s+Key.*$", "", address, flags=re.I))

    # hearings
    hearings = []
    for a in apps:
        res = None
        if a["body"] == "City Council":
            word = outcomes.get(a["summary"], {}).get(ono) if a["summary"] else None
            if not word and re.search(r"\bwithdrawn\b", a["desc"] or "", re.I):
                word = "WITHDRAWN"
            if not word and re.search(r"consider tabling|\btable\b", (a["desc"] or "") + " " + a["name"], re.I):
                word = "TABLED" if a["date"] <= today else None
            res = norm_result(word, a["reading"], a["body"])
        body = a["body"]
        if a["reading"]:
            body = f"City Council ({READING_LABEL.get(a['reading'], a['reading'])})"
        h = {"body": body, "date": a["date"], "result": res}
        if h not in hearings:
            hearings.append(h)

    status, raw, decision = None, None, None
    past = [h for h in hearings if h["date"] <= today]
    future = [h for h in hearings if h["date"] > today]
    final = [h for h in past if h["result"] in ("Approved", "Denied") and h["body"].startswith("City Council")]
    if any(h["result"] == "Withdrawn" for h in hearings):
        status, raw = "Withdrawn", "Withdrawn (City Council agenda / Summary of Actions)"
    elif final:
        status, decision = final[-1]["result"], final[-1]["date"]
        raw = f"{final[-1]['body']} {decision}: {final[-1]['result'].upper()} (Summary of Actions)"
    elif future:
        status, raw = "Hearing Scheduled", f"{future[0]['body']} {future[0]['date']}"
    elif past and past[-1]["result"] == "Continued":
        status, raw = "Continued", f"{past[-1]['body']} {past[-1]['date']}: tabled/continued"
    elif past:
        lastp = past[-1]
        if lastp["body"].startswith("City Council") and lastp["result"] is None:
            status, raw = "Hearing Scheduled", f"{lastp['body']} {lastp['date']} held; outcome not yet published"
        else:
            status = "Filed"
            raw = f"{lastp['body']} {lastp['date']}" + (f": {lastp['result']}" if lastp["result"] else "") + \
                  "; next City Council hearing not yet on an agenda"
    else:
        status, raw = "Filed", "On agenda"

    first_seen = min(a["first_seen"] for a in apps)
    cands = [d for d in app_dates if d <= first_seen and d >= (dt.date.fromisoformat(first_seen) - dt.timedelta(days=730)).isoformat()]
    filed = min(cands) if cands else first_seen
    if desc and len(desc) > 300:
        desc = desc[:297] + "..."
    return {
        "jurisdiction": JUR, "county": "Lake", "app_num": f"Ordinance No. {ono}", "category": category,
        "app_type": app_type, "project_name": pname or staff.get("PROJECT NAME"), "description": desc,
        "applicant": applicant or owner, "parcel_ids": keys, "address": address, "acres": acres,
        "from_zoning": from_z, "to_zoning": to_z,
        "from_flu": from_flu if kind == "CPA" else None, "to_flu": to_flu if kind == "CPA" else None,
        "filed_date": filed, "status": status, "status_raw": raw, "hearings": hearings,
        "decision_date": decision, "url": f"{PORTAL}/event/{latest['event_id']}/files",
        "docs_url": file_stream(latest["packet"]) if latest["packet"] else None,
        "lat": None, "lon": None, "source": SOURCE,
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
