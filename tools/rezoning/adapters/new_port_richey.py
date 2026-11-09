#!/usr/bin/env python3
"""City of New Port Richey (Pasco County) rezoning / PD / future land use map amendment adapter.

Source: the city's Granicus meeting portal (cityofnewportrichey.granicus.com/ViewPublisher.php?view_id=1).
City Council agendas are PDFs (AgendaViewer -> Granicus S3 attachment); every agenda item title is a
link to its backup PDF (staff memo + exhibits). Minutes are Granicus minutes reports (PDF) with the
motions ("Motion made by ... to approve the ordinance upon its second and final reading. The Motion
Passed. 4 - 0").
 * New Port Richey processes land use cases as numbered ordinances read twice by City Council
   ("Second Reading, Ordinance No. 2026-2374 FLUM: Amendment for 6506 Meadowbrook Lane, ... from
   LMDR-10 to LDR-5"). app_num = "Ordinance No. 2026-2374". Kept: future land use map amendments,
   rezonings / zoning map amendments, PD / MPUD. Skipped: LDC text, pension, budget, annexation only.
 * The Land Development Review Board (the city's planning board) does not publish to Granicus, so
   only Council hearings are tracked; filed_date = first Council agenda date.
 * Details (Pasco parcel numbers, acreage, applicant) come from the first pages of the item backup PDF.
Requires pypdf.
"""
import datetime as dt
import html as _html
import io
import json
import re
import sys
import time
import urllib.parse as up

import requests
from bs4 import BeautifulSoup

try:
    import pypdf
except ImportError:  # pragma: no cover
    pypdf = None

JURISDICTIONS = ["City of New Port Richey"]
JUR = "City of New Port Richey"
COUNTY = "Pasco"
BASE = "https://cityofnewportrichey.granicus.com"
PUBLISHER = f"{BASE}/ViewPublisher.php?view_id=1"
SOURCE = "New Port Richey Granicus (City Council agendas, item backup and minutes)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
LOOKBACK_DAYS = 150
CC = "City Council"
SKIP_BODY = re.compile(r"Work Session|Redevelopment|Executive|Workshop", re.I)
MAX_PDF = 25_000_000

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


def log(*a):
    print("[new_port_richey]", *a, file=sys.stderr)


def fix_url(u):
    """Granicus S3 bucket name has underscores; use the path-style URL so TLS hostname checks pass."""
    u = (u or "").strip()
    if u.startswith("//"):
        u = "https:" + u
    return re.sub(r"https?://granicus_production_attachments\.s3\.amazonaws\.com/",
                  "https://s3.amazonaws.com/granicus_production_attachments/", u)


def get(url, params=None, tries=4, sleep=0.4, **kw):
    last = None
    for i in range(tries):
        try:
            r = _s.get(fix_url(url), params=params, timeout=120, **kw)
            r.raise_for_status()
            time.sleep(sleep)
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


def sq(s):
    return re.sub(r"\s+", " ", s or "").strip()


# ------------------------------------------------------------------ Granicus
def list_meetings():
    soup = BeautifulSoup(get(PUBLISHER).text, "lxml")
    out, seen = [], set()
    for tr in soup.select("tr.listingRow"):
        tds = tr.find_all("td")
        if len(tds) < 2:
            continue
        body = tds[0].get_text(" ", strip=True)
        ds = sq(tds[1].get_text(" ", strip=True).replace("\xa0", " ")).split(" - ")[0]
        try:
            d = dt.datetime.strptime(ds, "%B %d, %Y").date().isoformat()
        except ValueError:
            continue
        if SKIP_BODY.search(body):
            continue
        agenda = minutes = None
        for a in tr.find_all("a"):
            href = a.get("href") or ""
            if "AgendaViewer" in href:
                agenda = fix_url(href)
            elif "minutes" in href.lower() or a.get_text(strip=True).lower() == "minutes":
                minutes = fix_url(href)
        if not agenda or agenda in seen:
            continue
        seen.add(agenda)
        out.append({"date": d, "body": body, "agenda": agenda, "minutes": minutes})
    return out


def agenda_pdf_url(viewer):
    r = _s.get(viewer, allow_redirects=False, timeout=60)
    time.sleep(0.3)
    loc = r.headers.get("Location") or ""
    if not loc and r.status_code == 200 and r.content[:4] == b"%PDF":
        return viewer
    q = up.parse_qs(up.urlparse(loc).query).get("url", [loc])[0]
    return fix_url(q) if q else None


def pdf_reader(url):
    r = get(url)
    if r.content[:4] != b"%PDF" or len(r.content) > MAX_PDF:
        return None
    return pypdf.PdfReader(io.BytesIO(r.content))


def agenda_items(rdr):
    """[(title, link)] for lettered agenda items; the title text is the link to the item backup."""
    items = []
    for page in rdr.pages:
        frags = []

        def visit(text, cm, tm, fd, fs):
            if text and text.strip():
                x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
                y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
                frags.append([round(y, 1), round(x, 1), text.strip()])
        try:
            page.extract_text(visitor_text=visit)
        except Exception:  # noqa: BLE001
            continue
        annots = []
        for a in page.get("/Annots") or []:
            try:
                a = a.get_object()
                act = a.get("/A")
                if act and act.get("/URI"):
                    r = [float(v) for v in a["/Rect"]]
                    annots.append((min(r[1], r[3]), max(r[1], r[3]), str(act["/URI"])))
            except Exception:  # noqa: BLE001
                continue
        frags.sort(key=lambda f: (-f[0], f[1]))
        cur = None
        for y, x, t in frags:
            if re.fullmatch(r"[a-z]\.", t):
                if cur:
                    items.append(cur)
                cur = {"y": y, "x": x, "title": "", "link": None}
                continue
            if re.fullmatch(r"\d{1,2}\.", t):
                if cur:
                    items.append(cur)
                cur = None
                continue
            if cur is not None and x > cur["x"] + 5 and y <= cur["y"] + 2 and y >= cur["y"] - 60:
                cur["title"] = sq(cur["title"] + " " + t)
        if cur:
            items.append(cur)
        for it in items:
            if it.get("link") is None and "page" not in it:
                it["page"] = True
                for y1, y2, uri in annots:
                    if y1 - 3 <= it["y"] <= y2 + 3:
                        it["link"] = fix_url(uri)
                        break
    return [(it["title"], it["link"]) for it in items if it["title"]]


# ------------------------------------------------------------------ classification
ORD_RE = re.compile(r"Ordinance\s*(?:No\.?\s*)?(\d{4})\s*-\s*(\d{3,5})\b", re.I)
IN_RE = re.compile(r"\bFLUM\b|Future Land Use|Land Use Map|Rezon|Zoning Map|Zoning Amendment|Zoning Designation|"
                   r"\bPUD\b|\bMPUD\b|Planned Development|Planned Unit|Small[- ]Scale|Large[- ]Scale", re.I)
OUT_RE = re.compile(r"Land Development Code|\bLDC\b|Text Amendment|Pension|Budget|Millage|Surplus|Conveyance|"
                    r"Comprehensive Plan Text|Element", re.I)
PIN_RE = re.compile(r"\b(\d{2})\s?-\s?(\d{2})\s?-\s?(\d{2})\s?-\s?([0-9A-Z]{4})\s?-\s?([0-9A-Z]{5})\s?-\s?([0-9A-Z]{4})\b")


def reading_of(t):
    t = (t or "").lower()
    if re.search(r"second|2nd|final reading|adoption", t):
        return "second"
    if re.search(r"transmittal", t):
        return "transmittal"
    if re.search(r"first|1st", t):
        return "first"
    return None


def kind_of(t):
    if re.search(r"\bFLUM\b|Future Land Use|Land Use Map|Small[- ]Scale|Large[- ]Scale", t, re.I):
        return "CPA"
    return "RZ"


_minutes = {}


def minutes_text(url):
    if url not in _minutes:
        txt = ""
        try:
            r = get(url)
            if r.content[:4] == b"%PDF":
                txt = "\n".join((p.extract_text() or "") for p in pypdf.PdfReader(io.BytesIO(r.content)).pages)
            else:
                txt = _html.unescape(re.sub(r"<[^>]+>", " ", r.text))
        except Exception as e:  # noqa: BLE001
            log(f"minutes {url[-40:]}: {e}")
        _minutes[url] = txt
    return _minutes[url]


def minutes_result(text, ono):
    y, n = ono.split("-")
    t = re.sub(r"[ \t]+", " ", text or "")
    for m in re.finditer(rf"Ordinance\s*(?:No\.?\s*)?{y}\s*-\s*{n}\b", t, re.I):
        seg = t[m.end(): m.end() + 5000]
        nxt = re.search(r"\n\s*[a-z]\.\s+(?:First|Second|Third)?\s*Reading|\n\s*\d{1,2}\.\s+[A-Z]", seg[30:])
        if nxt:
            seg = seg[:30 + nxt.start()]
        mm = None
        for mm in re.finditer(r"Motion made by.{0,300}?to (.{0,200}?)\.\s*The Motion (Passed|Failed|Carried)", seg, re.I | re.S):
            pass
        if not mm:
            if re.search(r"\bwithdr[ae]w", seg, re.I):
                return "Withdrawn"
            continue
        what, outcome = mm.group(1).lower(), mm.group(2).lower()
        failed = outcome == "failed"
        if re.search(r"continu|table|postpone|defer", what):
            return None if failed else "Continued"
        if re.search(r"\bden(y|ial)", what):
            return None if failed else "Denied"
        return "Denied" if failed else "Approved"
    return None


# ------------------------------------------------------------------ fetch
def fetch(since, refresh):
    if pypdf is None:
        raise RuntimeError("pypdf is required for the New Port Richey adapter")
    today = dt.date.today()
    refresh_ids = {}
    for r in refresh or []:
        m = ORD_RE.search(r or "") or re.search(r"(\d{4})-(\d{3,5})", r or "")
        if m:
            refresh_ids[f"{m.group(1)}-{m.group(2)}"] = r
    start_d = dt.date.fromisoformat(since) - dt.timedelta(days=LOOKBACK_DAYS)
    if refresh_ids:
        yr = min(int(k[:4]) for k in refresh_ids)
        start_d = min(start_d, max(dt.date(yr - 1, 9, 1), today - dt.timedelta(days=730)))
    end_d = today + dt.timedelta(days=75)
    try:
        meetings = list_meetings()
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"New Port Richey Granicus unreachable: {e}")
    meetings = sorted((m for m in meetings if start_d.isoformat() <= m["date"] <= end_d.isoformat()),
                      key=lambda m: m["date"])
    log(f"{len(meetings)} Council meetings {start_d}..{end_d}")

    cases = {}
    for mt in meetings:
        try:
            pu = agenda_pdf_url(mt["agenda"])
            rdr = pdf_reader(pu) if pu else None
        except Exception as e:  # noqa: BLE001
            log(f"agenda {mt['date']}: {e}")
            continue
        if not rdr:
            continue
        try:
            items = agenda_items(rdr)
        except Exception as e:  # noqa: BLE001
            log(f"agenda {mt['date']} items: {e}")
            continue
        for title, link in items:
            m = ORD_RE.search(title)
            if not m or not IN_RE.search(title) or OUT_RE.search(title):
                continue
            ono = f"{m.group(1)}-{m.group(2)}"
            res = None
            if mt["minutes"] and mt["date"] <= today.isoformat():
                res = minutes_result(minutes_text(mt["minutes"]), ono)
            c = cases.setdefault(ono, {"apps": []})
            c["apps"].append({"date": mt["date"], "title": title, "link": link, "reading": reading_of(title),
                              "result": res, "agenda": pu, "viewer": mt["agenda"]})
    log(f"{len(cases)} in-scope ordinances")

    out = []
    for ono, c in sorted(cases.items()):
        apps = sorted(c["apps"], key=lambda a: a["date"])
        first = apps[0]
        older = first["reading"] == "second" and first["date"] < (start_d + dt.timedelta(days=45)).isoformat()
        if (first["date"] < since or older) and ono not in refresh_ids:
            continue
        try:
            out.append(build(ono, apps, today.isoformat()))
        except Exception as e:  # noqa: BLE001
            log(f"Ordinance {ono}: skipped ({e})")
    for r in set(refresh_ids) - set(cases):
        log(f"refresh id {refresh_ids[r]} not on any agenda since {start_d}")
    return out


_backup = {}
PASCO_PARCELS = "https://maps.pascopa.com/arcgis/rest/services/Parcels/MapServer/4/query"
_addr_memo = {}


def parcel_by_address(address):
    """Pasco PA parcel for a city site address ('6506 Meadowbrook Lane') when exactly one parcel matches."""
    m = re.match(r"(\d{2,6})\s+(?:([NSEW])\.?\s+)?([A-Za-z0-9']+)", address or "")
    if not m:
        return None
    key = (m.group(1), (m.group(3) or "").upper())
    if key in _addr_memo:
        return _addr_memo[key]
    res = None
    try:
        where = (f"PHYS_STREET LIKE '{key[0]} {(m.group(2) + ' ') if m.group(2) else ''}{key[1].replace(chr(39), chr(39) * 2)}%' "
                 f"AND PHYS_CITY LIKE 'NEW PORT RICHEY%'")
        d = get(PASCO_PARCELS, params={"where": where, "outFields": "ParcelID,PHYS_STREET,VAL_ACRES",
                                       "returnGeometry": "false", "f": "json"}).json()
        feats = d.get("features") or []
        ids = {f["attributes"].get("ParcelID") for f in feats}
        if len(ids) == 1:
            at = feats[0]["attributes"]
            res = {"pin": at.get("ParcelID"), "acres": at.get("VAL_ACRES")}
    except Exception as e:  # noqa: BLE001
        log(f"parcel lookup {address}: {e}")
    _addr_memo[key] = res
    return res



def backup_text(url, pages=4):
    if not url:
        return ""
    if url not in _backup:
        txt = ""
        try:
            rdr = pdf_reader(url)
            if rdr:
                txt = "\n".join((p.extract_text() or "") for p in rdr.pages[:pages])
        except Exception as e:  # noqa: BLE001
            log(f"backup {url[-40:]}: {e}")
        _backup[url] = txt
    return _backup[url]


def build(ono, apps, today):
    title = max((a["title"] for a in apps), key=len)
    kind = kind_of(title)
    head = re.sub(r"^.*?Ordinance\s*(?:No\.?\s*)?\d{4}\s*-\s*\d{3,5}\s*:?\s*", "", title, flags=re.I)
    head = re.sub(r"\b(for|of|at) (\d) (\d{2,5}) ", r"\1 \2\3 ", head)   # PDF text splits '6 800 Trouble Creek'

    m = re.search(r"from (.+?) to (.+?)$", head, re.I)
    f_, t_ = (sq(m.group(1)).strip(" ,.;"), sq(m.group(2)).strip(" ,.;")) if m else (None, None)
    m = re.search(r"\b(?:for|of|at) (\d{2,6} [A-Za-z0-9 .]+?(?:Lane|Ln|Road|Rd|Street|St|Avenue|Ave|Drive|Dr|Boulevard|Blvd|Highway|Hwy|"
                  r"Court|Ct|Way|Place|Pl|Circle|Cir|Trail|Parkway|Pkwy)\b\.?)", head, re.I)
    address = sq(m.group(1)) if m else None
    bt = ""
    for a in reversed(apps):
        bt = backup_text(a["link"])
        if bt:
            break
    pins = list(dict.fromkeys("-".join(g) for g in PIN_RE.findall(bt)))
    acres = None
    m = re.search(r"([\d,]*\.?\d+)\s*(?:\+/-|±)?\s*-?\s*acres", bt, re.I) or re.search(r"([\d,]*\.?\d+)\s*acres", title, re.I)
    if m:
        try:
            v = float(m.group(1).replace(",", ""))
            acres = v if 0 < v < 50000 else None
        except ValueError:
            acres = None
    applicant = None
    m = re.search(r"(?:Applicant|Owner|Petitioner)\s*(?:/\s*(?:Owner|Agent))?\s*:\s*([^\n]{3,80})", bt)
    if m:
        applicant = sq(m.group(1))
    else:
        m = re.search(r"(?:request(?:ed)? by|application (?:submitted )?by|on behalf of|The applicant,)\s+([A-Z][^,.;\n]{2,70})", bt)
        applicant = sq(m.group(1)) if m else None
    if not address:
        m = re.search(r"(?:located at|address(?:ed)? (?:of|is))\s+(\d{2,6} [^,;\n]{3,60})", bt, re.I)
        address = sq(m.group(1)) if m else None
    if not pins and address:
        p = parcel_by_address(address)
        if p and p.get("pin"):
            pins = [p["pin"]]
    case = re.search(r"\b((?:LUA|RZ|REZ|PUD|MPUD|ZA|ZMA|CPA|LSCPA|SSCPA)-\d{2}-\d{2}-\d{3,5})(?!\d)", bt)
    is_pd = kind == "RZ" and bool(re.search(r"\bPUD\b|\bMPUD\b|Planned (?:Unit )?Development", title + " " + (t_ or ""), re.I))
    category = "Comp Plan Amendment" if kind == "CPA" else ("PD" if is_pd else "Rezoning")
    app_type = "Future Land Use Map Amendment" if kind == "CPA" else ("PD Rezoning" if is_pd else "Rezoning")
    pname = re.sub(r"^(?:FLUM:?\s*)?(?:FLUM\s+)?Amendment for\s+|^Amend the Future Land Use Map (?:for|of)\s+|"
                   r"^(?:Rezoning|Rezone|Zoning (?:Map )?Amendment)\s+(?:for\s+|of\s+)?", "", head, flags=re.I)
    pname = re.split(r",?\s+from\s+", pname, flags=re.I)[0].strip(" :,") or None
    if pname and len(pname) > 90:
        pname = pname[:87] + "..."

    hearings = []
    for a in apps:
        rd = a["reading"]
        body = f"City Council ({rd} reading)" if rd else CC
        h = {"body": body, "date": a["date"], "result": a["result"], "_rd": rd}
        prev = next((x for x in hearings if x["body"] == h["body"] and x["date"] == h["date"]), None)
        if prev:
            prev["result"] = prev["result"] or h["result"]
        else:
            hearings.append(h)
    hearings.sort(key=lambda h: h["date"])
    rank = {"first": 1, "transmittal": 1, "second": 2, None: 0}
    for i, h in enumerate(hearings):
        later = [x for x in hearings[i + 1:] if x["date"] > h["date"]]
        if h["result"] is not None or not later or h["date"] > today:
            continue
        if h["_rd"] and any(x["_rd"] == h["_rd"] for x in later):
            h["result"] = "Continued"
        elif any(rank.get(x["_rd"], 0) > rank.get(h["_rd"], 0) for x in later):
            h["result"] = "Approved" if h["_rd"] in ("first", "transmittal") else None
    for h in hearings:
        if h["_rd"] in ("first", "transmittal") and h["result"] == "Approved":
            h["result"] = "Approved on first reading" if h["_rd"] == "first" else "Transmitted"
    status, raw, decision = status_from(hearings, today)
    desc = sq(title)
    desc = desc[:260] + "..." if len(desc) > 263 else desc
    if case:
        desc = f"{desc} (city application {case.group(1)})"
    latest = apps[-1]
    return {
        "jurisdiction": JUR, "county": COUNTY, "app_num": f"Ordinance No. {ono}", "category": category,
        "app_type": app_type, "project_name": pname, "description": desc, "applicant": applicant,
        "parcel_ids": pins, "address": address, "acres": acres,
        "from_zoning": f_ if kind == "RZ" else None, "to_zoning": t_ if kind == "RZ" else None,
        "from_flu": f_ if kind == "CPA" else None, "to_flu": t_ if kind == "CPA" else None,
        "filed_date": apps[0]["date"], "status": status, "status_raw": raw,
        "hearings": [{"body": h["body"], "date": h["date"], "result": h["result"]} for h in hearings],
        "decision_date": decision, "url": latest["viewer"], "docs_url": latest["link"] or latest["agenda"],
        "lat": None, "lon": None, "source": SOURCE,
    }


def status_from(hearings, today):
    past = [h for h in hearings if h["date"] <= today]
    future = [h for h in hearings if h["date"] > today]
    if any(h["result"] == "Withdrawn" for h in past):
        h = [x for x in past if x["result"] == "Withdrawn"][-1]
        return "Withdrawn", f"{h['body']} {h['date']}: withdrawn", None
    den = [h for h in past if h["result"] == "Denied"]
    if den:
        return "Denied", f"{den[-1]['body']} {den[-1]['date']}: denied", den[-1]["date"]
    fin = [h for h in past if h["result"] == "Approved" and h["_rd"] not in ("first", "transmittal")]
    if fin:
        return "Approved", f"{fin[-1]['body']} {fin[-1]['date']}: approved (minutes)", fin[-1]["date"]
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
            return "Filed", f"{lp['body']} {lp['date']}: {lp['result']}; next reading not yet on an agenda", None
        return "Hearing Scheduled", f"{lp['body']} {lp['date']} held; minutes not yet posted", None
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
