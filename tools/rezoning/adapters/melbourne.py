#!/usr/bin/env python3
"""City of Melbourne (Brevard County) rezoning / PUD / future land use map amendment adapter.

Source: the city's CivicClerk public API (melbournefl.api.civicclerk.com), same platform as
clermont.py. No login.
 * Melbourne numbers every map amendment "MAPyyyy-nnnn": future land use map amendments
   ("Comprehensive Plan Amendment *Minor Amendment* (MAP2026-0011)") and zoning map amendments
   ("Zoning Map Amendment (MAP2025-0015) establishing PUD zoning") alike, so app_num = the MAP
   number and each one is its own record. Annexations (ANNX), conditional uses (CU), site plans
   (PLAN), text amendments (TEXT/FOC), preliminary development plans (PDP) and development
   agreements are skipped.
 * A case first appears on a Local Planning Agency / Planning & Zoning Board agenda (item title
   lists the MAP numbers, acreage and location), then on City Council first and second reading
   as "Ordinance No. 2026-40/MAP2025-0015: ... establishing PUD (Planned Unit Development)
   zoning on 120.65 acres". The Council parent item carries the staff memo and P&Z Board memo
   (owner / applicant, Brevard tax account numbers, location, P&Z vote per MAP number).
 * Council outcomes come from the minutes CivicClerk publishes for each meeting ("Moved by
   Neuman/Kennedy for approval of Ordinance No. 2026-15 ... Motion carried unanimously"),
   matched to MAP numbers through the "Ordinance No. X/MAPyyyy-nnnn" pairs. P&Z
   recommendations come from the P&Z Board memo and the board's minutes.
Cases surface when they first reach an agenda; filed_date = first agenda publish date.
Requires pypdf.
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

JURISDICTIONS = ["City of Melbourne"]
JUR = "City of Melbourne"
TENANT = "melbournefl"
API = f"https://{TENANT}.api.civicclerk.com/v1"
PORTAL = f"https://{TENANT}.portal.civicclerk.com"
SOURCE = "Melbourne CivicClerk (P&Z Board + City Council agendas, staff / P&Z memos, minutes)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
LOOKBACK_DAYS = 120
SMALL_PDF = 2_500_000
PZ_CAT = "Local Planning Agency/Planning & Zoning"
CC_CAT = "City Council"
PZ = "Planning and Zoning Board"
CC = "City Council"
SKIP_EVENT = re.compile(r"Cancel|Workshop|Budget|Retreat|Special Budget", re.I)
MAP_RE = re.compile(r"\bMAP\s?(\d{4})\s?-\s?(\d{4})\b")
ORDMAP_RE = re.compile(r"Ordinance\s+No\.?\s*(\d{4})-(\d{1,3})\s*/\s*MAP\s?(\d{4})\s?-\s?(\d{4})", re.I)
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


def log(*a):
    print("[melbourne]", *a, file=sys.stderr, flush=True)


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



# ------------------------------------------------------------------ case keys
def norm_map(y, n):
    y = int(y)
    if not 2000 <= y <= 2099:          # typo on agendas: "MAP5025-0014"
        y = 2000 + y % 100
    return f"MAP{y}-{n}"


def map_kinds(text):
    """[(key, kind)] for MAP numbers in text; kind 'FLU', 'ZON' or None."""
    out = []
    t = text or ""
    for m in MAP_RE.finditer(t):
        key = norm_map(m.group(1), m.group(2))
        before = t[max(0, m.start() - 70):m.start()]
        after = t[m.end():m.end() + 160]
        after1 = re.split(r"MAP\s?\d{4}|;", after)[0]
        kind = None
        if re.search(r"Future\s+Land\s+Use", after1, re.I) or re.search(r"Comprehensive\s+Plan(?:\s+Map)?\s+Amendment[^()]*\($|"
                                                                          r"Comp\w*\s+Plan[^()]{0,40}\($", before, re.I):
            kind = "FLU"
        elif re.search(r"\bzoning\b", after1, re.I) or re.search(r"Zoning[^()]{0,30}\($|Zoning[^()]{0,40}\(MAP[^()]*\)\s*and\s*\($",
                                                                 before, re.I):
            kind = "ZON"
        out.append((key, kind))
    return out


def ord_map_pairs(text):
    return {(int(a), int(b)): norm_map(c, d) for a, b, c, d in ORDMAP_RE.findall(text or "")}


def lookup_ord(pairs, y, n):
    y, n = int(y), int(n)
    for yy in (y, y - 1, y + 1):
        if (yy, n) in pairs:
            return pairs[(yy, n)]
    return None


def reading_of(name):
    m = re.search(r"\((First|Second)\s+Reading", name or "", re.I)
    return m.group(1).title() if m else None


# ------------------------------------------------------------------ outcomes
def classify_motion(verb, ok, body, reading):
    v = verb.lower()
    if re.search(r"\b(continu|table|postpone|defer)", v):
        return "Continued" if ok else None
    if re.search(r"\bden(y|ial)\b", v):
        if not ok:
            return None
        return "Recommended Denial" if body == PZ else "Denied"
    if re.search(r"transmit", v):
        return ("Recommended Approval" if body == PZ else "Transmitted") if ok else "Transmittal failed"
    if re.search(r"approv|adopt|recommend", v):
        if body == PZ:
            return "Recommended Approval" if ok else "Motion to recommend approval failed"
        if reading == "First":
            return "Approved on first reading" if ok else "Denied"
        return "Approved" if ok else "Denied"
    return None


def summary_actions(text):
    """{MAP key: (verb, ok)} from a Council 'Summary of Action' (numbered items, each ending with
    'Approved.' / 'Denied.' / 'Continued ...' / 'Items 14 a through f were approved 6-1')."""
    res = {}
    t = re.sub(r"(\d)\s*-\s*(\d)", r"\1-\2", text)
    for seg in re.split(r"\n\s*\d{1,2}\.\s+(?=[A-Z])", t):
        keys = [k for k, _ in map_kinds(seg)]
        if not keys:
            continue
        last = list(MAP_RE.finditer(seg))[-1].end()
        tail = re.sub(r"\s+", " ", seg[last:])
        m = re.search(r"\b(approved|denied|continued|tabled|postponed|withdrawn|failed)\b", tail, re.I)
        if not m:
            continue
        w = m.group(1).lower()
        verb = {"approved": "approval", "denied": "denial", "failed": "approval", "withdrawn": "withdrawn"}.get(w, "continue")
        for k in keys:
            res[k] = (verb, w != "failed")
    return res


def council_minutes(text, pairs):
    """{MAP key: (verb, ok)} from City Council minutes or Summary of Action."""
    if re.search(r"Summary of Action", text[:3000], re.I):
        return summary_actions(text)
    text = re.sub(r"(\d)\s+-\s*(\d)|(\d)\s*-\s+(\d)", lambda m: "-".join(x for x in m.groups() if x), text)
    flat = re.sub(r"\s+", " ", text)
    pairs = dict(pairs)
    pairs.update(ord_map_pairs(flat))
    res = {}
    for m in re.finditer(r"Moved by\s+.{0,60}?\s+(?:for|to)\s+(.{0,400}?)Motion (carried|failed|passed)", flat, re.I):
        verb = m.group(1)
        ok = m.group(2).lower() != "failed"
        keys = [k for k, _ in map_kinds(verb)]
        for a, b in re.findall(r"Ordinance\s+No\.?\s*(\d{4})-(\d{1,3})", verb, re.I):
            k = lookup_ord(pairs, a, b)
            if k and k not in keys:
                keys.append(k)
        for k in keys:
            res[k] = (verb, ok)
    for m in re.finditer(r"(?:withdrawn|withdrew)", flat, re.I):
        seg = flat[max(0, m.start() - 300):m.end() + 100]
        for k, _ in map_kinds(seg):
            res.setdefault(k, ("withdrawn", True))
    return res


def pz_minutes(text):
    """{MAP key: (verb, ok)} from P&Z Board minutes (sections are numbered items)."""
    res = {}
    t = re.sub(r"[ \t]+", " ", text)
    for seg in re.split(r"\n\s*\d{1,2}\.\s+(?=[A-Z])", t):
        keys = [k for k, _ in map_kinds(seg)]
        if not keys:
            continue
        flat = re.sub(r"\s+", " ", seg)
        for m in re.finditer(r"Moved\s+(?:by\s+)?[A-Z][\w.\- ]{0,40}?/[A-Z][\w.\-]{0,30}\s+(?:for|to)\s+(.{0,500}?)"
                             r"Motion (carried|failed|passed)", flat):
            verb, ok = m.group(1), m.group(2).lower() != "failed"
            own = [k for k, _ in map_kinds(verb)]
            for k in (own or keys):
                res[k] = (verb, ok)
    return res


def pz_memo_results(text):
    """[(MAP key, date, result)] from a P&Z Board memo to Council."""
    out = []
    flat = re.sub(r"\s+", " ", text or "")
    md = re.search(rf"meeting (?:of|on) ((?:{MONTHS})\s+\d{{1,2}},?\s+\d{{4}})", flat)
    d = text_date(md.group(1)) if md else None
    if not d:
        return out
    seen = set()
    for m in re.finditer(r"voted\s+([\w\-]+(?:-to-\d+)?|unanimously)\s+to\s+(recommend approval|recommend denial|approve|deny)"
                         r"(.{0,160}?)(?=voted|On [A-Z][a-z]+ \d|$)", flat):
        res = "Recommended Denial" if "den" in m.group(2) else "Recommended Approval"
        keys = [k for k, _ in map_kinds(m.group(3))]
        for k in keys:
            if k not in seen:
                out.append((k, d, res))
                seen.add(k)
    if not out:
        head = re.search(r"Re:(.{0,500}?)Date:", flat)
        g = re.search(r"voted\s+[\w\-]+\s+to\s+(recommend approval|recommend denial|approve|deny)", flat)
        if head and g:
            res = "Recommended Denial" if "den" in g.group(1) else "Recommended Approval"
            for k, _ in map_kinds(head.group(1)):
                out.append((k, d, res))
    return out


# ------------------------------------------------------------------ memo details
def memo_details(text):
    out = {}
    flat = re.sub(r"\s+", " ", text or "")
    m = re.search(r"Owners?:\s*(.+?)(?:➢|Applicants?:|Representatives?:|Proposed Actions|The Planning|Date:|$)", flat)
    if m:
        out["owner"] = clean(m.group(1))
    m = re.search(r"Applicants?:\s*(.+?)(?:➢|Representatives?:|Proposed Actions|The Planning|Owners?:|$)", flat)
    if m:
        out["applicant"] = clean(m.group(1))
    m = re.search(r"Owner\s*/\s*Applicant\s*[–:\-]\s*(.+?)(?:➢|Proposed Actions|The Planning|Location|Representative|$)", flat)
    if m:
        out["applicant"] = out.get("applicant") or clean(m.group(1))
    for k in ("owner", "applicant"):
        if out.get(k) and len(out[k]) > 200:
            out[k] = out[k][:200]
    accts = []
    for m in re.finditer(r"Tax Account\s*(?:Nos?\.?|Numbers?|#)?\s*:?\s*((?:\d{6,8}[\s,]*(?:and\s*)?)+)", flat, re.I):
        for a in re.findall(r"\d{6,8}", m.group(1)):
            if a not in accts:
                accts.append(a)
    out["accounts"] = accts
    m = re.search(r"Location\s+(?:The property|The site|The subject property) is located (.{5,250}?)(?:\s*\(Tax|\.\s+(?:Property|The|Access)|$)", flat)
    if m:
        out["location"] = clean(m.group(1))
    m = re.search(r"\b(?:located at|address(?:ed)? (?:is|of))\s+(\d{2,6} [A-Z][\w .]+?(?:Drive|Road|Street|Avenue|Boulevard|"
                  r"Parkway|Lane|Court|Way|Place|Highway|Trail|Circle))\b", flat)
    if m:
        out["address"] = clean(m.group(1))
    acres = {}
    for m in re.finditer(r"\((MAP\s?\d{4}\s?-\s?\d{4})\)[^;➢]{0,160}?([\d,]*\.?\d+)\s*±?\s*-?acres?", flat):
        k = map_kinds(m.group(1))[0][0]
        acres.setdefault(k, to_float(m.group(2)))
    out["acres"] = acres
    return out


# ------------------------------------------------------------------ fetch
def fetch(since, refresh):
    if pypdf is None:
        raise RuntimeError("pypdf is required for the Melbourne adapter")
    today = dt.date.today()
    refresh_keys = {}
    for r in refresh or []:
        ks = map_kinds(r)
        if ks:
            refresh_keys[ks[0][0]] = r
    start_d = dt.date.fromisoformat(since) - dt.timedelta(days=LOOKBACK_DAYS)
    if refresh_keys:
        yr = min(int(k[3:7]) for k in refresh_keys)
        start_d = min(start_d, max(dt.date(yr - 1, 10, 1), today - dt.timedelta(days=730)))
    end_d = today + dt.timedelta(days=75)
    try:
        events = list_events(start_d.isoformat(), end_d.isoformat())
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Melbourne CivicClerk unreachable: {e}") from e
    log(f"{len(events)} events {start_d}..{end_d}")

    cases, pairs, minutes = {}, {}, []
    for ev in events:
        cat = ev.get("categoryName") or ""
        name = ev.get("eventName") or ""
        if SKIP_EVENT.search(name):
            continue
        body = PZ if cat == PZ_CAT else (CC if cat == CC_CAT else None)
        if body is None:
            continue
        mdate = ev["startDateTime"][:10]
        files = ev.get("publishedFiles") or []
        for f in files:
            if f.get("type") == "Minutes" and f.get("fileId"):
                minutes.append((body, mdate, f["fileId"]))
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

        def visit(items, parent_atts, parent_name):
            for it in items or []:
                iname = sq(it.get("agendaObjectItemName"))
                atts = it.get("attachmentsList") or []
                kids = it.get("childItems") or []
                kinds = map_kinds(iname)
                pairs.update(ord_map_pairs(iname))
                if kinds and not re.search(r"^Approval of Minutes", iname, re.I):
                    is_child = bool(ORDMAP_RE.search(iname)) and len(kinds) == 1
                    for key, kind in kinds:
                        c = cases.setdefault(key, {"apps": [], "kinds": []})
                        if kind:
                            c["kinds"] += [kind] * (3 if is_child else 1)
                        c["apps"].append({
                            "date": mdate, "first_seen": first_seen, "body": body, "name": iname,
                            "parent": parent_name if is_child else None,
                            "reading": reading_of(iname) or reading_of(parent_name),
                            "atts": atts or (parent_atts if is_child else []), "event_id": ev.get("id"),
                            "packet": packet, "child": is_child,
                            "desc": sq(it.get("agendaObjectItemDescription")) or None,
                        })
                visit(kids, atts or parent_atts, iname)

        visit(mt.get("items"), [], None)
    log(f"{len(cases)} MAP cases on agendas; {len(minutes)} minutes files")

    outcomes = {}
    for body, mdate, fid in minutes:
        if mdate < start_d.isoformat():
            continue
        try:
            txt = pdf_text(file_stream(fid), key=f"min{fid}")
        except Exception as e:  # noqa: BLE001
            log(f"minutes {fid} ({body} {mdate}): {e}")
            continue
        got = council_minutes(txt, pairs) if body == CC else pz_minutes(txt)
        for k, v in got.items():
            outcomes[(k, body, mdate)] = v

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
        if rec and rec["filed_date"] < since and key not in refresh_keys:
            continue   # P&Z hearing (from the P&Z memo) predates the window
        if rec:
            out.append(rec)
    for k in set(refresh_keys) - set(cases):
        log(f"refresh id {refresh_keys[k]} not found on agendas since {start_d}")
    return out


def build(key, c, outcomes, today):
    apps = sorted(c["apps"], key=lambda a: (a["date"], a["body"] != PZ))
    kinds = c["kinds"]
    kind = max(set(kinds), key=kinds.count) if kinds else "ZON"

    # memos (newest Council appearance that has them)
    memo, pzres = {}, []
    for a in reversed(apps):
        if not a["atts"]:
            continue
        for x in a["atts"]:
            fn = x.get("fileName") or ""
            url = x.get("pdfVersionFullPath")
            if not url:
                continue
            try:
                if re.fullmatch(r"\s*Memo\s*", fn, re.I) and not memo:
                    memo = memo_details(pdf_text(url, x.get("fileSize") or 0, max_pages=3, key=f"att{x.get('id')}"))
                elif re.search(r"P\s*&\s*Z", fn, re.I):
                    t = pdf_text(url, x.get("fileSize") or 0, max_pages=3, key=f"att{x.get('id')}")
                    if key in [k for k, _ in map_kinds(t)]:
                        pzres += [r for r in pz_memo_results(t) if r[0] == key]
                        if not memo:
                            memo = memo_details(t)
            except Exception as e:  # noqa: BLE001
                log(f"{key}: {fn} unreadable ({e})")
        if memo:
            break

    hmap = {}
    for a in apps:
        k = (a["body"], a["date"])
        h = hmap.setdefault(k, {"body": a["body"], "date": a["date"], "result": None, "_reading": a["reading"]})
        h["_reading"] = h["_reading"] or a["reading"]
        o = outcomes.get((key, a["body"], a["date"]))
        if o and h["result"] is None:
            verb, ok = o
            h["result"] = "Withdrawn" if verb == "withdrawn" else classify_motion(verb, ok, a["body"], h["_reading"])
    for k2, d, res in pzres:
        h = hmap.setdefault((PZ, d), {"body": PZ, "date": d, "result": None, "_reading": None})
        h["result"] = h["result"] or res
    hearings = sorted(hmap.values(), key=lambda h: (h["date"], h["body"] != PZ))
    for h in hearings:
        if h["body"] == CC and h["_reading"]:
            h["body"] = f"City Council ({h['_reading'].lower()} reading)"

    past = [h for h in hearings if h["date"] <= today]
    future = [h for h in hearings if h["date"] > today]
    decision = None
    if any(h["result"] == "Withdrawn" for h in hearings) or any(re.search(r"\bwithdrawn\b", a["name"], re.I) for a in apps):
        status, raw = "Withdrawn", "Withdrawn (agenda / minutes)"
    elif past and past[-1]["body"].startswith(CC) and past[-1]["result"] in ("Approved", "Denied") and not future:
        status, decision = past[-1]["result"], past[-1]["date"]
        raw = f"{past[-1]['body']} {decision}: motion for {'approval' if status == 'Approved' else 'denial'} carried (minutes)"
    elif past and past[-1]["result"] == "Continued":
        status, raw = "Continued", f"{past[-1]['body']} {past[-1]['date']}: continued"
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

    # fields from item titles
    child = [a["name"] for a in apps if a["child"]]
    titles = child + [a["name"] for a in apps if not a["child"]] + \
        list(dict.fromkeys(a["parent"] for a in apps if a["parent"]))
    to_z = to_flu = from_z = from_flu = None
    acres = (memo.get("acres") or {}).get(key)
    for t in child:
        if kind == "ZON":
            m = re.search(r"establishing\s+(?:an?\s+)?(.+?)\s+zoning", t, re.I)
            if m and not to_z:
                to_z = clean(m.group(1))
            m = re.search(r"changing the existing\s+(.+?)\s+zoning to\s+(.+?)(?:\s+on\b|\.|$)", t, re.I)
            if m:
                from_z = from_z or clean(m.group(1))
        else:
            m = re.search(r"establishing\s+(?:an?\s+)?(.+?)\s+Future Land Use", t, re.I)
            if m and not to_flu:
                to_flu = clean(m.group(1))
        nums = [to_float(x) for x in re.findall(r"(?<![\d.])([\d,]*\.?\d+)\s*±?\s*-?acres?\b", t, re.I)]
        nums = [x for x in nums if x]
        if nums:
            acres = round(sum(nums), 2)
    if acres is None or not (to_z or to_flu):
        for t in titles:
            for m in re.finditer(r"\((?:" + re.escape(key) + r")\)\s*,?\s*(.{0,160}?)(?=;|\(MAP|$)", t.replace("MAP5025", "MAP2025")):
                seg = m.group(1)
                if acres is None:
                    mm = re.search(r"([\d,]*\.?\d+)\s*±?\s*-?acres", seg)
                    acres = to_float(mm.group(1)) if mm else None
                if kind == "ZON" and not to_z:
                    mm = re.search(r"establishing\s+(.+?)\s+zoning", seg, re.I) or re.search(r"\bto\s+(\S+(?: \([^)]*\))?)", seg)
                    to_z = clean(mm.group(1)) if mm else None
                if kind == "FLU" and not to_flu:
                    mm = re.search(r"establishing\s+(?:an?\s+)?(.+?)(?:\s+Future Land Use)?\s+on\b", seg, re.I)
                    to_flu = clean(mm.group(1)) if mm else None
    if acres is None:
        for t in titles:
            mm = re.search(r"([\d,]*\.?\d+)\s*±\s*-?acres?", t)
            if mm:
                acres = to_float(mm.group(1))
                break
    parent = next((a["parent"] for a in reversed(apps) if a["parent"]), None) or \
        next((a["name"] for a in apps if not a["child"]), None) or apps[-1]["name"]
    pname = None
    head = re.split(r":\s*\((?:First|Second|Public)", parent or "")[0]
    if head != (parent or ""):
        head = head.rsplit(")", 1)[-1]
        head = re.sub(r"(?:(?:and\s+)?(?:Ordinance|Resolution)\s+No\.?\s*\d{4}\s*-\s*\d+\s*[;,]?\s*)+", " ", head, flags=re.I)
        pname = clean(re.sub(r"^\W+", "", head))
    if kind == "FLU":
        category = "Comp Plan Amendment"
        app_type = "Comprehensive Plan Map Amendment (MAP)"
        if re.search(r"Major Amendment", " ".join(titles), re.I) and re.search(r"Major[^()]{0,30}\(" + key, " ".join(titles).replace("MAP5025", "MAP2025")):
            app_type = "Comprehensive Plan Amendment, Major (large scale)"
        elif re.search(r"Minor[^()]{0,30}\(" + key, " ".join(titles)):
            app_type = "Comprehensive Plan Amendment, Minor (small scale)"
    else:
        pd = re.search(r"\bPUD\b|Planned Unit Development", to_z or "", re.I)
        category = "PD" if pd else "Rezoning"
        app_type = "Zoning Map Amendment (MAP)" + (" to PUD" if pd else "")
    desc = re.sub(r"^Ordinance No\.?\s*\d{4}-\d+\s*/\s*MAP\S*\s*:\s*", "", child[-1]) if child else (parent or "")
    desc = clean(desc)
    if desc and len(desc) > 400:
        desc = desc[:397] + "..."
    applicant = memo.get("applicant") or memo.get("owner")
    if not applicant:
        m = re.search(r"\[(?:Owner/)?Applicant[^:]*:\s*([^\]]+)\]", " ".join(titles)) or \
            re.search(r"\((?:Owner/)?Applicant\s*-\s*([^)]+)\)", " ".join(titles))
        applicant = clean(m.group(1)) if m else None
    latest = apps[-1]
    packet = next((a["packet"] for a in reversed(apps) if a["packet"]), None)
    return {
        "jurisdiction": JUR, "county": "Brevard", "app_num": key, "category": category, "app_type": app_type,
        "project_name": pname, "description": desc, "applicant": applicant,
        "parcel_ids": memo.get("accounts") or [], "address": memo.get("address") or memo.get("location"),
        "acres": acres, "from_zoning": from_z if category != "Comp Plan Amendment" else None,
        "to_zoning": to_z if category != "Comp Plan Amendment" else None,
        "from_flu": from_flu if category == "Comp Plan Amendment" else None,
        "to_flu": to_flu if category == "Comp Plan Amendment" else None,
        "filed_date": min([a["first_seen"] for a in apps] + [h["date"] for h in hearings]), "status": status,
        "status_raw": raw,
        "hearings": [{"body": h["body"], "date": h["date"], "result": h["result"]} for h in hearings],
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
