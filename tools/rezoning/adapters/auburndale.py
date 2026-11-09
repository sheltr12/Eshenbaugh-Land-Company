#!/usr/bin/env python3
"""City of Auburndale (Polk County) rezoning / PD / future land use map amendment adapter.

Source: City Commission agenda packets and minutes PDFs the city uploads to its WordPress site
(auburndalefl.com). They are found through the public WordPress media API
(https://auburndalefl.com/wp-json/wp/v2/media?after=...&mime_type=application/pdf), file names like
"City-of-Auburndale-City-Commission-Meeting-Agenda-7-20-2026.pdf" and
"20260803-Commission-Minutes.pdf" / "Commission-Minutes-7-20-2026.pdf".
 * The agenda packet lists the items ("2. ORDINANCE #1851- AMENDING THE OFFICIAL ZONING MAP -
   SUNTRAX") and has a staff memo per item: petitioner / owner, location, current and proposed
   future land use and zoning, acreage, the Planning Commission recommendation with vote and date
   ("(5-0, 7-7-26)") and the date of the second and final reading.
 * Minutes give the outcome ("Motion ... to approve Ordinance #1851 ... on second and final
   reading ... Upon vote, all ayes.").
app_num = "Ordinance #1851". Future land use map amendments -> Comp Plan Amendment, zoning map
amendments -> Rezoning (PD when the new district is a planned development). Comprehensive plan
text / EAR ordinances, city code and LDR text changes, annexations, plats and vacations are
skipped. Planning Commission agendas are not posted (only some minutes, months later), so a case
first appears with its City Commission hearing; the Planning Commission date comes from the memo.
Parcel ids are not published in these documents. Requires pypdf.
"""
import datetime as dt
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

JURISDICTIONS = ["City of Auburndale"]
JUR = "City of Auburndale"
BASE = "https://auburndalefl.com"
MEDIA = BASE + "/wp-json/wp/v2/media"
SOURCE = "Auburndale City Commission agenda packets and minutes (auburndalefl.com)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
LOOKBACK_DAYS = 150
PC = "Planning Commission"
CC = "City Commission"

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


def log(*a):
    print("[auburndale]", *a, file=sys.stderr)


def get(url, params=None, tries=4, sleep=0.4):
    last = None
    for i in range(tries):
        try:
            r = _s.get(url, params=params, timeout=120)
            if r.status_code == 400 and params and params.get("page", 1) > 1:
                return None          # past the last page
            r.raise_for_status()
            time.sleep(sleep)
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


def sq(s):
    s = re.sub(r"\s+", " ", (s or "").replace("\u00a0", " ")).strip()
    return s or None


def nd(s):
    return (s or "").replace("\u2013", "-").replace("\u2014", "-").replace("\u2019", "'")


def pdf_text(url):
    rdr = pypdf.PdfReader(io.BytesIO(get(url).content))
    out = []
    for p in rdr.pages:
        try:
            out.append(p.extract_text() or "")
        except Exception:  # noqa: BLE001
            out.append("")
    return nd("\n".join(out))


def file_date(name):
    m = re.search(r"(?<!\d)(\d{1,2})[-.](\d{1,2})[-.](20\d{2})(?!\d)", name)
    if m:
        try:
            return dt.date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
        except ValueError:
            return None
    m = re.search(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)", name)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return None
    return None


def list_docs(start):
    """{'agendas': [(date, url)], 'minutes': [(date, url)]} for City Commission meetings since start."""
    ag, mn = {}, {}
    after = (dt.date.fromisoformat(start) - dt.timedelta(days=20)).isoformat()
    for page in range(1, 30):
        r = get(MEDIA, params={"after": f"{after}T00:00:00", "per_page": 100, "page": page, "orderby": "date",
                               "order": "asc", "mime_type": "application/pdf", "_fields": "date,source_url"})
        if r is None:
            break
        rows = r.json()
        if not rows:
            break
        for m in rows:
            url = m.get("source_url") or ""
            name = url.rsplit("/", 1)[-1]
            if re.search(r"Workshop|CRA|Special-Workshop|Budget", name, re.I):
                continue
            d = file_date(name)
            if not d or d < start:
                continue
            if re.search(r"Commission-Meeting-Agenda", name, re.I):
                ag[d] = url               # later uploads (amended agendas) replace earlier ones
            elif re.search(r"Commission-Minutes|CC-Minutes", name, re.I) and not re.search(r"Planning", name, re.I):
                mn[d] = url
        if len(rows) < 100:
            break
    return {"agendas": sorted(ag.items()), "minutes": sorted(mn.items())}


# ------------------------------------------------------------------ parsing
ORD_RE = re.compile(r"ORDINANCE\s*#\s*(\d{4})", re.I)
IN_RE = re.compile(r"FUTURE LAND USE MAP|ZONING MAP|ZONIG MAP|REZON|PLANNED (?:UNIT )?DEVELOPMENT|\bPUD\b", re.I)
OUT_RE = re.compile(r"EVALUATION AND APPRAISAL|CITY CODE|LAND DEVELOPMENT REGULATIONS|TEXT|ANNEX|BUDGET|MILLAGE|RATES", re.I)


def agenda_list(text):
    """[(ord_no, title)] for in-scope ordinances in the numbered agenda list on page 1."""
    head = text.split("AGENDA ITEM")[0]
    out = []
    for m in re.finditer(r"\n\s*\d{1,2}\.\s*(ORDINANCE\s*#\s*\d{4}[^\n]*(?:\n(?!\s*\d{1,2}\.)[^\n]*){0,2})", head):
        title = sq(m.group(1)) or ""
        title = re.split(r"\s+(?:P\.O\. Box|F\.S\. 286|\(863\))", title)[0]
        om = ORD_RE.search(title)
        if not om or not IN_RE.search(title) or OUT_RE.search(title):
            continue
        out.append((om.group(1), title))
    return out


def memo_for(text, ono):
    """Staff memo text of the agenda item for ordinance ono (from its AGENDA ITEM header to the next)."""
    parts = re.split(r"(?=City Commission Meeting\s*\n?\s*[A-Z][a-z]+ \d{1,2}, \d{4}\s*\n?\s*AGENDA ITEM)", text)
    for p in parts:
        if not p.lstrip().startswith("City Commission Meeting"):
            continue
        hdr = p[:600]
        if re.search(rf"ORDINANCE\s*#\s*{ono}\b", hdr, re.I):
            return p
    return ""


LABEL_RE = re.compile(r"Owner|Petitioner|Location|Land Use|Zoning|Current use|Applicant", re.I)


def parse_memo(memo):
    out = {}
    if not memo:
        return out
    m = re.search(r"following propert(?:y|ies):\s*\n", memo)
    if m:
        raw_lines = [sq(x) for x in memo[m.end():].split("\n") if sq(x)][:30]
        lines = []
        for x in raw_lines:
            if len(x) > 95 or re.match(r"(?:As a result|In [A-Z][a-z]+ of|The |This |Properties )", x):
                break
            if lines and not lines[-1].endswith(":") and re.search(r"(?:\b(?:and|or|of|for|the|on behalf)|,|&|/)$", lines[-1]):
                lines[-1] = lines[-1] + " " + x
            else:
                lines.append(x)
        labels = [x for x in lines if x.endswith(":") and LABEL_RE.search(x)]
        values = [x for x in lines if not x.endswith(":")]
        while labels and len(values) > len(labels):
            idx = next((i for i in range(len(values) - 1) if values[i + 1].startswith("(")), None)
            if idx is None:
                idx = next((i for i in range(len(values) - 1) if len(values[i + 1].split()) <= 2
                            and not re.search(r"\d", values[i + 1]) and not values[i].endswith(")")), None)
            if idx is None:
                break
            values[idx:idx + 2] = [values[idx] + " " + values[idx + 1]]
        if labels and len(values) >= len(labels):
            for lab, val in zip(labels, values):
                out[lab.rstrip(":").strip().lower()] = val
    d = sq(memo) or ""
    d = d.split("STAFF RECOMMENDATION")[0]
    acr = []
    for v in [" ".join(str(x) for x in out.values()), d]:
        for am in re.finditer(r"([\d,]*\.?\d+)\s*(?:\+/-\s*|\(\+/-\)\s*)?acres", v or "", re.I):
            try:
                acr.append(float(am.group(1).replace(",", "")))
            except ValueError:
                pass
        if acr:
            break
    acr = [a for a in acr if 0 < a < 100000]
    if acr:
        out["acres"] = max(acr)
    m = re.search(r"PLANNING COMMISSION RECOMMENDATION:\s*(Approval|Denial)[^()]*\((\d+-\d+),\s*(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\)", d, re.I)
    if m:
        y = int(m.group(5))
        y = y + 2000 if y < 100 else y
        try:
            out["pc_date"] = dt.date(y, int(m.group(3)), int(m.group(4))).isoformat()
            out["pc_result"] = ("Recommended Approval" if m.group(1).lower() == "approval" else "Recommended Denial") + \
                f" ({m.group(2)})"
        except ValueError:
            pass
    m = re.search(r"second and final reading on ([A-Z][a-z]+ \d{1,2}, \d{4})", d)
    if m:
        try:
            out["second_date"] = dt.datetime.strptime(m.group(1), "%B %d, %Y").date().isoformat()
        except ValueError:
            pass
    if re.search(r"If approved on first reading", d, re.I):
        out["is_first"] = True
    elif re.search(r"is being presented (?:for|during the required) second|presented during the required second|"
                   r"approved on first reading", d, re.I):
        out["is_second"] = True
    return out


def minutes_result(text, ono):
    t = sq(text) or ""
    t = re.sub(r"Ordinance\s*No\.\s*", "Ordinance #", t, flags=re.I)
    m = re.search(rf"Motion by[^.]*?\bto (approve|adopt|deny|table|continue|postpone)\s+(?:proposed\s+)?Ordinance\s*#\s*{ono}\b", t, re.I)
    if not m:
        seg = ""
        mm = re.search(rf"ORDINANCE\s*#\s*{ono}\b", t, re.I)
        if mm and re.search(r"withdr[ae]w", t[mm.start(): mm.start() + 1500], re.I):
            return "withdrawn"
        return None
    rest = t[m.end(): m.end() + 4000]
    rest = rest.split("Motion by")[0]
    v = re.search(r"Upon vote,?\s*([^.]*)\.|Motion (carried|failed|passed|died)", rest, re.I)
    act = m.group(1).lower()
    if not v:
        return None
    vote = (v.group(1) or v.group(2) or "").lower()
    ayes = len(re.findall(r"\baye\b", vote))
    nays = len(re.findall(r"\bnay\b", vote))
    if "fail" in vote or "died" in vote or (nays and nays > ayes and "all ayes" not in vote):
        return "failed"
    if act in ("table", "continue", "postpone"):
        return "continued"
    if act == "deny":
        return "denied"
    return "carried"


# ------------------------------------------------------------------ fetch
def fetch(since, refresh):
    if pypdf is None:
        raise RuntimeError("pypdf is required for the Auburndale adapter")
    today = dt.date.today()
    refresh_ids = {}
    for r in refresh or []:
        m = re.search(r"(\d{4})", r or "")
        if m:
            refresh_ids[m.group(1)] = r
    start = (dt.date.fromisoformat(since) - dt.timedelta(days=LOOKBACK_DAYS)).isoformat()
    if refresh_ids:
        start = min(start, (today - dt.timedelta(days=540)).isoformat())
    docs = list_docs(start)
    if not docs["agendas"]:
        raise RuntimeError("no Auburndale City Commission agendas found via the media API")
    log(f"{len(docs['agendas'])} agendas, {len(docs['minutes'])} minutes since {start}")

    cases = {}
    for d, url in docs["agendas"]:
        try:
            txt = pdf_text(url)
        except Exception as e:  # noqa: BLE001
            log(f"agenda {d}: {e}")
            continue
        for ono, title in agenda_list(txt):
            memo = parse_memo(memo_for(txt, ono))
            c = cases.setdefault(ono, {"apps": []})
            c["apps"].append({"date": d, "title": title, "memo": memo, "url": url})
    minutes_txt = {}
    need = {a["date"] for c in cases.values() for a in c["apps"]}
    for d, url in docs["minutes"]:
        if d in need:
            try:
                minutes_txt[d] = (pdf_text(url), url)
            except Exception as e:  # noqa: BLE001
                log(f"minutes {d}: {e}")
    log(f"{len(cases)} in-scope ordinances; {len(minutes_txt)} minutes read")

    out = []
    for ono, c in sorted(cases.items()):
        first = min(a["date"] for a in c["apps"])
        if first < since and ono not in refresh_ids:
            continue
        try:
            out.append(build(ono, c, minutes_txt, today.isoformat()))
        except Exception as e:  # noqa: BLE001
            log(f"{ono}: skipped ({e})")
    for r in set(refresh_ids) - set(cases):
        log(f"refresh id {refresh_ids[r]} not found on agendas since {start}")
    return out


def tidy(v):
    v = sq(v)
    if not v:
        return None
    v = re.sub(r"\(\s*\+/-\s*[\d.,]+\s*acres\)", "", v).strip(" ,;")
    return v or None


def build(ono, c, minutes_txt, today):
    apps = sorted(c["apps"], key=lambda a: a["date"])
    memo = {}
    for a in apps:
        for k, v in a["memo"].items():
            memo.setdefault(k, v)
    hearings = []
    if memo.get("pc_date"):
        hearings.append({"body": PC, "date": memo["pc_date"], "result": memo.get("pc_result")})
    for i, a in enumerate(apps):
        m = a["memo"]
        reading = "second" if m.get("is_second") or (i > 0 and not m.get("is_first")) else "first"
        word = minutes_result(minutes_txt[a["date"]][0], ono) if a["date"] in minutes_txt else None
        res = {"carried": "Approved" if reading == "second" else "Approved (first reading)", "failed": "Denied",
               "denied": "Denied", "continued": "Continued", "withdrawn": "Withdrawn"}.get(word)
        hearings.append({"body": f"{CC} ({reading} reading)", "date": a["date"], "result": res})
        if reading == "first" and m.get("second_date") and m["second_date"] > today and \
                not any(x["date"] == m["second_date"] for x in apps):
            hearings.append({"body": f"{CC} (second reading)", "date": m["second_date"], "result": None})
    seen, hs = set(), []
    for h in sorted(hearings, key=lambda h: h["date"]):
        k = (h["date"], h["body"].split(" (")[0])
        if k in seen:
            continue
        seen.add(k)
        hs.append(h)
    hearings = hs

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
            status, raw = "Filed", f"{lp['body']} {lp['date']}: {lp['result']}"
    else:
        status, raw = "Filed", "On agenda"

    title = apps[0]["title"]
    kind = "CPA" if re.search(r"FUTURE LAND USE", title, re.I) else "ZONING"
    owner = tidy(memo.get("petitioner/ owner") or memo.get("owner/petitioner") or memo.get("petitioner/owner")
                 or memo.get("owner") or memo.get("petitioner"))
    loc = tidy(memo.get("location"))
    cur_flu = tidy(memo.get("current future land use") or memo.get("current county future land use"))
    new_flu = tidy(memo.get("proposed future land use"))
    cur_z = tidy(memo.get("current city zoning") or memo.get("current zoning") or memo.get("current county zoning"))
    new_z = tidy(memo.get("proposed zoning classification") or memo.get("proposed zoning"))
    if kind == "CPA":
        category, app_type = "Comp Plan Amendment", "Future Land Use Map Amendment"
        if memo.get("acres") and memo["acres"] > 50 or re.search(r"Large-Scale", " ".join(str(v) for v in memo.values()), re.I):
            app_type = "Large-Scale Future Land Use Map Amendment"
    else:
        is_pd = bool(re.search(r"Planned (?:Unit )?Development|\bPUD\b|\bPD\b", new_z or title, re.I))
        category = "PD" if is_pd else "Rezoning"
        app_type = "Zoning Map Amendment" + (" (PD)" if is_pd else "")
    pname = re.split(r"\s+-\s+", title)[-1].strip() if " - " in title else None
    pname = pname.title() if pname and pname.isupper() else pname
    desc = sq(re.sub(r"^ORDINANCE\s*#\s*\d{4}\s*-?\s*", "", title, flags=re.I))
    desc = desc.capitalize() if desc and desc.isupper() else desc
    if loc:
        desc = f"{desc}; location: {loc}"
    filed = min([apps[0]["date"]] + ([memo["pc_date"]] if memo.get("pc_date") else []))
    latest = apps[-1]
    minutes_url = next((minutes_txt[a["date"]][1] for a in reversed(apps) if a["date"] in minutes_txt), None)
    return {
        "jurisdiction": JUR, "county": "Polk", "app_num": f"Ordinance #{ono}", "category": category,
        "app_type": app_type, "project_name": pname, "description": desc, "applicant": owner, "parcel_ids": [],
        "address": loc, "acres": memo.get("acres"),
        "from_zoning": cur_z if kind == "ZONING" else None, "to_zoning": new_z if kind == "ZONING" else None,
        "from_flu": cur_flu if kind == "CPA" else None, "to_flu": new_flu if kind == "CPA" else None,
        "filed_date": filed, "status": status, "status_raw": raw, "hearings": hearings,
        "decision_date": decision, "url": latest["url"], "docs_url": minutes_url, "lat": None, "lon": None,
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
