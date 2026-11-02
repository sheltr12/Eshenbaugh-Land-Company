#!/usr/bin/env python3
"""City of Pinellas Park (Pinellas) rezonings, PUD master plans and land use plan map amendments.

Pinellas Park moved its agendas from Legistar (pinellaspark) to CivicClerk (pinellasparkfl) in
September 2026, so this adapter reads both:
  * Legistar matters (Planning and Zoning Commission, City Council), agenda dates = hearings,
    for cases heard up to early September 2026.
  * CivicClerk agendas (P&Z Commission, City Council) from then on, plus CivicClerk minutes for
    outcomes ("MOTION ... to PASS Ordinance No. 2026-12 ... MOTION CARRIED").
Agenda items are full ordinance titles that end with the city case numbers and applicant:
  "... (REZ-2026-00001 and PUD-0526-00005; Macfarlane, Ferguson & McMullen, P.A.) PUBLIC HEARING"
Case numbers tie the P&Z item and the Council readings together; app_num = the case number(s).
Kept: zoning map amendments (Rezoning), PUD master plans (PD), land use plan map amendments
(Comp Plan Amendment). Skipped: annexation-only ordinances (their land use and zoning ordinances
are kept), code / text amendments, moratoria.
"""
import datetime as dt
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pinellas_common as P  # noqa: E402

JURISDICTIONS = ["City of Pinellas Park"]
JUR = "City of Pinellas Park"
LEG = "https://webapi.legistar.com/v1/pinellaspark"
LEG_SITE = "https://pinellaspark.legistar.com"
SOURCE = "Pinellas Park CivicClerk + Legistar (P&Z Commission + City Council)"
P.set_tag("pinellas_park")
log = P.log
cc = P.CivicClerk("pinellasparkfl")

ID_RE = re.compile(r"\b(REZ|PUD|LUPA|LUP|ZON|CPA|MPA|AX|SPA)-(\d{4})-(\d{5})\b", re.I)
ORD_RE = re.compile(r"ORDINANCE\s+NO\.?\s*(\d{4})\s*[-.]\s*(\d{1,3})\b", re.I)
PZ, CC = "Planning and Zoning Commission", "City Council"


def ids_of(t):
    return sorted({f"{a.upper()}-{b}-{c}" for a, b, c in ID_RE.findall(t or "")})


def kind_of(t):
    u = (t or "").upper()
    if re.search(r"MORATORIUM|AMENDING THE CODE OF ORDINANCES|LAND DEVELOPMENT CODE|AMENDING THE (?:LAND USE|"
                 r"TRANSPORTATION|HOUSING) ELEMENT", u) and not re.search(r"OFFICIAL (?:ZONING|LAND USE PLAN) MAP", u):
        return None
    if re.search(r"OFFICIAL LAND USE PLAN MAP|FUTURE LAND USE MAP|LAND USE DESIGNATION", u):
        return "CPA"
    if re.search(r"PLANNED UNIT DEVELOPMENT|\b[RCM]?PUD\b|MASTER PLAN", u) and re.search(r"MASTER PLAN|ZONING", u):
        return "PD"
    if re.search(r"OFFICIAL ZONING MAP|ZONING DISTRICT|REZON", u):
        return "RZ"
    return None


def parse(t):
    u = P.sq(t)
    out = {}
    m = re.search(r"(?:GENERALLY\s+)?LOCATED\s+AT\s+(?:PARCEL\s+(?:ID|NO\.?)\s*)?(.+?)(?:,|\s+AND\s+(?:MORE|LEGALLY)|"
                  r"\s+ON\s+LAND|;|\s+FROM\s|$)", u, re.I)
    if m and re.match(r"\d", m.group(1)) and not P.PIN_RE.match(m.group(1)):
        out["address"] = m.group(1).strip(" .,").title()
    out["pins"] = P.pins_in(u)
    m = re.search(r"([\d,]*\.?\d+)\s*(?:\+/-\s*)?ACRES?\b", u, re.I)
    if m:
        try:
            out["acres"] = float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    m = re.search(r"\bFROM\s+(.+?)\s+TO\s+(?:A\s+)?(.+?)(?:\s+FOR\s|\s+ON\s|;|,|\s+DESIGNATION)", u, re.I)
    if m:
        out["from"], out["to"] = m.group(1).strip(" ,"), m.group(2).strip(" ,")
    m = re.search(r"LAND USE DESIGNATION OF\s+(.+?)\s+FOR\b", u, re.I)
    if m and not out.get("to"):
        out["to"] = m.group(1).strip()
    m = re.search(r"WITH UNDERLYING\s+(.+?)\s+ZONING", u, re.I)
    if m and not out.get("to"):
        out["to"] = re.sub(r"^A\s+", "", m.group(1).strip())
        mm = re.search(r"\(([RCM]?PUD)\)", u)
        if mm:
            out["to"] = f"{mm.group(1)} with underlying {out['to']}"
    m = re.search(r"\(\s*(?:(?:[A-Z]+-\d{4}-\d{5})(?:\s*(?:and|&|,)\s*)?)+\s*[;,]\s*([^)]+)\)", u, re.I)
    if m:
        out["applicant"] = P.sq(m.group(1)).strip(" ,")
    m = ORD_RE.search(u)
    if m:
        out["ord"] = f"{m.group(1)}-{int(m.group(2)):02d}"
    return out


def legistar_items(start):
    out = []
    try:
        ms = P.get(f"{LEG}/matters", params={"$filter": f"MatterIntroDate ge datetime'{start}'", "$top": 1000},
                   sleep=0.25).json()
    except Exception as e:  # noqa: BLE001
        log(f"legistar: {e}")
        return out
    for m in ms:
        body = PZ if "Planning" in (m.get("MatterBodyName") or "") else (CC if m.get("MatterBodyName") == "City Council" else None)
        d = (m.get("MatterAgendaDate") or m.get("MatterIntroDate") or "")[:10]
        if not body or not d:
            continue
        out.append({"src": "legistar", "date": d, "first_seen": (m.get("MatterIntroDate") or d)[:10], "body": body,
                    "text": P.sq(m.get("MatterTitle")), "url": f"{LEG_SITE}/LegislationDetail.aspx?ID={m['MatterId']}",
                    "minutes": None, "atts": [], "docs": None})
    return out


MINUTES = {}  # (body, meeting date) -> attachment url of draft minutes posted on a later agenda


def civicclerk_items(start, end):
    out = []
    for ev in cc.events(start, end):
        name = ev.get("eventName") or ""
        if not ev.get("agendaId") or re.search(r"Workshop|Redevelopment|\bCRA\b|Adjustment|Angel", name, re.I):
            continue
        body = PZ if re.search(r"Planning", name, re.I) else (CC if re.search(r"City Council", name, re.I) else None)
        if not body:
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
        for it, _parent in items:
            ma = P.minutes_attachment(it)
            if ma and re.search(r"council|planning|zoning", ma[2], re.I) and not re.search(r"CRA|Redevelopment|Angel", ma[2]):
                mb = PZ if re.search(r"planning|zoning", ma[2], re.I) else CC
                MINUTES.setdefault((mb, ma[0]), ma[1])
            t = P.item_name(it) + " " + P.item_desc(it)
            if not kind_of(t):
                continue
            out.append({"src": "civicclerk", "date": d, "first_seen": min(pub + [d]), "body": body, "text": P.sq(t),
                        "url": cc.event_url(ev["id"]), "minutes": mins, "atts": it.get("attachmentsList") or [],
                        "docs": cc.file_url(packet) if packet else None})
    return out


def fetch(since, refresh):
    today = dt.date.today()
    start = (dt.date.fromisoformat(since) - dt.timedelta(days=60))
    refresh = {r.strip().upper() for r in (refresh or []) if r and r.strip()}
    if refresh:
        start = min(start, today - dt.timedelta(days=540))
    items = legistar_items(start.isoformat()) + civicclerk_items(start.isoformat(),
                                                                 (today + dt.timedelta(days=75)).isoformat())
    log(f"{len(items)} in-scope agenda items since {start}")
    # group items into cases: items sharing any case id, else the same ordinance number
    groups = []
    for it in items:
        k = kind_of(it["text"])
        if not k:
            continue
        info = parse(it["text"])
        ids = set(ids_of(it["text"])) - ({x for x in ids_of(it["text"]) if x.startswith("AX-")} if k != "CPA" else set())
        if k == "CPA":
            ids = {x for x in ids_of(it["text"]) if not x.startswith("AX-")} or ids
        if not ids and not info.get("ord"):
            continue
        it.update(kind=k, info=info, ids=ids)
        hit = [g for g in groups if (ids and g["ids"] & ids) or (info.get("ord") and info["ord"] in g["ords"]
                                                               and g["kind"] == k)]
        if hit:
            g = hit[0]
            for o in hit[1:]:
                g["ids"] |= o["ids"]; g["ords"] |= o["ords"]; g["items"] += o["items"]
                groups.remove(o)
        else:
            g = {"ids": set(), "ords": set(), "items": [], "kind": k}
            groups.append(g)
        g["ids"] |= ids
        if info.get("ord"):
            g["ords"].add(info["ord"])
        g["items"].append(it)
    out = []
    for g in groups:
        app = " / ".join(sorted(g["ids"])) if g["ids"] else f"Ordinance {sorted(g['ords'])[0]}"
        try:
            rec = build(app, g, today.isoformat())
        except Exception as e:  # noqa: BLE001
            log(f"{app}: skipped ({e})")
            continue
        in_refresh = app.upper() in refresh or any(i in r for r in refresh for i in g["ids"])
        if (rec["filed_date"] or "9999") < since and not in_refresh:
            continue
        out.append(rec)
    return out


def build(app, g, today):
    items = sorted(g["items"], key=lambda i: (i["date"], i["src"] != "civicclerk"))
    info = {}
    for it in reversed(items):  # latest wording first
        for k, v in it["info"].items():
            if v and not info.get(k):
                info[k] = v
    keys = [f"ORDINANCE NO. {o}" for o in sorted(g["ords"])] + sorted(g["ids"])
    hearings = {}
    for it in items:
        rd = P.reading_of(it["text"])
        if it["body"] == CC and not rd and not re.search(r"FIRST READING", it["text"], re.I):
            rd = "final" if re.search(r"PUBLIC HEARING", it["text"], re.I) else None
        res = None
        if it["date"] <= today:
            if it["minutes"]:
                res = P.outcome_near(cc.text_of_file(it["minutes"]), keys)
            if res is None and MINUTES.get((it["body"], it["date"])):
                res = P.outcome_near(cc.text_of_url(MINUTES[(it["body"], it["date"])]), keys)
        if it["body"] == PZ and res in ("Approved", "Denied"):
            res = "Recommended " + ("Approval" if res == "Approved" else "Denial")
        key = (it["body"], it["date"])
        cur = hearings.get(key)
        if cur is None or (res and not cur["result"]):
            hearings[key] = {"body": it["body"], "date": it["date"], "result": res, "reading": rd,
                             "url": it["url"], "docs": it["docs"]}
    hl = P.implied_first_readings(sorted(hearings.values(), key=lambda h: (h["date"], h["body"] != PZ)), CC)
    status, raw, decision = P.status_from_hearings(hl, today, final_body=CC, final_words=("second", "final"))
    hearings_out = []
    for h in hl:
        res = h["result"]
        if h["body"] == CC and res == "Approved" and h["reading"] == "first":
            res = "Approved on first reading"
        body = h["body"] + (f" ({h['reading']} reading)" if h["body"] == CC and h["reading"] else "")
        hearings_out.append({"body": body, "date": h["date"], "result": res})
    pins = list(info.get("pins") or [])
    acres = info.get("acres")
    if not pins and info.get("address"):
        p = P.parcel_lookup(info["address"], "PINELLAS PARK") or P.parcel_lookup(info["address"])
        if p and p.get("pin"):
            pins = [p["pin"]]
            if acres is None and p.get("acres"):
                acres = p["acres"]
    k = g["kind"]
    category = {"CPA": "Comp Plan Amendment", "PD": "PD", "RZ": "Rezoning"}[k]
    app_type = {"CPA": "Land Use Plan Map Amendment", "PD": "Planned Unit Development master plan",
                "RZ": "Zoning Map Amendment"}[k]
    if k == "RZ" and re.search(r"\bPUD\b|PLANNED UNIT", info.get("to") or "", re.I):
        category = "PD"
    latest = hl[-1] if hl else {}
    desc = items[-1]["text"]
    desc = re.sub(r"^ORDINANCE NO\.?\s*[\d.\-]*\s*[._]*\s*\.?\s*AN ORDINANCE OF THE CITY COUNCIL OF THE CITY OF PINELLAS PARK, "
                  r"PINELLAS COUNTY, FLORIDA,\s*", "", desc, flags=re.I)
    desc = desc.capitalize() if desc.isupper() else desc
    desc = desc if len(desc) <= 300 else desc[:297] + "..."
    return {
        "jurisdiction": JUR, "county": "Pinellas", "app_num": app, "category": category, "app_type": app_type,
        "project_name": info.get("address"), "description": desc, "applicant": info.get("applicant"),
        "parcel_ids": pins, "address": info.get("address"), "acres": acres,
        "from_zoning": info.get("from") if k != "CPA" else None, "to_zoning": info.get("to") if k != "CPA" else None,
        "from_flu": info.get("from") if k == "CPA" else None, "to_flu": info.get("to") if k == "CPA" else None,
        "filed_date": min(i["first_seen"] for i in items), "status": status, "status_raw": raw,
        "hearings": hearings_out, "decision_date": decision, "url": latest.get("url") or cc.portal,
        "docs_url": latest.get("docs"), "lat": None, "lon": None, "source": SOURCE,
        "ordinances": sorted(g["ords"]) or None,
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
