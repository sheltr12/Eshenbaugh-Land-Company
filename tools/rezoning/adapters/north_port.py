"""City of North Port (Sarasota County) - Accela Citizen Access (agency NORTHPORT, module Planning)
plus the city's Legistar web API (cityofnorthport) for hearing dates and outcomes.

Record types (search-form "Record Type" dropdown) and mapping:
  Planning/Rezone/NA/NA                        "Rezone" (PRZ-yy-nnnnn)
        -> PD        when the proposed district is a planned/village district (PCD, V, PUD, ...)
        -> Rezoning  otherwise
  Planning/Rezone Village/NA/NA                "Rezone, Village"                    -> PD
  Planning/Comprehensive Plan Amendments/Map/NA "Future Land Use Map Amendment" (PFLU-) -> Comp Plan Amendment
  Planning/Comprehensive Plan Amendments/NA/NA "Comprehensive Plan Amendment" (PCPA-)
        -> Comp Plan Amendment only when it is tied to land (parcels, a land use / rezone /
           development request); privately initiated text amendments and mis-filed permit
           requests (generators, pools, ...) are skipped.
  Planning/Conceptual Plan/Amendment/NA        "Master Concept Plan Amendment" (PMCPA-)
        -> PD only for amendments to a planned district's governing plan (Village District
           Pattern Plan / VDPP, Planned Community Development / PCD, Development Master Plan);
           ordinary commercial site concept plan changes (drive-thrus, parking lots) are skipped.
Out of scope / not searched: Annexation (the concurrent PFLU / PRZ records are kept), Special
  Exception, Variance, Waiver, Site Development Plan, Master Concept Plan (new), plats, ULDC text
  amendments, Development Agreements, Zoning Compliance, etc.

Hearings: Accela workflow tasks "PZAB Hearing", "CC First Reading", "CC Adoption Hearing" (status
+ date marked), merged with Legistar matters whose File # or title carries the case number
(Planning & Zoning Advisory Board / City Commission agenda items): Legistar histories give the
actual hearing dates and actions, MatterAgendaDate gives a scheduled future hearing.
Parcel IDs: 10-digit Sarasota County PA ids as posted (e.g. 0840001010).
Note: North Port adopted a temporary moratorium (Ord. 2026-25, July 2026) on accepting rezonings
and comp plan amendments, so few new filings appear after mid-2026.
"""
import datetime as dt
import os
import re
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import accela_core as C  # noqa: E402

JURISDICTIONS = ["City of North Port"]
LEG = "https://webapi.legistar.com/v1/cityofnorthport"
LEG_SITE = "https://cityofnorthport.legistar.com"
PZAB, CC = "Planning & Zoning Advisory Board", "City Commission"

CFG = {
    "agency": "NORTHPORT", "module": "Planning", "tab": "Planning",
    "jurisdiction": "City of North Port", "county": "Sarasota",
    "source": "North Port Accela + Legistar",
    "types": {
        "Planning/Rezone/NA/NA": None,
        "Planning/Rezone Village/NA/NA": "PD",
        "Planning/Comprehensive Plan Amendments/Map/NA": "Comp Plan Amendment",
        "Planning/Comprehensive Plan Amendments/NA/NA": "Comp Plan Amendment",
        "Planning/Conceptual Plan/Amendment/NA": "PD",
    },
    "chunk_days": 400,
}

PD_RX = re.compile(r"\bP(U|C|I|E|M|T|R)D\b|\bV\b|village|planned", re.I)
PD_PLAN_RX = re.compile(r"pattern plan|\bV?DPP\b|planned community|\bPCD\b|development master plan|\bDMA\b|"
                        r"village (?:district|[a-z]\b)", re.I)
JUNK_RX = re.compile(r"generator|\bpool\b|fence|\bshed\b|lanai|\bcage\b|roof|driveway|culvert|cement pad|"
                     r"single family home|\bSFR\b", re.I)
LAND_RX = re.compile(r"land use|\bFLU|rezon|map amendment|mixed[- ]use|development|annex|acre", re.I)
TEXT_RX = re.compile(r"text amendment", re.I)

_leg = requests.Session()
_leg.headers.update({"User-Agent": C.UA, "Accept": "application/json"})


def log(*a):
    print("[north_port]", *a, file=sys.stderr, flush=True)


def _text(row, d):
    rd = d.get("record_details", {})
    narr = C.asi_get(d, r"narrative|description of the project|description for the application")
    return " ".join(x for x in [row.get("project_name"), row.get("description"), rd.get("Project Description"), narr] if x)


def _kind(row, d):
    t = row.get("_tval") or ""
    if not t:
        ty = (d.get("type") or row.get("type") or "").lower()
        if "village" in ty and "rezone" in ty:
            return "rzv"
        if "rezone" in ty:
            return "rz"
        if "future land use" in ty:
            return "flum"
        if "comprehensive plan" in ty:
            return "cpa"
        if "master concept plan amendment" in ty:
            return "mcpa"
        return None
    return {"Planning/Rezone/NA/NA": "rz", "Planning/Rezone Village/NA/NA": "rzv",
            "Planning/Comprehensive Plan Amendments/Map/NA": "flum",
            "Planning/Comprehensive Plan Amendments/NA/NA": "cpa",
            "Planning/Conceptual Plan/Amendment/NA": "mcpa"}.get(t)


def prefilter(row, d):
    k = _kind(row, d)
    txt = _text(row, d)
    if k is None:
        return False
    if k in ("rz", "rzv", "flum"):
        return not (JUNK_RX.search(txt) and not LAND_RX.search(txt))
    if k == "cpa":
        if TEXT_RX.search(txt) and not re.search(r"map|future land use map|rezon", txt, re.I):
            return False
        if JUNK_RX.search(txt) and not LAND_RX.search(txt):
            return False
        return bool(LAND_RX.search(txt) or d.get("parcels"))
    if k == "mcpa":
        return bool(PD_PLAN_RX.search(txt))
    return False


# ---------------------------------------------------------------- Legistar
def _lget(path, params=None):
    last = None
    for i in range(3):
        try:
            r = _leg.get(f"{LEG}/{path}", params=params, timeout=60)
            r.raise_for_status()
            time.sleep(0.3)
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"Legistar {path}: {last}")


def _leg_body(name):
    n = (name or "").lower()
    if "planning" in n:
        return PZAB
    if "commission" in n:
        return CC
    return None


def _leg_result(action, body):
    a = (action or "").strip()
    if not a:
        return None
    al = a.lower()
    if body == PZAB:
        if "approv" in al:
            return "Recommended Approval"
        if "den" in al:
            return "Recommended Denial"
    if re.search(r"continu|defer|postpone|tabled", al):
        return "Continued"
    if re.search(r"approved on first reading|first reading", al) and "approv" in al:
        return "Approved on first reading"
    if re.search(r"adopt|approv|passed", al):
        return "Approved"
    if re.search(r"den|fail", al):
        return "Denied"
    if re.search(r"withdr", al):
        return "Withdrawn"
    return a[:1].upper() + a[1:]


def legistar_info(app_num, today):
    """Hearings from Legistar matters that carry the case number; plus a link to the first one."""
    num = app_num.replace("'", "''")
    flt = f"substringof('{num}',MatterFile) or substringof('{num}',MatterTitle)"
    try:
        ms = _lget("matters", {"$filter": flt, "$top": 50})
    except Exception as e:  # noqa: BLE001
        log(f"{app_num}: legistar lookup failed: {e}")
        return [], None
    hs, link = [], None
    for m in ms:
        body0 = _leg_body(m.get("MatterBodyName"))
        try:
            hist = _lget(f"matters/{m['MatterId']}/histories")
        except Exception as e:  # noqa: BLE001
            log(f"{app_num}: histories {m['MatterId']}: {e}")
            hist = []
        seen_dates = set()
        for h in hist:
            d = (h.get("MatterHistoryActionDate") or "")[:10]
            b = _leg_body(h.get("MatterHistoryActionBodyName")) or body0
            if not d or not b:
                continue
            if re.search(r"not applicable", h.get("MatterHistoryActionName") or "", re.I):
                continue
            res = _leg_result(h.get("MatterHistoryActionName"), b)
            if res and re.search(r"^(Referred|Filed|Accepted|Introduced|Presented|Not Applicable|Read by title|"
                                 r"Received|No Action)", res, re.I):
                res = "First reading" if re.search(r"^Read by title", res, re.I) else None
            hs.append({"body": b, "date": d, "result": res})
            seen_dates.add(d)
        ad = (m.get("MatterAgendaDate") or "")[:10]
        if ad and body0 and ad not in seen_dates and ad >= today:
            hs.append({"body": body0, "date": ad, "result": None})
        if link is None and m.get("MatterId"):
            link = m
    return hs, link


def _wf_body(name):
    n = name or ""
    if re.search(r"PZAB|planning|zoning advisory", n, re.I):
        return PZAB
    if re.search(r"\bCC\b|commission", n, re.I):
        return CC
    return None


def _wf_hearings(d):
    hs = C.hearings_from_workflow(d, task_rx=r"PZAB|^CC\b|Commission", body_map=_wf_body)
    out = []
    for h in hs:
        r = h.get("result") or ""
        if re.search(r"^(Scheduled|Pending|Ready|In Progress|Not Required|Note|Received)", r, re.I):
            r = None
        elif h["body"] == PZAB and re.search(r"approv", r, re.I) and not re.search(r"recommend", r, re.I):
            r = "Recommended Approval"
        elif h["body"] == PZAB and re.search(r"den", r, re.I) and not re.search(r"recommend", r, re.I):
            r = "Recommended Denial"
        out.append({"body": h["body"], "date": h["date"], "result": r or None})
    return [h for h in out if h["result"]]


def build(row, d, aca):
    if row.get("_tval") is None and not prefilter(row, d):
        return None
    k = _kind(row, d)
    txt = _text(row, d)
    to_z = C.asi_get(d, r"Proposed Zoning", r"Requested Zoning")
    from_z = C.asi_get(d, r"Current Zoning", r"Existing Zoning")
    if k in ("flum", "cpa"):
        cat = "Comp Plan Amendment"
    elif k in ("rzv", "mcpa"):
        cat = "PD"
    else:
        cat = "PD" if PD_RX.search(to_z or "") else "Rezoning"
    rec = C.default_record(row, d, CFG, cat)
    pids = []
    for p in list(d.get("parcels", [])) + [r.get("Parcel Number") for r in d.get("asit", [])
                                           if re.search(r"parcel", r.get("table") or "", re.I)]:
        p = (p or "").strip()
        if re.fullmatch(r"\d{10}", p) and p not in pids and not re.fullmatch(r"9+|0+", p):
            pids.append(p)
    for p in re.findall(r"(?<!\d)\d{10}(?!\d)", C.asi_get(d, r"narrative") or ""):
        if p not in pids:
            pids.append(p)
    rec["parcel_ids"] = pids
    ac = C.to_float(C.asi_get(d, r"^Total Acreage$"))
    if ac and ac > 5000:  # some applicants enter square feet (the sum of the parcel areas)
        ac = round(ac / 43560.0, 2)
    rec["acres"] = ac or C.acres_from_text(txt)
    fz, tz = C.zoning_from_text(txt)
    rec["from_zoning"] = from_z or (fz if cat != "Comp Plan Amendment" else None)
    rec["to_zoning"] = to_z or (tz if cat != "Comp Plan Amendment" else None)
    if cat == "Comp Plan Amendment":
        rec["from_flu"] = C.asi_get(d, r"Current Future Land Use", r"Existing Future Land Use")
        rec["to_flu"] = C.asi_get(d, r"Proposed Future Land Use")
    ent = C.asi_get(d, r"^Entity Name$")
    rd = d.get("record_details", {})
    appl = rd.get("Applicant")
    if appl and re.fullmatch(r"City of North Port|Organization|Individual|Applicant", appl, re.I):
        appl = ent if ent else (None if not re.fullmatch(r"City of North Port", appl, re.I) else appl)
    appl = appl or ent or rec.get("applicant")
    if appl:
        appl = re.sub(r"\S+@\S+", " ", appl)
        appl = re.sub(r"\s+", " ", appl).strip(" ,;") or None
    rec["applicant"] = appl
    narr = C.asi_get(d, r"narrative|description of the project|description for the application")
    if narr and (not rec.get("description") or len(rec["description"]) < len(narr)):
        rec["description"] = narr
    if rec.get("description") and rec.get("project_name") and rec["description"].strip() == rec["project_name"].strip():
        rec["description"] = None
    if rec.get("address") and (re.search(r"ADDRESS (NOT PLATTED|NEEDED)", rec["address"], re.I)
                               or re.fullmatch(r"0(\s+[\d\s]*)?", rec["address"].strip())):
        rec["address"] = None
    today = dt.date.today().isoformat()
    wf = _wf_hearings(d)
    lh, lm = legistar_info(rec["app_num"], today)
    # Legistar has the real meeting dates; workflow rows carry the date staff marked the task, so
    # they are used only for a body Legistar has nothing for
    lbodies = {h["body"] for h in lh}
    hs = C.dedup_hearings(lh + [h for h in wf if h["body"] not in lbodies])
    rec["hearings"] = hs
    st = C.norm_status(rec["status_raw"], hs, final_bodies=(CC,), raw_map={
        r"^Pending CC Decision$": "Hearing!",
        r"^Closed$": None,
        r"Awaiting Plans|Routed for Review|Revisions Required|Additional Info|Waiting for|Submitted|"
        r"In Review|Under Review": None,
    })
    if st == "Filed" and re.fullmatch(r"Closed", rec["status_raw"] or "", re.I):
        if any(h["body"] == CC and re.search(r"^(Approved|Adopted)", h.get("result") or "", re.I) for h in hs):
            st = "Approved"
        else:
            # Closed without a recorded City Commission adoption: completed elsewhere (e.g. the
            # adopting ordinance carried a different petition) or closed out -> keep as Approved only
            # when an adoption step is in the workflow
            adopted = any(re.search(r"adopt|approv", e.get("status") or "", re.I)
                          for t in d.get("workflow", []) if re.search(r"adoption|CC|commission", t.get("task") or "", re.I)
                          for e in t.get("entries", []))
            st = "Approved" if adopted else "Withdrawn"
    rec["status"] = st
    dec = C.decision_date(hs, st, final_bodies=(CC,))
    if st == "Approved" and not dec:
        ds = [e["date"] for t in d.get("workflow", []) if re.search(r"adoption", t.get("task") or "", re.I)
              for e in t.get("entries", []) if e.get("date")]
        dec = max(ds) if ds else None
    rec["decision_date"] = dec
    if lm is not None:
        try:
            import legistar_links as LL  # noqa: E402
            rec["docs_url"] = LL.site_link(LEG, [lm], meeting_fallback=True)
        except Exception as e:  # noqa: BLE001
            log(f"{rec['app_num']}: legistar link failed: {e}")
    return rec


CFG["prefilter"] = prefilter
fetch = C.make_fetch(CFG, build)

if __name__ == "__main__":
    C.run_cli(fetch)
