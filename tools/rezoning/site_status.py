#!/usr/bin/env python3
"""Vacant / improved call-out for every tracked application.

Adds a "site_status" object to each record in out/rezonings.json, from the Florida statewide
cadastral layer (county property appraiser tax roll, Jan 1 snapshot) matched on the record's parcel
numbers. Run after rezoning_pipeline.py and before push_layer_locked.py:

  python3 site_status.py --out-dir ~/rz/out --run-date 2026-11-02

site_status = {
  "status": "Vacant" | "Improved" | "Partly improved" | "Unknown",
  "detail": "2 buildings, 2,499 sf, built 1981. Use: light manufacturing.",
  "parcels_found": 1, "parcels_total": 1, "roll_year": 2026, "checked": "2026-11-02",
  "ids": "<parcel ids this was computed for>", "method": "parcel id" | "parcel location" | "none"
}
Records are refreshed when new, when their parcel numbers change, or every --max-age days.
Prints "done: ..." at the end. No em dashes anywhere.
"""
import argparse, json, os, re, sys, time, datetime as dt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import parcels  # noqa: E402

OUT = ("PARCEL_ID,ALT_KEY,DOR_UC,PA_UC,NO_BULDNG,TOT_LVG_AR,ACT_YR_BLT,EFF_YR_BLT,JV,LND_VAL,SPEC_FEAT_,"
       "NO_RES_UNT,ASMNT_YR,OWN_NAME")

# Florida DOR land use codes (last two digits of DOR_UC)
DOR = {
    "00": "vacant residential", "01": "single family home", "02": "mobile home", "03": "multifamily (10+ units)",
    "04": "condominium", "05": "co-op", "06": "retirement home", "07": "boarding home", "08": "multifamily (under 10 units)",
    "09": "residential common area", "10": "vacant commercial", "11": "retail store", "12": "mixed use (store and office or residential)",
    "13": "department store", "14": "supermarket", "15": "regional shopping center", "16": "community shopping center",
    "17": "office (one story)", "18": "office (multi-story)", "19": "professional services building", "20": "airport or marina",
    "21": "restaurant", "22": "drive-in restaurant", "23": "financial institution", "24": "insurance office",
    "25": "repair service shop", "26": "service station", "27": "auto sales, repair or storage", "28": "parking lot or mobile home park",
    "29": "wholesale outlet", "30": "florist or greenhouse", "31": "drive-in theater or open stadium", "32": "theater or auditorium",
    "33": "nightclub or bar", "34": "bowling alley, rink or arena", "35": "tourist attraction", "36": "camp",
    "37": "race track", "38": "golf course", "39": "hotel or motel", "40": "vacant industrial", "41": "light manufacturing",
    "42": "heavy industrial", "43": "lumber yard or sawmill", "44": "packing plant", "45": "cannery or brewery",
    "46": "food processing", "47": "mineral processing", "48": "warehouse or distribution", "49": "open storage",
    "50": "improved agricultural", "51": "cropland", "52": "cropland", "53": "cropland", "54": "timberland",
    "55": "timberland", "56": "timberland", "57": "timberland", "58": "timberland", "59": "timberland",
    "60": "grazing land", "61": "grazing land", "62": "grazing land", "63": "grazing land", "64": "grazing land",
    "65": "grazing land", "66": "orchard or citrus grove", "67": "poultry, bees or fish farm", "68": "dairy or feed lot",
    "69": "ornamentals or nursery", "70": "vacant institutional", "71": "church", "72": "private school or college",
    "73": "private hospital", "74": "home for the aged", "75": "nonprofit or charitable", "76": "mortuary or cemetery",
    "77": "club or lodge", "78": "sanitarium or rest home", "79": "cultural organization", "80": "vacant governmental",
    "81": "military", "82": "forest, park or recreation area", "83": "public school", "84": "public college",
    "85": "public hospital", "86": "county owned", "87": "state owned", "88": "federal owned", "89": "municipal owned",
    "90": "leasehold interest", "91": "utility", "92": "mining or petroleum land", "93": "subsurface rights",
    "94": "right-of-way", "95": "river, lake or submerged land", "96": "sewage, waste land or borrow pit",
    "97": "outdoor recreation or park land", "98": "centrally assessed", "99": "acreage, not agricultural",
}
AG = {f"{i:02d}" for i in range(50, 70)}


def log(*a):
    print("[site_status]", *a, file=sys.stderr, flush=True)


def use_code(a):
    d = re.sub(r"\D", "", str(a.get("DOR_UC") or ""))
    return d[-2:].zfill(2) if d else None


def num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def improved(a):
    return num(a.get("NO_BULDNG")) > 0 or num(a.get("TOT_LVG_AR")) > 0 or num(a.get("ACT_YR_BLT")) > 1700


def q_ids(sw, ids):
    """Indexed PARCEL_ID equality query on the statewide layer, no geometry. -> {canon id: attrs}"""
    out = {}
    ids = list(dict.fromkeys(ids))
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        lits = ",".join("'{}'".format(v.replace("'", "''")) for v in chunk)
        where = f"{sw.where} AND PARCEL_ID IN ({lits})" if sw.where else f"PARCEL_ID IN ({lits})"
        try:
            feats = parcels._query(sw.url, dict(where=where, outFields=OUT, returnGeometry="false"), method="POST")
        except Exception as e:  # noqa: BLE001
            log(f"statewide id query failed: {e}")
            continue
        for f in feats:
            out[parcels._canon(sw, "id", f["attributes"].get("PARCEL_ID"))] = f["attributes"]
    return out


def q_point(sw, lat, lon):
    try:
        return [f["attributes"] for f in parcels._query(sw.url, dict(
            where=sw.where or "1=1", geometry=f"{lon},{lat}", geometryType="esriGeometryPoint", inSR=4326,
            spatialRel="esriSpatialRelIntersects", outFields=OUT, returnGeometry="false"), page=False)]
    except Exception as e:  # noqa: BLE001
        log(f"statewide point query failed: {e}")
        return []


def digits(s):
    return re.sub(r"[^0-9A-Z]", "", str(s or "").upper())


def county_attrs(county, ids):
    """-> {raw id: (attrs, method)} for the parcels that could be matched."""
    c = parcels._county(county)
    sw = next((s for s in c.sources if s.url == parcels.SW_URL), None)
    if sw is None:
        return {}
    try:
        found = parcels.lookup(county, ids)
    except Exception as e:  # noqa: BLE001
        log(f"{county}: parcel lookup failed: {e}")
        found = {}
    want = {}
    for raw in ids:
        r = found.get(raw) or {}
        cands = []
        for v0 in (r.get("norm_id"), r.get("alt_key"), raw):  # alt_key is the PIN/strap in some counties (Hillsborough)
            if v0:
                try:
                    cands += c.normalize(str(v0)) or []
                except Exception:  # noqa: BLE001
                    pass
        for kind, v in cands:
            m = c.sw_map(kind, v) if c.sw_map else (kind, v)
            if m and m[0] == "id":
                want[raw] = m[1]
                break
    hits = q_ids(sw, want.values()) if want else {}
    out = {}
    for raw, v in want.items():
        a = hits.get(parcels._canon(sw, "id", v))
        if a:
            out[raw] = (a, "parcel id")
    n_pts, t_pts = 0, time.time() + 300
    for raw in ids:  # fall back to the exact parcel centroid from the county layer, accepted only if ids agree
        if raw in out:
            continue
        if n_pts >= 80 or time.time() > t_pts:
            log(f"{county}: location fallback limit reached; remaining parcels are checked next run")
            break
        n_pts += 1
        r = found.get(raw)
        if not r or r.get("lat") is None:
            continue
        keys = {digits(raw), digits(r.get("norm_id")), digits(r.get("alt_key"))} - {""}
        pts = q_point(sw, r["lat"], r["lon"])
        pick = [a for a in pts if {digits(a.get("PARCEL_ID")), digits(a.get("ALT_KEY"))} & keys]
        if not pick and len(pts) == 1 and r.get("owner") and parcels._clean(pts[0].get("OWN_NAME")) == r.get("owner"):
            pick = pts
        if pick:
            out[raw] = (pick[0], "parcel location")
    return out


def summarize(rows, total):
    """rows: list of attrs for the matched parcels of one application."""
    n = len(rows)
    imp = [a for a in rows if improved(a)]
    uses = [use_code(a) for a in rows if use_code(a)]
    main_use = None
    if uses:
        main_use = max(set(uses), key=lambda u: (uses.count(u), u))
    use_txt = DOR.get(main_use) if main_use else None
    bld = int(sum(num(a.get("NO_BULDNG")) for a in imp))
    sf = int(sum(num(a.get("TOT_LVG_AR")) for a in imp))
    yrs = sorted({int(num(a.get("ACT_YR_BLT"))) for a in imp if num(a.get("ACT_YR_BLT")) > 1700})
    feat = sum(num(a.get("SPEC_FEAT_")) for a in rows)

    def bits():
        b = []
        if bld:
            b.append(f"{bld:,} building" + ("s" if bld != 1 else ""))
        if sf:
            b.append(f"{sf:,} sf")
        if yrs:
            b.append(f"built {yrs[0]}" if len(yrs) == 1 else f"built {yrs[0]} to {yrs[-1]}")
        return ", ".join(b)

    if not imp:
        status = "Vacant"
        detail = "No buildings on the tax roll"
        if feat >= 10000:
            detail += f"; some site improvements such as paving, fencing or wells (${feat:,.0f})"
    elif len(imp) == n:
        status = "Improved"
        detail = bits() or "Improvements on the tax roll"
    else:
        status = "Partly improved"
        detail = f"{len(imp)} of {n} parcels {'has' if len(imp) == 1 else 'have'} buildings" + (f" ({bits()})" if bits() else "")
    if use_txt:
        detail += f". {'Main use' if n > 1 else 'Use'}: {use_txt}"
        if status != "Vacant" and main_use in AG:
            detail += " (farm house or farm buildings)"
    if total > n:
        detail += f". {total - n} of {total} parcels not found in the tax roll"
    return status, detail + "."


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--run-date", required=True)
    ap.add_argument("--max-age", type=int, default=90, help="days before a record is re-checked")
    ap.add_argument("--budget", type=int, default=1200, help="seconds")
    ap.add_argument("--all", action="store_true", help="re-check every record")
    a = ap.parse_args(argv)
    od = os.path.expanduser(a.out_dir)
    path = os.path.join(od, "rezonings.json")
    db = json.load(open(path))
    recs = db["records"]
    os.environ.setdefault("RZ_PARCEL_CACHE", os.path.join(od, "owner_cache.json"))
    run = dt.date.fromisoformat(a.run_date)

    # this week's digest items first, then open cases, then the rest
    prio = set()
    dp = os.path.join(od, f"rezoning-digest-{a.run_date}.json")
    if os.path.exists(dp):
        dg = json.load(open(dp))
        prio = {x["id"] for lst in (dg.get("new_filings") or {}).values() for x in lst} | \
               {x["id"] for x in dg.get("status_changes") or []}

    def stale(r):
        s = r.get("site_status")
        ids = "|".join(map(str, r.get("parcel_ids") or []))
        if a.all or not s or s.get("ids") != ids:
            return True
        try:
            return (run - dt.date.fromisoformat(s.get("checked"))).days >= a.max_age
        except Exception:  # noqa: BLE001
            return True

    todo = [r for r in recs if stale(r)]
    todo.sort(key=lambda r: (r["id"] not in prio, r.get("status") in ("Approved", "Denied", "Withdrawn")))
    log(f"{len(todo)} of {len(recs)} records need a check ({len(prio)} in this week's digest)")
    t_end = time.time() + a.budget
    by_county = {}
    for r in todo:
        by_county.setdefault(r.get("county"), []).append(r)
    order = sorted(by_county, key=lambda k: min(todo.index(r) for r in by_county[k]))
    done = n_unknown = 0
    tally = {}
    for county in order:
        if time.time() > t_end:
            log("time budget used; remaining records keep their old value and are checked next run")
            break
        rs = by_county[county]
        ids = sorted({str(p) for r in rs for p in (r.get("parcel_ids") or []) if str(p).strip()})
        try:
            attrs = county_attrs(county, ids) if ids else {}
        except ValueError as e:  # unsupported county
            log(str(e))
            attrs = {}
        for r in rs:
            pids = [str(p) for p in (r.get("parcel_ids") or []) if str(p).strip()]
            rows, methods, seen = [], set(), set()
            for p in pids:
                if p in attrs:
                    at, m = attrs[p]
                    k = digits(at.get("PARCEL_ID"))
                    if k in seen:
                        continue
                    seen.add(k)
                    rows.append(at)
                    methods.add(m)
            if rows:
                status, detail = summarize(rows, len(set(pids)))
                yr = max(int(num(x.get("ASMNT_YR"))) for x in rows) or None
                method = "parcel id" if methods == {"parcel id"} else "parcel location" if methods == {"parcel location"} else "parcel id and location"
            else:
                status = "Unknown"
                detail = ("No parcel number posted with the application." if not pids
                          else "Parcel number not found in the tax roll.")
                yr, method = None, "none"
                n_unknown += 1
            r["site_status"] = {"status": status, "detail": detail, "parcels_found": len(rows),
                                "parcels_total": len(set(pids)), "roll_year": yr, "checked": a.run_date,
                                "ids": "|".join(map(str, r.get("parcel_ids") or [])), "method": method}
            tally[status] = tally.get(status, 0) + 1
            done += 1
        log(f"{county}: {len(rs)} records, {len(attrs)} of {len(ids)} parcels matched")
    db.setdefault("meta", {})["site_status"] = {"checked": done, "unknown": n_unknown, "run_date": a.run_date,
                                                "source": "Florida statewide cadastral (county property appraiser tax roll)"}
    tmp = path + ".tmp"
    json.dump(db, open(tmp, "w"), ensure_ascii=False, indent=1)
    json.load(open(tmp))
    os.replace(tmp, path)
    have = sum(1 for r in recs if r.get("site_status"))
    print(f"done: checked {done} records {tally}; {have} of {len(recs)} records have a site status")


if __name__ == "__main__":
    main()
