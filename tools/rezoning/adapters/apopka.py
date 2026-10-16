#!/usr/bin/env python3
"""City of Apopka rezoning adapter.

Source: CivicClerk public API (apopkafl.api.civicclerk.com).
 * Apopka numbers each land use action as its own ordinance and agendas it as its own item,
   e.g. Planning Commission "Quasi-Judicial - Ordinance Number 3133 - Change of Zoning - 606 S.
   Hawthorne Avenue" and City Commission "Ordinance No. 3133 - Second Reading - Change of
   Zoning ...". A small-scale FLU amendment and its companion rezoning are therefore two records
   (two ordinances, two staff reports, two votes); app_num is "Ordinance 3133".
 * Staff reports are not item attachments; they are generated into the agenda packet. The packet
   is read as plain text through the API and split at "PLANNING COMMISSION STAFF REPORT" /
   "CITY COMMISSION STAFF REPORT" headings: owner, applicant, parcel IDs, location, existing /
   proposed FLU and zoning, tract size.
 * Outcomes come from the minutes attached to a later agenda's "Approval of Minutes" item
   ("PC 06-09-26 Minutes", "09-02-26 - CC Minutes"): "Motion to approve Ordinance No. 3158 at
   first reading ... Motion carried unanimously".
Cases first appear when the Planning Commission agenda is posted (about 5 days before the
hearing). The city's OpenGov permitting portal (apopkafl.portal.opengov.com) has planning record
types (Rezoning, FLU Amendment, PD/Master Plan) but its record search requires a Cloudflare
Turnstile token, so it cannot be used without a browser.
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

JURISDICTIONS = ["City of Apopka"]
JUR, COUNTY = "City of Apopka", "Orange"

API = "https://apopkafl.api.civicclerk.com/v1"
PORTAL = "https://apopkafl.portal.civicclerk.com"
SOURCE = "Apopka CivicClerk (Planning Commission + City Commission agendas, packets, minutes)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
PC_CAT, CC_CAT = "Planning Commission", "City Commission"
PC_BODY, CC_BODY = "Planning Commission", "City Commission"
LOOKBACK_DAYS = 540   # scan agendas this far before `since` (first appearance + hearing history)

_s = requests.Session()
_s.headers.update({"User-Agent": UA})
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"


def log(*a):
    print("[apopka]", *a, file=sys.stderr)


def get(url, params=None, tries=3, sleep=0.4, timeout=90):
    last = None
    for i in range(tries):
        try:
            r = _s.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            time.sleep(sleep)
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"GET failed {url.split('?')[0]}: {last}")


# ------------------------------------------------------------------ CivicClerk helpers
def list_events(start, end):
    url = f"{API}/Events"
    params = {"$filter": f"startDateTime ge {start}T00:00:00Z and startDateTime lt {end}T00:00:00Z",
              "$orderby": "startDateTime"}
    out = []
    while url:
        d = get(url, params).json()
        out.extend(d.get("value", []))
        url, params = d.get("@odata.nextLink"), None
    keep = []
    for e in out:
        name = e.get("eventName") or ""
        if re.search(r"cancel|workshop|budget", name, re.I):
            continue
        if e.get("categoryName") == PC_CAT or (e.get("categoryName") in (CC_CAT, "Special Meeting")
                                               and re.search(r"Commission|Council", name, re.I)):
            keep.append(e)
    return keep


def walk(items, parent=None):
    for it in items or []:
        yield it, parent
        yield from walk(it.get("childItems"), it)


_TEXT_CACHE = {}


def file_text(file_id):
    """Plain text of a published meeting file (agenda packet). CivicClerk builds the text on
    request; poll its status, then fall back to extracting the PDF with pypdf."""
    if file_id in _TEXT_CACHE:
        return _TEXT_CACHE[file_id]
    txt = ""
    try:
        for _ in range(8):
            st = get(f"{API}/Meetings/GetPlainTextFileStatus(fileId={file_id},retry=false)").json()
            if (st or {}).get("status") == "Completed":
                break
            time.sleep(5)
        txt = get(f"{API}/Meetings/GetMeetingFileStream(fileId={file_id},plainText=true)",
                  timeout=180).text
    except Exception as e:  # noqa: BLE001
        log(f"plain text {file_id}: {e}")
    if len(txt.strip()) < 200 and pypdf is not None:
        log(f"file {file_id}: no plain text from API, extracting PDF")
        try:
            r = get(f"{API}/Meetings/GetMeetingFileStream(fileId={file_id},plainText=false)", timeout=300)
            txt = "\n".join((p.extract_text() or "") for p in pypdf.PdfReader(io.BytesIO(r.content)).pages)
        except Exception as e:  # noqa: BLE001
            log(f"pdf text {file_id}: {e}")
    _TEXT_CACHE[file_id] = txt
    return txt


def pdf_url_text(url):
    r = get(url, timeout=120)
    return "\n".join((p.extract_text() or "") for p in pypdf.PdfReader(io.BytesIO(r.content)).pages)


def file_url(file_id):
    return f"{API}/Meetings/GetMeetingFileStream(fileId={file_id},plainText=false)"


def pub_file(ev, kind):
    for f in ev.get("publishedFiles") or []:
        if f.get("type") == kind:
            return f
    return None


# ------------------------------------------------------------------ text helpers
def clean(v):
    v = html.unescape(re.sub(r"<br\s*/?>", " ", v or "", flags=re.I))
    v = re.sub(r"<[^>]+>", " ", v).replace("\u200b", "")
    v = re.sub(r"[\u2010-\u2015]", "-", v)
    v = re.sub(r"\s+", " ", v).strip(" .,;")
    return v or None


def iso(month, day, year):
    try:
        y = int(year)
        y = y + 2000 if y < 100 else y
        return dt.datetime.strptime(f"{month} {int(day)} {y}", "%B %d %Y").date().isoformat()
    except ValueError:
        return None


ORD_RE = re.compile(r"Ord(?:inance)?\.?\s*(?:No\.?|Number|#)?\s*(\d{4})\b", re.I)
SCOPE_RE = re.compile(r"Future Land Use Amendment|Future Land Use Map|Change of Zoning|Rezoning|"
                      r"PD Rezoning|Planned Development|PUD\b", re.I)
SKIP_RE = re.compile(r"Text Amendment|Element Text|Land Development Code|\bLDC\b|Special Exception|"
                     r"Variance|Vacat|Code of Ordinances", re.I)

PARCEL_TOK = re.compile(r"(?<![\d-])(\d{2}-\d{2}-\d{2}-\d{4}-\d{2}-\d{3})(?![\d])|"
                        r"(?<![\d-])-\s?((?:\d{2}-)?\d{3})(?![\d-])")


def parcels_in(text):
    """Orange County parcel IDs (dd-dd-dd-dddd-dd-ddd), joining IDs broken across lines."""
    t = re.sub(r"[\u2010-\u2015]", "-", text or "")
    t = re.sub(r"(\d)-\s+(\d)", r"\1-\2", t)
    out, base, last_end = [], None, None
    for m in PARCEL_TOK.finditer(t):
        if m.group(1):
            base = m.group(1)
            out.append(base)
        elif base and re.fullmatch(r"[\s,;&]*(?:and)?[\s,;&]*", t[last_end:m.start()] or ""):
            suf = m.group(2)
            head = base.rsplit("-", 2 if "-" in suf else 1)[0]
            out.append(f"{head}-{suf}")
        else:
            base = None
        last_end = m.end()
    return list(dict.fromkeys(out))


def acres_in(text):
    m = re.search(r"([\d,]*\.?\d+)\s*(?:\+/-|\u00b1|\+/\u2013)?\s*(?:gross\s+)?ac(?:re)?s?\b", text or "", re.I) or \
        re.search(r"(?:\+/-|\u00b1)\s*([\d,]*\.?\d+)\s*ac", text or "", re.I)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            return None
    return None


# ------------------------------------------------------------------ staff reports
REPORT_SPLIT = re.compile(r"(PLANNING COMMISSION|CITY (?:COMMISSION|COUNCIL)) STAFF REPORT")
LABELS = [
    ("owner", r"Owner\(?s?\)?"), ("applicant", r"Applicant\(?s?\)?"),
    ("parcels", r"Parcel (?:Identification )?(?:Number|ID)\(?s?\)?"), ("location", r"Location"),
    ("from_flu", r"Existing Future Land Use|Current Future Land Use|Future Land Use"),
    ("to_flu", r"Proposed Future Land Use"),
    ("from_zoning", r"Existing Zoning|Current Zoning|Zoning(?: District)?"),
    ("to_zoning", r"Proposed Zoning"), ("acres", r"Tract Size|Tract|Acreage|Project Size|Size"),
    ("project", r"Project"), ("use", r"Proposed Use"), ("existing_use", r"Existing Use\(?s?\)?"),
    ("density", r"Density(?:/Intensity)?|Proposed Density/Intensity"), ("manager", r"Project Manager"),
]
_LAB = re.compile(r"^\s*(" + "|".join(p for _, p in LABELS) + r")\s*:\s*(.*)$", re.I)


def parse_reports(text):
    """{ord_no: [fields]} for every staff report in a packet's plain text."""
    out = {}
    ms = list(REPORT_SPLIT.finditer(text or ""))
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        chunk = text[m.end():min(end, m.end() + 15000)]
        sm = re.search(r"SUBJECT:\s*(.+?)\n\s*\n|SUBJECT:\s*(.+?)ACTION ITEM", chunk, re.S)
        subject = clean((sm.group(1) or sm.group(2)) if sm else "") or ""
        om = ORD_RE.search(subject)
        if not om:
            continue
        d = {"subject": subject, "_body": PC_BODY if m.group(1).startswith("PLANNING") else CC_BODY}
        rq = re.search(r"REQUEST:\s*(.+?)\n\s*\n", chunk, re.S)
        if rq:
            d["request"] = clean(rq.group(1))
        # labelled lines: prefer the SUMMARY block, fall back to ACTION ITEM INFORMATION
        body = chunk
        sumi = chunk.find("SUMMARY:")
        cut = re.search(r"\n\s*(?:Direction\s+Future Land Use|PROJECT SUMMARY|BACKGROUND|ADDITIONAL COMMENTS)", chunk)
        blocks = [chunk[sumi:cut.start() if cut and cut.start() > sumi else sumi + 4000]] if sumi >= 0 else []
        blocks.append(chunk[:sumi if sumi > 0 else 3000])
        for blk in blocks:
            cur = None
            for line in blk.splitlines():
                lm = _LAB.match(line)
                if lm:
                    lab = lm.group(1)
                    cur = next(k for k, p in LABELS if re.fullmatch(p, lab, re.I))
                    if cur in d and d[cur]:
                        cur = "_dup"
                        continue
                    d[cur] = lm.group(2).strip()
                elif cur and cur != "_dup" and line.strip() and cur in ("parcels", "owner", "location", "acres") \
                        and not re.match(r"^\s*[A-Z][A-Za-z ()/]+:", line):
                    d[cur] = (d[cur] + " " + line.strip()).strip()
                else:
                    cur = None
        out.setdefault(om.group(1), []).append(d)
    return out


# ------------------------------------------------------------------ minutes
HEAD_RE = re.compile(r"(?m)^\s*\d+\.\s+(?:[A-Za-z- ]+[-:]\s*)?Ordinance\s+(?:No\.?|Number)\s*(\d{4})\b")


def minutes_outcomes(text):
    """{ord_no: (motion text, outcome word)} from Planning Commission or City Commission minutes."""
    t = re.sub(r"[\u2010-\u2015]", "-", text or "")
    heads = list(HEAD_RE.finditer(t))
    out = {}
    for i, h in enumerate(heads):
        seg = t[h.end(): heads[i + 1].start() if i + 1 < len(heads) else h.end() + 6000][:6000]
        mm = re.search(r"(?:asked for a\s+[Mm]otion\s+to|[Mm]otion\s+by\s+[^.]{0,80}?\s+to|[Mm]otioned\s+by\s+[^.]{0,80}?\s+to|"
                       r"[Mm]otion\s+to)\s+(.{0,500}?)\.(?=\s+(?!\d)|$)", seg, re.S)
        oc = re.search(r"[Mm]otion\s+(carried|failed|died|passed)", seg)
        if mm or oc:
            out.setdefault(h.group(1), (re.sub(r"\s+", " ", mm.group(1)) if mm else "",
                                        oc.group(1).lower() if oc else None))
    return out


def classify(motion, outcome, body, reading):
    t = (motion or "").lower()
    if outcome in ("failed", "died"):
        return "Motion failed"
    if "withdr" in t:
        return "Withdrawn"
    if re.search(r"continu|table|postpon|defer", t):
        return "Continued"
    deny = re.search(r"\bden(y|ied|ial)\b", t)
    if body == PC_BODY:
        if deny:
            return "Recommended Denial"
        return "Recommended Approval" if re.search(r"approv|recommend|consistent", t) or outcome else None
    if deny:
        return "Denied"
    if "transmit" in t:
        return "Transmitted"
    if reading == 1 or "first reading" in t:
        return "Approved on First Reading" if (re.search(r"approv|adopt", t) or outcome) else None
    if re.search(r"approv|adopt", t) or outcome == "carried":
        return "Approved"
    return None


# ------------------------------------------------------------------ fetch
def fetch(since, refresh):
    today = dt.date.today()
    refresh = {m.group(1) for r in (refresh or []) if r for m in [re.search(r"(\d{4})", r)] if m}
    start_d = dt.date.fromisoformat(since) - dt.timedelta(days=LOOKBACK_DAYS)
    end_d = today + dt.timedelta(days=120)
    events = list_events(start_d.isoformat(), end_d.isoformat())
    log(f"{len(events)} Planning Commission / City Commission meetings {start_d}..{end_d}")

    apps = {}       # ord -> list of appearances
    minutes = {}    # (body, date) -> attachment url
    for ev in events:
        if not ev.get("agendaId"):
            continue
        try:
            mt = get(f"{API}/Meetings/{ev['agendaId']}").json()
        except Exception as e:  # noqa: BLE001
            log(f"meeting {ev.get('agendaId')}: {e}")
            continue
        mdate = ev["startDateTime"][:10]
        body = PC_BODY if ev["categoryName"] == PC_CAT else CC_BODY
        ag = pub_file(ev, "Agenda")
        first_seen = min(mdate, ((ag or {}).get("publishOn") or mdate)[:10])
        for it, parent in walk(mt.get("items")):
            name = clean(it.get("agendaObjectItemName")) or ""
            pname = clean((parent or {}).get("agendaObjectItemName")) or ""
            if re.search(r"minutes", name, re.I) and re.search(r"minutes", pname + name, re.I):
                for a in it.get("attachmentsList") or []:
                    fn = a.get("fileName") or ""
                    if not re.search(r"minutes", fn, re.I) or not a.get("pdfVersionFullPath"):
                        continue
                    dm = re.search(r"(\d{1,2})-(\d{1,2})-(\d{2,4})", fn)
                    d = None
                    if dm:
                        y = int(dm.group(3))
                        y = y + 2000 if y < 100 else y
                        try:
                            d = dt.date(y, int(dm.group(1)), int(dm.group(2))).isoformat()
                        except ValueError:
                            d = None
                    if not d:
                        nm = re.search(rf"({MONTHS})\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})", name)
                        d = iso(*nm.groups()) if nm else None
                    if d:
                        mb = PC_BODY if (body == PC_BODY or re.search(r"\bPC\b|Planning", fn + name)) else CC_BODY
                        if re.search(r"budget|workshop|special|CRA|redevelopment", fn, re.I):
                            continue
                        minutes.setdefault((mb, d), [])
                        if a["pdfVersionFullPath"] not in minutes[(mb, d)]:
                            minutes[(mb, d)].append(a["pdfVersionFullPath"])
                continue
            om = ORD_RE.search(name)
            if not om or not SCOPE_RE.search(name) or SKIP_RE.search(name):
                continue
            if re.search(r"Annexation", name, re.I) and not re.search(r"Zoning|Land Use", name, re.I):
                continue
            reading = 1 if re.search(r"First\s+Reading", name, re.I) else (
                2 if re.search(r"Second\s+Reading|Adoption", name, re.I) else None)
            apps.setdefault(om.group(1), []).append({
                "date": mdate, "first_seen": first_seen, "body": body, "name": name, "reading": reading,
                "event": ev, "url": f"{PORTAL}/event/{ev['id']}/files",
                "withdrawn": bool(re.search(r"withdr", name, re.I)),
                "continued": bool(re.search(r"continu|postpone", name, re.I)),
            })
    log(f"{len(apps)} land use ordinances on agendas; {len(minutes)} minutes attachments")

    todo = {}
    for o, aps in apps.items():
        first = min(a["first_seen"] for a in aps)
        if first >= since or o in refresh:
            todo[o] = aps
    log(f"{len(todo)} ordinances to report")

    # staff reports from the packets of the meetings these cases were on
    reports = {}
    for ev_id in sorted({a["event"]["id"] for aps in todo.values() for a in aps}):
        ev = next(a["event"] for aps in todo.values() for a in aps if a["event"]["id"] == ev_id)
        pk = pub_file(ev, "Agenda Packet")
        if not pk:
            continue
        try:
            rep = parse_reports(file_text(pk["fileId"]))
        except Exception as e:  # noqa: BLE001
            log(f"packet {ev['startDateTime'][:10]}: {e}")
            continue
        for o, lst in rep.items():
            for d in lst:
                d["_date"] = ev["startDateTime"][:10]
                reports.setdefault(o, []).append(d)

    # outcomes from minutes
    outcomes = {}
    need = {(a["body"], a["date"]) for aps in todo.values() for a in aps if a["date"] <= today.isoformat()}
    for key in sorted(need):
        for url in minutes.get(key, []):
            try:
                oc = minutes_outcomes(pdf_url_text(url))
            except Exception as e:  # noqa: BLE001
                log(f"minutes {key}: {e}")
                continue
            outcomes.setdefault(key, {})
            for k, v in oc.items():
                outcomes[key].setdefault(k, v)

    out = []
    for o, aps in sorted(todo.items()):
        try:
            out.append(build(o, aps, reports.get(o, []), outcomes, today.isoformat()))
        except Exception as e:  # noqa: BLE001
            log(f"Ordinance {o}: skipped ({e})")
    for r in refresh - set(todo):
        log(f"refresh id Ordinance {r} not on agendas since {start_d}")
    return out


def build(o, aps, reps, outcomes, today):
    aps = sorted(aps, key=lambda a: (a["date"], a["body"] != PC_BODY))
    title = aps[-1]["name"]
    f = {}
    generic = re.compile(r"^(?:Adopt|Accept|Approve|Hold)\b.{0,60}Ordinance", re.I)
    req = next((d["request"] for d in sorted(reps, key=lambda d: d["_date"])
                if d.get("request") and not generic.match(d["request"])), None)
    for d in sorted(reps, key=lambda d: d["_date"]):
        for k, v in d.items():
            if v and not k.startswith("_"):
                f.setdefault(k, v) if k == "subject" else f.__setitem__(k, v)
    is_flu = bool(re.search(r"Future Land Use", title, re.I))
    pd_text = " ".join(filter(None, [title, f.get("to_zoning")]))
    is_pd = bool(re.search(r"\bPD\b|Planned Development|\bPUD\b|Master Plan", pd_text, re.I))
    category = "Comp Plan Amendment" if is_flu else ("PD" if is_pd else "Rezoning")
    m = re.search(r"((?:Small|Large)[- ]Scale Future Land Use Amendment|Future Land Use Amendment|"
                  r"Change of Zoning[^-]*?(?=\s-\s|$)|Rezoning[^-]*?(?=\s-\s|$))", title, re.I)
    app_type = clean(m.group(1)) if m else ("Future Land Use Amendment" if is_flu else "Change of Zoning")
    large = bool(re.search(r"Large[- ]Scale", title, re.I))

    # hearings
    hmap = {}
    for a in aps:
        res = None
        oc = outcomes.get((a["body"], a["date"]))
        if oc is not None and o in oc:
            res = classify(oc[o][0], oc[o][1], a["body"], a["reading"])
        if a["withdrawn"]:
            res = "Withdrawn"
        k = (a["body"], a["date"])
        h = hmap.setdefault(k, {"body": a["body"], "date": a["date"], "result": None,
                                "_minutes": oc is not None, "_reading": a["reading"]})
        if res:
            h["result"] = res
    hearings = sorted(hmap.values(), key=lambda h: (h["date"], h["body"] != PC_BODY))

    status, raw, decision = None, None, None
    past = [h for h in hearings if h["date"] <= today]
    future = [h for h in hearings if h["date"] > today]
    final = [h for h in past if h["body"] == CC_BODY and h["result"] in ("Approved", "Denied")
             and not (large and h.get("_reading") == 1)]
    if any(h["result"] == "Withdrawn" for h in hearings):
        status, raw = "Withdrawn", "Withdrawn (agenda / minutes)"
    elif final:
        status, decision = final[-1]["result"], final[-1]["date"]
        raw = f"City Commission {decision}: {final[-1]['result']} (minutes)"
    elif past and past[-1]["result"] == "Continued":
        status = "Continued"
        raw = f"{past[-1]['body']} {past[-1]['date']}: continued" + (
            f"; next {future[0]['body']} {future[0]['date']}" if future else "")
    elif future:
        status, raw = "Hearing Scheduled", f"next {future[0]['body']} {future[0]['date']}"
        if past and past[-1]["result"]:
            raw = f"{past[-1]['body']} {past[-1]['date']}: {past[-1]['result']}; " + raw
    elif past and past[-1]["result"] is None:
        status = "Hearing Scheduled"
        raw = f"{past[-1]['body']} hearing {past[-1]['date']} held; outcome not yet in published minutes"
    elif past:
        status = "Filed"
        raw = f"{past[-1]['body']} {past[-1]['date']}: {past[-1]['result']}; next hearing not yet scheduled"
    else:
        status, raw = "Filed", "On agenda"

    loc = f.get("location")
    if not loc:
        parts = re.split(r"\s-\s", title)
        loc = parts[-1] if len(parts) > 1 else None
    pname = None
    parts = [p.strip() for p in re.split(r"\s-\s", title) if p.strip()]
    if parts and not re.match(r"^\d", parts[-1]):
        pname = parts[-1]
    acres = acres_in(f.get("acres")) or acres_in(f.get("project")) or acres_in(f.get("request"))
    desc = req
    if not desc:
        bits = [app_type]
        a_, b_ = (f.get("from_flu"), f.get("to_flu")) if is_flu else (f.get("from_zoning"), f.get("to_zoning"))
        if a_ and b_:
            bits.append(f"from {a_} to {b_}")
        if f.get("use"):
            bits.append(f"for {f['use']}")
        desc = " ".join(bits)
    if desc and len(desc) > 300:
        desc = desc[:297] + "..."
    applicant = f.get("applicant") or f.get("owner")
    if applicant and f.get("owner") and f.get("owner") != applicant:
        applicant = f"{applicant} (owner: {f['owner']})"
    latest = aps[-1]
    pk = pub_file(latest["event"], "Agenda Packet")
    return {
        "jurisdiction": JUR, "county": COUNTY, "app_num": f"Ordinance {o}", "category": category,
        "app_type": app_type, "project_name": pname, "description": desc, "applicant": applicant,
        "parcel_ids": parcels_in(f.get("parcels")), "address": loc, "acres": acres,
        "from_zoning": f.get("from_zoning") if not is_flu else f.get("from_zoning"),
        "to_zoning": f.get("to_zoning") if not is_flu else None,
        "from_flu": f.get("from_flu") if is_flu else None, "to_flu": f.get("to_flu") if is_flu else None,
        "filed_date": min(a["first_seen"] for a in aps), "status": status, "status_raw": raw,
        "hearings": [{"body": h["body"], "date": h["date"], "result": h["result"]} for h in hearings],
        "decision_date": decision, "url": latest["url"],
        "docs_url": file_url(pk["fileId"]) if pk else None,
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
