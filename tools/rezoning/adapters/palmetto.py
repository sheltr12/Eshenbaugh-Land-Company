#!/usr/bin/env python3
"""City of Palmetto (Manatee County) rezonings, planned developments and future land use map
amendments.

Sources (both public, no login):
  * City Commission agendas on TownCloud (towncloud.io/go/palmetto-fl): the agenda archive table
    (JSON behind the archive page) and each meeting's HTML agenda. Land use ordinances appear as
    "FIRST READING ORDINANCE 2026-12 ...", "PUBLIC HEARING ORDINANCE 2026-12: RE-ZONE-..." and
    "CONTINUED PUBLIC HEARING-ORDINANCE 2026-11 GDP ..." followed by the ordinance title. The
    consent agenda carries the approved minutes of earlier meetings as PDF attachments; their
    "MOTION: ... the motion carried 5-0 to approve Ordinance No. 2026-13" blocks give outcomes.
  * Planning and Zoning Board agendas in the city's CivicPlus Document Center (one folder per
    meeting, listed on palmettofl.org/226/Agendas): the "PZ Agenda" PDF names the case
    ("Rezone RZ-2026-03 / Ordinance 2026-12"); TAB 1 of a meeting is the previous meeting's
    minutes (Board recommendations).
Palmetto publishes no application register, so app_num = the ordinance number
("Ordinance No. 2026-12"); the case number (RZ-yyyy-nn, CPA-...) is added to app_type when the
P&Z agenda gives it. Kept: rezonings (PD when the new district is a planned development, PD-*),
standalone General Development Plan ordinances (PD plans / amendments; a GDP heard together with
its PD rezoning is folded into the rezoning record), and comprehensive plan amendments that change
the Future Land Use Map. Skipped: vacations, annexations, conditional uses, moratoria, text
amendments.
Parcel IDs: Manatee PA ids (e.g. 2700200209), looked up from the site address in the Manatee PAO
parcel layer (single exact match), since agendas do not print them.
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

JURISDICTIONS = ["City of Palmetto"]
JUR = "City of Palmetto"
TC = "https://towncloud.io/go/palmetto-fl"
CP = "https://www.palmettofl.org"
MANATEE = "https://gis.manateepao.gov/arcgis/rest/services/Website/WebLayers/MapServer/0/query"
SOURCE = "Palmetto TownCloud City Commission agendas + CivicPlus P&Z agendas"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
CC, PZ = "City Commission", "Planning and Zoning Board"
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
MAX_PDF = 12_000_000

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


def log(*a):
    print("[palmetto]", *a, file=sys.stderr, flush=True)


def req(method, url, tries=3, sleep=0.4, timeout=90, **kw):
    last = None
    for i in range(tries):
        try:
            r = _s.request(method, url, timeout=timeout, **kw)
            r.raise_for_status()
            time.sleep(sleep)
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"{method} failed {url}: {last}")


def sq(s):
    return re.sub(r"\s+", " ", s or "").strip()


def html_lines(h):
    t = re.sub(r"<script.*?</script>|<style.*?</style>", "", h, flags=re.S | re.I)
    t = _html.unescape(re.sub(r"<[^>]+>", "\n", t)).replace("\xa0", " ")
    return [sq(x) for x in t.split("\n") if sq(x)]


def pdf_text(url, max_pages=40):
    if pypdf is None:
        return ""
    try:
        r = req("GET", url, timeout=150)
        if len(r.content) > MAX_PDF or r.content[:4] != b"%PDF":
            return ""
        rdr = pypdf.PdfReader(io.BytesIO(r.content))
        return "\n".join((p.extract_text() or "") for p in rdr.pages[:max_pages])
    except Exception as e:  # noqa: BLE001
        log(f"pdf {url[-60:]}: {e}")
        return ""


def norm_nums(t):
    """'2026- 12' / '2026 -12' (OCR) -> '2026-12'."""
    return re.sub(r"\b(20\d\d)\s*-\s*(\d{1,3})\b", r"\1-\2", t or "")


def long_date(s):
    m = re.search(rf"({MONTHS})\s+(\d{{1,2}}),?\s+(\d{{4}})", s or "")
    if not m:
        return None
    return dt.datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y").date().isoformat()


# ---------------------------------------------------------------- City Commission (TownCloud)
def tc_meetings(start, end):
    r = req("GET", f"{TC}/agendas/archive")
    tok = re.search(r'name="csrf-token" content="([^"]+)"', r.text).group(1)
    ctx = _html.unescape(re.search(r'id="agenda-list"><table[^>]*data-context="([^"]+)"', r.text).group(1))
    out, pos = [], 0
    while pos < 2000:
        data = {"draw": 1, "start": pos, "length": 100, "table_class": "Meetings::AgendaArchiveTable", "context": ctx}
        for i, c in enumerate(["name", "meeting_date", "agenda_pdf", "minutes_pdf", "agenda_packets"]):
            data[f"columns[{i}][data]"] = c
            data[f"columns[{i}][name]"] = c
        d = req("POST", f"{TC}/agendas/table_data", data=data, headers={
            "X-CSRF-Token": tok, "X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}).json()
        rows = d.get("data", [])
        oldest = None
        for x in rows:
            md = long_date(x.get("meeting_date"))
            if not md:
                continue
            oldest = md if oldest is None else min(oldest, md)
            name = x.get("name") or ""
            if not re.search(r"City Commission Meeting", name, re.I) or re.search(r"Workshop|CRA", name, re.I):
                continue
            if start <= md <= end:
                link = (x.get("DT_RowData") or {}).get("link")
                if link:
                    out.append({"date": md, "url": "https://towncloud.io" + link, "body": CC})
        if not rows or (oldest and oldest < start) or len(rows) < 100:
            break
        pos += 100
    return out


HEAD_RX = re.compile(r"^(CONTINUED\s+)?(FIRST READING|SECOND READING|PUBLIC HEARING)[\s:-]*(?:OF\s+)?ORDINANCE\s+(?:NO\.?\s*)?"
                     r"(20\d\d-\d{1,3})\b[\s:-]*(.*)$", re.I)


def parse_cc_agenda(mt, h, data):
    lines = html_lines(h)
    for i, ln in enumerate(lines):
        m = HEAD_RX.match(norm_nums(ln))
        if not m:
            continue
        o = m.group(3)
        title = []
        for nx in lines[i + 1:i + 12]:
            if re.match(r"^(ACTION REQUEST|OPEN PUBLIC HEARING|Attachments:|CLOSE PUBLIC)", nx, re.I) or HEAD_RX.match(norm_nums(nx)):
                break
            if re.match(r"^THIS WAS CONTINUED", nx, re.I):
                continue
            title.append(nx)
        kind = "first" if re.search(r"FIRST READING", m.group(2), re.I) else "hearing"
        data["ords"].setdefault(o, {"apps": [], "title": None, "label": None, "case": None})
        ent = data["ords"][o]
        t = sq(" ".join(title))
        if t and (not ent["title"] or len(t) > len(ent["title"])):
            ent["title"] = t
        ent["label"] = ent["label"] or sq(m.group(4)) or None
        ent["apps"].append({"body": CC, "date": mt["date"], "kind": kind, "continued": bool(m.group(1)), "url": mt["url"]})
    # minutes attachments on the consent agenda
    for m in re.finditer(r"<h6>\)?\s*Minutes[^<]*</h6>(.*?)</li>\s*</ul>", h, re.S | re.I):
        for u in re.findall(r'href="(https://[^"]+\.pdf)"', m.group(1)):
            data["cc_minutes"].add(u)


# ---------------------------------------------------------------- P&Z (CivicPlus Document Center)
def pz_folders(start, end):
    h = req("GET", f"{CP}/226/Agendas").text
    out = []
    for fid, label in re.findall(r'href="/DocumentCenter/Index/(\d+)"[^>]*>(.*?)</a>', h, re.S):
        d = long_date(sq(_html.unescape(re.sub(r"<[^>]+>", " ", label))))
        if d and start <= d <= end and "CONTINUED" not in label.upper():
            out.append({"id": fid, "date": d})
    return out


def pz_docs(fid):
    body = {"folderId": int(fid), "getDocuments": 1, "imageRepo": False, "renderMode": 0, "loadSource": 7,
            "requestingModuleID": 75, "searchString": "", "pageNumber": 1, "rowsPerPage": 100,
            "sortColumn": "Name", "sortOrder": 0}
    d = req("POST", f"{CP}/Admin/DocumentCenter/Home/Document_AjaxBinding?renderMode=0&loadSource=7",
            data=json.dumps(body), headers={"Content-Type": "application/json"}).json()
    return [{"name": x.get("DisplayName") or "", "type": x.get("FileType"), "kb": x.get("FileSize") or 0,
             "url": CP + x["URL"] if (x.get("URL") or "").startswith("/") else x.get("URL")}
            for x in d.get("Documents", []) if x.get("URL")]


def parse_pz_agenda(mt, text, docs, data):
    t = norm_nums(text)
    for m in re.finditer(r"PUBLIC\s+HEARING\s+(?:-\s*)?ORDINANCE\s+(20\d\d-\d{1,3})\s*[:-]?\s*(.*?)\(?\s*TAB\s*(\d+)\s*\)(.*?)(?=\n\s*\d{1,2}\.\s+[A-Z]|\Z)",
                         t, re.S | re.I):
        o, label, tab, rest = m.group(1), sq(m.group(2)), m.group(3), m.group(4)
        ent = data["ords"].setdefault(o, {"apps": [], "title": None, "label": None, "case": None})
        tm = re.search(r"(AN ORDINANCE OF THE CITY OF PALMETTO.*?EFFECTIVE DATE\.?)", rest, re.S | re.I)
        if tm and not ent["title"]:
            ent["title"] = sq(tm.group(1))
        ent["label"] = ent["label"] or label or None
        cm = re.search(r"\b((?:RZ|CPA|PD|GDP|SSA|LSA|FLUM|ZC)\s*-?\s*20\d\d\s*-\s*\d{1,3})\b", rest)
        if cm and not ent["case"]:
            ent["case"] = re.sub(r"\s+", "", cm.group(1))
        tabdoc = next((d for d in docs if re.fullmatch(rf"TAB\s*{tab}", d["name"].strip(), re.I)), None)
        ent["apps"].append({"body": PZ, "date": mt["date"], "kind": "pz", "continued": False,
                            "url": mt["agenda_url"], "tab": tabdoc["url"] if tabdoc else None})


# ---------------------------------------------------------------- minutes outcomes
def _motion_result(blk, body):
    b = sq(blk).lower()
    if re.search(r"re-?open|failed|lack of a second|\bdied\b", b):
        return None
    if re.search(r"\bcontinu", b):
        return "Continued"
    if "carried" not in b:
        return None
    if re.search(r"\bto deny|move to deny|moved to deny|recommend denial", b):
        return "Denied" if body == CC else "Recommended Denial"
    if re.search(r"\bto approve|to adopt|move to approve|moved to approve|recommend approval|to recommend", b):
        if body == PZ:
            return "Recommended Approval"
        return "Approved on first reading" if "first reading" in b else "Approved"
    return None


def minutes_outcomes(text, body):
    """{(ordinance, meeting date): result} from minutes text. Minutes are split into agenda
    sections ('8. PUBLIC HEARING ORDINANCE 2026-12 ...'); the last decisive MOTION of a section
    gives the result for the ordinance(s) named in its heading (or, failing that, in the motion)."""
    t = norm_nums(text)
    heads = [(m.start(), long_date(m.group(1))) for m in
             re.finditer(rf"(?:City Commission|Planning and Zoning Board|Planning & Zoning Board)[^\n]{{0,40}}\n?\s*"
                         rf"((?:{MONTHS})\s+\d{{1,2}},?\s+\d{{4}})", t)]
    out = {}
    secs = [m.start() for m in re.finditer(r"\n\s*\d{1,2}\.\s+(?=[A-Z])", t)] + [len(t)]
    for a, b in zip(secs, secs[1:]):
        sec = t[a:b]
        head = sec[:400].split("\n\n")[0]
        ords = set(re.findall(r"ORDINANCE\s+(?:NO\.?\s*)?(20\d\d-\d{1,3})", head[:300], re.I))
        d = None
        for pos, hd in heads:
            if pos <= a + 200 and hd:
                d = hd
        if not d:
            continue
        res, mords = None, set()
        for m in re.finditer(r"MOTION\s*:(.{0,900}?)(?=MOTION\s*:|\Z)", sec, re.S):
            r = _motion_result(m.group(1), body)
            if r:
                res = r
                mords = set(re.findall(r"Ord(?:inance|\.)\s+(?:No\.?\s*)?(20\d\d-\d{1,3})", m.group(1), re.I))
        for o in (ords or mords):
            if res:
                out[(o, d)] = res
    return out


# ---------------------------------------------------------------- record building
def classify(title):
    t = (title or "").upper()
    if re.search(r"MORATORIUM|VACATING|ANNEXING|EVALUATION AND APPRAISAL", t):
        return None
    if re.search(r"\bREZONING\s+APPROXIMATELY|\bREZONING\s+(?:CERTAIN|THE|REAL)|ZONING ATLAS|ZONING MAP", t):
        return "rz"
    if re.search(r"GENERAL DEVELOPMENT PLAN|PLANNED DEVELOPMENT", t) and "ACRES" in t:
        return "gdp"
    if "COMPREHENSIVE PLAN" in t and re.search(r"FUTURE LAND USE MAP|FUTURE LAND USE DESIGNATION|FLUM", t) and "ACRES" in t:
        return "cpa"
    return None


def parse_title(t):
    u = sq(t)
    out = {}
    m = re.search(r"APPROXIMATELY\s+([\d,]*\.?\d+)\s*\+?/?-?\s*ACRES", u, re.I)
    if m:
        try:
            out["acres"] = float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    m = re.search(r"LOCATED AT\s+(\d+[^,;]+?)(?:,\s*PALMETTO|,|;)", u, re.I)
    if m:
        out["address"] = m.group(1).strip()
    m = re.search(r"FROM\s+(?:CITY|COUNTY|MANATEE COUNTY)?\s*(?:ZONING\s+)?(.+?)\s+TO\s+(?:CITY\s+)?(?:ZONING\s+)?(.+?),?\s+AND AS MORE", u, re.I)
    if m:
        out["from"], out["to"] = m.group(1).strip(" ,"), m.group(2).strip(" ,")
    m = re.search(r"FUTURE LAND USE (?:MAP )?(?:DESIGNATION|CATEGORY|CLASSIFICATION)?.*?FROM\s+(.+?)\s+TO\s+(.+?)(?:,|;|\s+AND AS MORE)", u, re.I)
    if m:
        out["from_f"], out["to_f"] = m.group(1).strip(" ,"), m.group(2).strip(" ,")
    m = re.search(r"\(([A-Z0-9][^()]{2,80}?(?:LLC|INC|L\.L\.C\.|ASSOCIATION|TRUST|CORP|COMPANY|LP|LTD)\.?)\)\s*$", u, re.I)
    if m:
        out["applicant"] = m.group(1).strip()
    return out


def parcel_by_address(addr):
    m = re.match(r"\s*(\d+)\s+(.+?)\s*$", addr or "")
    if not m:
        return None
    a = re.sub(r"\bAVENUE\b", "AVE", m.group(2).upper())
    a = re.sub(r"\bSTREET\b", "ST", a)
    a = re.sub(r"\bDRIVE\b", "DR", a)
    a = re.sub(r"\bWEST\b", "W", re.sub(r"\bEAST\b", "E", re.sub(r"\bNORTH\b", "N", re.sub(r"\bSOUTH\b", "S", a))))
    a = re.sub(r"\bTH\s+", "TH ", a).replace(".", "")
    where = f"SITUS_ADDRESS = '{m.group(1)} {a}'".replace("''", "'")
    try:
        d = req("GET", MANATEE, params={"where": where, "outFields": "PARID,SITUS_ADDRESS,LAND_ACREAGE_CAMA,PAR_OWNER_NAME1",
                                         "returnGeometry": "false", "f": "json"}, tries=2).json()
    except Exception as e:  # noqa: BLE001
        log(f"parcel lookup {addr}: {e}")
        return None
    fs = [f["attributes"] for f in d.get("features", [])]
    ids = {f.get("PARID") for f in fs if f.get("PARID")}
    if len(ids) != 1:
        return None
    return {"pin": fs[0]["PARID"], "acres": fs[0].get("LAND_ACREAGE_CAMA")}


def collect(start, end):
    data = {"ords": {}, "cc_minutes": set(), "pz_minutes": set()}
    try:
        mts = tc_meetings(start, end)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Palmetto TownCloud unreachable: {e}") from e
    log(f"{len(mts)} City Commission agendas since {start}")
    for mt in sorted(mts, key=lambda x: x["date"]):
        try:
            parse_cc_agenda(mt, req("GET", mt["url"]).text, data)
        except Exception as e:  # noqa: BLE001
            log(f"agenda {mt['date']}: {e}")
    try:
        fols = pz_folders(start, end)
    except Exception as e:  # noqa: BLE001
        log(f"P&Z agenda list failed: {e}")
        fols = []
    log(f"{len(fols)} P&Z agenda folders since {start}")
    for f in sorted(fols, key=lambda x: x["date"]):
        try:
            docs = pz_docs(f["id"])
        except Exception as e:  # noqa: BLE001
            log(f"P&Z folder {f['id']}: {e}")
            continue
        ag = next((d for d in docs if re.search(r"PZ\s*Agenda", d["name"], re.I) and (d["type"] or "").lower() == "pdf"), None)
        if ag:
            f["agenda_url"] = ag["url"]
            parse_pz_agenda(f, pdf_text(ag["url"]), docs, data)
        tab1 = next((d for d in docs if re.fullmatch(r"TAB\s*1", d["name"].strip(), re.I)), None)
        if tab1 and (tab1["kb"] or 0) * 1024 < MAX_PDF:
            data["pz_minutes"].add(tab1["url"])
    return data


def fetch(since, refresh=None):
    today = dt.date.today()
    refresh = {re.sub(r"\s+", " ", r.strip()).upper() for r in (refresh or []) if r and r.strip()}
    start = dt.date.fromisoformat(since) - dt.timedelta(days=150)
    if refresh:
        start = min(start, today - dt.timedelta(days=450))
    data = collect(start.isoformat(), (today + dt.timedelta(days=60)).isoformat())
    cands = {o: e for o, e in data["ords"].items() if classify(e.get("title")) or
             (not e.get("title") and re.search(r"RE-?ZONE|REZONING|\bGDP\b|FLUM", e.get("label") or "", re.I))}
    # outcomes from minutes, only for meetings that matter
    outcomes = {}
    if cands:
        for u in sorted(data["cc_minutes"]):
            outcomes.update(minutes_outcomes(pdf_text(u, 60), CC))
        for u in sorted(data["pz_minutes"]):
            outcomes.update(minutes_outcomes(pdf_text(u, 30), PZ))
    log(f"{len(cands)} land use ordinances; {len(outcomes)} minute outcomes")
    recs = {}
    for o, e in sorted(cands.items()):
        kind = classify(e.get("title")) or ("rz" if re.search(r"RE-?ZONE|REZONING", e.get("label") or "", re.I) else
                                             "gdp" if "GDP" in (e.get("label") or "").upper() else "cpa")
        try:
            recs[o] = build(o, e, kind, outcomes, today.isoformat())
        except Exception as ex:  # noqa: BLE001
            log(f"{o}: skipped ({type(ex).__name__}: {ex})")
    # fold a GDP heard together with its PD rezoning into the rezoning record
    for o, r in list(recs.items()):
        if r is None or r["_kind"] != "gdp":
            continue
        twin = next((x for x in recs.values() if x and x["_kind"] == "rz" and x.get("address") and r.get("address")
                     and x["address"].upper().split()[0:2] == r["address"].upper().split()[0:2]), None)
        if twin:
            twin["description"] = sq((twin.get("description") or "") + f" | With {r['app_num']} (General Development Plan): "
                                     + (r.get("description") or ""))[:600]
            seen = {(h["body"], h["date"]) for h in twin["hearings"]}
            twin["hearings"] = sorted(twin["hearings"] + [dict(h, body=h["body"] + " (GDP)") for h in r["hearings"]
                                                          if (h["body"], h["date"]) not in seen], key=lambda h: h["date"])
            twin["category"] = "PD"
            recs[o] = None
    out = []
    for o, r in sorted(recs.items()):
        if not r:
            continue
        r.pop("_kind", None)
        if r["filed_date"] < since and r["app_num"].upper() not in refresh:
            continue
        out.append({k: (None if v == "" else v) for k, v in r.items()})
    return out


def build(o, e, kind, outcomes, today):
    apps = sorted(e["apps"], key=lambda a: a["date"])
    info = parse_title(e.get("title") or "")
    hs = {}
    for a in apps:
        body = a["body"]
        if body == CC:
            body = CC + (" (first reading)" if a["kind"] == "first" else " (public hearing)")
        k = (body, a["date"])
        res = outcomes.get((o, a["date"]))
        if res is None and a["date"] < today and any(x["date"] > a["date"] and x["continued"] and x["body"] == a["body"]
                                                     and x["kind"] == a["kind"] for x in apps):
            res = "Continued"
        hs[k] = {"body": body, "date": a["date"], "result": res}
    hl = sorted(hs.values(), key=lambda h: h["date"])
    for h in hl:  # a public hearing heard again later was continued, whatever a motion said that night
        if h["body"].endswith("(public hearing)") and h["date"] < today and \
                any(x["body"] == h["body"] and x["date"] > h["date"] for x in hl):
            h["result"] = "Continued"
    final = [h for h in hl if h["body"].startswith(CC + " (public") and h["result"] in ("Approved", "Denied") and h["date"] <= today]
    future = [h for h in hl if h["date"] >= today]
    past = [h for h in hl if h["date"] < today]
    decision = None
    if final:
        status, decision = final[-1]["result"], final[-1]["date"]
        raw = f"City Commission {decision}: {status} (minutes)"
    elif future:
        status, raw = "Hearing Scheduled", f"next {future[0]['body']} {future[0]['date']}"
    elif past and past[-1]["result"] == "Continued":
        status, raw = "Continued", f"{past[-1]['body']} {past[-1]['date']}: continued"
    elif past:
        lp = past[-1]
        status = "Filed"
        raw = f"{lp['body']} {lp['date']}: " + (lp["result"] or "held; outcome not yet posted")
    else:
        status, raw = "Filed", None
    pids, acres = [], info.get("acres")
    addr = info.get("address")
    if not addr:
        m = re.search(r"(\d{2,6}\s+\d*[A-Z0-9 ]+?\b(?:AVE|AVENUE|ST|STREET|DR|DRIVE|RD|ROAD|BLVD|LN|WAY|CT|PL)\b\.?\s*(?:[NSEW]\b|WEST|EAST|NORTH|SOUTH)?)",
                      e.get("label") or "", re.I)
        addr = m.group(1).strip() if m else None
    if addr:
        p = parcel_by_address(addr)
        if p:
            pids = [p["pin"]]
            if acres is None and p.get("acres"):
                acres = round(float(p["acres"]), 3)
    to_z = info.get("to")
    if kind == "rz":
        category = "PD" if re.search(r"\bPD\b|PD-|PLANNED DEVELOPMENT", to_z or "", re.I) else "Rezoning"
        app_type = "Rezoning"
    elif kind == "gdp":
        category, app_type = "PD", "General Development Plan (Planned Development)"
    else:
        category, app_type = "Comp Plan Amendment", "Comprehensive Plan Amendment (Future Land Use Map)"
    if e.get("case"):
        app_type += f" ({e['case']})"
    title = e.get("title") or e.get("label") or ""
    lab = e.get("label") or ""
    appl = info.get("applicant")
    if not appl:
        m = re.match(r"(?:RE-?ZONE\s*-\s*)?([A-Z][A-Z0-9 .,&'/-]{3,80}?)(?:\s*-\s*\d|\s*\(|$)", lab)
        appl = m.group(1).strip(" -,") if m and not re.search(r"^(GDP|RE-?ZONE|PLANNING)$", m.group(1).strip(), re.I) else None
    if appl:
        appl = re.sub(r"^(GDP|RE-?ZONE)\s*-\s*", "", appl, flags=re.I).strip() or None
    urls = [a for a in apps if a.get("url")]
    tab = next((a["tab"] for a in reversed(apps) if a.get("tab")), None)
    return {
        "_kind": kind,
        "jurisdiction": JUR, "county": "Manatee", "app_num": f"Ordinance No. {o}", "category": category,
        "app_type": app_type, "project_name": re.sub(r"\s*\((PLANNING|LEGAL)\)\s*$", "", lab, flags=re.I) or None,
        "description": re.sub(r"^\d{1,2}:\d{2}\s*[AP]M\s*(or After)?\s*", "", title, flags=re.I)[:600] or None,
        "applicant": appl,
        "parcel_ids": pids, "address": addr, "acres": acres,
        "from_zoning": info.get("from") if kind != "cpa" else None, "to_zoning": to_z if kind != "cpa" else None,
        "from_flu": info.get("from_f") if kind == "cpa" else None, "to_flu": info.get("to_f") if kind == "cpa" else None,
        "filed_date": apps[0]["date"], "status": status, "status_raw": raw,
        "hearings": hl, "decision_date": decision if status in ("Approved", "Denied") else None,
        "url": urls[-1]["url"] if urls else f"{TC}/agendas/archive", "docs_url": tab,
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
