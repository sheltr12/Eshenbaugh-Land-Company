"""Shared scraper for Accela Citizen Access (ACA) public portals (aca-prod.accela.com).

ACA is an ASP.NET WebForms app. The flow used here:
  1. GET  Cap/CapHome.aspx?module=<Module>&TabName=<Module>   -> search form (+ cookies, __VIEWSTATE ...)
  2. POST same URL with all hidden fields, record type + date range, __EVENTTARGET=btnNewSearch
     (Origin + Referer headers are REQUIRED, otherwise ACA answers with a CSRF error page)
  3. Parse the results grid (..._gdvPermitList); page through with the grid's "Next >" __doPostBack.
     A search that matches exactly one record redirects straight to CapDetail.aspx.
  4. GET each record's CapDetail.aspx: header (number, type, status), Application Location,
     Record Details (applicant, project description, owner), More Details -> Related Contacts,
     Application Information (ASI), Application Information Table (ASIT), Parcel Information.
  5. POST CapDetail.aspx/GetProcessingData (ASP.NET PageMethod, JSON) in the same session to get
     the "Processing Status" workflow (task name, due date, "Marked as <status> on <date>",
     comments). Hearing tasks there give hearings + outcomes.

Jurisdiction modules supply a config dict and a `classify(rec)` hook; see make_fetch().
Only stdlib + requests + bs4 + lxml.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
import time
from typing import Callable, Iterable

import requests
from bs4 import BeautifulSoup

HOST = "https://aca-prod.accela.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36 ELC-RezoningTracker/1.0")
GS = "ctl00$PlaceHolderMain$generalSearchForm$"
GRID_ID = "ctl00_PlaceHolderMain_dgvPermitList_gdvPermitList"
SLEEP = 0.5


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ----------------------------------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------------------------------
def clean(s) -> str | None:
    if s is None:
        return None
    s = re.sub(r"[ \t\r\f\v\xa0]+", " ", str(s))
    s = re.sub(r"\s*\n\s*", "\n", s).strip()
    return s or None


def one_line(s) -> str | None:
    s = clean(s)
    return re.sub(r"\s+", " ", s) if s else None


def iso(d) -> str | None:
    """'09/24/2026' / '9/24/26' / '2026-09-24' -> '2026-09-24'."""
    if not d:
        return None
    d = str(d).strip()
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", d)
    if m:
        mo, da, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if yr < 100:
            yr += 2000
        try:
            return dt.date(yr, mo, da).isoformat()
        except ValueError:
            return None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", d)
    if m:
        return m.group(0)
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y"):
        try:
            return dt.datetime.strptime(d, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def mdy(d: dt.date) -> str:
    return d.strftime("%m/%d/%Y")


def to_float(s) -> float | None:
    if s is None:
        return None
    m = re.search(r"(\d[\d,]*\.?\d*|\.\d+)", str(s))
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return v if v > 0 else None


# ----------------------------------------------------------------------------------------------
# status normalization (spec list)
# ----------------------------------------------------------------------------------------------
_ST_WITHDRAWN = re.compile(r"withdr|\bvoid|cancel|expired|abandon|incomplete.*clos|clos.*incomplete|"
                           r"inactiv|terminat|null and void|not accepted|closed - no action|"
                           r"closed-no|deemed withdrawn", re.I)
_ST_DENIED = re.compile(r"\bden(y|ied|ial)\b|disapprov|not approved|rejected", re.I)
_ST_APPROVED = re.compile(r"approv|adopt|granted|ordinance|final action|enacted", re.I)
_NOT_FINAL = re.compile(r"recommend|pending|await|ready|prior to|for hearing|for public hearing|"
                        r"\bpc\b|planning commission|hearing pc|admin|plans approved|staff|"
                        r"sufficien|in progress|transmit", re.I)
_ST_CONTINUED = re.compile(r"contin|defer|postpone|tabled|reschedul", re.I)


def norm_status(raw: str | None, hearings: list[dict] | None = None, today: str | None = None,
                final_bodies: Iterable[str] = ("BOCC", "City Council"),
                raw_map: dict | None = None) -> str:
    """Normalize to Filed / Hearing Scheduled / Continued / Approved / Denied / Withdrawn.
    Order: jurisdiction raw_map override > explicit terminal record status > final-body hearing
    result > future hearing > continued > raw 'hearing scheduled' > Filed.
    raw_map: {regex: normalized or None}; None means "decide from hearings, default Filed";
    the special value 'Hearing?' means Hearing Scheduled only if a future hearing date is known;
    'Hearing!' means Hearing Scheduled unless the deciding body's outcome is already recorded."""
    today = today or dt.date.today().isoformat()
    r = (raw or "").strip()
    hearings = hearings or []
    finals = sorted([h for h in hearings if h.get("body") in final_bodies and h.get("date") and h.get("result")
                     and h["date"] <= today], key=lambda h: h["date"])
    final_res = None
    if finals:
        res = finals[-1]["result"]
        if _ST_DENIED.search(res) and not re.search(r"recommend", res, re.I):
            final_res = "Denied"
        elif _ST_WITHDRAWN.search(res):
            final_res = "Withdrawn"
        elif _ST_APPROVED.search(res) and not re.search(r"recommend|for hearing", res, re.I):
            final_res = "Approved"
        elif _ST_CONTINUED.search(res):
            final_res = "Continued"
    future = sorted([h for h in hearings if h.get("date") and h["date"] >= today], key=lambda h: h["date"])
    past = sorted([h for h in hearings if h.get("date") and h["date"] < today], key=lambda h: h["date"])

    mapped = "_none_"
    for rx, val in (raw_map or {}).items():
        if r and re.search(rx, r, re.I):
            mapped = val
            break
    if mapped not in ("_none_", None, "Hearing?", "Hearing!"):
        return mapped
    if mapped == "_none_" and r:
        if _ST_WITHDRAWN.search(r):
            return "Withdrawn"
        if _ST_DENIED.search(r) and not re.search(r"recommend", r, re.I):
            return "Denied"
        if _ST_APPROVED.search(r) and not _NOT_FINAL.search(r):
            return "Approved"
    if final_res in ("Approved", "Denied", "Withdrawn"):
        return final_res
    if future or mapped == "Hearing!":
        return "Hearing Scheduled"
    if final_res == "Continued" or (past and past[-1].get("result") and _ST_CONTINUED.search(past[-1]["result"])):
        return "Continued"
    if mapped == "_none_" and r and _ST_CONTINUED.search(r):
        return "Continued"
    if mapped == "_none_" and r and re.search(r"hearing scheduled|scheduled for|agenda|hearing pending|"
                                               r"pending hearing", r, re.I):
        return "Hearing Scheduled"
    return "Filed"


# ----------------------------------------------------------------------------------------------
# hearing body normalization
# ----------------------------------------------------------------------------------------------
HEARING_BODIES = [
    (re.compile(r"city council|council hearing|\bcc hearing|city commission", re.I), "City Council"),
    (re.compile(r"\bbocc\b|\bbcc\b|board of county|county commission|\bboard hearing|commissioners", re.I), "BOCC"),
    (re.compile(r"hearing master|\bzhm\b", re.I), "Zoning Hearing Master"),
    (re.compile(r"hearing officer|hearing examiner|\bluho\b|land use hearing", re.I), "Hearing Officer"),
    (re.compile(r"planning commis+ion|planning commision|planning board|planning planning commis+ion|planning commision|planning board|planning & zoning|planning and zoning| zoning|planning and zoning|planning zoning|\bp&z\b|"
                r"\bpzc\b|\bpc hearing|\bpc\b|\blpa\b|local planning agency|planning agency", re.I),
     "Planning Commission"),
    (re.compile(r"\bdrc\b|development review committee", re.I), "DRC"),
]


def hearing_body(name: str | None) -> str | None:
    if not name:
        return None
    for rx, body in HEARING_BODIES:
        if rx.search(name):
            return body
    return None


# ----------------------------------------------------------------------------------------------
# HTTP session wrapper
# ----------------------------------------------------------------------------------------------
class Blocked(Exception):
    pass


class ACA:
    def __init__(self, agency: str, module: str = "Planning", tab: str | None = None,
                 sleep: float = SLEEP):
        self.agency = agency
        self.module = module
        self.tab = tab or module
        self.sleep = sleep
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA,
                               "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                               "Accept-Language": "en-US,en;q=0.9"})
        self.home_url = f"{HOST}/{agency}/Cap/CapHome.aspx?module={module}&TabName={self.tab}"
        self._form_cache: dict | None = None

    # -- low level ------------------------------------------------------------------------------
    def _req(self, method: str, url: str, **kw) -> requests.Response:
        kw.setdefault("timeout", 90)
        last = None
        for attempt in range(3):
            try:
                time.sleep(self.sleep)
                r = self.s.request(method, url, **kw)
                if r.status_code in (403, 429) or "cf-chl" in r.text[:5000] or "captcha" in r.text[:20000].lower() and "recaptcha" not in r.text[:20000].lower():
                    raise Blocked(f"{r.status_code} at {url}")
                if r.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {r.status_code}")
                return r
            except Blocked:
                raise
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(2 * (attempt + 1) ** 2)
        raise last  # type: ignore[misc]

    def get(self, url: str, **kw) -> requests.Response:
        if url.startswith("/"):
            url = HOST + url
        return self._req("GET", url, **kw)

    def post_form(self, url: str, data: dict, **kw) -> requests.Response:
        h = {"Referer": url, "Origin": HOST, "Content-Type": "application/x-www-form-urlencoded"}
        return self._req("POST", url, data=data, headers=h, **kw)

    @staticmethod
    def form_fields(html: str) -> dict:
        s = BeautifulSoup(html, "lxml")
        f = s.find("form", id="aspnetForm") or s.find("form")
        data: dict = {}
        if f is None:
            return data
        for i in f.find_all("input"):
            n = i.get("name")
            t = (i.get("type") or "text").lower()
            if not n or t in ("submit", "image", "button", "file"):
                continue
            if t in ("radio", "checkbox"):
                if i.has_attr("checked"):
                    data[n] = i.get("value", "on")
                continue
            data[n] = i.get("value", "")
        for sel in f.find_all("select"):
            n = sel.get("name")
            if not n:
                continue
            o = sel.find("option", selected=True) or sel.find("option")
            data[n] = o.get("value", "") if o else ""
        for t in f.find_all("textarea"):
            if t.get("name"):
                data[t["name"]] = t.get_text()
        return data

    # -- search --------------------------------------------------------------------------------
    def record_types(self) -> list[tuple[str, str]]:
        r = self.get(self.home_url)
        s = BeautifulSoup(r.text, "lxml")
        sel = s.find("select", attrs={"name": GS + "ddlGSPermitType"})
        if sel is None:
            raise Blocked(f"no search form at {self.home_url} (login required?)")
        return [(o.get("value", ""), o.get_text(strip=True)) for o in sel.find_all("option") if o.get("value")]

    def search(self, rtype: str | None = None, start: dt.date | None = None, end: dt.date | None = None,
               permit_no: str | None = None, max_pages: int = 300) -> tuple[list[dict], str | None]:
        """Run a general search. Returns (rows, detail_html_if_redirected).
        If the portal's general search has no date fields (e.g. Manatee), results come back newest
        first; paging stops once a whole page is older than `start`."""
        r = self.get(self.home_url)
        if "ddlGSPermitType" not in r.text and "txtGSPermitNumber" not in r.text:
            raise Blocked(f"no general search form at {self.home_url}")
        data = self.form_fields(r.text)
        self.has_dates = (GS + "txtGSStartDate") in data
        data["ctl00$PlaceHolderMain$ddlSearchType"] = "0"
        if GS + "ddlGSPermitType" in data:
            data[GS + "ddlGSPermitType"] = rtype or ""
        data[GS + "txtGSPermitNumber"] = permit_no or ""
        if self.has_dates:
            data[GS + "txtGSStartDate"] = mdy(start) if start else ""
            data[GS + "txtGSEndDate"] = mdy(end) if end else ""
        data["__EVENTTARGET"] = "ctl00$PlaceHolderMain$btnNewSearch"
        data["__EVENTARGUMENT"] = ""
        r = self.post_form(self.home_url, data)
        self.last_url = r.url
        if "CapDetail.aspx" in r.url:
            return [], r.text
        if "Error.aspx" in r.url:
            raise RuntimeError(f"ACA error page after search ({r.url})")
        rows: list[dict] = []
        html = r.text
        for _page in range(max_pages):
            page_rows, next_target = self._parse_grid(html)
            rows.extend(page_rows)
            if not next_target:
                break
            if start and not self.has_dates and page_rows and all(
                    (x.get("date") or "9999") < start.isoformat() for x in page_rows):
                break
            data = self.form_fields(html)
            data["__EVENTTARGET"] = next_target
            data["__EVENTARGUMENT"] = ""
            r = self.post_form(self.home_url, data)
            html = r.text
        return rows, None

    @staticmethod
    def result_count(html: str) -> str | None:
        m = re.search(r"Showing\s+\d+-\d+\s+of\s+([\d,]+\+?)", html)
        return m.group(1) if m else None

    def _parse_grid(self, html: str) -> tuple[list[dict], str | None]:
        s = BeautifulSoup(html, "lxml")
        t = s.find("table", id=GRID_ID)
        if t is None:
            return [], None
        rows = []
        for tr in t.find_all("tr", recursive=False) or t.find_all("tr"):
            a = tr.find("a", id=re.compile(r"hlPermitNumber$"))
            num_span = tr.find("span", id=re.compile(r"lblPermitNumber1?$"))
            if not num_span:
                continue

            def sp(suffix):
                e = tr.find("span", id=re.compile(suffix + "$"))
                return one_line(e.get_text(" ")) if e else None

            rows.append({
                "app_num": one_line(num_span.get_text()),
                "date": iso(sp("lblUpdatedTime")),
                "type": sp("lblType"),
                "description": sp("lblDescription"),
                "project_name": sp("lblProjectName"),
                "status": sp("lblStatus"),
                "address": sp("lblPermitAddress") or sp("lblAddress"),
                "url": (HOST + a["href"]) if a and a.get("href", "").startswith("/") else (a["href"] if a else None),
            })
        nxt = None
        for a in t.find_all("a"):
            if a.get_text(strip=True).startswith("Next"):
                m = re.search(r"__doPostBack\('([^']+)'", a.get("href", ""))
                if m:
                    nxt = m.group(1)
        return rows, nxt

    # -- detail --------------------------------------------------------------------------------
    def detail(self, url: str, workflow: bool = True) -> dict:
        r = self.get(url)
        d = parse_detail(r.text)
        d["url"] = r.url if "CapDetail" in r.url else url
        if workflow:
            try:
                d["workflow"] = self.processing(d["url"])
            except Exception as e:  # noqa: BLE001
                log(f"  workflow fetch failed for {d.get('app_num')}: {e}")
                d["workflow"] = []
        return d

    def detail_from_html(self, html: str, url: str, workflow: bool = True) -> dict:
        d = parse_detail(html)
        d["url"] = url
        if workflow:
            try:
                d["workflow"] = self.processing(url)
            except Exception as e:  # noqa: BLE001
                log(f"  workflow fetch failed for {d.get('app_num')}: {e}")
                d["workflow"] = []
        return d

    def processing(self, detail_url: str) -> list[dict]:
        u = f"{HOST}/{self.agency}/Cap/CapDetail.aspx/GetProcessingData"
        body = json.dumps({"agencyCode": self.agency_code, "moduleName": self.module})
        r = self._req("POST", u, data=body, headers={
            "Content-Type": "application/json; charset=utf-8", "Referer": detail_url, "Origin": HOST,
            "X-Requested-With": "XMLHttpRequest"})
        if r.status_code != 200:
            return []
        try:
            html = r.json().get("d") or ""
        except ValueError:
            return []
        return parse_workflow(html)

    @property
    def agency_code(self) -> str:
        return getattr(self, "_agency_code", None) or self.agency.upper()

    @agency_code.setter
    def agency_code(self, v):
        self._agency_code = v


# ----------------------------------------------------------------------------------------------
# detail page parsing
# ----------------------------------------------------------------------------------------------
def _text(e) -> str | None:
    return clean(e.get_text("\n")) if e is not None else None


def parse_contact(block) -> str | None:
    """Return a one-line name (business / person) from an ACA contact block."""
    if block is None:
        return None
    names = []
    for cls in ("contactinfo_businessname", "contactinfo_firstname", "contactinfo_middlename",
                "contactinfo_lastname", "contactinfo_fullname"):
        for e in block.find_all(class_=cls):
            t = one_line(e.get_text(" "))
            if t:
                names.append((cls, t))
    biz = [t for c, t in names if c == "contactinfo_businessname"]
    person = " ".join(t for c, t in names if c in ("contactinfo_firstname", "contactinfo_middlename",
                                                    "contactinfo_lastname", "contactinfo_fullname"))
    if biz and person:
        return f"{person} ({biz[0]})"
    if biz:
        return biz[0]
    if person:
        return person
    t = _text(block)
    if not t:
        return None
    first = t.split("\n")[0]
    return None if first.endswith(":") else first


def clean_address(t: str | None) -> str | None:
    t = one_line(t)
    if not t:
        return None
    t = re.split(r"\*|View Additional|ADDRESSKEY:|ADDID:|HISTORICAL:", t)[0]
    t = re.sub(r"\bUnited States\b", "", t)
    t = re.sub(r"\s+,", ",", re.sub(r"\s{2,}", " ", t)).strip(" ,")
    t = re.sub(r"^(.{4,}?)\s+\1\b", r"\1", t)  # "2271 KEYSTONE RD 2271 KEYSTONE RD TARPON..." 
    if not t or re.fullmatch(r"(FL|Florida|,)*", t, re.I):
        return None
    return t


def parse_detail(html: str) -> dict:
    s = BeautifulSoup(html, "lxml")
    out: dict = {"asi": [], "asit": [], "contacts": [], "parcels": [], "record_details": {}}
    e = s.find(id="ctl00_PlaceHolderMain_lblPermitNumber")
    out["app_num"] = one_line(e.get_text()) if e else None
    e = s.find(id="ctl00_PlaceHolderMain_lblRecordStatus")
    out["status_raw"] = one_line(e.get_text()) if e else None
    # record type: text of the h1 after the number
    out["type"] = None
    if e := s.find(id="ctl00_PlaceHolderMain_lblPermitNumber"):
        h1 = e.find_parent("h1")
        if h1:
            nxt = h1.find_next_sibling()
            t = one_line(h1.get_text(" "))
            m = re.search(r":\s*(.+)$", t or "")
            if m and m.group(1).strip():
                out["type"] = m.group(1).strip()
            elif nxt is not None:
                out["type"] = one_line(nxt.get_text(" "))
    if not out["type"]:
        e = s.find(id="ctl00_PlaceHolderMain_lblPermitType")
        out["type"] = one_line(e.get_text()) if e else None

    # work location / address
    wl = s.find(id="tbl_worklocation")
    addr = None
    if wl:
        addr = clean_address(wl.get_text(" "))
    out["address"] = addr

    # Record details: h1 label + span content, inside PermitDetailList1 table
    rd = s.find(id=re.compile(r"PermitDetailList1_TBPermitDetailTest$"))
    if rd:
        for td in rd.find_all("td", class_="td_parent_left"):
            h = td.find("h1")
            if not h:
                continue
            label = one_line(h.get_text(" "))
            if not label:
                continue
            label = label.rstrip(":").strip()
            content = h.find_next_sibling()
            if label.lower().startswith(("applicant", "licensed professional")):
                out["record_details"][label] = parse_contact(content)
            elif label.lower().startswith("owner"):
                t = _text(content)
                out["record_details"][label] = re.sub(r"\s*\*\s*$", "", t.split("\n")[0]).strip() if t else None
            else:
                out["record_details"][label] = _text(content)

    # Related contacts (More details)
    rc = s.find(id=re.compile(r"PermitDetailList1_RelatContactList$"))
    if rc is not None:
        for blk in rc.find_all("div", class_="MoreDetail_ItemCol1"):
            h2 = blk.find("h2")
            typ = one_line(h2.get_text(" ")) if h2 else None
            nm = parse_contact(blk.find(class_="ACA_ConfigInfo") or blk)
            if nm:
                out["contacts"].append({"type": typ, "name": nm})

    # ASI (Application Information): label/value div pairs, with group titles
    asi_root = s.find(id=re.compile(r"PermitDetailList1_phPlumbingGroup$"))
    if asi_root is not None:
        group = None
        for el in asi_root.find_all("div"):
            cls = el.get("class") or []
            if "MoreDetail_ItemTitle" in cls:
                group = one_line(el.get_text(" "))
            elif "MoreDetail_ItemColASI" in cls and "MoreDetail_ItemCol1" in cls:
                label = one_line(el.get_text(" "))
                val_div = el.find_next_sibling("div")
                val = clean(val_div.get_text("\n")) if val_div is not None else None
                if label:
                    out["asi"].append({"group": group, "label": label.rstrip(":").strip(), "value": val})

    # ASIT (Application Information Table)
    asit_root = s.find(id="trASITList")
    if asit_root is not None:
        table = None
        row: dict = {}
        for el in asit_root.find_all(["div"]):
            cls = el.get("class") or []
            if "ACA_Title_Text" in cls:
                if row:
                    out["asit"].append({"table": table, **row})
                    row = {}
                table = one_line(el.get_text(" "))
            elif "MoreDetail_Item" in cls:
                # one ASIT row: alternating Col1 (label) / Col2 (value) divs
                if row:
                    out["asit"].append({"table": table, **row})
                    row = {}
                for c1 in el.find_all("div", class_="MoreDetail_ItemCol1", recursive=False):
                    c2 = c1.find_next_sibling("div")
                    k = (one_line(c1.get_text(" ")) or "").rstrip(":").strip()
                    v = one_line(c2.get_text(" ")) if c2 is not None and "MoreDetail_ItemCol2" in (c2.get("class") or []) else None
                    if k:
                        row[k] = v
        if row:
            out["asit"].append({"table": table, **row})

    # Parcel Information
    out["parcel_attrs"] = []
    pl = s.find(id=re.compile(r"PermitDetailList1_palParceList$"))
    if pl is not None:
        txt = pl.get_text("|")
        for chunk in re.split(r"(?=Parcel Number:)", txt):
            m = re.match(r"Parcel Number:\s*([^|*]+)", chunk)
            if not m:
                continue
            p = one_line(m.group(1))
            attrs = {"Parcel Number": p}
            for piece in chunk.split("|")[1:]:
                mm = re.match(r"\s*([A-Za-z][A-Za-z0-9 _/#.\-]{0,40}?)\s*:\s*(.*)$", piece)
                if mm and mm.group(2).strip() not in ("", "--"):
                    attrs.setdefault(mm.group(1).strip(), mm.group(2).strip())
            if p and p not in out["parcels"]:
                out["parcels"].append(p)
                out["parcel_attrs"].append(attrs)
    return out


def parse_workflow(html: str) -> list[dict]:
    """Processing Status -> [{task, entries:[{due, assigned, status, date, by, comment}]}]"""
    if not html:
        return []
    s = BeautifulSoup(html, "lxml")
    top = s.find("table")
    if top is None:
        return []
    tasks = []
    rows = top.find_all("tr", recursive=False)
    if not rows and top.find("tbody"):
        rows = top.find("tbody").find_all("tr", recursive=False)
    i = 0
    while i < len(rows):
        tr = rows[i]
        tds = tr.find_all("td", recursive=False)
        if tr.get("id") is None and len(tds) >= 2:
            img = tds[0].find("img")
            state = img.get("title") if img else None
            name = one_line(tds[1].get_text(" "))
            entries = []
            if i + 1 < len(rows) and rows[i + 1].get("id"):
                det = rows[i + 1]
                for cell in det.find_all("td", class_="ACA_ALeft"):
                    txt = cell.get_text(" ")
                    if "Marked as" not in txt or cell.find("td", class_="ACA_ALeft"):
                        continue
                    spans = [one_line(x.get_text(" ")) for x in cell.find_all("span")]
                    t1 = one_line(txt) or ""
                    ent = {"due": None, "assigned": None, "status": None, "date": None, "by": None, "comment": None}
                    m = re.search(r"Due on\s+(\S+)", t1)
                    ent["due"] = iso(m.group(1)) if m else None
                    m = re.search(r"assigned to\s+(.+?)\s+Marked as", t1)
                    ent["assigned"] = m.group(1) if m else None
                    m = re.search(r"Marked as\s+(.+?)\s+on\s+(\S+)\s+by\s+(.+?)(?:\s+Comment:|$)", t1)
                    if m:
                        ent["status"] = None if m.group(1) == "TBD" else m.group(1)
                        ent["date"] = iso(m.group(2))
                        ent["by"] = None if m.group(3).strip() == "TBD" else m.group(3).strip()
                    m = re.search(r"Comment:\s*(.+)$", t1)
                    ent["comment"] = m.group(1).strip() if m else None
                    # comments sometimes sit in a following row of the same entry table
                    entries.append(ent)
                # comments placed in separate rows
                if entries and not any(e["comment"] for e in entries):
                    ct = det.get_text("\n")
                    cms = re.findall(r"Comment:\s*\n?([^\n]+)", ct)
                    for e, c in zip([e for e in entries], cms):
                        e["comment"] = one_line(c)
                i += 1
            tasks.append({"task": name, "state": state, "entries": entries})
        i += 1
    return tasks


# ----------------------------------------------------------------------------------------------
# generic field extraction from parsed detail
# ----------------------------------------------------------------------------------------------
def asi_get(d: dict, *patterns: str, exclude: str | None = None) -> str | None:
    """First ASI value whose label matches any regex pattern (case-insensitive)."""
    for p in patterns:
        rx = re.compile(p, re.I)
        for it in d.get("asi", []):
            if rx.search(it["label"]) and (not exclude or not re.search(exclude, it["label"], re.I)):
                v = it.get("value")
                if v and v.strip() not in ("--", "N/A", "n/a", "NA", "None", "0", "TBD"):
                    return v
    return None


def asit_values(d: dict, key_rx: str) -> list[str]:
    rx = re.compile(key_rx, re.I)
    out = []
    for row in d.get("asit", []):
        for k, v in row.items():
            if k != "table" and rx.search(k) and v:
                out.append(v)
    return out


MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December|" \
         "Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
_DATE_IN_TEXT = re.compile(r"(\d{1,2}/\d{1,2}/\d{2,4})|((?:%s)\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})" % MONTHS, re.I)


def dates_in_text(text: str | None) -> list[str]:
    out = []
    for m in _DATE_IN_TEXT.finditer(text or ""):
        t = m.group(0)
        t = re.sub(r"(\d)(st|nd|rd|th)", r"\1", t).replace(".", "")
        t = re.sub(r"\bSept\b", "Sep", t)
        d = iso(t) if "/" in t else (iso(t.replace(",", ", ").replace(",  ", ", ")) or iso(t))
        if not d:
            for fmt in ("%B %d %Y", "%b %d %Y", "%B %d, %Y", "%b %d, %Y"):
                try:
                    d = dt.datetime.strptime(re.sub(r"\s+", " ", t.replace(",", "")), fmt.replace(",", "")).date().isoformat()
                    break
                except ValueError:
                    pass
        if d:
            out.append(d)
    return out


_ADMIN_RESULT = re.compile(r"^(in progress|scheduled|notice|advertis|in review|pending|routed|assigned|sent|"
                           r"received|note|ready|prepare|staff report|agenda|complete[d]?$|not required|"
                           r"comments?$|no comments?|hearing complete$|n/?a$)", re.I)
_SKIP_TASK = re.compile(r"prep|packet|set case|notice|noticing|advertis|post hearing|documentation|"
                        r"distribution|scheduling|review$|staff report|final letter|archive", re.I)


def hearings_from_workflow(d: dict, default_body: str | None = None, task_rx: str | None = None,
                           skip_rx: str | None = None, body_map: Callable[[str], str | None] = hearing_body,
                           today: str | None = None) -> list[dict]:
    """Hearing outcomes from the Processing Status workflow.
    A task counts when its name maps to a hearing body (or matches task_rx / contains 'hearing').
    Each entry with an outcome ('Marked as X on D') -> hearing; date = a date mentioned in the
    comment that lies within 120 days before the mark date, else the mark date itself."""
    out = []
    trx = re.compile(task_rx, re.I) if task_rx else None
    srx = re.compile(skip_rx, re.I) if skip_rx else None
    for t in d.get("workflow", []):
        name = t.get("task") or ""
        if srx and srx.search(name):
            continue
        body = body_map(name)
        generic = bool(re.search(r"hearing", name, re.I)) or bool(trx and trx.search(name))
        if not body and not generic:
            continue
        if _SKIP_TASK.search(name) and not (trx and trx.search(name)):
            continue
        for e in t.get("entries", []):
            st, marked = e.get("status"), e.get("date")
            if not st or not marked or _ADMIN_RESULT.search(st.strip()):
                continue
            b = body or body_map(e.get("comment") or "") or default_body
            if not b:
                continue
            hd = marked
            if not _ST_CONTINUED.search(st):
                lo = (dt.date.fromisoformat(marked) - dt.timedelta(days=120)).isoformat()
                cands = [x for x in dates_in_text(e.get("comment")) if lo <= x <= marked]
                if cands:
                    hd = cands[0]
            out.append({"body": b, "date": hd, "result": st})
    return dedup_hearings(out)


_BODY_TOKEN = r"(Planning Commission|PC|LPA|BCC|BOCC|BoCC|Board of County Commissioners|City Council|DRC)"
_OUTCOME = re.compile(r"(recommended approval|recommended denial|recommend(?:ed)? approval|approved|denied|"
                      r"continued|deferred|withdrawn|adopted)", re.I)


def hearings_from_comments(d: dict, today: str | None = None) -> list[dict]:
    """Outcomes written in free-text workflow comments, e.g. 'Approved at the 1/20/2026 BCC' or
    'Approved with deed restrictions - PC 2/19/2026'. Only (body, date) pairs written next to each
    other are used."""
    date_rx = r"(\d{1,2}/\d{1,2}/\d{2,4}|(?:%s)\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})" % MONTHS
    pair1 = re.compile(_BODY_TOKEN + r"\s*(?:hearing|meeting)?\s*(?:on|of)?\s*" + date_rx, re.I)
    pair2 = re.compile(date_rx + r"\s*(?:,\s*)?" + _BODY_TOKEN, re.I)
    out = []
    for t in d.get("workflow", []):
        for e in t.get("entries", []):
            c = e.get("comment") or ""
            m_out = _OUTCOME.search(c)
            if not m_out:
                continue
            for m in pair1.finditer(c):
                b, dd = hearing_body(m.group(1)), dates_in_text(m.group(2))
                if b and dd:
                    out.append({"body": b, "date": dd[0], "result": m_out.group(1).capitalize()})
            for m in pair2.finditer(c):
                b, dd = hearing_body(m.group(2)), dates_in_text(m.group(1))
                if b and dd:
                    out.append({"body": b, "date": dd[0], "result": m_out.group(1).capitalize()})
    return dedup_hearings(out)


def asi_hearings(d: dict, spec: list[tuple[str, str, str | None]]) -> list[dict]:
    """spec: [(date_label_regex, body, result_label_regex_or_None)] read from ASI fields."""
    out = []
    for date_rx, body, res_rx in spec:
        for it in d.get("asi", []):
            if re.search(date_rx, it["label"], re.I) and it.get("value"):
                hd = iso(it["value"])
                if not hd:
                    continue
                res = asi_get(d, res_rx) if res_rx else None
                out.append({"body": body, "date": hd, "result": res})
                break
    return out


def dedup_hearings(hs: list[dict]) -> list[dict]:
    best: dict = {}
    for h in hs:
        k = (h["body"], h["date"])
        if k not in best or (h.get("result") and not best[k].get("result")):
            best[k] = h
    return sorted(best.values(), key=lambda h: (h["date"], h["body"]))


def merge_hearings(primary: list[dict], secondary: list[dict], window_days: int = 21) -> list[dict]:
    """Combine ASI-scheduled hearings (exact dates, maybe no result) with workflow outcomes
    (result, approximate date). A workflow outcome for the same body within window_days AFTER an
    ASI date fills in that hearing's result instead of adding a new row."""
    out = [dict(h) for h in primary]
    for h in secondary:
        hit = None
        for p in out:
            if p["body"] == h["body"] and p["date"] <= h["date"]:
                gap = (dt.date.fromisoformat(h["date"]) - dt.date.fromisoformat(p["date"])).days
                if gap <= window_days and (hit is None or p["date"] > hit["date"]):
                    hit = p
        if hit is not None:
            if not hit.get("result"):
                hit["result"] = h["result"]
        else:
            out.append(dict(h))
    return dedup_hearings(out)


ZONING_RX = re.compile(r"\b(?:[Ff]rom|FROM)\s+([A-Z][A-Za-z0-9\-/ ,&\.]{0,40}?)\s+(?:zoning\s+)?"
                       r"(?:district\s+)?(?:to|To|TO)\s+(?:the\s+)?([A-Z][A-Za-z0-9\-/ ,&\.]{0,40}?)"
                       r"(?=\s+(?:zoning|district|for|to allow|to permit|in order|with|on|[Ff]rom|[Ii]n|[Tt]o|"
                       r"[Oo]n|[Ff]or|[Ww]ith|\(|$)|[\.;,\n]|$)")


def zoning_from_text(text: str | None) -> tuple[str | None, str | None]:
    if not text:
        return None, None
    m = ZONING_RX.search(text)
    if not m:
        return None, None
    a, b = m.group(1).strip(" ,."), m.group(2).strip(" ,.")
    if len(a) > 40 or len(b) > 40:
        return None, None
    return a or None, b or None


def acres_from_text(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"(\d[\d,]*\.?\d*)\s*(?:\+/-|\+/−|±|\+-)?\s*(?:gross\s+)?(?:acres?|ac\.?\b)", text, re.I)
    return to_float(m.group(1)) if m else None


def first_contact(d: dict, *types_rx: str) -> str | None:
    for rx in types_rx:
        for c in d.get("contacts", []):
            if c.get("type") and re.search(rx, c["type"], re.I):
                return c["name"]
    return None


# ----------------------------------------------------------------------------------------------
# driver
# ----------------------------------------------------------------------------------------------
def split_range(start: dt.date, end: dt.date, days: int) -> list[tuple[dt.date, dt.date]]:
    out = []
    a = start
    while a <= end:
        b = min(end, a + dt.timedelta(days=days - 1))
        out.append((a, b))
        a = b + dt.timedelta(days=1)
    return out


def aca_has_dates(aca: ACA) -> bool:
    if not hasattr(aca, "has_dates"):
        r = aca.get(aca.home_url)
        aca.has_dates = (GS + "txtGSStartDate") in r.text.replace("&#36;", "$")
    return aca.has_dates


def make_fetch(cfg: dict, build: Callable[[dict, dict, ACA], dict | None]):
    """cfg keys:
        agency, module, tab, agency_code (opt), jurisdiction, county, source
        types: {record_type_value: category_or_None}  (None => decide in build())
        chunk_days: date-window size for the search (default 400)
        workflow: bool (default True)
    build(row, detail, aca) -> record dict, or None to skip (out of scope)."""

    def fetch(since: str, refresh: list[str] | None = None) -> list[dict]:
        t0 = time.time()
        refresh = refresh or []
        aca = ACA(cfg["agency"], cfg.get("module", "Planning"), cfg.get("tab"))
        if cfg.get("agency_code"):
            aca.agency_code = cfg["agency_code"]
        try:
            avail = dict(aca.record_types())
        except Blocked:
            raise
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"{cfg['jurisdiction']}: portal unreachable: {e}") from e
        start = dt.date.fromisoformat(since)
        end = dt.date.today()
        seen: dict[str, dict] = {}
        rows: list[dict] = []
        for tval in cfg["types"]:
            if tval not in avail:
                log(f"[{cfg['jurisdiction']}] record type not offered by portal any more: {tval}")
                continue
            for a, b in split_range(start, end, cfg.get("chunk_days", 400) if aca_has_dates(aca) else 100000):
                try:
                    rs, single = aca.search(rtype=tval, start=a, end=b)
                except Blocked:
                    raise
                except Exception as e:  # noqa: BLE001
                    log(f"[{cfg['jurisdiction']}] search failed {tval} {a}..{b}: {e}")
                    continue
                if single is not None:
                    d = parse_detail(single)
                    rs = [{"app_num": d.get("app_num"), "type": d.get("type"), "date": None,
                           "status": d.get("status_raw"), "url": aca.last_url, "_html": single}]
                log(f"[{cfg['jurisdiction']}] {avail[tval]}: {len(rs)} rows {a}..{b}")
                for r in rs:
                    r["_tval"] = tval
                    if r.get("date") and r["date"] < since:
                        continue
                    rows.append(r)
        for r in rows:
            if not r.get("app_num") or re.match(r"^\d{2}TMP-", r["app_num"]):
                continue  # temporary / unsubmitted online drafts
            if r["app_num"] not in seen:
                seen[r["app_num"]] = r
        # refresh ids not already covered
        for num in refresh:
            if num in seen:
                continue
            try:
                rs, single = aca.search(permit_no=num)
            except Blocked:
                raise
            except Exception as e:  # noqa: BLE001
                log(f"[{cfg['jurisdiction']}] refresh search failed {num}: {e}")
                continue
            if single is not None:
                seen[num] = {"app_num": num, "_html": single, "url": aca.last_url, "date": None, "_refresh": True}
            else:
                for r in rs:
                    if r.get("app_num") == num:
                        r["_refresh"] = True
                        seen[num] = r
                        break
                else:
                    log(f"[{cfg['jurisdiction']}] refresh id not found: {num}")
        out = []
        for num, r in seen.items():
            try:
                # always (re)load the detail page right before its workflow call: ACA keeps the
                # "current record" for GetProcessingData in the server-side session.
                if r.get("url") and "CapDetail" in r["url"]:
                    d = aca.detail(r["url"], workflow=False)
                elif r.get("_html"):
                    d = aca.detail_from_html(r["_html"], r.get("url") or aca.home_url, workflow=False)
                else:
                    log(f"[{cfg['jurisdiction']}] no detail link for {num}; skipping")
                    continue
                pre = cfg.get("prefilter")
                if pre is not None and not pre(r, d):
                    continue
                d["workflow"] = []
                if cfg.get("workflow", True):
                    try:
                        d["workflow"] = aca.processing(d["url"])
                    except Exception as e:  # noqa: BLE001
                        log(f"  workflow fetch failed for {num}: {e}")
                rec = build(r, d, aca)
                if rec is None:
                    continue
                if not r.get("_refresh") and rec.get("filed_date") and rec["filed_date"] < since:
                    continue
                out.append(finalize(rec, cfg))
            except Blocked:
                raise
            except Exception as e:  # noqa: BLE001
                log(f"[{cfg['jurisdiction']}] failed on {num}: {type(e).__name__}: {e}")
        log(f"[{cfg['jurisdiction']}] {len(out)} in-scope records ({len(seen)} candidates) "
            f"in {time.time() - t0:.0f}s")
        return out

    return fetch


SCHEMA_KEYS = ["jurisdiction", "county", "app_num", "category", "app_type", "project_name", "description",
               "applicant", "parcel_ids", "address", "acres", "from_zoning", "to_zoning", "from_flu", "to_flu",
               "filed_date", "status", "status_raw", "hearings", "decision_date", "url", "docs_url",
               "lat", "lon", "source"]


def finalize(rec: dict, cfg: dict) -> dict:
    rec.setdefault("jurisdiction", cfg["jurisdiction"])
    rec.setdefault("county", cfg["county"])
    rec.setdefault("source", cfg["source"])
    out = {}
    for k in SCHEMA_KEYS:
        v = rec.get(k)
        if isinstance(v, str):
            v = clean(v)
        if k in ("parcel_ids", "hearings"):
            v = v or []
        out[k] = v if v not in ("",) else None
    # description: keep it short-ish
    if out["description"] and len(out["description"]) > 600:
        out["description"] = out["description"][:597].rstrip() + "..."
    return out


_APPL_RX = [
    re.compile(r"on behalf of\s+(?:the\s+)?([A-Z][\w&.,' \-]{2,80}?)\s*\((?:the\s+)?[“\"]?(?:Applicant|Owner|Developer)", re.I),
    re.compile(r"\bApplicant,?\s+([A-Z][\w&./' \-]{2,60}?(?:LLC|L\.L\.C\.|Inc\.?|LP|LLLP|Corp\.?|Company|Trust))\b"),
]


def applicant_from_text(text: str | None) -> str | None:
    for rx in _APPL_RX:
        m = rx.search(text or "")
        if m:
            return m.group(1).strip(" ,")
    return None


def earliest_workflow_date(d: dict) -> str | None:
    ds = [e["date"] for t in d.get("workflow", []) for e in t.get("entries", []) if e.get("date")]
    return min(ds) if ds else None


def default_record(row: dict, d: dict, cfg: dict, category: str) -> dict:
    """Common mapping used by most jurisdictions; modules post-process what they need."""
    rd = d.get("record_details", {})
    pdesc = rd.get("Project Description")
    desc_lines = (pdesc or "").split("\n") if pdesc else []
    project_name = row.get("project_name")
    description = row.get("description")
    if not description and desc_lines:
        description = " ".join(desc_lines[1:]) if len(desc_lines) > 1 else desc_lines[0]
    if not project_name and len(desc_lines) > 1:
        project_name = desc_lines[0]
    applicant = rd.get("Applicant") or first_contact(d, r"applicant") or first_contact(d, r"agent|represent") \
        or rd.get("Owner") or applicant_from_text(pdesc) or applicant_from_text(row.get("description"))
    address = d.get("address") or clean_address(row.get("address"))
    return {
        "jurisdiction": cfg["jurisdiction"],
        "county": cfg["county"],
        "app_num": d.get("app_num") or row.get("app_num"),
        "category": category,
        "app_type": d.get("type") or row.get("type"),
        "project_name": project_name,
        "description": one_line(description),
        "applicant": applicant,
        "parcel_ids": [],
        "address": one_line(address),
        "acres": None,
        "from_zoning": None, "to_zoning": None, "from_flu": None, "to_flu": None,
        "filed_date": row.get("date") or earliest_workflow_date(d),
        "status_raw": d.get("status_raw") or row.get("status"),
        "hearings": [],
        "decision_date": None,
        "url": d.get("url") or row.get("url"),
        "docs_url": None,
        "lat": None, "lon": None,
        "source": cfg["source"],
    }


def decision_date(hearings: list[dict], status: str, final_bodies=("BOCC", "City Council")) -> str | None:
    if status not in ("Approved", "Denied"):
        return None
    hs = [h for h in hearings if h.get("body") in final_bodies and h.get("result")]
    if not hs:
        hs = [h for h in hearings if h.get("result")]
    return sorted(hs, key=lambda h: h["date"])[-1]["date"] if hs else None


def run_cli(fetch, argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    since = argv[0] if argv else (dt.date.today() - dt.timedelta(days=14)).isoformat()
    refresh = argv[1].split(",") if len(argv) > 1 and argv[1] else []
    t0 = time.time()
    recs = fetch(since, refresh)
    print(json.dumps(recs, indent=2))
    from collections import Counter
    log(f"{len(recs)} records since {since} in {time.time() - t0:.0f}s; "
        f"categories={dict(Counter(r['category'] for r in recs))} statuses={dict(Counter(r['status'] for r in recs))}")
