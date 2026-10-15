"""Manatee County - Accela Citizen Access (agency MANATEE, module Planning).

Record types (search-form "Record Type" dropdown) and mapping:
  Planning/Application/Rezoning/NA                 "A10 - Zoning Atlas Amendment"  -> Rezoning
        (-> PD when the proposed zoning designation is a planned district, PD-xx)
  Planning/Application/Planned Unit Development/NA "A02 - Planned Development"     -> PD
        only when ASI "Type of Planned Development" is a public-hearing request (new PD /
        General Development Plan with Rezone / PD zoning ordinance amendment). Skipped: the
        administrative kinds ("... (Admin)", Final Site Plan, Changes to Approved Site Plan,
        Preliminary Site Plan without rezone, etc.).
  Planning/Application/Plan Amendment/NA           "A08 - Comprehensive Plan Amendment"
        -> Comp Plan Amendment (map amendments; text-only amendments without parcels skipped)
Out of scope / not searched: A12 Special Permit, A17 Variances, A04 Site Plan, plats, A19 Local
  Development Agreement, A20 CDD, A21 DRI, A26 Appeal, A99 Misc, Modification/Field Change, etc.

Portal quirk: Manatee's general search form has NO date fields; results are returned newest
first, so the adapter pages until a whole page is older than `since`.
Parcel IDs: from the parcel attribute "PARCEL_ID" (Manatee PA format, e.g. 568810204); the
Accela parcel key ("5688102040000-3953125385") is only used as a fallback.
Hearings: Manatee's public workflow is sparse ("Hearing Complete" -> "Public Hearing Approved");
the deciding hearing is reported as BOCC with the date the outcome was recorded.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import accela_core as C  # noqa: E402

JURISDICTIONS = ["Manatee County"]

CFG = {
    "agency": "MANATEE", "module": "Planning", "tab": "Planning",
    "jurisdiction": "Manatee County", "county": "Manatee", "source": "Manatee Accela",
    "types": {
        "Planning/Application/Rezoning/NA": "Rezoning",
        "Planning/Application/Planned Unit Development/NA": "PD",
        "Planning/Application/Plan Amendment/NA": "Comp Plan Amendment",
    },
}

# "Type of Planned Development" values seen: General Development Plan with Rezone (Public Hearing),
# Preliminary Site Plan with Rezone (Public Hearing), General Development Plan (Public Hearing),
# Changes to Approved Site Plan (Public Hearing) [= PD amendment]  -> kept;  Preliminary Site Plan (Public Hearing) [site plan in an existing PD],
# Changes to Approved Site Plan (Admin) / - Limited Review (Admin), Preliminary Site Plan/ Final
# Site Plan (Admin) -> skipped.
PD_IN = re.compile(r"rezone|zoning ordinance|ordinance amend|changes to approved.*public hearing|"
                   r"general development plan", re.I)
PD_OUT = re.compile(r"\(admin\)|administrative|limited review|field change|minor", re.I)


def _cat(row, d):
    cat = CFG["types"].get(row.get("_tval"))
    if cat:
        return cat
    t = (d.get("type") or "").lower()
    if "zoning atlas" in t:
        return "Rezoning"
    if "planned development" in t:
        return "PD"
    if "comprehensive plan" in t:
        return "Comp Plan Amendment"
    return None


def prefilter(row, d):
    cat = _cat(row, d)
    if cat == "PD":
        kind = C.asi_get(d, r"^Type of Planned Development$") or ""
        return bool(PD_IN.search(kind)) and not PD_OUT.search(kind)
    if cat == "Comp Plan Amendment":
        kind = C.asi_get(d, r"^Type of Requested Amendment$") or ""
        if re.search(r"text", kind, re.I) and not re.search(r"map", kind, re.I) and not d.get("parcels"):
            return False
    return cat is not None


def build(row, d, aca):
    cat = _cat(row, d)
    if cat is None or (row.get("_tval") is None and not prefilter(row, d)):
        return None
    prop = C.asi_get(d, r"^Proposed Zoning Designation$", r"^Proposed Zoning")
    if cat == "Rezoning" and prop and re.match(r"^PD", prop.strip(), re.I):
        cat = "PD"
    rec = C.default_record(row, d, CFG, cat)
    pa = d.get("parcel_attrs", [])
    pids = []
    for a in pa:
        v = a.get("PARCEL_ID")
        if not v and a.get("Parcel Number"):
            v = re.sub(r"000$", "", a["Parcel Number"].split("-")[0])
        if v and v not in pids:
            pids.append(v)
    rec["parcel_ids"] = pids
    area = C.to_float(C.asi_get(d, r"^Total Project Area$"))
    unit = (C.asi_get(d, r"^Total Project Area Unit$") or "Acres").lower()
    if area and unit.startswith("sq"):
        area = round(area / 43560.0, 3)
    if not area:
        tot = sum(C.to_float(a.get("PARCELAREA")) or 0 for a in pa)
        area = round(tot, 3) if tot else None
    rec["acres"] = area
    cur_z = C.asi_get(d, r"^Current Zoning Designation$") or (pa[0].get("ZONING") if pa else None)
    rec["from_zoning"] = cur_z
    rec["to_zoning"] = prop
    if cat == "Comp Plan Amendment":
        rec["from_flu"] = C.asi_get(d, r"^Current Future Land Use", r"^Existing Future Land Use") or \
            (pa[0].get("FUTURE LANDUSE") if pa else None)
        rec["to_flu"] = C.asi_get(d, r"^Proposed Future Land Use")
        rec["to_zoning"] = None
    kind = C.asi_get(d, r"^Type of Planned Development$", r"^Type of Requested Amendment$", r"^Application Type$")
    if kind:
        rec["app_type"] = f"{rec['app_type']} - {kind}"
    pn = C.asi_get(d, r"^Project Number$", r"^Public Hearing/Zoning Approval Project Number$")
    bits = []
    if rec["from_zoning"] or rec["to_zoning"]:
        bits.append(f"Rezone from {rec['from_zoning'] or '?'} to {rec['to_zoning'] or '?'}")
    if rec["from_flu"] or rec["to_flu"]:
        bits.append(f"FLU from {rec['from_flu'] or '?'} to {rec['to_flu'] or '?'}")
    units = C.asi_get(d, r"Number of Residential Units - Proposed")
    nonres = C.asi_get(d, r"Non-Residential Building Floor Area - Proposed")
    if units:
        bits.append(f"{units} residential units")
    if nonres:
        bits.append(f"{nonres} SF non-residential")
    dev = C.asi_get(d, r"^Type of Development$")
    if dev:
        bits.append(dev)
    if pn:
        bits.append(f"case {pn}")
    rec["description"] = "; ".join(bits) or rec["description"]
    rec["project_name"] = row.get("project_name") or rec["project_name"]

    hs = C.hearings_from_workflow(d, default_body="BOCC", task_rx=r"^Hearing Complete$|Public Hearing")
    # "SR - Hearing Complete" in the case-manager task marks the day staff logged the hearing
    for t in d.get("workflow", []):
        for e in t.get("entries", []):
            if re.search(r"^SR - Hearing Complete$", e.get("status") or "") and e.get("date"):
                hs.append({"body": "BOCC", "date": e["date"], "result": None})
    hs = C.merge_hearings([h for h in hs if not h.get("result")], [h for h in hs if h.get("result")],
                          window_days=60)
    rec["hearings"] = hs
    st = C.norm_status(rec["status_raw"], hs, raw_map={
        r"Administratively Approved|Plans Approved": None,
        r"^Hearing Scheduled$": "Hearing Scheduled",
        r"^Hearing Complete$": None,
    })
    rec["status"] = st
    rec["decision_date"] = C.decision_date(hs, st)
    return rec


CFG["prefilter"] = prefilter
fetch = C.make_fetch(CFG, build)

if __name__ == "__main__":
    C.run_cli(fetch)
