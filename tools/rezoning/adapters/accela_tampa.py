"""City of Tampa - Accela Citizen Access (agency TAMPA, module Planning).

Record types (search-form "Record Type" dropdown) and mapping:
  Planning/Rezoning/Rezoning/NA  "Rezoning" (REZ-yy-nnnnnnn)
        -> PD        when ASI "Proposed Zoning District" is a PD district (PD, PD-A, ...)
        -> Rezoning  otherwise (Euclidean or other site-plan-controlled districts)
Out of scope / not searched: Special Use 1/2, AB (alcohol) permits, Variance Review Board,
  Substantial Change (site plan / special use), PD-A Incremental Review, Design Exceptions,
  Plats, ROW vacating, DRI NOPC / annual reports, Historic Preservation, Zoning letters, etc.
City of Tampa comprehensive plan (FLUM) amendments are processed by Plan Hillsborough, not in
this Accela portal, so this adapter yields no "Comp Plan Amendment" records.

Hearings: ASI "HEARINGS" group gives exact dates (DRC Date, First Hearing Date = City Council 1st
reading, Second Hearing Date = 2nd reading, Ordinance Number). Outcomes come from the workflow
tasks "City Council Public Hearing" / "Second City Council Public Hearing".
Parcel IDs: Accela parcel attributes carry the Hillsborough PA FOLIO (e.g. "184080.0000");
returned as "184080-0000" (falls back to the 22-char PIN when no folio is shown).
Acres: ASI "Property size" is entered in acres or square feet; values > 200 are treated as sq ft.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import accela_core as C  # noqa: E402

JURISDICTIONS = ["City of Tampa"]

CFG = {
    "agency": "TAMPA", "module": "Planning", "tab": "Planning",
    "jurisdiction": "City of Tampa", "county": "Hillsborough", "source": "City of Tampa Accela",
    "types": {"Planning/Rezoning/Rezoning/NA": None},
    "chunk_days": 400,
}


def folio(v):
    if not v:
        return None
    m = re.match(r"^\s*(\d{6})\.?(\d{4})\s*$", v)
    return f"{m.group(1)}-{m.group(2)}" if m else v.strip()


def build(row, d, aca):
    if d.get("type") and "rezoning" not in d["type"].lower():
        return None
    prop = C.asi_get(d, r"^Proposed Zoning District$")
    cur = C.asi_get(d, r"^Current Zoning District$")
    cat = "PD" if prop and re.search(r"\bPD\b|^PD|PD-A|Planned Development", prop, re.I) else "Rezoning"
    rec = C.default_record(row, d, CFG, cat)
    rec["app_type"] = f"Rezoning ({C.asi_get(d, r'^Rezoning Type$')})" if C.asi_get(d, r"^Rezoning Type$") else rec["app_type"]
    size = C.to_float(C.asi_get(d, r"^Property size$"))
    if size is not None and size > 200:
        size = round(size / 43560.0, 3)
    rec["acres"] = size
    rec["from_zoning"], rec["to_zoning"] = cur, prop
    if rec["address"]:
        rec["address"] = re.sub(r",\s*T,\s*(\d{5})", r", Tampa, FL \1", rec["address"])
    pids = []
    for a in d.get("parcel_attrs", []):
        f = folio(a.get("FOLIO")) or a.get("Parcel Number")
        if f and f not in pids:
            pids.append(f)
    for v in C.asit_values(d, r"folio"):
        f = folio(v)
        if f and f not in pids:
            pids.append(f)
    rec["parcel_ids"] = pids
    use = C.asi_get(d, r"^If requesting a PD, please state the proposed use$") or \
        C.asi_get(d, r"^Proposed Use of Land Description$") or C.asi_get(d, r"^Proposed Use of Land$")
    bits = []
    if cur or prop:
        bits.append(f"Rezone{f' {size} ac' if size else ''} from {cur or '?'} to {prop or '?'}")
    if use and not re.fullmatch(r"\s*(see attached.*|n\s*[/-]?\s*a|none|no pd.*|tbd|-+)\s*", use, re.I):
        bits.append(f"proposed use: {use}")
    rec["description"] = "; ".join(bits) or rec["description"]
    ordn = C.asi_get(d, r"^Ordinance Number$")
    if ordn:
        rec["description"] = (rec["description"] or "") + f" ({ordn})"

    # hearings: ASI dates + workflow outcomes per task
    def task_result(rx):
        res = None
        for t in d.get("workflow", []):
            if re.search(rx, t.get("task") or "", re.I):
                for e in t.get("entries", []):
                    if e.get("status") and not C._ADMIN_RESULT.search(e["status"]):
                        res = e["status"]
        return res

    hs = []
    drc = C.iso(C.asi_get(d, r"^DRC Date$"))
    if drc:
        hs.append({"body": "DRC", "date": drc, "result": None})
    h1 = C.iso(C.asi_get(d, r"^First Hearing Date$"))
    h2 = C.iso(C.asi_get(d, r"^Second Hearing Date$"))
    if h1:
        hs.append({"body": "City Council", "date": h1, "result": task_result(r"^City Council Public Hearing")})
    if h2:
        hs.append({"body": "City Council", "date": h2, "result": task_result(r"^Second City Council Public Hearing")})
    # outcomes recorded in workflow without ASI dates
    wf = C.hearings_from_workflow(d, skip_rx=r"Planning Commission Review")
    hs = C.merge_hearings(hs, wf, window_days=120)
    # a workflow outcome dated the same day as the 2nd reading but belonging to the first is a dup
    rec["hearings"] = hs
    raw = rec["status_raw"] or ""
    st = C.norm_status(raw, hs, final_bodies=("City Council",), raw_map={
        r"^(Complete|Closed)$": None, r"In Process|Open|Awaiting": None})
    if raw in ("Complete", "Closed") and st in ("Filed", "Hearing Scheduled", "Continued"):
        closure = task_result(r"^Closure$") or ""
        if re.search(r"withdr|incomplete|closed", closure, re.I) or raw == "Closed":
            st = "Withdrawn"
        elif re.search(r"approv", closure, re.I):
            st = "Approved"
        elif re.search(r"den", closure, re.I):
            st = "Denied"
    rec["status"] = st
    rec["decision_date"] = C.decision_date(hs, st, final_bodies=("City Council",))
    return rec


fetch = C.make_fetch(CFG, build)

if __name__ == "__main__":
    C.run_cli(fetch)
