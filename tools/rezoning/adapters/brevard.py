#!/usr/bin/env python3
"""Brevard County (unincorporated) rezoning adapter.

www.brevardfl.gov answers HTTP 403 from the pipeline network, so the county site is not used.
Sources:
 1. Accela Citizen Access, agency BREVARD, module Development (aca-prod.accela.com/BREVARD), via
    accela_core.ACA (read-only import). Record types searched by opened date:
        Development/Zoning/Rezoning/NA                 "Rezoning" (yyZnnnnn)         -> Rezoning / PD
        Development/Zoning/Combined Rezoning-CUP/NA    "Combined Rezoning-CUP"       -> Rezoning / PD
        Development/Zoning/PUD Major/NA                "Amendment - PUD Major"       -> PD
        Development/Zoning/SSCPA/NA                    "Comp Plan - Small Scale"     -> Comp Plan Amendment
        Development/Zoning/LSCPA/NA                    "Comp Plan - Large Scale"     -> Comp Plan Amendment
                                                       (text-only / EAR amendments without parcels skipped)
    Not searched: CUP, Variance, PUD Minor, Binding Development Plan (BDP) amendments, waivers,
    site plans, plats. Rezoning-type records whose request is an appeal or CUP-only are skipped.
    Accela gives: opened date, status, ASI Request / Acreage / Commission District, the parcel
    list (Brevard 7-digit tax account numbers, "Account Number"), and the Processing Status
    workflow (P&Z / special district board recommendations are typed in comments there; the
    final BOCC outcome is the "Final Actions" task).
 2. Legistar web API (webapi.legistar.com/v1/brevardfl): agenda matters of the Planning and Zoning
    Board / Local Planning Agency (body 203), the North Merritt Island (206) and Port St. John
    (208) dependent special district boards, and BOCC zoning items (matter body 158 "Planning and
    Development", heard at BOCC events, body 138). Titles carry the case number "(26Z00032)",
    "Tax Account" numbers, from/to zoning or FLU and the district. Event items give hearing
    dates; Legistar has no recorded votes for these items.
Cases appear at filing (Accela), well before any agenda.
"""
import datetime as dt
import json
import os
import re
import sys
import time

import requests
import os as _os_ll
sys.path.insert(0, _os_ll.path.dirname(_os_ll.path.abspath(__file__)))
import legistar_links as LL  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import accela_core as C  # noqa: E402

JURISDICTIONS = ["Brevard County"]
SOURCE = "Brevard Accela (Development) + Legistar (P&Z/LPA, special district boards, BOCC)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
LEG = "https://webapi.legistar.com/v1/brevardfl"
LEG_SITE = "https://brevardfl.legistar.com"

TYPES = {
    "Development/Zoning/Rezoning/NA": "Rezoning",
    "Development/Zoning/Combined Rezoning-CUP/NA": "Rezoning",
    "Development/Zoning/PUD Major/NA": "PD",
    "Development/Zoning/SSCPA/NA": "Comp Plan Amendment",
    "Development/Zoning/LSCPA/NA": "Comp Plan Amendment",
}
MATTER_BODIES = [203, 158, 206, 208, 138]
EVENT_BODIES = {203: "Planning and Zoning Board", 138: "BOCC",
                206: "North Merritt Island Special District Board",
                208: "Port St. John Special District Board"}
CASE_RX = re.compile(r"\b(\d{2}(?:Z|SS|LS|PUD)\d{5})\b")

_leg = requests.Session()
_leg.headers.update({"User-Agent": UA, "Accept": "application/json"})


def log(*a):
    print("[brevard]", *a, file=sys.stderr, flush=True)


def leg_get(path, params=None, tries=3, sleep=0.3):
    last = None
    for i in range(tries):
        try:
            r = _leg.get(f"{LEG}/{path}", params=params, timeout=60)
            r.raise_for_status()
            time.sleep(sleep)
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"Legistar GET failed {path}: {last}")


def leg_all(path, flt, select=None):
    out, skip = [], 0
    while True:
        p = {"$filter": flt, "$top": 1000, "$skip": skip}
        if select:
            p["$select"] = select
        d = leg_get(path, p)
        out.extend(d)
        if len(d) < 1000:
            return out
        skip += 1000


class BrevardACA(C.ACA):
    """Adds the 'Account Number' parcel list (Brevard tax accounts), which the generic parser
    (looking for 'Parcel Number') does not pick up."""

    def detail(self, url, workflow=True):
        r = self.get(url)
        d = C.parse_detail(r.text)
        d["url"] = r.url if "CapDetail" in r.url else url
        d["accounts"] = list(dict.fromkeys(re.findall(r"Account Number\s*(?:</[^>]+>\s*)*(?:<[^>]+>\s*)*(\d{5,8})", r.text)))
        if not d["accounts"]:
            from bs4 import BeautifulSoup
            t = BeautifulSoup(r.text, "lxml").get_text("\n")
            d["accounts"] = list(dict.fromkeys(re.findall(r"Account Number\s*:?\s*(\d{5,8})", t)))
        if workflow:
            try:
                d["workflow"] = self.processing(d["url"])
            except Exception as e:  # noqa: BLE001
                log(f"workflow fetch failed for {d.get('app_num')}: {e}")
                d["workflow"] = []
        return d


# ------------------------------------------------------------------ Legistar
def parse_title(t):
    t = re.sub(r"\s+", " ", t or "").strip()
    out = {"title": t}
    m = re.match(r"^(.+?)\s+(?:is |are )?(?:request(?:s|ing)?|are requesting|is requesting)\b", t, re.I)
    out["applicant"] = m.group(1).strip(" ,") if m else None
    accts = []
    for m in re.finditer(r"Tax Accounts?\s*#?\s*([\d][\d,\s&and\[\]{}()A-Za-z\.]*?)(?:\)|\]|$)", t):
        accts += re.findall(r"\b(\d{6,8})\b", m.group(1))
    out["accounts"] = list(dict.fromkeys(accts))
    m = re.search(r"(?:zoning classification change|change (?:of|in) zoning classification|requests? a change|"
                  r"zoning change)\s+from\s+(.+?)\s+to\s+(?:all\s+)?(.+?)(?:\.\s*\(|\s*\(\d{2}Z|\.$|\s+for\s|$)", t, re.I)
    if m:
        out["from_zoning"], out["to_zoning"] = m.group(1).strip(" ,."), m.group(2).strip(" ,.")
    m = re.search(r"Future Land Use designation\s+from\s+(.+?)\s+to\s+(.+?)(?:\.\s*\(|\s*\(\d{2}|\.$|$)", t, re.I)
    if m:
        out["from_flu"], out["to_flu"] = m.group(1).strip(" ,."), m.group(2).strip(" ,.")
    m = re.search(r"\(District\s*(\d)\)", t)
    out["district"] = m.group(1) if m else None
    out["is_rezoning"] = bool(re.search(r"zoning classification|change (?:of|in) zoning|requests? a change from|"
                                        r"rezon", t, re.I))
    out["is_cpa"] = bool(re.search(r"Comprehensive Plan Amendment|Future Land Use designation", t, re.I))
    out["is_cup_only"] = bool(re.search(r"\bCUP\b|Conditional Use", t)) and not out["is_rezoning"]
    out["is_appeal"] = bool(re.search(r"^Appeal\b|\bappeal of\b", t, re.I))
    out["is_acceptance"] = bool(re.match(r"^Acceptance\b", t, re.I))
    return out


def legistar_matters(cases, start, refresh_cases):
    """{case: [matter,...]} for the given case numbers."""
    flt = f"MatterIntroDate ge datetime'{start}' and (" + " or ".join(f"MatterBodyId eq {b}" for b in MATTER_BODIES) + ")"
    ms = leg_all("matters", flt, "MatterId,MatterGuid,MatterTitle,MatterBodyId,MatterBodyName,MatterIntroDate,MatterAgendaDate,MatterFile")
    out = {}
    for m in ms:
        for c in set(CASE_RX.findall(m.get("MatterTitle") or "")):
            if c in cases:
                out.setdefault(c, []).append(m)
    for c in refresh_cases:
        if c in out:
            continue
        try:
            got = leg_get("matters", {"$filter": f"substringof('{c}',MatterTitle)"})
        except Exception as e:  # noqa: BLE001
            log(f"Legistar lookup {c} failed: {e}")
            continue
        got = [m for m in got if c in CASE_RX.findall(m.get("MatterTitle") or "")]
        if got:
            out[c] = got
    log(f"Legistar: {len(ms)} matters since {start}; {len(out)} cases matched")
    return out


def legistar_hearings(matter_ids, start):
    """{matter_id: [{body, date, event_url, agenda_number}]} from event items."""
    flt = f"EventDate ge datetime'{start}' and (" + " or ".join(f"EventBodyId eq {b}" for b in EVENT_BODIES) + ")"
    evs = leg_all("events", flt)
    out = {}
    n = 0
    for ev in sorted(evs, key=lambda e: e["EventDate"]):
        try:
            items = leg_get(f"events/{ev['EventId']}/eventitems")
        except Exception as e:  # noqa: BLE001
            log(f"event {ev['EventId']}: {e}")
            continue
        n += 1
        for it in items:
            mid = it.get("EventItemMatterId")
            if mid in matter_ids:
                out.setdefault(mid, []).append({
                    "body": EVENT_BODIES.get(ev["EventBodyId"], ev.get("EventBodyName")),
                    "date": ev["EventDate"][:10], "event_url": ev.get("EventInSiteURL"),
                    "action": it.get("EventItemActionName"), "passed": it.get("EventItemPassedFlagName"),
                })
    log(f"Legistar: scanned {n} events since {start}")
    return out


# ------------------------------------------------------------------ workflow helpers
_BODY_IN_COMMENT = [
    (re.compile(r"\bP\s*&\s*Z\b|\bP\s*and\s*Z\b|Planning (?:and|&) Zoning|\bLPA\b", re.I), "Planning and Zoning Board"),
    (re.compile(r"\bNMI\b|North Merritt", re.I), "North Merritt Island Special District Board"),
    (re.compile(r"\bPSJ\b|Port St\.? John", re.I), "Port St. John Special District Board"),
]


def wf_recommendations(d):
    """[(body, mark_date, result)] from 'Recommendation' entries / comments."""
    out = []
    for t in d.get("workflow", []):
        for e in t.get("entries", []):
            c = e.get("comment") or ""
            m = re.search(r"recommend(?:s|ed)?\s+(approval|denial|approve|deny)", c, re.I)
            if not m:
                continue
            res = "Recommended Denial" if m.group(1).lower().startswith("den") else "Recommended Approval"
            for rx, body in _BODY_IN_COMMENT:
                if rx.search(c):
                    out.append((body, e.get("date"), res, c))
                    break
    return out


def wf_final(d):
    """(status, date) of the final BOCC outcome recorded in the workflow, if any."""
    best = None
    for t in d.get("workflow", []):
        name = t.get("task") or ""
        for e in t.get("entries", []):
            st = (e.get("status") or "").strip()
            c = (e.get("comment") or "").strip()
            if re.search(r"final action", name, re.I) and re.search(r"approv|den|withdr", st, re.I):
                best = (st, e.get("date"))
            elif re.search(r"^BOCC", name, re.I):
                if re.search(r"^(approved|denied|withdrawn)", st, re.I):
                    best = best or (st, e.get("date"))
                elif re.search(r"^(approved|denied)\b", c, re.I):
                    best = best or (c.split()[0], e.get("date"))
    return best


def junk_addr(a):
    a = (a or "").strip()
    return not a or a in ("FL", "United States of America") or re.fullmatch(r"[A-Z]{2}", a) is not None \
        or re.search(r"\bsee\b.*\b(attached|survey)\b|^see ", a, re.I) is not None


def contact_name(block):
    """ACA contact block: line 1 first (+middle) name, line 2 last name, optional line 3 business
    name, then the address. 'Kimberly\nBonder\nRezanka\nLacey Lyons Rezanka\n1290 ...' style blocks
    (first/middle/last on separate lines) are handled too."""
    lines = []
    for x in (block or "").split("\n"):
        x = x.strip(" ,")
        if not x:
            continue
        if re.match(r"^\d|^P\.?\s*O\.?\s*BOX|^(FL|Florida|[A-Z]{2})$", x, re.I) or "@" in x or \
                re.search(r"Contact Number", x, re.I):
            break
        lines.append(x)
    if not lines:
        return None
    if len(lines) >= 4 and all(" " not in x for x in lines[:3]):
        name, rest = lines[:3], lines[3:]
    else:
        name, rest = lines[:2], lines[2:]
    nm = " ".join(name)
    org = rest[0] if rest else None
    if org and all(t.lower() in org.lower() for t in " ".join(name).split()):
        org = None
    return ", ".join(x for x in [nm, org] if x) or None


# ------------------------------------------------------------------ build
def build(row, d, matters, by_matter, today):
    num = d.get("app_num") or row["app_num"]
    tval = row.get("_tval")
    typ = d.get("type") or row.get("type")
    cat = TYPES.get(tval)
    if cat is None:
        t = (typ or "").lower()
        cat = "Comp Plan Amendment" if "comp plan" in t else ("PD" if "pud major" in t else
                                                               ("Rezoning" if "rezon" in t else None))
    if cat is None:
        return None
    request = C.asi_get(d, r"^Request$")
    rd = d.get("record_details", {})
    desc = C.one_line(rd.get("Description"))
    titles = [parse_title(m.get("MatterTitle")) for m in matters if not parse_title(m.get("MatterTitle"))["is_acceptance"]]
    main = max(titles, key=lambda x: len(x["title"])) if titles else {}
    req_txt = " ".join(x for x in [request, main.get("title")] if x)
    if main.get("is_appeal") or re.search(r"\bappeal\b", request or "", re.I):
        return None
    if cat == "Rezoning" and titles and all(x["is_cup_only"] for x in titles):
        return None
    if cat == "Rezoning" and not titles and request and re.search(r"\bCUP\b|conditional use", request, re.I) \
            and not re.search(r"rezon|zoning", request, re.I):
        return None
    accts = list(d.get("accounts") or [])
    for x in titles:
        for a in x.get("accounts", []):
            if a not in accts:
                accts.append(a)
    if "LSCPA" in (tval or "") and not accts and not re.search(r"future land use designation|map", req_txt, re.I):
        return None   # text-only large scale amendment
    fz, tz = main.get("from_zoning"), main.get("to_zoning")
    if not (fz and tz):
        a, b = C.zoning_from_text(request)
        fz, tz = fz or a, tz or b
    if cat == "Rezoning" and tz and re.search(r"\bPUD\b|planned unit", tz, re.I):
        cat = "PD"
    ff, tf = main.get("from_flu"), main.get("to_flu")
    if cat == "Comp Plan Amendment":
        fz = tz = None
    else:
        ff = tf = None

    # hearings from Legistar event items (fallback: matter agenda dates)
    hmap = {}
    for m in matters:
        if parse_title(m.get("MatterTitle"))["is_acceptance"]:
            continue
        evs = by_matter.get(m["MatterId"], [])
        for e in evs:
            hmap.setdefault((e["body"], e["date"]), {"body": e["body"], "date": e["date"], "result": None})
        if not evs and m.get("MatterAgendaDate"):
            b = {203: "Planning and Zoning Board", 206: EVENT_BODIES[206], 208: EVENT_BODIES[208]}.get(
                m.get("MatterBodyId"), "BOCC")
            dd = m["MatterAgendaDate"][:10]
            hmap.setdefault((b, dd), {"body": b, "date": dd, "result": None})
    hs = sorted(hmap.values(), key=lambda h: h["date"])
    # same matter on two consecutive meetings of a body -> earlier one was continued
    for i, h in enumerate(hs):
        later = [x for x in hs[i + 1:] if x["body"] == h["body"]]
        if later and h["date"] < today:
            h["result"] = "Continued"
    # advisory recommendations typed in workflow comments
    for body, mark, res, c in wf_recommendations(d):
        cands = [h for h in hs if h["body"] == body and h["date"] <= (mark or today) and h["result"] != "Continued"]
        mm = re.search(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", c)
        if mm and mark:
            yr = int(mm.group(3)) if mm.group(3) else int(mark[:4])
            yr = yr + 2000 if yr < 100 else yr
            try:
                cd = dt.date(yr, int(mm.group(1)), int(mm.group(2))).isoformat()
                exact = [h for h in hs if h["body"] == body and abs((dt.date.fromisoformat(h["date"]) - dt.date.fromisoformat(cd)).days) <= 1]
                if exact:
                    cands = exact
            except ValueError:
                pass
        if cands:
            cands[-1]["result"] = res
    status_raw = d.get("status_raw") or row.get("status")
    fin = wf_final(d)
    raw = status_raw or ""
    if re.search(r"withdr|void|cancel|expired|closed|inactive", raw, re.I):
        status = "Withdrawn"
    elif re.search(r"\bdenied\b", raw, re.I):
        status = "Denied"
    elif re.search(r"approved|adopted", raw, re.I):
        status = "Approved"
    elif fin and re.search(r"approv", fin[0], re.I):
        status = "Approved"
    elif fin and re.search(r"den", fin[0], re.I):
        status = "Denied"
    else:
        status = None
    decision = None
    if status in ("Approved", "Denied"):
        bocc = [h for h in hs if h["body"] == "BOCC" and h["date"] <= today and h["result"] != "Continued"]
        if bocc:
            bocc[-1]["result"] = status
            decision = bocc[-1]["date"]
        else:
            decision = fin[1] if fin else None
    if status is None:
        future = [h for h in hs if h["date"] > today]
        past = [h for h in hs if h["date"] <= today]
        if future:
            status = "Hearing Scheduled"
        elif past and past[-1]["result"] == "Continued":
            status = "Continued"
        elif past and (past[-1]["body"] == "BOCC" or re.search(r"pending final", raw, re.I)):
            status = "Hearing Scheduled"   # heard; outcome not yet posted
        else:
            status = "Filed"

    applicant = main.get("applicant") or rd.get("Applicant") or contact_name(rd.get("Contractor")) \
        or C.first_contact(d, r"applicant", r"contact", r"agent")
    if not applicant:
        own = C.first_contact(d, r"owner")
        applicant = own if own and not junk_addr(own) else None
    address = None if junk_addr(row.get("address")) else C.one_line(row.get("address"))
    if not address:
        loc = C.asi_get(d, r"^Location$")
        address = C.one_line(loc) if loc and not junk_addr(loc) else None
    acres = C.to_float(C.asi_get(d, r"Acreage|acres"))
    description = request or desc
    if main.get("title"):
        description = main["title"]
    if description and len(description) > 500:
        description = description[:497] + "..."
    docs = None
    url_leg = None
    if matters:
        lm = sorted(matters, key=lambda m: (m.get("MatterAgendaDate") or "", m["MatterId"]))[-1]
        url_leg = LL.site_link(LEG, [lm] + [m for m in matters if m is not lm])
        try:
            atts = leg_get(f"matters/{lm['MatterId']}/attachments")
            rpt = [a for a in atts if re.search(r"staff comment|staff report", a.get("MatterAttachmentName") or "", re.I)]
            if rpt:
                docs = rpt[0].get("MatterAttachmentHyperlink")
        except Exception:  # noqa: BLE001
            pass
    proj = row.get("project_name")
    if proj and re.match(r"^(BASS/)?(Rezoning|SSCPA|LSCPA|PUD Major)(/null)?$|/null$|^[A-Za-z ]+/[A-Z]+_\d+", proj):
        proj = None
    return {
        "jurisdiction": "Brevard County", "county": "Brevard", "app_num": num, "category": cat,
        "app_type": typ, "project_name": proj, "description": C.one_line(description),
        "applicant": C.one_line(applicant), "parcel_ids": accts, "address": address, "acres": acres,
        "from_zoning": fz, "to_zoning": tz, "from_flu": ff, "to_flu": tf,
        "filed_date": row.get("date") or C.earliest_workflow_date(d), "status": status,
        "status_raw": status_raw, "hearings": hs, "decision_date": decision,
        "url": d.get("url") or row.get("url") or url_leg, "docs_url": docs,
        "lat": None, "lon": None, "source": SOURCE,
        "commission_district": C.asi_get(d, r"Commission District") or main.get("district"),
        "legistar_url": url_leg,
    }


# ------------------------------------------------------------------ fetch
def fetch(since, refresh):
    t0 = time.time()
    today = dt.date.today().isoformat()
    refresh = [r.strip().upper() for r in (refresh or []) if r and r.strip()]
    aca = BrevardACA("BREVARD", "Development", "Development")
    try:
        avail = dict(aca.record_types())
    except C.Blocked:
        raise
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Brevard Accela unreachable: {e}") from e
    start, end = dt.date.fromisoformat(since), dt.date.today()
    seen = {}
    for tval in TYPES:
        if tval not in avail:
            log(f"record type not offered any more: {tval}")
            continue
        for a, b in C.split_range(start, end, 400):
            try:
                rs, single = aca.search(rtype=tval, start=a, end=b)
            except C.Blocked:
                raise
            except Exception as e:  # noqa: BLE001
                log(f"search failed {tval} {a}..{b}: {e}")
                continue
            if single is not None:
                # exactly one hit redirects to the record page, which has no opened date; widen the
                # window so the result grid (with dates) comes back instead
                try:
                    rs2, single2 = aca.search(rtype=tval, start=a - dt.timedelta(days=365), end=b)
                except Exception:  # noqa: BLE001
                    rs2, single2 = [], None
                if single2 is None and rs2:
                    rs = rs2
                else:
                    d0 = C.parse_detail(single)
                    rs = [{"app_num": d0.get("app_num"), "type": d0.get("type"), "date": None,
                           "status": d0.get("status_raw"), "url": aca.last_url}]
            log(f"{avail[tval]}: {len(rs)} rows {a}..{b}")
            for r in rs:
                if not r.get("app_num") or re.match(r"^\d{2}TMP-", r["app_num"]):
                    continue
                if r.get("date") and r["date"] < since:
                    continue
                r["_tval"] = tval
                seen.setdefault(r["app_num"], r)
    for num in refresh:
        if num in seen:
            seen[num]["_refresh"] = True
            continue
        try:
            rs, single = aca.search(permit_no=num)
        except C.Blocked:
            raise
        except Exception as e:  # noqa: BLE001
            log(f"refresh search failed {num}: {e}")
            continue
        if single is not None:
            seen[num] = {"app_num": num, "url": aca.last_url, "date": None, "_refresh": True}
        else:
            hit = next((r for r in rs if r.get("app_num") == num), None)
            if hit:
                hit["_refresh"] = True
                seen[num] = hit
            else:
                log(f"refresh id not found: {num}")
    log(f"{len(seen)} Accela candidates")

    details = {}
    for num, r in seen.items():
        if not r.get("url"):
            continue
        try:
            details[num] = aca.detail(r["url"], workflow=True)
        except C.Blocked:
            raise
        except Exception as e:  # noqa: BLE001
            log(f"{num}: detail failed ({type(e).__name__}: {e})")
    if not details:
        return []

    dates = [r.get("date") for r in seen.values() if r.get("date")] + \
        [C.earliest_workflow_date(d) for d in details.values() if C.earliest_workflow_date(d)]
    leg_start = (dt.date.fromisoformat(min(dates + [since])) - dt.timedelta(days=7)).isoformat()
    leg_start = max(leg_start, (dt.date.today() - dt.timedelta(days=760)).isoformat())
    try:
        mats = legistar_matters(set(details), leg_start,
                                [n for n, r in seen.items() if r.get("_refresh") and (r.get("date") or "9999") < leg_start])
        mids = {m["MatterId"] for ms in mats.values() for m in ms}
        hstart = min([(m.get("MatterAgendaDate") or m.get("MatterIntroDate") or "9999")[:10]
                      for ms in mats.values() for m in ms] + [today])
        by_matter = legistar_hearings(mids, hstart) if mids else {}
    except Exception as e:  # noqa: BLE001
        log(f"Legistar unavailable, hearings from Accela only: {e}")
        mats, by_matter = {}, {}

    out = []
    for num, r in seen.items():
        d = details.get(num)
        if d is None:
            continue
        if not r.get("_tval"):
            t = (d.get("type") or "").lower()
            r["_tval"] = next((k for k, v in TYPES.items() if k.split("/")[2].lower() in t), None)
        try:
            rec = build(r, d, mats.get(num, []), by_matter, today)
        except Exception as e:  # noqa: BLE001
            log(f"{num}: skipped ({type(e).__name__}: {e})")
            continue
        if rec is None:
            log(f"{num}: out of scope ({d.get('type')}; {C.asi_get(d, r'^Request$')})")
            continue
        if not r.get("_refresh") and rec["filed_date"] and rec["filed_date"] < since:
            continue
        for k, v in list(rec.items()):
            if isinstance(v, str) and not v.strip():
                rec[k] = None
        out.append(rec)
    log(f"{len(out)} records in {time.time() - t0:.0f}s")
    return out


if __name__ == "__main__":
    since = sys.argv[1] if len(sys.argv) > 1 else (dt.date.today() - dt.timedelta(days=14)).isoformat()
    refresh = sys.argv[2].split(",") if len(sys.argv) > 2 and sys.argv[2] else []
    t0 = time.time()
    recs = fetch(since, refresh)
    print(json.dumps(recs, indent=2, ensure_ascii=False))
    from collections import Counter
    log(f"{len(recs)} records in {time.time() - t0:.0f}s; status={dict(Counter(r['status'] for r in recs))}; "
        f"category={dict(Counter(r['category'] for r in recs))}")
