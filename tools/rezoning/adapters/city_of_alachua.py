#!/usr/bin/env python3
"""City of Alachua (Alachua County) rezoning / PD / future land use map amendment adapter.

Source: CivicClerk public API (alachuafl.api.civicclerk.com), the city's agenda portal
(alachuafl.portal.civicclerk.com). Not to be confused with alachua.py (Alachua County,
unincorporated, CitizenServe).
 * Cases first appear on a Planning & Zoning Board agenda as numbered applications:
   "PR26-0002 | Farmlands Commercial Site-Specific Amendment to the Official Zoning Atlas (Rezoning)",
   "PSSC26-0002 | Farmlands Commercial Small-Scale Comprehensive Plan Amendment". Prefixes kept:
   PR (rezoning, incl. PD), PSSC / PLSC (small / large scale FLUM amendment), PPD / PPDA (PD).
   Skipped: PSP / PSPM site plans, PPP / PFP plats, PSE special exceptions, PCA certificates of
   appropriateness, PTAL / PTAC text amendments, annexations.
   app_num = the application number exactly as the city prints it ("PR26-0002").
 * City Commission hearings are ordinance items ("Ordinance 26-10: AN ORDINANCE ... AMENDING THE
   OFFICIAL ZONING ATLAS ... (Quasi-Judicial Hearing; First Reading)"). They are tied to the
   application by the application number printed on page 1 of the attached staff report; failing
   that, by matching tax parcels; failing that, the ordinance stands alone as "Ordinance 26-10".
 * Details (applicant, owner, parcels, acreage, from/to) come from the item text and page 1 of the
   staff report ("APPLICANT/AGENT:", "PARCEL ID NUMBER:", "ACREAGE:"), read with HTTP range requests.
 * Outcomes: CivicClerk minutes votes on the item (minutesItemVotes: motion name + pass/fail) and
   the executed ordinance the clerk attaches after adoption ("O.26.10", "O26-17", "O.26-13-signed").
   Minutes PDFs are scanned images, so they are not read.
Requires pypdf.
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

JURISDICTIONS = ["City of Alachua"]
JUR = "City of Alachua"
COUNTY = "Alachua"
TENANT = "alachuafl"
API = f"https://{TENANT}.api.civicclerk.com/v1"
PORTAL = f"https://{TENANT}.portal.civicclerk.com"
SOURCE = "City of Alachua CivicClerk (P&Z Board + City Commission agendas, staff reports, votes)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
LOOKBACK_DAYS = 150
SMALL_PDF = 1_500_000

PZ, CC = "Planning & Zoning Board", "City Commission"
SKIP_EVENT = re.compile(r"Workshop|Budget|Canvass|Education Task|Cancel|Redevelopment|Advisory", re.I)

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


def log(*a):
    print("[city_of_alachua]", *a, file=sys.stderr)


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
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = s.replace("&nbsp;", " ").replace("\u00a0", " ").replace("&amp;", "&").replace("\u200b", "")
    return sq(s.replace("\u2013", "-").replace("\u2014", "-"))


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
    """Text of the first pages of a CivicClerk attachment."""
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
    if re.search(r"Planning and Zoning", cat + " " + name, re.I):
        return PZ
    if re.search(r"City Commission", cat + " " + name, re.I) or re.search(r"Special City Commission", name, re.I):
        return CC
    return None


# ------------------------------------------------------------------ classification
CASE_RE = re.compile(r"\b(P[A-Z]{1,5})\s?(\d{2})\s?-\s?(\d{3,4})\b")
IN_PRE = {"PR", "PSSC", "PLSC", "PLSCPA", "PSSCPA", "PPD", "PPDA", "PRZ", "PZ", "PCPA", "PFLUM"}
OUT_PRE = re.compile(r"^P(SP|SPM|PP|FP|SE|CA|TA|TAL|TAC|CU|V|VAR|AN|ANX|DA|SD)")
ORD_RE = re.compile(r"\bOrdinance\s*(?:No\.?\s*)?(\d{2,4})\s*-\s*(\d{1,3})\b", re.I)
CC_RZ = re.compile(r"OFFICIAL ZONING ATLAS|REZON|ZONING MAP|PLANNED DEVELOPMENT", re.I)
CC_CPA = re.compile(r"FUTURE LAND USE MAP|COMPREHENSIVE PLAN FUTURE LAND USE", re.I)
CC_OUT = re.compile(r"LAND DEVELOPMENT REGULATIONS|MORATORIUM|FRANCHISE|EVALUATION AND APPRAISAL|"
                    r"RELATING TO THE AMENDMENT OF ORDINANCE|TEXT AMENDMENT", re.I)
PIN_RE = re.compile(r"\b(\d{5})\s?-\s?(\d{3})\s?-\s?(\d{3})\b")


def case_no(m):
    return f"{m.group(1)}{m.group(2)}-{m.group(3)}"


def case_kind(pre, text):
    if pre in ("PSSC", "PLSC", "PLSCPA", "PSSCPA", "PCPA", "PFLUM"):
        return "CPA"
    if pre in ("PR", "PPD", "PPDA", "PRZ", "PZ"):
        return "RZ"
    if re.search(r"Comprehensive Plan Amendment|Future Land Use Map", text, re.I) and \
            not re.search(r"Text Amendment", text, re.I):
        return "CPA"
    if re.search(r"Rezoning|Zoning Atlas|Planned Development", text, re.I):
        return "RZ"
    return None


def pins_in(text):
    t = re.sub(r"(\d)\s*-\s*\n?\s*(\d)", r"\1-\2", text or "")
    return list(dict.fromkeys(f"{a}-{b}-{c}" for a, b, c in PIN_RE.findall(t)))


def reading_of(text):
    t = (text or "").lower()
    if re.search(r"first and final|adoption hearing|adoption reading", t):
        return "adoption"
    if re.search(r"\b(second|2nd)\b[^.;)]{0,20}reading|final reading", t):
        return "second"
    if re.search(r"\btransmittal\b", t):
        return "transmittal"
    if re.search(r"\b(first|1st)\b[^.;)]{0,10}reading", t):
        return "first"
    return None


def vote_result(item):
    """Result from CivicClerk minutes votes on an item, or None."""
    res = None
    for v in item.get("minutesItemVotes") or []:
        name = (v.get("motionName") or "").lower()
        if re.search(r"extend|agenda|minutes|recess|adjourn|reconsider", name):
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


def executed_ordinance(item, ono):
    """True when the executed ordinance ('O.26.10', 'O26-17', 'O.26-13-signed') is attached."""
    y, n = ono.split("-")
    pat = re.compile(rf"^\s*O\.?\s*{y[-2:]}\s*[.\-]\s*0*{int(n)}\b", re.I)
    return any(pat.search(a.get("fileName") or "") for a in item.get("attachmentsList") or [])


# ------------------------------------------------------------------ text parsing
LABELS = ["SUBJECT", "APPLICANT/AGENT", "APPLICANT", "AGENT", "PROPERTY OWNERS", "PROPERTY OWNER", "LOCATION",
          "PARCEL ID NUMBERS", "PARCEL ID NUMBER", "TAX PARCELS", "TAX PARCEL",
          "FLUM DESIGNATION", "ZONING", "OVERLAY", "ACREAGE", "PROJECT PLANNER", "RECOMMENDATION",
          "RECOMMENDED MOTION", "Planning & Zoning Board Hearing Date", "Quasi-Judicial Hearing Application #"]
LABELS_UP = {x.upper() for x in LABELS}
LAB_RE = re.compile(r"^\s*(" + "|".join(re.escape(x) for x in LABELS) + r")\s*:", re.I | re.M)


def parse_staff(text):
    out = {}
    ms = list(LAB_RE.finditer(text or ""))
    for i, m in enumerate(ms):
        lab = m.group(1).upper()
        end = ms[i + 1].start() if i + 1 < len(ms) else min(len(text), m.end() + 300)
        val = sq(text[m.end():end]).strip(" :.;")
        val = re.sub(r"\s*Staff Report:.*$", "", val)
        if lab.endswith("S") and lab[:-1] in LABELS_UP:
            lab = lab[:-1]
        if val and lab not in out and val.upper() not in ("N/A", "NA", "NONE"):
            out[lab] = val
    return out


def parse_acres(text):
    t = sq(text)
    for pat in (r"^±?\s*([\d,]*\.?\d+)\s*(?:\+/-\s*)?(?:net|gross|acres?|ac\b|$)", r"±\s*([\d,]*\.?\d+)\s*-?\s*(?:net\s+|gross\s+)?acres?", r"([\d,]*\.?\d+)\s*(?:\+/-|±)?\s*-?\s*acres?",
                r"ACREAGE:\s*±?\s*([\d,]*\.?\d+)"):
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
        pats = [r"FUTURE LAND USE MAP (?:CLASSIFICATION|DESIGNATION)S? OF .*? FROM (.+?) TO (.+?)(?:;|,? GENERALLY| ON A)",
                r"future land use (?:designation|categories|category|classification)[^.;]{0,80}? from (.+?) to (.+?)(?: on a| on the|;|\.\s|$)",
                r"(?:Current|Existing) (?:FLU|FLUM|Future Land Use):\s*(.+?);\s*Proposed (?:FLU|FLUM|Future Land Use):\s*(.+?)(?:;|\(|$)"]
    else:
        pats = [r"OFFICIAL ZONING ATLAS FROM (.+?) TO (.+?)(?: ON A| ON THE|;|,? GENERALLY)",
                r"(?:Official Zoning Atlas|zone district|zoning district|zoning designation|zoning)[^.;]{0,80}? from (.+?) to (.+?)(?: on a| on the|;|\.\s|$)",
                r"(?:Current|Existing) Zoning:\s*(.+?);\s*Proposed Zoning:\s*(.+?)(?:\s*\(|;|$)"]
    for p in pats:
        m = re.search(p, t, re.I)
        if m:
            f_, t_ = sq(m.group(1)).strip(" ,.;"), sq(m.group(2)).strip(" ,.;")
            if 0 < len(f_) < 200 and 0 < len(t_) < 200:
                return f_, t_
    return None, None


def request_parts(text):
    """Applicant / owner from 'A request by X (Agent) on behalf of Y (Property Owner)'."""
    t = sq(text)
    app = own = None
    m = re.search(r"request by (.+?)(?:\s*\((?:the\s+)?[^)]*(?:Agent|Applicant)[^)]*\)|,\s*(?:applicant|agent))", t, re.I)
    if m:
        app = m.group(1).strip(" ,")
    m = re.search(r"on behalf of (.+?)\s*\((?:the\s+)?[^)]*(?:Owner)[^)]*\)", t, re.I) or \
        re.search(r"(?:applicant and agent|agent) for (.+?),\s*(?:the\s+)?property owner", t, re.I)
    if m:
        own = m.group(1).strip(" ,")
    return app, own


def location_of(text):
    m = re.search(r"generally located (?:at|on|along)?\s*(.+?)(?:\s*\(|;|\.\s|$)", sq(text), re.I)
    if m:
        v = m.group(1).strip(" ,")
        return v[:150] if v else None
    return None


# ------------------------------------------------------------------ fetch
def fetch(since, refresh):
    if pypdf is None:
        raise RuntimeError("pypdf is required for the City of Alachua adapter")
    today = dt.date.today()
    refresh_ids = {sq(r).upper(): r for r in (refresh or []) if r and sq(r)}
    start_d = dt.date.fromisoformat(since) - dt.timedelta(days=LOOKBACK_DAYS)
    if refresh_ids:
        yrs = [2000 + int(m.group(1)) for r in refresh_ids for m in [re.search(r"(\d{2})-\d{3,4}$", r)] if m]
        if yrs:
            start_d = min(start_d, max(dt.date(min(yrs), 1, 1), today - dt.timedelta(days=730)))
    end_d = today + dt.timedelta(days=75)
    try:
        events = list_events(start_d.isoformat(), end_d.isoformat())
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"City of Alachua CivicClerk unreachable: {e}")
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
        for it in walk(mt.get("items")):
            name = strip_html(it.get("agendaObjectItemName"))
            desc = strip_html(it.get("agendaObjectItemDescription"))
            app = {"date": mdate, "first_seen": first_seen, "body": body, "name": name, "desc": desc, "item": it,
                   "event_id": ev.get("id"), "packet": packet}
            if body == PZ:
                m = CASE_RE.search(name[:40])
                if not m:
                    continue
                pre = m.group(1).upper()
                if OUT_PRE.match(pre) and pre not in IN_PRE:
                    continue
                kind = case_kind(pre, name)
                if not kind:
                    continue
                cases.setdefault(case_no(m), {"kind": kind, "apps": []})["apps"].append(app)
            else:
                om = ORD_RE.search(name[:60])
                if not om or CC_OUT.search(name):
                    continue
                kind = "CPA" if CC_CPA.search(name) else ("RZ" if CC_RZ.search(name) else None)
                if not kind:
                    continue
                app["ono"] = f"{om.group(1)[-2:]}-{int(om.group(2)):02d}"
                app["kind"] = kind
                ords.append(app)
    log(f"{len(cases)} P&Z cases, {len(ords)} Commission ordinance items")

    # tie Commission ordinance items to applications
    ord_case = {}
    for a in ords:
        ono = a["ono"]
        key = ord_case.get(ono)
        if not key:
            txt = a["name"] + " " + a["desc"]
            cands = [c for c in dict.fromkeys(case_no(m) for m in CASE_RE.finditer(txt))
                     if case_kind(CASE_RE.match(c).group(1), "") == a["kind"]]
            key = cands[0] if cands else None
            pins = set(pins_in(a["name"]))
            if not key and pins:
                for k, c in cases.items():
                    if c["kind"] == a["kind"] and pins & set(pins_in(" ".join(x["name"] for x in c["apps"]))):
                        key = k
                        break
            if not key:
                sr = [x for x in a["item"].get("attachmentsList") or [] if re.search(r"Staff Report", x.get("fileName") or "", re.I)
                      and not re.search(r"from Ordinance", x.get("fileName") or "", re.I)]
                if sr:
                    cands = [c for c in dict.fromkeys(case_no(m) for m in CASE_RE.finditer(att_text(sr[0], 1)[:1500]))
                             if case_kind(CASE_RE.match(c).group(1), "") == a["kind"]]
                    key = cands[0] if cands else None
            key = key or f"Ordinance {ono}"
            ord_case[ono] = key
        a["case"] = key
        c = cases.setdefault(key, {"kind": a["kind"], "apps": []})
        c["apps"].append(a)
        c.setdefault("onos", set()).add(ono)
    log("ordinance -> case: " + ", ".join(f"{k}={v}" for k, v in sorted(ord_case.items())))

    out = []
    for key, c in sorted(cases.items()):
        apps = sorted(c["apps"], key=lambda x: (x["date"], x["body"] != PZ))
        first = min(x["first_seen"] for x in apps)
        older = apps[0]["body"] == CC and reading_of(apps[0]["name"]) in ("second",) and \
            apps[0]["date"] < (start_d + dt.timedelta(days=45)).isoformat()
        if (first < since or older) and key.upper() not in refresh_ids:
            continue
        try:
            out.append(build(key, c, apps, today.isoformat()))
        except Exception as e:  # noqa: BLE001
            log(f"{key}: skipped ({e})")
    for r in set(refresh_ids) - {k.upper() for k in cases}:
        log(f"refresh id {refresh_ids[r]} not found on any agenda since {start_d}")
    return out


def build(key, c, apps, today):
    kind = c["kind"]
    pz = [a for a in apps if a["body"] == PZ]
    cc = [a for a in apps if a["body"] == CC]
    texts = [a["name"] + " " + a["desc"] for a in apps]
    alltxt = " ".join(texts)

    # staff report page 1 (newest P&Z item first, then Commission items)
    staff = {}
    for a in list(reversed(pz)) + list(reversed(cc)):
        sr = [x for x in a["item"].get("attachmentsList") or [] if re.search(r"Staff Report", x.get("fileName") or "", re.I)
              and not re.search(r"from Ordinance", x.get("fileName") or "", re.I)]
        if sr:
            t = att_text(sr[0], 2)
            if t and (key in re.sub(r"\s", "", t[:3000]) or key.startswith("Ordinance")):
                staff = parse_staff(t)
                staff["_text"] = t
                break

    pins = pins_in(staff.get("PARCEL ID NUMBER", "") + " " + staff.get("TAX PARCEL", "")) or pins_in(alltxt) or pins_in(staff.get("_text", ""))
    acres = parse_acres(staff.get("ACREAGE", "")) if staff.get("ACREAGE") else None
    if acres is None:
        acres = parse_acres(alltxt)
    f_, t_ = None, None
    for txt in [a["name"] for a in cc] + [a["name"] for a in pz] + [staff.get("SUBJECT", ""), staff.get("_text", "")]:
        f_, t_ = from_to(txt, kind)
        if f_:
            break
    applicant = staff.get("APPLICANT/AGENT") or staff.get("APPLICANT")
    owner = staff.get("PROPERTY OWNER")
    if not applicant or not owner:
        a2, o2 = request_parts(" ".join(a["name"] for a in pz) or alltxt)
        applicant, owner = applicant or a2, owner or o2
    if applicant and owner and owner.lower() not in applicant.lower():
        applicant = f"{applicant} (agent) for {owner} (owner)"
    applicant = applicant or owner
    if not applicant and re.search(r"request by the City of Alachua|City of Alachua \(applicant", alltxt, re.I):
        applicant = "City of Alachua"
    address = staff.get("LOCATION")
    if address:
        address = re.sub(r"^Generally located (?:at|on)?\s*", "", address, flags=re.I)[:150] or None
    address = address or location_of(alltxt)

    # project name from the P&Z item ("PR26-0002 | Farmlands Commercial Site-Specific ... - A request")
    pname = None
    if pz:
        nm = CASE_RE.sub("", pz[0]["name"], count=1)
        nm = re.split(r"\s+-\s+A request|\s*[\u2014-]\s*A REQUEST|:\s*A request", nm, flags=re.I)[0]
        nm = re.sub(r"Site-Specific Amendment to the Official Zoning Atlas|\(Rezoning\)|Small-Scale Comprehensive Plan "
                    r"(Map )?Amendment|Large-Scale Comprehensive Plan Amendment|\(S?SCPA\)|Rezoning( for)?", " ", nm, flags=re.I)
        pname = sq(nm).strip(" |-:,") or None
    to_txt = " ".join(filter(None, [t_, alltxt if kind == "RZ" else ""]))
    is_pd = kind == "RZ" and bool(re.search(r"\bPD-|\bPD\b|Planned Development|\bPUD\b", t_ or "") or
                                  re.search(r"\bPPD", key) or re.search(r"TO PD|TO PLANNED DEVELOPMENT", to_txt, re.I))
    category = "Comp Plan Amendment" if kind == "CPA" else ("PD" if is_pd else "Rezoning")
    if kind == "CPA":
        app_type = "Large-Scale Comprehensive Plan Amendment" if re.search(r"LARGE[- ]SCALE|^PLSC", alltxt + key, re.I) \
            else "Small-Scale Comprehensive Plan Amendment"
    else:
        app_type = "Site-Specific Amendment to the Official Zoning Atlas" + (" (Planned Development)" if is_pd else "")
    desc = staff.get("SUBJECT")
    if desc and (len(desc) < 40 or re.search(r"\b(the|of|to|from)$", desc)):
        desc = None
    if not desc:
        base = pz[0]["name"] if pz else apps[0]["name"]
        m = re.search(r"A request .*", base, re.I)
        desc = m.group(0) if m else base
    desc = sq(desc)
    if len(desc) > 300:
        desc = desc[:297] + "..."
    onos = sorted(c.get("onos") or [])
    if onos and not key.startswith("Ordinance"):
        desc = f"{desc} (Ordinance {', '.join(onos)})"

    hearings = []
    for a in apps:
        res = vote_result(a["item"])
        rd = reading_of(a["name"]) if a["body"] == CC else None
        if a["body"] == CC and executed_ordinance(a["item"], a["ono"]):
            res = "Approved"
            rd = rd if rd in ("second", "adoption") else "adoption"
        if a["body"] == CC and res is None and re.search(r"\bwithdrawn\b", a["name"] + " " + a["desc"], re.I):
            res = "Withdrawn"
        body = a["body"]
        if a["body"] == CC:
            body = f"City Commission ({rd} reading)" if rd in ("first", "second", "transmittal") else \
                ("City Commission (adoption)" if rd == "adoption" else "City Commission")
        h = {"body": body, "date": a["date"], "result": res, "_rd": rd, "_body": a["body"]}
        if a["body"] == PZ and res in ("Approved", "Denied"):
            h["result"] = "Recommended " + ("Approval" if res == "Approved" else "Denial")
        prev = next((x for x in hearings if x["body"] == h["body"] and x["date"] == h["date"]), None)
        if prev:
            prev["result"] = prev["result"] or h["result"]
        else:
            hearings.append(h)
    hearings.sort(key=lambda h: (h["date"], h["_body"] != PZ))
    # a Commission hearing with no recorded result that is followed by another hearing at the same
    # reading was continued; a first reading followed by a later reading passed
    ccs = [h for h in hearings if h["_body"] == CC]
    rank = {"first": 1, "transmittal": 1, "second": 2, "adoption": 2, None: 0}
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
        "from_zoning": f_ if kind == "RZ" else None, "to_zoning": t_ if kind == "RZ" else None,
        "from_flu": f_ if kind == "CPA" else None, "to_flu": t_ if kind == "CPA" else None,
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
