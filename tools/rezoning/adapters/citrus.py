#!/usr/bin/env python3
"""Citrus County rezoning adapter.

Source: Citrus County Clerk's CivicClerk portal (citrusclerk.portal.civicclerk.com), public API
citrusclerk.api.civicclerk.com. Planning & Development Commission (PDC) and BCC agenda items are
named with the county case number ("PUD-2026-00004 Attorney ... for One Oak Homosassa, LLC",
"CPA/AA/PUD-2025-00010 Lecanto Sand LLC", "AA-2026-00001 ...") and carry the full request text
(acreage, Alternate Keys, address, from/to districts, prior PDC recommendation) in the item
description, so no PDF parsing is needed for filings. BCC outcomes ("RESULT: Adopted") are read
from the BCC minutes PDF that CivicClerk publishes with each meeting (needs pypdf; optional:
without it, outcomes are left unknown).

Case prefixes: AA = LDC Atlas (zoning map) amendment -> Rezoning; PUD -> PD;
CPA (+AA/PUD) with a Future Land Use Map change -> Comp Plan Amendment. CU, V, OA, PV, text-only
CPAs are out of scope.
"""
import datetime as dt
import html
import io
import json
import re
import sys
import time

import requests

try:
    import pypdf
except ImportError:  # pragma: no cover
    pypdf = None

JURISDICTIONS = ["Citrus County"]

API = "https://citrusclerk.api.civicclerk.com/v1"
PORTAL = "https://citrusclerk.portal.civicclerk.com"
SOURCE = "Citrus County CivicClerk (PDC + BCC agendas, BCC minutes)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
LOOKBACK_DAYS = 180
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"

CASE_RE = re.compile(r"\b((?:CPA|AA|PUD|CU|V|OA|PV|DRI|LSCPA|SSCPA)(?:[/-](?:CPA|AA|PUD))*-\d{4}-\d{3,5})\b")
MEETING_RE = re.compile(r"Planning and Development Commission|Board of County Commissioners (?:Regular|Special)|"
                        r"Special Master Hearing|Special Magistrate Hearing", re.I)

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


def log(*a):
    print("[citrus]", *a, file=sys.stderr)


def get(url, params=None, tries=3, sleep=0.3):
    last = None
    for i in range(tries):
        try:
            r = _s.get(url, params=params, timeout=90)
            r.raise_for_status()
            time.sleep(sleep)
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


def strip_html(s):
    s = re.sub(r"<br\s*/?>", "\n", s or "", flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s).replace("\xa0", " ").replace("​", "")
    return re.sub(r"[ \t]+", " ", s).strip()


def walk(items):
    for it in items or []:
        yield it
        yield from walk(it.get("childItems"))


def list_events(start, end):
    url = f"{API}/Events"
    params = {"$filter": f"startDateTime ge {start}T00:00:00Z and startDateTime lt {end}T00:00:00Z",
              "$orderby": "startDateTime"}
    out = []
    while url:
        d = get(url, params).json()
        out.extend(d.get("value", []))
        url, params = d.get("@odata.nextLink"), None
    return out


def body_of(ev):
    n = ev.get("eventName") or ""
    if re.search(r"Planning and Development", n, re.I):
        return "Planning and Development Commission"
    if re.search(r"Special Ma(?:ster|gistrate)", n, re.I):
        return "Special Magistrate"
    return "BOCC"


def iso(m_, d_, y_):
    return dt.datetime.strptime(f"{m_} {d_} {y_}", "%B %d %Y").date().isoformat()


def category_of(case, desc):
    parts = re.split(r"[/-]", case.split("-20")[0])
    if "CPA" in parts or "LSCPA" in parts or "SSCPA" in parts:
        if re.search(r"Future Land Use Map|\bFLUM\b", desc or "", re.I):
            return "Comp Plan Amendment"
        if "AA" in parts or "PUD" in parts:
            return "PD" if "PUD" in parts else "Rezoning"
        return None  # text-only comp plan amendment
    if "PUD" in parts:
        return "PD"
    if "AA" in parts:
        return "Rezoning"
    return None


def parse_desc(desc):
    d = re.sub(r"\s+", " ", desc or "")
    out = {}
    m = re.search(r"(?:approximately|approx\.|totaling|consisting of)\s*([\d,]*\.?\d+)\s*(?:\+/-\s*)?acres", d, re.I) \
        or re.search(r"([\d,]*\.?\d+)\s*(?:\+/-\s*|±\s*)?acres", d, re.I)
    if m:
        try:
            out["acres"] = float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    keys = []
    for km in re.finditer(r"(?:Alternate Keys?(?: Numbers?| Nos?\.?)?|\bAK)\s*#?\s*((?:\d{6,8}(?:\s*(?:,|and|&)\s*)?)+)", d):
        keys += re.findall(r"\d{6,8}", km.group(1))
    out["parcels"] = list(dict.fromkeys(keys))
    m = re.search(r"address(?:es)? (?:is|are)(?: known as)? (.+?,\s*F(?:L|lorida))\b", d, re.I) or \
        re.search(r"address(?:es)? (?:is|are)(?: known as)? (.+?)(?:;|\.\s+(?:The|This|It|Planning)\b|$)", d, re.I)
    if m and len(m.group(1)) < 250:
        out["address"] = m.group(1).strip(" ,.")
    m = re.search(r"Future Land Use Map.*?\bfrom\s+(.+?),?\s+to\s+(.+?)(?:;|\. |, and | and to | and the |$)", d, re.I)
    if m:
        out["from_flu"], out["to_flu"] = m.group(1).strip(" ,"), m.group(2).strip(" ,")
    m = re.search(r"Atlas Map.*?\bfrom\s+(.+?),?\s+to\s+(.+?)(?:;|\. | for | pursuant|, and |$)", d, re.I)
    if m:
        out["from_zoning"], out["to_zoning"] = m.group(1).strip(" ,"), \
            re.split(r"\s+on approximately|\s+for a\b|\s+to allow", m.group(2))[0].strip(" ,")
    # prior PDC actions described in the text ("... heard by the PDC on July 2, 2026, and recommended
    # for denial", "recommended denial by the PDC on June 4, 2026", "continued by the PDC on ...")
    pdc = []
    for sent in re.split(r"(?<=[.;])\s+(?=[A-Z])", desc or ""):
        if not re.search(r"Planning and Development Commission|\bPDC\b", sent):
            continue
        dates = [iso(a, b, c) for a, b, c in re.findall(rf"\bon\s+({MONTHS})\s+(\d{{1,2}}),\s*(\d{{4}})", sent)]
        acts = re.findall(r"recommend(?:ed|s)?\s+(?:for\s+)?(approval|denial)|\b(continued|tabled)\b", sent, re.I)
        acts = [a or b for a, b in acts]
        for k, d_ in enumerate(dates):
            if acts:
                pdc.append((d_, acts[min(k, len(acts) - 1)]))
    out["pdc"] = pdc
    return out


def pdc_result(txt):
    t = txt.lower()
    if "denial" in t:
        return "Recommended Denial"
    if "approval" in t:
        return "Recommended Approval"
    return "Continued"


# ---------------------------------------------------------------- minutes
RESULT_MAP = {"adopted": "Approved", "approved": "Approved", "approved as amended": "Approved",
              "passed": "Approved", "denied": "Denied", "failed": "Denied", "continued": "Continued",
              "tabled": "Continued", "postponed": "Continued", "withdrawn": "Withdrawn",
              "transmitted": "Transmitted"}


def minutes_results(pdf_bytes, cases):
    rdr = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    t = "\n".join(p.extract_text() or "" for p in rdr.pages)
    t1 = re.sub(r"\s+", " ", t)
    res = {}
    heads = [m.start() for m in re.finditer(
        r"\s[A-Z]{1,2}\.\d{1,2}\.\s|\s[A-Z]\.\s+(?!SECONDER|MOVER|AYES|NAY)[A-Z][A-Z ,&]{4,}\b|ADJOURN", t1)]
    for c in cases:
        pat = re.escape(c).replace(r"\-", r"\s*-\s*")
        for m in re.finditer(pat, t1):
            nxt_head = next((h for h in heads if h > m.end() + 5), len(t1))
            seg = t1[m.end(): nxt_head]
            decision = None
            prev = 0
            for rm in re.finditer(r"RESULT:\s*([A-Za-z ]+?)(?=\s+(?:\[|MOVER|SECONDER|AYES|NAY|Next:)|$)", seg):
                motion = seg[prev: rm.start()]
                prev = rm.end()
                mm = list(re.finditer(r"\bMotion to (\w+)", motion))  # sentence-initial motion, not "added to the motion to"
                verb = mm[-1].group(1).lower() if mm else ""
                mtext = motion[mm[-1].start():] if mm else motion
                if re.search(r"party status|backup materials|as evidence|intervenor|recess|extend", mtext, re.I):
                    continue
                r = rm.group(1).strip().lower()
                if r in ("failed", "denied") and verb.startswith("approv"):
                    continue  # failed approval motion is not itself a decision
                if r in ("adopted", "approved", "passed", "approved as amended"):
                    if verb.startswith("den"):
                        decision = "Denied"
                    elif verb.startswith(("contin", "table", "postpon", "defer")):
                        decision = "Continued"
                    elif verb.startswith("transmit") or re.search(r"\btransmi", mtext, re.I):
                        decision = "Transmitted"
                    elif verb.startswith("withdr") or re.search(r"withdraw", mtext, re.I):
                        decision = "Withdrawn"
                    else:
                        decision = "Approved"
                else:
                    decision = RESULT_MAP.get(r, r.title())
            if decision:
                res[c] = decision
            break
    return res


# ---------------------------------------------------------------- fetch
def fetch(since, refresh):
    today = dt.date.today()
    start = (dt.date.fromisoformat(since) - dt.timedelta(days=LOOKBACK_DAYS)).isoformat()
    end = (today + dt.timedelta(days=90)).isoformat()
    events = [e for e in list_events(start, end) if MEETING_RE.search(e.get("eventName") or "")]
    refresh = {r.strip() for r in (refresh or []) if r and r.strip()}
    log(f"{len(events)} PDC/BCC events {start}..{end}")

    cases = {}
    bcc_minutes = {}   # event date -> minutes fileId
    for ev in events:
        if not ev.get("agendaId"):
            continue
        mdate = ev["startDateTime"][:10]
        body = body_of(ev)
        cancelled = bool(re.match(r"\s*CANCEL", ev.get("eventName") or "", re.I))
        files = ev.get("publishedFiles") or []
        for f in files:
            if f.get("type") == "Minutes" and body != "Planning and Development Commission":
                bcc_minutes[mdate] = f.get("fileId")
        pub = [f.get("publishOn")[:10] for f in files if f.get("type") == "Agenda" and f.get("publishOn")]
        seen = min(pub + [mdate])
        packet = [f.get("fileId") for f in files if f.get("type") == "Agenda Packet"]
        try:
            mt = get(f"{API}/Meetings/{ev['agendaId']}").json()
        except Exception as e:  # noqa: BLE001
            log(f"meeting {ev['agendaId']}: {e}")
            continue
        for it in walk(mt.get("items")):
            name = strip_html(it.get("agendaObjectItemName"))
            cm = CASE_RE.search(name)
            if not cm:
                continue
            case = cm.group(1)
            desc = strip_html(it.get("agendaObjectItemDescription"))
            cat = category_of(case, desc)
            if cat is None:
                continue
            rest = name[cm.end():].strip(" -–—:")
            rest = re.sub(r"^\d{1,2}:\d{2}\s*[ap]\.?m\.?\s*", "", rest, flags=re.I)
            c = cases.setdefault(case, {"apps": []})
            c["apps"].append({
                "date": mdate, "seen": seen, "body": body, "cancelled": cancelled, "title": rest,
                "desc": desc, "category": cat, "event_id": ev["id"], "item_id": it["id"],
                "packet": (f"{API}/Meetings/GetMeetingFileStream(fileId={packet[0]},plainText=false)"
                           if packet else None),
            })
    log(f"{len(cases)} in-scope cases; {len(bcc_minutes)} BCC minutes available")

    # BCC outcomes from minutes
    outcomes = {}
    if pypdf is None:
        log("pypdf not installed: BCC outcomes skipped")
    else:
        need = {}
        for case, c in cases.items():
            for a in c["apps"]:
                if a["body"] != "Planning and Development Commission" and a["date"] in bcc_minutes:
                    need.setdefault(a["date"], set()).add(case)
        for d, cs in sorted(need.items()):
            try:
                pdf = get(f"{API}/Meetings/GetMeetingFileStream(fileId={bcc_minutes[d]},plainText=false)").content
                for case, r in minutes_results(pdf, cs).items():
                    outcomes[(case, d)] = r
            except Exception as e:  # noqa: BLE001
                log(f"minutes {d}: {e}")

    out = []
    for case, c in sorted(cases.items()):
        first = min(a["seen"] for a in c["apps"])
        if first < since and case not in refresh:
            continue
        try:
            out.append(build(case, c, outcomes, bcc_minutes, today.isoformat()))
        except Exception as e:  # noqa: BLE001
            log(f"{case}: skipped ({e})")
    for r in refresh - set(cases):
        log(f"refresh id {r} not found on PDC/BCC agendas since {start}")
    return out


def build(case, c, outcomes, bcc_minutes, today):
    apps = sorted(c["apps"], key=lambda a: a["date"])
    latest = apps[-1]
    desc = max((a["desc"] for a in apps), key=len)
    info = parse_desc(latest["desc"] or desc)
    full = parse_desc(" ".join(a["desc"] for a in apps))
    title = latest["title"]
    applicant = title or None

    hmap = {}
    for a in apps:
        k = (a["body"], a["date"])
        res = None
        if a["cancelled"]:
            res = "Continued (meeting cancelled)"
        elif a["body"] != "Planning and Development Commission":
            res = outcomes.get((case, a["date"]))
        hmap[k] = {"body": a["body"], "date": a["date"], "result": res}
    for d, txt in full["pdc"]:
        k = ("Planning and Development Commission", d)
        hmap.setdefault(k, {"body": k[0], "date": d, "result": None})
        hmap[k]["result"] = hmap[k]["result"] or pdc_result(txt)
    hearings = sorted(hmap.values(), key=lambda h: h["date"])

    final = [h for h in hearings if h["body"] == "BOCC" and h["result"] in ("Approved", "Denied")]
    past = [h for h in hearings if h["date"] <= today]
    future = [h for h in hearings if h["date"] > today]
    if any(h["result"] == "Withdrawn" for h in hearings) or re.search(r"\bwithdr", title or "", re.I):
        status, raw, decision = "Withdrawn", "Withdrawn", None
    elif final:
        status, decision = final[-1]["result"], final[-1]["date"]
        raw = f"BCC {decision}: {final[-1]['result']} (minutes)"
    elif past and past[-1]["result"] and past[-1]["result"].startswith("Continued"):
        status, decision = "Continued", None
        raw = f"{past[-1]['body']} {past[-1]['date']}: {past[-1]['result']}"
        if future:
            raw += f"; next {future[0]['body']} {future[0]['date']}"
    elif future:
        status, decision = "Hearing Scheduled", None
        raw = f"next {future[0]['body']} {future[0]['date']}"
    elif past and past[-1]["body"] != "Planning and Development Commission":
        status, decision = "Hearing Scheduled", None
        raw = (f"{past[-1]['body']} {past[-1]['date']} held; "
               + ("result not found in minutes" if past[-1]["date"] in bcc_minutes else "minutes not yet published"))
    elif past:
        status, decision = "Filed", None
        raw = f"PDC {past[-1]['date']}: {past[-1]['result'] or 'heard'}; BCC hearing not yet scheduled"
    else:
        status, decision, raw = "Filed", None, "On agenda"
    for h in hearings:
        if h["result"] == "Transmitted":
            raw += "; transmitted to state (adoption hearing pending)"
            break

    cat = latest["category"] if latest["category"] else c["apps"][0]["category"]
    short = re.sub(r"\s+", " ", latest["desc"] or desc)
    short = re.split(r"(?<=\.)\s+(?:The (?:property|site|subject)|This request is made|The Planning)", short)[0]
    if len(short) > 300:
        short = short[:297] + "..."
    return {
        "jurisdiction": "Citrus County", "county": "Citrus", "app_num": case, "category": cat,
        "app_type": case.split("-20")[0], "project_name": None, "description": short or None,
        "applicant": applicant, "parcel_ids": full["parcels"], "address": info.get("address") or full.get("address"),
        "acres": info.get("acres") or full.get("acres"),
        "from_zoning": info.get("from_zoning") or full.get("from_zoning"),
        "to_zoning": info.get("to_zoning") or full.get("to_zoning"),
        "from_flu": info.get("from_flu") or full.get("from_flu"), "to_flu": info.get("to_flu") or full.get("to_flu"),
        "filed_date": min(a["seen"] for a in apps), "status": status, "status_raw": raw,
        "hearings": hearings, "decision_date": decision,
        "url": f"{PORTAL}/event/{latest['event_id']}/files", "docs_url": latest["packet"],
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
