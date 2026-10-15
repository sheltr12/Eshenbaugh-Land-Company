"""Pinellas County (unincorporated) - Accela Citizen Access (agency PINELLAS, module Planning).

Record types (search-form "Record Type" dropdown) and mapping:
  Planning/Zoning/Change/NA                 "Zoning Change" (ZON-yy-nnnnn)
        -> PD        when the requested district is a planned district (RPD, MPD, CPD, IPD, PD...)
        -> Rezoning  otherwise
  Planning/Zoning/Land Use/Plan Change      "Future Land Use Change" (FLU-yy-nnnnn) -> Comp Plan Amendment
  Planning/Zoning/Development Master Plan/NA "Development Master Plan" (DMP-yy-nnnnn) -> PD
        (new / modified master plans for planned developments; heard by LPA + BCC)
Out of scope / not searched: Type 2 Use, Type 3 Use (conditional use), Variance, Board of
  Adjustment extension, Nonconforming Use, Certificate of Appropriateness, Development
  Agreements, Annexation.
"yyTMP-..." records (unsubmitted online drafts) are skipped.

Hearings come from the workflow: "DRC Meeting" (DRC), "LPA Public Hearing" (LPA = Local Planning
Agency), "BOCC" (final action). Staff write the actual meeting date in the comment ("On August 13,
2025, the LPA voted ..."), which is used as the hearing date.
Parcel IDs: Accela shows 18 digits; returned in Pinellas PA format 10-27-16-00000-430-0100.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import accela_core as C  # noqa: E402

JURISDICTIONS = ["Pinellas County"]

CFG = {
    "agency": "PINELLAS", "module": "Planning", "tab": "Planning",
    "jurisdiction": "Pinellas County", "county": "Pinellas", "source": "Pinellas Accela",
    "types": {
        "Planning/Zoning/Change/NA": None,
        "Planning/Zoning/Land Use/Plan Change": "Comp Plan Amendment",
        "Planning/Zoning/Development Master Plan/NA": "PD",
    },
    "chunk_days": 400,
}


def fmt_parcel(p):
    p = re.sub(r"\D", "", p or "")
    if len(p) == 18:
        return f"{p[0:2]}-{p[2:4]}-{p[4:6]}-{p[6:11]}-{p[11:14]}-{p[14:18]}"
    return p or None


def body_map(name):
    if re.search(r"\bLPA\b|local planning agency", name or "", re.I):
        return "LPA"
    return C.hearing_body(name)


def build(row, d, aca):
    t = (d.get("type") or row.get("type") or "").lower()
    to_z = C.asi_get(d, r"requested Zoning District")
    if "land use" in t:
        cat = "Comp Plan Amendment"
    elif "master plan" in t:
        cat = "PD"
    elif "zoning change" in t:
        cat = "PD" if to_z and re.search(r"\b[RCIM]?PD\b|planned", to_z, re.I) else "Rezoning"
    else:
        return None
    rec = C.default_record(row, d, CFG, cat)
    txt = " ".join(x for x in [rec["description"], C.asi_get(d, r"proposed use")] if x)
    rec["parcel_ids"] = [x for x in dict.fromkeys(fmt_parcel(p) for p in d.get("parcels", [])) if x]
    rec["from_zoning"] = C.asi_get(d, r"current Zoning District")
    rec["to_zoning"] = to_z
    rec["from_flu"] = C.asi_get(d, r"current Future Land Use", r"Future Land Use Category on the property")
    rec["to_flu"] = C.asi_get(d, r"requested Future Land Use")
    if cat != "Comp Plan Amendment":
        rec["from_flu"] = rec["to_flu"] = None
    rec["acres"] = C.to_float(C.asi_get(d, r"\bacre")) or C.acres_from_text(txt)
    use = C.asi_get(d, r"Describe the proposed use")
    if use and rec["description"] and use.lower() not in rec["description"].lower():
        rec["description"] = f"{rec['description']} (proposed: {use})"
    hs = C.hearings_from_workflow(d, body_map=body_map)
    hs = C.merge_hearings(hs, [h for h in C.hearings_from_comments(d) if h["body"] != "Planning Commission"],
                          window_days=3)
    rec["hearings"] = hs
    st = C.norm_status(rec["status_raw"], hs, final_bodies=("BOCC",), raw_map={
        r"^Hearing Scheduled$": "Hearing Scheduled", r"Waiting|In Review": None})
    rec["status"] = st
    rec["decision_date"] = C.decision_date(hs, st, final_bodies=("BOCC",))
    return rec


fetch = C.make_fetch(CFG, build)

if __name__ == "__main__":
    C.run_cli(fetch)
