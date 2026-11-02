#!/usr/bin/env python3
"""City of Seminole (Pinellas) rezonings / land use plan amendments (CivicClerk public API: seminolefl).

Only City Council agendas are published on CivicClerk (the Developmental Review Board is not), so
cases appear once they reach Council as ordinances:
  "First Reading of Ordinance No. 05-2026: Rezoning ... 8701 Seminole Boulevard"
  "Public Hearing, Second and Final Reading of Ordinance No. 05-2026: ..."
app_num = "Ordinance No. NN-YYYY". Voluntary annexations of single lots, budget, code and text
ordinances are skipped. Outcomes come from the published Council minutes. This is a small,
mostly built-out city: most weeks return nothing.
Not to be confused with Seminole County (adapter seminole.py).
"""
import datetime as dt
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pinellas_common as P  # noqa: E402

JURISDICTIONS = ["City of Seminole"]
JUR = "City of Seminole"
SOURCE = "City of Seminole CivicClerk (City Council agendas)"
P.set_tag("city_of_seminole")
log = P.log
cc = P.CivicClerk("seminolefl")
CC = "City Council"
MAX_ATT = 4_000_000

ORD_RE = re.compile(r"Ordinance\s*(?:No\.?\s*)?(\d{1,3})\s*-\s*(\d{4})\b", re.I)
IN_RE = re.compile(r"Rezon|Zoning\s+(?:Map|Atlas|District|Amendment|Change)|Future\s+Land\s+Use|Land\s+Use\s+(?:Plan|Map|"
                   r"Designation|Amendment)|Planned\s+(?:Unit|Residential|Commercial)?\s*Development|Comprehensive\s+Plan\s+Map",
                   re.I)
OUT_RE = re.compile(r"Budget|Millage|Code of Ordinances|Land Development (?:Code|Regulations)|Text\s+Amendment|"
                    r"Stormwater|Fee|Franchise|SB\s*\d+|Senate Bill|Recovery Residence|Plat", re.I)


def fetch(since, refresh):
    today = dt.date.today()
    start = dt.date.fromisoformat(since) - dt.timedelta(days=60)
    refresh = {r.strip() for r in (refresh or []) if r and r.strip()}
    if refresh:
        start = min(start, today - dt.timedelta(days=540))
    events = cc.events(start.isoformat(), (today + dt.timedelta(days=75)).isoformat())
    cases = {}
    for ev in events:
        name = ev.get("eventName") or ""
        if not ev.get("agendaId") or not re.search(r"Council", name, re.I) or re.search(r"Workshop", name, re.I):
            continue
        files = ev.get("publishedFiles") or []
        mins = next((f.get("fileId") for f in files if f.get("type") == "Minutes"), None)
        pub = [f.get("publishOn")[:10] for f in files if f.get("type") == "Agenda" and f.get("publishOn")]
        d = ev["startDateTime"][:10]
        try:
            items = cc.items(ev["agendaId"])
        except Exception as e:  # noqa: BLE001
            log(f"meeting {ev['agendaId']}: {e}")
            continue
        for it, _parent in items:
            nm = P.item_name(it)
            full = nm + " " + P.item_desc(it)
            m = ORD_RE.search(nm)
            if not m or not IN_RE.search(full) or OUT_RE.search(nm):
                continue
            if re.search(r"Annexation", nm, re.I) and not re.search(r"Rezon|Zoning|Land\s+Use", full, re.I):
                continue
            key = f"Ordinance No. {int(m.group(1)):02d}-{m.group(2)}"
            cases.setdefault(key, []).append({"date": d, "first_seen": min(pub + [d]), "name": nm, "text": full,
                                              "reading": P.reading_of(nm), "minutes": mins, "event_id": ev.get("id"),
                                              "atts": it.get("attachmentsList") or []})
    log(f"{len(events)} events since {start}; {len(cases)} in-scope ordinances")
    out = []
    for key, apps in sorted(cases.items()):
        if min(a["first_seen"] for a in apps) < since and key not in refresh:
            continue
        try:
            out.append(build(key, apps, today.isoformat()))
        except Exception as e:  # noqa: BLE001
            log(f"{key}: skipped ({e})")
    return out


def build(key, apps, today):
    apps = sorted(apps, key=lambda a: a["date"])
    txt = max((a["text"] for a in apps), key=len)
    u = P.sq(txt)
    addr = None
    m = re.search(r"[–\-:]\s*(\d+\s+[0-9A-Z][^.;(]*?(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Boulevard|Blvd|Way|Lane|Ln|"
                  r"Place|Pl|Court|Ct|Terrace|Ter|Highway|Hwy)\b\.?)", u, re.I) or \
        re.search(r"located at\s+(\d+[^.;(,]+)", u, re.I)
    if m:
        addr = m.group(1).strip(" .,").title()
    frm = to = None
    m = re.search(r"\bfrom\s+(.+?)\s+to\s+(.+?)(?:[.;]|\s+for\s|$)", u, re.I)
    if m:
        frm, to = m.group(1).strip(), m.group(2).strip()
    acres = None
    m = re.search(r"([\d,]*\.?\d+)\s*acres?\b", u, re.I)
    if m:
        try:
            acres = float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    pins = P.pins_in(u)
    if not pins:
        for a in sorted((x for ap in reversed(apps) for x in ap["atts"]), key=lambda x: x.get("fileSize") or 0):
            if (a.get("fileSize") or 0) > MAX_ATT or not a.get("pdfVersionFullPath"):
                continue
            pins = P.pins_in(cc.text_of_url(a["pdfVersionFullPath"]))
            if pins:
                break
    pins = P.valid_pins(pins)
    if not pins and addr:
        p = P.parcel_lookup(addr, "SEMINOLE") or P.parcel_lookup(addr)
        if p and p.get("pin"):
            pins = [p["pin"]]
            acres = acres if acres is not None else p.get("acres")
    hearings = []
    for a in apps:
        res = P.outcome_near(cc.text_of_file(a["minutes"]), [key, key.replace("No. ", "")]) \
            if a["minutes"] and a["date"] <= today else None
        hearings.append({"body": CC, "date": a["date"], "result": res, "reading": a["reading"], "event_id": a["event_id"]})
    hearings = P.implied_first_readings(hearings, CC)
    status, raw, decision = P.status_from_hearings(hearings, today, final_body=CC, final_words=("second", "final"))
    cpa = re.search(r"Land\s+Use|Comprehensive\s+Plan", u, re.I) and not re.search(r"Rezon|Zoning", u, re.I)
    pd = re.search(r"Planned\s+(?:Unit|Residential|Commercial)?\s*Development", to or u, re.I)
    category = "Comp Plan Amendment" if cpa else ("PD" if pd else "Rezoning")
    return {
        "jurisdiction": JUR, "county": "Pinellas", "app_num": key, "category": category,
        "app_type": {"Comp Plan Amendment": "Land Use Plan Amendment", "PD": "Planned Development Rezoning"}.get(category, "Rezoning"),
        "project_name": addr, "description": u if len(u) <= 300 else u[:297] + "...", "applicant": None,
        "parcel_ids": pins, "address": addr, "acres": acres,
        "from_zoning": None if cpa else frm, "to_zoning": None if cpa else to,
        "from_flu": frm if cpa else None, "to_flu": to if cpa else None,
        "filed_date": min(a["first_seen"] for a in apps), "status": status, "status_raw": raw,
        "hearings": [{"body": CC + (f" ({h['reading']} reading)" if h["reading"] else ""), "date": h["date"],
                      "result": ("Approved on first reading" if h["result"] == "Approved" and h["reading"] == "first"
                                 else h["result"])} for h in hearings],
        "decision_date": decision, "url": cc.event_url(apps[-1]["event_id"]), "docs_url": None,
        "lat": None, "lon": None, "source": SOURCE,
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
