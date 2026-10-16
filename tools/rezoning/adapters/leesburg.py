#!/usr/bin/env python3
"""City of Leesburg (Lake County) rezoning / PUD / future land use map amendment adapter.

Sources (no public case portal: the city's OPRS permit/DRC portal sits behind reCAPTCHA):
 * Planning Commission agendas + summary minutes, PDFs listed on the city website
   https://www.leesburgflorida.gov/city_agendas_and_minutes/planning_commission.php
   Each public hearing carries the case number ("CASE # RZ-26-1379 - TFS LUCAS STREET
   MULTI-FAMILY - REZONING"), acreage, from/to designations, location and the tentative City
   Commission first/second reading dates. Minutes give the PC vote ("the Planning Commission
   approved case # RZ-26-1379").
 * City Commission agendas on CivicClerk (leesburgfl.api.civicclerk.com): ordinance items under
   FIRST/SECOND READING OF ORDINANCES. Items are tied to cases through the case numbers in the
   staff recommendation / staff summary attachments (or the short project name in parentheses).
   The map attachments ("Aerial", "Zoning", ...) print the Lake County Alternate Keys
   ("AK#s: 1065895, 1088348"); the legal description sometimes prints the parcel number.
 * City Commission minutes (CivicClerk "Minutes" file) for adoption results
   ("ADOPTED ORDINANCE 26-10", "TABLED ORDINANCE ...", "WITHDRAWN ...").
Cases first appear when they reach a Planning Commission agenda (about 1 week before the
hearing). Requires pypdf.
"""
import datetime as dt
import html as _html
import io
import json
import re
import sys
import time
import urllib.parse

import requests

try:
    import pypdf
except ImportError:  # pragma: no cover
    pypdf = None

JURISDICTIONS = ["City of Leesburg"]
JUR = "City of Leesburg"
CITY = "https://www.leesburgflorida.gov"
PC_LIST = CITY + "/city_agendas_and_minutes/planning_commission.php"
TENANT = "leesburgfl"
API = f"https://{TENANT}.api.civicclerk.com/v1"
PORTAL = f"https://{TENANT}.portal.civicclerk.com"
SOURCE = "Leesburg Planning Commission agendas/minutes (city website) + City Commission CivicClerk"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
LOOKBACK_DAYS = 150
SMALL_PDF = 2_500_000

IN_PREFIX = {"SSCP", "LSCP", "CP", "CPA", "SSCPA", "LSCPA", "RZ", "REZ", "PUD", "SPUD", "PUDA", "PUDM", "MPUD"}
CPA_PREFIX = {"SSCP", "LSCP", "CP", "CPA", "SSCPA", "LSCPA"}
CASE_RE = re.compile(r"\b(SSCPA?|LSCPA?|CPA?|RZ|REZ|S?PUD[AM]?|MPUD|VAR|CUP|CU|AMDT|ANX|ANNX|SP)\s*-\s*(\d{2})\s*-\s*(\d{2,5})\b")
MONTHS = "JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER"

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


def log(*a):
    print("[leesburg]", *a, file=sys.stderr)


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


def pdf_bytes_text(content, max_pages=None):
    rdr = pypdf.PdfReader(io.BytesIO(content))
    pages = rdr.pages if max_pages is None else rdr.pages[:max_pages]
    return "\n".join((p.extract_text() or "") for p in pages)


_att_cache = {}


def att_text(att, max_pages=2):
    url = att.get("pdfVersionFullPath")
    if not url:
        return ""
    key = (att.get("id"), max_pages)
    if key not in _att_cache:
        size = att.get("fileSize") or 0
        try:
            if size > SMALL_PDF:
                rdr = pypdf.PdfReader(RangeFile(url, size))
                txt = "\n".join((p.extract_text() or "") for p in rdr.pages[:max_pages])
            else:
                txt = pdf_bytes_text(get(url).content, max_pages)
        except Exception as e:  # noqa: BLE001
            log(f"attachment {att.get('fileName')}: {e}")
            txt = ""
        _att_cache[key] = txt
    return _att_cache[key]


def file_stream(fid):
    return f"{API}/Meetings/GetMeetingFileStream(fileId={fid},plainText=false)"


def sq(s):
    return re.sub(r"\s+", " ", s or "").strip()


def norm_case(m):
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


def key_name(s):
    s = (s or "").lower().replace("multifamily", "multi family")
    s = re.sub(r"\b(rz|rezoning|sscp|lscp|cp|pud|spud|ssc?pa|annx|anx|site plan|sp)\b", " ", s)
    return re.sub(r"[^a-z0-9]", "", s)


def parse_date_words(s):
    m = re.search(rf"({MONTHS})\s+(\d{{1,2}})\s*(?:ST|ND|RD|TH)?\s*,?\s*(\d{{4}})", s.upper())
    if not m:
        return None
    try:
        return dt.datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y").date().isoformat()
    except ValueError:
        return None


# ------------------------------------------------------------------ Planning Commission
def list_pc_meetings():
    """[(date, agenda_url, minutes_url, posted_date)] from the city website listing."""
    t = get(PC_LIST).text
    out = []
    for row in re.split(r"<tr[\s>]", t):
        th = re.search(r"<th[^>]*>\s*(\d{2})/(\d{2})/(\d{2})\s*(.*?)</t[hd]>", row, re.S)
        if not th or "Planning Commission" not in th.group(4):
            continue
        d = f"20{th.group(3)}-{th.group(1)}-{th.group(2)}"
        ag = re.search(r'<a href="([^"]+)"[^>]*>\s*Agenda\s*</a>', row)
        mn = re.search(r'<a href="([^"]+)"[^>]*>\s*Minutes\s*</a>', row)

        def fix(u):
            u = _html.unescape(u)
            p = urllib.parse.urlsplit(u)
            return urllib.parse.urlunsplit((p.scheme, p.netloc, urllib.parse.quote(p.path), p.query, ""))
        posted = None
        if ag:
            pm = re.search(r"\?t=(\d{4})(\d{2})(\d{2})", ag.group(1))
            if pm:
                posted = f"{pm.group(1)}-{pm.group(2)}-{pm.group(3)}"
        out.append((d, fix(ag.group(1)) if ag else None, fix(mn.group(1)) if mn else None, posted))
    return out


def parse_pc_agenda(text):
    """Public hearing items of a PC agenda -> list of dicts."""
    t = re.sub(r"(\w) -(\w)", r"\1-\2", text)          # PDF word splits: "MULTI -FAMILY", "R -3"
    t = re.sub(r"(\w)- (\w)", r"\1-\2", t)
    t = re.sub(r"\b([A-Z]) ?-\s+(\d)", r"\1-\2", t)
    t = re.sub(r"\b(\d+) (TH|ST|ND|RD)\b", r"\1\2", t)
    t = t.replace("\u2013", "-").replace("\u2014", "-")
    t = re.sub(r"Agenda\s*-\s*Planning Commission Regular Meeting\s+Page \d+ of \d+[^\n]*\n", "\n", t)
    parts = re.split(r"\n\s*\d+\)\s*(?=PUBLIC\s+HEARING)", "\n" + t)
    items = []
    for p in parts[1:]:
        p = re.split(r"\n\s*(?:ANNOUNCEMENTS|DISCUSSION ITEMS|ADJOURNMENT|OLD BUSINESS)\b", p)[0]
        cm = CASE_RE.search(p[:200])
        if not cm:
            continue
        case = norm_case(cm)
        head = sq(p[cm.end():cm.end() + 250])
        hm = re.match(r"\s*-\s*(.+?)\s+-\s+([A-Z][A-Z /&]+?)\s+(?:AN ORDINANCE|A REQUEST|A RESOLUTION|$)", head)
        name = sq(hm.group(1)) if hm else None
        typ = sq(hm.group(2)) if hm else None
        body = sq(p)
        items.append({"case": case, "prefix": cm.group(1), "name": name, "type": typ, "text": body})
    return items


def fields_from_title(text):
    t = sq(text).upper().replace("+ / -", "+/-").replace("+/ -", "+/-")
    out = {}
    m = re.search(r"([\d,]*\.?\d+)\s*\+/-\s*ACRES", t) or re.search(r"([\d,]*\.?\d+)\s*ACRES", t)
    if m:
        try:
            out["acres"] = float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    m = re.search(r"\bFROM\s+(.+?)\s+TO\s+(.+?)(?:,|\s+TO ALLOW|\s+TO APPLY|\s+FOR A PROPERTY|\s+FOR PROPERTY|;)", t)
    if m:
        out["from"], out["to"] = sq(m.group(1)), sq(m.group(2))
    m = re.search(r"GENERALLY LOCATED\s+(.+?)(?:,\s*AS LEGALLY|,\s*LYING|\s+AS LEGALLY|;|\.\s)", t)
    if m:
        out["location"] = sq(m.group(1))
    m = re.search(r"FIRST READING ON\s+(?:[A-Z]+,\s*)?(" + MONTHS + r")\s+(\d{1,2}),\s*(\d{4})", t)
    if m:
        out["cc1"] = parse_date_words(" ".join(m.groups()))
    m = re.search(r"SECOND READING (?:IS )?(?:TENTATIVELY )?(?:SCHEDULED )?(?:FOR|ON)\s+(?:[A-Z]+,\s*)?(" + MONTHS
                  + r")\s+(\d{1,2}),\s*(\d{4})", t)
    if m:
        out["cc2"] = parse_date_words(" ".join(m.groups()))
    sn = re.findall(r"\(([^()]{3,80})\)\s*\.?\s*$", sq(text))
    if sn:
        out["short"] = sn[-1].strip()
    return out


def pc_results(text):
    """{case: result} from PC summary minutes (the last successful motion on a case wins)."""
    d = re.sub(r"\s+", "", text).upper().replace("\u2019", "'")
    found = []
    for m in re.finditer(r"PLANNINGCOMMISSION(APPROVED|DENIED|RECOMMENDEDAPPROVAL(?:OF)?|RECOMMENDEDDENIAL(?:OF)?|"
                         r"TABLED|CONTINUED|POSTPONED)CASE#?(?:NUMBER)?([A-Z]+-\d{2}-\d+)", d):
        v = m.group(1)
        found.append((m.start(), m.group(2), "Recommended Approval" if "APPROV" in v else
                      "Recommended Denial" if "DEN" in v else "Continued"))
    for m in re.finditer(r"MOTIONTO(APPROVE|DENY|TABLE|CONTINUE|POSTPONE|RECOMMEND)(STAFF'?S?RECOMMENDATIONOF)?"
                         r"(APPROVAL|DENIAL)?(?:FOR|OF)?CASE#?(?:NUMBER)?([A-Z]+-\d{2}-\d+)(.{0,500}?)"
                         r"WHICH(?:WAS)?(APPROVED|PASSED|CARRIED|FAILED|DENIED)", d):
        if m.group(6) in ("FAILED", "DENIED"):
            continue
        v, rec = m.group(1), m.group(3)
        if v in ("TABLE", "CONTINUE", "POSTPONE"):
            r = "Continued"
        elif v == "DENY" or rec == "DENIAL":
            r = "Recommended Denial"
        else:
            r = "Recommended Approval"
        found.append((m.start(), m.group(4), r))
    for m in re.finditer(r"CASE#?([A-Z]+-\d{2}-\d+)[^#]{0,80}?(?:WASWITHDRAWN|HASBEENWITHDRAWN)", d):
        found.append((m.start(), m.group(1), "Withdrawn"))
    res = {}
    for _, case, r in sorted(found):
        res[case] = r
    return res


# ------------------------------------------------------------------ City Commission (CivicClerk)
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


def walk(items, parent=None):
    for it in items or []:
        yield it, parent
        yield from walk(it.get("childItems"), it)


def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = _html.unescape(s).replace("\u00a0", " ").replace("\u200b", "")
    return sq(s)


def cc_kind(title):
    t = title.lower()
    if not t.startswith(("an ordinance", "ordinance")):
        return None
    if re.search(r"annexing|development agreement|franchise|community development district|amending chapter|"
                 r"code of ordinances|pension|taxing unit|sign regulations", t) and \
            not re.search(r"future land use map|changing the zoning|major modification", t):
        return None
    if re.search(r"future land use", t):
        return "CPA"
    if re.search(r"changing the zoning|rezoning|planned unit development|major modification", t):
        return "RZ"
    return None


def alt_keys_text(t):
    keys = []
    for m in re.finditer(r"(?:AK\s*#?\s*s?|Alt(?:ernate)?\.?\s*Keys?(?:\s*(?:#|No\.?|Numbers?)s?)?)\s*:\s*", t, re.I):
        seg = re.split(r"[A-Za-z]{3,}", t[m.end(): m.end() + 400])[0]
        keys += re.findall(r"\b\d{7}\b", seg)
    return list(dict.fromkeys(keys))


def parcel_ids_text(t):
    return list(dict.fromkeys(re.findall(r"\b\d{2}-\d{2}-\d{2}-\d{4}-\d{3}-\d{5}\b", t)))


def cc_minutes_results(text):
    """[(short_name_key, word, ord_no)] from City Commission minutes."""
    out = []
    for m in re.finditer(r"\n\s*(ADOPTED|TABLED|WITHDRAWN|DENIED|FAILED|POSTPONED|CONTINUED|PASSED)\s+(?:ON\s+FIRST\s+READING\s+)?"
                         r"(?:ORDINANCES?|ORDINANCE)\s*(\d{2}-\d+)?", text):
        pre = text[max(0, m.start() - 700): m.start()]
        sn = re.findall(r"\(([^()]{3,80})\)\s*\.?\s*$", sq(pre))
        if sn:
            out.append((key_name(sn[-1]), m.group(1), m.group(2)))
    return out


# ------------------------------------------------------------------ fetch
def fetch(since, refresh):
    if pypdf is None:
        raise RuntimeError("pypdf is required for the Leesburg adapter")
    today = dt.date.today()
    refresh = {re.sub(r"\s", "", r).upper() for r in (refresh or []) if r}
    start_d = dt.date.fromisoformat(since) - dt.timedelta(days=LOOKBACK_DAYS)
    for r in refresh:
        m = re.search(r"-(\d{2})-", r)
        if m:
            start_d = min(start_d, max(dt.date(2000 + int(m.group(1)), 1, 1) - dt.timedelta(days=60),
                                       today - dt.timedelta(days=730)))
    start, end = start_d.isoformat(), (today + dt.timedelta(days=75)).isoformat()

    cases = {}

    def case_rec(case):
        return cases.setdefault(case, {"pc": [], "cc": [], "short": set(), "names": []})

    # Planning Commission
    try:
        pcs = list_pc_meetings()
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Leesburg PC agenda listing unreachable: {e}")
    pcs = [p for p in pcs if start <= p[0] <= end]
    log(f"{len(pcs)} Planning Commission meetings {start}..{end}")
    for d, ag, mn, posted in sorted(pcs):
        if not ag:
            continue
        try:
            items = parse_pc_agenda(pdf_bytes_text(get(ag).content))
        except Exception as e:  # noqa: BLE001
            log(f"PC agenda {d}: {e}")
            continue
        res = {}
        if mn and d <= today.isoformat():
            try:
                res = pc_results(pdf_bytes_text(get(mn).content))
            except Exception as e:  # noqa: BLE001
                log(f"PC minutes {d}: {e}")
        for it in items:
            if it["prefix"] not in IN_PREFIX:
                continue
            rec = case_rec(it["case"])
            f = fields_from_title(it["text"])
            rec["pc"].append({"date": d, "posted": posted or d, "url": ag, "minutes": mn, "item": it,
                              "fields": f, "result": res.get(it["case"])})
            if f.get("short"):
                rec["short"].add(key_name(f["short"]))
            if it["name"]:
                rec["short"].add(key_name(it["name"]))

    # City Commission
    events = [e for e in list_events(start, end)
              if e.get("agendaId") and re.search(r"Commission", (e.get("categoryName") or "") + (e.get("eventName") or ""))
              and not re.search(r"Workshop|Budget|Millage|CRA|Redevelopment", e.get("eventName") or "")]
    log(f"{len(events)} City Commission meetings")
    minutes_cache = {}
    for ev in events:
        try:
            mt = get(f"{API}/Meetings/{ev['agendaId']}").json()
        except Exception as e:  # noqa: BLE001
            log(f"meeting {ev['agendaId']}: {e}")
            continue
        mdate = ev["startDateTime"][:10]
        files = ev.get("publishedFiles") or []
        packet = next((f.get("fileId") for f in files if f.get("type") == "Agenda Packet"), None)
        mins = next((f.get("fileId") for f in files if f.get("type") == "Minutes"), None)
        for it, parent in walk(mt.get("items")):
            title = strip_html(it.get("agendaObjectItemName"))
            kind = cc_kind(title)
            if not kind:
                continue
            ptitle = strip_html((parent or {}).get("agendaObjectItemName")).upper()
            reading = "first reading" if "FIRST" in ptitle else ("second reading" if "SECOND" in ptitle else None)
            atts = it.get("attachmentsList") or []
            short = (re.findall(r"\(([^()]{3,80})\)\s*\.?\s*$", title) or [None])[-1]
            # case number: attachment names, then staff recommendation / summary text
            cands = []
            for a in atts:
                cands += [norm_case(m) for m in CASE_RE.finditer(a.get("fileName") or "")]
            if not cands:
                for a in atts:
                    if re.search(r"Staff\s*Rec|Staff\s*Summ", a.get("fileName") or "", re.I):
                        cands += [norm_case(m) for m in CASE_RE.finditer(att_text(a, 1))]
                        if any(c.split("-")[0] in (CPA_PREFIX if kind == "CPA" else IN_PREFIX - CPA_PREFIX) for c in cands):
                            break
            want = CPA_PREFIX if kind == "CPA" else (IN_PREFIX - CPA_PREFIX)
            cands = [c for c in dict.fromkeys(cands) if c.split("-")[0] in want]
            case = cands[0] if len(cands) >= 1 else None
            if not case and short:
                k = key_name(short)
                for cno, rec in cases.items():
                    if k and k in rec["short"] and (cno.split("-")[0] in want):
                        case = cno
                        break
            if not case:
                case = short or title[:60]
                log(f"{mdate}: no case number for '{case}', using the agenda short name")
            rec = case_rec(case)
            if short:
                rec["short"].add(key_name(short))
            rec["cc"].append({"date": mdate, "reading": reading, "title": title, "kind": kind, "atts": atts,
                              "event_id": ev.get("id"), "packet": packet, "minutes": mins, "short": short,
                              "posted": min([f.get("publishOn")[:10] for f in files
                                             if f.get("type") == "Agenda" and f.get("publishOn")] + [mdate])})
            if mins and mins not in minutes_cache:
                try:
                    minutes_cache[mins] = cc_minutes_results(pdf_bytes_text(get(file_stream(mins)).content))
                except Exception as e:  # noqa: BLE001
                    log(f"minutes {mdate}: {e}")
                    minutes_cache[mins] = []
    log(f"{len(cases)} in-scope cases")

    out = []
    for cno, c in sorted(cases.items()):
        firsts = [p["posted"] for p in c["pc"]] + [x["posted"] for x in c["cc"]]
        if not firsts:
            continue
        if min(firsts) < since and re.sub(r"\s", "", cno).upper() not in refresh:
            continue
        try:
            out.append(build(cno, c, minutes_cache, today.isoformat()))
        except Exception as e:  # noqa: BLE001
            log(f"{cno}: skipped ({e})")
    for r in refresh - {re.sub(r"\s", "", k).upper() for k in cases}:
        log(f"refresh id {r} not found since {start}")
    return out


GIS = "https://maps.leesburgflorida.gov/arcgis/rest/services/Planning_Zoning/P_Z_Layers/MapServer"


def gis_adoption(kind, keys, first_cc_date):
    """(ord_no, year, month) when the city zoning (RZ) / FLU (CPA) layer shows these Alternate Keys changed
    by an ordinance dated on/after the first Commission hearing, else None."""
    layer, ordf, yf, mf = (0, "ORD", "YEAR", "MONTH") if kind == "CPA" else (1, "ORD_NO", "Year", "Month")
    y0, m0 = int(first_cc_date[:4]), int(first_cc_date[5:7])
    ks = ",".join(f"'{k}'" for k in keys[:50])
    where = f"AltKey IN ({ks}) AND ({yf} > {y0} OR ({yf} = {y0} AND {mf} >= {m0}))"
    try:
        d = get(f"{GIS}/{layer}/query", params={"where": where, "outFields": f"{ordf},{yf},{mf}",
                                                "returnGeometry": "false", "f": "json"}).json()
    except Exception as e:  # noqa: BLE001
        log(f"GIS lookup failed: {e}")
        return None
    hits = [f["attributes"] for f in d.get("features", []) if f["attributes"].get(ordf)]
    if not hits:
        return None
    a = max(hits, key=lambda a: (a[yf] or 0, a[mf] or 0))
    return a[ordf], int(a[yf]), int(a[mf])


def build(cno, c, minutes_cache, today):
    pcs = sorted(c["pc"], key=lambda x: x["date"])
    ccs = sorted(c["cc"], key=lambda x: x["date"])
    f = {}
    for p in pcs:
        for k, v in p["fields"].items():
            f.setdefault(k, v)
    cc_f = fields_from_title(ccs[-1]["title"]) if ccs else {}
    for k, v in cc_f.items():
        if k not in ("cc1", "cc2"):
            f.setdefault(k, v)
    prefix = cno.split("-")[0] if CASE_RE.match(cno) else None
    kind = "CPA" if (prefix in CPA_PREFIX or (not prefix and ccs and ccs[-1]["kind"] == "CPA")) else "RZ"
    to_ = f.get("to")
    is_pd = bool(prefix and "PUD" in prefix) or bool(re.search(r"\bPUD\b|PLANNED UNIT", to_ or "")) or \
        bool(ccs and re.search(r"MAJOR MODIFICATION", ccs[-1]["title"].upper()))
    category = "Comp Plan Amendment" if kind == "CPA" else ("PD" if is_pd else "Rezoning")
    item = pcs[0]["item"] if pcs else None
    app_type = (item["type"].title() if item and item["type"] else None)
    if not app_type and ccs and re.search(r"major modification", ccs[-1]["title"], re.I):
        app_type = "PUD Major Modification"
    app_type = app_type or prefix or ("Future Land Use Map Amendment" if kind == "CPA" else "Rezoning")
    shorts = [x["short"] for x in ccs if x["short"]]
    pname = None
    if shorts:
        pname = re.sub(r"\s+(RZ|SSCP|LSCP|CP|PUD|SPUD|ANNX)$", "", shorts[0]).strip() or None
    if not pname and item and item["name"]:
        pname = item["name"]
    desc_src = item["text"] if item else (ccs[-1]["title"] if ccs else "")
    desc = re.sub(r"^.*?(AN ORDINANCE|A REQUEST)", r"\1", desc_src)
    desc = re.sub(r"\(CITY COMMISSION DATES.*$", "", desc).strip()
    if len(desc) > 300:
        desc = desc[:297] + "..."

    # documents from the latest Commission appearance
    keys, pids, applicant = [], [], None
    for x in reversed(ccs):
        for a in x["atts"]:
            fn = a.get("fileName") or ""
            if not keys and re.search(r"Aerial|Zoning|Future Land Use|FLU", fn, re.I):
                keys = alt_keys_text(att_text(a, 1))
            if not pids and re.search(r"LEGAL", fn, re.I):
                pids = parcel_ids_text(att_text(a, 2))
            if not applicant and re.search(r"Staff\s*(Rec|Summ)", fn, re.I):
                t = att_text(a, 1)
                m = re.search(r"PETITIONER:\s*(.+?)\s*(?:\n|PROJECT:)", t)
                m2 = re.search(r"OWNERS?:\s*(.+?)\s*(?:\n\s*[A-Z]{4,}:|PETITIONER:)", t, re.S)
                applicant = sq(m.group(1)) if m else (sq(m2.group(1)) if m2 else None)
        if keys:
            break
    parcel_ids = keys or pids

    # hearings (an item that comes back for the same reading at a later meeting was tabled at the earlier one)
    for i, x in enumerate(ccs):
        x["_again"] = any(y["reading"] == x["reading"] and y["date"] > x["date"] for y in ccs[i + 1:])
    hearings = []
    for p in pcs:
        hearings.append({"body": "Planning Commission", "date": p["date"], "result": p["result"]})
    seen_cc = set()
    ord_no = None
    decision = None
    for x in ccs:
        res = None
        k = key_name(x["short"]) if x["short"] else None
        for (sk, word, ono) in minutes_cache.get(x["minutes"], []) if x["minutes"] else []:
            if k and sk == k:
                res = {"ADOPTED": "Approved", "PASSED": "Approved", "TABLED": "Continued", "POSTPONED": "Continued",
                       "CONTINUED": "Continued", "WITHDRAWN": "Withdrawn", "DENIED": "Denied", "FAILED": "Denied"}[word]
                if x["reading"] == "first reading" and res == "Approved":
                    res = "Approved (first reading)"
                if ono and word == "ADOPTED":
                    ord_no = ono
                break
        if res is None and x["_again"] and x["date"] <= today:
            res = "Continued"
        body = "City Commission" + (f" ({x['reading']})" if x["reading"] else "")
        hearings.append({"body": body, "date": x["date"], "result": res})
        seen_cc.add(x["date"])
    if pcs:   # tentative Commission dates printed on the PC agenda
        for key, lab in (("cc1", "first reading"), ("cc2", "second reading")):
            d = pcs[-1]["fields"].get(key)
            if d and d > today and d not in seen_cc:
                hearings.append({"body": f"City Commission ({lab}, tentative)", "date": d, "result": None})
    hearings.sort(key=lambda h: h["date"])

    # adoption seen in the city's zoning / future land use GIS layers (they carry ORD_NO + AltKey)
    gis_note = None
    cc_past = [h for h in hearings if h["body"].startswith("City Commission") and h["date"] <= today
               and "tentative" not in h["body"]]
    if keys and cc_past and not any(h["result"] in ("Approved", "Denied", "Withdrawn") for h in cc_past):
        hit = gis_adoption(kind, keys, cc_past[0]["date"])
        if hit:
            ono, yr, mo = hit
            ord_no = ono
            ym = f"{yr:04d}-{mo:02d}"
            same = [h for h in cc_past if h["date"][:7] <= ym and "second" in h["body"]] or \
                   [h for h in cc_past if h["date"][:7] <= ym]
            if same:
                same[-1]["result"] = "Approved"
            gis_note = f"adopted as Ordinance {ono} per City {'FLU' if kind == 'CPA' else 'zoning'} GIS layer ({ym})"

    past = [h for h in hearings if h["date"] <= today]
    future = [h for h in hearings if h["date"] > today]
    final = [h for h in past if h["result"] in ("Approved", "Denied") and h["body"].startswith("City Commission")]
    if any(h["result"] == "Withdrawn" for h in hearings):
        status, raw = "Withdrawn", "Withdrawn (minutes)"
    elif final:
        status, decision = final[-1]["result"], final[-1]["date"]
        raw = gis_note or (f"City Commission {decision}: " + (f"ADOPTED ORDINANCE {ord_no}" if ord_no
                                                               else final[-1]["result"]))
    elif future:
        status, raw = "Hearing Scheduled", f"{future[0]['body']} {future[0]['date']}"
    elif past and past[-1]["result"] == "Continued":
        status, raw = "Continued", f"{past[-1]['body']} {past[-1]['date']}: tabled/continued"
    elif past:
        status = "Hearing Scheduled" if past[-1]["body"].startswith("City Commission") and past[-1]["result"] is None \
            else "Filed"
        raw = f"{past[-1]['body']} {past[-1]['date']}" + (f": {past[-1]['result']}" if past[-1]["result"] else "") + \
            ("; outcome not yet in published minutes" if status == "Hearing Scheduled" else "; awaiting City Commission")
    else:
        status, raw = "Filed", "On Planning Commission agenda"

    filed = min([p["posted"] for p in pcs] + [x["posted"] for x in ccs])
    if ccs:
        url = f"{PORTAL}/event/{ccs[-1]['event_id']}/files"
        docs = file_stream(ccs[-1]["packet"]) if ccs[-1]["packet"] else None
    else:
        url, docs = pcs[-1]["url"], pcs[-1]["url"]
    return {
        "jurisdiction": JUR, "county": "Lake", "app_num": cno, "category": category, "app_type": app_type,
        "project_name": pname, "description": desc or None, "applicant": applicant, "parcel_ids": parcel_ids,
        "address": f.get("location"), "acres": f.get("acres"),
        "from_zoning": f.get("from") if kind == "RZ" else None, "to_zoning": f.get("to") if kind == "RZ" else None,
        "from_flu": f.get("from") if kind == "CPA" else None, "to_flu": f.get("to") if kind == "CPA" else None,
        "filed_date": filed, "status": status, "status_raw": raw, "hearings": hearings,
        "decision_date": decision, "url": url, "docs_url": docs, "lat": None, "lon": None, "source": SOURCE,
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
