"""Charlotte County - Accela Citizen Access (agency BOCC, module Planning).

Record types (search-form "Record Type" dropdown) and mapping:
  Planning/Growth Mgmt/Land Use Amendments/Rezonings                  "GM-Land Use Rezonings" (Z-yy-nn)
        -> Rezoning
  Planning/Growth Mgmt/Land Use Amendments/Rezonings-PD               "GM-Land Use Rezonings-PD" (PD-yy-nn)
        -> PD (new PDs and PD major modifications)
  Planning/Growth Mgmt/Land Use Amendments/Plan Amendments Small Scale "GM-Land Use Amendment Small" (PAS-)
  Planning/Growth Mgmt/Land Use Amendments/Plan Amendments Large Scale "GM-Land Use Plan Amendment Large" (PAL-)
        -> Comp Plan Amendment
Out of scope / not searched: GM-Text Plan Amendment (Comp Plan / LDR), BZA variances / special
  exceptions / appeals, Site Plan Review, Minor Modification, CDD, DRI, TDU, Zoning
  Determination letters, all LD- (plats, excavations, lot splits, vacations) types.

Caveat: Charlotte's public Accela records mostly stay at status "Awaiting Plans" for their whole
life (review/hearings are tracked outside this portal), so status is usually "Filed" and
hearings are rarely available; the adapter is mainly useful for discovering new filings.
Parcel IDs: 12-digit Charlotte PA account numbers as shown (e.g. 412105481003).
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import accela_core as C  # noqa: E402

JURISDICTIONS = ["Charlotte County"]

CFG = {
    "agency": "BOCC", "module": "Planning", "tab": "Planning",
    "jurisdiction": "Charlotte County", "county": "Charlotte", "source": "Charlotte County Accela",
    "types": {
        "Planning/Growth Mgmt/Land Use Amendments/Rezonings": "Rezoning",
        "Planning/Growth Mgmt/Land Use Amendments/Rezonings-PD": "PD",
        "Planning/Growth Mgmt/Land Use Amendments/Plan Amendments Small Scale": "Comp Plan Amendment",
        "Planning/Growth Mgmt/Land Use Amendments/Plan Amendments Large Scale": "Comp Plan Amendment",
    },
    "chunk_days": 400,
}


def _cat(row, d):
    cat = CFG["types"].get(row.get("_tval"))
    if cat:
        return cat
    t = (d.get("type") or "").lower()
    if "rezonings-pd" in t:
        return "PD"
    if "rezonings" in t:
        return "Rezoning"
    if "plan amendment" in t or "land use amendment" in t:
        return None if "text" in t else "Comp Plan Amendment"
    return None


def build(row, d, aca):
    cat = _cat(row, d)
    if cat is None:
        return None
    rec = C.default_record(row, d, CFG, cat)
    rec["parcel_ids"] = list(dict.fromkeys(d.get("parcels", [])))
    changes = C.asi_get(d, r"Proposed Changes")
    txt = " ".join(x for x in [rec["description"], changes] if x)
    fa, ta = C.zoning_from_text(rec["description"])
    if not ta:
        fa, ta = C.zoning_from_text(changes)
    if not ta and changes:
        m = re.match(r"^\s*to\s+(.+)$", changes, re.I) or re.match(r"^([A-Z][A-Z0-9\-]{1,8})$", changes.strip())
        ta = m.group(1).strip() if m else None
    if not ta:
        m = re.search(r"\bto\s+([A-Z][A-Z0-9\-]{1,8})\b", rec["description"] or "")
        ta = m.group(1) if m else None
    ex_flu = C.asi_get(d, r"Existing Future Land Use")
    ex_z = C.asi_get(d, r"Existing Zoning District")
    if cat == "Comp Plan Amendment":
        rec["from_flu"] = fa or (ex_flu.split(",")[0].strip() if ex_flu else None)
        rec["to_flu"] = ta
        rec["from_zoning"] = ex_z.split(",")[0].strip() if ex_z else None
    else:
        rec["from_zoning"] = fa or (ex_z.split(",")[0].strip() if ex_z else None)
        rec["to_zoning"] = ta or ("PD" if cat == "PD" else None)
    tot = sum(C.to_float(r.get("FLUM Acreage")) or 0 for r in d.get("asit", []) if r.get("table") == "FLUM")
    if not tot:
        tot = sum(C.to_float(r.get("Zoning District Acreage")) or 0 for r in d.get("asit", []) if r.get("table") == "ZONING")
    if not tot and ex_flu and "," in ex_flu:
        tot = C.to_float(ex_flu.split(",", 1)[1]) or 0
    rec["acres"] = round(tot, 3) if tot else C.acres_from_text(txt)
    reason = C.asi_get(d, r"Reason for Proposed Changes")
    if reason and rec["description"] and reason.lower() not in rec["description"].lower():
        rec["description"] = f"{rec['description']} ({reason})"
    hs = C.hearings_from_workflow(d)
    rec["hearings"] = hs
    st = C.norm_status(rec["status_raw"], hs, raw_map={r"Awaiting Plans|In Review": None})
    rec["status"] = st
    rec["decision_date"] = C.decision_date(hs, st)
    if st in ("Approved", "Denied") and not rec["decision_date"]:
        ds = [e["date"] for t in d.get("workflow", []) if re.search(r"final|approval", t.get("task") or "", re.I)
              for e in t.get("entries", []) if e.get("date") and re.search(r"approv|den", e.get("status") or "", re.I)]
        rec["decision_date"] = max(ds) if ds else None
    return rec


fetch = C.make_fetch(CFG, build)

if __name__ == "__main__":
    C.run_cli(fetch)
