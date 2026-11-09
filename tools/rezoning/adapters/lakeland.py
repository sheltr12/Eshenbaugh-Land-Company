#!/usr/bin/env python3
"""City of Lakeland (Polk County) rezoning / PUD / future land use map amendment adapter.

Source: the monthly Planning & Zoning Board agenda packets the city posts on
https://www.lakelandgov.net/departments/city-clerk/board-meetings-agendas/ (one PDF per meeting,
about the last 12 months, e.g. /media/23617/p-and-z-packet_10-20-26.pdf). Each packet holds:
 * the agenda: "ITEM 14: Change in zoning on approximately 24.93 acres from LD ... to PUD ...
   Owner: ... Applicant: ... (PUD26-011/ZON26-007)". New cases are heard under PUBLIC HEARING,
   then come back the next month under GENERAL MEETING for the board's recommendation.
 * "Planning & Zoning Board General Application" pages for new cases (Project No, Application
   Date, Parcel ID, Total Acreage, applicant, owner, current/proposed land use and zoning).
 * "Staff Recommendation" reports (location, owners, P&Z hearing / final decision dates and the
   legal description with Polk parcel numbers such as 23-28-33-000000-044050).
 * the minutes of the previous P&Z meeting (continuances, recommendations, vote counts).
 * a "Report of City Commission Action on Planning and Zoning Board Recommendations" listing the
   ordinances the City Commission passed on first / second reading, with the case numbers
   ("Proposed 26-028; ... (PUD26-012) Approved 7-0, Ordinance 6158"). That gives final outcomes.
Case numbers: ZON = rezoning, PUD = PUD (new and major modifications; minor modifications are
skipped), LUS / LUL = small / large scale land use map amendments. CUP (conditional use), ANX
(annexation), LDC / CPA text amendments are skipped. One agenda item can carry several numbers
("ANX26-003/LUS26-001/ZON26-009/ZON26-010"): it becomes one Comp Plan record (first LUS/LUL) and
one zoning record (first PUD, else first ZON); the other numbers go into the description.
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

JURISDICTIONS = ["City of Lakeland"]
JUR = "City of Lakeland"
BASE = "https://www.lakelandgov.net"
BOARD_PAGE = BASE + "/departments/city-clerk/board-meetings-agendas/"
SOURCE = "Lakeland Planning & Zoning Board packets (agenda, applications, staff reports, minutes, City Commission action report)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
LOOKBACK_DAYS = 150
PZ = "Planning & Zoning Board"
CC = "City Commission"

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


def log(*a):
    print("[lakeland]", *a, file=sys.stderr)


def get(url, tries=4, sleep=0.6, **kw):
    last = None
    for i in range(tries):
        try:
            r = _s.get(url, timeout=180, **kw)
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


def norm_dash(s):
    return (s or "").replace("\u2014", "-").replace("\u2013", "-").replace("\u2012", "-")


# ------------------------------------------------------------------ packet list
def list_packets():
    h = get(BOARD_PAGE).text
    out = {}
    for href, label in re.findall(r'href="([^"]*p-and-z-packet[^"]*\.pdf)"[^>]*>([^<]*)', h, re.I):
        m = re.search(r"(\d{1,2})[-_](\d{1,2})[-_](\d{2,4})\.pdf$", href)
        if not m:
            m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", label)
        if not m:
            continue
        y = int(m.group(3))
        y = y + 2000 if y < 100 else y
        try:
            d = dt.date(y, int(m.group(1)), int(m.group(2))).isoformat()
        except ValueError:
            continue
        url = href if href.startswith("http") else BASE + href
        out[d] = _html.unescape(url)
    return sorted(out.items())


def pdf_pages(url):
    rdr = pypdf.PdfReader(io.BytesIO(get(url).content))
    pages = []
    for p in rdr.pages:
        try:
            pages.append(norm_dash(p.extract_text() or ""))
        except Exception:  # noqa: BLE001
            pages.append("")
    return pages


# ------------------------------------------------------------------ case numbers
CASE_RE = re.compile(r"\b(ZON|PUD|LUS|LUL|CPA|ANX|CUP|LDC|ADU|SIT|CPT|HPB|VAR|ZBA)\s?(\d{2})\s?-\s?(\d{3})\b")


def cases_in(text):
    out = []
    for m in CASE_RE.finditer(text or ""):
        c = f"{m.group(1)}{m.group(2)}-{m.group(3)}"
        if c not in out:
            out.append(c)
    return out


def item_cases(text):
    """Case numbers from the trailing '(...)' of an agenda / minutes item."""
    paren = re.findall(r"\(([^()]*\b(?:ZON|PUD|LUS|LUL|CPA|ANX|CUP|LDC)\s?\d{2}\s?-\s?[^()]*)\)", text or "")
    return cases_in(" ".join(paren)) if paren else cases_in(text)


MINOR_RE = re.compile(r"\bminor modification\b", re.I)
MAPCPA_RE = re.compile(r"land use map amendment|future land use (?:map )?designation of|change the future land use", re.I)


def primaries(cases, text):
    """[(primary_case, kind)] kind in CPA / ZONING for an in-scope agenda item."""
    out = []
    cpa = [c for c in cases if c[:3] in ("LUS", "LUL")]
    if not cpa and MAPCPA_RE.search(text or ""):
        cpa = [c for c in cases if c[:3] == "CPA"]
    if cpa:
        out.append((cpa[0], "CPA"))
    z = [c for c in cases if c[:3] == "PUD"] or [c for c in cases if c[:3] == "ZON"]
    if z and not (z[0].startswith("PUD") and MINOR_RE.search(text or "")):
        out.append((z[0], "ZONING"))
    return out


# ------------------------------------------------------------------ page classification
HEAD_RE = re.compile(r"^\s*(AGENDA\b|Planning & Zoning Board General Application|Community & Economic Development|"
                     r"MINUTES\b|Report of City Commission Action)", re.I)


def page_head(t):
    first = (sq(t) or "")[:400]
    m = HEAD_RE.match(first)
    if not m:
        return None
    h = m.group(1).lower()
    if h.startswith("agenda"):
        return "agenda"
    if h.startswith("planning & zoning board general"):
        return "app"
    if h.startswith("community"):
        return "staff" if "Staff Recommendation" in first[:120] else None
    if h.startswith("minutes"):
        return "minutes"
    if h.startswith("report of city"):
        return "report"
    return "other"


def split_sections(pages):
    """[(kind, text, first_page_index)] runs of pages starting at a recognised header page."""
    secs = []
    cur = None
    for i, t in enumerate(pages):
        k = page_head(t)
        if k:
            if cur:
                secs.append(cur)
            cur = [k, t, i]
        elif cur:
            cur[1] += "\n" + t
    if cur:
        secs.append(cur)
    return [tuple(s) for s in secs]


# ------------------------------------------------------------------ agenda / minutes items
def split_items(text):
    """[(section, item_no, text)] for 'ITEM n:' blocks; section = PUBLIC HEARING / GENERAL MEETING."""
    t = text
    marks = [(m.start(), m.group(0).strip()) for m in re.finditer(r"\n\s*(PUBLIC HEARING|GENERAL MEETING)\s*\n", t)]
    items = []
    ms = list(re.finditer(r"ITEM\s+(\d+)\s*:", t))
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(t)
        sec = None
        for pos, name in marks:
            if pos < m.start():
                sec = name
        items.append((sec, int(m.group(1)), t[m.end():end]))
    return items


def minutes_result(seg):
    s = sq(seg) or ""
    if re.search(r"\bwithdr[ae]w", s, re.I):
        return "Withdrawn"
    if re.search(r"motion (?:for|to) (?:a |an )?(?:[\w-]+ )?(?:month )?continu|motion to continue|continuance[^.]*passed", s, re.I):
        return "Continued"
    if re.search(r"motion (?:for|to) (?:recommend )?den|recommend(?:ed|ation of)? denial", s, re.I):
        return "Recommended Denial"
    if re.search(r"motion (?:for|to) (?:recommend )?approv", s, re.I):
        if re.search(r"passed|carried|approved", s, re.I):
            return "Recommended Approval"
        if re.search(r"failed", s, re.I):
            return "Recommended Denial"
    if re.search(r"written recommendation[^.]*next", s, re.I):
        return "Public hearing held"
    return None


def parse_report(text):
    """[(cc_meeting_date, reading, cases, result, label)] from a Report of City Commission Action."""
    out = []
    t = norm_dash(text)
    blocks = re.split(r"(Meeting of\s+[A-Z][a-z]+\s+\d{1,2},\s*\d{4})", t)
    for i in range(1, len(blocks) - 1, 2):
        try:
            d = dt.datetime.strptime(re.sub(r"\s+", " ", blocks[i][11:]).strip(), "%B %d, %Y").date().isoformat()
        except ValueError:
            continue
        body = blocks[i + 1]
        reading = None
        parts = re.split(r"(Ordinances?\s*\((?:First|Second) Reading\)|Resolutions?)", body)
        for p in parts:
            if re.match(r"Ordinances?\s*\(First", p):
                reading = "first"
                continue
            if re.match(r"Ordinances?\s*\(Second", p):
                reading = "second"
                continue
            if re.match(r"Resolutions?", p):
                reading = "resolution"
                continue
            for e in re.split(r"(?=Proposed\s+\d{2}\s*-\s*\d{3})", p):
                cs = item_cases(e)
                if not cs or not e.strip().startswith("Proposed"):
                    continue
                es = sq(e)
                lab = re.match(r"Proposed\s+(\d{2}\s*-\s*\d{3})", es)
                res = None
                if re.search(r"\bDenied\b|\bFailed\b", es):
                    res = "Denied"
                elif re.search(r"\bWithdrawn\b", es):
                    res = "Withdrawn"
                elif re.search(r"\bContinued\b|\bTabled\b|\bDeferred\b", es):
                    res = "Continued"
                elif re.search(r"\bApproved\b", es):
                    res = "Approved" if reading == "second" else "Approved (first reading)"
                elif reading == "first":
                    res = "Approved (first reading)"
                ordm = re.search(r"Ordinance\s+(\d{4})\s*$", es)
                label = f"Proposed {lab.group(1).replace(' ', '')}" if lab else None
                if ordm:
                    label = f"{label}, Ordinance {ordm.group(1)}" if label else f"Ordinance {ordm.group(1)}"
                out.append((d, reading, cs, res, label))
    return out


def field(text, label, stop):
    m = re.search(rf"{label}\s*:?\s*(.+?)\s*(?:{stop}|\n)", text)
    return sq(m.group(1)) if m else None


def parse_app(text):
    t = text
    out = {}
    out["project"] = cases_in(field(t, r"Project No", r"Application Date") or "")
    m = re.search(r"Application Date:\s*(\d{1,2})/(\d{1,2})/(\d{4})", t)
    if m:
        try:
            out["app_date"] = dt.date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
        except ValueError:
            pass
    out["assoc"] = cases_in(field(t, r"Associated Projects", r"Project Name") or "")
    out["name"] = field(t, r"Project Name", r"Subject Property")
    out["address"] = field(t, r"Subject Property Address", r"Parcel ID")
    pm = re.search(r"Parcel ID:\s*([\d\- ,]+?)\s*Total Acreage", t)
    out["parcels"] = re.findall(r"\d[\d-]{15,}\d", pm.group(1)) if pm else []
    am = re.search(r"Total Acreage:\s*([\d,]*\.?\d+)", t)
    out["acres"] = float(am.group(1).replace(",", "")) if am else None
    out["applicant"] = field(t, r"Applicant Name", r"Applicant Address")
    out["owner"] = field(t, r"Owner Name", r"Owner Address")
    out["type"] = field(t, r"Application Type", r"\n")
    m = re.search(r"Land Use\s*\n\s*Current\s+(.+?)\n\s*Proposed\s+(.+?)\n", t)
    if m:
        out["flu"] = (sq(m.group(1)), sq(m.group(2)))
    m = re.search(r"\nZoning\s*\n\s*Current\s+(.+?)\n\s*Proposed\s+(.+?)\n", t)
    if m:
        out["zoning"] = (sq(m.group(1)), sq(m.group(2)))
    m = re.search(r"Explanation of\s*Request:\s*(.+?)(?:Justification:|$)", t, re.S)
    if m:
        out["explanation"] = sq(m.group(1))
    return out


def parse_staff(text):
    t = text
    out = {}
    m = re.search(r"Project No:\s*(.+?)\s*Location:", t, re.S)
    out["project"] = cases_in(m.group(1)) if m else []
    out["location"] = field(t, r"Location", r"\n")
    out["owner"] = field(t, r"Owners?", r"\n")
    out["applicant"] = field(t, r"Applicant", r"\n")
    out["cur_zoning"] = field(t, r"Current Zoning", r"Future Land Use|\n")
    out["cur_flu"] = field(t, r"Future Land Use", r"\n")
    ds = {}
    for lab, key in ((r"P&Z Hearing", "pz_hearing"), (r"P&Z Final Decision", "pz_final")):
        m = re.search(lab + r":\s*([A-Z][a-z]+ \d{1,2}, \d{4})", t)
        if m:
            try:
                ds[key] = dt.datetime.strptime(m.group(1), "%B %d, %Y").date().isoformat()
            except ValueError:
                pass
    out.update(ds)
    m = re.search(r"Request:\s*(.+?)(?:1\.0 Location|Location Maps|$)", t, re.S)
    out["request"] = sq(m.group(1)) if m else None
    pids = re.findall(r"\b\d{2}-\d{2}-\d{2}-\d{6}-\d{6}\b", t)
    pids += [p for p in re.findall(r"(?<![\d-])\d{18}(?![\d-])", t)]
    out["parcels"] = list(dict.fromkeys(pids))
    return out


# ------------------------------------------------------------------ text helpers
def parse_acres(text):
    m = re.search(r"(?:approximately|on)\s+([\d,]*\.?\d+)\s*(?:\+/-\s*)?acres", sq(text) or "", re.I)
    if m:
        try:
            v = float(m.group(1).replace(",", ""))
            return v if 0 < v < 100000 else None
        except ValueError:
            return None
    return None


def clean(v):
    v = sq(v)
    if not v:
        return None
    v = v.strip(" .,;:")
    return v or None


def zoning_from_to(text):
    d = sq(text) or ""
    m = re.search(r"(?:change in zoning|rezon\w*|zoning)\b(?:[^.]|\.\d){0,80}?\bfrom\s+(.+?\))\s+to\s+(.+?\))", d, re.I)
    if m:
        return clean(m.group(1)), clean(m.group(2))
    m = re.search(r"application of\s+(?:new\s+)?(.+?\))\s+zoning", d, re.I)
    if m:
        return None, clean(m.group(1))
    if re.search(r"Major modification of PUD", d, re.I):
        return "PUD", "PUD"
    return None, None


def flu_from_to(text):
    d = sq(text) or ""
    m = re.search(r"future land use (?:map )?designation (?:from\s+(.+?\))\s+)?to\s+(.+?\))", d, re.I)
    if m:
        return clean(m.group(1)), clean(m.group(2))
    m = re.search(r"apply a future land use designation of\s+(.+?\))", d, re.I)
    if m:
        return None, clean(m.group(1))
    return None, None


def item_desc(text):
    d = sq(text) or ""
    d = re.split(r"\s*Owners?:\s", d)[0]
    d = re.sub(r"^\s*a\.\s*", "", d)
    return d[:400] if d else None


def item_people(text):
    d = sq(text) or ""
    o = re.search(r"Owners?:\s*(.+?)\.\s+Applicant", d)
    a = re.search(r"Applicant:\s*(.+?)\.\s*\(", d)
    return (clean(o.group(1)) if o else None), (clean(a.group(1)) if a else None)


# ------------------------------------------------------------------ fetch
def fetch(since, refresh):
    if pypdf is None:
        raise RuntimeError("pypdf is required for the Lakeland adapter")
    today = dt.date.today().isoformat()
    refresh_ids = {re.sub(r"\s", "", r).upper(): r for r in (refresh or []) if r}
    packets = list_packets()
    if not packets:
        raise RuntimeError("no Planning & Zoning Board packets found on " + BOARD_PAGE)
    start = (dt.date.fromisoformat(since) - dt.timedelta(days=LOOKBACK_DAYS)).isoformat()
    if refresh_ids:
        start = min(start, packets[0][0])
    use = [(d, u) for d, u in packets if d >= start]
    log(f"{len(packets)} packets listed; reading {len(use)} since {start}")

    recs = {}          # primary case -> dict
    apps, staffs = {}, {}
    minutes_res = []   # (date, cases, result)
    cc_actions = []    # (date, reading, cases, result, label)

    for pdate, url in use:
        try:
            pages = pdf_pages(url)
        except Exception as e:  # noqa: BLE001
            log(f"packet {pdate} unreadable: {e}")
            continue
        secs = split_sections(pages)
        for kind, text, pidx in secs:
            try:
                if kind == "agenda":
                    for sec, no, itxt in split_items(text):
                        cs = item_cases(itxt)
                        for prim, k in primaries(cs, itxt):
                            r = recs.setdefault(prim, {"kind": k, "cases": [], "apps": []})
                            r["cases"] = list(dict.fromkeys(r["cases"] + cs))
                            r["apps"].append({"date": pdate, "section": sec, "item": no, "text": itxt, "url": url})
                elif kind == "app":
                    a = parse_app(text)
                    for c in a.get("project") or []:
                        apps.setdefault(c, (a, url))
                    for c in a.get("assoc") or []:
                        apps.setdefault(c, (a, url))
                elif kind == "staff":
                    st = parse_staff(text)
                    for c in st.get("project") or []:
                        staffs[c] = (st, url, pdate)
                elif kind == "minutes":
                    m = re.search(r"([A-Z][a-z]+day),\s*([A-Z][a-z]+ \d{1,2},\s*\d{4})", text[:600])
                    if not m:
                        continue
                    mdate = dt.datetime.strptime(re.sub(r"\s+", " ", m.group(2)), "%B %d, %Y").date().isoformat()
                    body = text.split("Respectfully Submitted")[0]
                    for sec, no, itxt in split_items(body):
                        cs = item_cases(itxt.split("\n\n")[0] if False else itxt[:900])
                        res = minutes_result(itxt)
                        if cs:
                            minutes_res.append((mdate, cs, res))
                elif kind == "report":
                    cc_actions += parse_report(text)
            except Exception as e:  # noqa: BLE001
                log(f"packet {pdate} page {pidx}: {e}")
    log(f"{len(recs)} in-scope cases on agendas; {len(minutes_res)} minutes items; {len(cc_actions)} commission actions")

    out = []
    for prim, r in sorted(recs.items()):
        first = min(a["date"] for a in r["apps"])
        if first < since and prim not in refresh_ids:
            continue
        try:
            out.append(build(prim, r, apps, staffs, minutes_res, cc_actions, today))
        except Exception as e:  # noqa: BLE001
            log(f"{prim}: skipped ({e})")
    for rid in set(refresh_ids) - set(recs):
        log(f"refresh id {refresh_ids[rid]} not found in packets since {start}")
    return out


def build(prim, r, apps, staffs, minutes_res, cc_actions, today):
    apps_l = sorted(r["apps"], key=lambda a: a["date"])
    first_app = apps_l[0]
    latest = apps_l[-1]
    kind = r["kind"]
    cases = r["cases"]
    text0 = first_app["text"]
    app, app_url = apps.get(prim, (None, None))
    if not app:
        for c in cases:
            if c in apps:
                app, app_url = apps[c]
                break
    st = None
    for c in [prim] + cases:
        if c in staffs:
            st, st_url, _ = staffs[c]
            break

    # hearings: P&Z appearances, minutes results, staff report dates, City Commission actions
    hearings = []
    by_date = {}
    for a in apps_l:
        sec = "public hearing" if (a["section"] or "").startswith("PUBLIC") else "recommendation"
        by_date[a["date"]] = {"body": PZ, "date": a["date"], "result": None, "_sec": sec}
    for mdate, cs, res in minutes_res:
        if prim in cs:
            h = by_date.setdefault(mdate, {"body": PZ, "date": mdate, "result": None})
            if res:
                h["result"] = res
    if st:
        for key in ("pz_hearing", "pz_final"):
            d = st.get(key)
            if d and d not in by_date and d >= first_app["date"]:
                by_date[d] = {"body": PZ, "date": d, "result": None}
    for h in by_date.values():
        if h["result"] is None and h["date"] <= today:
            h["result"] = None
        h.pop("_sec", None)
        hearings.append(h)
    cc_labels = []
    for d, reading, cs, res, label in cc_actions:
        if prim in cs:
            body = f"{CC} ({'second reading' if reading == 'second' else 'first reading'})" \
                if reading in ("first", "second") else CC
            h = {"body": body, "date": d, "result": res}
            if h not in hearings:
                hearings.append(h)
            if label:
                cc_labels.append(label)
    hearings.sort(key=lambda h: (h["date"], h["body"]))

    # status
    status, raw, decision = None, None, None
    final = [h for h in hearings if h["body"].startswith(CC) and h["result"] in ("Approved", "Denied")]
    future = [h for h in hearings if h["date"] > today]
    past = [h for h in hearings if h["date"] <= today]
    if any(h["result"] == "Withdrawn" for h in hearings):
        w = [h for h in hearings if h["result"] == "Withdrawn"][-1]
        status, raw = "Withdrawn", f"{w['body']} {w['date']}: withdrawn"
    elif final:
        status, decision = final[-1]["result"], final[-1]["date"]
        raw = f"{final[-1]['body']} {decision}: {final[-1]['result']}" + (f" ({cc_labels[-1]})" if cc_labels else "")
    elif future:
        status, raw = "Hearing Scheduled", f"{future[0]['body']} {future[0]['date']}"
    elif past and past[-1]["result"] == "Continued":
        status, raw = "Continued", f"{past[-1]['body']} {past[-1]['date']}: continued"
    elif past:
        lp = past[-1]
        status = "Filed"
        raw = f"{lp['body']} {lp['date']}" + (f": {lp['result']}" if lp["result"] else "") + \
              "; next hearing not yet on a posted agenda"
    else:
        status, raw = "Filed", "On P&Z agenda"

    # fields
    pre_text = st.get("request") if st and st.get("request") else text0
    owner, applicant = item_people(text0)
    if st:
        owner = owner or clean(st.get("owner"))
        applicant = applicant or clean(st.get("applicant"))
    if app:
        applicant = applicant or clean(app.get("applicant"))
        owner = owner or clean(app.get("owner"))
    if applicant and owner and owner.lower() not in applicant.lower():
        appl = f"{applicant} (owner: {owner})"
    else:
        appl = applicant or owner

    acres = parse_acres(text0) or parse_acres(pre_text) or (app.get("acres") if app else None)
    from_z = to_z = from_flu = to_flu = None
    if kind == "CPA":
        from_flu, to_flu = flu_from_to(text0)
        if not to_flu and app and app.get("flu"):
            from_flu, to_flu = app["flu"]
        if not from_flu and st:
            from_flu = clean(st.get("cur_flu"))
        category = "Comp Plan Amendment"
        app_type = "Large Scale Land Use Map Amendment" if prim.startswith("LUL") else "Small Scale Land Use Map Amendment"
    else:
        from_z, to_z = zoning_from_to(text0)
        if not to_z and app and app.get("zoning"):
            from_z, to_z = app["zoning"]
        if not from_z and st:
            from_z = clean(st.get("cur_zoning"))
        is_pd = prim.startswith("PUD") or bool(re.search(r"\bPUD\b|Planned Unit Development", to_z or ""))
        category = "PD" if is_pd else "Rezoning"
        if prim.startswith("PUD"):
            app_type = "PUD Major Modification" if re.search(r"major modification", text0, re.I) else "PUD Rezoning"
        else:
            app_type = "Change in Zoning"
    for v in ("Not Applicable", "N/A", "NA"):
        if from_z == v:
            from_z = None
        if to_z == v:
            to_z = None
        if from_flu == v:
            from_flu = None
        if to_flu == v:
            to_flu = None

    parcels = []
    if app:
        parcels += app.get("parcels") or []
    if st:
        parcels += st.get("parcels") or []
    seen, pids = set(), []
    for p in parcels:
        k = re.sub(r"\D", "", p)
        if len(k) == 19 and k[12] == "0":   # tolerate a stray digit in hand-typed legal descriptions
            continue
        if len(k) == 18 and k not in seen:
            seen.add(k)
            pids.append(p)

    address = None
    if app and app.get("address") and not re.search(r"PENDING|N/A", app["address"], re.I):
        address = app["address"].title()
    if not address and st:
        address = clean(st.get("location"))
    if not address:
        m = re.search(r"(?:property )?located at\s+(.+?)(?:\.\s|\. Owner|$)", sq(text0) or "", re.I)
        address = clean(m.group(1)) if m else None

    desc = item_desc(text0)
    others = [c for c in cases if c != prim]
    if others:
        desc = (desc or "") + f" (related cases: {', '.join(others)})"
    if cc_labels:
        desc = (desc or "") + f" [{cc_labels[-1]}]"
    if desc and len(desc) > 500:
        desc = desc[:497] + "..."

    filed = (app or {}).get("app_date") or first_app["date"]
    if filed > first_app["date"]:
        filed = first_app["date"]
    pname = clean((app or {}).get("name"))
    if pname:
        pname = pname.title()
    return {
        "jurisdiction": JUR, "county": "Polk", "app_num": prim, "category": category, "app_type": app_type,
        "project_name": pname, "description": sq(desc), "applicant": appl, "parcel_ids": pids,
        "address": address, "acres": acres, "from_zoning": from_z, "to_zoning": to_z,
        "from_flu": from_flu, "to_flu": to_flu, "filed_date": filed, "status": status, "status_raw": raw,
        "hearings": hearings, "decision_date": decision, "url": latest["url"],
        "docs_url": st_url if st else (app_url or None), "lat": None, "lon": None, "source": SOURCE,
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
