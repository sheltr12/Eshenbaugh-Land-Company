#!/usr/bin/env python3
"""City of Titusville (Brevard County) rezoning / PUD / future land use map amendment adapter.

Source: the city's CivicClerk public API (titusvillefl.api.civicclerk.com), same platform as
clermont.py. No login.
 * Land use cases are agenda items of the Planning and Zoning Commission and City Council, named
   by the city case number: "Small Scale Amendment (SSA) No. 3-2026 - Diamond Oaks",
   "Rezoning (REZ) Application No. 2-2026 - Farm Land ...", "Comprehensive Plan Map Amendment
   (CPA) No. 1-2026 ... & Planned Industrial Development (PID) Rezoning". app_num = "SSA 3-2026",
   "REZ 2-2026", "CPA 1-2026". An SSA always comes with a companion rezoning (one record, category
   Comp Plan Amendment, to_zoning filled when stated). Text-only CPAs, annexations, CUPs, plats
   and code amendments are skipped.
 * Item descriptions and the attached Staff Report (first two pages) give applicant, acreage,
   Brevard parcel IDs ("Parcel ID No. 22-35-28-00-254"), from/to designations and the scheduled
   P&Z / Council dates.
 * Outcomes come from the minutes PDFs CivicClerk publishes for each meeting (P&Z "Motion passed",
   Council "The motion carried unanimously"). Once Council minutes are published the Council
   agenda items are no longer served, so Council appearances are also read from the minutes.
Cases surface when they first reach a P&Z agenda; filed_date = date in the "Application" attachment
name when present, else the agenda publish date. Requires pypdf.
"""
import datetime as dt
import io
import json
import logging
import re
import sys
import time

import requests

try:
    import pypdf
    logging.getLogger("pypdf").setLevel(logging.ERROR)
except ImportError:  # pragma: no cover
    pypdf = None

JURISDICTIONS = ["City of Titusville"]
JUR = "City of Titusville"
TENANT = "titusvillefl"
API = f"https://{TENANT}.api.civicclerk.com/v1"
PORTAL = f"https://{TENANT}.portal.civicclerk.com"
SOURCE = "Titusville CivicClerk (P&Z Commission + City Council agendas, staff reports, minutes)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
LOOKBACK_DAYS = 120
SMALL_PDF = 2_500_000
PZ_CAT = "Planning and Zoning Commission"
CC_CAT = "City Council"
SKIP_EVENT = re.compile(r"Cancel|Canceled|Presentation Meeting|Workshop|Retreat|Budget|CDBG", re.I)

CASE_RE = re.compile(r"\b(SSA|REZ|CPA|PUD|PID|LSA)\s*\)?\s*(?:Application\s*)?(?:No\.?|#)\s*(\d{1,2})\s*-\s*(\d{4})", re.I)
PID_RE = re.compile(r"\b(\d{2})\s?-\s?(\d{2})\s?-\s?(\d{2})\s?-\s?([0-9A-Z]{2})\s?-\s?([0-9A-Z*]{1,4}(?:\.\d+)?)"
                    r"(?:\s?-\s?([0-9A-Z*]{1,5}(?:\.\d+)?))?\b")
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


def log(*a):
    print("[titusville]", *a, file=sys.stderr, flush=True)


def get(url, params=None, tries=3, sleep=0.3, **kw):
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
    """Seekable read-only file over HTTP range requests (pypdf reads a few pages of a big PDF)."""

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
            self.cache[i] = get(self.url, headers={"Range": f"bytes={a}-{b}"}, sleep=0.05).content
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


def pdf_text(url, size=0, max_pages=None, key=None):
    key = (key or url, max_pages)
    if key in _pdf_cache:
        return _pdf_cache[key]
    if size and size > SMALL_PDF:
        # the attachment's fileSize is the original upload; the PDF rendition differs
        try:
            size = int(_s.head(url, timeout=60).headers.get("Content-Length") or 0)
        except Exception:  # noqa: BLE001
            size = 0
    if size and size > SMALL_PDF:
        rdr = pypdf.PdfReader(RangeFile(url, size))
    else:
        rdr = pypdf.PdfReader(io.BytesIO(get(url).content))
    pages = rdr.pages if max_pages is None else rdr.pages[:max_pages]
    out = []
    for p in pages:
        try:
            out.append(p.extract_text() or "")
        except Exception:  # noqa: BLE001
            out.append("")
    txt = "\n".join(out)
    _pdf_cache[key] = txt
    return txt


def file_stream(file_id):
    return f"{API}/Meetings/GetMeetingFileStream(fileId={file_id},plainText=false)"


def sq(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = s.replace("&nbsp;", " ").replace(" ", " ").replace("&amp;", "&").replace("​", "")
    return re.sub(r"\s+", " ", s).strip()


def clean(v):
    v = re.sub(r"\s+", " ", v or "").strip(" .,;:")
    return v or None


def to_float(s):
    try:
        v = float(str(s).replace(",", ""))
        return v if 0 < v < 100000 else None
    except (TypeError, ValueError):
        return None


def text_date(s):
    m = re.search(rf"({MONTHS})\s+(\d{{1,2}}),?\s+(\d{{4}})", s or "")
    if not m:
        return None
    try:
        return dt.datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y").date().isoformat()
    except ValueError:
        return None


# ------------------------------------------------------------------ case keys
def case_keys(text):
    """[(key, kind)] for in-scope case numbers in a text; text-only CPAs dropped."""
    out = []
    t = text or ""
    for m in CASE_RE.finditer(t):
        typ = m.group(1).upper()
        key = f"{typ} {int(m.group(2))}-{m.group(3)}"
        before, after = t[max(0, m.start() - 45):m.start()], t[m.end():m.end() + 14]
        if typ == "CPA" and (re.search(r"\(Text\)", after) or (re.search(r"Text Amendment", before, re.I)
                                                                and not re.search(r"\(Map\)", after))):
            continue
        if typ in ("PUD", "PID") and not re.search(r"Rezon|Zoning|Planned", t, re.I):
            continue
        if key not in [k for k, _ in out]:
            out.append((key, typ))
    return out


def norm_pid(m):
    parts = [p for p in m.groups() if p]
    return "-".join(parts)


def parcels_of(text):
    out = []
    t = re.sub(r"(\d)\s+-", r"\1-", text or "")
    for m in PID_RE.finditer(t):
        p = norm_pid(m)
        if p not in out:
            out.append(p)
    return out


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


# ------------------------------------------------------------------ minutes
def minutes_results(text, body):
    """{case_key: result} from a minutes PDF; sections are separated by 'XXX' lines."""
    res = {}
    t = re.sub(r"[ \t]+", " ", text)
    segs = re.split(r"\n\s*x{3}\s*\n", t, flags=re.I)
    current = []
    for seg in segs:
        flat = re.sub(r"\s+", " ", seg)
        mot = re.search(r"(?:made a motion|moved)\s+to\b", flat, re.I)
        keys = [k for k, _ in case_keys(seg[:600])] or [k for k, _ in case_keys(seg)]
        if not keys and mot and mot.start() < 400 and current:
            keys = current   # motion paragraph split from its item heading by an 'XXX' line
        if not keys:
            current = []
            continue
        current = keys
        result = None
        for m in re.finditer(r"(?:made a motion|moved)\s+to\s+(.{0,2000}?)(?:Motion (passed|failed|carried)|"
                             r"The motion (carried|passed|failed))", flat, re.I):
            verb = m.group(1).lower()
            ok = (m.group(2) or m.group(3) or "").lower() != "failed"
            if re.search(r"\b(table|continue|postpone|defer)", verb):
                r = "Continued" if ok else None
            elif re.search(r"\bden(y|ial)\b", verb):
                r = ("Recommended Denial" if body == "Planning and Zoning Commission" else "Denied") if ok else None
            elif re.search(r"reconsider", verb):
                r = "Reconsideration approved" if ok else None
            elif re.search(r"transmit", verb):
                r = ("Recommended Approval" if body == "Planning and Zoning Commission" else "Transmitted") if ok \
                    else "Transmittal failed"
            elif re.search(r"approv|adopt|recommend", verb):
                if body == "Planning and Zoning Commission":
                    r = "Recommended Approval" if ok else "Motion to recommend approval failed"
                elif re.search(r"first reading", verb):
                    r = "Approved on first reading" if ok else "Denied"
                else:
                    r = "Approved" if ok else "Denied"
            else:
                r = None
            if r:
                result = r
        if result is None and re.search(r"withdr[ae]w[n]? (?:the|this|his|her|their)? ?(?:application|request)|"
                                        r"applicant (?:has )?withdr", flat, re.I):
            result = "Withdrawn"
        if result is None and re.search(r"\b(?:tabled|continued|postponed) (?:to|until)\b", flat, re.I):
            result = "Continued"
        for k in keys:
            if result or k not in res:
                res[k] = result
    return res


# ------------------------------------------------------------------ staff report
def parse_staff(t):
    out = {}
    if not t:
        return out
    t1 = re.sub(r"[ \t]+", " ", t)
    t1 = re.sub(r"(?m)^ ?\d{1,2} (?=\S)", "", t1)       # pleading line numbers (line start)
    t1 = re.sub(r"(?m)(\S) \d{1,2} ?$", r"\1", t1)      # pleading line numbers (line end)
    m = re.search(r"Applicant\(?s?\)?:\s*(.+?)(?:\n\s*(?:Applicant.s Request|Request|Owner)|$)", t1, re.S | re.I)
    if m:
        a = clean(re.sub(r"\s\d{1,2}\s*$", "", re.sub(r"\s+\d{1,2}\s*\n", " ", m.group(1))))
        out["applicant"] = a[:200] if a else None
    m = re.search(r"Meeting Dates:(.{0,400}?)(?:Prepared By|Applicant)", t1, re.S | re.I)
    sched = []
    if m:
        for mm in re.finditer(rf"(Planning and Zoning Commission|City Council[^\n]*?)\s+((?:{MONTHS})\s+\d{{1,2}},?\s+\d{{4}})", m.group(1)):
            d = text_date(mm.group(2))
            if d:
                lab = mm.group(1)
                body = "Planning and Zoning Commission" if lab.startswith("Planning") else "City Council"
                sched.append((body, d, lab.strip()))
    out["sched"] = sched
    flat = re.sub(r"\s+", " ", t1)
    out["flat"] = flat
    return out


# ------------------------------------------------------------------ fetch
def fetch(since, refresh):
    if pypdf is None:
        raise RuntimeError("pypdf is required for the Titusville adapter")
    today = dt.date.today()
    refresh_keys = {}
    for r in refresh or []:
        ks = case_keys(re.sub(r"^([A-Z]{3})\s+", r"\1 No. ", (r or "").strip().upper()))
        if ks:
            refresh_keys[ks[0][0]] = r
    start_d = dt.date.fromisoformat(since) - dt.timedelta(days=LOOKBACK_DAYS)
    if refresh_keys:
        yr = min(int(k[-4:]) for k in refresh_keys)
        start_d = min(start_d, max(dt.date(yr - 1, 10, 1), today - dt.timedelta(days=730)))
    end_d = today + dt.timedelta(days=75)
    try:
        events = list_events(start_d.isoformat(), end_d.isoformat())
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Titusville CivicClerk unreachable: {e}") from e
    log(f"{len(events)} events {start_d}..{end_d}")

    cases = {}
    minutes = []   # (body, date, fileId, event_id)
    for ev in events:
        cat = ev.get("categoryName") or ""
        name = ev.get("eventName") or ""
        if SKIP_EVENT.search(name):
            continue
        if cat == PZ_CAT:
            body = "Planning and Zoning Commission"
        elif cat == CC_CAT:
            body = "City Council"
        else:
            continue
        mdate = ev["startDateTime"][:10]
        files = ev.get("publishedFiles") or []
        for f in files:
            if f.get("type") == "Minutes" and f.get("fileId"):
                minutes.append((body, mdate, f["fileId"], ev.get("id")))
        if not ev.get("agendaId"):
            continue
        pub = [f.get("publishOn")[:10] for f in files if f.get("type") == "Agenda" and f.get("publishOn")]
        first_seen = min(pub + [mdate])
        packet = next((f.get("fileId") for f in files if f.get("type") == "Agenda Packet"), None)
        try:
            mt = get(f"{API}/Meetings/{ev['agendaId']}").json()
        except Exception as e:  # noqa: BLE001
            log(f"meeting {ev['agendaId']}: {e}")
            continue
        for it in walk_items(mt.get("items")):
            iname = sq(it.get("agendaObjectItemName"))
            desc = sq(it.get("agendaObjectItemDescription"))
            keys = case_keys(iname)
            if not keys or re.search(r"Minutes", iname, re.I):
                continue
            for key, typ in keys:
                cases.setdefault(key, {"typ": typ, "apps": []})["apps"].append({
                    "date": mdate, "first_seen": first_seen, "body": body, "name": iname, "desc": desc,
                    "atts": it.get("attachmentsList") or [], "event_id": ev.get("id"), "packet": packet,
                    "multi": len(keys) > 1,
                })
    log(f"{len(cases)} cases on agendas; {len(minutes)} minutes files")

    # minutes: outcomes, plus Council appearances whose agenda items are no longer served
    outcomes = {}
    for body, mdate, fid, eid in minutes:
        try:
            txt = pdf_text(file_stream(fid), key=f"min{fid}")
        except Exception as e:  # noqa: BLE001
            log(f"minutes {fid}: {e}")
            continue
        for k, r in minutes_results(txt, body).items():
            outcomes[(k, body, mdate)] = r
            c = cases.get(k)
            if c is None:
                continue   # case never on an agenda in the window (older case)
            if not any(a["body"] == body and a["date"] == mdate for a in c["apps"]):
                c["apps"].append({"date": mdate, "first_seen": mdate, "body": body, "name": None, "desc": None,
                                  "atts": [], "event_id": eid, "packet": None, "multi": False})

    out = []
    for key, c in sorted(cases.items()):
        first = min(a["first_seen"] for a in c["apps"])
        if first < since and key not in refresh_keys:
            continue
        try:
            rec = build(key, c, outcomes, today.isoformat())
        except Exception as e:  # noqa: BLE001
            log(f"{key}: skipped ({type(e).__name__}: {e})")
            continue
        if rec:
            if key in refresh_keys:
                rec["app_num"] = key
            out.append(rec)
    for k in set(refresh_keys) - set(cases):
        log(f"refresh id {refresh_keys[k]} not found on agendas since {start_d}")
    return out


def build(key, c, outcomes, today):
    apps = sorted(c["apps"], key=lambda a: (a["date"], a["body"] != "Planning and Zoning Commission"))
    named = [a for a in apps if a["name"]]
    typ = c["typ"]
    # staff report (newest appearance that has one)
    staff, staff_url = {}, None
    app_dates = []
    for a in reversed(named):
        for x in a["atts"]:
            fn = x.get("fileName") or ""
            if re.match(r"\s*Application", fn, re.I):
                for m in re.finditer(r"(?<!\d)(\d{1,2})[.\-_](\d{1,2})[.\-_](\d{2}|\d{4})(?!\d)", fn):
                    y = int(m.group(3)) + (2000 if len(m.group(3)) == 2 else 0)
                    try:
                        app_dates.append(dt.date(y, int(m.group(1)), int(m.group(2))).isoformat())
                    except ValueError:
                        pass
        if staff:
            continue
        sr = [x for x in a["atts"] if re.search(r"Staff\s*Report", x.get("fileName") or "", re.I)]
        if sr and sr[0].get("pdfVersionFullPath"):
            try:
                txt = pdf_text(sr[0]["pdfVersionFullPath"], sr[0].get("fileSize") or 0, max_pages=2,
                               key=f"att{sr[0].get('id')}")
                staff = parse_staff(txt)
            except Exception as e:  # noqa: BLE001
                log(f"{key}: staff report unreadable ({e})")
    flat = staff.get("flat") or ""
    descs = [a["desc"] for a in named if a["desc"] and not re.search(r"administratively withdrawn this item", a["desc"], re.I)]
    desc_all = " ".join(descs)
    main_desc = max(descs, key=len) if descs else None

    # hearings
    hmap = {}
    for a in apps:
        r = outcomes.get((key, a["body"], a["date"]))
        if r is None and a["desc"] and re.search(r"administratively withdrawn this item from the agenda|"
                                                 r"will be readvertised|postponed", a["desc"], re.I):
            r = "Continued"
        k = (a["body"], a["date"])
        h = hmap.setdefault(k, {"body": a["body"], "date": a["date"], "result": None})
        h["result"] = h["result"] or r
    for body, d, lab in staff.get("sched", []):
        if d > today and (body, d) not in hmap:
            hmap[(body, d)] = {"body": body, "date": d, "result": None}
    hearings = sorted(hmap.values(), key=lambda h: (h["date"], h["body"] != "Planning and Zoning Commission"))

    past = [h for h in hearings if h["date"] <= today]
    future = [h for h in hearings if h["date"] > today]
    decision = None
    last_cc = [h for h in past if h["body"] == "City Council" and h["result"] not in (None, "Continued")]
    withdrawn = any(h["result"] == "Withdrawn" for h in hearings) or \
        re.search(r"applicant (?:has )?withdrawn|request (?:was|has been) withdrawn", desc_all, re.I)
    if withdrawn:
        status, raw = "Withdrawn", "Withdrawn (agenda / minutes)"
    elif past and past[-1]["body"] == "City Council" and past[-1]["result"] in ("Approved", "Denied") and not future:
        status, decision = past[-1]["result"], past[-1]["date"]
        raw = f"City Council {decision}: motion to {'approve' if status == 'Approved' else 'deny'} (minutes)"
    elif past and past[-1]["result"] == "Continued":
        status, raw = "Continued", f"{past[-1]['body']} {past[-1]['date']}: tabled / continued"
        if future:
            raw += f"; next {future[0]['body']} {future[0]['date']}"
    elif future:
        status, raw = "Hearing Scheduled", f"next {future[0]['body']} {future[0]['date']}"
        if past and past[-1]["result"]:
            raw = f"{past[-1]['body']} {past[-1]['date']}: {past[-1]['result']}; " + raw
    elif past and past[-1]["result"] is None:
        status, raw = "Hearing Scheduled", f"{past[-1]['body']} {past[-1]['date']} held; outcome not yet in published minutes"
    elif past:
        status = "Filed"
        raw = f"{past[-1]['body']} {past[-1]['date']}: {past[-1]['result']}; next hearing not yet on an agenda"
    else:
        status, raw = "Filed", "On agenda"
    if status in ("Hearing Scheduled", "Filed") and last_cc and last_cc[-1]["result"] in ("Approved", "Denied") \
            and not future and past[-1]["body"] == "City Council" and past[-1]["result"] is None:
        raw += f" (earlier Council action {last_cc[-1]['date']}: {last_cc[-1]['result']})"

    # fields
    src = desc_all + " " + flat
    req = re.search(r"Request:(.+?)(?:Staff Recommendation|BACKGROUND|$)", flat, re.I)
    parcels = parcels_of(req.group(1) if req else "") or parcels_of(flat[:2500]) or parcels_of(desc_all)[:1]
    acres = None
    for pat in (r"(?:approximately|\+/\s?-)\s*([\d,]*\.?\d+)\s*(?:\+/-\s*)?acres", r"([\d,]*\.?\d+)\s*(?:\+/-|±)?\s*acres"):
        m = re.search(pat, desc_all, re.I) or re.search(pat, flat, re.I)
        if m:
            acres = to_float(m.group(1))
            if acres:
                break
    if acres is None and re.search(r"([\d,]+)\s*\+/-\s*square feet", desc_all, re.I):
        sf = to_float(re.search(r"([\d,]+)\s*\+/-\s*square feet", desc_all, re.I).group(1))
        acres = round(sf / 43560, 3) if sf else None
    from_flu = to_flu = from_z = to_z = None
    m = re.search(r"future land use designations? from (?:the )?(.+?) to (?:the )?(.+?)(?: on | and | for |\.|,)", src, re.I)
    if m:
        from_flu, to_flu = clean(m.group(1)), clean(m.group(2))
    m = re.search(r"(?:the )?zoning (?:district )?from (?:the )?(.+?) to (?:the )?(.+?)(?: on | and | for |\.|,|\s+district)", src, re.I) or \
        re.search(r"[Rr]ezone the property from (?:the )?(.+?) to (?:the )?(.+?)(?: on | zoning| and |\.)", src)
    if m:
        from_z, to_z = clean(m.group(1)), clean(m.group(2))
    from_z = clean(re.sub(r"\s+zoning(?: district)?$", "", from_z or "", flags=re.I))
    to_z = clean(re.sub(r"\s+zoning(?: district)?$", "", to_z or "", flags=re.I))
    applicant = staff.get("applicant")
    if not applicant:
        m = re.search(r"The applicant,?\s+(.+?),\s+is requesting", desc_all) or \
            re.search(r"^(.{3,120}?) (?:on behalf of .{3,80}?)?,?\s*(?:Owner,?\s*)?is requesting", desc_all)
        applicant = clean(m.group(1)) if m else None
    address = None
    m = re.search(r"located at (\d+ [A-Z0-9][A-Za-z0-9 .]+?(?:Avenue|Ave|Road|Rd|Street|St|Boulevard|Blvd|Highway|Hwy|"
                  r"Drive|Dr|Place|Pl|Court|Ct|Lane|Ln|Way|Parkway|Pkwy))\b", src)
    if m:
        address = clean(m.group(1))
    else:
        nm = (named[0]["name"] if named else "") or ""
        m = re.search(r"-\s*(\d+ [A-Za-z0-9 .]+)$", nm) or \
            re.search(r"\b(\d{2,6} [A-Z][A-Za-z .]+?(?:Avenue|Ave|Road|Rd|Street|St|Boulevard|Blvd|Highway|Hwy|Drive|Dr|Place|Pl|"
                      r"Court|Ct|Lane|Ln|Way|Parkway|Pkwy))\b", nm)
        address = clean(m.group(1)) if m else None

    if typ in ("SSA", "LSA", "CPA"):
        category = "Comp Plan Amendment"
        app_type = {"SSA": "Small Scale Comprehensive Plan Amendment (SSA) with Rezoning",
                    "LSA": "Large Scale Comprehensive Plan Amendment",
                    "CPA": "Comprehensive Plan Map Amendment (CPA)"}[typ]
        if typ == "CPA" and re.search(r"\b(PID|PUD)\b.{0,40}Rezoning|Rezoning", " ".join(a["name"] for a in named), re.I):
            app_type += " with Rezoning"
    else:
        pd = typ in ("PUD", "PID") or re.search(r"\bPUD\b|\bPID\b|Planned (?:Unit|Industrial) Development", to_z or "", re.I)
        category = "PD" if pd else "Rezoning"
        app_type = "Rezoning (REZ)" if typ == "REZ" else f"Rezoning ({typ})"
    if category == "PD" and not re.search(r"PUD|PID|Planned", app_type):
        app_type = "Rezoning (REZ) to planned development"
    nm = named[0]["name"] if named else key
    pname = None
    mm = re.search(r"\d{4}\s*[-–]\s*(.+)$", nm)
    if mm:
        pname = clean(re.sub(r"^(?:Farm Land at |)", "", mm.group(1)))
    if main_desc and len(main_desc) > 400:
        main_desc = main_desc[:397] + "..."
    first_seen = min(a["first_seen"] for a in apps)
    cands = [d for d in app_dates if d <= first_seen and d >= (dt.date.fromisoformat(first_seen) - dt.timedelta(days=540)).isoformat()]
    filed = min(cands) if cands else first_seen
    latest = (named or apps)[-1]
    packet = next((a["packet"] for a in reversed(named) if a["packet"]), None)
    return {
        "jurisdiction": JUR, "county": "Brevard", "app_num": key, "category": category, "app_type": app_type,
        "project_name": pname, "description": main_desc, "applicant": applicant, "parcel_ids": parcels,
        "address": address, "acres": acres,
        "from_zoning": from_z, "to_zoning": to_z,
        "from_flu": from_flu if category == "Comp Plan Amendment" else None,
        "to_flu": to_flu if category == "Comp Plan Amendment" else None,
        "filed_date": filed, "status": status, "status_raw": raw, "hearings": hearings,
        "decision_date": decision, "url": f"{PORTAL}/event/{latest['event_id']}/files",
        "docs_url": file_stream(packet) if packet else None,
        "lat": None, "lon": None, "source": SOURCE,
    }


if __name__ == "__main__":
    since = sys.argv[1] if len(sys.argv) > 1 else (dt.date.today() - dt.timedelta(days=14)).isoformat()
    refresh = sys.argv[2].split(",") if len(sys.argv) > 2 and sys.argv[2] else []
    t0 = time.time()
    recs = fetch(since, refresh)
    print(json.dumps(recs, indent=2, ensure_ascii=False))
    from collections import Counter
    log(f"{len(recs)} records in {time.time() - t0:.0f}s; status={dict(Counter(r['status'] for r in recs))}; "
        f"category={dict(Counter(r['category'] for r in recs))}")
