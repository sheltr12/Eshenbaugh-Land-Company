#!/usr/bin/env python3
"""City of Bartow (Polk County) rezoning / PD / future land use map amendment adapter.

Source: the city's CivicPlus AgendaCenter (https://www.cityofbartow.net/AgendaCenter), category 2
"Planning and Zoning Commission" and category 5 "City Commission". Each row is one PDF: P&Z agendas
are short; City Commission rows are either a short agenda or a full packet (up to ~400 pages /
20 MB; the server ignores HTTP Range, so packets are downloaded whole).
 * P&Z agendas list applications: "Application #CPA-26-03-LS Review and recommendation to the City
   Commission of a request to assign an Industrial Future Land Use Map designation to 111.4 acres
   (+/-) ... owned by Ruthven 17, LLC ...". Prefixes: CPA (comp plan map amendment), Z (zoning),
   PD / PUD; A (annexation), ULDC (code text), V, SE, SP are skipped.
 * City Commission agendas list ordinances ("Public hearing and first reading of Ordinance No.
   2026-09 - AN ORDINANCE AMENDING THE COMPREHENSIVE PLAN FUTURE LAND USE MAP ...").
 * Packets add the staff report ("Application #CPA-26-03-LS ... City Commission Dates: Ordinance
   2026-09 October 19, 2026 (First Reading ...)", owner / applicant, location, property size, existing
   and proposed zoning) and the ordinance exhibits with Polk parcel numbers
   ("25-29-14-000000-041050"). Packets also carry the minutes of the previous meeting
   ("MOTION was made by ... to pass on first reading Ordinance No. 2026-09 ... Motion carried."),
   which give the outcomes.
app_num = the application number ("CPA-26-03-LS", "Z-26-01-ALUP") when known, else the ordinance
number ("Ordinance No. 2026-09"); both are in the description. Requires pypdf.
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

JURISDICTIONS = ["City of Bartow"]
JUR = "City of Bartow"
BASE = "https://www.cityofbartow.net"
SOURCE = "Bartow AgendaCenter (P&Z + City Commission agendas, packets, staff reports, minutes)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
LOOKBACK_DAYS = 150
CATS = {2: "Planning and Zoning Commission", 5: "City Commission"}
PZ = "Planning and Zoning Commission"
CC = "City Commission"
SKIP_TITLE = re.compile(r"Workshop|Attorney-Client|Canvass|Community Meeting|Budget|Students|Funding|Resolution No", re.I)

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


def log(*a):
    print("[bartow]", *a, file=sys.stderr)


def req(method, url, tries=6, sleep=0.6, **kw):
    last = None
    for i in range(tries):
        try:
            r = _s.request(method, url, timeout=240, **kw)
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
    return (s or "").replace("\u2013", "-").replace("\u2014", "-").replace("\u2019", "'").replace("\u201c", '"') \
        .replace("\u201d", '"')


# ------------------------------------------------------------------ AgendaCenter
def list_rows(year, cat):
    t = req("POST", f"{BASE}/AgendaCenter/UpdateCategoryList",
            data={"year": year, "catID": cat, "startDate": "", "endDate": "", "term": "",
                  "prevVersionScreen": "false"}).text
    out = []
    for row in re.findall(r'<tr[^>]*class="catAgendaRow"[^>]*>(.*?)</tr>', t, re.S):
        a = re.search(r'href="(/AgendaCenter/ViewFile/Agenda/_(\d{2})(\d{2})(\d{4})-(\d+))"[^>]*>\s*(.*?)\s*</a>', row, re.S)
        if not a:
            continue
        title = sq(re.sub(r"<[^>]+>", " ", a.group(6))) or ""
        if SKIP_TITLE.search(title):
            continue
        mn = re.search(r'href="(/AgendaCenter/ViewFile/Minutes/[^"]+)"', row)
        out.append({"date": f"{a.group(4)}-{a.group(2)}-{a.group(3)}", "title": title, "url": BASE + a.group(1),
                    "minutes": BASE + mn.group(1) if mn else None, "body": CATS[cat], "id": int(a.group(5))})
    return out


def pdf_pages(url):
    rdr = pypdf.PdfReader(io.BytesIO(req("GET", url).content))
    out = []
    for p in rdr.pages:
        try:
            out.append(nd(p.extract_text() or ""))
        except Exception:  # noqa: BLE001
            out.append("")
    return out


# ------------------------------------------------------------------ parsing
APP_RE = re.compile(r"Application\s*#\s*([A-Z]{1,5})\s*-\s*(\d{2})\s*-\s*(\d{2})((?:\s*-\s*[A-Z]{1,6})?)")
ORD_RE = re.compile(r"Ordinance\s+No\.?\s*(20\d{2})\s*-\s*(\d{2,3})", re.I)
IN_PREFIX = {"CPA", "Z", "PD", "PUD", "RZ", "ZM"}
MAP_RE = re.compile(r"Future Land Use Map|FUTURE LAND USE MAP|zoning classification|ZONING MAP|REZONING|rezone|"
                    r"PLANNED (?:UNIT )?DEVELOPMENT|Planned (?:Unit )?Development|\bPUD\b", re.I)
TEXT_RE = re.compile(r"UNIFIED LAND DEVELOPMENT CODE|Land Development Code|TEXT AMENDMENT|Articles? of the", re.I)


def app_id(m):
    suf = re.sub(r"\s", "", m.group(4) or "")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}{suf}"


def pz_items(text):
    """[(app_no, text)] in-scope applications on a P&Z agenda."""
    t = text
    ms = list(APP_RE.finditer(t))
    out = []
    for i, m in enumerate(ms):
        seg = t[m.end(): ms[i + 1].start() if i + 1 < len(ms) else len(t)]
        seg = re.split(r"\n\s*\d{1,2}\.\s|NEXT MEETINGS?|ADJOURNMENT", seg)[0]
        if m.group(1) not in IN_PREFIX:
            continue
        seg = sq(seg) or ""
        if m.group(1) == "CPA" and not re.search(r"Future Land Use Map|map designation|FLUM", seg, re.I):
            continue
        out.append((app_id(m), seg))
    return out


def cc_items(text):
    """[(ord_no, text, reading)] in-scope ordinances on a City Commission agenda."""
    t = text
    out = []
    for m in ORD_RE.finditer(t):
        line_start = max(t.rfind("\n", 0, m.start() - 60), 0)
        pre = t[max(line_start, m.start() - 160): m.start()]
        pre = re.split(r"\n\s*[a-z]\.\s", "\n" + pre)[-1]
        seg = t[m.end(): m.end() + 1500]
        seg = re.split(r"\n\s*[a-z]\.\s|\n\s*\d{1,2}\.\s", seg)[0]
        full = sq(pre + " Ordinance No. " + m.group(1) + "-" + m.group(2) + seg) or ""
        if not re.search(r"reading|public hearing|consideration", pre, re.I):
            continue
        body = seg.upper()
        if not MAP_RE.search(body) or TEXT_RE.search(body) or re.search(r"ANNEXING", body):
            continue
        reading = None
        if re.search(r"final reading|second reading|adoption", pre, re.I):
            reading = "second"
        elif re.search(r"first reading|transmittal", pre, re.I):
            reading = "first"
        out.append((f"{m.group(1)}-{m.group(2)}", full, reading))
    seen, res = set(), []
    for o in out:
        if o[0] not in seen:
            seen.add(o[0])
            res.append(o)
    return res


def agenda_text(pages):
    out = []
    for i, t in enumerate(pages[:10]):
        out.append(t)
        if re.search(r"\bADJOURNMENT\b", t):
            break
    return "\n".join(out)


def staff_reports(pages):
    """{app_no: {...}} from staff report pages in a packet."""
    out = {}
    for i, t in enumerate(pages):
        if "Staff Report" not in t[:1500] or not APP_RE.search(t[:600]):
            continue
        m = APP_RE.search(t[:600])
        aid = app_id(m)
        if aid in out:
            continue
        body = "\n".join(pages[i: i + 12])
        nxt = re.search(r"\n\s*ITEM NO\.\s*(?!" + re.escape(re.search(r"ITEM NO\.\s*([\d.a-z]+)", t).group(1)
                                                             if re.search(r"ITEM NO\.\s*([\d.a-z]+)", t) else "x") + r")",
                        body[200:])
        if nxt:
            body = body[: 200 + nxt.start()]
        d = sq(body) or ""
        rec = {"text": d[:3000], "page": i}
        om = re.search(r"City Commission Dates?:\s*Ordinance\s*(20\d{2})\s*-\s*(\d{2,3})", d)
        if om:
            rec["ord"] = f"{om.group(1)}-{om.group(2)}"
        mm = re.search(r"Owner/Applicant\s*(.+?)\s+Location:", d)
        if mm:
            rec["applicant"] = mm.group(1).strip(" :")
        mm = re.search(r"Location:\s*(.+?)\s*(?:\(See Location Map\)|Property Size:)", d)
        if mm:
            rec["location"] = mm.group(1).strip(" .")
        mm = re.search(r"Property Size:\s*([\d,]*\.?\d+)\s*acres", d, re.I)
        if mm:
            rec["acres"] = float(mm.group(1).replace(",", ""))
        mm = re.search(r"Existing Zoning:\s*(.+?)\.?\s+(?:Adjacent|Proposed)", d)
        if mm:
            rec["cur_zoning"] = mm.group(1).strip()
        mm = re.search(r"Proposed Zoning:\s*(?:If the requested changes are approved[^.]*?will be\s*)?(.+?)(?:\s+respectively)?[.(]", d)
        if mm:
            rec["new_zoning"] = mm.group(1).strip()
        mm = re.search(r"Existing Future Land Use:\s*(.+?)\.?\s+(?:Adjacent|Proposed)", d)
        if mm:
            rec["cur_flu"] = mm.group(1).strip()
        pids = re.findall(r"\b\d{2}-\d{2}-\d{2}-\d{6}-\d{6}\b", "\n".join(pages[i: i + 40]))
        rec["parcels"] = list(dict.fromkeys(pids))
        out[aid] = rec
    return out


def ordinance_apps(pages):
    """{ord_no: app_no} from ordinance texts ('AMENDMENT NO. CPA-26-03-LS') and staff report headers."""
    out = {}
    for t in pages:
        d = sq(t) or ""
        m = re.search(r"ORDINANCE NO\.?\s*(20\d{2})\s*-\s*(\d{2,3})", d[:200])
        if m:
            a = re.search(r"AMENDMENT (?:NO\.|#)\s*([A-Z]{1,5}\s*-\s*\d{2}\s*-\s*\d{2}(?:\s*-\s*[A-Z]{1,6})?)", d)
            if a:
                out[f"{m.group(1)}-{m.group(2)}"] = re.sub(r"\s", "", a.group(1))
    return out


def minutes_outcomes(pages):
    """{(meeting_date, ord_no): (word, first_reading)} from City Commission minutes inside a packet."""
    out = {}
    by_date = {}
    for t in pages:
        d = sq(t) or ""
        h = re.search(r"City Commission (?:Regular|Special) Meeting\s+([A-Z][a-z]+ \d{1,2}, \d{4})\s*@", d[:300]) or \
            re.search(r"met at a (?:Regular|Special) Meeting on ([A-Z][a-z]+ \d{1,2}, \d{4})", d[:1200])
        if not h:
            continue
        try:
            cur = dt.datetime.strptime(h.group(1), "%B %d, %Y").date().isoformat()
        except ValueError:
            continue
        by_date.setdefault(cur, []).append(d)
    for cur, texts in by_date.items():
        d = " ".join(texts)
        for m in re.finditer(r"MOTION was made by[^.]*?to (pass|approve|adopt|deny|table|continue|postpone|withdraw)"
                             r"[^.]*?Ordinance No\.?\s*(20\d{2})\s*-\s*(\d{2,3})", d, re.I):
            rest = d[m.end(): m.end() + 2500]
            rest = re.split(r"MOTION was made", rest, flags=re.I)[0]
            v = re.search(r"Motion (carried|failed|passed)", rest, re.I)
            act = m.group(1).lower()
            if not v:
                continue
            word = "failed" if v.group(1).lower() == "failed" else \
                ("continued" if act in ("table", "continue", "postpone") else
                 "denied" if act == "deny" else "withdrawn" if act == "withdraw" else "carried")
            first = bool(re.search(r"first reading", d[m.start(): m.end() + 200], re.I))
            out[(cur, f"{m.group(2)}-{m.group(3)}")] = (word, first)
    return out


def pz_recommendations(pages):
    """{ord_no: 'Recommended Approval' | 'Recommended Denial'} from ordinance WHEREAS clauses."""
    out = {}
    for t in pages:
        d = sq(t) or ""
        m = re.search(r"ORDINANCE NO\.?\s*(20\d{2})\s*-\s*(\d{2,3})", d[:200])
        if not m:
            continue
        r = re.search(r"Planning and Zoning Commission[^.]{0,450}?recommend(?:s|ed)?\s+(?:the\s+)?(approval|denial|the zoning|"
                      r"approval of|that)", d, re.I)
        if r:
            out[f"{m.group(1)}-{m.group(2)}"] = "Recommended Denial" if r.group(1).lower() == "denial" else "Recommended Approval"
    return out


# ------------------------------------------------------------------ fetch
def fetch(since, refresh):
    if pypdf is None:
        raise RuntimeError("pypdf is required for the Bartow adapter")
    today = dt.date.today()
    refresh_ids = {re.sub(r"^Ordinance No\.\s*", "", r.strip()): r for r in (refresh or []) if r and r.strip()}
    start = (dt.date.fromisoformat(since) - dt.timedelta(days=LOOKBACK_DAYS)).isoformat()
    if refresh_ids:
        start = min(start, (today - dt.timedelta(days=540)).isoformat())
    rows = []
    for y in range(int(start[:4]), today.year + 1):
        for cat in CATS:
            try:
                rows += list_rows(y, cat)
            except Exception as e:  # noqa: BLE001
                log(f"list {y}/{cat}: {e}")
    rows = [r for r in rows if r["date"] >= start]
    if not rows:
        raise RuntimeError("no Bartow agendas found")
    log(f"{len(rows)} P&Z / City Commission documents since {start}")

    apps = []          # appearances
    reports, ord2app, outcomes, pzrec = {}, {}, {}, {}
    for r in sorted(rows, key=lambda r: (r["date"], r["id"])):
        try:
            pages = pdf_pages(r["url"])
        except Exception as e:  # noqa: BLE001
            log(f"{r['date']} {r['title']}: {e}")
            continue
        ag = agenda_text(pages)
        if r["body"] == PZ:
            for aid, txt in pz_items(ag):
                apps.append({"date": r["date"], "body": PZ, "app": aid, "ord": None, "text": txt, "reading": None,
                             "url": r["url"]})
        else:
            for ono, txt, reading in cc_items(ag):
                apps.append({"date": r["date"], "body": CC, "app": None, "ord": ono, "text": txt, "reading": reading,
                             "url": r["url"]})
        if len(pages) > 10:
            for aid, rep in staff_reports(pages).items():
                rep["url"] = r["url"]
                reports[aid] = rep
                if rep.get("ord"):
                    ord2app.setdefault(rep["ord"], aid)
            for k, v in ordinance_apps(pages).items():
                ord2app.setdefault(k, v)
            outcomes.update(minutes_outcomes(pages))
            pzrec.update(pz_recommendations(pages))
    log(f"{len(apps)} in-scope appearances; {len(reports)} staff reports; {len(outcomes)} minute outcomes")

    cases = {}
    for a in sorted(apps, key=lambda a: a["date"]):
        aid = a["app"] or ord2app.get(a["ord"])
        key = aid or f"Ordinance No. {a['ord']}"
        c = cases.setdefault(key, {"apps": [], "ords": set()})
        c["apps"].append(a)
        if a["ord"]:
            c["ords"].add(a["ord"])
    for aid, c in cases.items():
        for o, ap in ord2app.items():
            if ap == aid:
                c["ords"].add(o)

    out = []
    for key, c in sorted(cases.items()):
        first = min(a["date"] for a in c["apps"])
        ids = {key} | {f"Ordinance No. {o}" for o in c["ords"]} | c["ords"]
        if first < since and not (ids & set(refresh_ids)):
            continue
        try:
            out.append(build(key, c, reports, outcomes, pzrec, today.isoformat()))
        except Exception as e:  # noqa: BLE001
            log(f"{key}: skipped ({e})")
    return out


def build(key, c, reports, outcomes, pzrec, today):
    apps = sorted(c["apps"], key=lambda a: a["date"])
    rep = reports.get(key) or {}
    hearings = []
    cc_n = 0
    for a in apps:
        if a["body"] == PZ:
            h = {"body": PZ, "date": a["date"], "result": None}
        else:
            reading = a["reading"] or ("second" if cc_n else "first")
            cc_n += 1
            o = outcomes.get((a["date"], a["ord"]))
            res = None
            if o:
                word, _ = o
                res = {"carried": "Approved" if reading == "second" else "Approved (first reading)",
                       "failed": "Denied", "denied": "Denied", "continued": "Continued",
                       "withdrawn": "Withdrawn"}.get(word)
            h = {"body": f"{CC} ({reading} reading)", "date": a["date"], "result": res}
        if not any(x["body"] == h["body"] and x["date"] == h["date"] for x in hearings):
            hearings.append(h)
    # P&Z recommendation: implied by the ordinance text "unanimously recommended approval"
    rec = next((pzrec[o] for o in sorted(c["ords"]) if o in pzrec), None)
    if rec:
        for h in hearings:
            if h["body"] == PZ and h["result"] is None:
                h["result"] = rec

    status, raw, decision = None, None, None
    past = [h for h in hearings if h["date"] <= today]
    future = [h for h in hearings if h["date"] > today]
    final = [h for h in hearings if h["body"].startswith(CC) and h["result"] in ("Approved", "Denied")]
    if any(h["result"] == "Withdrawn" for h in hearings):
        status, raw = "Withdrawn", "Withdrawn (minutes)"
    elif final:
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

    pz_text = next((a["text"] for a in apps if a["body"] == PZ), None)
    cc_text = next((a["text"] for a in apps if a["body"] == CC), None)
    text = pz_text or cc_text or ""
    prefix = key.split("-")[0] if not key.startswith("Ordinance") else None
    alltext = " ".join(a["text"] for a in apps)
    if prefix == "CPA" or (not prefix and re.search(r"FUTURE LAND USE MAP", alltext, re.I)):
        kind, category = "CPA", "Comp Plan Amendment"
        app_type = "Large Scale Comprehensive Plan (FLUM) Amendment" if re.search(r"-LS\b", key) or \
            re.search(r"Large[- ]Scale", alltext, re.I) else "Comprehensive Plan (FLUM) Amendment"
    else:
        kind = "ZONING"
        is_pd = bool(re.search(r"PLANNED (?:UNIT )?DEVELOPMENT|\bPUD\b|\bPD\b", alltext + " " + (rep.get("new_zoning") or ""), re.I))
        category = "PD" if is_pd else "Rezoning"
        app_type = "Rezoning (Planned Development)" if is_pd else "Rezoning / Zoning Assignment"
    acres = rep.get("acres")
    if acres is None:
        m = re.search(r"([\d,]*\.?\d+)\s*acres", alltext, re.I)
        acres = float(m.group(1).replace(",", "")) if m else None
    applicant = rep.get("applicant")
    if not applicant:
        m = re.search(r"(?:currently )?owned by\s+(.+?)(?:,\s*and is located|\s+and is located|\.|;)", text, re.I)
        applicant = m.group(1).strip(" ,") if m else None
    addr = rep.get("location")
    if not addr:
        m = re.search(r"located on (?:the )?(.+?)(?: in Section|\.|\(Review)", text, re.I)
        addr = m.group(1).strip(" ,;") if m else None
    if addr:
        addr = re.split(r"\s*\(and surrounding|\s*\(See", addr)[0].strip(" ,;")
    if addr and len(addr) > 200:
        addr = addr[:197] + "..."
    to_flu = None
    m = re.search(r"assign (?:an? )?(.+?) Future Land Use Map designation", alltext, re.I)
    if m:
        to_flu = m.group(1).strip()
    to_z = rep.get("new_zoning")
    if not to_z:
        m = re.search(r"assign (.+?) zoning classifications?", alltext, re.I)
        to_z = m.group(1).strip() if m else None
    desc = re.sub(r"\(Review[^)]*\)", "", text).strip()
    ords = sorted(c["ords"])
    if ords and not key.startswith("Ordinance"):
        desc += f" (Ordinance No. {', '.join(ords)})"
    if desc and len(desc) > 500:
        desc = desc[:497] + "..."
    latest = apps[-1]
    return {
        "jurisdiction": JUR, "county": "Polk", "app_num": key, "category": category, "app_type": app_type,
        "project_name": None, "description": sq(desc), "applicant": applicant, "parcel_ids": rep.get("parcels") or [],
        "address": addr, "acres": acres,
        "from_zoning": rep.get("cur_zoning") if kind == "ZONING" else None, "to_zoning": to_z if kind == "ZONING" else None,
        "from_flu": rep.get("cur_flu") if kind == "CPA" else None, "to_flu": to_flu if kind == "CPA" else None,
        "filed_date": apps[0]["date"], "status": status, "status_raw": raw, "hearings": hearings,
        "decision_date": decision, "url": latest["url"], "docs_url": rep.get("url"), "lat": None, "lon": None,
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
