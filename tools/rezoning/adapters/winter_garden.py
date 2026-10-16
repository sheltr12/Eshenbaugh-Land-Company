#!/usr/bin/env python3
"""City of Winter Garden rezoning adapter.

Source: CivicClerk public API (wintergardenfl.api.civicclerk.com), no permitting portal with
public case records exists (BS&A Online only carries building permits).
 * Planning and Zoning Board agendas carry one item per land use case under the section
   "Annexation / Future Land Use Map Amendment / Rezoning (Public Hearing)", e.g.
   "Garden Commerce Parkway - 761 ... (Winter Garden Flex) FLU & RZ<br>Parcel ID # 24-22-27-...".
   The P&Z agenda packet (read as plain text through the API) starts each case with a cover
   sheet "PLANNING AND ZONING BOARD AGENDA ITEM # 3.A": project name, parcel IDs, owner /
   applicant, current / proposed zoning and FLU, summary, the ordinance numbers staff
   recommends, and the scheduled City Commission readings.
 * City Commission agendas list the ordinances ("Ordinance 26-07: AN ORDINANCE ... REZONING
   ... APPROXIMATELY 2.88 ACRES ... FROM PID ... TO PCD") at first and second reading.
 * Outcomes come from the published P&Z and Commission minutes ("Motion by Commissioner X to
   adopt Ordinances 26-06 and 26-07 ... carried unanimously").
The city has no case numbers in public documents, so app_num is the lowest ordinance number
of the case, e.g. "Ord. 26-06" (the case's other ordinances are listed in app_type).
Cases first appear when the P&Z agenda is posted (about two weeks before the hearing).
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

JURISDICTIONS = ["City of Winter Garden"]
JUR, COUNTY = "City of Winter Garden", "Orange"

API = "https://wintergardenfl.api.civicclerk.com/v1"
PORTAL = "https://wintergardenfl.portal.civicclerk.com"
SOURCE = "Winter Garden CivicClerk (P&Z Board + City Commission agendas, packets, minutes)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
PZ_CAT, CC_CAT = "Planning and Zoning Board", "City Commission"
PZ_BODY, CC_BODY = "Planning & Zoning Board", "City Commission"
LOOKBACK_DAYS = 540   # scan agendas this far before `since` (first appearance + hearing history)

_s = requests.Session()
_s.headers.update({"User-Agent": UA})
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"


def log(*a):
    print("[winter_garden]", *a, file=sys.stderr)


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
    raise RuntimeError(f"GET failed {url}: {last}")


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
    return [e for e in out if e.get("categoryName") in (PZ_CAT, CC_CAT)]


def walk(items, parent=None):
    for it in items or []:
        yield it, parent
        yield from walk(it.get("childItems"), it)


_TEXT_CACHE = {}


def file_text(file_id):
    """Plain text of a published meeting file (agenda packet / minutes). CivicClerk builds the
    text on request; poll its status, then fall back to extracting the PDF with pypdf."""
    if file_id in _TEXT_CACHE:
        return _TEXT_CACHE[file_id]
    txt = ""
    try:
        for i in range(8):
            st = get(f"{API}/Meetings/GetPlainTextFileStatus(fileId={file_id},retry=false)").json()
            if (st or {}).get("status") == "Completed":
                break
            time.sleep(5)
        txt = get(f"{API}/Meetings/GetMeetingFileStream(fileId={file_id},plainText=true)",
                  timeout=180).text
    except Exception as e:  # noqa: BLE001
        log(f"plain text {file_id}: {e}")
    if len(txt.strip()) < 200 and pypdf is not None:
        try:
            r = get(f"{API}/Meetings/GetMeetingFileStream(fileId={file_id},plainText=false)", timeout=300)
            rdr = pypdf.PdfReader(io.BytesIO(r.content))
            txt = "\n".join((p.extract_text() or "") for p in rdr.pages)
        except Exception as e:  # noqa: BLE001
            log(f"pdf text {file_id}: {e}")
    _TEXT_CACHE[file_id] = txt
    return txt


def file_url(file_id):
    return f"{API}/Meetings/GetMeetingFileStream(fileId={file_id},plainText=false)"


def pub_file(ev, kind):
    for f in ev.get("publishedFiles") or []:
        if f.get("type") == kind:
            return f
    return None


# ------------------------------------------------------------------ text helpers
def clean(v):
    v = html.unescape(re.sub(r"<br\s*/?>", " | ", v or "", flags=re.I))
    v = re.sub(r"<[^>]+>", " ", v).replace("\u200b", "").replace("\xa0", " ")
    v = re.sub(r"[\u2010-\u2015]", "-", v)
    v = re.sub(r"\s+", " ", v).strip(" .,;|")
    return v or None


def norm_ord(a, b):
    return f"{int(a):02d}-{int(b):02d}"


ORD_RE = re.compile(r"(?<![\d-])(\d{2})\s?-\s?(\d{2,3})(?![\d-])")


def ords_in(text):
    text = re.sub(r"[\u2010-\u2015]", "-", text or "")
    return [norm_ord(a, b) for a, b in ORD_RE.findall(text or "")]


def iso(month, day, year):
    try:
        return dt.datetime.strptime(f"{month} {int(day)} {year}", "%B %d %Y").date().isoformat()
    except ValueError:
        return None


def acres_in(text):
    m = re.search(r"([\d,]*\.?\d+)\s*(?:\u00b1|\+/-|\+/\u2013|\+-)?\s*(?:gross\s+)?acres?\b", text or "", re.I)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            return None
    return None


PARCEL_TOK = re.compile(r"(?<![\d-])(\d{2}-\d{2}-\d{2}-\d{4}-\d{2}-\d{3})(?![\d])|(?<![\d-])-\s?((?:\d{2}-)?\d{3})(?![\d-])")


def parcels_in(text):
    """Orange County parcel IDs (dd-dd-dd-dddd-dd-ddd); expands the shorthand used on agendas,
    e.g. '13-22-27-0895-00-070; -080; and -120' or '21-22-27-6100-16-000, -16-007'."""
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


# ------------------------------------------------------------------ P&Z
PZ_SCOPE = re.compile(r"\b(FLU|FUTURE LAND USE|REZONING|REZONE|RZ|PUD|PCD|PID|UVPUD|CAPUD|PRD|PND)\b", re.I)
PZ_SKIP = re.compile(r"VARIANCE|SPECIAL EXCEPTION|PLAT\b|LOT SPLIT|SITE PLAN|Code Amendment|"
                     r"^\s*Ordinance\s+\d|Comprehensive Plan 20|Minutes", re.I)

COVER_SPLIT = re.compile(r"PLANNING\s+(?:AND|&)\s+ZONING\s+BOARD\s+AGENDA\s+ITEM\s*#\s*(\d+)\s*\.?\s*([A-Z])\b")
COVER_LABELS = [
    ("date", r"Date"), ("subject", r"Subject"), ("project", r"Project Name"), ("parcel", r"Parcel ID"),
    ("issue", r"Issue"), ("supp", r"Supplemental Material\s*/\s*Analysis"),
    ("owner", r"Owner\s*/\s*Applicant|Owner|Applicant"), ("cur_zoning", r"Current Zoning"),
    ("prop_zoning", r"Proposed Zoning"), ("cur_flu", r"Current FLU|Current Future Land Use"),
    ("prop_flu", r"Proposed FLU|Proposed Future Land Use"), ("summary", r"Summary"),
    ("staff", r"Staff Recommendation\(?s?\)?"), ("next", r"Next Step\(?s?\)?"),
    ("attach", r"Attachment\(?s?\)?"),
]
_LAB = re.compile(r"^\s*(" + "|".join(p for _, p in COVER_LABELS) + r")\s*:\s*(.*)$", re.I)


def parse_covers(text):
    """{'3.A': {label: value}} from a P&Z agenda packet's plain text."""
    out = {}
    ms = list(COVER_SPLIT.finditer(text or ""))
    for i, m in enumerate(ms):
        key = f"{int(m.group(1))}.{m.group(2)}"
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        chunk = text[m.end():min(end, m.end() + 8000)]
        cut = re.search(r"\n\s*Attachment\(?s?\)?\s*:", chunk, re.I)
        if cut:
            chunk = chunk[:cut.start()]
        d, cur = {}, None
        for line in chunk.splitlines():
            # 'Date: September 4, 2026   Meeting Date: September 14, 2026'
            if re.match(r"^\s*Date\s*:", line, re.I):
                md = re.search(r"Meeting Date\s*:\s*(.+)$", line, re.I)
                if md:
                    d["meeting_date"] = [md.group(1).strip()]
                d["date"] = [re.sub(r"Meeting Date.*$", "", line.split(":", 1)[1]).strip()]
                cur = None
                continue
            lm = _LAB.match(line)
            if lm:
                lab = lm.group(1)
                cur = next(k for k, p in COVER_LABELS if re.fullmatch(p, lab, re.I))
                d.setdefault(cur, [])
                if lm.group(2).strip():
                    d[cur].append(lm.group(2).strip())
            elif cur and line.strip():
                d[cur].append(line.strip())
        out[key] = {k: re.sub(r"[\u2010-\u2015]", "-", re.sub(r"\s+", " ", " ".join(v))).strip()
                    for k, v in d.items() if v}
    return out


# ------------------------------------------------------------------ Commission ordinances
ORD_ITEM = re.compile(r"^\s*ORDINANCE\s+(\d{2})\s*-\s*(\d{2,3})\s*:?\s*(.*)$", re.I | re.S)


def parse_ord_title(title):
    """Classify a Commission ordinance title. Returns None for non-land-use ordinances."""
    m = ORD_ITEM.match(title or "")
    if not m:
        return None
    no, body = norm_ord(m.group(1), m.group(2)), m.group(3)
    up = body.upper()
    if "ANNEXATION" in up and "REZONING" not in up and "FUTURE LAND USE MAP" not in up:
        kind = "annex"
    elif "FUTURE LAND USE MAP OF THE" in up and "MAP SERIES" not in up:
        kind = "flu"
    elif re.search(r"\bREZONING\b|\bBY REZONING\b", up) or re.search(r"AMENDING .{0,80}\bP[A-Z]?UD\b.{0,80}REZON", up):
        kind = "rz"
    else:
        return None
    if re.search(r"CHAPTER \d+|CODE OF ORDINANCES|VACATING", up) and kind != "flu":
        return None
    acres = [float(x.replace(",", "")) for x in
             re.findall(r"([\d,]*\.?\d+)\s*(?:\u00b1|\+/-|\+/\u2013)?\s*ACRES?\b", up)]
    loc = re.search(r"LOCATED AT (.+?)(?:,?\s+(?:GENERALLY|NORTH OF|SOUTH OF|EAST OF|WEST OF|ON THE|AT THE)\b|;| FROM | INTO |$)", up)
    loc2 = re.search(r"GENERALLY LOCATED (.+?)(?:;| FROM | INTO |,\s*AT\s|$)", up)
    frm = to = None
    fm = re.search(r"\bFROM\s+(.+?)\s+TO\s+(.+?)(?:;|\s+AS SET FORTH|\s+PROVIDING|,\s*PROVIDING|$)", up)
    if fm:
        frm, to = fm.group(1).strip(" ,"), fm.group(2).strip(" ,")
    name = re.search(r"DESCRIBING THE DEVELOPMENT AS(?: THE)? (.+?)(?:;| AND PROVIDING|,)", up)
    return {"ord": no, "kind": kind, "title": body.strip(), "acres": acres,
            "location": (loc.group(1) if loc else (loc2.group(1) if loc2 else None)),
            "from": frm, "to": to, "dev_name": name.group(1).title() if name else None,
            "withdrawn": bool(re.search(r"WITHDRAWN", up))}


# ------------------------------------------------------------------ minutes
MOTION_RE = re.compile(r"Motion\s+by\s+(?:Commissioner|Mayor|Board\s+Member|Vice-?\s?Chair\w*|Chair\w*|Member)\s+"
                       r"[A-Z][\w'.-]+\s+(?:to\s+)?(.{0,600}?)(?:carried|failed|passed|died)([^.]{0,60})", re.I | re.S)


def motion_result(text, body, reading=None):
    """Classify one motion. Returns a hearing result string or None."""
    t = re.sub(r"\s+", " ", text).lower()
    if "withdr" in t:
        return "Withdrawn"
    if re.search(r"\btable|\bcontinu|\bpostpon|\bdefer", t):
        return "Continued"
    deny = re.search(r"\bden(y|ied|ial)\b", t)
    if body == PZ_BODY:
        return "Recommended Denial" if deny else ("Recommended Approval" if re.search(r"approv|recommend|adopt", t) else None)
    if deny:
        return "Denied"
    if re.search(r"first reading|transmit", t) or reading == 1:
        return "Approved on First Reading" if re.search(r"approv|adopt|move|transmit", t) else None
    if re.search(r"adopt|approv", t):
        return "Approved"
    return None


def cc_minutes_outcomes(text):
    """{ord: (result, motion text)} from City Commission minutes."""
    t = re.sub(r"[\u2010-\u2015]", "-", text or "")
    t = re.sub(r"(\d{2})\s+-\s?(\d{2,3})\b", r"\1-\2", t)
    t = re.sub(r"(\d{2})-\s+(\d{2,3})\b", r"\1-\2", t)
    out = {}
    for m in MOTION_RE.finditer(t):
        mot, tail = m.group(1), m.group(2)
        if "fail" in m.group(0)[-80:].lower() and "carried" not in m.group(0)[-80:].lower():
            res = "Motion failed"
        else:
            res = None
        for o in ords_in(mot):
            out.setdefault(o, (res, mot))
    return out


def pz_minutes_outcome(text, outline, parcels, ords):
    """Find the motion for one P&Z item in the board's minutes."""
    if not text:
        return None
    pos = None
    if outline:
        n, letter = outline.split(".")
        m = re.search(rf"(?m)^\s*{n}\s*\.\s*{letter}\b\.?\s", text)
        if m:
            pos = m.end()
    if pos is None:
        for key in list(ords) + list(parcels):
            k = text.find(key)
            if k >= 0:
                pos = k
                break
    if pos is None:
        return None
    m = MOTION_RE.search(text, pos)
    if not m:
        return None
    seg = m.group(0)
    if re.search(r"\bfailed\b", seg, re.I) and not re.search(r"carried", seg, re.I):
        return "Motion failed"
    return motion_result(m.group(1), PZ_BODY)


# ------------------------------------------------------------------ fetch
def fetch(since, refresh):
    today = dt.date.today()
    refresh = [r.strip() for r in (refresh or []) if r and r.strip()]
    ref_years = [int(m.group(1)) for r in refresh for m in [re.search(r"(\d{2})-\d{2,3}", r)] if m]
    start_d = dt.date.fromisoformat(since) - dt.timedelta(days=LOOKBACK_DAYS)
    if ref_years:
        start_d = min(start_d, dt.date(2000 + min(ref_years), 1, 1) - dt.timedelta(days=60))
    end_d = today + dt.timedelta(days=120)
    events = list_events(start_d.isoformat(), end_d.isoformat())
    log(f"{len(events)} P&Z / Commission events {start_d}..{end_d}")

    pz_items = []     # P&Z case appearances
    cc_items = []     # Commission ordinance appearances
    for ev in events:
        if not ev.get("agendaId") or re.search(r"cancel", ev.get("eventName") or "", re.I):
            continue
        try:
            mt = get(f"{API}/Meetings/{ev['agendaId']}").json()
        except Exception as e:  # noqa: BLE001
            log(f"meeting {ev.get('agendaId')}: {e}")
            continue
        mdate = ev["startDateTime"][:10]
        ag = pub_file(ev, "Agenda")
        first_seen = min(mdate, (ag or {}).get("publishOn", mdate)[:10] or mdate)
        info = {"date": mdate, "first_seen": first_seen, "event": ev,
                "url": f"{PORTAL}/event/{ev['id']}/files"}
        for it, parent in walk(mt.get("items")):
            name = clean(it.get("agendaObjectItemName"))
            if not name:
                continue
            pname = clean((parent or {}).get("agendaObjectItemName")) or ""
            if ev["categoryName"] == PZ_CAT:
                if parent is None or not PZ_SCOPE.search(name) or PZ_SKIP.search(name) or PZ_SKIP.search(pname):
                    continue
                if not (re.search(r"Parcel", name, re.I) or re.search(r"Annex|Land Use|Rezon", pname, re.I)):
                    continue
                outline = f"{(parent.get('agendaObjectItemOutlineNumber') or '').strip(' .')}." \
                          f"{(it.get('agendaObjectItemOutlineNumber') or '').strip(' .')}"
                pz_items.append(dict(info, name=name, outline=outline, item_id=it.get("id")))
            else:
                po = parse_ord_title(name)
                if not po:
                    continue
                reading = 1 if re.search(r"First Reading", pname, re.I) else (
                    2 if re.search(r"Second Reading", pname, re.I) else None)
                cc_items.append(dict(info, name=name, reading=reading, **po))
    log(f"{len(pz_items)} P&Z land use items, {len(cc_items)} Commission land use ordinances")

    # P&Z cover sheets (one plain text packet per P&Z meeting with cases)
    covers = {}
    for ev_id in sorted({p["event"]["id"] for p in pz_items}):
        ev = next(p["event"] for p in pz_items if p["event"]["id"] == ev_id)
        pk = pub_file(ev, "Agenda Packet")
        if not pk:
            continue
        try:
            covers[ev_id] = parse_covers(file_text(pk["fileId"]))
        except Exception as e:  # noqa: BLE001
            log(f"packet {ev['startDateTime'][:10]}: {e}")

    # ---- build cases: P&Z items seed cases, ordinances join by number
    cases = []
    ord_case = {}
    for p in sorted(pz_items, key=lambda x: x["date"]):
        cov = covers.get(p["event"]["id"], {}).get(p["outline"], {})
        p["cover"] = cov
        ords = list(dict.fromkeys(ords_in(cov.get("staff", "")) + ords_in(
            re.sub(r".*?(Ordinances?)", r"\1", cov.get("next", "")) if "rdinance" in cov.get("next", "") else "")))
        p["ords"] = ords
        pids = parcels_in(p["name"]) or parcels_in(cov.get("parcel", ""))
        # same case on a later P&Z agenda (continued item): match by ordinance or parcels
        hit = None
        for c in cases:
            if (set(ords) & c["ords"]) or (pids and set(pids) & c["parcels"] and c["pz"]
                                           and c["pz"][-1]["date"] < p["date"]):
                hit = c
                break
        if hit is None:
            hit = {"pz": [], "cc": [], "ords": set(), "parcels": set()}
            cases.append(hit)
        hit["pz"].append(p)
        hit["ords"].update(ords)
        hit["parcels"].update(pids)
        for o in ords:
            ord_case[o] = hit
    # ordinances: attach to known cases, otherwise group by meeting + location
    for o in sorted(cc_items, key=lambda x: (x["date"], x["ord"])):
        c = ord_case.get(o["ord"])
        if c is None:
            loc = o.get("location") or ""
            for cand in cases:
                if any(x["date"] == o["date"] and loc and x.get("location") == loc for x in cand["cc"]):
                    c = cand
                    break
        if c is None:
            c = {"pz": [], "cc": [], "ords": set(), "parcels": set()}
            cases.append(c)
        c["cc"].append(o)
        c["ords"].add(o["ord"])
        ord_case[o["ord"]] = c

    out_cases = []
    for c in cases:
        kinds = {x["kind"] for x in c["cc"]}
        if c["cc"] and not c["pz"] and kinds <= {"annex"}:
            continue  # annexation only
        c["app_num"] = (f"Ord. {min(c['ords'])}" if c["ords"] else
                        f"P&Z {c['pz'][0]['date']} Item {c['pz'][0]['outline']}")
        firsts = [x["first_seen"] for x in c["pz"] + c["cc"]]
        c["first_seen"] = min(firsts)
        if c["first_seen"] < since and c["app_num"] not in refresh:
            continue
        out_cases.append(c)
    log(f"{len(cases)} cases total, {len(out_cases)} to report")

    # ---- outcomes from minutes
    minutes_cc, minutes_pz = {}, {}
    need_ev = {}
    for c in out_cases:
        for x in c["pz"] + c["cc"]:
            if x["date"] <= today.isoformat():
                need_ev[x["event"]["id"]] = x["event"]
    for ev_id, ev in need_ev.items():
        mf = pub_file(ev, "Minutes")
        if not mf:
            continue
        try:
            txt = file_text(mf["fileId"])
        except Exception as e:  # noqa: BLE001
            log(f"minutes {ev['startDateTime'][:10]}: {e}")
            continue
        if ev["categoryName"] == CC_CAT:
            minutes_cc[ev_id] = cc_minutes_outcomes(txt)
        else:
            minutes_pz[ev_id] = txt

    out = []
    for c in out_cases:
        try:
            out.append(build(c, minutes_cc, minutes_pz, today.isoformat()))
        except Exception as e:  # noqa: BLE001
            log(f"{c.get('app_num')}: skipped ({e})")
    for r in set(refresh) - {o["app_num"] for o in out}:
        log(f"refresh id {r} not found on P&Z / Commission agendas since {start_d}")
    return out


def same_flu(a, b):
    f = lambda s: re.sub(r"\b(city|county|orange county|of winter garden)\b|\(.*?\)|[^a-z ]", "",  # noqa: E731
                         (s or "").lower()).split()
    return bool(a and b) and f(a) == f(b)


def build(c, minutes_cc, minutes_pz, today):
    pz = sorted(c["pz"], key=lambda x: x["date"])
    cc = sorted(c["cc"], key=lambda x: (x["date"], x["ord"]))
    cov = {}
    for p in pz:
        for k, v in p.get("cover", {}).items():
            cov[k] = v  # latest cover wins
    kinds = {x["kind"] for x in cc}
    label = (pz[-1]["name"] if pz else "") or ""
    label_kinds = set()
    if re.search(r"\bFLU\b|FUTURE LAND USE", label, re.I):
        label_kinds.add("flu")
    if re.search(r"REZON|\bRZ\b|PUD|PCD|PID", label, re.I):
        label_kinds.add("rz")
    kinds |= label_kinds
    from_flu, to_flu = cov.get("cur_flu"), cov.get("prop_flu")
    flu_ord = next((x for x in cc if x["kind"] == "flu"), None)
    rz_ord = next((x for x in reversed(cc) if x["kind"] == "rz"), None)
    if flu_ord and not (from_flu and to_flu) and flu_ord.get("from"):
        from_flu, to_flu = flu_ord["from"].title(), flu_ord["to"].title()
    from_z, to_z = cov.get("cur_zoning"), cov.get("prop_zoning")
    if rz_ord and not (from_z and to_z) and rz_ord.get("from"):
        from_z, to_z = rz_ord["from"].title(), rz_ord["to"].title()
    real_flu = "flu" in kinds and not same_flu(from_flu, to_flu)
    pd_text = " ".join(filter(None, [to_z, label, rz_ord["title"] if rz_ord else None]))
    is_pd = bool(re.search(r"\b(?:UV)?P[A-Z]?UD\b|\bPCD\b|\bPID\b|\bPRD\b|PLANNED (?:UNIT|COMMERCIAL|INDUSTRIAL|RESIDENTIAL)",
                           pd_text, re.I))
    if real_flu or ("flu" in kinds and "rz" not in kinds):
        category = "Comp Plan Amendment"
    elif is_pd:
        category = "PD"
    else:
        category = "Rezoning"

    # ---- hearings
    hmap = {}
    for p in pz:
        res = None
        txt = minutes_pz.get(p["event"]["id"])
        if txt:
            res = pz_minutes_outcome(txt, p["outline"], parcels_in(p["name"]), sorted(c["ords"]))
        if re.search(r"WITHDRAWN", p["name"], re.I):
            res = "Withdrawn"
        hmap[(PZ_BODY, p["date"])] = {"body": PZ_BODY, "date": p["date"], "result": res,
                                     "_minutes": txt is not None, "_reading": None}
    for x in cc:
        k = (CC_BODY, x["date"])
        mins = minutes_cc.get(x["event"]["id"])
        res = None
        if mins is not None:
            hit = mins.get(x["ord"])
            if hit:
                res = hit[0] or motion_result(hit[1], CC_BODY, x["reading"])
        if x["withdrawn"]:
            res = "Withdrawn"
        h = hmap.setdefault(k, {"body": CC_BODY, "date": x["date"], "result": None,
                                "_minutes": mins is not None, "_reading": x["reading"]})
        if res and (h["result"] is None or x["kind"] != "annex"):
            h["result"] = res
    # readings announced on the P&Z cover sheet ("first reading ... September 15, 2026")
    nxt = cov.get("next", "")
    for m in re.finditer(rf"({MONTHS})\s+(\d{{1,2}}),\s*(\d{{4}})", nxt):
        d = iso(*m.groups())
        if d and d > today and (CC_BODY, d) not in hmap:
            hmap[(CC_BODY, d)] = {"body": CC_BODY, "date": d, "result": None, "_minutes": False,
                                  "_reading": None}
    hearings = sorted(hmap.values(), key=lambda h: (h["date"], h["body"] != PZ_BODY))

    # ---- status
    status, raw, decision = None, None, None
    past = [h for h in hearings if h["date"] <= today]
    future = [h for h in hearings if h["date"] > today]
    final = [h for h in past if h["body"] == CC_BODY and h["result"] in ("Approved", "Denied")]
    if any(h["result"] == "Withdrawn" for h in hearings):
        status, raw = "Withdrawn", "Withdrawn (agenda / minutes)"
    elif final:
        status, decision = final[-1]["result"], final[-1]["date"]
        raw = f"City Commission {decision}: {final[-1]['result']} (minutes)"
    elif past and past[-1]["result"] == "Continued":
        status = "Continued"
        raw = f"{past[-1]['body']} {past[-1]['date']}: tabled/continued" + (
            f"; next {future[0]['body']} {future[0]['date']}" if future else "")
    elif future:
        status = "Hearing Scheduled"
        raw = f"next {future[0]['body']} {future[0]['date']}"
        if past and past[-1]["result"]:
            raw = f"{past[-1]['body']} {past[-1]['date']}: {past[-1]['result']}; " + raw
    elif past and past[-1]["result"] is None:
        status = "Hearing Scheduled"
        raw = f"{past[-1]['body']} hearing {past[-1]['date']} held; outcome not yet in published minutes"
    elif past:
        status, raw = "Filed", f"{past[-1]['body']} {past[-1]['date']}: {past[-1]['result']}; " \
                               f"Commission hearing not yet scheduled"
    else:
        status, raw = "Filed", "On agenda"

    # ---- fields
    parcels = parcels_in(cov.get("parcel", ""))
    if not parcels:
        for p in pz:
            parcels += parcels_in(p["name"])
    parcels = list(dict.fromkeys(parcels))
    acres = None
    if rz_ord and rz_ord["acres"]:
        acres = round(sum(rz_ord["acres"]), 2)
    elif flu_ord and flu_ord["acres"]:
        acres = round(sum(flu_ord["acres"]), 2)
    else:
        acres = acres_in(cov.get("issue")) or acres_in(cov.get("summary"))
    pname = cov.get("project")
    if pname:
        pname = re.sub(r"\s+(AZFLU|AFLURZ|FLU\s*&\s*RZ|RZ|PCD RZ|PUD RZ)$", "", pname).strip() or None
    if not pname and pz:
        m = re.search(r"\(([^)]+)\)", pz[-1]["name"])
        pname = m.group(1) if m else None
    if not pname:
        pname = next((x["dev_name"] for x in cc if x.get("dev_name")), None)
    address = cov.get("subject")
    if not address and pz:
        address = re.split(r"\(|\|", pz[-1]["name"])[0].strip(" -") or None
    if not address:
        address = next((x["location"].title() for x in cc if x.get("location")), None)
    desc = cov.get("issue") or cov.get("summary")
    if not desc and cc:
        desc = (rz_ord or flu_ord or cc[0])["title"].capitalize()
    if desc and len(desc) > 300:
        desc = desc[:297] + "..."
    ords = sorted(c["ords"])
    labels = {"annex": "Annexation", "flu": "FLU Map Amendment", "rz": "Rezoning"}
    kl = [labels[k] for k in ("annex", "flu", "rz") if k in kinds or (k == "annex" and re.search(r"ANNEX", label, re.I))]
    app_type = " / ".join(kl) or "Land use case"
    if is_pd and "rz" in kinds:
        app_type = app_type.replace("Rezoning", "PD Rezoning")
    if ords:
        app_type += f" (Ord. {', '.join(ords)})"
    pz_last = pz[-1] if pz else None
    latest = (cc[-1] if cc else pz_last)
    docs = None
    if pz_last:
        pk = pub_file(pz_last["event"], "Agenda Packet")
        docs = file_url(pk["fileId"]) if pk else None
    if not docs and cc:
        pk = pub_file(cc[-1]["event"], "Agenda Packet")
        docs = file_url(pk["fileId"]) if pk else None
    return {
        "jurisdiction": JUR, "county": COUNTY, "app_num": c["app_num"], "category": category,
        "app_type": app_type, "project_name": pname, "description": desc,
        "applicant": cov.get("owner"), "parcel_ids": parcels, "address": address, "acres": acres,
        "from_zoning": from_z, "to_zoning": to_z,
        "from_flu": from_flu if "flu" in kinds else None, "to_flu": to_flu if "flu" in kinds else None,
        "filed_date": c["first_seen"], "status": status, "status_raw": raw,
        "hearings": [{"body": h["body"], "date": h["date"], "result": h["result"]} for h in hearings],
        "decision_date": decision, "url": latest["url"], "docs_url": docs,
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
