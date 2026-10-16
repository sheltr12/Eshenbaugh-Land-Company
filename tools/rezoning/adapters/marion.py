#!/usr/bin/env python3
"""Marion County rezoning adapter (Legistar web API: marionfl).

Cases appear as agenda matters before the Planning & Zoning Commission and the BOCC.
Each body gets its own matter; the county case number (e.g. 261010ZP, 260904ZC, 26-S07,
26-L02) at the start of the title ties them together.
  ZC = zoning change (Rezoning), ZP = PUD zoning change / PUD amendment (PD),
  YY-Snn = small-scale land use change, YY-Lnn = large-scale (Comp Plan Amendment).
Hearing results come from event items (agenda actions), which are more complete than
matter histories in this Legistar instance.
"""
import datetime as dt
import json
import re
import sys
import time

import requests
import os as _os_ll
sys.path.insert(0, _os_ll.path.dirname(_os_ll.path.abspath(__file__)))
import legistar_links as LL  # noqa: E402

JURISDICTIONS = ["Marion County"]

LEG = "https://webapi.legistar.com/v1/marionfl"
SITE = "https://marionfl.legistar.com"
SOURCE = "Marion County Legistar (P&Z Commission + BOCC agendas)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"

# P&Z Commission, BOCC, BOCC Public Hearing Meeting, BOCC Planning and Zoning, Special Board Mtg
BODY_IDS = [139, 138, 194, 229, 231]
CASE_RE = re.compile(r"^\s*(?:CPA\s*)?(\d{6}\s?(?:ZC|ZP)|\d{2}-[SL]\d{2,3})\b", re.I)

_s = requests.Session()
_s.headers.update({"User-Agent": UA, "Accept": "application/json"})


def log(*a):
    print("[marion]", *a, file=sys.stderr)


def get_json(url, params=None, tries=3, sleep=0.3):
    last = None
    for i in range(tries):
        try:
            r = _s.get(url, params=params, timeout=60)
            r.raise_for_status()
            time.sleep(sleep)
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


def get_all(path, flt, select=None):
    out, skip = [], 0
    while True:
        p = {"$filter": flt, "$top": 1000, "$skip": skip}
        if select:
            p["$select"] = select
        d = get_json(f"{LEG}/{path}", p)
        out.extend(d)
        if len(d) < 1000:
            return out
        skip += 1000


def esc(s):
    return s.replace("'", "''")


def case_of(title):
    m = CASE_RE.match(title or "")
    return m.group(1).replace(" ", "").upper() if m else None


def body_label(name):
    n = (name or "").lower()
    if "planning & zoning commission" in n or "planning and zoning commission" in n:
        return "Planning & Zoning Commission"
    if "county commissioners" in n or "special board" in n:
        return "BOCC"
    return (name or "").strip() or None


def category_of(case, title):
    if case.endswith("ZP"):
        return "PD"
    if case.endswith("ZC"):
        return "Rezoning"
    return "Comp Plan Amendment"


def in_scope(case, title):
    t = title or ""
    if re.search(r"Final Planned Unit Development Master Plan", t, re.I) and "Zoning Change" not in t:
        return False  # final PUD master plan = site plan approval
    if re.search(r"Developer.s Agreement", t, re.I) and "Zoning Change" not in t:
        return False
    if re.match(r"\d{2}-L", case) and re.search(r"Text Amendment", t, re.I) \
            and not re.search(r"Map Amendment|Land Use (?:Change|Designation)", t, re.I):
        return False  # text-only CPA
    return True


# ------------------------------------------------------------- parsing
PARCEL_RE = re.compile(r"\b\d{4,5}-[0-9A-Z]{2,4}[-+][0-9A-Z]{2,3}\b")


def parse_title(title):
    t = re.sub(r"\s+", " ", title or "").strip()
    out = {}
    m = re.match(r"^\s*(?:CPA\s*)?\S+\s*-\s*(.+?),\s*(?:(?:is |are )?request(?:s|ing)?\b|Zoning Change|"
                 r"Small[- ]Scale|Large[- ]Scale|Land Use Change|Comprehensive Plan|Request)", t, re.I)
    if m:
        out["applicant"] = m.group(1).strip(" ,")
    m = re.search(r"\(PL ([^)]+)\)", t)
    if m:
        out["pl"] = m.group(1).strip()
    m = re.search(r"\bfrom (.+?) to (?:an? )?(.+?)(?:,| Zone\b| for\b| to Allow| on\b|$)", t)
    if m and re.search(r"\([A-Z][A-Za-z0-9\- ,]*\)|Residential|Commercial|Agricultur|Business|Industrial|"
                       r"Rural|Employment|Public|Preservation|Planned Unit", m.group(1) + m.group(2)):
        out["from"], out["to"] = m.group(1).strip(), m.group(2).strip()
    acres = re.findall(r"([\d,]*\.?\d+)\s*(?:\+/-\s*)?Acres?\b", t, re.I)
    if acres:
        try:
            out["acres"] = float(acres[0].replace(",", ""))
        except ValueError:
            pass
    out["parcels"] = list(dict.fromkeys(p.replace("+", "-") for p in PARCEL_RE.findall(t)))
    m = re.search(r"Site Address(?:es)?\s+(.+?)(?:\s*\(CASE|$)", t)
    if m:
        out["address"] = m.group(1).strip(" .,")
    out["withdrawn"] = bool(re.search(r"\bWITHDRAWN\b", t))
    return out


def classify(action, passed, text, body):
    a = (action or "").lower()
    txt = (text or "").lower()
    if "withdr" in a or "withdrew" in txt or "withdrawn" in txt:
        return "Withdrawn"
    if passed and passed.lower().startswith("fail"):
        return f"Motion to {action} failed" if action else None
    if "contin" in a or "table" in a or "postpone" in a or "defer" in a:
        return "Continued"
    if "transmit" in a or "transmit" in txt:
        return "Transmitted"
    pz = body == "Planning & Zoning Commission"
    if "deny" in a or "denial" in a or "denied" in a:
        return "Recommended Denial" if pz else "Denied"
    if "approv" in a or "adopt" in a:
        return "Recommended Approval" if pz else "Approved"
    return action or None


# ------------------------------------------------------------- fetch
def fetch(since, refresh):
    today = dt.date.today().isoformat()
    bodies = " or ".join(f"MatterBodyId eq {b}" for b in BODY_IDS)
    flt = f"MatterIntroDate ge datetime'{since}' and ({bodies})"
    recent = get_all("matters", flt, "MatterId,MatterTitle,MatterIntroDate")
    cases = {}
    for m in recent:
        c = case_of(m.get("MatterTitle"))
        if c:
            cases.setdefault(c, None)
    refresh = [r.strip().upper() for r in (refresh or []) if r and r.strip()]
    for r in refresh:
        cases.setdefault(r, None)
    log(f"{len(recent)} matters since {since}; {len(cases)} candidate cases")

    # all matters for each case (across bodies and dates)
    case_matters = {}
    for c in list(cases):
        try:
            ms = get_json(f"{LEG}/matters", {"$filter": f"substringof('{esc(c)}',MatterTitle)"})
        except Exception as e:  # noqa: BLE001
            log(f"{c}: matter lookup failed: {e}")
            continue
        ms = [m for m in ms if case_of(m.get("MatterTitle")) == c and
              (m.get("MatterBodyId") in BODY_IDS or body_label(m.get("MatterBodyName")) == "BOCC")]
        ms = [m for m in ms if not re.search(r"Developer.s Agreement", m.get("MatterTitle") or "", re.I)]
        if not ms:
            continue
        first = min((m.get("MatterIntroDate") or "9999")[:10] for m in ms)
        if first < since and c not in refresh:
            continue  # first appeared before the window
        case_matters[c] = ms
    if not case_matters:
        return []
    first_intro = min((m.get("MatterIntroDate") or "9999")[:10] for ms in case_matters.values() for m in ms)

    # events (with agenda actions) in the window
    ev_flt = f"EventDate ge datetime'{first_intro}' and (" + \
        " or ".join(f"EventBodyId eq {b}" for b in BODY_IDS) + ")"
    events = get_all("events", ev_flt)
    wanted = {m["MatterId"] for ms in case_matters.values() for m in ms}
    by_matter = {}
    for ev in sorted(events, key=lambda e: e["EventDate"]):
        try:
            items = get_json(f"{LEG}/events/{ev['EventId']}/eventitems")
        except Exception as e:  # noqa: BLE001
            log(f"event {ev['EventId']}: {e}")
            continue
        acts = [i for i in items if i.get("EventItemActionName")]
        for it in items:
            mid = it.get("EventItemMatterId")
            if mid not in wanted:
                continue
            action, passed, text = it.get("EventItemActionName"), it.get("EventItemPassedFlagName"), \
                it.get("EventItemActionText")
            if not action:
                # grouped motion recorded on a sibling item ("approve agenda items 15.2.1. and 15.2.2.")
                num = (it.get("EventItemAgendaNumber") or "").rstrip(".")
                if num:
                    for o in acts:
                        if re.search(rf"(?<![\d.]){re.escape(num)}\.?(?![\d])", o.get("EventItemActionText") or "") \
                                and o.get("EventItemPassedFlagName") == "Pass":
                            action, passed, text = o["EventItemActionName"], "Pass", o["EventItemActionText"]
                            break
            if not action:
                # consent items inherit the "approve the Consent Agenda" motion of their parent heading
                num = (it.get("EventItemAgendaNumber") or "").rstrip(".")
                parent = num.rsplit(".", 1)[0] if "." in num else None
                for o in acts:
                    if parent and (o.get("EventItemAgendaNumber") or "").rstrip(".") == parent \
                            and o.get("EventItemPassedFlagName") == "Pass" \
                            and re.search(r"consent", (o.get("EventItemTitle") or "") + (o.get("EventItemActionName") or ""), re.I):
                        action, passed, text = o["EventItemActionName"], "Pass", "Approved on consent agenda"
                        break
            by_matter.setdefault(mid, []).append({
                "date": ev["EventDate"][:10], "body": body_label(ev.get("EventBodyName")),
                "action": action, "passed": passed, "text": text,
                "event_url": ev.get("EventInSiteURL"), "agenda": ev.get("EventAgendaFile"),
            })
    log(f"scanned {len(events)} events from {first_intro}")

    out = []
    for c, ms in sorted(case_matters.items()):
        try:
            rec = build(c, ms, by_matter, today)
        except Exception as e:  # noqa: BLE001
            log(f"{c}: skipped ({e})")
            continue
        if rec is None:
            continue
        if rec["filed_date"] and rec["filed_date"] < since and c not in refresh:
            continue
        out.append(rec)
    return out


def build(case, matters, by_matter, today):
    matters = sorted(matters, key=lambda m: ((m.get("MatterAgendaDate") or m.get("MatterIntroDate") or ""),
                                             m["MatterId"]))
    titles = [m.get("MatterTitle") or "" for m in matters]
    main_title = max(titles, key=lambda t: len(parse_title(t).get("parcels", [])) * 1000 + len(t))
    if not in_scope(case, main_title):
        return None
    info = parse_title(main_title)
    parcels = []
    for t in titles:
        for p in parse_title(t)["parcels"]:
            if p not in parcels:
                parcels.append(p)

    # hearings
    hmap = {}
    for m in matters:
        evs = by_matter.get(m["MatterId"], [])
        for e in evs:
            res = classify(e["action"], e["passed"], e["text"], e["body"])
            key = (e["body"], e["date"])
            cur = hmap.get(key)
            # prefer a decisive (passed) result over failed motions / blanks
            if cur is None or (res and (cur["result"] is None or "failed" in (cur["result"] or ""))):
                hmap[key] = {"body": e["body"], "date": e["date"], "result": res}
        ad = (m.get("MatterAgendaDate") or "")[:10]
        if ad and not evs:
            b = body_label(m.get("MatterBodyName"))
            hmap.setdefault((b, ad), {"body": b, "date": ad, "result": None})
    hearings = sorted(hmap.values(), key=lambda h: (h["date"], h["body"] != "Planning & Zoning Commission"))

    is_lscpa = bool(re.match(r"\d{2}-L", case))
    status, raw, decision = None, None, None
    wd = [h for h in hearings if h["result"] == "Withdrawn"]
    final = [h for h in hearings if h["body"] == "BOCC" and h["result"] in ("Approved", "Denied")]
    if is_lscpa:
        final = [h for h in final if h["date"] == (final[-1]["date"] if final else None)] if len(final) >= 2 else []
    past = [h for h in hearings if h["date"] <= today]
    future = [h for h in hearings if h["date"] > today]
    if wd or any(parse_title(t)["withdrawn"] for t in titles):
        status, raw = "Withdrawn", (f"{wd[-1]['body']} {wd[-1]['date']}: withdrawn" if wd else "CASE WITHDRAWN (agenda title)")
    elif final:
        status, decision = final[-1]["result"], final[-1]["date"]
        raw = f"BOCC {final[-1]['date']}: {final[-1]['result']}"
    elif past and past[-1]["result"] == "Continued":
        status, raw = "Continued", f"{past[-1]['body']} {past[-1]['date']}: continued"
        if future:
            raw += f"; next {future[0]['body']} {future[0]['date']}"
    elif future:
        status, raw = "Hearing Scheduled", f"next {future[0]['body']} {future[0]['date']}"
        if past and past[-1]["result"]:
            raw = f"{past[-1]['body']} {past[-1]['date']}: {past[-1]['result']}; " + raw
    elif past and past[-1]["result"] is None:
        status, raw = "Hearing Scheduled", f"{past[-1]['body']} {past[-1]['date']}: result not yet posted"
    elif past:
        # advisory hearing done, BOCC hearing not yet set -> pending ("Filed" per spec)
        status, raw = "Filed", f"{past[-1]['body']} {past[-1]['date']}: {past[-1]['result']}; BOCC hearing not yet scheduled"
    else:
        status, raw = "Filed", "On agenda"

    main = matters[-1]
    for m in reversed(matters):
        if body_label(m.get("MatterBodyName")) == "BOCC":
            main = m
            break
    url = LL.site_link(LEG, [main] + [m for m in matters if m is not main]) or f"{SITE}/Calendar.aspx"
    docs = None
    try:
        pz = next((m for m in reversed(matters) if body_label(m.get("MatterBodyName")) != "BOCC"), main)
        atts = get_json(f"{LEG}/matters/{pz['MatterId']}/attachments")
        rpt = [a for a in atts if re.search(r"rpt|report|staff", a.get("MatterAttachmentName") or "", re.I)]
        pick = (rpt or atts)
        if pick:
            docs = pick[-1].get("MatterAttachmentHyperlink")
    except Exception:  # noqa: BLE001
        pass

    cat = category_of(case, main_title)
    frm, to = info.get("from"), info.get("to")
    desc_t = re.sub(r"\s+", " ", main_title).strip()
    desc_t = re.sub(r"^\S+\s*-\s*", "", desc_t)
    desc = desc_t if len(desc_t) <= 300 else desc_t[:297] + "..."
    filed = min((m.get("MatterIntroDate") or "9999")[:10] for m in matters)
    return {
        "jurisdiction": "Marion County", "county": "Marion", "app_num": case, "category": cat,
        "app_type": {"PD": "Zoning Change (PUD)", "Rezoning": "Zoning Change"}.get(cat) or
        ("Large-Scale Land Use Change" if is_lscpa else "Small-Scale Land Use Change"),
        "project_name": None, "description": desc, "applicant": info.get("applicant"),
        "parcel_ids": parcels, "address": info.get("address"), "acres": info.get("acres"),
        "from_zoning": frm if cat != "Comp Plan Amendment" else None,
        "to_zoning": to if cat != "Comp Plan Amendment" else None,
        "from_flu": frm if cat == "Comp Plan Amendment" else None,
        "to_flu": to if cat == "Comp Plan Amendment" else None,
        "filed_date": None if filed == "9999" else filed, "status": status, "status_raw": raw,
        "hearings": hearings, "decision_date": decision, "url": url, "docs_url": docs,
        "lat": None, "lon": None, "source": SOURCE, "pl_number": info.get("pl"),
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
