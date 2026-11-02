#!/usr/bin/env python3
"""City of Clearwater (Pinellas) rezonings, land use plan amendments and annexations with initial
zoning / future land use (Legistar web API: clearwater).

Clearwater planning cases carry a case number that ties every agenda matter together:
  ANX2026-05006  annexation + initial Future Land Use Map + initial Zoning Atlas designation
  LUP2025-11005  future land use map amendment          REZ2026-xxxxx  zoning atlas amendment
  CPA2025-10001  comprehensive plan amendment (kept only when it maps a specific site)
The planning-case matter ("Approve the annexation ... and pass Ordinances 9910-26, 9911-26 and
9912-26 on first reading") is heard first; each ordinance then gets its own second-reading matter
("9911-26 2nd rdg", MatterName "Second Reading Ord.# 9911-26, ANX2026-05006"). Hearing dates and
results come from matter histories (City Council, Community Development Board); a future
MatterAgendaDate with no history yet is a scheduled hearing. Site address -> Pinellas parcel id
through the county PA layer.
"""
import datetime as dt
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import legistar_links as LL  # noqa: E402
import pinellas_common as P  # noqa: E402

JURISDICTIONS = ["City of Clearwater"]
JUR = "City of Clearwater"
LEG = "https://webapi.legistar.com/v1/clearwater"
SITE = "https://clearwater.legistar.com"
SOURCE = "Clearwater Legistar (Community Development Board + City Council)"
P.set_tag("clearwater")
log = P.log

CASE_RE = re.compile(r"\b(ANX|LUP|REZ|CPA)\s?(\d{4})-(\d{5})\b", re.I)
ORD_RE = re.compile(r"\b(\d{4})-(\d{2})\b")
BODIES = {"City Council": "City Council", "Community Development Board": "Community Development Board"}
SEL = "MatterId,MatterFile,MatterName,MatterTitle,MatterTypeName,MatterIntroDate,MatterAgendaDate,MatterStatusName"


def get_json(url, params=None):
    return P.get(url, params=params, sleep=0.25).json()


def get_all(flt):
    out, skip = [], 0
    while True:
        d = get_json(f"{LEG}/matters", {"$filter": flt, "$top": 1000, "$skip": skip, "$select": SEL})
        out.extend(d)
        if len(d) < 1000:
            return out
        skip += 1000


def text_of(m):
    return " ".join(P.sq(m.get(k)) for k in ("MatterFile", "MatterName", "MatterTitle"))


def case_of(m):
    mm = CASE_RE.search(text_of(m))
    return f"{mm.group(1).upper()}{mm.group(2)}-{mm.group(3)}" if mm else None


def ords_in(text):
    return {f"{a}-{b}" for a, b in ORD_RE.findall(text or "") if 9000 <= int(a) <= 9999}


def ord_of_matter(m):
    mm = re.match(r"\s*(\d{4}-\d{2})\b", m.get("MatterFile") or "")
    return mm.group(1) if mm else None


def result_of(action, passed, body):
    a = (action or "").lower()
    if passed and passed.lower().startswith("fail"):
        return None
    if "withdr" in a:
        return "Withdrawn"
    if any(w in a for w in ("contin", "table", "postpon", "defer")):
        return "Continued"
    adv = body == "Community Development Board"
    if "den" in a:
        return "Recommended Denial" if adv else "Denied"
    if any(w in a for w in ("approv", "adopt", "pass", "recommend")):
        return "Recommended Approval" if adv else "Approved"
    return None


def parse(title):
    t = P.sq(title)
    out = {}
    m = re.search(r"whose post office address is\s+(.+?),\s*(?:Clearwater|Largo|Dunedin|Safety Harbor)", t, re.I) or \
        re.search(r"\bfor\s+(\d+\s+[A-Za-z0-9 .'-]+?(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Boulevard|Blvd|Lane|Ln|"
                  r"Court|Ct|Way|Place|Pl|Circle|Cir|Highway|Hwy|Parkway|Pkwy|Terrace|Ter|Trail|Trl)\.?)\b", t, re.I)
    if not m:
        m = re.search(r"(?:property|parcels?|site)\s+(?:generally\s+)?located at\s+(\d+[^(;,]+?)(?:\s*\(|,|;|\s+and\s+pass|$)", t, re.I)
    if m:
        out["address"] = m.group(1).strip(" ,.")
    m = re.search(r"Future Land Use Map designations? of\s+(.+?)(?:\s+and initial|\s+and\s+Zoning|\s+for\s+\d|;|$)", t, re.I)
    if m:
        out["to_flu"] = m.group(1).strip(" ,")
    m = re.search(r"Zoning Atlas designations? of\s+(.+?)(?:\s+for\s+\d|\s+and pass|;|$)", t, re.I)
    if m:
        out["to_zoning"] = m.group(1).strip(" ,")
    m = re.search(r"\bfrom\s+(.+?)\s+to\s+(.+?)(?:\s+for\s+|\s+on\s+|,|;|$)", t, re.I)
    if m and re.search(r"\([A-Z0-9-]{1,8}\)|District|Residential|Commercial|Industrial|Mixed|Institutional", m.group(0)):
        clean = lambda x: re.sub(r"^the\s+|\s+(?:category|district|designation)$", "", x.strip(), flags=re.I)
        out["from"], out["to"] = clean(m.group(1)), clean(m.group(2))
    ac = re.search(r"([\d,]*\.?\d+)\s*(?:\+/-\s*)?acres?\b", t, re.I)
    if ac:
        try:
            out["acres"] = float(ac.group(1).replace(",", ""))
        except ValueError:
            pass
    out["pins"] = P.pins_in(t)
    return out


def fetch(since, refresh):
    today = P.iso_today()
    start = (dt.date.fromisoformat(since) - dt.timedelta(days=45)).isoformat()
    recent = get_all(f"MatterIntroDate ge datetime'{start}'")
    refresh = [r.strip().upper() for r in (refresh or []) if r and r.strip()]
    log(f"{len(recent)} matters since {start}; {len(refresh)} refresh ids")
    by_case = {}
    for m in recent:
        c = case_of(m)
        if c:
            by_case.setdefault(c, {})[m["MatterId"]] = m
    for c in refresh:
        if c in by_case:
            continue
        try:
            ms = get_json(f"{LEG}/matters", {"$filter": f"substringof('{c}',MatterName) or substringof('{c}',MatterFile) "
                                                        f"or substringof('{c}',MatterTitle)", "$select": SEL})
        except Exception as e:  # noqa: BLE001
            log(f"refresh {c}: {e}")
            continue
        for m in ms:
            if case_of(m) == c:
                by_case.setdefault(c, {})[m["MatterId"]] = m
    # ordinance matters that do not name the case ("9884-26 3rd rdg") join through the ordinance numbers
    ord_matters = {}
    for m in recent:
        o = ord_of_matter(m)
        if o and not case_of(m):
            ord_matters.setdefault(o, []).append(m)
    for c, ms in by_case.items():
        ords = set()
        for m in ms.values():
            ords |= ords_in(text_of(m))
        for o in ords:
            for m in ord_matters.get(o, []):
                ms.setdefault(m["MatterId"], m)
    # event dates -> body, for scheduled agenda dates that have no history yet
    ev_body = {}
    try:
        for e in get_json(f"{LEG}/events", {"$filter": f"EventDate ge datetime'{start}'"}):
            b = BODIES.get(e.get("EventBodyName"))
            if b:
                ev_body.setdefault(e["EventDate"][:10], set()).add(b)
    except Exception as e:  # noqa: BLE001
        log(f"events: {e}")
    out = []
    for c, ms in sorted(by_case.items()):
        try:
            rec = build(c, list(ms.values()), ev_body, today)
        except Exception as e:  # noqa: BLE001
            log(f"{c}: skipped ({e})")
            continue
        if not rec:
            continue
        if (rec["filed_date"] or "9999") < since and c not in refresh:
            continue
        out.append(rec)
    return out


def build(case, matters, ev_body, today):
    kind = case[:3]
    matters = sorted(matters, key=lambda m: ((m.get("MatterIntroDate") or ""), m["MatterId"]))
    main = next((m for m in matters if (m.get("MatterFile") or "").upper().startswith(case)), matters[0])
    title_all = " ".join(P.sq(m.get("MatterTitle")) for m in matters)
    info = parse(P.sq(main.get("MatterTitle")))
    for m in matters:  # ordinance titles carry the street address
        extra = parse(m.get("MatterTitle"))
        for k, v in extra.items():
            if v and not info.get(k):
                info[k] = v
    if kind == "CPA" and not re.search(r"Future Land Use Map|Zoning Atlas", title_all, re.I):
        return None  # text-only comprehensive plan amendment
    if not info.get("address") and not info.get("pins"):
        if re.search(r"city[- ]initiated|overlay|citywide|community redevelopment area", title_all, re.I):
            return None  # area-wide amendment with no site
    hearings = {}
    for m in matters:
        rd = P.reading_of(text_of(m)) or ("first" if (m.get("MatterFile") or "").upper().startswith(case) else None)
        try:
            hist = get_json(f"{LEG}/matters/{m['MatterId']}/histories")
        except Exception as e:  # noqa: BLE001
            log(f"{case}: histories {m['MatterId']}: {e}")
            hist = []
        seen = False
        for h in hist:
            body = BODIES.get(h.get("MatterHistoryActionBodyName"))
            if not body:
                continue
            d = (h.get("MatterHistoryActionDate") or "")[:10]
            res = result_of(h.get("MatterHistoryActionName"), h.get("MatterHistoryPassedFlagName"), body)
            r2 = P.reading_of(h.get("MatterHistoryActionName")) or rd
            if body == "City Council" and res == "Approved" and r2 == "first":
                res_txt = "Approved on first reading"
            else:
                res_txt = res
            k = (body, d)
            cur = hearings.get(k)
            if cur is None or (res and not cur["result"]):
                hearings[k] = {"body": body, "date": d, "result": res_txt, "reading": r2,
                               "_res": res}
            seen = True
        ad = (m.get("MatterAgendaDate") or "")[:10]
        if ad and not seen and ad >= (m.get("MatterIntroDate") or "")[:10]:
            bodies = ev_body.get(ad) or {"City Council"}
            body = "City Council" if "City Council" in bodies else sorted(bodies)[0]
            hearings.setdefault((body, ad), {"body": body, "date": ad, "result": None, "reading": rd, "_res": None})
    hl = sorted(hearings.values(), key=lambda h: (h["date"], h["body"] != "Community Development Board"))
    for h in hl:
        if h["body"] == "City Council" and h.get("reading"):
            h["body"] = f"City Council ({h['reading']} reading)"
    calc = [dict(h, result=h["_res"]) for h in hl]
    status, raw, decision = P.status_from_hearings(calc, today, final_body="City Council",
                                                   final_words=("second", "third"))
    hearings_out = [{"body": h["body"], "date": h["date"], "result": h["result"]} for h in hl]

    addr = info.get("address")
    pins = info.get("pins") or []
    acres = info.get("acres")
    if addr and not pins:
        p = P.parcel_lookup(addr, "CLEARWATER") or P.parcel_lookup(addr)
        if p and p.get("pin"):
            pins = [p["pin"]]
            if acres is None and p.get("acres"):
                acres = p["acres"]
    if kind == "LUP" or (kind == "CPA"):
        category, app_type = "Comp Plan Amendment", "Future Land Use Map Amendment" if kind == "LUP" else "Comprehensive Plan Amendment"
    elif kind == "ANX":
        category, app_type = "Rezoning", "Annexation with initial Future Land Use and Zoning Atlas designation"
    else:
        category, app_type = "Rezoning", "Zoning Atlas Amendment"
    to_z = info.get("to_zoning") or (info.get("to") if kind == "REZ" else None)
    if category == "Rezoning" and re.search(r"Planned Development|\bPD\b|\bPUD\b", to_z or "", re.I):
        category = "PD"
    desc = P.sq(main.get("MatterTitle"))
    desc = desc if len(desc) <= 300 else desc[:297] + "..."
    filed = min((m.get("MatterIntroDate") or "9999")[:10] for m in matters)
    url = LL.site_link(LEG, [main] + [m for m in matters if m is not main]) or f"{SITE}/Calendar.aspx"
    docs = None
    try:
        atts = get_json(f"{LEG}/matters/{main['MatterId']}/attachments")
        rpt = [a for a in atts if re.search(r"staff|report|map|application", a.get("MatterAttachmentName") or "", re.I)]
        pick = rpt or atts
        if pick:
            docs = pick[0].get("MatterAttachmentHyperlink")
    except Exception:  # noqa: BLE001
        pass
    return {
        "jurisdiction": JUR, "county": "Pinellas", "app_num": case, "category": category, "app_type": app_type,
        "project_name": addr, "description": desc, "applicant": None, "parcel_ids": pins, "address": addr,
        "acres": acres,
        "from_zoning": info.get("from") if kind == "REZ" else None, "to_zoning": to_z,
        "from_flu": info.get("from") if kind == "LUP" else None,
        "to_flu": info.get("to_flu") or (info.get("to") if kind == "LUP" else None),
        "filed_date": None if filed == "9999" else filed, "status": status, "status_raw": raw,
        "hearings": hearings_out, "decision_date": decision, "url": url, "docs_url": docs,
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
