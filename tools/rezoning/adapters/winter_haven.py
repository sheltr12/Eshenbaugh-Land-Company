#!/usr/bin/env python3
"""City of Winter Haven (Polk County) rezoning / PUD / future land use map amendment adapter.

Source: the city's Granicus meeting portal https://winterhaven-fl.granicus.com/ViewPublisher.php?view_id=1
 * City Commission (and, when posted, Planning Commission) agendas are HTML pages
   (AgendaViewer.php -> granicus_production_attachments S3 HTML). Land use cases are ordinances:
   "O-26-24 - Request by Ethan Falk, agent for Millers Landing, LLC to rezone a parcel from Planned
   Unit Development (PUD) zoning district to Commercial-Highway (C-3) zoning district", listed under
   "ORDINANCE(S) - FIRST READING" and then "ORDINANCE(S) - SECOND READING - PUBLIC HEARING".
 * Each item links its Legistar attachments; the "Summary - Ordinance O-26-24" fact sheet gives the
   general location, acreage, applicant and the Planning Commission recommendation and date.
 * Outcomes come from the draft City Commission minutes attached to later agendas
   ("2026-09-15 DRAFT Regular City Commission Meeting Minutes ...": "Motion carried 5:0").
 app_num = the ordinance number ("O-26-24"). Text amendments (Code of Ordinances / Table of Land
 Uses), budget ordinances, the citywide 2050 comprehensive plan / FLUM series and annexation-only
 ordinances are skipped. The Planning Commission stopped posting agendas on Granicus in March 2026,
 so a case usually first appears at its City Commission first reading (the fact sheet supplies the
 Planning Commission hearing date). Parcel ids are not published in the HTML or fact sheets.
The S3 host name contains underscores, which fails TLS host checks, so redirects to it are rewritten
to the equivalent path-style URL (https://s3.amazonaws.com/granicus_production_attachments/...).
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

JURISDICTIONS = ["City of Winter Haven"]
JUR = "City of Winter Haven"
BASE = "https://winterhaven-fl.granicus.com"
PUBLISHER = BASE + "/ViewPublisher.php?view_id=1"
SOURCE = "Winter Haven Granicus (City Commission agendas, ordinance fact sheets, draft minutes)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
LOOKBACK_DAYS = 150
PC = "Planning Commission"
CC = "City Commission"

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


def log(*a):
    print("[winter_haven]", *a, file=sys.stderr)


def get(url, tries=4, sleep=0.4):
    last = None
    for i in range(tries):
        try:
            u = url
            for _ in range(8):
                r = _s.get(u, timeout=120, allow_redirects=False)
                if r.status_code in (301, 302, 303, 307, 308):
                    u = r.headers["Location"]
                    if u.startswith("//"):
                        u = "https:" + u
                    m = re.match(r"https?://granicus_production_attachments\.s3\.amazonaws\.com/(.*)", u)
                    if m:
                        u = "https://s3.amazonaws.com/granicus_production_attachments/" + m.group(1)
                    continue
                break
            r.raise_for_status()
            time.sleep(sleep)
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


def sq(s):
    s = re.sub(r"\s+", " ", _html.unescape(s or "").replace("\u00a0", " ")).strip()
    return s or None


def nd(s):
    return (s or "").replace("\u2013", "-").replace("\u2014", "-").replace("\u2019", "'")


def pdf_text(url, max_pages=None):
    rdr = pypdf.PdfReader(io.BytesIO(get(url).content))
    out = []
    for p in rdr.pages[:max_pages] if max_pages else rdr.pages:
        try:
            out.append(p.extract_text() or "")
        except Exception:  # noqa: BLE001
            out.append("")
    return nd("\n".join(out))


# ------------------------------------------------------------------ meetings
def list_meetings(start):
    h = get(PUBLISHER).text
    out = []
    for row in re.findall(r'<tr[^>]*class="listingRow"[^>]*>(.*?)</tr>', h, re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(cells) < 2:
            continue
        name = sq(re.sub(r"<[^>]+>", " ", cells[0])) or ""
        if re.search(r"Workshop|Review Session|Budget|CRA|Redevelopment|Test", name, re.I):
            continue
        if re.search(r"Planning Commission", name, re.I):
            body = PC
        elif re.search(r"City Commission", name, re.I):
            body = CC
        else:
            continue
        ds = sq(re.sub(r"<[^>]+>", " ", cells[1])) or ""
        m = re.match(r"([A-Z][a-z]{2})\s+(\d{1,2}),\s*(\d{4})", ds)
        if not m:
            continue
        d = dt.datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%b %d %Y").date().isoformat()
        if d < start:
            continue
        a = re.search(r'href="((?:https?:)?//winterhaven-fl\.granicus\.com/AgendaViewer\.php\?[^"]+)"', row)
        if not a:
            continue
        url = _html.unescape(a.group(1))
        url = "https:" + url if url.startswith("//") else url
        pk = re.search(r'href="(https://[^"]+\.pdf)"', row)
        out.append({"date": d, "body": body, "name": name, "url": url, "packet": pk.group(1) if pk else None})
    uniq = {(m["date"], m["body"], m["url"]): m for m in out}
    return sorted(uniq.values(), key=lambda m: m["date"])


def agenda_items(html_text):
    """[(section, number, title, [(attachment_title, url)])]."""
    t = re.sub(r"<script.*?</script>|<style.*?</style>", "", html_text, flags=re.S | re.I)
    t = re.sub(r'<a [^>]*href="([^"]+)"[^>]*>(.*?)</a>', lambda m: f"\n@@LINK {m.group(1)} {sq(re.sub('<[^>]+>', ' ', m.group(2))) or ''}\n",
               t, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", "\n", t)
    lines = [nd(sq(x)) for x in t.split("\n")]
    lines = [x for x in lines if x]
    items, section, cur = [], None, None
    i = 0
    while i < len(lines):
        ln = lines[i]
        if re.fullmatch(r"\d{1,2}\.", ln) and i + 1 < len(lines):
            section = lines[i + 1]
            cur = None
            i += 2
            continue
        if re.fullmatch(r"\d{1,2}\.[A-Z]{1,2}", ln) and i + 1 < len(lines):
            cur = {"section": section, "no": ln, "title": lines[i + 1], "links": []}
            items.append(cur)
            i += 2
            continue
        if ln.startswith("@@LINK ") and cur is not None:
            parts = ln.split(" ", 2)
            cur["links"].append((parts[2] if len(parts) > 2 else "", _html.unescape(parts[1])))
        i += 1
    return items


ORD_RE = re.compile(r"\bO-(\d{2})-(\d{1,3})\b")
IN_RE = re.compile(r"rezon|Planned Unit Development|\bPUD\b|Future Land Use|land use map|Comprehensive Plan|"
                   r"zoning district|zoning map", re.I)
OUT_RE = re.compile(r"Code of Ordinances|Table of Land Uses|Chapter \d+|appropriat|Millage|Budget|Map Series|"
                    r"2050|Evaluation and Appraisal|\bEAR\b|Compensation|Pretreatment|Rates", re.I)


def classify(title):
    m = ORD_RE.search(title or "")
    if not m or m.start() > 15:
        return None
    rest = title[m.end():]
    if not IN_RE.search(rest) or OUT_RE.search(rest):
        return None
    if re.search(r"Future Land Use|land use map|Comprehensive Plan", rest, re.I) and not re.search(r"rezone", rest, re.I):
        kind = "CPA"
    else:
        kind = "ZONING"
    return f"O-{m.group(1)}-{m.group(2)}", kind


def reading_of(section):
    s = section or ""
    if re.search(r"SECOND READING", s, re.I):
        return "second"
    if re.search(r"FIRST READING", s, re.I):
        return "first"
    return None


def minutes_outcomes(text):
    """{ordinance: word} from City Commission minutes text."""
    t = sq(text) or ""
    out = {}
    ms = list(re.finditer(r"\b[A-Z]\.\s+(O-\d{2}-\d{1,3})\s+-\s", t))
    for i, m in enumerate(ms):
        seg = t[m.end(): ms[i + 1].start() if i + 1 < len(ms) else min(len(t), m.end() + 4000)]
        seg = re.split(r"\s\d{1,2}\.\s+[A-Z][A-Z(),' -]{6,}\s", seg)[0]
        ono = m.group(1)
        if re.search(r"withdr[ae]w", seg, re.I):
            out[ono] = "withdrawn"
        elif re.search(r"Motion (?:to (?:table|continue|postpone|defer))|tabled|continued to", seg, re.I) and \
                not re.search(r"Motion carried", seg[-300:], re.I):
            out[ono] = "continued"
        elif re.search(r"Motion (?:failed|died)", seg, re.I):
            out[ono] = "failed"
        elif re.search(r"Motion carried|Motion passed", seg, re.I):
            out[ono] = "carried"
        elif re.search(r"come back for (?:a )?second reading", seg, re.I):
            out[ono] = "first"
    return out


# ------------------------------------------------------------------ fetch
def fetch(since, refresh):
    if pypdf is None:
        raise RuntimeError("pypdf is required for the Winter Haven adapter")
    today = dt.date.today()
    refresh_ids = {}
    for r in refresh or []:
        m = ORD_RE.search(r or "")
        if m:
            refresh_ids[f"O-{m.group(1)}-{m.group(2)}"] = r
    start = (dt.date.fromisoformat(since) - dt.timedelta(days=LOOKBACK_DAYS)).isoformat()
    if refresh_ids:
        yr = min(int(k[2:4]) for k in refresh_ids) + 2000
        start = min(start, max(f"{yr - 1}-10-01", (today - dt.timedelta(days=730)).isoformat()))
    meetings = list_meetings(start)
    if not meetings:
        raise RuntimeError("no Winter Haven meetings found on Granicus")
    log(f"{len(meetings)} meetings since {start}")

    cases, minutes_docs = {}, {}
    for mt in meetings:
        try:
            items = agenda_items(get(mt["url"]).text)
        except Exception as e:  # noqa: BLE001
            log(f"agenda {mt['date']} {mt['body']}: {e}")
            continue
        for it in items:
            mm = re.match(r"([A-Z][a-z]+ \d{1,2}, \d{4}),?\s+(?:Regular |Special )?City Commission (?:Meeting )?Minutes",
                          it["title"] or "")
            if mm and re.search(r"MINUTES", it["section"] or "", re.I):
                try:
                    md = dt.datetime.strptime(mm.group(1), "%B %d, %Y").date().isoformat()
                except ValueError:
                    md = None
                pdfs = [u for lab, u in it["links"] if u.lower().endswith(".pdf")]
                if md and pdfs:
                    minutes_docs.setdefault(md, pdfs[0])
                continue
            c = classify(it["title"])
            if not c:
                continue
            ono, kind = c
            rec = cases.setdefault(ono, {"kind": kind, "apps": []})
            rec["apps"].append({"date": mt["date"], "body": mt["body"], "section": it["section"],
                                "reading": reading_of(it["section"]), "title": it["title"], "links": it["links"],
                                "url": mt["url"], "packet": mt["packet"]})
    log(f"{len(cases)} in-scope ordinances; {len(minutes_docs)} minutes documents")

    outcomes = {}
    need = {a["date"] for c in cases.values() for a in c["apps"] if a["body"] == CC}
    for md, url in sorted(minutes_docs.items()):
        if md not in need:
            continue
        try:
            outcomes[md] = minutes_outcomes(pdf_text(url, max_pages=20))
        except Exception as e:  # noqa: BLE001
            log(f"minutes {md}: {e}")

    out = []
    for ono, c in sorted(cases.items()):
        first = min(a["date"] for a in c["apps"])
        if first < since and ono not in refresh_ids:
            continue
        try:
            out.append(build(ono, c, outcomes, today.isoformat()))
        except Exception as e:  # noqa: BLE001
            log(f"{ono}: skipped ({e})")
    for r in set(refresh_ids) - set(cases):
        log(f"refresh id {refresh_ids[r]} not found on agendas since {start}")
    return out


def parse_fact(t):
    d = sq(t) or ""
    out = {}
    m = re.search(r"General Location\s*:\s*(.+?)\.\s", d)
    if m:
        out["location"] = m.group(1).strip()
    m = re.search(r"request is\s*([\d,]*\.?\d+)\s*(?:\u00b1|\+/-|\+)?\s*acres", d, re.I) or \
        re.search(r"([\d,]*\.?\d+)\s*(?:\u00b1|\+/-)\s*acres", d, re.I)
    if m:
        try:
            out["acres"] = float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    m = re.search(r"Planning Commission,? at its ([A-Z][a-z]+ \d{1,2}, \d{4})[^.]*?(?:voted|meeting,)\s*(?:unanimously\s+)?"
                  r"(?:voted\s+)?(?:\d-\d\s+)?to recommend (approval|denial)", d, re.I)
    if m:
        try:
            out["pc_date"] = dt.datetime.strptime(m.group(1), "%B %d, %Y").date().isoformat()
            out["pc_result"] = "Recommended Approval" if m.group(2).lower() == "approval" else "Recommended Denial"
        except ValueError:
            pass
    m = re.search(r"current adopted Future Land Use on the property is\s+(.+?)\s*\.", d, re.I)
    if m:
        out["flu"] = m.group(1)
    pids = re.findall(r"\b\d{2}-\d{2}-\d{2}-\d{6}-\d{6}\b", d)
    out["parcels"] = list(dict.fromkeys(pids))
    return out


def build(ono, c, outcomes, today):
    apps = sorted(c["apps"], key=lambda a: a["date"])
    title = apps[-1]["title"]
    fact, fact_url = {}, None
    for a in reversed(apps):
        cand = [u for lab, u in a["links"] if re.search(r"^Summary", lab or "", re.I) and u.lower().endswith(".pdf")]
        if cand:
            try:
                fact = parse_fact(pdf_text(cand[0], max_pages=4))
                fact_url = cand[0]
            except Exception as e:  # noqa: BLE001
                log(f"{ono}: fact sheet unreadable ({e})")
            break

    hearings = []
    if fact.get("pc_date"):
        hearings.append({"body": PC, "date": fact["pc_date"], "result": fact.get("pc_result")})
    cc_n = 0
    for a in apps:
        if a["body"] == PC:
            h = {"body": PC, "date": a["date"], "result": None}
        else:
            reading = a["reading"] or ("second" if cc_n else "first")
            cc_n += 1
            word = outcomes.get(a["date"], {}).get(ono)
            res = None
            if word == "withdrawn":
                res = "Withdrawn"
            elif word == "continued":
                res = "Continued"
            elif word == "failed":
                res = "Denied"
            elif word in ("carried", "first"):
                res = "Approved" if reading == "second" else "Approved (first reading)"
            h = {"body": f"{CC} ({'second reading' if reading == 'second' else 'first reading'})",
                 "date": a["date"], "result": res}
        if not any(x["body"] == h["body"] and x["date"] == h["date"] for x in hearings):
            hearings.append(h)
    hearings.sort(key=lambda h: h["date"])

    status, raw, decision = None, None, None
    past = [h for h in hearings if h["date"] <= today]
    future = [h for h in hearings if h["date"] > today]
    final = [h for h in hearings if h["body"].startswith(CC) and h["result"] in ("Approved", "Denied")]
    if any(h["result"] == "Withdrawn" for h in hearings):
        status, raw = "Withdrawn", "Withdrawn (minutes)"
    elif final:
        status, decision = final[-1]["result"], final[-1]["date"]
        raw = f"{final[-1]['body']} {decision}: {final[-1]['result']} (draft minutes)"
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

    t = sq(title) or ""
    rest = ORD_RE.sub("", t, count=1).strip(" -")
    applicant = None
    m = re.match(r"Request by\s+(.+?)\s+to\s+(?:rezone|amend|assign|change)", rest, re.I)
    if m:
        applicant = m.group(1).strip(" ,")
    frm = to = None
    m = re.search(r"from\s+(.+?)\s+(?:zoning district\s+|land use\s+)?to\s+(.+?)(?:\s+zoning district|\s+(?:future )?land use|$)", rest, re.I)
    if m:
        frm, to = sq(m.group(1)), sq(m.group(2))
        frm = re.sub(r"\s*zoning district$", "", frm, flags=re.I)
        to = re.sub(r"\s*\([0-9][^)]*\)\s*$", "", to)
    addr = fact.get("location")
    if not addr:
        m = re.search(r"\(([0-9][^)]+)\)\s*$", rest)
        addr = m.group(1) if m else None
    kind = c["kind"]
    if kind == "CPA":
        category, app_type = "Comp Plan Amendment", "Future Land Use Map Amendment"
    else:
        is_pd = bool(re.search(r"Planned Unit Development|\bPUD\b", to or "")) or \
            bool(re.search(r"amend (?:a )?(?:Planned Unit Development|PUD)", rest, re.I))
        category = "PD" if is_pd else "Rezoning"
        if re.search(r"amend Planned Unit Development|amend PUD", rest, re.I):
            app_type = "PUD Amendment"
            frm = frm or "PUD"
            to = to or "PUD"
        else:
            app_type = "Rezoning (PUD)" if is_pd else "Rezoning"
    first = apps[0]["date"]
    filed = min([first] + ([fact["pc_date"]] if fact.get("pc_date") else []))
    latest = apps[-1]
    return {
        "jurisdiction": JUR, "county": "Polk", "app_num": ono, "category": category, "app_type": app_type,
        "project_name": None, "description": rest[:500] or None, "applicant": applicant,
        "parcel_ids": fact.get("parcels") or [], "address": addr, "acres": fact.get("acres"),
        "from_zoning": frm if kind == "ZONING" else None, "to_zoning": to if kind == "ZONING" else None,
        "from_flu": (frm or fact.get("flu")) if kind == "CPA" else None, "to_flu": to if kind == "CPA" else None,
        "filed_date": filed, "status": status, "status_raw": raw, "hearings": hearings,
        "decision_date": decision, "url": latest["url"], "docs_url": fact_url or latest["packet"],
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
