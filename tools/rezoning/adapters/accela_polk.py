"""Polk County - Accela Citizen Access (agency POLKCO, module "LandDev" = Land Development tab).

Record types (search-form "Record Type" dropdown) and mapping:
  LandDev/BOCC/LDC District Change/NA        "BOCC-LDC District Change" (LDCD-yyyy-n) -> Rezoning
  LandDev/BOCC/PD/NA                         "BOCC-Planned Development" (LDPD-)       -> PD
  LandDev/PC/Planned Development/NA          "PC-Planned Development"   (LDPD-)       -> PD
  LandDev/PC/Suburban Planned Development/NA "PC-Suburban Planned Development"        -> PD
  LandDev/PC/Major Modifications/NA          "PC-Major Modifications"   (LDPD-)       -> PD
        (only PD major modifications; ASI "Application Type" = Conditional Use ... is skipped)
  LandDev/BOCC/CPA Small/NA                  "BOCC-CPA Small" (LDCPAS-)  -> Comp Plan Amendment
  LandDev/BOCC/CPA Large/NA                  "BOCC-CPA Large" (LDCPAL-)  -> Comp Plan Amendment
        (county-initiated text amendments / new-designation policy changes are skipped)
Out of scope / not searched: BOCC/PC Conditional Use, BOA & LUHO special exceptions / variances,
  DRC site plans / plats / waivers, Prescriptive Planned Development (administrative, DRC level),
  CDD, DRI, Developer's / Infrastructure Agreements, LDC Text Change, Admin determinations.
Note: Polk "Planned Developments" are approved by the Planning Commission (BOCC only on appeal
or when combined with a CPA); ASI "Development Type" tells which board decides.

Hearings: exact dates from ASI "MEETING DATES" (Planning Commission Date, 1st/2nd BOCC Date) and
"DRC Meeting"; results from ASI "HEARING" (Hearing Results = PC, BOCC 1st/2nd Hearing Results)
plus workflow tasks "Planning Commision" / "BOCC Hearing".
Parcel IDs: 18 digits -> Polk PA format 25-32-33-488000-003010.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import accela_core as C  # noqa: E402

JURISDICTIONS = ["Polk County"]

CFG = {
    "agency": "POLKCO", "module": "LandDev", "tab": "LandDev",
    "jurisdiction": "Polk County", "county": "Polk", "source": "Polk Accela",
    "types": {
        "LandDev/BOCC/LDC District Change/NA": "Rezoning",
        "LandDev/BOCC/PD/NA": "PD",
        "LandDev/PC/Planned Development/NA": "PD",
        "LandDev/PC/Suburban Planned Development/NA": "PD",
        "LandDev/PC/Major Modifications/NA": "PD",
        "LandDev/BOCC/CPA Small/NA": "Comp Plan Amendment",
        "LandDev/BOCC/CPA Large/NA": "Comp Plan Amendment",
    },
    "chunk_days": 400,
}

TEXT_ONLY = re.compile(r"text amendment|text cpa|\btext\b.*(amend|change)|new future land use designation|"
                       r"modifications? to section|amend(ment)? to section|section \d+\.\d+", re.I)
MAP_HINT = re.compile(r"\bmap\b|FLUM|\bfrom\s+[A-Z][A-Z0-9/\-]*\s+to\s+[A-Z]|future land use (map )?amendment|"
                      r"land use change|to assign|small[- ]scale", re.I)


def fmt_parcel(p):
    p = re.sub(r"\D", "", p or "")
    if len(p) == 18:
        return f"{p[0:2]}-{p[2:4]}-{p[4:6]}-{p[6:12]}-{p[12:18]}"
    return p or None


def _cat(row, d):
    cat = CFG["types"].get(row.get("_tval"))
    if cat:
        return cat
    t = (d.get("type") or "").lower()
    if "ldc district change" in t:
        return "Rezoning"
    if "cpa" in t:
        return "Comp Plan Amendment"
    if "planned development" in t or "major modification" in t:
        return "PD"
    return None


def _text(row, d):
    return " ".join(x for x in [row.get("project_name"), row.get("description"),
                                d.get("record_details", {}).get("Project Description")] if x)


def prefilter(row, d):
    cat = _cat(row, d)
    if cat is None:
        return False
    apptype = C.asi_get(d, r"^Application Type$") or ""
    if cat == "PD" and re.search(r"conditional use", apptype, re.I):
        return False
    if cat == "Comp Plan Amendment":
        txt = _text(row, d)
        if TEXT_ONLY.search(txt) and not MAP_HINT.search(txt):
            return False
    return True


def build(row, d, aca):
    cat = _cat(row, d)
    if cat is None or (row.get("_tval") is None and not prefilter(row, d)):
        return None
    rec = C.default_record(row, d, CFG, cat)
    txt = _text(row, d)
    apptype = C.asi_get(d, r"^Application Type$")
    if apptype:
        rec["app_type"] = f"{rec['app_type']} - {apptype}"
    rec["parcel_ids"] = [x for x in dict.fromkeys(fmt_parcel(p) for p in d.get("parcels", [])) if x]
    rec["acres"] = C.to_float(C.asi_get(d, r"^Acreage$")) or C.acres_from_text(txt)
    a, b = C.zoning_from_text(txt)
    if cat == "Comp Plan Amendment":
        rec["from_flu"], rec["to_flu"] = a, b
    else:
        rec["from_zoning"], rec["to_zoning"] = a, b
        if cat == "PD" and not b:
            rec["to_zoning"] = "PD"
    asi = C.asi_hearings(d, [
        (r"^DRC Meeting$", "DRC", None),
        (r"^Planning Commission Date$", "Planning Commission", r"^Hearing Results$"),
        (r"^1st BOCC Date$", "BOCC", r"^BOCC 1st Hearing Results$"),
        (r"^2nd BOCC Date$", "BOCC", r"^BOCC 2nd Hearing Results$"),
    ])
    wf = C.hearings_from_workflow(d)
    hs = C.merge_hearings(asi, wf, window_days=45)
    rec["hearings"] = hs
    dev_type = C.asi_get(d, r"^Development Type$") or ""
    finals = ("BOCC",) if (cat != "PD" or re.search(r"county commission", dev_type, re.I)) \
        else ("Planning Commission", "BOCC")
    st = C.norm_status(rec["status_raw"], hs, final_bodies=finals, raw_map={
        r"approved for hearing": "Hearing?",
        r"BOCC Hearing Pending|Pending Hearing": "Hearing Scheduled",
        r"Final Processing": None,
    })
    rec["status"] = st
    rec["decision_date"] = C.decision_date(hs, st, final_bodies=finals)
    return rec


CFG["prefilter"] = prefilter
fetch = C.make_fetch(CFG, build)

if __name__ == "__main__":
    C.run_cli(fetch)
