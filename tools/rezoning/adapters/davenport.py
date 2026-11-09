#!/usr/bin/env python3
"""City of Davenport (Polk County) rezoning / PUD / future land use map amendment adapter.

Source: agenda and minutes PDFs on the city website (Catalis GovOffice):
 * https://www.mydavenport.org/commissionagendas -> "2026 City Commission Agendas" page listing
   one PDF per meeting ("June 15, 2026 - Planning Commission", "July 6, 2026 - Regular Meeting").
 * https://www.mydavenport.org/commissionminutes -> yearly pages with the minutes PDFs.
Land use cases are numbered by ordinance and the agenda titles carry the details:
   "ORDINANCE NO. 1451: PUBLIC HEARING AND REQUEST AND RECOMMENDATION TO ASSIGN THE LAND USE
    CLASSIFICATION TO PAGE ROAD FROM CITY RESIDENTIAL MEDIUM (RM) TO CITY MANUFACTURING
    WAREHOUSE (MW) - (OWNER OF RECORD: RJR CONTRACTOR LLC)"
 A case goes Planning Commission (recommendation) -> City Commission first reading -> second and
 final reading. app_num = "Ordinance No. 1451". "Land use classification" items are future land
 use map amendments (category Comp Plan Amendment); "zoning classification" / PUD items are
 Rezoning / PD. Annexations, LDR text amendments and other ordinances are skipped.
 Outcomes come from the minutes ("Motion ... to approve Ordinance No. 1449 on second and final
 reading. Motion carried unanimously."). Parcel ids are captured when the agenda title lists them
 ("PARCEL NOS: 27-26-25-000000-011000 (PORTION) ..."); acreage is usually not published.
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

JURISDICTIONS = ["City of Davenport"]
JUR = "City of Davenport"
BASE = "https://www.mydavenport.org"
SOURCE = "Davenport Planning Commission + City Commission agendas and minutes (mydavenport.org)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
LOOKBACK_DAYS = 150
PC = "Planning Commission"
CC = "City Commission"

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


def log(*a):
    print("[davenport]", *a, file=sys.stderr)


def get(url, tries=4, sleep=0.5):
    last = None
    for i in range(tries):
        try:
            r = _s.get(url, timeout=120)
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
    return (s or "").replace("\u2013", "-").replace("\u2014", "-").replace("\u2012", "-")


# ------------------------------------------------------------------ listings
def year_pages(index_path, word):
    h = get(BASE + index_path).text
    out = {}
    for href, lab in re.findall(r'href="([^"]*index\.asp\?SEC=[^"]+)"[^>]*>\s*(\d{4})\s+City Commission ' + word, h):
        out[int(lab)] = BASE + _html.unescape(href) if href.startswith("/") else _html.unescape(href)
    return out


def list_pdfs(page_url):
    h = get(page_url).text
    out = []
    for href, lab in re.findall(r'href="\s*(?:&#xA;)?\s*([^"]+?\.pdf)"[^>]*>([^<]+)', h):
        href = _html.unescape(href).strip()
        url = href if href.startswith("http") else BASE + href
        lab = sq(_html.unescape(lab)) or ""
        m = re.match(r"([A-Z][a-z]+)\s+(\d{1,2}),\s*(\d{4})\s*-\s*(.+)$", lab)
        if not m:
            continue
        try:
            d = dt.datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y").date().isoformat()
        except ValueError:
            continue
        kind = m.group(4)
        if re.search(r"Planning Commission", kind, re.I):
            body = PC
        elif re.search(r"Regular Meeting|Special Meeting|Public Hearing", kind, re.I):
            body = CC
        else:
            continue
        out.append({"date": d, "body": body, "label": lab, "url": url,
                    "amended": bool(re.search(r"amended", kind, re.I))})
    # keep the amended agenda when both exist
    best = {}
    for x in out:
        k = (x["date"], x["body"])
        if k not in best or x["amended"]:
            best[k] = x
    return sorted(best.values(), key=lambda x: x["date"])


def pdf_text(url):
    rdr = pypdf.PdfReader(io.BytesIO(get(url).content))
    out = []
    for p in rdr.pages:
        try:
            out.append(p.extract_text() or "")
        except Exception:  # noqa: BLE001
            out.append("")
    return nd("\n".join(out))


# ------------------------------------------------------------------ parsing
ORD_HEAD = re.compile(r"ORDINANCE\s+(?:NO\.?\s*)?(\d\s?\d\s?\d\s?\d)\b", re.I)
IN_RE = re.compile(r"LAND USE CLASSIFICATION|ZONING CLASSIFICATION|FUTURE LAND USE|REZON|\bPUD\b|"
                   r"PLANNED UNIT DEVELOPMENT|ZONING MAP|COMPREHENSIVE PLAN MAP", re.I)
OUT_RE = re.compile(r"LAND DEVELOPMENT REGULATIONS|\bLDRS?\b|\bTEXT\b|ANNEX|IMPACT FEE|BUDGET|MILLAGE|VACAT|"
                    r"RETIREMENT|FRANCHISE|RENTAL FEES", re.I)


def agenda_items(text):
    """[(ord_no, item_text, reading)] in-scope ordinance items of an agenda."""
    t = text
    out = []
    ms = list(ORD_HEAD.finditer(t))
    for i, m in enumerate(ms):
        seg = t[m.start(): ms[i + 1].start() if i + 1 < len(ms) else len(t)]
        seg = re.split(r"\n\s*(?:\d{1,2}\.\s|[A-H]\.\s|CITY MANAGER REPORT|ADJOURNMENT|CITY OF DAVENPORT)", seg)[0]
        # include the words before "ORDINANCE NO." on the same item line (e.g. "SECOND AND FINAL READING OF")
        pre = t[max(0, m.start() - 120): m.start()]
        pre = pre.split("\n\n")[-1]
        pre = re.split(r"\n\s*\d{1,2}\.\s", "\n" + pre)[-1]
        full = sq(pre + " " + seg) or ""
        if not IN_RE.search(full) or OUT_RE.search(full.split("(OWNER")[0]):
            continue
        reading = None
        if re.search(r"SECOND AND FINAL READING|SECOND READING|FINAL READING", full, re.I):
            reading = "second"
        elif re.search(r"FIRST READING", full, re.I):
            reading = "first"
        out.append((re.sub(r"\s", "", m.group(1)), full, reading))
    seen, res = set(), []
    for o in out:
        if o[0] not in seen:
            seen.add(o[0])
            res.append(o)
    return res


def minutes_results(text):
    """{ord_no: result_word} from a minutes PDF (section headed by ORDINANCE NO. N)."""
    t = sq(text) or ""
    t = re.sub(r"\bNo\.\s*", "No ", t, flags=re.I)
    res = {}
    ms = list(re.finditer(r"\b\d{1,2}\.\s*(?:PUBLIC HEARING AND (?:FIRST|SECOND)[A-Z ]*?READING OF\s+)?ORDINANCE\s+NO\.?\s*"
                          r"(\d\s?\d)\s?(\d\s?\d)\b", t, re.I))
    for i, m in enumerate(ms):
        seg = t[m.end(): ms[i + 1].start() if i + 1 < len(ms) else len(t)]
        mo = re.search(r"Motion (?:by|was made)[^.]*?(approv|recommend|den|tabl|continu|postpon|withdr)[^.]*\.\s*"
                       r"(?:The\s+)?Motion\s+(carried|failed|passed|died)", seg, re.I)
        if mo:
            act, outc = mo.group(1).lower(), mo.group(2).lower()
            if outc in ("failed", "died"):
                word = "failed"
            elif act.startswith(("tabl", "continu", "postpon")):
                word = "continued"
            elif act.startswith("withdr"):
                word = "withdrawn"
            elif act.startswith("den"):
                word = "denied"
            else:
                word = "carried"
            res[re.sub(r"\s", "", m.group(1) + m.group(2))] = word
        elif re.search(r"\bwithdr[ae]wn\b", seg[:600], re.I):
            res[re.sub(r"\s", "", m.group(1) + m.group(2))] = "withdrawn"
    return res


def norm_result(word, body, reading):
    if not word:
        return None
    if word == "withdrawn":
        return "Withdrawn"
    if word == "continued":
        return "Continued"
    if body == PC:
        return "Recommended Approval" if word == "carried" else "Recommended Denial"
    if word in ("failed", "denied"):
        return "Denied"
    return "Approved" if reading == "second" else "Approved (first reading)"


# ------------------------------------------------------------------ fetch
def fetch(since, refresh):
    if pypdf is None:
        raise RuntimeError("pypdf is required for the Davenport adapter")
    today = dt.date.today()
    refresh_ids = {}
    for r in refresh or []:
        m = re.search(r"(\d{4})", r or "")
        if m:
            refresh_ids[m.group(1)] = r
    start = (dt.date.fromisoformat(since) - dt.timedelta(days=LOOKBACK_DAYS)).isoformat()
    if refresh_ids:
        start = min(start, (today - dt.timedelta(days=540)).isoformat())
    years = range(int(start[:4]), today.year + 1)
    ag_pages = year_pages("/commissionagendas", "Agendas")
    mn_pages = year_pages("/commissionminutes", "Minutes")
    if not ag_pages:
        raise RuntimeError("Davenport agenda index not found")
    agendas, minutes = [], []
    for y in years:
        if y in ag_pages:
            agendas += list_pdfs(ag_pages[y])
        if y in mn_pages:
            minutes += list_pdfs(mn_pages[y])
    agendas = [a for a in agendas if a["date"] >= start]
    minutes = [m for m in minutes if m["date"] >= start]
    log(f"{len(agendas)} agendas, {len(minutes)} minutes since {start}")

    cases = {}
    for a in agendas:
        try:
            txt = pdf_text(a["url"])
        except Exception as e:  # noqa: BLE001
            log(f"agenda {a['label']}: {e}")
            continue
        for ono, itxt, reading in agenda_items(txt):
            c = cases.setdefault(ono, {"apps": []})
            c["apps"].append({"date": a["date"], "body": a["body"], "text": itxt, "reading": reading, "url": a["url"]})
    results = {}
    for m in minutes:
        if not any(any(x["date"] == m["date"] and x["body"] == m["body"] for x in c["apps"]) for c in cases.values()):
            continue
        try:
            mt = pdf_text(m["url"])
            results[(m["date"], m["body"])] = (minutes_results(mt), m["url"], re.sub(r"(\d)\s(\d)", r"\1\2", sq(mt) or ""))
        except Exception as e:  # noqa: BLE001
            log(f"minutes {m['label']}: {e}")
    log(f"{len(cases)} in-scope ordinances; {len(results)} minutes read")

    out = []
    for ono, c in sorted(cases.items()):
        first = min(x["date"] for x in c["apps"])
        if first < since and ono not in refresh_ids:
            continue
        try:
            out.append(build(ono, c, results, today.isoformat()))
        except Exception as e:  # noqa: BLE001
            log(f"{ono}: skipped ({e})")
    for r in set(refresh_ids) - set(cases):
        log(f"refresh id {refresh_ids[r]} not found on agendas since {start}")
    return out


KEEP_UP = {"LLC", "INC", "PUD", "LLP", "LP", "PA", "US", "SR", "CR", "II", "III", "FL"}


def smart_title(v):
    if not v or not v.isupper():
        return v
    out = []
    for w in re.split(r"(\s+)", v):
        core = re.sub(r"[^A-Z0-9-]", "", w)
        if core in KEEP_UP or re.fullmatch(r"\(?[A-Z0-9-]{1,5}\)?[,.]?", w) and "(" in w:
            out.append(w)
        else:
            out.append(w.capitalize())
    return "".join(out)


def clean(v):
    v = sq(v)
    if not v:
        return None
    v = v.strip(" .,;:-")
    return smart_title(v) or None


def build(ono, c, results, today):
    apps = sorted(c["apps"], key=lambda a: (a["date"], a["body"] != PC))
    text = max((a["text"] for a in apps), key=len)
    hearings = []
    cc_n = 0
    for a in apps:
        reading = a["reading"]
        if a["body"] == CC:
            reading = reading or ("second" if cc_n else "first")
            cc_n += 1
            body = f"{CC} ({'second reading' if reading == 'second' else 'first reading'})"
        else:
            body = PC
        mres = results.get((a["date"], a["body"]))
        word = mres[0].get(ono) if mres else None
        res = norm_result(word, a["body"], reading)
        if mres is not None and ono not in mres[0] and a["body"] == CC and \
                not re.search(rf"ORDINANCE\s+(?:NO\.?\s*)?{ono}", mres[2], re.I):
            res = "Not heard (absent from minutes)"
        h = {"body": body, "date": a["date"], "result": res}
        if not any(x["body"] == h["body"] and x["date"] == h["date"] for x in hearings):
            hearings.append(h)

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
    elif past and (past[-1]["result"] or "").startswith(("Continued", "Not heard")):
        status, raw = "Continued", f"{past[-1]['body']} {past[-1]['date']}: {past[-1]['result'].lower()}; not on a later agenda"
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

    t = text.upper()
    kind = "CPA" if re.search(r"LAND USE CLASSIFICATION|FUTURE LAND USE|COMPREHENSIVE PLAN", t) else "ZONING"
    frm = to = None
    m = re.search(r"\bFROM\s+(.+?)\s+TO\s+(.+?)(?:\s*-?\s*\(?\s*OWNER OF RECORD|$)", text, re.I)
    if m:
        frm, to = clean(m.group(1)), clean(m.group(2))
    owner = None
    m = re.search(r"OWNER OF RECORD:\s*(.+?)\)", text, re.I)
    if m:
        owner = clean(m.group(1))
    addr = None
    m = re.search(r"CLASSIFICATION TO\s+(.+?)\s+(?:AT\s+)?FROM\s", text, re.I)
    if m and not re.match(r"PARCEL", m.group(1), re.I):
        a = re.split(r"\s*-?\s*PARCEL NOS?\.?", m.group(1), flags=re.I)[0]
        a = re.sub(r"^PROPERTY LOCATED AT\s+", "", a, flags=re.I)
        addr = clean(a)
    flat = re.sub(r"\s*-\s*", "-", text)
    pids = re.findall(r"\b\d{2}-\d{2}-\d{2}-\d{6}-\d{6}\b", flat)
    pids += re.findall(r"(?<![\d-])\d{6}-\d{6}-\d{6}(?![\d-])", flat)
    pids = list(dict.fromkeys(pids))
    acres = None
    m = re.search(r"([\d,]*\.?\d+)\s*(?:\+/-\s*)?ACRES", text, re.I)
    if m:
        try:
            acres = float(m.group(1).replace(",", ""))
        except ValueError:
            acres = None
    if kind == "CPA":
        category, app_type = "Comp Plan Amendment", "Land Use Classification (Future Land Use Map Amendment)"
    else:
        is_pd = bool(re.search(r"\bPUD\b|PLANNED UNIT DEVELOPMENT", t))
        category = "PD" if is_pd else "Rezoning"
        app_type = "PUD Amendment" if is_pd and re.search(r"AMENDED PLANNED|AMEND", t) else \
            ("PUD Zoning" if is_pd else "Zoning Classification")
    desc = re.sub(r"^.*?ORDINANCE\s+NO\.?\s*\d\s?\d{3}\s*:?\s*", "", text, flags=re.I)
    desc = re.sub(r"^(?:PUBLIC HEARING AND )?(?:REQUEST AND RECOMMENDATION|FIRST READING OF|SECOND AND FINAL READING OF)\s*", "",
                  desc, flags=re.I)
    desc = sq(desc)
    if desc and desc.isupper():
        desc = smart_title(desc)
    if desc and len(desc) > 500:
        desc = desc[:497] + "..."
    latest = apps[-1]
    return {
        "jurisdiction": JUR, "county": "Polk", "app_num": f"Ordinance No. {ono}", "category": category,
        "app_type": app_type, "project_name": None, "description": desc, "applicant": owner,
        "parcel_ids": pids, "address": addr, "acres": acres,
        "from_zoning": frm if kind == "ZONING" else None, "to_zoning": to if kind == "ZONING" else None,
        "from_flu": frm if kind == "CPA" else None, "to_flu": to if kind == "CPA" else None,
        "filed_date": apps[0]["date"], "status": status, "status_raw": raw, "hearings": hearings,
        "decision_date": decision, "url": latest["url"], "docs_url": None, "lat": None, "lon": None,
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
