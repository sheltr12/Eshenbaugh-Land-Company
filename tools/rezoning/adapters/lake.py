#!/usr/bin/env python3
"""Lake County (unincorporated) rezoning adapter.

Source: CivicClerk public API (lakecountyfl.api.civicclerk.com).
 * BCC zoning meetings carry an item "Public Hearings on Rezonings, Conditional Use Permits,
   Comprehensive Plan Amendments and Variances" whose attachments are one PDF per case
   ("Tab 6 - Ordinance 2026-XX Sunshower Estates (RZ)"). Page 1 of each PDF is the staff report
   cover sheet: case no. (PZ2026-155), hearings (PZB + BCC dates, continued/tabled flags),
   applicant, owner, requested action, PZB recommendation, size, location, alternate keys,
   FLU, current/proposed zoning. Only page 1 is read, via HTTP range requests.
 * BCC outcomes come from the draft minutes that are attached to a later BCC agenda
   ("2026-09-01_Regular_Meeting Draft"), parsed for "the Board approved Tab 3 ... PZ2026-36".
Notes: the Planning & Zoning Board agendas are not published on CivicClerk and
lakecountyfl.gov sits behind a Cloudflare challenge, so cases surface at the BCC-agenda stage.
The county's GIS "CD_Plus_AppRequets" application layer exists but stopped updating in 2021.
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

JURISDICTIONS = ["Lake County"]

API = "https://lakecountyfl.api.civicclerk.com/v1"
PORTAL = "https://lakecountyfl.portal.civicclerk.com"
SOURCE = "Lake County CivicClerk (BCC zoning agenda staff reports + minutes)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
LOOKBACK_DAYS = 120   # scan meetings this far before `since` to know when a case first appeared

_s = requests.Session()
_s.headers.update({"User-Agent": UA})

MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"


def log(*a):
    print("[lake]", *a, file=sys.stderr)


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
    """Seekable read-only file over HTTP range requests (lets pypdf read page 1 of a 17 MB packet)."""

    def __init__(self, url, chunk=512 * 1024):
        self.url, self.chunk = url, chunk
        r = _s.head(url, timeout=60)
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
            self.cache[i] = get(self.url, headers={"Range": f"bytes={a}-{b}"}, sleep=0).content
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


def pdf_text(url, pages=(0,), layout=True):
    rdr = pypdf.PdfReader(RangeFile(url))
    out = []
    for p in pages:
        if p < len(rdr.pages):
            out.append(rdr.pages[p].extract_text(extraction_mode="layout") if layout
                       else rdr.pages[p].extract_text())
    return "\n".join(out)


# ------------------------------------------------------------------ CivicClerk
def list_events(start, end):
    url = f"{API}/Events"
    params = {"$filter": f"startDateTime ge {start}T00:00:00Z and startDateTime lt {end}T00:00:00Z "
                         f"and categoryName eq 'Board of County Commissioners'",
              "$orderby": "startDateTime"}
    out = []
    while url:
        d = get(url, params).json()
        out.extend(d.get("value", []))
        url, params = d.get("@odata.nextLink"), None
    return out


def walk_items(items, parent=None):
    for it in items or []:
        yield it, parent
        yield from walk_items(it.get("childItems"), it)


SCOPE_TAB = re.compile(r"\((?=[^)]*\b(?:RZ|FLU|FLUM|CPA|SSCPA|LSCPA|CP|PUD|REZ)\b)[^)]*\)|\bPUD\b", re.I)
SKIP_TAB = re.compile(r"\((?:VAR|CUP|MCUP|MSP|CDD|DA|VAC)\)", re.I)


LABELS = [  # matched against the label with all whitespace removed (PDF text splits words)
    ("tab", r"tabnumbers?(?:\(s\))?"), ("hearings", r"publichearings?(?:dates?)?"),
    ("case", r"caseno\.andprojectname"), ("district", r"commissionerdistrict"),
    ("applicant", r"applicants?(?:\(s\))?"), ("owner", r"owners?(?:\(s\))?"),
    ("request", r"requestedaction"), ("staff", r"staffdetermination"), ("manager", r"casemanager"),
    ("pzb", r"pzbrecommendation"), ("size", r"size"), ("location", r"location"),
    ("altkey", r"alternatekey(?:no|nos|numbers?)?\.?"),
    ("proposed_flu", r"proposedfuturelanduse(?:category)?"),
    ("flu", r"(?:current)?futurelanduse(?:category)?"),
    ("proposed_zoning", r"proposedzoningdistricts?"), ("zoning", r"(?:current)?zoningdistricts?"),
    ("jpa", r"jpa/isba"), ("overlay", r"overlay/ruralprotectionarea"), ("flood", r"floodzones?(?:\(s\))?"),
    ("other", r"bmaplocation|wildlifecorridor|commercialcorridor|.*corridor"),
]
_LINE = re.compile(r"^\s*([A-Za-z][A-Za-z .()/&]{1,60}?)\s*:\s*(.*)$")


def fix_words(v):
    """Undo common PDF word splits: 'D istrict' -> 'District', '( A)' -> '(A)'."""
    v = re.sub(r"\b([A-Z])\s+(?=[a-z]{2,})", r"\1", v)
    v = re.sub(r"\(\s+", "(", v)
    v = re.sub(r"\s+\)", ")", v)
    return v


def clean(v):
    v = re.sub(r"\(\s*A\s*ttachments?[^)]*\)?", "", v or "")
    v = re.sub(r"\s+", " ", v).strip(" .,;")
    v = fix_words(v).strip(" .,;")
    return v or None


def despace(v):
    return re.sub(r"\s+", "", v or "")


def parse_cover(text):
    """Parse the two-column cover sheet of a Lake County staff report (pypdf layout mode)."""
    out, cur = {}, None
    for line in text.splitlines():
        m = _LINE.match(line)
        key = None
        if m:
            lab = despace(m.group(1)).lower()
            key = next((k for k, p in LABELS if re.fullmatch(p, lab)), None)
        if key:
            cur = key
            out.setdefault(cur, [])
            if m.group(2).strip():
                out[cur].append(m.group(2).strip())
        elif cur and line.strip() and not re.search(r"Page \d+ of \d+|STAFF REPORT|Subject\s*P\s*roperty", line):
            # continuation lines are indented into the value column
            if re.match(r"^\s{20,}\S", line):
                out[cur].append(line.strip())
            else:
                cur = None
    return {k: v for k, v in out.items() if v}


def parse_hearings(lines):
    hs = []
    for ln in lines:
        d = despace(ln)
        m = re.search(rf"({MONTHS})(\d{{1,2}}),?(\d{{4}})", d, re.I)
        if not m:
            continue
        date = dt.datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y").date().isoformat()
        if re.search(r"PZB|Planning&Zoning", d, re.I):
            body = "Planning & Zoning Board"
        else:
            body = "BOCC"
        flag = None
        if re.search(r"Continu|Postpon", d, re.I):
            flag = "Continued"
        elif re.search(r"Tabled", d, re.I):
            flag = "Continued"
        elif re.search(r"Withdr", d, re.I):
            flag = "Withdrawn"
        kind = "Transmittal" if re.search(r"Transmittal", d, re.I) else ("Adoption" if re.search(r"Adoption", d, re.I) else None)
        hs.append({"body": body, "date": date, "result": flag, "_kind": kind})
    return hs


def pzb_result(v):
    if not v:
        return None
    t = v.lower()
    if "denial" in t or "denied" in t or "deny" in t:
        return "Recommended Denial"
    if "approv" in t:
        return "Recommended Approval"
    if "continu" in t or "tabled" in t:
        return "Continued"
    return clean(v)


# ------------------------------------------------------------------ minutes
ACTION_RE = re.compile(r"the Board (approved|denied|tabled|postponed|continued|withdrew|adopted|transmitted)"
                       r"(.{0,400}?)(?:\.\s+[A-Z]|$)", re.I | re.S)
ZSTART = re.compile(r"PUBLIC HEARINGS?:\s*REZONING", re.I)
ZEND = re.compile(r"\n\s*(?:REGULAR AGENDA|OTHER BUSINESS|REPORTS\b|COUNTY ATTORNEY|COUNTY MANAGER|"
                  r"COMMISSIONER REPORTS|ADJOURNMENT)", re.I)


def parse_minutes(text, tab_to_case):
    """Return {case_no: action} from the rezoning section of draft BCC minutes."""
    t = re.sub(r"[ \t]+\d{1,2}[ \t]*\n", "\n", text)      # strip margin line numbers
    m0 = ZSTART.search(t)
    if not m0:
        return {}
    t = t[m0.end():]
    me = ZEND.search(t)
    if me:
        t = t[:me.start()]
    t = re.sub(r"\s+", " ", t)
    known = set(v for k, v in tab_to_case.items() if not k.startswith("_"))
    res = {}
    for m in ACTION_RE.finditer(t):
        verb = m.group(1).lower()
        body = m.group(2)
        cases = []
        # "Tab 3, Rezoning Case # PZ2023-232" pairs; trust the case number when it is one of ours
        for tm in re.finditer(r"Tabs?\s+(\d+(?:\s*(?:,|and|&|through|-)\s*\d+)*)(.{0,60})", body):
            nums = re.findall(r"\d+", tm.group(1))
            if re.search(r"through|-", tm.group(1)) and len(nums) == 2:
                nums = [str(x) for x in range(int(nums[0]), int(nums[1]) + 1)]
            pz = re.search(r"PZ\s?\d{4}\s?-\s?\d+", tm.group(2))
            if pz and len(nums) == 1:
                pzn = despace(pz.group(0))
                tabc = tab_to_case.get(nums[0])
                if pzn in known:
                    cases.append(pzn)
                elif tabc and tabc.startswith(pzn):   # truncated case number in minutes
                    cases.append(tabc)
                continue  # an out-of-scope case (variance, CUP) with the same tab number
            cases += [tab_to_case[n] for n in nums if n in tab_to_case]
        act = {"approved": "Approved", "adopted": "Approved", "denied": "Denied", "tabled": "Continued",
               "postponed": "Continued", "continued": "Continued", "withdrew": "Withdrawn",
               "transmitted": "Transmitted"}[verb]
        for c in cases:
            res.setdefault(c, act)
    return res


# ------------------------------------------------------------------ fetch
def fetch(since, refresh):
    if pypdf is None:
        raise RuntimeError("pypdf is required for the Lake County adapter")
    today = dt.date.today()
    start = (dt.date.fromisoformat(since) - dt.timedelta(days=LOOKBACK_DAYS)).isoformat()
    end = (today + dt.timedelta(days=90)).isoformat()
    events = list_events(start, end)
    refresh = {re.sub(r"\s", "", r).upper() for r in (refresh or []) if r}
    log(f"{len(events)} BCC events {start}..{end}")

    cases = {}         # case -> dict
    minutes = {}       # 'YYYY-MM-DD' -> pdf url
    meeting_tabs = {}  # meeting date -> {tab no: case, '_consent': [...]}
    for ev in events:
        if not ev.get("agendaId"):
            continue
        try:
            mt = get(f"{API}/Meetings/{ev['agendaId']}").json()
        except Exception as e:  # noqa: BLE001
            log(f"meeting {ev['agendaId']}: {e}")
            continue
        mdate = ev["startDateTime"][:10]
        pub = [f.get("publishOn") for f in ev.get("publishedFiles", []) if f.get("type") == "Agenda"]
        first_seen = min([p[:10] for p in pub if p] or [mdate])
        portal_url = f"{PORTAL}/event/{ev['id']}/files"
        packet = [f.get("fileId") for f in ev.get("publishedFiles", []) if f.get("type") == "Agenda Packet"]
        packet_url = (f"{API}/Meetings/GetMeetingFileStream(fileId={packet[0]},plainText=false)"
                      if packet else None)
        for it, parent in walk_items(mt.get("items")):
            for a in it.get("attachmentsList") or []:
                fn = a.get("fileName") or ""
                if re.match(r"\d{4}-\d{2}-\d{2}_", fn) and re.search(r"minute", (it.get("agendaObjectItemName") or ""), re.I):
                    minutes.setdefault(fn[:10], a.get("pdfVersionFullPath"))
                if not re.match(r"\s*Tab\s", fn, re.I):
                    continue
                if SKIP_TAB.search(fn) or not SCOPE_TAB.search(fn):
                    continue
                try:
                    txt = pdf_text(a["pdfVersionFullPath"])
                    cov = parse_cover(txt)
                    if "case" not in cov:  # some covers spill onto page 2
                        txt = pdf_text(a["pdfVersionFullPath"], pages=(0, 1))
                        cov = parse_cover(txt)
                except Exception as e:  # noqa: BLE001
                    log(f"{mdate} {fn}: PDF parse failed: {e}")
                    continue
                cm = re.search(r"PZ\d{4}-\d+", despace(" ".join(cov.get("case", []))))
                if not cm:
                    log(f"{mdate} {fn}: no case number found")
                    continue
                cno = cm.group(0)
                tabno = re.match(r"\s*Tabs?\s+([\d\s&,and]+)", fn, re.I)
                mtabs = meeting_tabs.setdefault(mdate, {})
                if tabno:
                    for n in re.findall(r"\d+", tabno.group(1)):
                        mtabs[n] = cno
                rec = cases.setdefault(cno, {"appearances": []})
                rec["appearances"].append({
                    "meeting": mdate, "first_seen": first_seen, "file": fn, "cover": cov,
                    "pdf": a.get("pdfVersionFullPath"), "portal": portal_url, "packet": packet_url,
                    "withdrawn": bool(re.search(r"WITHDRAWN", fn)),
                })
    log(f"{len(cases)} in-scope cases on BCC zoning agendas; {len(minutes)} draft minutes found")

    # outcomes from minutes
    outcomes, minutes_parsed = {}, set()
    for mdate, tabs in meeting_tabs.items():
        url = minutes.get(mdate)
        if not url or mdate > today.isoformat():
            continue
        try:
            r = get(url)
            rdr = pypdf.PdfReader(io.BytesIO(r.content))
            txt = "\n".join(p.extract_text() or "" for p in rdr.pages)
            minutes_parsed.add(mdate)
            for c, act in parse_minutes(txt, tabs).items():
                outcomes[(c, mdate)] = act
        except Exception as e:  # noqa: BLE001
            log(f"minutes {mdate}: {e}")

    out = []
    for cno, c in sorted(cases.items()):
        try:
            rec = build(cno, c, outcomes, today.isoformat(), minutes_parsed)
        except Exception as e:  # noqa: BLE001
            log(f"{cno}: skipped ({e})")
            continue
        first = min(a["first_seen"] for a in c["appearances"])
        if first < since and cno not in refresh:
            continue
        out.append(rec)
    for r in refresh - set(cases):
        log(f"refresh id {r} not on any BCC zoning agenda since {start}")
    return out


def build(cno, c, outcomes, today, minutes_parsed=()):
    apps = sorted(c["appearances"], key=lambda a: a["meeting"])
    latest = apps[-1]
    same = [a for a in apps if a["meeting"] == latest["meeting"]]   # e.g. FLU tab + RZ tab, one case
    cov = {}
    for a in same:
        for k, v in a["cover"].items():
            if k not in cov:
                cov[k] = list(v)
            elif k == "request" and v != cov[k]:
                cov[k] += v
    g = lambda k: clean(" ".join(cov.get(k, []))) if cov.get(k) else None  # noqa: E731
    fn = " ".join(a["file"] for a in same)
    request = g("request")
    kinds = []
    for a in same:
        k = re.findall(r"\(([^)]*)\)", a["file"])
        if k and k[-1] not in kinds:
            kinds.append(k[-1])
    tab_kind = " & ".join(kinds) or None
    is_flum = bool(re.search(r"\bFLUM?\b|CPA", tab_kind or "")) or \
        bool(re.search(r"Amend the Future Land Use Map|FLUM\)", request or "", re.I))
    is_pd = bool(re.search(r"Planned Unit Development|\bPUD\b", " ".join(filter(None, [
        request, g("proposed_zoning"), fn])), re.I))
    category = "Comp Plan Amendment" if is_flum else ("PD" if is_pd else "Rezoning")

    # name from case line: "PZ2026-155, Ady's Marketplace LLC" or "Voitik Property, PZ2026-188"
    caseline = g("case") or ""
    pname = clean(re.sub(r"PZ\s?\d{4}\s?-\s?\d+", "", caseline).strip(" ,")) or None
    acres = None
    for src in (" ".join(cov.get("size", [])), request):
        m = re.search(r"([\d,]*\.?\d+)\+/-(?:gross|net)?acres", despace(src), re.I) or \
            re.search(r"([\d,]*\.?\d+)(?:\+/-)?(?:gross|net)?acres", despace(src), re.I)
        if m:
            try:
                acres = float(m.group(1).replace(",", ""))
                break
            except ValueError:
                pass
    keys = re.findall(r"\b\d{7}\b", " ".join(cov.get("altkey", [])))
    if not keys:
        keys = re.findall(r"Alternate Key (?:numbers?|Nos?\.?)\s*([\d ,and]+)", request or "", re.I)
        keys = re.findall(r"\b\d{7}\b", " ".join(keys))
    from_z = g("zoning")
    to_z = g("proposed_zoning")
    if not to_z and request:
        m = re.search(r"\bto\s+([A-Z][^,.;]*?(?:District|\(PUD\)|PUD))", request)
        if m:
            to_z = clean(m.group(1))
    from_flu = g("flu")
    to_flu = g("proposed_flu")
    if is_flum and request and not to_flu:
        m = re.search(r"Future Land Use Map.*?from\s+(.+?)\s+to\s+(.+?)(?:\s+to accommodate|\s*2\.|$)", request, re.I)
        if m:
            from_flu, to_flu = clean(m.group(1)), clean(m.group(2))

    # hearings: staff-report schedule (latest report) + agenda meetings + minutes outcomes
    hmap = {}
    for a in apps:
        for h in parse_hearings(a["cover"].get("hearings", [])):
            k = (h["body"], h["date"])
            if k not in hmap or (h["result"] and not hmap[k]["result"]):
                hmap[k] = h
        hmap.setdefault(("BOCC", a["meeting"]), {"body": "BOCC", "date": a["meeting"], "result": None, "_kind": None})
        if a["withdrawn"]:
            hmap[("BOCC", a["meeting"])]["result"] = "Withdrawn"
    pzr = pzb_result(g("pzb"))
    for k, h in hmap.items():
        if h["body"] == "Planning & Zoning Board" and not h["result"] and pzr:
            h["result"] = pzr
        o = outcomes.get((cno, h["date"])) if h["body"] == "BOCC" else None
        if o:
            h["result"] = o
    hearings = sorted(hmap.values(), key=lambda h: (h["date"], h["body"] != "Planning & Zoning Board"))

    status, raw, decision = None, None, None
    bocc = [h for h in hearings if h["body"] == "BOCC"]
    past = [h for h in bocc if h["date"] <= today]
    future = [h for h in bocc if h["date"] > today]
    final = [h for h in past if h["result"] in ("Approved", "Denied") and h.get("_kind") != "Transmittal"]
    if any(h["result"] == "Withdrawn" for h in hearings):
        status, raw = "Withdrawn", "Withdrawn (agenda tab / minutes)"
    elif final:
        status, decision = final[-1]["result"], final[-1]["date"]
        raw = f"BCC {decision}: {final[-1]['result']} (draft minutes)"
    elif past and past[-1]["result"] == "Continued":
        status = "Continued"
        raw = f"BCC {past[-1]['date']}: continued/tabled" + (f"; next BCC {future[0]['date']}" if future else "")
    elif future:
        status, raw = "Hearing Scheduled", f"BCC {future[0]['date']}"
    elif past and past[-1]["date"] in minutes_parsed:
        status, raw = "Continued", (f"BCC {past[-1]['date']}: no vote recorded in draft minutes "
                                    f"(item pulled/deferred)")
    elif past:
        status, raw = "Hearing Scheduled", f"BCC hearing {past[-1]['date']} held; outcome not yet in published minutes"
    else:
        status, raw = "Filed", "On agenda"
    if pzr and status in ("Hearing Scheduled", "Continued"):
        raw += f"; PZB: {g('pzb')}"

    first_dates = [h["date"] for h in hearings] + [a["first_seen"] for a in apps]
    desc = request
    if desc and len(desc) > 300:
        desc = desc[:297] + "..."
    return {
        "jurisdiction": "Lake County", "county": "Lake", "app_num": cno, "category": category,
        "app_type": tab_kind or None, "project_name": pname, "description": desc,
        "applicant": g("applicant") or g("owner"), "parcel_ids": list(dict.fromkeys(keys)),
        "address": g("location"), "acres": acres,
        "from_zoning": from_z, "to_zoning": to_z,
        "from_flu": from_flu if is_flum else None, "to_flu": to_flu if is_flum else None,
        "filed_date": min(first_dates) if first_dates else None,
        "status": status, "status_raw": raw,
        "hearings": [{"body": h["body"], "date": h["date"], "result": h["result"]} for h in hearings],
        "decision_date": decision, "url": latest["portal"], "docs_url": latest["packet"],
        "lat": None, "lon": None, "source": SOURCE, "owner": g("owner"),
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
