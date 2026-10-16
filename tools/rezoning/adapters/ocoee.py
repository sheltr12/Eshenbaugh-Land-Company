#!/usr/bin/env python3
"""City of Ocoee rezoning adapter.

Source: CivicClerk public API (ocoeefl.api.civicclerk.com).
 * Planning & Zoning Commission (Local Planning Agency) agendas carry land use cases as items
   titled with the city's project numbers, e.g. "5068 Adair Street - Torres Aguilar/Salcedo
   Property, Annexation & Rezoning from Orange County A-1 to City of Ocoee R-1; Project No.
   AX-03-26-01 & RZ-26-03-01". City Commission agendas carry "First Reading of the Ordinance
   for ..." and "Second Reading ..." items with the same title and project numbers.
   RZ-yy-mm-nn = rezoning (PUD = planned development), CPA-yyyy-nnn = comprehensive plan
   amendment (map amendments kept, text-only amendments skipped), AX = annexation.
 * Staff reports are generated into the agenda packet (read as plain text through the API):
   owner / applicant, "approximately 0.38 acres", "Parcel I.D. number 05-22-28-6052-05-010".
 * Outcomes come from the draft minutes attached to the next agenda's "Approval of Minutes"
   item ("Motion: Move to adopt Ordinance 2026-14 ... Project Number RZ-26-03-01 ...; Motion
   carried 5-0"). The minutes PDFs that CivicClerk publishes as "Minutes" are scanned images
   without a text layer, so they are not used.
app_num is the RZ / CPA project number as the city writes it. Cases first appear when the
P&Z agenda is posted (a few days before the hearing). Ocoee's annexation-plus-rezoning cases
are mostly single lots; volume is low (a handful of cases a year).
"""
import datetime as dt
import html
import io
import json
import re
import sys
import time
from collections import Counter

import requests

try:
    import pypdf
except ImportError:  # pragma: no cover
    pypdf = None

JURISDICTIONS = ["City of Ocoee"]
JUR, COUNTY = "City of Ocoee", "Orange"

API = "https://ocoeefl.api.civicclerk.com/v1"
PORTAL = "https://ocoeefl.portal.civicclerk.com"
SOURCE = "Ocoee CivicClerk (P&Z Commission + City Commission agendas, packets, draft minutes)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
PZ_CAT, CC_CAT = "Planning & Zoning Commission", "City Commission"
PZ_BODY, CC_BODY = "Planning & Zoning Commission", "City Commission"
LOOKBACK_DAYS = 540   # scan agendas this far before `since` (first appearance + hearing history)

_s = requests.Session()
_s.headers.update({"User-Agent": UA})
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"


def log(*a):
    print("[ocoee]", *a, file=sys.stderr)


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
    return [e for e in out if e.get("categoryName") in (PZ_CAT, CC_CAT)
            and not re.search(r"cancel|workshop|budget", e.get("eventName") or "", re.I)]


def walk(items, parent=None):
    for it in items or []:
        yield it, parent
        yield from walk(it.get("childItems"), it)


_TEXT_CACHE = {}


def file_text(file_id):
    """Plain text of a published meeting file. CivicClerk builds it on request: poll the status
    (asking for a rebuild once if the text comes back empty), then fall back to pypdf."""
    if file_id in _TEXT_CACHE:
        return _TEXT_CACHE[file_id]
    txt = ""
    for retry in ("false", "true"):
        try:
            for _ in range(10):
                st = get(f"{API}/Meetings/GetPlainTextFileStatus(fileId={file_id},retry={retry})").json()
                retry = "false"
                if (st or {}).get("status") == "Completed":
                    break
                time.sleep(6)
            txt = get(f"{API}/Meetings/GetMeetingFileStream(fileId={file_id},plainText=true)", timeout=180).text
        except Exception as e:  # noqa: BLE001
            log(f"plain text {file_id}: {e}")
        if len(txt.strip()) >= 200:
            break
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
    v = re.sub(r"<[^>]+>", " ", v).replace("\u200b", "").replace("\xa0", " ")
    v = re.sub(r"[\u2010-\u2015]", "-", v)
    v = re.sub(r"\s+", " ", v).strip(" .,;")
    return v or None


PROJ_RE = re.compile(r"\b(AX|RZ|CPA|SSCPA|LSCPA)\s*-\s*(\d{2,4}(?:\s*-\s*\d{2,3}){1,2})\b", re.I)


def projects(text):
    """[(raw spelling, normalized)] project numbers in a title / minutes text."""
    out = []
    for m in PROJ_RE.finditer(text or ""):
        norm = f"{m.group(1).upper()}-" + re.sub(r"\s+", "", m.group(2))
        out.append((re.sub(r"\s*-\s*", "-", m.group(0)), norm))
    return out


def addr_token(title):
    """'1460 N. Lakewood PUD - ...' -> '1460 LAKEWOOD' (ties items that lack project numbers)."""
    m = re.match(r"\s*(?:(?:First|Second) Reading of (?:the |an )?Ordinances? for (?:the )?)?(\d{2,6})\s+"
                 r"(?:(?:N|S|E|W|North|South|East|West)\.?\s+)?([A-Za-z]+)", title or "", re.I)
    return f"{m.group(1)} {m.group(2).upper()}" if m else None


PARCEL_TOK = re.compile(r"(?<![\d-])(\d{2}-\d{2}-\d{2}-\d{4}-\d{2}-\d{3})(?![\d])|"
                        r"(?<![\d-])-\s?((?:\d{2}-)?\d{3})(?![\d-])")


def parcels_in(text):
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


SCOPE_RE = re.compile(r"\bRZ\s*-|Rezoning|Rezone|Future Land Use Map Amendment|Comprehensive Plan .{0,40}Map|"
                      r"\bSSCPA\b|Small[- ]Scale Comprehensive|Large[- ]Scale Comprehensive|"
                      r"(?<!Non-)Substantial Amendment", re.I)
SKIP_RE = re.compile(r"Text Amendment|Capital Improvement|Special Exception|Variance|Site Plan|"
                     r"Non-Substantial|Vacat|Land Development Code", re.I)


# ------------------------------------------------------------------ staff reports
def parse_reports(text):
    """[{subject, fields}] for each staff report in a packet's plain text."""
    out = []
    ms = list(re.finditer(r"STAFF REPORT\s*\n", text or ""))
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        chunk = text[m.end():min(end, m.end() + 12000)]
        sm = re.search(r"Subject:\s*(.+?)(?:BACKGROUND SUMMARY|ISSUE:|\n\s*\n\s*\n)", chunk[:3000], re.S)
        if not sm:
            continue
        d = {"subject": clean(sm.group(1)) or ""}
        om = re.search(r"Owner\s*/\s*Applicant:\s*(.+?)\n\s*\n", chunk, re.S) or \
            re.search(r"Applicant\s*/\s*Owner:\s*(.+?)\n\s*\n", chunk, re.S)
        if om:
            d["owner"] = clean(om.group(1))
        pm = re.search(r"Property Size[^:]*:\s*(.+?)\n\s*\n", chunk, re.S)
        if pm:
            d["size"] = clean(pm.group(1))
            if not re.search(r"\d\s*(?:\+/-|\u00b1)?\s*acres?", d["size"], re.I):
                # acreage table ('508 Woodson  17-22-28-0000-  0.38'): sum the acreage column
                tb = re.search(r"Property Size.*?(?=Existing Site Conditions|Surrounding Properties|$)", chunk, re.S)
                vals = re.findall(r"(?<![\d.-])(\d{1,4}\.\d{1,3})[ \t]*\r?\n", tb.group(0) if tb else "")
                if vals:
                    d["size"] += f" (table total {round(sum(float(v) for v in vals), 2)} acres)"
        lm = re.search(r"General Location:\s*(.+?)\n\s*\n", chunk, re.S)
        if lm:
            d["location"] = clean(lm.group(1))
        im = re.search(r"ISSUE:\s*(.+?)\n\s*\n", chunk, re.S)
        if im:
            d["issue"] = clean(im.group(1))
        d["parcels"] = parcels_in(chunk[:6000])
        # parcel IDs split across table rows: '17-22-28-0000-    0.38 \n Avenue   00-076'
        for pm2 in re.finditer(r"(\d{2}-\d{2}-\d{2}-\d{4}-)[ \t]+\S*[^\n]*\n[^\n]*?\b(\d{2}-\d{3})\b", chunk[:6000]):
            pid = pm2.group(1) + pm2.group(2)
            if pid not in d["parcels"]:
                d["parcels"].append(pid)
        out.append(d)
    return out


# ------------------------------------------------------------------ minutes
def minutes_motion(text, keys):
    """Find the motion for a case in minutes text. keys: normalized project numbers and an
    address token. Returns (result kind, motion text) or (None, None) when the item is found
    without a motion (e.g. a first reading), or None when the item is not in these minutes."""
    t = re.sub(r"[\u2010-\u2015]", "-", text or "")
    norm = re.sub(r"\s*-\s*", "-", t)
    up = norm.upper()
    pos = None
    for k in keys:
        if not k:
            continue
        i = up.find(k.upper())
        if i >= 0 and (pos is None or i < pos):
            pos = i
    if pos is None:
        return None
    nxt = re.search(r"\n\s*\d{1,2}\.\s+[A-Z0-9]", norm[pos + 80:])
    seg = norm[pos: pos + 80 + (nxt.start() if nxt else 6000)]
    mots = re.findall(r"Motion:\s*(.+?Motion\s+(?:carried|failed|died)[^.;\n]*)", seg, re.S | re.I)
    if not mots:
        return (None, None)
    rz = [m for m in mots if re.search(r"\b(RZ|CPA|SSCPA)-|Rezon|Land Use|Comprehensive", m, re.I)]
    mot = re.sub(r"\s+", " ", (rz or mots)[-1])
    return ("failed" if re.search(r"Motion\s+(failed|died)", mot, re.I) else "carried", mot)


def classify(kind, mot, body, reading):
    if kind is None:
        return "First Reading Held" if (body == CC_BODY and reading == 1) else None
    t = mot.lower()
    if kind == "failed":
        return "Motion failed"
    if "withdr" in t:
        return "Withdrawn"
    if re.search(r"continu|table|postpon|defer", t):
        return "Continued"
    deny = re.search(r"\bden(y|ied|ial)\b", t)
    if body == PZ_BODY:
        return "Recommended Denial" if deny else "Recommended Approval"
    if deny:
        return "Denied"
    if "transmit" in t:
        return "Transmitted"
    if reading == 1:
        return "Approved on First Reading"
    return "Approved"


# ------------------------------------------------------------------ fetch
def fetch(since, refresh):
    today = dt.date.today()
    refresh_n = {re.sub(r"\s+", "", r).upper(): r for r in (refresh or []) if r and r.strip()}
    start_d = dt.date.fromisoformat(since) - dt.timedelta(days=LOOKBACK_DAYS)
    yrs = []
    for r in refresh_n:
        m = re.match(r"(?:RZ|AX)-(\d{2})-", r) or re.match(r"(?:SS|LS)?CPA-(\d{4})-", r)
        if m:
            y = int(m.group(1))
            yrs.append(y + 2000 if y < 100 else y)
    if yrs:
        start_d = min(start_d, dt.date(min(yrs), 1, 1))
    end_d = today + dt.timedelta(days=120)
    events = list_events(start_d.isoformat(), end_d.isoformat())
    log(f"{len(events)} P&Z / City Commission meetings {start_d}..{end_d}")

    apps, minutes = [], {}
    for ev in events:
        if not ev.get("agendaId"):
            continue
        try:
            mt = get(f"{API}/Meetings/{ev['agendaId']}").json()
        except Exception as e:  # noqa: BLE001
            log(f"meeting {ev.get('agendaId')}: {e}")
            continue
        mdate = ev["startDateTime"][:10]
        body = PZ_BODY if ev["categoryName"] == PZ_CAT else CC_BODY
        ag = pub_file(ev, "Agenda")
        first_seen = min(mdate, ((ag or {}).get("publishOn") or mdate)[:10])
        for it, parent in walk(mt.get("items")):
            name = clean(it.get("agendaObjectItemName")) or ""
            if re.search(r"Approval of Minutes", name, re.I):
                for a in it.get("attachmentsList") or []:
                    fn = a.get("fileName") or ""
                    dm = re.match(r"\s*(\d{1,2})-(\d{1,2})-(\d{2,4})\s+(?:Draft\s+)?Minutes", fn, re.I)
                    if dm and a.get("pdfVersionFullPath"):
                        y = int(dm.group(3))
                        y = y + 2000 if y < 100 else y
                        try:
                            d = dt.date(y, int(dm.group(1)), int(dm.group(2))).isoformat()
                        except ValueError:
                            continue
                        minutes.setdefault((body, d), a["pdfVersionFullPath"])
                continue
            if not SCOPE_RE.search(name) or SKIP_RE.search(name):
                continue
            prj = projects(name)
            if prj and not any(n.startswith(("RZ", "CPA", "SSCPA", "LSCPA")) for _, n in prj) \
                    and not re.search(r"Rezon|Land Use Map|Substantial", name, re.I):
                continue
            if any(n.startswith("CPA") for _, n in prj) and not re.search(r"Map|Rezon|Land Use Map|Small|Large", name, re.I):
                continue
            reading = 1 if re.search(r"First Reading", name, re.I) else (
                2 if re.search(r"Second Reading", name, re.I) else None)
            apps.append({"date": mdate, "first_seen": first_seen, "body": body, "name": name, "reading": reading,
                         "projects": prj, "addr": addr_token(name), "event": ev,
                         "url": f"{PORTAL}/event/{ev['id']}/files",
                         "withdrawn": bool(re.search(r"withdr", name, re.I))})
    log(f"{len(apps)} land use agenda items; {len(minutes)} draft minutes attachments")

    # group appearances into cases (union-find over project numbers and address tokens)
    parent = list(range(len(apps)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    seen = {}
    for i, a in enumerate(apps):
        keys = [n for _, n in a["projects"]] + ([f"@{a['addr']}"] if a["addr"] else [])
        for k in keys:
            if k in seen:
                parent[find(i)] = find(seen[k])
            else:
                seen[k] = i
    groups = {}
    for i, a in enumerate(apps):
        groups.setdefault(find(i), []).append(a)

    cases = []
    for aps in groups.values():
        spell = Counter()
        last = {}
        for a in aps:
            for raw, n in a["projects"]:
                if n.startswith(("RZ", "CPA", "SSCPA", "LSCPA")):
                    spell[raw.upper()] += 1
                    last[raw.upper()] = a["date"]
        if spell:
            best = max(spell, key=lambda k: (spell[k], last[k]))
            app_num = best
        else:
            app_num = re.split(r"\s-\s|;", aps[0]["name"])[0].strip()
        first = min(a["first_seen"] for a in aps)
        norm_ids = {re.sub(r"\s+", "", r).upper() for a in aps for r, _ in a["projects"]}
        hit_refresh = bool(norm_ids & set(refresh_n)) or re.sub(r"\s+", "", app_num).upper() in refresh_n
        if first < since and not hit_refresh:
            continue
        cases.append({"app_num": app_num, "apps": sorted(aps, key=lambda a: a["date"]), "first_seen": first})
    log(f"{len(groups)} cases on agendas, {len(cases)} to report")

    reports = {}
    need_ev = set()
    for c in cases:
        pz = [a for a in c["apps"] if a["body"] == PZ_BODY and pub_file(a["event"], "Agenda Packet")]
        need_ev.update(a["event"]["id"] for a in (pz[-1:] if pz else c["apps"][-1:]))
    for ev_id in sorted(need_ev):
        ev = next(a["event"] for c in cases for a in c["apps"] if a["event"]["id"] == ev_id)
        pk = pub_file(ev, "Agenda Packet")
        if not pk:
            continue
        try:
            reports[ev_id] = parse_reports(file_text(pk["fileId"]))
        except Exception as e:  # noqa: BLE001
            log(f"packet {ev['startDateTime'][:10]}: {e}")

    mtext = {}
    for key in sorted({(a["body"], a["date"]) for c in cases for a in c["apps"] if a["date"] <= today.isoformat()}):
        url = minutes.get(key)
        if url:
            try:
                mtext[key] = pdf_url_text(url)
            except Exception as e:  # noqa: BLE001
                log(f"minutes {key}: {e}")

    out = []
    for c in cases:
        try:
            out.append(build(c, reports, mtext, today.isoformat()))
        except Exception as e:  # noqa: BLE001
            log(f"{c['app_num']}: skipped ({e})")
    found = {re.sub(r"\s+", "", x).upper() for c in cases for a in c["apps"] for x, _ in a["projects"]}
    for r in set(refresh_n) - found - {re.sub(r"\s+", "", c["app_num"]).upper() for c in cases}:
        log(f"refresh id {refresh_n[r]} not on agendas since {start_d}")
    return out


def build(c, reports, mtext, today):
    aps = c["apps"]
    keys = list(dict.fromkeys(n for a in aps for _, n in a["projects"] if not n.startswith("AX"))) + \
        list(dict.fromkeys(n for a in aps for _, n in a["projects"] if n.startswith("AX")))
    addr = next((a["addr"] for a in aps if a["addr"]), None)
    # staff report: the one whose subject shares a project number or the address token
    rep = {}
    for a in aps:
        for d in reports.get(a["event"]["id"], []):
            subj = d["subject"].upper()
            if any(k in re.sub(r"\s*-\s*", "-", subj) for k in keys) or (addr and addr_token(d["subject"]) == addr):
                for k, v in d.items():
                    if v and (k not in rep or a["body"] == PZ_BODY):
                        rep[k] = v
    title = max((a["name"] for a in aps), key=len)
    t_up = title.upper()
    is_cpa = bool(re.search(r"\bCPA-|COMPREHENSIVE PLAN|FUTURE LAND USE MAP", re.sub(r"\s*-\s*", "-", t_up)))
    is_pd = bool(re.search(r"\bPUD\b|PLANNED UNIT|PLANNED DEVELOPMENT", t_up))
    category = "Comp Plan Amendment" if is_cpa else ("PD" if is_pd else "Rezoning")
    fz = re.search(r"Rezoning (?:to PUD )?from (.+?) to (.+?)(?:;|\s*&\s*PUD|\s+Project|$)", title, re.I)
    from_z, to_z = (clean(fz.group(1)), clean(fz.group(2))) if fz else (None, None)
    if not to_z and re.search(r"Rezoning to PUD", title, re.I):
        to_z = "PUD"
    issue = rep.get("issue") or ""
    if not from_z:
        fz2 = re.search(r"Rezoning from (.+?) to (.+?)(?:\s+for\b|\s+as requested|\?|$)", issue, re.I)
        if fz2:
            from_z, to_z = clean(fz2.group(1)), clean(fz2.group(2))
    from_flu = to_flu = None
    if is_cpa:
        ff = re.search(r"Map Amendment from (.+?) to (.+?)(?:\s+and\b|\s+for\b|\?|$)", issue, re.I)
        if ff:
            from_flu, to_flu = clean(ff.group(1)), clean(ff.group(2))

    hmap = {}
    for a in aps:
        res = None
        mt = mtext.get((a["body"], a["date"]))
        if mt is not None:
            mk = [k for k in keys if not k.startswith("AX")] + [k for k in keys if k.startswith("AX")]
            hit = minutes_motion(mt, mk + ([addr] if addr else []))
            if hit is not None:
                res = classify(hit[0], hit[1], a["body"], a["reading"])
        if a["withdrawn"]:
            res = "Withdrawn"
        h = hmap.setdefault((a["body"], a["date"]), {"body": a["body"], "date": a["date"], "result": None,
                                                     "_reading": a["reading"]})
        if res:
            h["result"] = res
    hearings = sorted(hmap.values(), key=lambda h: (h["date"], h["body"] != PZ_BODY))

    status, raw, decision = None, None, None
    past = [h for h in hearings if h["date"] <= today]
    future = [h for h in hearings if h["date"] > today]
    final = [h for h in past if h["body"] == CC_BODY and h["result"] in ("Approved", "Denied")]
    if any(h["result"] == "Withdrawn" for h in hearings):
        status, raw = "Withdrawn", "Withdrawn (agenda / minutes)"
    elif final:
        status, decision = final[-1]["result"], final[-1]["date"]
        raw = f"City Commission {decision}: {final[-1]['result']} (draft minutes)"
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
        raw = f"{past[-1]['body']} hearing {past[-1]['date']} held; outcome not yet in published draft minutes"
    elif past:
        status = "Filed"
        raw = f"{past[-1]['body']} {past[-1]['date']}: {past[-1]['result']}; next hearing not yet scheduled"
    else:
        status, raw = "Filed", "On agenda"

    acres = None
    for src in (rep.get("size"), rep.get("issue")):
        m = re.search(r"table total ([\d.]+) acres", src or "") or \
            re.search(r"([\d,]*\.?\d+)\s*(?:\+/-|\u00b1)?\s*acres?", src or "", re.I)
        if m:
            try:
                acres = float(m.group(1).replace(",", ""))
                break
            except ValueError:
                pass
    pz_title = next((a["name"] for a in aps if a["body"] == PZ_BODY), aps[0]["name"])
    pz_title = re.sub(r"\s*\([^()]*(?:Director|Manager|Coordinator|Planner|Administrator|Clerk|Chief|Engineer|"
                      r"Attorney)[^()]*\)\s*\.?\s*$", "", pz_title)
    head = re.split(r";\s*Project No", pz_title)[0]
    head = re.sub(r"^(?:First|Second) Reading of (?:the |an )?Ordinances? for (?:the )?", "", head, flags=re.I)
    parts = [p.strip() for p in re.split(r"\s+-\s+", head) if p.strip()]
    address = parts[0] if parts and re.match(r"\d", parts[0]) else (rep.get("location"))
    cand = parts[1] if len(parts) > 1 else (parts[0] if parts else "")
    pname = re.split(r",|\s*\b(?:Property\s+)?(?:Annexation|Rezoning|Small-Scale|Large-Scale|Comprehensive|"
                     r"Substantial)\b", cand)[0].strip(" -") or (parts[0] if parts else None)
    desc = rep.get("issue") or head
    if desc and len(desc) > 300:
        desc = desc[:297] + "..."
    owner = rep.get("owner")
    if owner:
        owner = re.sub(r"^The (?:property |developer)?\s*(?:owners?|developers?)\s*/\s*(?:applicants?|owners?),?\s*"
                       r"(?:is|are)\s+", "", owner, flags=re.I)
        owner = re.sub(r",?\s*referred to as[^,]*,?", "", owner)
    if owner and len(owner) > 200:
        owner = owner[:197] + "..."
    nums = sorted({r for a in aps for r, _ in a["projects"]})
    bits = []
    if re.search(r"Small[- ]Scale", title, re.I):
        bits.append("Small-Scale FLU Map Amendment")
    elif re.search(r"Large[- ]Scale", title, re.I):
        bits.append("Large-Scale FLU Map Amendment")
    elif is_cpa:
        bits.append("FLU Map Amendment")
    if re.search(r"Annexation", title, re.I):
        bits.insert(0, "Annexation")
    if re.search(r"Substantial Amendment", title, re.I):
        bits.append("PUD Substantial Amendment")
    elif re.search(r"Rezon", title, re.I):
        bits.append("Rezoning to PUD" if is_pd else "Rezoning")
    app_type = " & ".join(bits) or category
    if nums:
        app_type = f"{app_type} ({', '.join(sorted({n.upper() for n in nums}))})"
    latest = aps[-1]
    pk = pub_file(next((a for a in aps if a["body"] == PZ_BODY), latest)["event"], "Agenda Packet")
    return {
        "jurisdiction": JUR, "county": COUNTY, "app_num": c["app_num"], "category": category,
        "app_type": app_type, "project_name": pname, "description": desc, "applicant": owner,
        "parcel_ids": rep.get("parcels") or [], "address": address, "acres": acres,
        "from_zoning": from_z, "to_zoning": to_z, "from_flu": from_flu, "to_flu": to_flu,
        "filed_date": c["first_seen"], "status": status, "status_raw": raw,
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
    log(f"{len(recs)} records in {time.time() - t0:.0f}s; status={dict(Counter(r['status'] for r in recs))}; "
        f"category={dict(Counter(r['category'] for r in recs))}")
