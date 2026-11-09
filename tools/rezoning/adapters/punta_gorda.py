#!/usr/bin/env python3
"""City of Punta Gorda (Charlotte County) rezoning / PD / future land use map amendment adapter.

The city website (www.ci.punta-gorda.fl.us / cityofpuntagordafl.com) answers 403 to non-browser
clients, so this adapter reads the city's Granicus meeting archive (puntagorda.granicus.com,
view_id=1), no login:
 * ViewPublisher.php?view_id=1 lists every meeting with links to the agenda (AgendaViewer, a
   text PDF), the minutes (MinutesViewer, text PDF) and the agenda packet.
   Bodies used: Planning Commission (formerly "Planning and Zoning Board") and City Council.
 * Agenda items are numbered "3.a", "4.b" and carry the city case number:
     Z-nn-yyyy   rezoning ("amending the City of Punta Gorda District Map ... from General Single
                 Family-3.5 (GS-3.5) to General Multi-Family-10 (GM-10)"), PD when the target is PD
     CP-nn-yyyy  comprehensive plan amendment; kept only when it changes the Future Land Use Map
     PD-nn-yyyy / PUD-nn-yyyy planned development
   ZA (LDR text amendments), V, SE, SP, TU, VR, SV, GA and plats are skipped. Council items carry
   the Charlotte County parcel numbers (12 digits), acreage and from/to designations and say
   FIRST / SECOND READING.
 * Outcomes come from the minutes: "Councilmember Polk MOVED approval of the proposed ordinance
   ... MOTION CARRIED 3-2" under the item heading.
Granicus answers AgendaViewer / MinutesViewer with a redirect to an S3 bucket whose virtual-host
name (granicus_production_attachments.s3.amazonaws.com) fails TLS hostname checks, so the
redirect is followed in S3 path style instead. Requires pypdf.
Punta Gorda is largely built out; in-scope cases are rare (two in the first half of 2026).
"""
import datetime as dt
import io
import json
import logging
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

try:
    import pypdf
    logging.getLogger("pypdf").setLevel(logging.ERROR)
except ImportError:  # pragma: no cover
    pypdf = None

JURISDICTIONS = ["City of Punta Gorda"]
JUR = "City of Punta Gorda"
GRANICUS = "https://puntagorda.granicus.com"
PUBLISHER = GRANICUS + "/ViewPublisher.php?view_id=1"
SOURCE = "Punta Gorda Granicus (Planning Commission + City Council agendas and minutes)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
LOOKBACK_DAYS = 120
PC, CC = "Planning Commission", "City Council"
CASE_RE = re.compile(r"\b(Z|CP|PD|PUD|ZA)\s?-\s?(\d{2})\s?-\s?(\d{4})\b")
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


def log(*a):
    print("[punta_gorda]", *a, file=sys.stderr, flush=True)


def get(url, tries=3, sleep=0.5):
    """GET following redirects manually (S3 virtual-host names with '_' fail TLS checks)."""
    last = None
    for i in range(tries):
        try:
            u = url
            for _ in range(6):
                if u.startswith("//"):
                    u = "https:" + u
                u = re.sub(r"^https?://([a-z0-9_\-]+)\.s3\.amazonaws\.com/", r"https://s3.amazonaws.com/\1/", u)
                r = _s.get(u, timeout=120, allow_redirects=False)
                if r.is_redirect:
                    loc = r.headers.get("location") or ""
                    u = loc if re.match(r"^(https?:)?//", loc) else requests.compat.urljoin(u, loc)
                    continue
                r.raise_for_status()
                time.sleep(sleep)
                return r
            raise RuntimeError("too many redirects")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


def clean(v):
    v = re.sub(r"\s+", " ", v or "").strip(" .,;:")
    return v or None


def to_float(s):
    try:
        v = float(str(s).replace(",", ""))
        return v if 0 < v < 100000 else None
    except (TypeError, ValueError):
        return None


_pdf_cache = {}


def pdf_text(url):
    if url in _pdf_cache:
        return _pdf_cache[url]
    r = get(url)
    if r.content[:4] != b"%PDF":
        t = BeautifulSoup(r.text, "lxml").get_text("\n")
    else:
        out = []
        for p in pypdf.PdfReader(io.BytesIO(r.content)).pages:
            try:
                out.append(p.extract_text() or "")
            except Exception:  # noqa: BLE001
                out.append("")
        t = "\n".join(out)
    _pdf_cache[url] = t
    return t


def abs_url(h):
    if not h:
        return None
    return "https:" + h if h.startswith("//") else (h if h.startswith("http") else GRANICUS + "/" + h.lstrip("/"))


# ------------------------------------------------------------------ meeting list
def list_meetings(start, end):
    soup = BeautifulSoup(get(PUBLISHER).text, "lxml")
    out, seen = [], set()
    for tr in soup.select("tr.listingRow"):
        tds = tr.find_all("td")
        if len(tds) < 2:
            continue
        name = clean(tds[0].get_text(" ", strip=True)) or ""
        if re.search(r"Workshop|Pre-?Agenda|Budget|Emergency|Joint|Summit|Canal|Utility|Historic", name, re.I):
            continue
        if re.match(r"Planning (Commission|and Zoning Board)", name, re.I):
            body = PC
        elif re.match(r"City Council", name, re.I):
            body = CC
        else:
            continue
        m = re.search(r"([A-Z][a-z]{2})\s+(\d{1,2}),\s*(\d{4})", tds[1].get_text(" ", strip=True))
        if not m:
            continue
        try:
            d = dt.datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%b %d %Y").date().isoformat()
        except ValueError:
            continue
        if not (start <= d <= end):
            continue
        links = [a.get("href") or "" for a in tr.find_all("a")]
        ag = next((abs_url(h) for h in links if "AgendaViewer" in h), None)
        mn = next((abs_url(h) for h in links if "MinutesViewer" in h), None)
        pk = next((h for h in links if h.lower().endswith(".pdf")), None)
        if not ag or (body, d) in seen:
            continue
        seen.add((body, d))
        out.append({"body": body, "date": d, "agenda": ag, "minutes": mn, "packet": pk})
    return sorted(out, key=lambda x: x["date"])


# ------------------------------------------------------------------ parsing
def norm_key(m):
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


def split_items(text):
    """[(item_no, text)] for '3.a' style agenda / minutes items."""
    t = re.sub(r"[ \t]+", " ", text)
    parts = re.split(r"\n\s*(\d{1,2}\.[a-z])\s*\n?", t)
    out = []
    for i in range(1, len(parts) - 1, 2):
        body = re.split(r"\n\s*\d{1,2}\)\s+[A-Z]{3,}|\n\s*\d{1,2}\.\s*\n\s*[A-Z]{3,}", parts[i + 1])[0]
        out.append((parts[i], body))
    return out


def in_scope(key, txt):
    typ = key.split("-")[0]
    t = re.sub(r"\s+", " ", txt)
    if typ == "ZA":
        return bool(re.search(r"District Map|District Designation|rezon", t, re.I)) and \
            not re.search(r"Chapter 26|Land Development Regulations", t[:300], re.I)
    if typ == "CP":
        return bool(re.search(r"Future Land Use Map|FLU change|FLUM|land use designation|current designation", t, re.I)) \
            and not re.search(r"text amendment", t[:300], re.I)
    return True


def parse_item(txt):
    t = re.sub(r"\s+", " ", txt)
    out = {}
    pids = []
    for p in re.findall(r"\b(\d{12})\b", t):
        if p not in pids:
            pids.append(p)
    out["pids"] = pids
    m = re.search(r"approximately\s*([\d,]*\.?\d+)\s*(?:\+/-\s*)?acres", t, re.I) or \
        re.search(r"([\d,]*\.?\d+)\s*(?:\+/-\s*)?acres", t, re.I)
    out["acres"] = to_float(m.group(1)) if m else None
    m = re.search(r"\bfrom\s+(.+?)\s+to\s+(.+?)(?:;|\s+for\s+[\d.]+\s*acres|,\s*of land|\.\s|$)", t, re.I) or \
        re.search(r"designation\s+(?:form|from)\s+(\S+)\s+to\s+(\S+)", t, re.I)
    if m:
        out["from"], out["to"] = clean(m.group(1)), clean(m.group(2))
    m = re.search(r"A request by\s+(.+?)(?:,\s*(?:as\s+)?(?:the\s+)?(?:Authorized Agent|Applicant|property owner)|\s+for\s+a)", t, re.I)
    out["applicant"] = clean(m.group(1)) if m else None
    if not out["applicant"] and re.search(r"City Initiated", t, re.I):
        out["applicant"] = "City of Punta Gorda (city initiated)"
    m = re.search(r"(?:located at|property located at)\s+(\d+ [A-Z][\w .]+?(?:Trail|Avenue|Ave|Street|St|Drive|Dr|Road|Rd|Boulevard|Blvd|Court|Ct|Esplanade|Way|Lane))\b", t)
    out["address"] = clean(m.group(1)) if m else None
    m = re.search(r"-\s*([A-Z][\w .']{2,40}?)\s*-\s*(?:City Initiated|FLUM|Zoning|Amendment)", t)
    out["project"] = clean(m.group(1)) if m else None
    if not out["project"]:
        m = re.search(r"\d{4}\s*-\s*([A-Z][A-Za-z .']{2,40}?)\s+(?:FLU change|FLUM|Zoning|Rezoning)", t)
        out["project"] = clean(m.group(1)) if m else None
    rd = re.search(r"\b(FIRST|SECOND|FINAL) READING", t, re.I)
    out["reading"] = (rd.group(1).lower().replace("final", "second")) if rd else None
    return out


def minutes_results(text, body):
    res = {}
    for no, txt in split_items(text):
        keys = [norm_key(m) for m in CASE_RE.finditer(txt[:400])]
        if not keys:
            continue
        flat = re.sub(r"\s+", " ", txt)
        result = None
        for m in re.finditer(r"\bMOVED\b", flat):
            tail = flat[m.end():m.end() + 1500]
            mm = re.search(r"MOTION\s+(?:UNANIMOUSLY\s+)?(CARRIED|FAILED)", tail)
            if not mm:
                continue
            verb = re.split(r"SECONDED", tail[:mm.start()])[0][:400].lower()
            if re.search(r"close the public hearing|open the public hearing|approv\w* the consent", verb):
                continue
            ok = mm.group(1).upper() == "CARRIED"
            if re.search(r"continu|table|postpone|defer", verb):
                r = "Continued" if ok else None
            elif re.search(r"\bden", verb):
                r = ("Recommended Denial" if body == PC else "Denied") if ok else None
            elif re.search(r"approv|adopt|recommend|transmit", verb):
                if body == PC:
                    r = "Recommended Approval" if ok else "Motion to recommend approval failed"
                elif re.search(r"transmit", verb):
                    r = "Transmitted" if ok else None
                else:
                    r = "APPROVE" if ok else "Denied"
            else:
                r = None
            if r:
                result = r
        if result is None and re.search(r"withdr[ae]wn|withdrew", flat, re.I):
            result = "Withdrawn"
        if result:
            for k in keys:
                res[k] = result
    return res


# ------------------------------------------------------------------ fetch
def fetch(since, refresh):
    if pypdf is None:
        raise RuntimeError("pypdf is required for the Punta Gorda adapter")
    today = dt.date.today().isoformat()
    refresh = [re.sub(r"\s", "", r.upper()) for r in (refresh or []) if r and r.strip()]
    start = (dt.date.fromisoformat(since) - dt.timedelta(days=LOOKBACK_DAYS)).isoformat()
    for r in refresh:
        m = re.search(r"-(\d{4})$", r)
        if m:
            start = min(start, max(f"{int(m.group(1)) - 1}-10-01", (dt.date.today() - dt.timedelta(days=730)).isoformat()))
    end = (dt.date.today() + dt.timedelta(days=75)).isoformat()
    try:
        meetings = list_meetings(start, end)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Punta Gorda Granicus unreachable: {e}") from e
    log(f"{len(meetings)} Planning Commission / City Council meetings {start}..{end}")

    cases, outcomes = {}, {}
    for mt in meetings:
        try:
            txt = pdf_text(mt["agenda"])
        except Exception as e:  # noqa: BLE001
            log(f"agenda {mt['body']} {mt['date']}: {e}")
            continue
        for no, it in split_items(txt):
            keys = [norm_key(m) for m in CASE_RE.finditer(it[:300])]
            for k in dict.fromkeys(keys):
                if not in_scope(k, it):
                    continue
                cases.setdefault(k, []).append({"body": mt["body"], "date": mt["date"], "text": it,
                                                "url": mt["agenda"], "packet": mt["packet"], "item": no})
        if mt["minutes"] and mt["date"] <= today:
            try:
                for k, r in minutes_results(pdf_text(mt["minutes"]), mt["body"]).items():
                    outcomes[(k, mt["body"], mt["date"])] = r
            except Exception as e:  # noqa: BLE001
                log(f"minutes {mt['body']} {mt['date']}: {e}")
    log(f"{len(cases)} in-scope cases on agendas")

    out = []
    for k, aps in sorted(cases.items()):
        first = min(a["date"] for a in aps)
        if first < since and k not in refresh:
            continue
        try:
            out.append(build(k, aps, outcomes, today))
        except Exception as e:  # noqa: BLE001
            log(f"{k}: skipped ({type(e).__name__}: {e})")
    # a CP case's agenda text rarely lists parcels; borrow them from the companion rezoning
    # (same hearing dates and acreage)
    for r in out:
        if r["parcel_ids"]:
            continue
        sig = ({h["date"] for h in r["hearings"]}, r["acres"])
        for o in out:
            if o is not r and o["parcel_ids"] and ({h["date"] for h in o["hearings"]}, o["acres"]) == sig:
                r["parcel_ids"] = list(o["parcel_ids"])
                r["project_name"] = r["project_name"] or o["project_name"]
                r["applicant"] = r["applicant"] or o["applicant"]
                break
    for r in set(refresh) - set(cases):
        log(f"refresh id {r} not found on agendas since {start}")
    return out


def build(key, aps, outcomes, today):
    aps = sorted(aps, key=lambda a: (a["date"], a["body"] != PC))
    infos = [parse_item(a["text"]) for a in aps]
    best = max(infos, key=lambda x: (len(x["pids"]), bool(x.get("from")), x["acres"] is not None))
    pids = []
    for x in infos:
        for p in x["pids"]:
            if p not in pids:
                pids.append(p)
    hmap = {}
    for a, x in zip(aps, infos):
        rd = x["reading"] if a["body"] == CC else None
        r = outcomes.get((key, a["body"], a["date"]))
        if r == "APPROVE":
            r = "Approved on first reading" if rd == "first" else "Approved"
        body = f"City Council ({rd} reading)" if rd else a["body"]
        h = hmap.setdefault((a["body"], a["date"]), {"body": body, "date": a["date"], "result": None})
        h["result"] = h["result"] or r
    hearings = sorted(hmap.values(), key=lambda h: (h["date"], not h["body"].startswith("Planning")))
    past = [h for h in hearings if h["date"] <= today]
    future = [h for h in hearings if h["date"] > today]
    decision = None
    if any(h["result"] == "Withdrawn" for h in hearings):
        status, raw = "Withdrawn", "Withdrawn (minutes)"
    elif past and past[-1]["body"].startswith(CC) and past[-1]["result"] in ("Approved", "Denied") and not future:
        status, decision = past[-1]["result"], past[-1]["date"]
        raw = f"{past[-1]['body']} {decision}: {past[-1]['result']} (minutes)"
    elif past and past[-1]["result"] == "Continued":
        status, raw = "Continued", f"{past[-1]['body']} {past[-1]['date']}: continued"
        if future:
            raw += f"; next {future[0]['body']} {future[0]['date']}"
    elif future:
        status, raw = "Hearing Scheduled", f"next {future[0]['body']} {future[0]['date']}"
        if past and past[-1]["result"]:
            raw = f"{past[-1]['body']} {past[-1]['date']}: {past[-1]['result']}; " + raw
    elif past and past[-1]["result"] is None:
        status, raw = "Hearing Scheduled", f"{past[-1]['body']} {past[-1]['date']} held; outcome not yet in published minutes"
    elif past:
        status = "Filed"
        raw = f"{past[-1]['body']} {past[-1]['date']}: {past[-1]['result']}; next hearing not yet on an agenda"
    else:
        status, raw = "Filed", "On agenda"

    typ = key.split("-")[0]
    frm, to = best.get("from"), best.get("to")
    if typ == "CP":
        category, app_type = "Comp Plan Amendment", "Comprehensive Plan Future Land Use Map Amendment (CP)"
    elif typ in ("PD", "PUD") or re.search(r"\bPD\b|\bPUD\b|Planned (?:Unit )?Development", to or ""):
        category, app_type = "PD", ("Planned Development (PD)" if typ in ("PD", "PUD") else "Rezoning (Z) to PD")
    else:
        category, app_type = "Rezoning", "Rezoning / District Map Amendment (" + typ + ")"
    main = max(aps, key=lambda a: len(a["text"]))
    desc = clean(re.sub(r"^\s*(?:QUASI-JUDICIAL|LEGISLATIVE)\s*:\s*", "", re.sub(r"\s+", " ", main["text"])))
    desc = re.split(r"\s+Exhibits:", desc or "")[0] or None
    if desc and len(desc) > 400:
        desc = desc[:397] + "..."
    project = next((x["project"] for x in infos if x.get("project")), None)
    applicant = next((x["applicant"] for x in infos if x.get("applicant")), None)
    return {
        "jurisdiction": JUR, "county": "Charlotte", "app_num": key, "category": category, "app_type": app_type,
        "project_name": project, "description": desc, "applicant": applicant,
        "parcel_ids": pids, "address": best.get("address"), "acres": best.get("acres"),
        "from_zoning": None if category == "Comp Plan Amendment" else frm,
        "to_zoning": None if category == "Comp Plan Amendment" else to,
        "from_flu": frm if category == "Comp Plan Amendment" else None,
        "to_flu": to if category == "Comp Plan Amendment" else None,
        "filed_date": aps[0]["date"], "status": status, "status_raw": raw, "hearings": hearings,
        "decision_date": decision, "url": aps[-1]["url"], "docs_url": aps[-1]["packet"],
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
