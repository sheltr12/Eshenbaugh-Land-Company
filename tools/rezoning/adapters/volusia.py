#!/usr/bin/env python3
"""Volusia County (unincorporated) rezoning adapter.

Sources (all public, no login):
 1. AMANDA citizen portal "ConnectLive" (connectlivepermits.org/citizenportal, CSDC/Granicus AMANDA,
    Angular front end over a JSON REST API). A public access token is requested per session; the
    server only accepts page configuration calls in navigation order (public-search ->
    search-advanced3 -> Detail button -> folder-detail -> info / folder-people / address-view), so
    every folder is opened by replaying that path.
      * "Folder Detail" search (group 15 Planning) by folder type RZ (Rezoning), PUD (Planned Unit
        Development: New Application / Major Amendment / Minor Amendment), SSA (Small Scale
        Amendment), LSA (Large Scale Amendment: Map / Individual FLUM / Text Amendment) and
        application date. NOTE: the search silently drops folders that have an issue date
        (approved rezonings) unless an issue-date range is sent too, so a 1900-2099 range is sent.
      * "Folder Reference" search (search-advanced8) for refresh ids (accepts the AMANDA file number
        or the case reference such as Z-26-074).
      * Folder detail (type, status, application date, description), Additional Information
        (parcel size + units, current / proposed zoning, FLU, PLDRC decision, County Council
        decision, rendition / adoption dates), People (applicant), Property (address).
 2. ArcGIS layer maps5.vcgov.org/arcgis/rest/services/CurrentProjects/MapServer/0 ("Development"):
    one polygon per OPEN development folder with ALTKEY (7-digit alternate key), PID, FOLDERRSN and
    a public deep link (FOLDERLINK) into the portal. Closed folders drop out of this layer.
 3. Published agendas (vcservices.vcgov.org): PLDRC agendas (/PLDRCAgenda/agendas/YYYYMMDD/agenda.htm)
    list every case with applicant, request, parcel numbers (####-##-##-####), acreage and a staff
    report PDF; County Council agendas (/agenda/agendas/YYYYMMDD/agenda.htm) carry the case number
    in parentheses. These give hearing dates and continuance flags.
 4. Parcel numbers from agendas / descriptions are converted to alternate keys with the county
    parcel layer maps5.vcgov.org/arcgis/rest/services/Basemap/MapServer/6 (PID -> ALTKEY).

app_num is the AMANDA file number ("26 109817 000 00 RZ"); it never changes. The case reference
("Z-26-074", assigned after the completeness check) is in `case_ref` and starts the description.
parcel_ids are Volusia alternate keys (7 digits); a parcel number that could not be resolved is
kept as the agenda shows it (####-##-##-####).
"""
import datetime as dt
import json
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

JURISDICTIONS = ["Volusia County"]
SOURCE = "Volusia AMANDA (ConnectLive) + CurrentProjects GIS + PLDRC/Council agendas"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"

REST = "https://connectlivepermits.org/citizenportal/rest/"
PORTAL = "https://connectlivepermits.org/citizenportal/app/public-search"
GIS_DEV = "https://maps5.vcgov.org/arcgis/rest/services/CurrentProjects/MapServer/0/query"
GIS_PARCELS = "https://maps5.vcgov.org/arcgis/rest/services/Basemap/MapServer/6/query"
AG = "https://vcservices.vcgov.org"
PLDRC_LIST = AG + "/PLDRCAgenda/agenda-minutes.asp"
CC_LIST = AG + "/agenda/agenda-minutes.asp"

FOLDER_TYPES = ["RZ", "PUD", "SSA", "LSA"]
SLEEP = 0.35
MAX_AGENDA_LOOKBACK_DAYS = 760

# transaction codes as read from the page configurations (re-read at run time; these are fallbacks)
TX = {
    "search3": "MhhJ1yM1QoRdLYl0pbsezy0Nsdz8ACg=",
    "search8": "MhhJ1yI+QCzFvq+XaLd1hQKUNI2f4ZI=",
    "detail": "MhhJ1yE1QvA6kjbw7itsefqXbc8pMjE=",
    "info": "MhhJ1yE0Qvn5oekvLA6kQKH8oKEg2Kg=",
    "people": "MhhJ1yo2Qla1WMjH6Fy1BPUX+ChYbuo=",
    "property": "MhhJ1yA+Qn9Vh3esnKNMo6e4PwSWwF4=",
}

_http = requests.Session()
_http.headers.update({"User-Agent": UA})


def log(*a):
    print("[volusia]", *a, file=sys.stderr, flush=True)


def http_get(url, params=None, tries=3, sleep=SLEEP, **kw):
    last = None
    for i in range(tries):
        try:
            r = _http.get(url, params=params, timeout=90, **kw)
            r.raise_for_status()
            time.sleep(sleep)
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1) ** 2)
    raise RuntimeError(f"GET failed {url}: {last}")


def arcgis(url, params):
    p = dict(params)
    p["f"] = "json"
    r = http_get(url, p)
    d = r.json()
    if "error" in d:
        raise RuntimeError(f"ArcGIS error {d['error']}")
    return d


def clean(s):
    if s is None:
        return None
    s = re.sub(r"\s+", " ", str(s).replace("\xa0", " ")).strip()
    return s or None


def mdy(d):
    return d.strftime("%m-%d-%Y")


def iso_any(s):
    s = (s or "").strip()
    if not s:
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return m.group(0)
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def columns(rows):
    """AMANDA column-major result -> list of row dicts."""
    if not isinstance(rows, list) or not rows:
        return []
    cols = {c["columnName"]: c.get("columnValues") or [] for c in rows}
    n = max(len(v) for v in cols.values())
    return [{k: (v[i] if i < len(v) else None) for k, v in cols.items()} for i in range(n)]


# ------------------------------------------------------------------ AMANDA REST client
class PageError(Exception):
    pass


class Amanda:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept": "application/json, text/plain, */*",
                               "Origin": "https://connectlivepermits.org",
                               "Referer": "https://connectlivepermits.org/citizenportal/app/public-search"})
        self.tok = None
        self.page_cfg = {}

    def start(self):
        last = None
        for i in range(3):
            try:
                r = self.s.post(REST + "securityservice/publicAccessToken/", json={"currentPage": "public-search"},
                                timeout=60)
                r.raise_for_status()
                self.tok = r.headers.get("x-auth-token")
                if not self.tok:
                    raise RuntimeError("no x-auth-token")
                time.sleep(SLEEP)
                return
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(3 * (i + 1))
        raise RuntimeError(f"AMANDA portal unreachable: {last}")

    def post(self, path, body):
        last = None
        for i in range(3):
            try:
                r = self.s.post(REST + path, json=body, headers={"Authorization": "Bearer " + (self.tok or "")},
                                timeout=90)
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(2 * (i + 1) ** 2)
                continue
            time.sleep(SLEEP)
            if r.headers.get("x-auth-token"):
                self.tok = r.headers["x-auth-token"]
            if r.status_code == 401:
                raise PageError(f"401 on {path}: {r.text[:120]}")
            if r.status_code >= 500 and "Internal Server Error" in r.text and i < 2 and "For input" not in r.text:
                last = RuntimeError(f"HTTP {r.status_code}")
                time.sleep(2 * (i + 1) ** 2)
                continue
            if r.status_code != 200:
                raise PageError(f"HTTP {r.status_code} on {path}: {r.text[:160]}")
            try:
                return json.loads(r.text, strict=False)
            except ValueError as e:
                raise PageError(f"bad JSON on {path}") from e
        raise RuntimeError(f"POST failed {path}: {last}")

    def page(self, name):
        cfg = self.post("configurationservices/pageConfiguration/", {"currentPage": name})
        self.page_cfg[name] = cfg
        return cfg

    def tx(self, code, params):
        return self.post("amandaservice/executeCustomTransaction/", {
            "transactionCode": code,
            "transactionParameters": [{"fieldName": k, "fieldValue": v} for k, v in params.items()]})

    def button(self, section, name, param, page):
        return self.post("configurationservices/buttonAction/",
                         {"section": section, "buttonAction": name, "param": param, "currentPage": page})


def _find_code(cfg, *path):
    try:
        o = cfg
        for p in path:
            o = o[p]
        return o["transactionCode"]
    except (KeyError, IndexError, TypeError):
        return None


class Portal:
    """Navigation-aware wrapper (the server enforces the page order)."""

    def __init__(self):
        self.a = Amanda()
        self.a.start()

    def restart(self):
        self.a = Amanda()
        self.a.start()

    def _to_search(self, page):
        self.a.page("public-search")
        cfg = self.a.page(page)
        sec = "folder"
        code = _find_code(cfg, sec, "dataTable", "dataLoad", "parameters")
        key = "search3" if page == "search-advanced3" else "search8"
        if code:
            TX[key] = code
        return TX[key]

    def search_folders(self, ftype, start, end):
        for attempt in range(2):
            try:
                code = self._to_search("search-advanced3")
                rows = self.a.tx(code, {
                    "folderGroupCode": "15", "folderType": ftype, "subCode": "%", "workCode": "%",
                    "inDateFrom": mdy(start), "inDateTo": mdy(end),
                    "issueDateFrom": "01-01-1900", "issueDateTo": "12-31-2099", "folderName": ""})
                return columns(rows)
            except PageError as e:
                log(f"search {ftype}: {e}; new session")
                self.restart()
        raise RuntimeError(f"folder search failed for {ftype}")

    def search_ref(self, ref):
        for attempt in range(2):
            try:
                code = self._to_search("search-advanced8")
                return columns(self.a.tx(code, {"folderRef": ref}))
            except PageError as e:
                log(f"ref search {ref}: {e}; new session")
                self.restart()
        return []

    def open_folder(self, fid, page="search-advanced3"):
        """Returns dict(detail=row, info=[rows], people=[rows], props=[rows])."""
        last = None
        for attempt in range(2):
            try:
                self.a.page("public-search")
                self.a.page(page)
                r = self.a.button("folder", "Detail", json.dumps({"folderId": fid}), page)
                if str(r.get("actionResponse")) != "1":
                    raise PageError(f"Detail action refused: {r}")
                cfg = self.a.page("folder-detail")
                code = _find_code(cfg, "main", "detailView", "dataLoad", "parameters") or TX["detail"]
                det = columns(self.a.tx(code, {"folderRSN": fid}))
                out = {"detail": det[0] if det else {}, "info": [], "people": [], "props": []}
                cfg = self.a.page("info")
                code = _find_code(cfg, "main", "initServices", 0, "parameters") or TX["info"]
                out["info"] = columns(self.a.tx(code, {"webDisplayFlag": "public", "folderRSN": fid}))
                self.a.page("folder-detail")
                cfg = self.a.page("folder-people")
                code = _find_code(cfg, "standardView", "dataTable", "dataLoad", "parameters") or TX["people"]
                out["people"] = columns(self.a.tx(code, {"folderRSN": fid}))
                self.a.page("folder-detail")
                cfg = self.a.page("address-view")
                code = _find_code(cfg, "main", "folderProperties", "dataLoad", "parameters") or TX["property"]
                out["props"] = columns(self.a.tx(code, {"folderRSN": fid}))
                return out
            except PageError as e:
                last = e
                self.restart()
        raise RuntimeError(f"open folder failed: {last}")


# ------------------------------------------------------------------ agendas
def decode_html(b):
    if b[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return b.decode("utf-16", errors="replace")
    if b[:3] == b"\xef\xbb\xbf":
        return b[3:].decode("utf-8", errors="replace")
    head = b[:3000].decode("ascii", errors="ignore").lower()
    if "utf-8" in head:
        return b.decode("utf-8", errors="replace")
    return b.decode("cp1252", errors="replace")


def agenda_dates(list_url):
    r = http_get(list_url)
    s = BeautifulSoup(decode_html(r.content), "lxml")
    sel = s.find("select", id="CCMA")
    out = []
    for o in (sel.find_all("option") if sel else []):
        v = o.get("value") or ""
        if re.fullmatch(r"\d{8}", v):
            out.append((f"{v[:4]}-{v[4:6]}-{v[6:]}", v, clean(o.get_text())))
    return out


CASE_RX = r"(?:PUD|CPA|MA|Z|SSA|LSA)-\d{2}-[A-Z]?\d{2,4}"
ENTRY_RX = re.compile(r"\b(" + r"(?:PUD|CPA|MA|Z|SSA|LSA|S|V|O|SE|ZA|CUP)-\d{2}-[A-Z]?\d{2,4}" + r")\s*[^\w\s,;:.()/]\s*")
PARCEL_RX = re.compile(r"\b(\d{4}-\d{2}-\d{2}-\d{3,4})\b")


def parse_pldrc(html, base):
    """-> {case_ref: {...}} for one PLDRC agenda."""
    s = BeautifulSoup(html, "lxml")
    text = clean(s.get_text(" ")) or ""
    out = {}
    # section boundaries
    cont_a = re.search(r"Items to be Continued or Withdrawn", text, re.I)
    cont_b = re.search(r"Old Business|New Business", text[cont_a.end():], re.I) if cont_a else None
    cont_range = (cont_a.start(), cont_a.end() + cont_b.start()) if cont_a and cont_b else \
        ((cont_a.start(), cont_a.start() + 3000) if cont_a else (-1, -1))
    ms = list(ENTRY_RX.finditer(text))
    for i, m in enumerate(ms):
        ref = m.group(1)
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        chunk = text[m.end():end]
        chunk = re.split(r"\b(?:VIII|IX|X)\.\s+(?:OTHER|STAFF|COMMISSION)", chunk)[0]
        if not re.match(r"\s*Application", chunk, re.I) and ref in out:
            continue
        e = {"text": chunk[:1500], "continued": cont_range[0] <= m.start() < cont_range[1]}
        am = re.search(r"Application\s+of\s+(.+?),?\s+requesting", chunk, re.I | re.S)
        e["applicant"] = clean(am.group(1)) if am else None
        e["parcels"] = list(dict.fromkeys(PARCEL_RX.findall(chunk)))
        acm = re.search(r"\+/-\s*([\d,]*\.?\d+)\s*(acres?|square\s+feet|sq\.?\s*ft)", chunk, re.I)
        if acm:
            v = float(acm.group(1).replace(",", ""))
            e["acres"] = round(v / 43560.0, 3) if "acre" not in acm.group(2).lower() else v
        else:
            e["acres"] = None
        lm = re.search(r"located (?:at|on)\s+(.+?);", chunk, re.I)
        e["location"] = clean(lm.group(1)) if lm else None
        out.setdefault(ref, e)
    # staff report links: "15.%20Z-26-074.pdf"
    for a in s.find_all("a"):
        h = a.get("href") or ""
        if not h.lower().endswith(".pdf") or "Supplemental" in h:
            continue
        hm = re.search(r"(" + CASE_RX + r")", requests.utils.unquote(h))
        if hm and hm.group(1) in out:
            out[hm.group(1)]["docs_url"] = requests.compat.urljoin(base, h)
    return out


def parse_council(html):
    """-> {case_ref: {"continued": bool, "withdrawn": bool, "text": str}} for one Council agenda."""
    text = clean(BeautifulSoup(html, "lxml").get_text(" ")) or ""
    out = {}
    items = [m.start() for m in re.finditer(r"\bItem\s+(?:\d{1,2}|[A-Z])\b", text)]
    for m in re.finditer(r"\((" + CASE_RX + r")\)", text):
        ref = m.group(1)
        st = max([p for p in items if p < m.start()] or [max(0, m.start() - 800)])
        chunk = text[st:m.end() + 200]
        e = out.setdefault(ref, {"continued": False, "withdrawn": False, "text": chunk[:800]})
        if re.search(r"TO BE CONTINUED|CONTINUED TO|BE DEFERRED", chunk, re.I):
            e["continued"] = True
        if re.search(r"\bWITHDRAWN\b", chunk):
            e["withdrawn"] = True
    return out


def load_agendas(start, today):
    """Hearing index {case_ref: [ {body, date, continued, withdrawn, ...} ]}."""
    idx = {}
    for body, list_url, path, parser in (("PLDRC", PLDRC_LIST, "/PLDRCAgenda/agendas/", "pldrc"),
                                          ("County Council", CC_LIST, "/agenda/agendas/", "cc")):
        try:
            dates = agenda_dates(list_url)
        except Exception as e:  # noqa: BLE001
            log(f"{body} agenda list failed: {e}")
            continue
        n = 0
        for d, v, label in dates:
            if d < start or (label and re.search(r"workshop|canceled|cancelled", label, re.I)):
                continue
            url = f"{AG}{path}{v}/agenda.htm"
            try:
                r = http_get(url, tries=2)
            except Exception as e:  # noqa: BLE001
                log(f"{body} agenda {d} failed: {e}")
                continue
            n += 1
            html = decode_html(r.content)
            parsed = parse_pldrc(html, url) if parser == "pldrc" else parse_council(html)
            for ref, e in parsed.items():
                e = dict(e)
                e.update({"body": body, "date": d, "agenda_url": url})
                idx.setdefault(ref, []).append(e)
        log(f"{body}: read {n} agendas since {start}")
    return idx


# ------------------------------------------------------------------ GIS
def gis_folders(rsns):
    out = {}
    rsns = [str(int(x)) for x in rsns if str(x).isdigit()]
    for i in range(0, len(rsns), 100):
        chunk = rsns[i:i + 100]
        try:
            d = arcgis(GIS_DEV, {"where": f"FOLDERRSN IN ({','.join(chunk)})",
                                 "outFields": "FOLDERRSN,ALTKEY,PID,FOLDERLINK,REFERENCEFILE,COUNCILDISTRICT",
                                 "returnGeometry": "true", "outSR": "4326"})
        except Exception as e:  # noqa: BLE001
            log(f"CurrentProjects GIS query failed: {e}")
            continue
        for f in d.get("features", []):
            a = f["attributes"]
            lat = lon = None
            rings = (f.get("geometry") or {}).get("rings") or []
            pts = [p for ring in rings for p in ring[:-1]] if rings else []
            if pts:
                lon = round(sum(p[0] for p in pts) / len(pts), 6)
                lat = round(sum(p[1] for p in pts) / len(pts), 6)
            out[str(int(a["FOLDERRSN"]))] = {
                "altkey": str(int(a["ALTKEY"])) if a.get("ALTKEY") else None, "pid": a.get("PID"),
                "link": a.get("FOLDERLINK"), "lat": lat, "lon": lon}
    return out


def pid_to_altkey(pids):
    """'7014-00-00-0220' / '701400000220' -> {'raw': '2235322'}."""
    norm = {}
    for p in pids:
        digits = re.sub(r"\D", "", p)
        if len(digits) == 12:
            norm[p] = digits
    out = {}
    vals = sorted(set(norm.values()))
    for i in range(0, len(vals), 50):
        chunk = vals[i:i + 50]
        try:
            d = arcgis(GIS_PARCELS, {"where": "PID IN (" + ",".join(f"'{v}'" for v in chunk) + ")",
                                     "outFields": "PID,ALTKEY", "returnGeometry": "false"})
        except Exception as e:  # noqa: BLE001
            log(f"parcel layer query failed: {e}")
            continue
        m = {f["attributes"]["PID"]: f["attributes"]["ALTKEY"] for f in d.get("features", [])}
        for raw, dig in norm.items():
            if dig in m and m[dig]:
                out[raw] = str(int(m[dig]))
    return out


# ------------------------------------------------------------------ record building
def info_map(rows):
    m = {}
    for r in rows:
        lab = clean(r.get("label") or r.get("title"))
        val = clean(r.get("value"))
        if lab and val and lab not in m:
            m[lab] = val
    return m


def iget(info, *labels):
    for l in labels:
        for k, v in info.items():
            if k.lower().strip() == l.lower():
                return v
    return None


def to_acres(val, unit):
    try:
        v = float(str(val).replace(",", ""))
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    u = (unit or "acres").lower()
    if "sq" in u or "feet" in u or "ft" in u:
        return round(v / 43560.0, 3)
    return v


def category_of(ftype, typ, subtype, desc, name):
    t = " ".join(x for x in [typ, subtype] if x).lower()
    d = (desc or "").lower() + " " + (name or "").lower()
    if ftype == "RZ":
        return "PD" if re.search(r"\bpud\b|planned unit", d) and re.search(r"\bto\b[^.]{0,60}\b[a-z]?pud\b|planned unit", d) else "Rezoning"
    if ftype == "PUD":
        if "minor" in t:
            return None
        head = ((name or "") + " " + (desc or "")[:200]).lower()
        if "development agreement" in head and not re.search(r"rezon|major amendment|pud amendment", head):
            return None   # statutory development agreement filed in a PUD folder
        return "PD"
    if ftype == "SSA":
        return "Comp Plan Amendment"
    if ftype == "LSA":
        if "text" in t or re.search(r"text amendment", d):
            return None
        return "Comp Plan Amendment"
    return None


def pldrc_result(v):
    v = (v or "").strip()
    if not v:
        return None
    if re.search(r"den", v, re.I):
        return "Recommended Denial"
    if re.search(r"approv", v, re.I):
        return "Recommended Approval"
    if re.search(r"contin|defer|table", v, re.I):
        return "Continued"
    return v


def council_result(v):
    v = (v or "").strip()
    if not v:
        return None
    if re.search(r"den", v, re.I):
        return "Denied"
    if re.search(r"approv|adopt", v, re.I):
        return "Approved"
    if re.search(r"withdr", v, re.I):
        return "Withdrawn"
    if re.search(r"contin|defer|table", v, re.I):
        return "Continued"
    if re.search(r"transmit", v, re.I):
        return "Transmitted"
    return v


def build(row, f, gis, agenda_idx, today):
    det = f["detail"] or {}
    info = info_map(f["info"])
    ftype = row["ftype"]
    ref = clean(det.get("Reference Number")) or clean(row.get("ref"))
    if ref and re.match(r"^\d{2,4} \d{6} ", ref):
        ref = None
    file_no = clean(row.get("file_no"))
    typ = clean(det.get("Type"))
    sub = clean(row.get("sub")) or (re.search(r"\(([^)]+)\)", typ or "") or [None, None])[1]
    desc = clean(det.get("[FullRow]Description"))
    desc = re.sub(r"\s*Project Manager:.*$", "", desc or "", flags=re.I).strip() or None
    name = clean(det.get("Folder Name")) or clean(row.get("name"))
    cat = category_of(ftype, typ, sub, desc, name)
    if cat is None:
        return None
    status_raw = clean(det.get("Status")) or clean(row.get("status"))
    filed = iso_any(det.get("Application Date")) or row.get("date")

    ag = agenda_idx.get(ref, []) if ref else []
    pl = [e for e in ag if e["body"] == "PLDRC"]
    pl_entry = max(pl, key=lambda e: e["date"]) if pl else None

    # ---- hearings
    hearings = {}
    for e in sorted(ag, key=lambda e: e["date"]):
        res = None
        if e.get("withdrawn"):
            res = "Withdrawn"
        elif e.get("continued"):
            res = "Withdrawn" if re.search(r"withdr", status_raw or "", re.I) and e["body"] == "PLDRC" else "Continued"
        hearings[(e["body"], e["date"])] = {"body": e["body"], "date": e["date"], "result": res}
    for lab, body in (("PLDRC Hearing Date", "PLDRC"), ("County Council Adoption Date", "County Council"),
                      ("County Council Transmittal Date", "County Council"), ("County Council Hearing Date", "County Council")):
        d = iso_any(iget(info, lab))
        if d and (body, d) not in hearings:
            hearings[(body, d)] = {"body": body, "date": d, "result": None}
    # an item that shows up again on a later agenda of the same body was continued (or remanded)
    hl = sorted(hearings.values(), key=lambda h: h["date"])
    for i, h in enumerate(hl):
        if h["result"] is None and h["date"] < today and any(x["body"] == h["body"] for x in hl[i + 1:]):
            h["result"] = "Continued"
    pdec = pldrc_result(iget(info, "PLDRC Decision"))
    cdec = council_result(iget(info, "County Council Decision"))
    for body, dec in (("PLDRC", pdec), ("County Council", cdec)):
        if not dec:
            continue
        past = sorted([h for h in hearings.values() if h["body"] == body and h["date"] <= today
                       and h["result"] not in ("Continued", "Withdrawn")], key=lambda h: h["date"])
        if past:
            past[-1]["result"] = dec
    hs = sorted(hearings.values(), key=lambda h: (h["date"], h["body"] != "PLDRC"))

    # ---- status
    raw = status_raw or ""
    future = [h for h in hs if h["date"] > today]
    past = [h for h in hs if h["date"] <= today]
    decision = None
    if re.search(r"withdr|void|cancel|expired|closed|inactive|denied by applicant", raw, re.I):
        status = "Withdrawn"
    elif re.search(r"\bdenied\b|\bdenial\b", raw, re.I) or cdec == "Denied":
        status = "Denied"
    elif re.search(r"\bapproved\b|\badopted\b|\bissued\b", raw, re.I) or cdec == "Approved":
        status = "Approved"
    elif future:
        status = "Hearing Scheduled"
    elif past and past[-1]["result"] == "Continued":
        status = "Continued"
    elif past and past[-1]["result"] is None:
        status = "Hearing Scheduled"   # heard / on agenda, result not yet posted
    else:
        status = "Filed"
    if status in ("Approved", "Denied"):
        cc = [h for h in hs if h["body"] == "County Council" and h["date"] <= today
              and h["result"] in ("Approved", "Denied")]
        decision = cc[-1]["date"] if cc else (iso_any(iget(info, "County Council Adoption Date"))
                                             or iso_any(iget(info, "Rendition Date"))
                                             or iso_any(det.get("Issue Date")))
        if not cc and decision:
            hs = hs + [{"body": "County Council", "date": decision, "result": status}] \
                if not any(h["body"] == "County Council" and h["date"] == decision for h in hs) else hs
            for h in hs:
                if h["body"] == "County Council" and h["date"] == decision and not h["result"]:
                    h["result"] = status
            hs = sorted(hs, key=lambda h: (h["date"], h["body"] != "PLDRC"))

    # ---- people / applicant
    applicant = pl_entry.get("applicant") if pl_entry else None
    if not applicant:
        for p in f["people"]:
            if re.search(r"applicant", p.get("People Role") or "", re.I):
                nm = clean(f"{p.get('First Name') or ''} {p.get('Last Name') or ''}")
                addr = clean(p.get("Address")) or ""
                org = None
                mm = re.match(r"^(?:Mr\.|Mrs\.|Ms\.|Dr\.)?\s*[^,]*,\s*([^,]{2,60}?\b(?:LLC|L\.L\.C|Inc|LLP|P\.A|Corp|Company|Group|Firm))\.?\s*,?\s+\d", addr)
                if mm and len(mm.group(1).split()) <= 7:
                    org = clean(mm.group(1))
                applicant = ", ".join(x for x in [nm, org] if x) or None
                break
    if not applicant:
        for p in f["people"]:
            if re.search(r"owner", p.get("People Role") or "", re.I):
                nm = clean(f"{p.get('First Name') or ''} {p.get('Last Name') or ''}")
                if not nm:
                    a = clean(p.get("Address")) or ""
                    nm = clean(re.sub(r"\s+\d+\s.*$", "", a.lstrip(",")))
                applicant = nm
                break

    # ---- address
    address = None
    for p in f["props"]:
        a = clean(p.get("ADDRESS"))
        if a and not re.match(r"^(NO STREET|0 |PROPERTY LOCATION)", a, re.I):
            address = ", ".join(x for x in [a, clean(p.get("CITYPROVINCE"))] if x)
            break
    if not address and pl_entry and pl_entry.get("location"):
        address = pl_entry["location"]

    # ---- parcels
    raw_parcels = []
    g = gis.get(str(row["rsn"]))
    if g and g.get("altkey"):
        raw_parcels.append(("alt", g["altkey"]))
    for e in ag:
        for p in e.get("parcels") or []:
            raw_parcels.append(("pid", p))
    for p in re.findall(r"\b(\d{12})\b", desc or ""):
        raw_parcels.append(("pid", p))

    # ---- acres
    # the PLDRC agenda acreage covers all parcels of the request ("combined"); AMANDA's parcel
    # size field is sometimes only the primary parcel, so the agenda value wins when present
    acres = pl_entry.get("acres") if pl_entry else None
    if acres is None:
        acres = to_acres(iget(info, "Parcel Size", "Size of Parcel"), iget(info, "Units of Measurement"))
    if acres is None:
        m = re.search(r"([\d,]*\.?\d+)[\s-]*(?:\+/-\s*)?acres?", desc or "", re.I)
        if m:
            acres = to_acres(m.group(1), "acres")
        else:
            m = re.search(r"([\d,]*\.?\d+)[\s-]*square[\s-]*f(?:oo|ee)t", desc or "", re.I)
            if m:
                acres = to_acres(m.group(1), "sq ft")

    # ---- zoning / FLU
    fz = iget(info, "Current Zoning")
    tz = iget(info, "Proposed Zoning")
    pud_cls = iget(info, "Proposed PUD Classification")
    if tz and pud_cls and tz.upper() == "PUD":
        tz = f"PUD ({pud_cls})"
    ff = iget(info, "Existing Land Use I", "Existing Land Use")
    tf = iget(info, "Proposed Land Use I", "Proposed Land Use", "Adopted Land Use I")
    txt = " ".join(x for x in [desc, pl_entry.get("text") if pl_entry else None] if x)
    if cat == "Comp Plan Amendment" and not (ff and tf):
        m = re.search(r"from (?:the )?(.+?)\s+(?:\(\w+\)\s*)?(?:Future Land Use|FLU|land use|designation)?\s*"
                      r"(?:\(FLU\)\s*)?(?:designation\s*)?to (?:the )?(.+?)(?:\s+(?:Future Land Use|FLU)\b|[.;,]|$)", desc or "", re.I)
        if m:
            ff, tf = ff or clean(m.group(1)), tf or clean(m.group(2))
    if cat != "Comp Plan Amendment" and not (fz and tz):
        m = re.search(r"from (?:the )?(.{2,60}?)\s+(?:zoning )?(?:classification )?to (?:the |a )?(.{2,60}?)(?:\s+zoning|[.;,]|$)", desc or "", re.I)
        if m:
            fz, tz = fz or clean(m.group(1)), tz or clean(m.group(2))
    if cat == "Comp Plan Amendment":
        fz = tz = None
        ff = ff or iget(info, "Land Use Designation")
    else:
        ff = tf = None

    d_short = desc
    if d_short and len(d_short) > 400:
        d_short = d_short[:397] + "..."
    description = f"{ref}: {d_short}" if ref and d_short else (d_short or ref)
    url = (g or {}).get("link") or (pl_entry or {}).get("docs_url") or PORTAL
    docs = None
    for e in sorted(pl, key=lambda e: e["date"], reverse=True):
        if e.get("docs_url"):
            docs = e["docs_url"]
            break
    rec = {
        "jurisdiction": "Volusia County", "county": "Volusia",
        "app_num": file_no, "category": cat,
        "app_type": typ, "project_name": name, "description": description,
        "applicant": applicant, "parcel_ids": raw_parcels, "address": address, "acres": acres,
        "from_zoning": fz, "to_zoning": tz, "from_flu": ff, "to_flu": tf,
        "filed_date": filed, "status": status, "status_raw": status_raw or None,
        "hearings": hs, "decision_date": decision if status in ("Approved", "Denied") else None,
        "url": url, "docs_url": docs,
        "lat": (g or {}).get("lat"), "lon": (g or {}).get("lon"),
        "source": SOURCE, "case_ref": ref, "folder_rsn": str(row["rsn"]),
    }
    return rec


# ------------------------------------------------------------------ fetch
def fetch(since, refresh):
    t0 = time.time()
    today = dt.date.today().isoformat()
    refresh = [r.strip() for r in (refresh or []) if r and r.strip()]
    try:
        portal = Portal()
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Volusia AMANDA portal unreachable: {e}") from e

    cands = {}   # rsn -> row
    start = dt.date.fromisoformat(since)
    end = dt.date.today()
    for ft in FOLDER_TYPES:
        try:
            rows = portal.search_folders(ft, start, end)
        except Exception as e:  # noqa: BLE001
            log(f"search {ft} failed: {e}")
            continue
        n = 0
        for r in rows:
            d = clean(r.get("Application Date"))
            rsn = clean(r.get("FolderRSN"))
            if not d or not rsn or d < since or rsn in cands:
                continue
            fn = clean(r.get("File Number"))
            cands[rsn] = {"rsn": rsn, "id": r.get("_id"), "ftype": ft, "date": d, "status": r.get("Status"),
                          "name": r.get("Folder Name"), "sub": clean(r.get("Sub Type")), "page": "search-advanced3",
                          "file_no": None, "ref": fn if fn and not re.match(r"^\d{2} \d{6} ", fn) else None,
                          "fn_shown": fn}
            n += 1
        log(f"{ft}: {n} folders filed since {since}")

    # file numbers (stable ids) for every candidate + refresh ids
    by_ref = {}
    for rsn, c in list(cands.items()):
        fn = c["fn_shown"]
        if fn and re.match(r"^\d{2} \d{6} ", fn):
            c["file_no"] = fn
        else:
            rows = portal.search_ref(fn)
            for r in rows:
                if clean(r.get("FolderRSN")) == rsn:
                    c["file_no"] = clean(r.get("File Number"))
                    c["ref"] = clean(r.get("Reference File")) or c["ref"]
                    break
        if not c["file_no"]:
            c["file_no"] = fn
        by_ref[c["file_no"]] = rsn
    for rid in refresh:
        if rid in by_ref or any(c.get("ref") == rid for c in cands.values()):
            continue
        rows = portal.search_ref(rid)
        hit = None
        for r in rows:
            if rid in (clean(r.get("File Number")), clean(r.get("Reference File"))):
                hit = r
                break
        if not hit:
            log(f"refresh id not found: {rid}")
            continue
        rsn = clean(hit.get("FolderRSN"))
        ft = clean(hit.get("Type"))
        cands[rsn] = {"rsn": rsn, "id": hit.get("_id"), "ftype": ft, "date": clean(hit.get("Date")),
                      "status": None, "name": hit.get("Folder Name"), "sub": None, "page": "search-advanced8",
                      "file_no": clean(hit.get("File Number")), "ref": clean(hit.get("Reference File")),
                      "refresh": True}
    for c in cands.values():
        if c["file_no"] in refresh or (c.get("ref") and c["ref"] in refresh):
            c["refresh"] = True
            if c.get("ref") in refresh and c["file_no"] not in refresh:
                log(f"note: refresh id {c['ref']} is a case reference; returned under file number {c['file_no']}")
    cands = {k: v for k, v in cands.items() if v["ftype"] in FOLDER_TYPES}
    log(f"{len(cands)} candidate folders")
    if not cands:
        return []

    first = min([c["date"] for c in cands.values() if c.get("date")] + [since])
    ag_start = max(first, (dt.date.today() - dt.timedelta(days=MAX_AGENDA_LOOKBACK_DAYS)).isoformat())
    agenda_idx = load_agendas(ag_start, today)
    gis = gis_folders(list(cands))

    out = []
    for rsn, c in sorted(cands.items(), key=lambda kv: kv[1].get("date") or ""):
        try:
            f = portal.open_folder(c["id"], c["page"])
            rec = build(c, f, gis, agenda_idx, today)
        except Exception as e:  # noqa: BLE001
            log(f"{c.get('file_no')}: skipped ({type(e).__name__}: {e})")
            continue
        if rec is None:
            log(f"{c.get('file_no')} ({c.get('ref')}): out of scope ({c.get('sub') or c['ftype']})")
            continue
        if not c.get("refresh") and rec["filed_date"] and rec["filed_date"] < since:
            continue
        out.append(rec)

    # parcel numbers -> alternate keys
    pids = sorted({p for r in out for k, p in r["parcel_ids"] if k == "pid"})
    pmap = pid_to_altkey(pids) if pids else {}
    for r in out:
        ids = []
        for k, p in r["parcel_ids"]:
            v = p if k == "alt" else pmap.get(p, p)
            if v not in ids:
                ids.append(v)
        # drop an unresolved PID when its alt key is already present
        r["parcel_ids"] = ids
    log(f"done in {time.time() - t0:.0f}s")
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
