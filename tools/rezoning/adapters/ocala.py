#!/usr/bin/env python3
"""City of Ocala (Marion County) rezoning / PD / future land use map amendment adapter.

Source: the city's Legistar web API (webapi.legistar.com/v1/ocala), same platform as marion.py.
 * Land use cases appear as agenda matters before the Planning & Zoning Commission (body 186) and
   City Council (body 138, special meetings 237). Every title carries the city case number
   "(Case ZON26-0002)", plus parcel numbers "(Parcel 23835-000-00)", acreage and from/to districts.
     ZONyy-nnnn = rezoning (category PD when the target district is PD/PUD)
     PDyy-nnnn  = Planned Development rezoning (ordinance) + PD Plan and Standards Book (resolution)
     LUCyy-nnnn = future land use map change (small or large scale)
   Annexations (ANX), code amendments (COD), FLU policy text (FLUP), subdivisions, abrogations,
   CDDs and development agreements are skipped.
 * One case has several matters (P&Z item, Council first reading "Introduced", Council second
   reading "Adopted", PD resolution); they are grouped by case number.
 * Hearing results come from event items (agenda actions recorded by the clerk).
Cases surface when they first reach a P&Z agenda; filed_date = earliest matter intro date.
"""
import datetime as dt
import json
import os
import re
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import legistar_links as LL  # noqa: E402

JURISDICTIONS = ["City of Ocala"]
JUR = "City of Ocala"
LEG = "https://webapi.legistar.com/v1/ocala"
SITE = "https://ocala.legistar.com"
SOURCE = "Ocala Legistar (Planning & Zoning Commission + City Council agendas)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
BODY_IDS = [186, 138, 237]
BODY = {186: "Planning & Zoning Commission", 138: "City Council", 237: "City Council"}
CASE_RE = re.compile(r"\b(ZON|PD|LUC)(\d{2})-(\d{3,6})\b")
PARCEL_RE = re.compile(r"\b\d{4,5}[-+][0-9A-Z]{3}[-+][0-9A-Z]{2,3}\b")

_s = requests.Session()
_s.headers.update({"User-Agent": UA, "Accept": "application/json"})


def log(*a):
    print("[ocala]", *a, file=sys.stderr, flush=True)


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


def cases_of(title):
    return list(dict.fromkeys(f"{a}{b}-{c}" for a, b, c in CASE_RE.findall(title or "")))


def clean(s):
    s = re.sub(r"\s+", " ", s or "").strip(" .,;")
    return s or None


def body_label(bid, name=None):
    return BODY.get(bid) or (name or "").strip() or None


# ------------------------------------------------------------------ title parsing
def strip_prefix(t):
    t = re.sub(r"\s+", " ", t or "").strip()
    t = re.sub(r"^Request to postpone.*?(?:meeting|Commission)\s*[-:]\s*", "", t, flags=re.I)
    return t


def parse_title(title):
    t = strip_prefix(title)
    out = {"title": t}
    pids = []
    for p in PARCEL_RE.findall(t):
        p = p.replace("+", "-")
        if p not in pids:
            pids.append(p)
    out["parcels"] = pids
    m = re.search(r"\b(?:approximately|portion \()\s*([\d,]*\.?\d+)\s*acres", t, re.I) or \
        re.search(r"([\d,]*\.?\d+)\s*(?:\+/-\s*)?acres", t, re.I)
    if m:
        try:
            out["acres"] = float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    m = re.search(r"\bfrom\s+(.+?)\s+to\s+(?:an?\s+)?(.+?)(?=\s*\((?:Case|Parcel)|,\s*for\b|\s+for\s+(?:a\s+)?(?:portion|propert)|"
                  r",\s*approximately|\s+approximately|$)", t, re.I)
    if m:
        out["from"], out["to"] = clean(m.group(1)), clean(m.group(2))
    m = re.search(r"(?:(?:located|property)\s+(?:at|in|east of|west of|north of|south of)\s+(?:the\s+)?)(.+?)(?=\s*\(Parcel|,\s*approximately|"
                  r"\s+from\s|$)", t, re.I)
    if m:
        out["address"] = clean(m.group(1))
    m = re.search(r"\(([^()]*\S\s+PD)\)", t)
    if m and re.search(r"Standards Book", t, re.I):
        out["pd_name"] = clean(m.group(1))
    out["is_ord"] = bool(re.search(r"\bOrdinance\b", t, re.I))
    out["is_res"] = bool(re.search(r"\bResolution\b", t, re.I)) and not out["is_ord"]
    out["transmit"] = bool(re.search(r"\btransmit", t, re.I))
    out["postpone"] = bool(re.match(r"^\s*Request to postpone", title or "", re.I))
    out["withdrawn"] = bool(re.search(r"\bwithdraw", t, re.I))
    return out


def in_scope(case, titles):
    if case.startswith("LUC"):
        return any(re.search(r"Future Land Use designation|land use", t, re.I) for t in titles)
    return True


# ------------------------------------------------------------------ outcomes
def classify(action, passed, body, kind):
    """kind: 'intro' | 'adopt' | 'transmit' | 'res' | None"""
    a = (action or "").lower()
    if not a:
        return None
    if "withdr" in a:
        return "Withdrawn"
    if passed and passed.lower().startswith("fail"):
        return "Denied" if body == "City Council" and kind in ("adopt", None) else f"Motion to {action} failed"
    if any(w in a for w in ("contin", "postpon", "table", "defer")):
        return "Continued"
    if "deni" in a or "deny" in a:
        return "Recommended Denial" if body.startswith("Planning") else "Denied"
    if body.startswith("Planning"):
        if "approv" in a or "recommend" in a or "pass" in a:
            return "Recommended Approval"
        return action
    if "introduc" in a:
        return "Introduced (first reading)"
    if kind == "transmit" and ("approv" in a or "transmit" in a):
        return "Transmitted"
    if "adopt" in a or "approv" in a or "pass" in a:
        if kind == "res":
            return "Approved (PD plan resolution)"
        return "Approved"
    return action


# ------------------------------------------------------------------ fetch
def fetch(since, refresh):
    today = dt.date.today().isoformat()
    refresh = [r.strip().upper() for r in (refresh or []) if r and r.strip()]
    bodies = " or ".join(f"MatterBodyId eq {b}" for b in BODY_IDS)
    look = (dt.date.fromisoformat(since) - dt.timedelta(days=30)).isoformat()
    try:
        recent = get_all("matters", f"MatterIntroDate ge datetime'{look}' and ({bodies})",
                         "MatterId,MatterTitle,MatterIntroDate")
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Ocala Legistar unreachable: {e}") from e
    cand = []
    for m in recent:
        for c in cases_of(m.get("MatterTitle")):
            if c not in cand:
                cand.append(c)
    for r in refresh:
        if CASE_RE.fullmatch(r) and r not in cand:
            cand.append(r)
    log(f"{len(recent)} matters since {look}; {len(cand)} candidate cases")

    case_matters = {}
    for c in cand:
        try:
            ms = get_json(f"{LEG}/matters", {"$filter": f"substringof('{c}',MatterTitle)"})
        except Exception as e:  # noqa: BLE001
            log(f"{c}: matter lookup failed: {e}")
            continue
        ms = [m for m in ms if c in cases_of(m.get("MatterTitle")) and m.get("MatterBodyId") in BODY_IDS
              and not re.search(r"^\s*Minutes|Development Agreement|annex\s", m.get("MatterTitle") or "", re.I)]
        if not ms:
            if c in refresh:
                log(f"refresh id {c} not found")
            continue
        if min((m.get("MatterIntroDate") or "9999")[:10] for m in ms) < since and c not in refresh:
            continue   # first appeared before the window
        case_matters[c] = ms
    if not case_matters:
        return []
    first_intro = min((m.get("MatterIntroDate") or "9999")[:10] for ms in case_matters.values() for m in ms)

    events = get_all("events", f"EventDate ge datetime'{first_intro}' and (" +
                     " or ".join(f"EventBodyId eq {b}" for b in BODY_IDS) + ")")
    wanted = {m["MatterId"] for ms in case_matters.values() for m in ms}
    by_matter = {}
    for ev in sorted(events, key=lambda e: e["EventDate"]):
        if ev["EventDate"][:10] > (dt.date.today() + dt.timedelta(days=90)).isoformat():
            continue
        try:
            items = get_json(f"{LEG}/events/{ev['EventId']}/eventitems")
        except Exception as e:  # noqa: BLE001
            log(f"event {ev['EventId']}: {e}")
            continue
        for it in items:
            mid = it.get("EventItemMatterId")
            if mid in wanted:
                by_matter.setdefault(mid, []).append({
                    "date": ev["EventDate"][:10], "body": body_label(ev.get("EventBodyId"), ev.get("EventBodyName")),
                    "action": it.get("EventItemActionName"), "passed": it.get("EventItemPassedFlagName"),
                    "title": it.get("EventItemTitle") or "", "event_url": ev.get("EventInSiteURL"),
                })
    log(f"scanned {len(events)} events from {first_intro}")

    out = []
    for c, ms in sorted(case_matters.items()):
        try:
            rec = build(c, ms, by_matter, today)
        except Exception as e:  # noqa: BLE001
            log(f"{c}: skipped ({type(e).__name__}: {e})")
            continue
        if rec is None:
            continue
        if rec["filed_date"] and rec["filed_date"] < since and c not in refresh:
            continue
        out.append(rec)
    return out


def build(case, matters, by_matter, today):
    matters = sorted(matters, key=lambda m: ((m.get("MatterIntroDate") or ""), m["MatterId"]))
    titles = [m.get("MatterTitle") or "" for m in matters]
    if not in_scope(case, titles):
        return None
    infos = [parse_title(t) for t in titles]
    main = max(infos, key=lambda x: (x["is_ord"], bool(x.get("from")), len(x["parcels"]), len(x["title"])))
    parcels = []
    for x in infos:
        for p in x["parcels"]:
            if p not in parcels:
                parcels.append(p)

    hmap = {}
    for m, info in zip(matters, infos):
        kind = "transmit" if info["transmit"] else ("res" if info["is_res"] else None)
        for e in by_matter.get(m["MatterId"], []):
            k = kind
            if e["body"] == "City Council" and not k:
                if re.search(r"introduc", e["action"] or "", re.I) or re.match(r"\s*Introduc", e["title"], re.I):
                    k = "intro"
                elif re.search(r"adopt", e["action"] or "", re.I) or re.match(r"\s*Adopt", e["title"], re.I):
                    k = "adopt"
            res = classify(e["action"], e["passed"], e["body"], k)
            if res is None and re.match(r"\s*Request to postpone", e["title"], re.I) and e["date"] <= today:
                res = "Continued"
            key = (e["body"], e["date"])
            h = hmap.get(key)
            if h is None:
                hmap[key] = {"body": e["body"], "date": e["date"], "result": res, "_kind": k}
            elif res and (h["result"] is None or (h["_kind"] == "res" and k != "res")):
                h["result"], h["_kind"] = res, k
        if not by_matter.get(m["MatterId"]) and m.get("MatterAgendaDate"):
            b = body_label(m.get("MatterBodyId"), m.get("MatterBodyName"))
            d = m["MatterAgendaDate"][:10]
            hmap.setdefault((b, d), {"body": b, "date": d, "result": None, "_kind": kind})
    hearings = sorted(hmap.values(), key=lambda h: (h["date"], h["body"] != "Planning & Zoning Commission"))

    past = [h for h in hearings if h["date"] <= today]
    future = [h for h in hearings if h["date"] > today]
    final = [h for h in past if h["body"] == "City Council" and h["result"] in ("Approved", "Denied")]
    decision = None
    if any(h["result"] == "Withdrawn" for h in hearings) or any(x["withdrawn"] for x in infos):
        status, raw = "Withdrawn", "Withdrawn (Legistar agenda)"
    elif final and not future:
        status, decision = final[-1]["result"], final[-1]["date"]
        raw = f"City Council {decision}: {'Adopted' if status == 'Approved' else 'Denied'}"
    elif past and past[-1]["result"] == "Continued":
        status, raw = "Continued", f"{past[-1]['body']} {past[-1]['date']}: postponed"
        if future:
            raw += f"; next {future[0]['body']} {future[0]['date']}"
    elif future:
        status, raw = "Hearing Scheduled", f"next {future[0]['body']} {future[0]['date']}"
        if past and past[-1]["result"]:
            raw = f"{past[-1]['body']} {past[-1]['date']}: {past[-1]['result']}; " + raw
    elif past and past[-1]["result"] is None:
        status, raw = "Hearing Scheduled", f"{past[-1]['body']} {past[-1]['date']}: result not yet posted"
    elif past:
        status = "Filed"
        raw = f"{past[-1]['body']} {past[-1]['date']}: {past[-1]['result']}; next hearing not yet on an agenda"
    else:
        status, raw = "Filed", "On agenda"

    frm, to = main.get("from"), main.get("to")
    if case.startswith("LUC"):
        cat, app_type = "Comp Plan Amendment", "Land Use Change (LUC)"
        if any(x["transmit"] for x in infos):
            app_type = "Land Use Change (LUC), large scale"
    elif case.startswith("PD") or re.search(r"\bP\.?U?\.?D\b|Planned (?:Unit )?Development", to or "", re.I):
        cat, app_type = "PD", "Planned Development (PD)" if case.startswith("PD") else "Rezoning (ZON) to PD"
    else:
        cat, app_type = "Rezoning", "Rezoning (ZON)"
    pd_name = next((x["pd_name"] for x in infos if x.get("pd_name")), None)
    acres = next((x["acres"] for x in [main] + infos if x.get("acres")), None)
    desc = re.sub(r"\s*\(Quasi-Judicial\)\.?", "", main["title"]).strip()
    desc = re.sub(r"^(?:Introduce|Adopt)\s+", "", desc)
    if len(desc) > 400:
        desc = desc[:397] + "..."

    ref = next((m for m, x in sorted(zip(matters, infos), key=lambda p: p[0].get("MatterIntroDate") or "", reverse=True)
                if m.get("MatterBodyId") == 138 and x["is_ord"]), matters[-1])
    url = LL.site_link(LEG, [ref] + [m for m in matters if m is not ref]) or f"{SITE}/Calendar.aspx"
    docs = None
    try:
        pzm = next((m for m in reversed(matters) if m.get("MatterBodyId") == 186), matters[0])
        atts = get_json(f"{LEG}/matters/{pzm['MatterId']}/attachments")
        rpt = [a for a in atts if re.search(r"staff report|report|memo", a.get("MatterAttachmentName") or "", re.I)]
        pick = rpt or atts
        if pick:
            docs = pick[0].get("MatterAttachmentHyperlink")
    except Exception:  # noqa: BLE001
        pass
    filed = min((m.get("MatterIntroDate") or "9999")[:10] for m in matters)
    return {
        "jurisdiction": JUR, "county": "Marion", "app_num": case, "category": cat, "app_type": app_type,
        "project_name": pd_name, "description": desc or None, "applicant": None,
        "parcel_ids": parcels, "address": main.get("address"), "acres": acres,
        "from_zoning": None if cat == "Comp Plan Amendment" else frm,
        "to_zoning": None if cat == "Comp Plan Amendment" else to,
        "from_flu": frm if cat == "Comp Plan Amendment" else None,
        "to_flu": to if cat == "Comp Plan Amendment" else None,
        "filed_date": None if filed == "9999" else filed, "status": status, "status_raw": raw,
        "hearings": [{"body": h["body"], "date": h["date"], "result": h["result"]} for h in hearings],
        "decision_date": decision, "url": url, "docs_url": docs,
        "lat": None, "lon": None, "source": SOURCE,
    }


if __name__ == "__main__":
    since = sys.argv[1] if len(sys.argv) > 1 else (dt.date.today() - dt.timedelta(days=14)).isoformat()
    refresh = sys.argv[2].split(",") if len(sys.argv) > 2 and sys.argv[2] else []
    t0 = time.time()
    recs = fetch(since, refresh)
    print(json.dumps(recs, indent=2, ensure_ascii=False))
    from collections import Counter
    log(f"{len(recs)} records in {time.time() - t0:.0f}s; status={dict(Counter(r['status'] for r in recs))}; "
        f"category={dict(Counter(r['category'] for r in recs))}")
