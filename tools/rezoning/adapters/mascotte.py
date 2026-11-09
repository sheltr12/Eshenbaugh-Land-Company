#!/usr/bin/env python3
"""City of Mascotte (Lake County) rezoning / PUD / future land use map amendment adapter.

Source: the city's CivicWeb (iCompass) portal, cityofmascotte.civicweb.net. Mascotte has no planning
board of its own: the City Council sits as the Local Planning Agency (LPA) inside its regular meeting,
then holds first and final readings.
 * Agendas: Document Center folders "Agendas / Council / <year>" hold an HTML agenda per meeting
   ("Council - 06 Oct 2026 - Agenda - Html"). Each agenda item has a title ("Tuscan Trails ZMA",
   "Final Reading of Ordinance 2024-03-648 Paragon Pines Future Land Use Amendment"), a recommended
   motion naming the ordinance ("... Ordinance 2026-10-672 amending the Zoning Map ... to change 73.71
   acres from Lake County Agricultural to Urban Estates") and an "Agenda Item Report - Pdf" link.
 * app_num = "Ordinance YYYY-MM-NNN" (one ordinance per action; a project usually has ANX + CPA + ZMA
   ordinances, the annexation is skipped). Kept: CPA / LSCPA / SSCPA / Future Land Use amendments and
   ZMA / rezoning / PUD zoning. Skipped: annexation, CDD, EAR / text amendments, development agreements,
   construction plans.
 * The Agenda Item Report PDF (first pages) is the planning staff report: city case number
   ("2026-07-240 - ZMA - Tuscan Trails (AltKey 1068517, 1069645, 3702697)"), applicant, owner,
   location, acreage, existing / proposed FLU and zoning, and a hearing history list.
   filed_date = first day of the month in the city case number (YYYY-MM-NNN) when it is earlier than the
   first agenda appearance.
 * Outcomes: Council minutes PDFs (Document Center "Minutes / Council / <year>"): "Motion passed 4 to 0"
   under the ordinance item, split into the LPA and the Council parts of the meeting.
Requires pypdf.
"""
import datetime as dt
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

JURISDICTIONS = ["City of Mascotte"]
JUR = "City of Mascotte"
COUNTY = "Lake"
BASE = "https://cityofmascotte.civicweb.net"
AGENDA_ROOT = "/filepro/documents/1021"     # Agendas / Council
MINUTES_ROOT = "/filepro/documents/3998"    # Minutes / Council
SOURCE = "Mascotte CivicWeb (City Council / LPA agendas, item reports and minutes)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
LOOKBACK_DAYS = 150
LPA = "Local Planning Agency"
CC = "City Council"

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


def log(*a):
    print("[mascotte]", *a, file=sys.stderr)


def get(url, tries=5, sleep=0.5, **kw):
    if url.startswith("/"):
        url = BASE + url
    last = None
    for i in range(tries):
        try:
            r = _s.get(url, timeout=120, **kw)
            r.raise_for_status()
            time.sleep(sleep)
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


def sq(s):
    return re.sub(r"\s+", " ", (s or "").replace(" ", " ")).strip()


def pdf_text(content, max_pages=None):
    rdr = pypdf.PdfReader(io.BytesIO(content))
    out = []
    for p in rdr.pages[:max_pages] if max_pages else rdr.pages:
        try:
            out.append(p.extract_text() or "")
        except Exception:  # noqa: BLE001
            out.append("")
    return "\n".join(out)


def list_folder(path):
    s = BeautifulSoup(get(path).text, "lxml")
    out = []
    for a in s.find_all("a", href=True):
        if re.search(r"/(?:filepro/documents|document)/\d+", a["href"]):
            out.append((a["href"].split("?")[0] if "/filepro/" in a["href"] else a["href"], sq(a.get_text(" "))))
    return out


DOC_RE = re.compile(r"^(Council|Special(?:\s+Meeting)?)\s*-\s*(\d{1,2}\s+\w{3}\s+\d{4})\s*-\s*(Agenda|Minutes)\s*-\s*(\w+)", re.I)


def docs_for_years(root, years):
    out = []
    for href, title in list_folder(root):
        if title.strip() in years:
            for h, t in list_folder(href):
                m = DOC_RE.match(t)
                if m:
                    try:
                        d = dt.datetime.strptime(m.group(2), "%d %b %Y").date().isoformat()
                    except ValueError:
                        continue
                    out.append({"href": h, "date": d, "kind": m.group(3).title(), "fmt": m.group(4).title()})
    return out


# ------------------------------------------------------------------ parsing
ORD_RE = re.compile(r"\bOrdinance\s*(?:No\.?\s*)?(\d{4})\s*-\s*(\d{2})\s*-\s*(\d{3})\b", re.I)
CPA_RE = re.compile(r"\b(?:L|S)?S?CPA\b|Comprehensive\s+Plan\s+Amendment|Future\s+Land\s+Use|\bFLUM?\b", re.I)
RZ_RE = re.compile(r"\bZMA\b|Zoning\s+Map\s+Amendment|Rezon|\bPUD\s+(?:Zoning|Rezoning)|Planned\s+Unit\s+Development\s+Zoning",
                   re.I)
OUT_RE = re.compile(r"\bANX\b|Annex|\bCDD\b|Community\s+Development\s+District|\bEAR\b|Evaluation\s+and\s+Appraisal|"
                    r"Text\s+Amendment|Development\s+Agreement(?!.*Zoning)|Construction\s+Plan|Recovery\s+Residence|"
                    r"Irrigation|Food\s+Truck|Code\b", re.I)


def okey(m):
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


def classify(title, rec):
    """(key, kind) for in-scope items, else None."""
    t = sq(title)
    if OUT_RE.search(t):
        return None
    kind = "CPA" if CPA_RE.search(t) else ("RZ" if RZ_RE.search(t) else None)
    if not kind:
        return None
    m = ORD_RE.search(t) or ORD_RE.search(rec or "")
    if not m:
        m = re.search(r"\b(\d{4})\s*-\s*(\d{2})\s*-\s*(\d{3})\b", rec or "")
        if not m:
            return None
    return okey(m), kind


def parse_agenda(html):
    """[(section, title, recommendation, report_href)] in document order."""
    s = BeautifulSoup(html, "lxml")
    out, sec = [], ""
    for tb in s.find_all("table"):
        tds = tb.find_all("td", recursive=True)
        txt = [sq(td.get_text(" ")) for td in tds]
        link = tb.find("a", title=re.compile(r"ItemAttachment"))
        if len(tds) >= 3 and (link or re.match(r"\d+\.$", txt[1])):
            paras = [sq(p.get_text(" ")) for p in tds[2].find_all("p")]
            paras = [p for p in paras if p and not re.match(r"Agenda Item Report", p)]
            title = paras[0] if paras else txt[2]
            rec = " ".join(paras[1:])
            out.append((sec, title, rec, link["href"] if link else None))
        else:
            h = sq(" ".join(txt))
            if h and len(h) < 120:
                sec = h
    return out


def staff_fields(text):
    t = text.replace("\u2019", "'").replace("\u2013", "-").replace("\u2014", "-")
    out = {}
    m = re.search(r"\b(20\d\d-\d{2}-\d{3})\s*-\s*(?:[A-Za-z' ]+?\s*-\s*)?([A-Z]{2,6})?\b", t)
    m2 = re.search(r"PLANNING STAFF REPORT\s*\n?\s*(20\d\d-\d{2}-\d{3})", t)
    if m2:
        out["case"] = m2.group(1)
    elif m:
        out["case"] = m.group(1)
    keys = []
    for km in re.finditer(r"Alt\s*Keys?\s*#?:?\s*([\d,\s&and]+)", t, re.I):
        keys += re.findall(r"\b\d{7}\b", km.group(1))
    keys += re.findall(r"\b\d{2}-\d{2}-\d{2}-\d{4}-\d{3}-\d{5}\b", t)
    out["ids"] = list(dict.fromkeys(keys))
    for lab, key in (("Applicant", "applicant"), ("Owner\\(s\\)", "owner"), ("Site Location", "location"),
                     ("Acreage", "acreage"), ("Exist'g FLU/Zoning", "ex_both"), ("Prop'd FLU/Zoning", "pr_both"),
                     ("Exist'g FLU", "ex_flu"), ("Prop'd FLU", "pr_flu"), ("Exist'g Zoning", "ex_z"),
                     ("Prop'd Zoning", "pr_z")):
        lm = re.search(r"^\s*" + lab + r"\s*:\s*(.+)$", t, re.M)
        if lm and key not in out:
            out[key] = sq(lm.group(1))
    if out.get("acreage"):
        am = re.search(r"([\d,]*\.?\d+)", out["acreage"])
        if am:
            try:
                out["acres"] = float(am.group(1).replace(",", ""))
            except ValueError:
                pass
    for both, a, b in (("ex_both", "ex_flu", "ex_z"), ("pr_both", "pr_flu", "pr_z")):
        if out.get(both) and "/" in out[both]:
            x, y = out[both].split("/", 1)
            out.setdefault(a, sq(x))
            out.setdefault(b, sq(y))
    hist = []
    for hm in re.finditer(r"(?:\(([^)]{3,40})\)\s*)?(LPA|City Council)\s*\((?:CPA|ZMA|LSCPA|SSCPA|PUD|ANX)\),\s*"
                          r"(\w+ \d{1,2},? \d{4})", t):
        try:
            d = dt.datetime.strptime(hm.group(3).replace(",", ""), "%B %d %Y").date().isoformat()
        except ValueError:
            continue
        hist.append({"note": hm.group(1), "body": LPA if hm.group(2) == "LPA" else CC, "date": d})
    out["history"] = hist
    rm = re.search(r"^\s*Request\s*:\s*(.+?)(?:\n\s*\n|\n\s*(?:Applicant|Owner)\b)", t, re.M | re.S)
    if rm and len(sq(rm.group(1))) > 30:
        out["request"] = sq(rm.group(1))
    out["large"] = bool(re.search(r"\bLSCPA\b|Large[- ]Scale", t[:4000], re.I))
    return out


def rec_fields(rec):
    out = {}
    m = re.search(r"change\s+([\d,]*\.?\d+)\s*acres", rec, re.I)
    if m:
        out["acres"] = float(m.group(1).replace(",", ""))
    m = re.search(r"acres\s+from\s+(.+?),?\s+to\s+(.+?)(?:,\s*and\b|\s+and\s+direct|\.\s|\.$|$)", rec, re.I)
    if m:
        out["from"], out["to"] = sq(m.group(1)).strip(" ,"), sq(m.group(2)).strip(" ,")
    return out


def minutes_results(text):
    """{(body, key): result} from a Council minutes PDF (LPA part and Council part)."""
    d = sq(text)
    res = {}
    lpa_s = re.search(r"OPEN\s+(?:THE\s+)?LOCAL\s+PLANNING\s+AGENCY", d, re.I)
    lpa_e = re.search(r"CLOSE\s+(?:THE\s+)?LOCAL\s+PLANNING\s+AGENCY", d, re.I)
    parts = []
    if lpa_s and lpa_e and lpa_e.start() > lpa_s.start():
        parts = [(LPA, d[lpa_s.end(): lpa_e.start()]), (CC, d[: lpa_s.start()] + " " + d[lpa_e.end():])]
    else:
        parts = [(CC, d)]
    for body, seg in parts:
        hits = list(ORD_RE.finditer(seg)) + list(re.finditer(r"\b(20\d\d)\s*-\s*(\d{2})\s*-\s*(\d{3})\b", seg))
        hits.sort(key=lambda m: m.start())
        for m in hits:
            key = okey(m)
            if (body, key) in res:
                continue
            tail = seg[m.end(): m.end() + 6000]
            nxt = re.search(r"\s\d{1,2}\.\s+(?:First|Final|Second)?\s*(?:Reading\s+of\s+)?[A-Z]", tail)
            stop = nxt.start() if nxt and nxt.start() > 200 else len(tail)
            o = re.search(r"Motion\s+(passed|carried|failed)", tail[:stop], re.I)
            if not o:
                if re.search(r"\b(?:withdrawn|withdrew)\b", tail[:min(stop, 800)], re.I):
                    res[(body, key)] = "Withdrawn"
                continue
            before = tail[max(0, o.start() - 600): o.start()]
            if o.group(1).lower() == "failed":
                continue
            if re.search(r"\b(?:continue|table|postpone|defer)\w*", before, re.I):
                res[(body, key)] = "Continued"
            elif re.search(r"\bden(?:y|ial|ied)\b", before, re.I) and not re.search(r"approve", before, re.I):
                res[(body, key)] = "Denied"
            else:
                res[(body, key)] = "Approved"
    return res


# ------------------------------------------------------------------ fetch
def fetch(since, refresh):
    if pypdf is None:
        raise RuntimeError("pypdf is required for the Mascotte adapter")
    today = dt.date.today()
    refresh_ids = {}
    for r in refresh or []:
        m = re.search(r"(\d{4})\s*-\s*(\d{2})\s*-\s*(\d{3})", r or "")
        if m:
            refresh_ids[okey(m)] = r
    start_d = dt.date.fromisoformat(since) - dt.timedelta(days=LOOKBACK_DAYS)
    if refresh_ids:
        start_d = min(start_d, today - dt.timedelta(days=730))
    years = {str(y) for y in range(start_d.year, today.year + 2)}
    agendas = [d for d in docs_for_years(AGENDA_ROOT, years) if d["kind"] == "Agenda" and d["fmt"] == "Html"
               and d["date"] >= start_d.isoformat()]
    if not agendas:
        raise RuntimeError("no Mascotte agendas found on CivicWeb")
    minutes = {}
    for d in docs_for_years(MINUTES_ROOT, years):
        if d["kind"] == "Minutes" and d["fmt"] in ("Pdf", "Adopted"):
            if d["fmt"] == "Pdf" or d["date"] not in minutes:
                minutes[d["date"]] = d["href"]
    log(f"{len(agendas)} agendas since {start_d}; {len(minutes)} minutes")

    cases = {}
    for ag in sorted(agendas, key=lambda x: x["date"]):
        try:
            items = parse_agenda(get(ag["href"]).text)
        except Exception as e:  # noqa: BLE001
            log(f"agenda {ag['date']}: {e}")
            continue
        for sec, title, rec, rep in items:
            c = classify(title, rec)
            if not c:
                continue
            key, kind = c
            body = LPA if re.search(r"LOCAL\s+PLANNING\s+AGENCY", sec, re.I) or re.search(r"\bThe LPA\b|finding of Ordinance",
                                                                                         rec, re.I) else CC
            reading = None
            if body == CC:
                rm = re.search(r"\b(First|Final|Second)\s+Reading", title + " " + rec, re.I)
                reading = rm.group(1).lower() if rm else ("final" if re.search(r"\badoption\b", rec, re.I) else
                                                          ("first" if re.search(r"first reading", rec, re.I) else None))
                if reading == "second":
                    reading = "final"
            cases.setdefault(key, {"kind": kind, "apps": []})["apps"].append(
                {"date": ag["date"], "body": body, "reading": reading, "title": title, "rec": rec,
                 "report": rep, "agenda": ag["href"]})
    log(f"{len(cases)} in-scope ordinances")

    results = {}
    for d in sorted({a["date"] for c in cases.values() for a in c["apps"]}):
        if d in minutes:
            try:
                results[d] = minutes_results(pdf_text(get(minutes[d]).content))
            except Exception as e:  # noqa: BLE001
                log(f"minutes {d}: {e}")

    out = []
    for key, c in sorted(cases.items()):
        try:
            rec = build(key, c, results, today.isoformat())
        except Exception as e:  # noqa: BLE001
            log(f"Ordinance {key}: skipped ({e})")
            continue
        if rec["filed_date"] < since and key not in refresh_ids:
            continue
        out.append(rec)
    for r in set(refresh_ids) - set(cases):
        log(f"refresh id {refresh_ids[r]} not found on any agenda since {start_d}")
    return out


def build(key, c, results, today):
    apps = sorted(c["apps"], key=lambda a: (a["date"], a["body"] == CC))
    latest = apps[-1]
    kind = c["kind"]
    staff = {}
    for a in reversed(apps):
        if not a["report"]:
            continue
        try:
            staff = staff_fields(pdf_text(get(a["report"]).content, 6))
            break
        except Exception as e:  # noqa: BLE001
            log(f"{key}: item report: {e}")
    rf = {}
    for a in reversed(apps):
        for k2, v in rec_fields(a["rec"]).items():
            rf.setdefault(k2, v)
    if kind == "CPA":
        f_, t_ = staff.get("ex_flu") or rf.get("from"), staff.get("pr_flu") or rf.get("to")
    else:
        f_, t_ = staff.get("ex_z") or rf.get("from"), staff.get("pr_z") or rf.get("to")
    acres = rf.get("acres") or staff.get("acres")
    pname = re.sub(r"^(?:First|Final|Second)\s+Reading\s+of\s+", "", latest["title"], flags=re.I)
    pname = re.sub(r"Ordinance\s+\d{4}-\d{2}-\d{3}\s*", "", pname)
    pname = sq(re.sub(r"\b(?:L|S)?S?CPA\b|\bZMA\b|Comprehensive\s+Plan\s+Amendment|Future\s+Land\s+Use\s+(?:Map\s+)?Amendment|"
                      r"Zoning\s+Map\s+Amendment(?:\s+and\s+Development\s+Agreement)?|\(.*?\)", "", pname, flags=re.I)) or None
    is_pd = bool(re.search(r"\bPUD\b|Planned\s+Unit", (t_ or "") + " " + latest["title"], re.I))
    category = "Comp Plan Amendment" if kind == "CPA" else ("PD" if is_pd else "Rezoning")
    alltxt = " ".join(a["title"] + " " + a["rec"] for a in apps)
    if kind == "CPA":
        app_type = "LSCPA (Large-Scale FLUM Amendment)" if (staff.get("large") or
                                                            re.search(r"Large|LSCPA|Transmit", alltxt, re.I)) else \
            "SSCPA (Small-Scale FLUM Amendment)"
    else:
        app_type = "Zoning Map Amendment (PUD)" if is_pd else "Zoning Map Amendment (ZMA)"

    # hearings: agenda appearances + staff report history (older hearings), results from minutes
    hs = {}
    for h in staff.get("history", []):
        note = (h["note"] or "").lower()
        rd = "first" if "1st" in note else ("final" if re.search(r"2nd|final", note) else None)
        res = ("Pulled from agenda" if "withdrawn" in note else
               ("Continued" if re.search(r"postpone|continu|table", note) else None))
        hs[(h["date"], h["body"])] = {"body": h["body"], "date": h["date"], "reading": rd if h["body"] == CC else None,
                                      "result": res}
    for a in apps:
        k2 = (a["date"], a["body"])
        prev = hs.get(k2, {})
        res = results.get(a["date"], {}).get((a["body"], key))
        hs[k2] = {"body": a["body"], "date": a["date"], "reading": a["reading"] or prev.get("reading"),
                  "result": res or prev.get("result")}
    seq = sorted(hs.values(), key=lambda h: (h["date"], h["body"] == CC))
    hearings = []
    for i, h in enumerate(seq):
        res = h["result"]
        later = seq[i + 1:]
        if res is None and h["body"] == CC and h["reading"] == "first" and \
                any(b["body"] == CC and b["reading"] == "final" for b in later):
            res = "Approved"
        if res is None and h["body"] == LPA and any(b["body"] == CC for b in later) and h["date"] <= today:
            res = None
        if h["body"] == CC and h["reading"] == "first" and res == "Approved":
            res = "Approved (first reading)" if not re.search(r"Transmit", alltxt, re.I) else "Transmitted"
        if h["body"] == LPA and res in ("Approved", "Denied"):
            res = "Recommended " + ("Approval" if res == "Approved" else "Denial")
        body = h["body"] + (f" ({h['reading']} reading)" if h["reading"] else "")
        e = {"body": body, "date": h["date"], "result": res}
        if e not in hearings:
            hearings.append(e)
    status, raw, decision = status_from(hearings, today)
    first_agenda = min(a["date"] for a in apps)
    filed = min([first_agenda] + [h["date"] for h in hearings])
    cm = re.match(r"(20\d\d)-(\d{2})-", staff.get("case") or "")
    om = re.match(r"(20\d\d)-(\d{2})-", key)
    for mm in (cm, om):
        if mm and 1 <= int(mm.group(2)) <= 12:
            filed = min(filed, f"{mm.group(1)}-{mm.group(2)}-01")
    desc = staff.get("request") or next((a["rec"] for a in reversed(apps) if re.search(r"amending", a["rec"], re.I)),
                                        None) or latest["rec"] or latest["title"]
    desc = re.sub(r"^Motion\s+to\s+\([^)]*\)\s*(?:the\s+)?", "", desc, flags=re.I)
    desc = desc[:1].upper() + desc[1:] if desc else desc
    if staff.get("case"):
        desc = f"{desc} (city case {staff['case']})"
    if len(desc) > 300:
        desc = desc[:297] + "..."
    return {
        "jurisdiction": JUR, "county": COUNTY, "app_num": f"Ordinance {key}", "category": category,
        "app_type": app_type, "project_name": pname, "description": desc or None,
        "applicant": staff.get("applicant") or staff.get("owner"), "parcel_ids": staff.get("ids") or [],
        "address": staff.get("location"), "acres": acres,
        "from_zoning": f_ if kind == "RZ" else None, "to_zoning": t_ if kind == "RZ" else None,
        "from_flu": f_ if kind == "CPA" else None, "to_flu": t_ if kind == "CPA" else None,
        "filed_date": filed, "status": status, "status_raw": raw, "hearings": hearings,
        "decision_date": decision, "url": BASE + latest["agenda"].split("?")[0],
        "docs_url": BASE + latest["report"] if latest["report"] else None,
        "lat": None, "lon": None, "source": SOURCE,
    }


def status_from(hearings, today):
    past = [h for h in hearings if h["date"] <= today]
    future = [h for h in hearings if h["date"] > today]
    if past and past[-1]["result"] == "Withdrawn":
        return "Withdrawn", f"{past[-1]['body']} {past[-1]['date']}: withdrawn", None
    final = [h for h in past if h["result"] in ("Approved", "Denied") and h["body"].startswith(CC)]
    if final:
        h = final[-1]
        return h["result"], f"{h['body']} {h['date']}: {h['result']} (minutes)", h["date"]
    if future:
        return "Hearing Scheduled", f"{future[0]['body']} {future[0]['date']}", None
    if past and past[-1]["result"] == "Continued":
        return "Continued", f"{past[-1]['body']} {past[-1]['date']}: continued/postponed", None
    if past:
        lp = past[-1]
        if lp["body"].startswith(CC) and lp["result"] is None:
            return "Hearing Scheduled", f"{lp['body']} {lp['date']} held; outcome not yet in published minutes", None
        return "Filed", f"{lp['body']} {lp['date']}" + (f": {lp['result']}" if lp["result"] else "") + \
            "; next hearing not yet on an agenda", None
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
