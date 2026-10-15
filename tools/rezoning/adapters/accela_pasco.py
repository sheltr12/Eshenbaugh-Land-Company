"""Pasco County - Accela Citizen Access (agency PASCO, module Planning).

Record types (search-form "Record Type" dropdown, value -> label) and mapping:
  Planning/Zoning/MPUD/NA        "Rezoning (MPUD)"                 -> PD
        (skips non-substantial MPUD amendments / NS-Mods and scrivener's-error corrections:
         those are administrative)
  Planning/Zoning/Rezoning/NA    "Rezoning (Standard Euclidean)"   -> Rezoning
  Planning/Zoning/Comp Plan/NA   "Comprehensive Plan Amendment"    -> Comp Plan Amendment
        (skips text-only amendments: ASI "Text Change"=Yes and no "Future Land Use Change"=Yes)
Out of scope (not searched): Conditional Use, Special Exception, Variance, Admin Variance,
  Alternative Standards, Annexation, Plats, Site Plan, Appeal, Zoning Verification, etc.

Status / hearings: Pasco's workflow has a generic "Public Hearings" task whose outcome is written
in the status + free-text comment (e.g. "Approved at the 1/20/2026 BCC"). Record status "Closed"
is used after final distribution (approved) as well as for dead applications.
Parcel IDs: Accela shows "04 25 19 0000 00200 0000"; returned in Pasco PA format
"04-25-19-0000-00200-0000".
"""
import re
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import accela_core as C  # noqa: E402

JURISDICTIONS = ["Pasco County"]

CFG = {
    "agency": "PASCO", "module": "Planning", "tab": "Planning",
    "jurisdiction": "Pasco County", "county": "Pasco", "source": "Pasco Accela",
    "types": {
        "Planning/Zoning/MPUD/NA": "PD",
        "Planning/Zoning/Rezoning/NA": "Rezoning",
        "Planning/Zoning/Comp Plan/NA": "Comp Plan Amendment",
    },
    "chunk_days": 400,
}

_ADMIN_MPUD = re.compile(r"non[- ]?substantial|\bNS[- ]?Mod|\bNSM\b|scrivener", re.I)


def fmt_parcel(p):
    p = (p or "").strip()
    parts = p.split()
    if len(parts) == 6 and all(x.isalnum() for x in parts):
        return "-".join(parts)
    return p or None


def _text(row, d):
    rd = d.get("record_details", {})
    return " ".join(x for x in [row.get("project_name"), row.get("description"), rd.get("Project Description")] if x)


def prefilter(row, d):
    cat = CFG["types"].get(row.get("_tval"))
    txt = _text(row, d)
    if cat == "PD" and _ADMIN_MPUD.search(txt):
        return False
    if cat == "Comp Plan Amendment":
        text_only = (C.asi_get(d, r"^Text Change$") or "").lower() == "yes" and \
            (C.asi_get(d, r"^Future Land Use Change$") or "").lower() != "yes"
        if text_only and not re.search(r"\bfrom\b.+\bto\b|land use amendment|FLUM? (map )?amend", txt, re.I):
            return False
    return True


def build(row, d, aca):
    cat = CFG["types"].get(row.get("_tval")) or _cat_from_type(d.get("type"))
    if cat is None:
        return None
    if row.get("_tval") is None and not prefilter({**row, "_tval": _tval_for(cat)}, d):
        return None
    rec = C.default_record(row, d, CFG, cat)
    txt = _text(row, d)
    pids = []
    for p in C.asit_values(d, r"^Parcel Number$") + d.get("parcels", []):
        f = fmt_parcel(p)
        if f and f not in pids:
            pids.append(f)
    rec["parcel_ids"] = pids
    rec["acres"] = C.to_float(C.asi_get(d, r"^Total Project Acreage$", r"^Total Acreage$")) or C.acres_from_text(txt)
    fz, tz = C.zoning_from_text(txt)
    rec["from_zoning"] = C.asi_get(d, r"^Existing Zoning District$") or fz
    pz = C.asi_get(d, r"^Proposed Zoning District$")
    if pz:
        pz = re.sub(r"^Section\s+\d+\s+", "", pz).strip()
        m = re.match(r"^([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*)\b", pz)
        pz = m.group(1) if m else pz
    rec["to_zoning"] = pz or ("MPUD" if cat == "PD" else tz)
    if cat == "Comp Plan Amendment":
        existing = [it["label"] for it in d.get("asi", []) if (it.get("group") or "").upper() == "EXISTING FUTURE LAND USE"
                    and (it.get("value") or "").lower() == "yes"]
        ff, tf = C.zoning_from_text(txt)
        rec["from_flu"] = ff or (", ".join(existing) if existing else None)
        rec["to_flu"] = tf or C.asi_get(d, r"^Proposed Future Land Use$")
        rec["from_zoning"] = None if rec["from_zoning"] == rec["from_flu"] else rec["from_zoning"]
        rec["to_zoning"] = None
    hs = C.hearings_from_workflow(d, default_body="BOCC", task_rx=r"^Public Hearings?$")
    hs = C.merge_hearings(C.hearings_from_comments(d), hs, window_days=5)
    rec["hearings"] = hs
    raw = rec["status_raw"] or ""
    st = C.norm_status(raw, hs, raw_map={
        r"^Hearing Scheduled$": "Hearing Scheduled",
        r"Pending Board Approval|Hearing PC Approved|Staff Report Complete|Affidavit Received": "Hearing?",
        r"^Closed$": None,
        r"^Not Required$": None,
    })
    if raw == "Closed" and st == "Filed":
        outcome = None
        for t in d.get("workflow", []):
            if re.search(r"Public Hearing|Consolidation|Final Distribution", t.get("task") or "", re.I):
                for e in t.get("entries", []):
                    blob = f"{e.get('status') or ''} {e.get('comment') or ''}"
                    if re.search(r"\bdenied\b", blob, re.I):
                        outcome = "Denied"
                    elif re.search(r"approv|adopt", blob, re.I) and outcome != "Denied":
                        outcome = "Approved"
                    elif re.search(r"Distribute", e.get("status") or "", re.I) and not outcome:
                        outcome = "Approved"
        st = outcome or "Withdrawn"
    rec["status"] = st
    rec["decision_date"] = C.decision_date(hs, st)
    return rec


def _cat_from_type(t):
    t = (t or "").lower()
    if "mpud" in t:
        return "PD"
    if "rezoning" in t:
        return "Rezoning"
    if "comprehensive plan" in t:
        return "Comp Plan Amendment"
    return None


def _tval_for(cat):
    for k, v in CFG["types"].items():
        if v == cat:
            return k
    return None


CFG["prefilter"] = prefilter
fetch = C.make_fetch(CFG, build)

if __name__ == "__main__":
    C.run_cli(fetch)
