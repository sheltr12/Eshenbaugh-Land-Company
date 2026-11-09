#!/usr/bin/env python3
"""City of Lake Wales (Polk County) rezoning / PDP / future land use map amendment adapter.

Source: the city's CivicPlus AgendaCenter (https://www.lakewalesfl.gov/AgendaCenter), category 3
"Planning & Zoning Board" and category 4 "City Commission Regular Meeting" (10 = special meetings).
Agendas are published as HTML (ViewFile/Agenda/_MMDDYYYY-NNN?html=true) with one block per item
(title, description, linked PDFs such as "PZ Staff Report Oakwood Ridge FLUM, Zoning, and PSP",
"ORD 2026-35 FLUM MidFlorida Parking.pdf", "CC MEMO SR 60 Industrial Annex, FLUM, and Zoning").
 * P&Z items: "MidFlorida Parking Lot - Future Land Use Map Amendment and Zoning Map Amendment -
   Public Hearing Required". City Commission items: "ORDINANCE 2026-25 SR 60 Industrial Future Land
   Use Map Amendment - 1st Reading and Public Hearing".
 * The staff report / memo PDF (first pages only) gives parcel ids ("PID: 27-30-02-903500-002215",
   "Parcel IDs: 283005000000011020, ..."), acreage, applicant / agent and the request.
 * City Commission minutes PDFs (text) give outcomes ("made a motion to adopt ORDINANCE 2026-15 after
   2nd reading ... Motion passed 4-0"). P&Z minutes are scanned images and are not read.
Each item can hold several requests; it becomes one record per kind: Comp Plan Amendment (FLUM /
small or large scale comprehensive plan map amendment), Rezoning (zoning map amendment) and PD
(Residential Planned Development Project (PDP) approvals and major modifications; Lake Wales grants
PDPs through a special exception use permit, but they are the city's planned development zoning
tool, so they are kept). app_num = the ordinance number ("Ordinance 2026-25") when the case's first
appearance shows it (title or attached ordinance file name), otherwise "<project> (<request>)" from
that first appearance; later appearances are linked by ordinance number or project name.
Text amendments (Chapter 23), annexation-only ordinances, plats, site plans and other special
exceptions are skipped. Requires pypdf.
"""
import datetime as dt
import html as _html
import io
import json
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

try:
    import pypdf
except ImportError:  # pragma: no cover
    pypdf = None

JURISDICTIONS = ["City of Lake Wales"]
JUR = "City of Lake Wales"
BASE = "https://www.lakewalesfl.gov"
SOURCE = "Lake Wales AgendaCenter (P&Z Board + City Commission agendas, staff reports, minutes)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
LOOKBACK_DAYS = 365      # first appearance (and key) must stay in the scanned window
CATS = {3: "Planning & Zoning Board", 4: "City Commission", 10: "City Commission"}
PZ = "Planning & Zoning Board"
CC = "City Commission"

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


def log(*a):
    print("[lake_wales]", *a, file=sys.stderr)


def req(method, url, tries=6, sleep=0.5, **kw):
    last = None
    for i in range(tries):
        try:
            r = _s.request(method, url, timeout=120, **kw)
            r.raise_for_status()
            time.sleep(sleep)
            return r
        except Exception as e:  # noqa: BLE001  (the server intermittently resets connections)
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"{method} failed {url}: {last}")


def sq(s):
    s = re.sub(r"\s+", " ", _html.unescape(s or "").replace("\u00a0", " ")).strip()
    return s or None


def nd(s):
    return (s or "").replace("\u2013", "-").replace("\u2014", "-").replace("\u2019", "'")


def pdf_text(url, max_pages=None):
    rdr = pypdf.PdfReader(io.BytesIO(req("GET", url).content))
    out = []
    for p in rdr.pages[:max_pages] if max_pages else rdr.pages:
        try:
            out.append(p.extract_text() or "")
        except Exception:  # noqa: BLE001
            out.append("")
    return nd("\n".join(out))


# ------------------------------------------------------------------ AgendaCenter
def list_rows(year, cat):
    t = req("POST", f"{BASE}/AgendaCenter/UpdateCategoryList",
            data={"year": year, "catID": cat, "startDate": "", "endDate": "", "term": "",
                  "prevVersionScreen": "false"}).text
    out = []
    for row in re.findall(r'<tr[^>]*class="catAgendaRow"[^>]*>(.*?)</tr>', t, re.S):
        a = re.search(r"ViewFile/Agenda/(_(\d{2})(\d{2})(\d{4})-(\d+))", row)
        if not a:
            continue
        mn = re.search(r'href="(/AgendaCenter/ViewFile/Minutes/[^"]+)"', row)
        out.append({"date": f"{a.group(4)}-{a.group(2)}-{a.group(3)}", "aid": a.group(1), "body": CATS[cat],
                    "url": f"{BASE}/AgendaCenter/ViewFile/Agenda/{a.group(1)}?html=true",
                    "minutes": BASE + mn.group(1) if mn else None})
    return out


def agenda_items(url):
    soup = BeautifulSoup(req("GET", url).content.decode("utf-8", "replace"), "lxml")
    out = []
    for it in soup.select("div.item"):
        ti = it.select_one(".title")
        if not ti:
            continue
        docs = [(sq(a.get_text()) or "", BASE + a["href"]) for a in it.select(".documents a.file") if a.get("href")]
        desc = it.select_one(".desc")
        out.append({"title": nd(sq(ti.get_text(" "))) or "", "desc": nd(sq(desc.get_text(" ")) if desc else None),
                    "docs": docs})
    return out


# ------------------------------------------------------------------ classification
ORD_RE = re.compile(r"\bORD(?:INANCE)?\.?\s*(?:NO\.?\s*)?(20\d{2})\s*-\s*(\d{1,3})\b", re.I)
SKIP_RE = re.compile(r"Chapter 23|Land Development Regulations|Code of Ordinances|Impact Fee|Parcel Specific|"
                     r"Roadway Annexation|Right-of-Way|Board Appointment|Minutes", re.I)


def kinds_of(title):
    """[(kind, label)] for an item title; kind CPA / ZONING / PD."""
    t = title
    if SKIP_RE.search(t):
        return []
    out = []
    if re.search(r"Future Land Use Map|Land Use Map Amendment|Comprehensive Plan Map Amendment|\bFLUM\b", t, re.I):
        m = re.search(r"(Small|Large)[- ]Scale", t, re.I)
        out.append(("CPA", f"{m.group(1).title()}-Scale Comprehensive Plan Map Amendment" if m else "Future Land Use Map Amendment"))
    if re.search(r"Zoning Map Amendment|Rezon|Zoning Map\b", t, re.I):
        out.append(("ZONING", "Zoning Map Amendment"))
    if re.search(r"Planned Development Project|\bPDP\b|Planned Unit Development|\bPUD\b", t, re.I) and \
            not re.search(r"minor modification", t, re.I):
        out.append(("PD", "Residential Planned Development Project (PDP)" +
                    (" Major Modification" if re.search(r"modification|amendment", t, re.I) else "")))
    return out


def project_of(title):
    t = ORD_RE.sub(" ", title)
    t = re.sub(r"\s*-\s*(?:1st|2nd|First|Second)\s+Reading.*$", "", t, flags=re.I)
    t = re.sub(r"\s*-\s*Public Hearing.*$", "", t, flags=re.I)
    parts = [p.strip() for p in re.split(r"\s+-\s+|\s*:\s+", t) if p.strip()]
    kw = re.compile(r"Future Land Use|Zoning|Map Amendment|Annexation|Planned Development|Special Exception|"
                    r"Preliminary Subdivision|Request|Modification|Amendment", re.I)
    cand = [p for p in parts if not kw.search(p)]
    if cand:
        name = cand[0]
    else:
        name = re.split(r"\s+(?:Future Land Use|Zoning Map|Annexation|Request|Amendment|Major|Small|Large)", parts[0], flags=re.I)[0] \
            if parts else t
    name = re.sub(r"^\d+\s+\S.*?\s+-\s+", "", name).strip(" -,")
    return sq(name)


def pkey(p):
    p = (p or "").lower()
    p = re.sub(r"\bind\b", "industrial", p)
    p = re.sub(r"\b(the|property|project|mixed use|lot|subdivision)\b", " ", p)
    return re.sub(r"[^a-z0-9]+", " ", p).strip()


def ords_for(item, kind):
    """Ordinance numbers of one kind in an item's title / attachment names."""
    found = []
    m = ORD_RE.search(item["title"])
    if m:
        found.append((f"{m.group(1)}-{m.group(2)}", item["title"]))
    for lab, _ in item["docs"]:
        mm = ORD_RE.search(lab)
        if mm:
            found.append((f"{mm.group(1)}-{mm.group(2)}", lab))
    out = []
    for o, lab in found:
        is_flu = bool(re.search(r"FLUM|Future Land Use|Land Use Map|Comprehensive", lab, re.I))
        is_z = bool(re.search(r"Zoning", lab, re.I))
        if kind == "CPA" and is_flu or kind == "ZONING" and is_z and not is_flu or \
                (kind != "PD" and not is_flu and not is_z and lab == item["title"]):
            if o not in out:
                out.append(o)
    return out


def reading_of(title):
    if re.search(r"2nd\s+Read|Second\s+Read", title, re.I):
        return "second"
    if re.search(r"1st\s+Read|First\s+Read", title, re.I):
        return "first"
    return None


# ------------------------------------------------------------------ minutes
def minutes_outcome(text, ono=None, project=None):
    t = sq(text) or ""
    t = re.sub(r"(20\d{2})\s*-\s*(\d{1,3})\b", r"\1-\2", t)
    if ono:
        pat = rf"motion to (adopt|approve|deny|table|continue|postpone)[^.]{{0,80}}?ORDINANCE\s*{re.escape(ono)}\b"
    elif project:
        words = [w for w in re.findall(r"[A-Za-z0-9]+", project) if len(w) > 2][:3]
        if not words:
            return None
        pat = r"motion to (approve|deny|table|continue|postpone)[^.]{0,200}?" + r"\W+".join(map(re.escape, words))
    else:
        return None
    m = re.search(pat, t, re.I)
    if not m:
        return None
    rest = t[m.end(): m.end() + 1500]
    v = re.search(r"Motion (passed|carried|approved|adopted|failed|denied)", rest, re.I)
    if not v:
        return None
    act = m.group(1).lower()
    if v.group(1).lower() in ("failed", "denied"):
        return "failed"
    if act in ("table", "continue", "postpone"):
        return "continued"
    if act == "deny":
        return "denied"
    return "carried"


# ------------------------------------------------------------------ fetch
def fetch(since, refresh):
    if pypdf is None:
        raise RuntimeError("pypdf is required for the Lake Wales adapter")
    today = dt.date.today()
    refresh_ids = {r.strip(): r for r in (refresh or []) if r and r.strip()}
    start = (dt.date.fromisoformat(since) - dt.timedelta(days=LOOKBACK_DAYS)).isoformat()
    rows = []
    for y in range(int(start[:4]), today.year + 1):
        for cat in CATS:
            try:
                rows += list_rows(y, cat)
            except Exception as e:  # noqa: BLE001
                log(f"list {y}/{cat}: {e}")
    rows = sorted([r for r in rows if r["date"] >= start], key=lambda r: (r["date"], r["body"] != PZ))
    if not rows:
        raise RuntimeError("no Lake Wales agendas found")
    log(f"{len(rows)} P&Z / City Commission agendas since {start}")

    apps = []
    for r in rows:
        try:
            items = agenda_items(r["url"])
        except Exception as e:  # noqa: BLE001
            log(f"agenda {r['date']}: {e}")
            continue
        for it in items:
            for kind, label in kinds_of(it["title"]):
                apps.append({"date": r["date"], "body": r["body"], "kind": kind, "label": label, "title": it["title"],
                             "desc": it["desc"], "docs": it["docs"], "ords": ords_for(it, kind),
                             "proj": project_of(it["title"]), "reading": reading_of(it["title"]),
                             "url": r["url"].replace("?html=true", ""), "minutes": r["minutes"]})

    cases, by_ord = [], {}
    for a in apps:
        a["pk"] = pkey(a["proj"])
        case = next((by_ord[o] for o in a["ords"] if o in by_ord and by_ord[o]["kind"] == a["kind"]), None)
        if case is None and a["pk"]:
            cands = [c for c in cases if c["kind"] == a["kind"] and (a["pk"] in c["pks"] or
                     any(a["pk"] and (a["pk"].startswith(p) or p.startswith(a["pk"])) and len(min(a["pk"], p, key=len)) >= 5
                         for p in c["pks"]))
                     and (not a["ords"] or not c["ords"] or set(a["ords"]) & c["ords"])]
            case = cands[-1] if cands else None
        if case is None:
            key = f"Ordinance {a['ords'][0]}" if a["ords"] else f"{a['proj']} ({a['label']})"
            case = {"key": key, "kind": a["kind"], "apps": [], "pks": set(), "ords": set()}
            cases.append(case)
        case["apps"].append(a)
        if a["pk"]:
            case["pks"].add(a["pk"])
        for o in a["ords"]:
            case["ords"].add(o)
            by_ord.setdefault(o, case)
    log(f"{len(apps)} in-scope item requests -> {len(cases)} cases")

    minutes_cache = {}
    out, keys = [], set()
    for c in cases:
        first = min(a["date"] for a in c["apps"])
        ids = {c["key"]} | {f"Ordinance {o}" for o in c["ords"]}
        if (first < since and not ids & set(refresh_ids)) or c["key"] in keys:
            continue
        keys.add(c["key"])
        try:
            out.append(build(c, minutes_cache, today.isoformat()))
        except Exception as e:  # noqa: BLE001
            log(f"{c['key']}: skipped ({e})")
    for r in set(refresh_ids) - set().union(*[{c["key"]} | {f"Ordinance {o}" for o in c["ords"]} for c in cases] or [set()]):
        log(f"refresh id {r} not found on agendas since {start}")
    return out


def parse_report(t):
    d = sq(t) or ""
    out = {}
    pids = re.findall(r"\b\d{2}-\d{2}-\d{2}-\d{6}-\d{6}\b", d)
    for m in re.finditer(r"P(?:ID|arcel IDs?)s?\s*:?\s*((?:Portion of\s+)?(?:\d{18}|\d{2}-\d{2}-\d{2}-\d{6}-\d{6})"
                         r"(?:[,;\s]+(?:and\s+)?(?:Portion of\s+)?(?:\d{18}|\d{2}-\d{2}-\d{2}-\d{6}-\d{6}))*)", d):
        pids += re.findall(r"\d{18}|\d{2}-\d{2}-\d{2}-\d{6}-\d{6}", m.group(1))
    out["parcels"] = list(dict.fromkeys(pids))
    acres = [float(x.replace(",", "")) for x in re.findall(r"([\d,]*\.?\d+)\s*\+?\s*/?\s*-?\s*acres", d, re.I)
             if re.match(r"^[\d,]*\.?\d+$", x) and x.strip(",.")]
    acres = [a for a in acres if 0 < a < 100000]
    if acres:
        out["acres"] = max(acres)
    out["head"] = d[:2500]
    m = re.search(r"SYNOPSIS:\s*(.+?)(?:RECOMMENDED MOTION|RECOMMENDATION|$)", d)
    if m:
        out["synopsis"] = m.group(1).strip()[:600]
        a = re.match(r"(.+?),?\s+(?:is requesting|requests|request)\b", m.group(1))
        if a:
            out["applicant"] = a.group(1).strip(" ,")[:200]
    m = re.search(r"Current Future Land Use(?: Designation)?:\s*(.+?)\s+Current Zoning", d)
    if m:
        out["cur_flu"] = m.group(1).strip()
    m = re.search(r"Current Zoning(?: Designation)?:\s*(.+?)\s+(?:The |Location|Page|Proposed|Current)", d)
    if m:
        out["cur_zoning"] = m.group(1).strip()
    m = re.search(r"Proposed Land Use:\s*([A-Z][A-Z0-9-]{0,4}(?: (?!Proposed|These|This|Parcel)[A-Z][a-z]+){1,4})", d)
    if m:
        out["new_flu"] = m.group(1).strip()
    m = re.search(r"Proposed Zoning:\s*([A-Z][A-Z0-9-]{0,4}(?: (?!These|This|Parcel)[A-Z][a-z]+){1,4})", d)
    if m:
        out["new_zoning"] = m.group(1).strip()
    m = re.search(r"from ([A-Z0-9-]{1,5} [A-Z][\w /-]+?) to ([A-Z0-9-]{1,5} [A-Z][\w /-]+?)(?:[,.]| and )", d)
    if m:
        out["from_to"] = (m.group(1).strip(), m.group(2).strip())
    return out


def build(c, minutes_cache, today):
    apps = sorted(c["apps"], key=lambda a: (a["date"], a["body"] != PZ))
    kind = c["kind"]
    ords = sorted(c["ords"])
    # staff report / memo of the latest appearance that has one
    rep, rep_url = {}, None
    for a in reversed(apps):
        cand = [u for lab, u in a["docs"] if re.search(r"Staff Report|MEMO|Memo", lab)]
        if cand:
            try:
                rep = parse_report(pdf_text(cand[0], max_pages=4))
                rep_url = cand[0]
            except Exception as e:  # noqa: BLE001
                log(f"{c['key']}: report unreadable ({e})")
            break

    hearings, cc_n = [], 0
    for a in apps:
        if a["body"] == PZ:
            h = {"body": PZ, "date": a["date"], "result": None}
        else:
            reading = a["reading"]
            if kind == "PD" and not ords:
                reading = None
            elif not reading:
                reading = "second" if cc_n else "first"
            cc_n += 1
            res = None
            if a["minutes"] and a["date"] <= today:
                if a["minutes"] not in minutes_cache:
                    try:
                        minutes_cache[a["minutes"]] = pdf_text(a["minutes"])
                    except Exception as e:  # noqa: BLE001
                        log(f"minutes {a['date']}: {e}")
                        minutes_cache[a["minutes"]] = ""
                mt = minutes_cache[a["minutes"]]
                word = None
                for o in ords:
                    word = word or minutes_outcome(mt, ono=o)
                if not word and kind == "PD":
                    word = minutes_outcome(mt, project=a["proj"])
                if word:
                    res = {"carried": "Approved" if reading in ("second", None) else "Approved (first reading)",
                           "failed": "Denied", "denied": "Denied", "continued": "Continued"}.get(word)
            body = f"{CC} ({reading} reading)" if reading else CC
            h = {"body": body, "date": a["date"], "result": res}
        if not any(x["body"] == h["body"] and x["date"] == h["date"] for x in hearings):
            hearings.append(h)

    pzt = " ".join(filter(None, [a["desc"] for a in apps] + [rep.get("head")]))
    for m in re.finditer(r"(?:meeting on|On) ([A-Z][a-z]+ \d{1,2}, \d{4}),? the Planning (?:and|&) Zoning Board "
                         r"(?:unanimously )?(?:recommended|voted to recommend) (approval|denial)", pzt):
        try:
            pd_ = dt.datetime.strptime(m.group(1), "%B %d, %Y").date().isoformat()
        except ValueError:
            continue
        for h in hearings:
            if h["body"] == PZ and h["date"] == pd_ and h["result"] is None:
                h["result"] = "Recommended Approval" if m.group(2) == "approval" else "Recommended Denial"

    status, raw, decision = None, None, None
    past = [h for h in hearings if h["date"] <= today]
    future = [h for h in hearings if h["date"] > today]
    final = [h for h in hearings if h["body"].startswith(CC) and "first" not in h["body"]
             and h["result"] in ("Approved", "Denied")]
    if final:
        status, decision = final[-1]["result"], final[-1]["date"]
        raw = f"{final[-1]['body']} {decision}: {final[-1]['result']} (minutes)"
    elif future:
        status, raw = "Hearing Scheduled", f"{future[0]['body']} {future[0]['date']}"
    elif past and past[-1]["result"] == "Continued":
        status, raw = "Continued", f"{past[-1]['body']} {past[-1]['date']}: continued"
    elif past:
        lp = past[-1]
        if lp["body"].startswith(CC) and lp["result"] is None:
            status, raw = "Hearing Scheduled", f"{lp['body']} {lp['date']} held; minutes not yet posted"
        else:
            status = "Filed"
            raw = f"{lp['body']} {lp['date']}" + (f": {lp['result']}" if lp["result"] else "") + \
                "; next hearing not yet on an agenda"
    else:
        status, raw = "Filed", "On agenda"

    text = " ".join(filter(None, [a["title"] for a in apps] + [a["desc"] for a in apps] + [rep.get("head")]))
    text = re.sub(r"(\d)\s*\+\s*/?\s*-\s*", r"\1+/- ", text)
    acres = None
    pat = {"CPA": r"([\d,]*\.?\d+)\+/-\s*acres of land a Future Land Use",
           "ZONING": r"([\d,]*\.?\d+)\+/-\s*acres of land a Zoning"}.get(kind)
    m = re.search(pat, text) if pat else None
    if m:
        try:
            acres = float(m.group(1).replace(",", ""))
        except ValueError:
            acres = None
    acres = acres or rep.get("acres")
    if acres is None:
        m = re.search(r"([\d,]*\.?\d+)\s*\+?\s*/?\s*-?\s*acres", text, re.I)
        try:
            acres = float(m.group(1).replace(",", "")) if m else None
        except ValueError:
            acres = None
    frm = to = None
    desig = r"([A-Z][A-Z0-9-]{0,4} (?!Parcel|Public|Page|These|This)[A-Z][a-z]+(?: (?!Parcel|Public|Page|These|This)[A-Z][a-z]+){0,3})"
    if kind == "CPA":
        frm, to = rep.get("cur_flu"), rep.get("new_flu")
        m = re.search(r"Future Land Use designation of " + desig, text)
        to = to or (m.group(1) if m else None)
        m = re.search(r"(?:future )?land use on (?:[^.]|\.\d){0,60}?from " + desig + r" to " + desig, text)
        if m:
            frm, to = frm or m.group(1), to or m.group(2)
    elif kind == "ZONING":
        frm, to = rep.get("cur_zoning"), rep.get("new_zoning")
        m = re.search(r"Zoning designation of " + desig, text)
        to = to or (m.group(1) if m else None)
        m = re.search(r"zoning on (?:[^.]|\.\d){0,60}?from ([A-Z][A-Z0-9-]{0,4} [A-Za-z]+) to " + desig, text)
        if m:
            frm, to = frm or m.group(1), to or m.group(2)
    if rep.get("from_to") and not frm:
        frm = rep["from_to"][0]
        to = to or rep["from_to"][1]
    applicant = rep.get("applicant")
    if not applicant:
        m = re.search(r"([A-Z][^.]{3,120}?),?\s+(?:authorized agent|owner)[^.]{0,80}?,\s+(?:is requesting|requests)", text)
        applicant = m.group(1) if m else None
    category = {"CPA": "Comp Plan Amendment", "ZONING": "Rezoning", "PD": "PD"}[kind]
    if kind == "ZONING" and re.search(r"\bPDP?\b|Planned Development", to or "", re.I):
        category = "PD"
    desc = apps[0]["title"]
    d2 = next((a["desc"] for a in apps if a["desc"]), None) or rep.get("synopsis")
    if d2:
        desc = f"{desc}. {d2}"
    if ords and not c["key"].startswith("Ordinance"):
        desc += f" (Ordinance {', '.join(ords)})"
    if len(desc) > 500:
        desc = desc[:497] + "..."
    addr = None
    m = re.match(r"^(\d+\s+[A-Z][^-]+?)\s+-\s+", apps[0]["title"])
    if m:
        addr = m.group(1)
    else:
        m = re.search(r"located (?:at|on)\s+(.+?)(?:\.|,| and adjacent| in )", text)
        addr = m.group(1) if m else None
    latest = apps[-1]
    return {
        "jurisdiction": JUR, "county": "Polk", "app_num": c["key"], "category": category,
        "app_type": apps[0]["label"], "project_name": apps[0]["proj"], "description": sq(desc),
        "applicant": applicant, "parcel_ids": rep.get("parcels") or [], "address": addr, "acres": acres,
        "from_zoning": frm if kind != "CPA" else None, "to_zoning": to if kind != "CPA" else None,
        "from_flu": frm if kind == "CPA" else None, "to_flu": to if kind == "CPA" else None,
        "filed_date": apps[0]["date"], "status": status, "status_raw": raw, "hearings": hearings,
        "decision_date": decision, "url": latest["url"], "docs_url": rep_url, "lat": None, "lon": None,
        "source": SOURCE,
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
