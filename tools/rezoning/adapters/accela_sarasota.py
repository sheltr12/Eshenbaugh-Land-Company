"""Sarasota County - Accela Citizen Access (agency SARASOTACO, "Community Development" tab =
module Planning).

Record types (search-form "Record Type" dropdown) and mapping:
  Planning/Rezoning/NA/NA          "Rezone / Special Exception" (PLN-REZSE-yy-nnnnnn)
        Kept only when the project is a rezone ("RZ yy-nn ..." in the project name, or the
        description says rezone); pure special exceptions ("SE nnnn ...") are skipped.
        -> PD        when the proposed district is a planned district (PUD, PCD, PID, PED, PMI,
                     OPI/PD, ...), incl. PD-to-PD amendments
        -> Rezoning  otherwise
  Planning/General Plan/Amendment/NA "General Plan Amendment" (PLN-AMEND-yy-nnnnnn)
        -> Comp Plan Amendment when it is a map amendment (ASI "Comprehensive Plan Map
           Amendment" = Yes, or "SA-"/"CPA" small/large-scale amendment names); text-only and
           Critical Area Plan (CAP) amendments without a map change are skipped.
Out of scope / not searched: Community Development Amendment Request (permit amendments),
  Approved Plan Amendment (site/development plan revisions), Development/Construction
  Submittals, Plats, Variances (BZA / LDS), DOCC/DRI, NOPC, Development Agreement/CDD, Business
  Use Permit, vacations, environmental permits, etc.

Hearings: workflow tasks "Planning Commission Hearing" and "BCC Hearing" (outcome + date marked).
Parcel IDs: 10-digit Sarasota PA ids as shown (e.g. 0414100005).
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import accela_core as C  # noqa: E402

JURISDICTIONS = ["Sarasota County"]

CFG = {
    "agency": "SARASOTACO", "module": "Planning", "tab": "Planning",
    "jurisdiction": "Sarasota County", "county": "Sarasota", "source": "Sarasota County Accela",
    "types": {
        "Planning/Rezoning/NA/NA": None,
        "Planning/General Plan/Amendment/NA": "Comp Plan Amendment",
    },
    "chunk_days": 400,
}

PD_RX = re.compile(r"\bP(U|C|I|E|M|T|R)D\b|\bPMI\b|/PD\b|\bPD\b|planned", re.I)


def _text(row, d):
    return " ".join(x for x in [row.get("project_name"), row.get("description"),
                                d.get("record_details", {}).get("Project Description")] if x)


def _kind(row, d):
    t = (d.get("type") or row.get("type") or "").lower()
    if "general plan" in t:
        return "cpa"
    if "rezone" in t:
        return "rz"
    return None


def prefilter(row, d):
    k = _kind(row, d)
    txt = _text(row, d)
    if k == "rz":
        return bool(re.search(r"\bRZ\s*\d|\brezon", txt, re.I))
    if k == "cpa":
        flag = (C.asi_get(d, r"Comprehensive Plan Map Amendment") or "").lower() == "yes"
        name = (row.get("project_name") or txt).strip()
        if flag or re.match(r"^(SA|CPA)\s*[-_ ]?\s*\d", name, re.I) or re.search(r"map amendment|FLUM", txt, re.I):
            return True
        return False
    return False


def build(row, d, aca):
    if row.get("_tval") is None and not prefilter(row, d):
        return None
    k = _kind(row, d)
    rz = next((r for r in d.get("asit", []) if re.search(r"rezone|comprehensive plan", r.get("table") or "", re.I)), {})
    lu = next((r for r in d.get("asit", []) if re.search(r"land use change", r.get("table") or "", re.I)), {})
    to_z = rz.get("Proposed Zone District")
    if k == "cpa":
        cat = "Comp Plan Amendment"
    else:
        cat = "PD" if PD_RX.search(to_z or "") else "Rezoning"
    rec = C.default_record(row, d, CFG, cat)
    txt = _text(row, d)
    rec["parcel_ids"] = list(dict.fromkeys(d.get("parcels", [])))
    rec["acres"] = C.to_float(rz.get("Land Area (acres)") or C.asi_get(d, r"Total Land Area", r"\bacres?\b")) \
        or C.acres_from_text(txt)
    fz, tz = C.zoning_from_text(txt)
    rec["from_zoning"] = rz.get("Current Zone District") or fz
    rec["to_zoning"] = to_z or tz
    if cat == "Comp Plan Amendment":
        rec["from_flu"] = lu.get("Current Future Land Use") or rz.get("Current Future Land Use") or \
            C.asi_get(d, r"Current Future Land Use")
        rec["to_flu"] = lu.get("Proposed Future Land Use") or rz.get("Proposed Future Land Use") or \
            C.asi_get(d, r"Proposed Future Land Use")
        if not (rec["from_flu"] or rec["to_flu"]):
            rec["from_flu"], rec["to_flu"] = fz, tz
            rec["from_zoning"] = rz.get("Current Zone District")
            rec["to_zoning"] = to_z
    hs = C.hearings_from_workflow(d)
    rec["hearings"] = hs
    st = C.norm_status(rec["status_raw"], hs, raw_map={
        r"Board Hearing Scheduled|^Hearing Scheduled$": "Hearing!",
        r"PC Recommended": None,
    })
    rec["status"] = st
    rec["decision_date"] = C.decision_date(hs, st)
    return rec


CFG["prefilter"] = prefilter
fetch = C.make_fetch(CFG, build)

if __name__ == "__main__":
    C.run_cli(fetch)
