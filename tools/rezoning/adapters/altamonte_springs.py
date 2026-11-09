#!/usr/bin/env python3
"""City of Altamonte Springs (Seminole County) rezoning / PUD / future land use map amendment adapter.

Source: the city's CivicPlus AgendaCenter (www.altamonte.org/AgendaCenter), category 1 "City Commission".
Altamonte's Planning Board agendas are not posted online; every land use case reaches the City
Commission as numbered ordinances (first and second reading), and the Commission memo states the
Planning Board recommendation.
 * Agenda PDFs list "PUBLIC HEARING (1st Reading) Ordinance No. 1817-24 - Request to Rezone 176 and 180
   Vagabond Way" or "Ordinance Nos. 1798-23 and 1799-23 - Request to Change the future land use
   designation and rezone property at 1255 East Altamonte Drive" (first number = FLU amendment, second =
   rezoning). app_num = "Ordinance No. NNNN-YY". Text amendments (Land Development Code, City Code,
   Comprehensive Plan elements), annexations and PUD master plan extensions are skipped.
 * Some agenda files are full packets that include the Commission memo ("SUBJECT: Ordinance No. ...");
   when present it is used for acreage, from/to designations, applicant, parcel numbers and the Planning
   Board hearing date/recommendation.
 * Outcomes: Commission minutes PDFs on the same AgendaCenter row ("Motion: by Commissioner X ... to pass
   and adopt Ordinance No. 1817-24 on second and final reading. The motion carried ...").
Note: the Commission heard no rezoning / FLUM / PUD ordinances between mid 2025 and Nov 2026, so short
runs can legitimately return an empty list. Requires pypdf.
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

JURISDICTIONS = ["City of Altamonte Springs"]
JUR = "City of Altamonte Springs"
COUNTY = "Seminole"
BASE = "https://www.altamonte.org"
CAT = 1
SOURCE = "Altamonte Springs AgendaCenter (City Commission agendas, memos and minutes)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
LOOKBACK_DAYS = 120
CC = "City Commission"
PB = "Planning Board"

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


def log(*a):
    print("[altamonte_springs]", *a, file=sys.stderr)


def req(method, url, tries=6, sleep=0.5, **kw):
    last = None
    for i in range(tries):
        try:
            r = _s.request(method, url, timeout=180, **kw)
            r.raise_for_status()
            time.sleep(sleep)
            return r
        except Exception as e:  # noqa: BLE001  (www.altamonte.org intermittently resets connections)
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"{method} failed {url}: {last}")


def sq(s):
    return re.sub(r"\s+", " ", s or "").strip()


def list_meetings(year):
    t = req("POST", f"{BASE}/AgendaCenter/UpdateCategoryList",
            data={"year": year, "catID": CAT, "startDate": "", "endDate": "", "term": "",
                  "prevVersionScreen": "false"}).text
    out = []
    for row in re.findall(r'<tr[^>]*class="catAgendaRow"[^>]*>(.*?)</tr>', t, re.S):
        a = re.search(r'href="(/AgendaCenter/ViewFile/Agenda/_(\d{2})(\d{2})(\d{4})-(\d+))"[^>]*>\s*(.*?)\s*</a>', row, re.S)
        if not a:
            continue
        mn = re.search(r'href="(/AgendaCenter/ViewFile/Minutes/[^"]+)"', row)
        posted = re.search(r"Posted\s*<abbr[^>]*>(\w+)</abbr>\s*(\d{1,2}),\s*(\d{4})", row) or \
            re.search(r"Posted\s+(\w{3})\w*\s+(\d{1,2}),\s*(\d{4})", _html.unescape(re.sub(r"<[^>]+>", " ", row)))
        pdate = None
        if posted:
            try:
                pdate = dt.datetime.strptime(f"{posted.group(1)[:3]} {posted.group(2)} {posted.group(3)}",
                                             "%b %d %Y").date().isoformat()
            except ValueError:
                pdate = None
        date = f"{a.group(4)}-{a.group(2)}-{a.group(3)}"
        title = sq(_html.unescape(re.sub(r"<[^>]+>", " ", a.group(6))))
        out.append({"date": date, "title": title, "url": BASE + a.group(1),
                    "minutes": BASE + mn.group(1) if mn else None,
                    "posted": min(pdate, date) if pdate else date})
    return out


def pdf_pages(content):
    rdr = pypdf.PdfReader(io.BytesIO(content))
    pages = []
    for p in rdr.pages:
        try:
            pages.append(p.extract_text() or "")
        except Exception:  # noqa: BLE001
            pages.append("")
    return pages


# ------------------------------------------------------------------ parsing
NUM_RE = re.compile(r"\b(1[6-9]\d\d|2\d\d\d)\s*-\s*(\d{2})\b")
ORD_HEAD_RE = re.compile(r"Ordinance\s*(?:No\s*s?\.?|Nos\.?|Numbers?)\s*", re.I)
CPA_RE = re.compile(r"Future\s+Land\s+Use|Comprehensive\s+Plan\s+(?:Map\s+)?Amendment|Small[- ]Scale|Large[- ]Scale|\bFLUM?\b",
                    re.I)
RZ_RE = re.compile(r"\bRezon|Zoning\s+(?:Map\s+)?(?:Change|Amendment)|Planned\s+Unit\s+Development|\bPUD\b", re.I)
OUT_RE = re.compile(r"Land\s+Development\s+Code|Code\s+of\s+Ordinances|City\s+Code|Capital\s+Improvement|Element|"
                    r"Overlay|Text|Extension|Extend|Annex|Moratorium|Impact\s+Fee|Chapter\s+\d", re.I)


def okey(m):
    return f"{m.group(1)}-{m.group(2)}"


def agenda_items(text):
    """[(heading, [ordinance keys], title, reading)] for ordinance hearing items on an agenda."""
    t = text.split("The above meeting")[0].replace("\u2013", "-").replace("\u2014", "-")
    out = []
    for m in re.finditer(r"(?m)^\s*\d{1,2}\.\s+(.*(?:\n(?!\s*\d{1,2}\.\s).*)*)", t):
        s = sq(m.group(1))
        s = re.sub(r"No\s+s\s*\.", "Nos.", s)
        om = ORD_HEAD_RE.search(s)
        if not om:
            continue
        head = s[:om.start()]
        rest = s[om.end():]
        km = re.match(r"((?:\s*(?:and|,|&)?\s*\d{4}\s*-\s*\d{2})+)", rest)
        if not km:
            continue
        keys = [okey(x) for x in NUM_RE.finditer(km.group(1))]
        title = sq(rest[km.end():]).strip(" -:")
        rd = re.search(r"\((1st|2nd|First|Second|Final|Continuance|Continued)[^)]*\)", head, re.I)
        reading = None
        if rd:
            w = rd.group(1).lower()
            reading = "first" if w in ("1st", "first") else ("second" if w in ("2nd", "second", "final") else "continued")
        out.append((head, keys, title, reading))
    return out


def split_kinds(keys, title):
    """[(key, kind)] for in-scope items."""
    if OUT_RE.search(title):
        return []
    is_cpa, is_rz = bool(CPA_RE.search(title)), bool(RZ_RE.search(title))
    if is_cpa and is_rz and len(keys) >= 2:
        return [(keys[0], "CPA"), (keys[1], "RZ")]
    if is_cpa:
        return [(keys[0], "CPA")]
    if is_rz:
        return [(keys[-1], "RZ")]
    return []


def memo_for(pages, key):
    """Commission memo text for an ordinance number, if the agenda file includes memos."""
    y, n = key.split("-")
    pat = re.compile(r"SUBJECT:.{0,200}?" + y + r"\s*-\s*" + n, re.S | re.I)
    for i, p in enumerate(pages):
        if pat.search(p or ""):
            return "\n".join(pages[i:i + 3])
    return ""


def memo_fields(t):
    out = {}
    d = sq(t)
    m = re.search(r"([\d,]*\.?\d+)\s*(?:\+/-|±)?\s*(?:gross\s+)?acres", d, re.I)
    if m:
        try:
            out["acres"] = float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    m = re.search(r"\bfrom\s+([A-Z][^.;]{1,80}?)\s+to\s+([A-Z][^.;]{1,80}?)(?:\s+(?:for|on|at|zoning|future|designation)\b|[.;,])",
                  d)
    if m:
        out["from"], out["to"] = sq(m.group(1)), sq(m.group(2))
    ids = re.findall(r"\b\d{2}-\d{2}-\d{2}-[0-9A-Z]{3}-[0-9A-Z]{4}-[0-9A-Z]{4}\b", d)
    out["ids"] = list(dict.fromkeys(ids))
    m = re.search(r"\b(?:applicant|owner)(?:,|\s+is|:)\s+([A-Z][\w&.,' -]{2,70}?(?:LLC|Inc\.?|Corp\.?|Company|Trust|LP|L\.P\.))",
                  d)
    if m:
        out["applicant"] = m.group(1)
    m = re.search(r"(?:At its|On)\s+(\w+ \d{1,2}, \d{4})(?:,)?\s+(?:meeting,?\s+)?the\s+Planning\s+Board\s+"
                  r"(?:unanimously\s+)?(recommended\s+(?:approval|denial)|voted[^.]{0,60})", d, re.I)
    if m:
        try:
            out["pb_date"] = dt.datetime.strptime(m.group(1), "%B %d, %Y").date().isoformat()
            out["pb_result"] = "Recommended Denial" if re.search(r"den", m.group(2), re.I) else "Recommended Approval"
        except ValueError:
            pass
    return out


MOTION_RE = re.compile(r"Motion\b.{0,250}?\b(?:to|that\s+we|that\s+the\s+(?:City\s+)?Commission)\s+"
                       r"(pass\w*|approv\w*|adopt\w*|den\w+|tabl\w*|continu\w*|postpon\w*|defer\w*)\b", re.I)


def minutes_results(text):
    d = sq(text)
    d = re.sub(r"No\s+s\s*\.", "Nos.", d)
    res = {}
    for m in MOTION_RE.finditer(d):
        after = d[m.end(): m.end() + 700]
        o = re.search(r"\b(carried|passed|failed)\b", after, re.I)
        if not o or o.group(1).lower() == "failed":
            continue
        span = after[:min(o.start(), 300)]
        keys = []
        for om in ORD_HEAD_RE.finditer(span):
            km = re.match(r"((?:\s*(?:and|,|&)?\s*\d{4}\s*-\s*\d{2})+)", span[om.end():])
            if km:
                keys += [okey(x) for x in NUM_RE.finditer(km.group(1))]
        v = m.group(1).lower()
        val = ("Continued" if v.startswith(("tabl", "contin", "postp", "defer")) else
               "Denied" if v.startswith("den") else "Approved")
        for k in keys:
            res[k] = val
    for m in re.finditer(r"withdr[ae]w\w*[^.]{0,120}?Ordinance\s*(?:No\.?|Nos\.?)?\s*(\d{4})\s*-\s*(\d{2})", d, re.I):
        res[okey(m)] = "Withdrawn"
    return res


# ------------------------------------------------------------------ fetch
def fetch(since, refresh):
    if pypdf is None:
        raise RuntimeError("pypdf is required for the Altamonte Springs adapter")
    today = dt.date.today()
    refresh_ids = {}
    for r in refresh or []:
        m = NUM_RE.search(r or "")
        if m:
            refresh_ids[okey(m)] = r
    start_d = dt.date.fromisoformat(since) - dt.timedelta(days=LOOKBACK_DAYS)
    if refresh_ids:
        yr = min(2000 + int(k[-2:]) for k in refresh_ids)
        start_d = min(start_d, max(dt.date(yr, 1, 1), today - dt.timedelta(days=900)))
    meetings = []
    for y in range(start_d.year, today.year + 2):
        try:
            meetings += list_meetings(y)
        except Exception as e:  # noqa: BLE001
            if y <= today.year:
                raise
            log(f"list {y}: {e}")
    meetings = [m for m in meetings if m["date"] >= start_d.isoformat()
                and not re.search(r"Cancel", m["title"], re.I)]
    log(f"{len(meetings)} City Commission agendas since {start_d}")

    cases, minutes = {}, {}
    for mt in sorted(meetings, key=lambda m: m["date"]):
        try:
            pages = pdf_pages(req("GET", mt["url"]).content)
        except Exception as e:  # noqa: BLE001
            log(f"agenda {mt['date']}: {e}")
            continue
        head_txt = "\n".join(pages[:3])
        for head, keys, title, reading in agenda_items(head_txt):
            for key, kind in split_kinds(keys, title):
                memo = memo_for(pages, key)
                rec = cases.setdefault(key, {"kind": kind, "apps": []})
                rec["apps"].append({"date": mt["date"], "first_seen": mt["posted"], "title": title,
                                    "reading": reading, "memo": memo, "url": mt["url"],
                                    "minutes": mt["minutes"], "keys": keys})
    log(f"{len(cases)} in-scope ordinances")
    for c in cases.values():
        for a in c["apps"]:
            u = a["minutes"]
            if u and u not in minutes:
                try:
                    minutes[u] = minutes_results("\n".join(pdf_pages(req("GET", u).content)))
                except Exception as e:  # noqa: BLE001
                    log(f"minutes {a['date']}: {e}")
                    minutes[u] = {}

    out = []
    for key, c in sorted(cases.items()):
        first = min(a["first_seen"] for a in c["apps"])
        if first < since and key not in refresh_ids:
            continue
        try:
            out.append(build(key, c, minutes, today.isoformat()))
        except Exception as e:  # noqa: BLE001
            log(f"Ordinance {key}: skipped ({e})")
    for r in set(refresh_ids) - set(cases):
        log(f"refresh id {refresh_ids[r]} not found on any agenda since {start_d}")
    return out


def build(key, c, minutes, today):
    apps = sorted(c["apps"], key=lambda a: a["date"])
    latest = apps[-1]
    kind = c["kind"]
    title = max((a["title"] for a in apps), key=len)
    memo = next((a["memo"] for a in reversed(apps) if a["memo"]), "")
    mf = memo_fields(memo) if memo else {}
    am = re.search(r"(?<![\w-])((?:\d{2,6}(?:\s*(?:and|&|,)\s*\d{2,6})*)\s+[A-Z0-9][\w .']{2,40}?"
                   r"(?:Road\s+\d{2,3}|Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Way|Boulevard|Blvd|Lane|Ln|Pointe|Court|Ct|Circle|Parkway|Pkwy|"
                   r"Place|Pl|Trail|Highway|Hwy|Point\s?e))\b", title)
    address = re.sub(r"\bPoint e\b", "Pointe", sq(am.group(1))) if am else None
    is_pd = bool(re.search(r"\bPUD\b|Planned\s+Unit", title + " " + (mf.get("to") or ""), re.I))
    category = "Comp Plan Amendment" if kind == "CPA" else ("PD" if is_pd else "Rezoning")
    app_type = ("Future Land Use Map Amendment" if kind == "CPA" else ("PUD Rezoning" if is_pd else "Rezoning"))

    hearings = []
    if mf.get("pb_date"):
        hearings.append({"body": PB, "date": mf["pb_date"], "result": mf.get("pb_result")})
    for i, a in enumerate(apps):
        res = minutes.get(a["minutes"], {}).get(key) if a["minutes"] else None
        later = apps[i + 1:]
        if res is None and a["reading"] == "first" and any(b["reading"] == "second" for b in later):
            res = "Approved"
        if res is None and a["reading"] == "continued" and a["date"] <= today:
            res = "Continued"
        if a["reading"] == "first" and res == "Approved":
            res = "Approved (first reading)"
        rd = {"first": " (first reading)", "second": " (second reading)"}.get(a["reading"], "")
        h = {"body": CC + rd, "date": a["date"], "result": res}
        if h not in hearings:
            hearings.append(h)
    status, raw, decision = status_from(hearings, today)
    desc = title
    if len(latest["keys"]) > 1:
        desc = f"{title} (companion ordinances {', '.join('No. ' + k for k in latest['keys'])})"
    if len(desc) > 300:
        desc = desc[:297] + "..."
    filed = min(a["first_seen"] for a in apps)
    if mf.get("pb_date"):
        filed = min(filed, mf["pb_date"])
    return {
        "jurisdiction": JUR, "county": COUNTY, "app_num": f"Ordinance No. {key}", "category": category,
        "app_type": app_type, "project_name": address, "description": desc,
        "applicant": mf.get("applicant"), "parcel_ids": mf.get("ids") or [], "address": address,
        "acres": mf.get("acres"),
        "from_zoning": mf.get("from") if kind == "RZ" else None, "to_zoning": mf.get("to") if kind == "RZ" else None,
        "from_flu": mf.get("from") if kind == "CPA" else None, "to_flu": mf.get("to") if kind == "CPA" else None,
        "filed_date": filed, "status": status, "status_raw": raw, "hearings": hearings,
        "decision_date": decision, "url": latest["url"], "docs_url": latest["url"] if latest["memo"] else None,
        "lat": None, "lon": None, "source": SOURCE,
    }


def status_from(hearings, today):
    past = [h for h in hearings if h["date"] <= today]
    future = [h for h in hearings if h["date"] > today]
    if any(h["result"] == "Withdrawn" for h in hearings):
        h = next(h for h in hearings if h["result"] == "Withdrawn")
        return "Withdrawn", f"{h['body']} {h['date']}: withdrawn", None
    final = [h for h in past if h["result"] in ("Approved", "Denied") and h["body"].startswith(CC)]
    if final:
        h = final[-1]
        return h["result"], f"{h['body']} {h['date']}: {h['result']} (minutes)", h["date"]
    if future:
        return "Hearing Scheduled", f"{future[0]['body']} {future[0]['date']}", None
    if past and past[-1]["result"] == "Continued":
        return "Continued", f"{past[-1]['body']} {past[-1]['date']}: continued", None
    if past:
        lp = past[-1]
        if lp["body"].startswith(CC) and lp["result"] is None:
            return "Hearing Scheduled", f"{lp['body']} {lp['date']} held; outcome not yet in published minutes", None
        return "Filed", f"{lp['body']} {lp['date']}" + (f": {lp['result']}" if lp["result"] else ""), None
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
