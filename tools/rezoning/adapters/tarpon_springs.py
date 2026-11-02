#!/usr/bin/env python3
"""City of Tarpon Springs (Pinellas) rezonings, future land use map amendments, planned
developments and annexations with land use / zoning (CivicClerk public API: tarponspringsfl).

Agenda structure (Planning & Zoning Board, Board of Commissioners Regular Session):
  "Application 25-55; Annexation, Future Land Use Map amendment, and Rezoning of property located at
   630 July Drive"  with child items "Ordinance 2025-12 : Annexation", "Ordinance 2025-13 : Future
   Land Use Map Amendment", "Ordinance 2025-14 : Rezoning"
  or a single item: "Ordinance 2025-07 (2nd Reading); Rezoning request to rezone property from IPD
   (Industrial Planned Development) to IR (Industrial Restricted) ; 310, 320, & 340 Anclote Road
   (Application 25.28)"
app_num = "Application YY-NN" (or "Ordinance YYYY-NN" when no application number is given).
Parcel numbers come from the text of small attachments (staff reports, ordinances) or the
site address via the Pinellas PA layer. Outcomes come from CivicClerk minutes when the city posts
them; a first reading followed by a later reading is treated as passed.
"""
import datetime as dt
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pinellas_common as P  # noqa: E402

JURISDICTIONS = ["City of Tarpon Springs"]
JUR = "City of Tarpon Springs"
SOURCE = "Tarpon Springs CivicClerk (P&Z Board + Board of Commissioners)"
P.set_tag("tarpon_springs")
log = P.log
cc = P.CivicClerk("tarponspringsfl")
PZ, BOC = "Planning & Zoning Board", "Board of Commissioners"
MAX_ATT = 4_000_000

APP_RE = re.compile(r"Application\s*#?\s*(?:No\.?\s*)?(\d{2,4})\s*[-.]\s*(\d{1,3})\b", re.I)
ORD_RE = re.compile(r"\bOrdinance\s*(?:No\.?\s*)?(\d{4})\s*-\s*(\d{1,3})\b", re.I)
IN_RE = re.compile(r"Rezon|Zoning\s+(?:Amendment|Atlas|Map)|Future\s+Land\s+Use|Land\s+Use\s+(?:Map|Plan|Amendment)|"
                   r"Planned\s+Development|\b[RCIM]?PD\b|Comprehensive\s+Plan\s+(?:Map\s+)?Amendment", re.I)
OUT_RE = re.compile(r"Conditional\s+Use|Site\s+Plan|Variance|Land\s+Development\s+Code|\bLDC\b|Appointment|Plat\b|"
                    r"Text\s+Amendment|Special\s+Exception|Vacat", re.I)


def app_key(t):
    m = APP_RE.search(t or "")
    if not m:
        return None
    yy = m.group(1)[-2:]
    return f"Application {yy}-{int(m.group(2)):02d}"


def ord_key(t):
    m = ORD_RE.search(t or "")
    return f"{m.group(1)}-{int(m.group(2)):02d}" if m else None


def kind_of(t):
    if re.search(r"Future\s+Land\s+Use|Land\s+Use\s+(?:Map|Plan)|Comprehensive\s+Plan", t, re.I) and not \
            re.search(r"Rezon|Zoning", t, re.I):
        return "CPA"
    if re.search(r"Planned\s+Development|\b[RCIM]?PD\b", t, re.I) and re.search(r"to\s+(?:an?\s+)?[A-Z]*PD\b|"
                                                                                r"Planned\s+Development\s*\)?\s*$", t):
        return "PD"
    return "RZ"


def parse(t):
    u = P.sq(t)
    out = {}
    m = re.search(r"(?:property\s+)?located\s+(?:at|adjacent\s+to)\s+(\d+[^;(]+?)(?:\s*[;(]|\s+-\s|,\s*(?:Approx|Fair|and\s+Includ)|$)", u, re.I) \
        or re.search(r";\s*((?:\d+,?\s*(?:&|and)?\s*)+[A-Z][A-Za-z.]+(?:\s+[A-Z][A-Za-z.]+){0,3}\s+(?:Road|Rd|Avenue|Ave|"
                     r"Street|St|Drive|Dr|Boulevard|Blvd|Highway|Hwy|Lane|Ln|Way|Court|Ct|Place|Pl)\.?)", u)
    if m:
        out["address"] = re.split(r"\.\s+(?=[A-Z])|\s+from\s|\s+Approximately\s", m.group(1))[0].strip(" ,.")
    m = re.search(r"\bfrom\s+(.+?)\s+to\s+(.+?)(?:\s*;|\s*\(Application|\s+for\s|,\s|$)", u, re.I)
    if m and re.search(r"\([A-Z]|District|Residential|Commercial|Industrial|Planned", m.group(0)):
        out["from"], out["to"] = m.group(1).strip(" ,"), m.group(2).strip(" ,")
    m = re.search(r"([\d,]*\.?\d+)\s*Acres?\b", u, re.I)
    if m:
        try:
            out["acres"] = float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    out["pins"] = P.pins_in(u)
    return out


def body_of(name):
    if re.search(r"Planning", name, re.I):
        return PZ
    if re.search(r"Commissioners\s+Regular|Commissioners\s+Special|Board of Commissioners(?!.*Work)", name, re.I):
        return BOC
    return None


def fetch(since, refresh):
    today = dt.date.today()
    start = dt.date.fromisoformat(since) - dt.timedelta(days=60)
    refresh = {r.strip() for r in (refresh or []) if r and r.strip()}
    if refresh:
        start = min(start, today - dt.timedelta(days=540))
    events = cc.events(start.isoformat(), (today + dt.timedelta(days=75)).isoformat())
    log(f"{len(events)} events since {start}")
    cases = {}
    for ev in events:
        body = body_of(ev.get("eventName") or "")
        if not body or not ev.get("agendaId"):
            continue
        files = ev.get("publishedFiles") or []
        mins = next((f.get("fileId") for f in files if f.get("type") == "Minutes"), None)
        packet = next((f.get("fileId") for f in files if f.get("type") == "Agenda Packet"), None)
        pub = [f.get("publishOn")[:10] for f in files if f.get("type") == "Agenda" and f.get("publishOn")]
        d = ev["startDateTime"][:10]
        try:
            items = cc.items(ev["agendaId"])
        except Exception as e:  # noqa: BLE001
            log(f"meeting {ev['agendaId']}: {e}")
            continue
        for it, parent in items:
            name = P.item_name(it)
            full = name + " " + P.item_desc(it)
            pname = P.item_name(parent) if parent else ""
            key = app_key(full) or (app_key(pname) if parent and IN_RE.search(pname) else None)
            ono = ord_key(name)
            scope_txt = full if IN_RE.search(full) else (pname + " " + full if key else full)
            if not IN_RE.search(scope_txt) or OUT_RE.search(name):
                continue
            if re.search(r"^\s*Ordinance[^;:]*[;:]?\s*Annexation\s*\(", name, re.I) and key:
                continue  # annexation ordinance child: the application carries the land use / zoning
            if not key:
                if not ono:
                    continue
                key = f"Ordinance {ono}"
            c = cases.setdefault(key, {"apps": [], "ords": set(), "texts": []})
            if ono:
                c["ords"].add(ono)
            c["texts"].append(full)
            c["apps"].append({"date": d, "first_seen": min(pub + [d]), "body": body, "name": name, "text": full,
                              "reading": P.reading_of(name), "minutes": mins, "atts": it.get("attachmentsList") or [],
                              "event_id": ev.get("id"), "packet": packet})
    log(f"{len(cases)} in-scope cases")
    out = []
    for key, c in sorted(cases.items()):
        first = min(a["first_seen"] for a in c["apps"])
        if first < since and key not in refresh:
            continue
        try:
            out.append(build(key, c, today.isoformat()))
        except Exception as e:  # noqa: BLE001
            log(f"{key}: skipped ({e})")
    return out


def build(key, c, today):
    apps = sorted(c["apps"], key=lambda a: a["date"])
    main_txt = max(c["texts"], key=len)
    info = {}
    for t in sorted(c["texts"], key=len, reverse=True):
        for k, v in parse(t).items():
            if v and not info.get(k):
                info[k] = v
    pins = list(info.get("pins") or [])
    if not pins:  # small attachments (staff report, ordinance) usually list the parcel number
        for a in sorted((x for ap in reversed(apps) for x in ap["atts"]), key=lambda x: x.get("fileSize") or 0):
            if (a.get("fileSize") or 0) > MAX_ATT or not a.get("pdfVersionFullPath"):
                continue
            pins = P.pins_in(cc.text_of_url(a["pdfVersionFullPath"]))
            if pins:
                break
    acres = info.get("acres")
    if not pins and info.get("address"):
        first_addr = re.sub(r"^(\d+)[,\s&and]+(?:\d+[,\s&and]+)*", r"\1 ", info["address"])
        p = P.parcel_lookup(first_addr, "TARPON SPRINGS") or P.parcel_lookup(first_addr)
        if p and p.get("pin"):
            pins = [p["pin"]]
            if acres is None and p.get("acres"):
                acres = p["acres"]
    keys = [f"Ordinance {o}" for o in sorted(c["ords"])] + ([key] if key.startswith("Application") else [])
    keys += [f"Application {key.split()[-1].replace('-', '.')}"] if key.startswith("Application") else []
    hearings = {}
    for a in apps:
        res = None
        if a["minutes"] and a["date"] <= today:
            res = P.outcome_near(cc.text_of_file(a["minutes"]), keys)
        if a["body"] == PZ and res in ("Approved", "Denied"):
            res = "Recommended " + ("Approval" if res == "Approved" else "Denial")
        if re.search(r"\bDEFERRED\b|\bCONTINUED\b|\bPOSTPONED\b", a["name"]) and a["date"] <= today and not res:
            res = "Continued"
        if re.search(r"\bWITHDRAWN\b", a["name"], re.I):
            res = "Withdrawn"
        k = (a["body"], a["date"])
        cur = hearings.get(k)
        rd = a["reading"]
        if cur is None:
            hearings[k] = {"body": a["body"], "date": a["date"], "result": res, "reading": rd,
                           "event_id": a["event_id"], "packet": a["packet"]}
        else:
            cur["result"] = cur["result"] or res
            cur["reading"] = cur["reading"] or rd
    hl = P.implied_first_readings(sorted(hearings.values(), key=lambda h: (h["date"], h["body"] != PZ)), BOC)
    status, raw, decision = P.status_from_hearings(hl, today, final_body=BOC, final_words=("second", "final"))
    hearings_out = []
    for h in hl:
        res = h["result"]
        if h["body"] == BOC and res == "Approved" and h["reading"] == "first":
            res = "Approved on first reading"
        hearings_out.append({"body": h["body"] + (f" ({h['reading']} reading)" if h["body"] == BOC and h["reading"] else ""),
                             "date": h["date"], "result": res})
    allt = " ".join(c["texts"])
    k = kind_of(allt)
    has_cpa = re.search(r"Future\s+Land\s+Use|Land\s+Use\s+(?:Map|Plan)", allt, re.I)
    has_rz = re.search(r"Rezon|Zoning\s+(?:Amendment|Atlas|Map)", allt, re.I)
    if has_cpa and not has_rz:
        category, app_type = "Comp Plan Amendment", "Future Land Use Map Amendment"
    else:
        category = "PD" if k == "PD" else "Rezoning"
        app_type = ("Annexation, Future Land Use Map Amendment and Rezoning" if re.search(r"Annex", allt, re.I) and has_cpa
                    else "Future Land Use Map Amendment and Rezoning" if has_cpa else "Rezoning")
    name = re.sub(r"^(?:Application|Ordinance)[^;:]*[;:]\s*", "", P.sq(apps[-1]["name"]))
    desc = P.sq(main_txt)
    desc = desc if len(desc) <= 300 else desc[:297] + "..."
    latest = hl[-1] if hl else {}
    return {
        "jurisdiction": JUR, "county": "Pinellas", "app_num": key, "category": category, "app_type": app_type,
        "project_name": info.get("address") or (name[:80] if name else None), "description": desc, "applicant": None,
        "parcel_ids": pins, "address": info.get("address"), "acres": acres,
        "from_zoning": info.get("from") if category != "Comp Plan Amendment" else None,
        "to_zoning": info.get("to") if category != "Comp Plan Amendment" else None,
        "from_flu": info.get("from") if category == "Comp Plan Amendment" else None,
        "to_flu": info.get("to") if category == "Comp Plan Amendment" else None,
        "filed_date": min(a["first_seen"] for a in apps), "status": status, "status_raw": raw,
        "hearings": hearings_out, "decision_date": decision,
        "url": cc.event_url(latest.get("event_id")) if latest.get("event_id") else cc.portal,
        "docs_url": cc.file_url(latest["packet"]) if latest.get("packet") else None,
        "lat": None, "lon": None, "source": SOURCE, "ordinances": sorted(c["ords"]) or None,
    }


if __name__ == "__main__":
    since = sys.argv[1] if len(sys.argv) > 1 else (dt.date.today() - dt.timedelta(days=14)).isoformat()
    refresh = sys.argv[2].split(",") if len(sys.argv) > 2 else []
    t0 = time.time()
    recs = fetch(since, refresh)
    print(json.dumps(recs, indent=2, ensure_ascii=False))
    from collections import Counter
    log(f"{len(recs)} records in {time.time() - t0:.0f}s; status={dict(Counter(r['status'] for r in recs))}; "
        f"category={dict(Counter(r['category'] for r in recs))}")
