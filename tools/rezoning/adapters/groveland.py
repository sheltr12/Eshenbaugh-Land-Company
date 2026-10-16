#!/usr/bin/env python3
"""City of Groveland (Lake County) rezoning / PUD / future land use map amendment adapter.

Source: the city's CivicPlus AgendaCenter (www.groveland-fl.gov/AgendaCenter), category 10
"Planning and Zoning (P&Z)" and category 2 "City Council". Each agenda is a single packet PDF
(agenda pages + staff memos + exhibits, up to ~30 MB; the server ignores HTTP Range, so packets are
downloaded whole). Minutes are separate PDFs on the same row.
 * Land use cases are ordinances: P&Z "Recommendation of approval on Ordinance 2026-13: Sampey Road
   Parcel 2 Small Scale Comprehensive Plan Amendment", then City Council first and second reading.
   app_num = "Ordinance YYYY-NN" (the city's application number, e.g. PLAN2606-0044, is only printed
   inside the staff memo, so it goes into the description).
 * The staff memo for the ordinance ("SUBJECT: ... ORDINANCE 2026-13") gives the application
   number, applicant, acreage, Lake County Alternate Key(s) and the requested from/to designations.
 * Outcomes: P&Z and City Council minutes ("motioned to approve Ordinance 2026-14 ...; Motion passed").
   Council minutes are OCR scans, so some outcomes may be missed.
The city's eTRAKiT portal (gvld-trk.aspgov.com) has project records but its search requires reCAPTCHA,
and the ArcGIS "DvlpmntApplication" layers stopped updating in 2019, so neither is used.
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

JURISDICTIONS = ["City of Groveland"]
JUR = "City of Groveland"
BASE = "https://www.groveland-fl.gov"
SOURCE = "Groveland AgendaCenter (P&Z + City Council packets and minutes)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
LOOKBACK_DAYS = 120
CATS = {10: "Planning & Zoning Board", 2: "City Council"}
SKIP_TITLE = re.compile(r"Workshop|Closed|Budget|Retreat|Symposium|Training|CANCEL|Executive", re.I)

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


def log(*a):
    print("[groveland]", *a, file=sys.stderr)


def req(method, url, tries=6, sleep=0.6, **kw):
    last = None
    for i in range(tries):
        try:
            r = _s.request(method, url, timeout=180, **kw)
            r.raise_for_status()
            time.sleep(sleep)
            return r
        except Exception as e:  # noqa: BLE001  (www.groveland-fl.gov often resets connections)
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"{method} failed {url}: {last}")


def sq(s):
    return re.sub(r"\s+", " ", s or "").strip()


# ------------------------------------------------------------------ AgendaCenter
def list_meetings(year, cat):
    t = req("POST", f"{BASE}/AgendaCenter/UpdateCategoryList",
            data={"year": year, "catID": cat, "startDate": "", "endDate": "", "term": "",
                  "prevVersionScreen": "false"}).text
    out = []
    for row in re.findall(r'<tr[^>]*class="catAgendaRow"[^>]*>(.*?)</tr>', t, re.S):
        a = re.search(r'href="(/AgendaCenter/ViewFile/Agenda/_(\d{2})(\d{2})(\d{4})-(\d+))"[^>]*>\s*(.*?)\s*</a>', row, re.S)
        if not a:
            continue
        mn = re.search(r'href="(/AgendaCenter/ViewFile/Minutes/[^"]+)"', row)
        posted = re.search(r"Posted\s*<abbr[^>]*>(\w+)</abbr>\s*(\d{1,2}),\s*(\d{4})", row)
        pdate = None
        if posted:
            try:
                pdate = dt.datetime.strptime(f"{posted.group(1)} {posted.group(2)} {posted.group(3)}",
                                             "%b %d %Y").date().isoformat()
            except ValueError:
                pdate = None
        date = f"{a.group(4)}-{a.group(2)}-{a.group(3)}"
        out.append({"date": date, "title": sq(_html.unescape(re.sub(r"<[^>]+>", " ", a.group(6)))),
                    "url": BASE + a.group(1), "minutes": BASE + mn.group(1) if mn else None,
                    "posted": pdate or date, "body": CATS[cat]})
    return out


def pdf_pages(url):
    content = req("GET", url).content
    rdr = pypdf.PdfReader(io.BytesIO(content))
    pages = []
    for p in rdr.pages:
        try:
            pages.append(p.extract_text() or "")
        except Exception:  # noqa: BLE001
            pages.append("")
    return pages


# ------------------------------------------------------------------ parsing
ORD_RE = re.compile(r"Ordinance\s*(\d{4})\s*-\s*(\d{1,3})\b", re.I)
IN_RE = re.compile(r"Rezon|Comprehensive\s+Plan\s+Amendment|\bPlan\s+Amendment|Future\s+Land\s+Use|\bPUD\b|Planned\s+Unit|"
                   r"Zoning\s+(?:Map|Change|Amendment)|\bSSCPA\b|\bLSCPA\b", re.I)
OUT_RE = re.compile(r"Annex|Text\s+Amendment|Element|Land\s+Development\s+Code|Community\s+Development\s+Code|"
                    r"\bCode\b|Impact\s+Fee|\bEAR\b|Evaluation\s+and\s+Appraisal|Moratorium", re.I)


def agenda_text(pages):
    out = []
    for i, t in enumerate(pages[:12]):
        out.append(t)
        if re.search(r"\bADJOURNMENT\b", t) and i >= 1:
            break
    return "\n".join(out)


def parse_agenda(text):
    """[(ord_no, heading, title_text, reading)] for in-scope ordinance items on an agenda."""
    t = text.replace("\u2013", "-").replace("\u2014", "-")
    items = []
    starts = [m for m in ORD_RE.finditer(t)]
    for i, m in enumerate(starts):
        seg = t[m.start(): starts[i + 1].start() if i + 1 < len(starts) else len(t)]
        seg = re.split(r"\n\s*(?:\d{1,2}\.\s|[A-Z]\.\s|[A-Z][A-Z&,/ ]{5,}\s*\n)", seg)[0]
        hm = re.match(r"Ordinance\s*\d{4}\s*-\s*\d{1,3}\s*:?\s*(.*?)(?=\bAn ordinance\b|$)", seg, re.S | re.I)
        heading = sq(hm.group(1)) if hm else ""
        title = sq(seg[hm.end():]) if hm else ""
        if not heading and not title:
            continue
        if not IN_RE.search(heading) or OUT_RE.search(heading):
            continue
        rd = re.search(r"\b(First|Flrst|Second|Final)\s*-?\s*Rea[a-z]{2,4}", heading, re.I)
        reading = ("first" if rd.group(1).lower().startswith("f") and rd.group(1).lower() != "final"
                   else rd.group(1).lower()) if rd else None
        heading = sq(re.sub(r"\s*-?\s*\b(First|Flrst|Second|Final)\s*-?\s*Rea[a-z]{2,4}.*$", "", heading, flags=re.I))
        items.append((f"{m.group(1)}-{m.group(2)}", heading, title, reading))
    # one entry per ordinance (the number can repeat in the text)
    seen, out = set(), []
    for it in items:
        if it[0] not in seen:
            seen.add(it[0])
            out.append(it)
    return out


def find_memo(pages, ono):
    """Text of the staff memo page(s) for an ordinance in a packet, or ''."""
    y, n = ono.split("-")
    pat = re.compile(rf"SUBJECT:.{{0,200}}?ORDINANCE\s*{y}\s*-\s*0*{int(n)}\b", re.I | re.S)
    for i, t in enumerate(pages):
        if "SUBJECT" in t.upper() and pat.search(t):
            return t + "\n" + (pages[i + 1] if i + 1 < len(pages) else "")
    return ""


def parse_memo(t):
    out = {}
    d = sq(t)
    m = re.search(r"application\s+(PLAN\s*\d{4}\s*-\s*\d{4})", d, re.I)
    if m:
        out["application"] = re.sub(r"\s", "", m.group(1)).upper()
    m = re.search(r"from (?:the )?(?:applicant|property owners?|owners?|agent)\s+(.+?)\s+(?:for|requesting|to)\s", d, re.I)
    if m:
        out["applicant"] = m.group(1).strip(" ,")
    m = re.search(r"approximately\s+([\d,]*\.?\d+)\s*(?:\+/-\s*)?(?:\(?\+/-\)?\s*)?acres", d, re.I) or \
        re.search(r"([\d,]*\.?\d+)\s*\+\s*/\s*-\s*acres", d, re.I)
    if m:
        try:
            out["acres"] = float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    keys = []
    for km in re.finditer(r"alternate\s+key\s*(?:numbers?|nos?\.?|#)?\s*(?:is|are|:)?\s*", d, re.I):
        seg = re.split(r"[A-Za-z]{4,}", d[km.end(): km.end() + 150])[0]
        keys += re.findall(r"\b\d{7}\b", seg)
    out["keys"] = list(dict.fromkeys(keys))
    m = re.search(r"Request:\s*(.+?)(?:\s+The applicant is concurrently|\s+Reason for Request|\s+SITE ANALYS|$)", d)
    if m:
        out["request"] = m.group(1)[:400]
    m = re.search(r"generally located\s+(.+?)(?:\s+in the\s|\.\s)", d, re.I)
    if m:
        out["location"] = m.group(1).strip(" ,")
    return out


def from_to(text, kind):
    d = sq(text)
    if kind == "CPA":
        m = re.search(r"Future Land Use (?:Plan )?designation\s+f\s?rom\s+(.+?)\s+to\s+(?:the\s+)?(.+?)"
                      r"(?:\s+on the Future Land Use Map|\s+for the|\s+on the property|\.|;|$)", d, re.I)
    else:
        m = re.search(r"(?:change the zoning|Rezoning|rezone)\s+(?:designation\s+)?from\s+(.+?)\s+to\s+(?:the\s+)?(.+?)"
                      r"(?:\s+for the|\s+on the property|\s+on approximately|\.|;|$)", d, re.I)
    if not m:
        return None, None
    return sq(m.group(1)).strip(" ,"), sq(m.group(2)).strip(" ,")


def minutes_results(text, board):
    """{ord_no: result} from P&Z or Council minutes (motion + outcome)."""
    d = sq(text)
    res = {}
    for m in re.finditer(r"(?:motion(?:ed)?|moved)\s+to\s+(approve|deny|recommend\s+approval(?:\s+of)?|recommend\s+denial(?:\s+of)?|"
                         r"table|continue|postpone|deny)\b(.{0,160}?)Ordinance\s*(\d{4})\s*-\s*(\d{1,3})", d, re.I):
        tail = d[m.end(): m.end() + 4000]
        nxt = re.search(r"Motion:|(?:motion(?:ed)?|moved)\s+to\s|\s\d{1,2}\.\s+(?:Consideration|Recommendation)", tail, re.I)
        if nxt:
            tail = tail[:nxt.start()]
        if re.search(r"motion\s+(?:failed|died)|failed\s+for\s+lack|Failed:|Denied:\s*\d", tail, re.I):
            continue
        if not re.search(r"motion\s+(?:passed|carried)|voted\s+aye|unanimous|Approved:\s*\d|passed\s+\d", tail, re.I):
            continue
        v = m.group(1).lower()
        ono = f"{m.group(3)}-{int(m.group(4)):02d}"
        if v.startswith(("table", "continue", "postpone")):
            r = "Continued"
        elif "den" in v:
            r = "Recommended Denial" if board != "City Council" else "Denied"
        else:
            r = "Recommended Approval" if board != "City Council" else "Approved"
        res[ono] = r
    for m in re.finditer(r"Ordinance\s*(\d{4})\s*-\s*(\d{1,3})[^.]{0,120}?(?:was|has been)\s+withdrawn", d, re.I):
        res[f"{m.group(1)}-{int(m.group(2)):02d}"] = "Withdrawn"
    return res


def norm_ono(o):
    y, n = o.split("-")
    return f"{y}-{int(n):02d}"


# ------------------------------------------------------------------ fetch
def fetch(since, refresh):
    if pypdf is None:
        raise RuntimeError("pypdf is required for the Groveland adapter")
    today = dt.date.today()
    refresh_ids = {}
    for r in refresh or []:
        m = re.search(r"(\d{4})\s*-\s*(\d{1,3})", r or "")
        if m:
            refresh_ids[norm_ono(f"{m.group(1)}-{m.group(2)}")] = r
    start_d = dt.date.fromisoformat(since) - dt.timedelta(days=LOOKBACK_DAYS)
    if refresh_ids:
        yr = min(int(k[:4]) for k in refresh_ids)
        start_d = min(start_d, max(dt.date(yr, 1, 1) - dt.timedelta(days=60), today - dt.timedelta(days=550)))
    start, end = start_d.isoformat(), (today + dt.timedelta(days=60)).isoformat()

    meetings = []
    for cat in CATS:
        for yr in range(start_d.year, (today + dt.timedelta(days=60)).year + 1):
            try:
                meetings += list_meetings(yr, cat)
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(f"Groveland AgendaCenter unreachable: {e}")
    meetings = [m for m in meetings if start <= m["date"] <= end and not SKIP_TITLE.search(m["title"])]
    meetings.sort(key=lambda m: (m["date"], m["body"] != "Planning & Zoning Board"))
    # keep one row per (date, body): revised packets replace earlier ones
    uniq = {}
    for m in meetings:
        uniq[(m["date"], m["body"])] = m
    meetings = sorted(uniq.values(), key=lambda m: m["date"])
    log(f"{len(meetings)} P&Z/Council meetings {start}..{end}")

    cases = {}
    pz_rec, pz_min = {}, []
    for mt in meetings:
        try:
            pages = pdf_pages(mt["url"])
        except Exception as e:  # noqa: BLE001
            log(f"packet {mt['date']} {mt['body']}: {e}")
            continue
        full = "\n".join(pages)
        for m in re.finditer(r"Planning\s*(?:&|and)\s*Zoning\s*Board\s*(?:Recommendation)?\s*:?\s*(?:Unanimous\s+)?"
                             r"(?:recommended\s+)?(Approval|Denial)\s+of\s+Ordinance\s*(\d{4})\s*-\s*(\d{1,3})\b", full, re.I):
            pz_rec.setdefault(f"{m.group(2)}-{int(m.group(3)):02d}",
                              "Recommended Approval" if m.group(1).lower().startswith("a") else "Recommended Denial")
        if mt["body"] == "Planning & Zoning Board" and re.search(r"Planning\s*(?:&|and)\s*Zoning\s*Board\s*Meeting\s*Minutes",
                                                                full, re.I):
            for k, v in minutes_results(full, mt["body"]).items():
                pz_min.append((mt["date"], k, v))
        items = parse_agenda(agenda_text(pages))
        if not items:
            continue
        mres = {}
        if mt["minutes"] and mt["date"] <= today.isoformat():
            try:
                mres = minutes_results("\n".join(pdf_pages(mt["minutes"])), mt["body"])
            except Exception as e:  # noqa: BLE001
                log(f"minutes {mt['date']}: {e}")
        for ono, heading, title, reading in items:
            ono = norm_ono(ono)
            memo = find_memo(pages, ono)
            rec = cases.setdefault(ono, {"apps": []})
            rec["apps"].append({"date": mt["date"], "posted": mt["posted"], "body": mt["body"], "reading": reading,
                                "heading": heading, "title": title, "memo": parse_memo(memo) if memo else {},
                                "memo_raw": memo, "url": mt["url"], "result": mres.get(ono)})
    log(f"{len(cases)} in-scope ordinances")
    for ono, c in cases.items():   # P&Z results from later packets (minutes / Council staff presentations)
        for a in c["apps"]:
            if a["body"] != "Planning & Zoning Board" or a["result"]:
                continue
            later = [v for (d, k, v) in sorted(pz_min) if k == ono and d > a["date"]]
            a["result"] = (later[0] if later else None) or pz_rec.get(ono)

    out = []
    for ono, c in sorted(cases.items()):
        apps = sorted(c["apps"], key=lambda a: a["date"])
        first = apps[0]
        # an ordinance first seen at a Council second reading started before our window
        older = first["body"] == "City Council" and first["reading"] in ("second", "final")
        if (first["posted"] < since or older) and ono not in refresh_ids:
            continue
        try:
            out.append(build(ono, apps, today.isoformat()))
        except Exception as e:  # noqa: BLE001
            log(f"Ordinance {ono}: skipped ({e})")
    for r in set(refresh_ids) - set(cases):
        log(f"refresh id {refresh_ids[r]} not on any agenda since {start}")
    return out


def build(ono, apps, today):
    heading = next((a["heading"] for a in apps if a["body"] == "Planning & Zoning Board"), apps[0]["heading"])
    is_cpa = bool(re.search(r"Comprehensive\s+Plan|Future\s+Land\s+Use|SSCPA|LSCPA", heading, re.I))
    kind = "CPA" if is_cpa else "RZ"
    memo = {}
    for a in sorted(apps, key=lambda a: (a["body"] != "Planning & Zoning Board", a["date"])):
        for k, v in a["memo"].items():
            if v and k not in memo:
                memo[k] = v
    titles = [a["title"] for a in apps if a["title"]]
    title = max(titles, key=len) if titles else ""
    f_, t_ = from_to(title, kind)
    if not f_ and memo.get("request"):
        f_, t_ = from_to(memo["request"], kind)
    acres = memo.get("acres")
    if acres is None:
        m = re.search(r"([\d,]*\.?\d+)\s*\+/-\s*acres", title, re.I)
        if m:
            acres = float(m.group(1).replace(",", ""))
    is_pd = bool(re.search(r"\bPUD\b|Planned\s+Unit", " ".join([heading, t_ or ""]), re.I))
    category = "Comp Plan Amendment" if is_cpa else ("PD" if is_pd else "Rezoning")
    if is_cpa:
        app_type = "Large-Scale Comprehensive Plan Amendment" if re.search(r"Large", heading, re.I) else \
            "Small-Scale Comprehensive Plan Amendment"
    else:
        app_type = "PUD Rezoning" if is_pd else "Rezoning"
    pname = sq(re.sub(r"(Small|Large)[- ]Scale\s+Comprehensive\s+Plan\s+Amendment|Comprehensive\s+Plan\s+Amendment|"
                      r"Future\s+Land\s+Use(\s+Map)?\s+Amendment|Rezoning|Planned\s+Unit\s+Development|\bPUD\b", "",
                      heading, flags=re.I)).strip(" -:,") or None
    desc_parts = []
    if memo.get("application"):
        desc_parts.append(f"Application {memo['application']}")
    if memo.get("request"):
        desc_parts.append(memo["request"])
    elif title:
        desc_parts.append(title)
    desc = "; ".join(desc_parts) or None
    if desc and len(desc) > 300:
        desc = desc[:297] + "..."

    hearings = []
    for a in apps:
        body = a["body"]
        if a["body"] == "City Council" and a["reading"]:
            body = f"City Council ({a['reading']} reading)"
        res = a["result"]
        if a["body"] == "City Council" and res == "Approved" and a["reading"] == "first":
            res = "Approved (first reading)"
        h = {"body": body, "date": a["date"], "result": res}
        if h not in hearings:
            hearings.append(h)
    past = [h for h in hearings if h["date"] <= today]
    future = [h for h in hearings if h["date"] > today]
    final = [h for h in past if h["result"] in ("Approved", "Denied") and h["body"].startswith("City Council")]
    decision = None
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
        if lp["body"].startswith("City Council") and lp["result"] in (None,):
            status, raw = "Hearing Scheduled", f"{lp['body']} {lp['date']} held; outcome not yet in published minutes"
        else:
            status = "Filed"
            raw = f"{lp['body']} {lp['date']}" + (f": {lp['result']}" if lp["result"] else "") + \
                "; next hearing not yet on an agenda"
    else:
        status, raw = "Filed", "On agenda"
    pz = [a for a in apps if a["body"] == "Planning & Zoning Board"]
    return {
        "jurisdiction": JUR, "county": "Lake", "app_num": f"Ordinance {ono}", "category": category,
        "app_type": app_type, "project_name": pname, "description": desc, "applicant": memo.get("applicant"),
        "parcel_ids": memo.get("keys") or [], "address": memo.get("location"), "acres": acres,
        "from_zoning": f_ if kind == "RZ" else None, "to_zoning": t_ if kind == "RZ" else None,
        "from_flu": f_ if kind == "CPA" else None, "to_flu": t_ if kind == "CPA" else None,
        "filed_date": min(a["posted"] for a in apps), "status": status, "status_raw": raw, "hearings": hearings,
        "decision_date": decision, "url": apps[-1]["url"], "docs_url": (pz[0] if pz else apps[0])["url"],
        "lat": None, "lon": None, "source": SOURCE,
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
