#!/usr/bin/env python3
"""City of DeLand (Volusia County) rezoning / PD / future land use map amendment adapter.

Sources (all public, no login):
 1. Accela Citizen Access, agency DELAND, module Planning (aca-prod.accela.com/DELAND), via the
    shared accela_core scraper. Record types searched:
      Planning/Rezoning/NA/NA  "Rezoning" (Z26-062): ASI "Zoning Change Type" = Change of Zoning ->
          Rezoning; Planned Development / PD target / PD amendment -> PD.
      Planning/Land Use/NA/NA  "Comprehensive Plan Amendment" (SMLU26-203 small scale, CPA26-176):
          ASI Application Type Small / Large Scale Land Use -> Comp Plan Amendment (text-only
          amendments with no parcel are skipped).
    Annexations (AN26-) and Plan Modifications (site plan revisions) are not collected.
    Accela gives status, application date, parcel (12-digit Volusia parcel number, returned dashed
    as 7020-00-00-0872), size in acres, requested zoning / land use, owner and the workflow
    ("Hearing Process": Planning Board Approved, City Commission Approved ... with mark dates).
 2. CivicClerk (delandfl.api.civicclerk.com): Planning Board and City Commission agendas. Items
    carry the case number in the item text or attachment names ("Staff Report Z26-062_...",
    "Project Number: Z26-062"), which gives exact hearing dates, ordinance readings and the staff
    report link. Results come from the Accela workflow marks (attached to the latest hearing of
    that body on or before the mark date).
"""
import datetime as dt
import os
import re
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import accela_core as C  # noqa: E402

JURISDICTIONS = ["City of DeLand"]
JUR = "City of DeLand"
SOURCE = "DeLand Accela (Planning) + CivicClerk (Planning Board / City Commission agendas)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
CC_API = "https://delandfl.api.civicclerk.com/v1"
CC_PORTAL = "https://delandfl.portal.civicclerk.com"
CASE_RX = re.compile(r"\b((?:Z|SMLU|LSLU|LLU|CPA)\d{2}-\d{3,4})\b")
LOOKAHEAD_DAYS = 75
MIN_SCAN_DAYS = 240

CFG = {
    "agency": "DELAND", "module": "Planning", "tab": "Planning",
    "jurisdiction": JUR, "county": "Volusia", "source": SOURCE,
    "types": {
        "Planning/Rezoning/NA/NA": None,
        "Planning/Land Use/NA/NA": "Comp Plan Amendment",
    },
    "chunk_days": 400,
}

_s = requests.Session()
_s.headers.update({"User-Agent": UA})
_HEARINGS = {}   # case number -> list of agenda appearances (filled once per fetch)


def log(*a):
    print("[deland]", *a, file=sys.stderr, flush=True)


def get(url, params=None, tries=3, sleep=0.35):
    last = None
    for i in range(tries):
        try:
            r = _s.get(url, params=params, timeout=90)
            r.raise_for_status()
            time.sleep(sleep)
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1) ** 2)
    raise RuntimeError(f"GET failed {url}: {last}")


def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = s.replace("&nbsp;", " ").replace(" ", " ").replace("&amp;", "&").replace("​", "")
    s = re.sub(r"&#?\w+;", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def walk(items):
    for it in items or []:
        yield it
        yield from walk(it.get("childItems"))


def fmt_parcel(p):
    p = re.sub(r"[\s\-]", "", p or "")
    if re.fullmatch(r"[0-9A-Z]{12}", p or ""):
        return f"{p[0:4]}-{p[4:6]}-{p[6:8]}-{p[8:12]}"
    return p or None


# ------------------------------------------------------------------ CivicClerk agendas
def scan_agendas(start, end):
    url = f"{CC_API}/Events"
    params = {"$filter": f"startDateTime ge {start}T00:00:00Z and startDateTime lt {end}T00:00:00Z",
              "$orderby": "startDateTime"}
    events = []
    while url:
        d = get(url, params).json()
        events.extend(d.get("value", []))
        url, params = d.get("@odata.nextLink"), None
    out = {}
    n = 0
    for ev in events:
        name = ev.get("eventName") or ""
        cat = ev.get("categoryName") or ""
        if not ev.get("agendaId") or re.search(r"workshop|budget|cancel|test|strategic|organizational", name, re.I):
            continue
        if re.search(r"Planning Board", cat + " " + name, re.I):
            body = "Planning Board"
        elif re.search(r"City Commission", cat + " " + name, re.I):
            body = "City Commission"
        else:
            continue
        try:
            mt = get(f"{CC_API}/Meetings/{ev['agendaId']}").json()
        except Exception as e:  # noqa: BLE001
            log(f"meeting {ev['agendaId']}: {e}")
            continue
        n += 1
        date = ev["startDateTime"][:10]
        files = {f.get("type"): f.get("fileId") for f in ev.get("publishedFiles") or []}
        for it in walk(mt.get("items")):
            iname = strip_html(it.get("agendaObjectItemName"))
            desc = strip_html(it.get("agendaObjectItemDescription"))
            atts = it.get("attachmentsList") or []
            fnames = " ".join(a.get("fileName") or "" for a in atts)
            cases = CASE_RX.findall(iname) + CASE_RX.findall(desc) + CASE_RX.findall(fnames)
            if not cases:
                continue
            reading = None
            m = re.search(r"\b(First|Second|Continuation of First|Continuation of Second)\s+Reading", iname, re.I)
            if m:
                reading = m.group(1).lower()
            staff = [a for a in atts if re.search(r"staff\s*report", a.get("fileName") or "", re.I)]
            appl = None
            m = re.search(r"Applicant Name:\s*(.+?)\s+Project Number:", iname + " " + desc)
            if m:
                appl = m.group(1).strip(" -")
            ordno = None
            m = re.search(r"Ordinance\s+(?:No\.?\s*)?(\d{4}-\d{1,3})", iname + " " + fnames, re.I)
            if m:
                ordno = m.group(1)
            for c in dict.fromkeys(cases):
                out.setdefault(c, []).append({
                    "date": date, "body": body, "reading": reading, "event_id": ev["id"], "name": iname,
                    "applicant": appl, "ordinance": ordno,
                    "staff": bool(staff), "packet": files.get("Agenda Packet"),
                })
    log(f"{n} Planning Board / City Commission agendas read {start}..{end}; {len(out)} case numbers")
    return out


# ------------------------------------------------------------------ record building
def category_of(row, d):
    t = (d.get("type") or row.get("type") or "")
    tval = row.get("_tval") or ""
    if "Land Use" in tval or re.search(r"Comprehensive Plan|Land Use", t, re.I):
        return "Comp Plan Amendment"
    if "Rezoning" in tval or re.search(r"Rezon", t, re.I):
        ztype = C.asi_get(d, r"^Zoning Change Type$") or ""
        target = C.asi_get(d, r"^Requested Zoning Designation$") or ""
        pdesc = d.get("record_details", {}).get("Project Description") or ""
        if re.search(r"Planned Development", ztype, re.I) or re.search(r"\bPD\b|Planned Development", target) \
                or re.search(r"\bPD\b|\bPUD\b", pdesc.split("\n")[0]):
            return "PD"
        return "Rezoning"
    num = d.get("app_num") or row.get("app_num") or ""
    if re.match(r"(SMLU|LSLU|LLU|CPA)\d", num):
        return "Comp Plan Amendment"
    if re.match(r"Z\d", num):
        return "Rezoning"
    return None


def prefilter(row, d):
    cat = category_of(row, d)
    if cat is None:
        return False
    if cat == "Comp Plan Amendment":
        apptype = C.asi_get(d, r"^Application Type$") or ""
        if re.search(r"text", apptype, re.I) and not d.get("parcels"):
            return False
    return True


WF_RESULT = re.compile(r"^(Planning Board|City Commission)\s+(Approved|Denied|Continued|Tabled|Withdrawn|"
                       r"Postponed|Approved with Conditions|Recommended .*)$", re.I)


def workflow_results(d):
    """[(body, word, mark_date)] from Accela workflow hearing marks."""
    out = []
    for t in d.get("workflow", []):
        for e in t.get("entries", []):
            st, when = e.get("status"), e.get("date")
            if not st or not when:
                continue
            m = WF_RESULT.match(st.strip())
            if m:
                out.append((m.group(1).title(), m.group(2).title(), when))
    return out


def result_label(body, word):
    w = word.lower()
    if "contin" in w or "tabled" in w or "postpon" in w:
        return "Continued"
    if "withdr" in w:
        return "Withdrawn"
    if body == "Planning Board":
        return "Recommended Denial" if "denied" in w else "Recommended Approval"
    return "Denied" if "denied" in w else "Approved"


def build(row, d, aca):
    cat = category_of(row, d)
    if cat is None:
        return None
    rec = C.default_record(row, d, CFG, cat)
    num = rec["app_num"]
    rd = d.get("record_details", {})
    pdesc = rd.get("Project Description") or ""
    lines = [x.strip() for x in pdesc.split("\n") if x.strip()]
    rec["project_name"] = lines[0] if lines else row.get("project_name")
    rec["description"] = " ".join(lines[1:]) if len(lines) > 1 else None
    rec["parcel_ids"] = [x for x in dict.fromkeys(fmt_parcel(p) for p in d.get("parcels", [])) if x]
    size = C.to_float(C.asi_get(d, r"^Size of Land$"))
    unit = C.asi_get(d, r"^Unit of Land Measurement$") or "Acres"
    if size is not None and re.search(r"square|sq", unit, re.I):
        size = round(size / 43560.0, 3)
    rec["acres"] = size if size else C.acres_from_text(pdesc)
    if cat == "Comp Plan Amendment":
        rec["from_flu"] = C.asi_get(d, r"^Current Future Land Use Designation$")
        rec["to_flu"] = C.asi_get(d, r"^Requested Future Land Use$")
        apptype = C.asi_get(d, r"^Application Type$")
        rec["app_type"] = f"Comprehensive Plan Amendment - {apptype}" if apptype else "Comprehensive Plan Amendment"
    else:
        rec["to_zoning"] = C.asi_get(d, r"^Requested Zoning Designation$")
        a, b = C.zoning_from_text(pdesc)
        rec["from_zoning"] = a
        if not rec["to_zoning"]:
            rec["to_zoning"] = b
        ztype = C.asi_get(d, r"^Zoning Change Type$")
        rec["app_type"] = f"Rezoning - {ztype}" if ztype else "Rezoning"

    # hearings from agendas + results from workflow
    apps = sorted(_HEARINGS.get(num, []), key=lambda a: a["date"])
    for a in apps:   # agenda titles: "... from Springwood Town Homes PD (Planned Development), to R-16 (...)."
        m = re.search(r"(?:Rezon\w*|Zoning|Land Use)\b.{0,120}?\bfrom\s+(.+?),?\s+to\s+(?:the\s+)?(.+?)"
                      r"(?:\.\s*$|\.\s|\s+Project\b|\s+The\b|$)", a["name"], re.I)
        if not m:
            continue
        f_, t_ = C.one_line(m.group(1)), C.one_line(m.group(2).rstrip(". "))
        if len(f_ or "") > 150 or len(t_ or "") > 120:
            continue
        if cat == "Comp Plan Amendment":
            rec["from_flu"] = rec["from_flu"] if rec["from_flu"] and not re.match(r"\d{3,4}-", rec["from_flu"]) else f_
            rec["to_flu"] = rec["to_flu"] or t_
        else:
            rec["from_zoning"] = rec["from_zoning"] or f_
            rec["to_zoning"] = rec["to_zoning"] or t_
        break
    hearings = []
    for a in apps:
        body = "Planning Board" if a["body"] == "Planning Board" else "City Commission"
        label = body if not a["reading"] else f"City Commission ({a['reading']} reading)"
        h = {"body": label, "date": a["date"], "result": None, "_body": body}
        if not any(x["body"] == h["body"] and x["date"] == h["date"] for x in hearings):
            hearings.append(h)
    wf = workflow_results(d)
    decision = None
    for body, word, when in sorted(wf, key=lambda x: x[2]):
        cands = [h for h in hearings if h["_body"] == body and h["date"] <= when]
        res = result_label(body, word)
        if cands:
            h = cands[-1]
            if h["result"] is None or res not in ("Continued",):
                h["result"] = res
            if body == "City Commission" and res in ("Approved", "Denied"):
                decision = h["date"]
        elif body == "City Commission" and res in ("Approved", "Denied"):
            decision = when
    for i, h in enumerate(hearings):   # a first reading followed by a second reading passed
        if h["result"] is None and "first reading" in h["body"] and \
                any("second reading" in x["body"] for x in hearings[i + 1:]):
            h["result"] = "Approved (first reading)"
    for h in hearings:
        h.pop("_body", None)
    rec["hearings"] = hearings

    raw = rec["status_raw"] or ""
    today = dt.date.today().isoformat()
    cc_final = [w for w in wf if w[0] == "City Commission" and result_label(*w[:2]) in ("Approved", "Denied")]
    future = [h for h in hearings if h["date"] >= today]
    if re.search(r"void|withdr|cancel|expired", raw, re.I):
        status = "Withdrawn"
    elif re.search(r"denied", raw, re.I) or (cc_final and result_label(*cc_final[-1][:2]) == "Denied"):
        status = "Denied"
    elif re.search(r"City Commission Approved|^Approved", raw, re.I) or \
            (re.search(r"^Complete", raw, re.I) and cc_final):
        status = "Approved"
    elif re.search(r"^Complete", raw, re.I):
        status = "Approved" if hearings else "Filed"
    elif future:
        status = "Hearing Scheduled"
    elif hearings and hearings[-1]["result"] == "Continued":
        status = "Continued"
    else:
        status = "Filed"
    rec["status"] = status
    if status in ("Approved", "Denied"):
        rec["decision_date"] = decision or (hearings[-1]["date"] if hearings else None)
    if not rec["filed_date"]:
        rec["filed_date"] = C.earliest_workflow_date(d)
    appl = next((a["applicant"] for a in apps if a.get("applicant")), None)
    if appl:
        rec["applicant"] = appl
    # stable link to the agenda packet holding the staff report (attachment blob links expire)
    pk = [a["packet"] for a in reversed(apps) if a.get("staff") and a.get("packet")] or \
        [a["packet"] for a in reversed(apps) if a.get("packet")]
    rec["docs_url"] = f"{CC_API}/Meetings/GetMeetingFileStream(fileId={pk[0]},plainText=false)" if pk else None
    if rec["description"] and len(rec["description"]) > 400:
        rec["description"] = rec["description"][:397].rstrip() + "..."
    return rec


CFG["prefilter"] = prefilter
_base_fetch = C.make_fetch(CFG, build)


def fetch(since, refresh=None):
    refresh = [r for r in (refresh or []) if r and r.strip()]
    today = dt.date.today()
    start = min(dt.date.fromisoformat(since) - dt.timedelta(days=14), today - dt.timedelta(days=MIN_SCAN_DAYS))
    if refresh:
        start = min(start, today - dt.timedelta(days=420))
    end = today + dt.timedelta(days=LOOKAHEAD_DAYS)
    _HEARINGS.clear()
    try:
        _HEARINGS.update(scan_agendas(start.isoformat(), end.isoformat()))
    except Exception as e:  # noqa: BLE001
        log(f"CivicClerk scan failed ({e}); continuing with Accela only")
    return _base_fetch(since, refresh)


if __name__ == "__main__":
    C.run_cli(fetch)
