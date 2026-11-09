#!/usr/bin/env python3
"""City of Dade City (Pasco County) rezoning / PD / future land use map amendment adapter.

Source: the city's iCompass CivicWeb portal (dadecityfl.civicweb.net), Planning Board (meeting type 19)
and City Commission (type 10) meetings. Each meeting has a published HTML agenda and, after
approval, HTML minutes, served as JSON by /Services/MeetingsService.svc/meetings/<id>/meetingDocuments.
 * Dade City handles every land use case as a numbered ordinance; there is no separate application
   number on the agendas. app_num = "Ordinance 2025-20". Companion requests are separate ordinances
   (e.g. Meridian Avenue: 2025-20 small-scale FLUM amendment, 2025-19 rezoning), like the city does.
 * In scope: ordinances amending the Official Zoning Map, PD rezonings / substantial PD modifications,
   and small / large scale comprehensive plan amendments to the Future Land Use Map. Skipped:
   annexation, CDD, Land Development Regulation text, overlays without a map change, moratoria.
 * Hearings: Planning Board (local planning agency, advisory) then City Commission first reading
   ("Introduction") and second reading ("Adoption"). Outcomes come from the HTML minutes ("Moved by
   ... to approve on Second Reading Ordinance 2025-20 ... CARRIED") and from "Continued to a Date
   Certain" notes on the agenda.
 * Details: acreage and from/to designations from the ordinance title, Pasco parcel numbers and site
   address from the attached staff analysis memo (first 2 pages).
Requires pypdf.
"""
import datetime as dt
import html as _html
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

JURISDICTIONS = ["City of Dade City"]
JUR = "City of Dade City"
COUNTY = "Pasco"
BASE = "https://dadecityfl.civicweb.net"
SOURCE = "Dade City CivicWeb (Planning Board + City Commission agendas and minutes)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
LOOKBACK_DAYS = 150
TYPES = {19: "Planning Board", 10: "City Commission", 24: "Planning Board"}
PB, CC = "Planning Board", "City Commission"
SKIP_TITLE = re.compile(r"CANCEL|Workshop|Budget|Charrette|Open House", re.I)
MAX_PDF = 8_000_000

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


def log(*a):
    print("[dade_city]", *a, file=sys.stderr)


def get(url, params=None, tries=4, sleep=0.4, **kw):
    last = None
    for i in range(tries):
        try:
            r = _s.get(url, params=params, timeout=90, **kw)
            r.raise_for_status()
            time.sleep(sleep)
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


def sq(s):
    return re.sub(r"\s+", " ", s or "").strip()


def text_of(h):
    h = re.sub(r"<style.*?</style>|<script.*?</script>", " ", h or "", flags=re.S)
    t = _html.unescape(re.sub(r"<[^>]+>", " ", h)).replace("\u00a0", " ")
    return sq(t.replace("\u2013", "-").replace("\u2014", "-").replace("\u2011", "-"))


# ------------------------------------------------------------------ CivicWeb
def list_meetings():
    out = {}
    for ty, body in TYPES.items():
        try:
            h = get(f"{BASE}/Portal/MeetingInformation.aspx", params={"type": ty}).text
        except Exception as e:  # noqa: BLE001
            if ty == 24:
                continue
            raise
        for mid, date, title in re.findall(
                r'meetingClick\(this, (\d+),\d+,\d+\);return false;"[^>]*><div class="meeting-list-item-button-date">'
                r'([^<]+)</div><div[^>]*title="([^"]*)"', h):
            try:
                d = dt.datetime.strptime(date.strip(), "%d %b %Y").date().isoformat()
            except ValueError:
                continue
            out[mid] = {"id": mid, "date": d, "title": _html.unescape(title), "body": body}
    return list(out.values())


def meeting_docs(mid):
    return get(f"{BASE}/Services/MeetingsService.svc/meetings/{mid}/meetingDocuments").json()


SEC_RE = re.compile(r'font-weight:bold">([A-Z])\.</span>.{0,700}?font-weight:bold">([^<]{3,160})</span>', re.S)
ANCHOR_RE = re.compile(r'name="AgendaItem(\d+)"')


def parse_items(h):
    """[{id, section, text, links:[(label, url)]}] from a CivicWeb HTML agenda / minutes."""
    marks = [(m.start(), "item", m.group(1)) for m in ANCHOR_RE.finditer(h)]
    marks += [(m.start(), "sec", _html.unescape(m.group(2))) for m in SEC_RE.finditer(h)]
    marks.sort()
    items, section = [], ""
    for i, (pos, kind, val) in enumerate(marks):
        if kind == "sec":
            section = val
            continue
        nxt = next((p for p, k, _ in marks[i + 1:] if k == "item"), len(h))
        chunk = h[h.find(">", pos) + 1:nxt]
        # do not let a following section header leak into this item
        sm = SEC_RE.search(chunk)
        if sm:
            chunk = chunk[:sm.start()]
        links = [(text_of(t), _html.unescape(u)) for u, t in re.findall(r'href="(/document/[^"]+)"[^>]*>(.*?)</a>', chunk, re.S)]
        items.append({"id": val, "section": section, "text": text_of(chunk), "links": links})
    return items


# ------------------------------------------------------------------ classification
ORD_RE = re.compile(r"\b(?:RESTATED\s+)?ORDI\w*\s*(?:NO\.?\s*:?\s*)?(\d{4})\s*-\s*(\d{1,3})\b", re.I)
RZ_RE = re.compile(r"OFFICIAL ZONING MAP|ZONING DISTRICT ON|REZON|ZONING AMENDMENT|PLANNED DEVELOPMENT|\bPD-[A-Z]\b|"
                   r"SUBSTANTIALLY MODIFY", re.I)
CPA_RE = re.compile(r"FUTURE LAND USE MAP|SMALL[- ]SCALE COMPREHENSIVE PLAN AMENDMENT|LARGE[- ]SCALE COMPREHENSIVE PLAN AMENDMENT|"
                    r"\bFLUM\b", re.I)
OUT_RE = re.compile(r"(?:AMENDING|UPDATING)[^;]{0,40}LAND DEVELOPMENT REGULATIONS|MORATORIUM|COMMUNITY DEVELOPMENT DISTRICT|"
                    r"CHAPTER 70|VACATING|ABANDONING|CREATING A [A-Z ]*OVERLAY DISTRICT", re.I)
PIN_RE = re.compile(r"\b(\d{2})\s?-\s?(\d{2})\s?-\s?(\d{2})\s?-\s?([0-9A-Z]{4})\s?-\s?([0-9A-Z]{5})\s?-\s?([0-9A-Z]{4})\b")


def classify(text):
    """(ordinance no, kind 'RZ'|'CPA') for an in-scope agenda item, else None."""
    head = text[:1200]
    m = ORD_RE.search(head)
    if not m:
        return None
    if OUT_RE.search(head):
        return None
    if CPA_RE.search(head):
        kind = "CPA"
    elif RZ_RE.search(head):
        kind = "RZ"
    else:
        return None
    if re.search(r"\bANNEXING\b", head[:400]) and not re.search(r"ZONING MAP|LAND USE MAP", head[:600]):
        return None
    return f"{m.group(1)}-{int(m.group(2)):02d}", kind


def pins_in(text):
    return list(dict.fromkeys("-".join(g) for g in PIN_RE.findall(text or "")))


def reading_of(text, section):
    t = (text[:300] + " | " + (section or "")).lower()
    head = text[:300].lower()
    if re.search(r"transmittal", head):
        return "transmittal"
    if re.search(r"\badoption\b|second reading|2nd reading|final reading", head):
        return "second"
    if re.search(r"\bintroduction\b|first reading|1st reading", head):
        return "first"
    if re.search(r"adoption|second reading", t):
        return "second"
    if re.search(r"introduction|first reading", t):
        return "first"
    return None


def minutes_items(h):
    """Minutes HTML has no item anchors: split its text on the item numbers ('H.1.')."""
    t = text_of(h)
    parts = re.split(r"(?=(?<![\w.])[A-Z]\.\d{1,2}\.\s)", t)
    return [{"text": p} for p in parts if p.strip()]


def minutes_result(items, ono):
    """Outcome for an ordinance from the parsed minutes items of one meeting."""
    y, n = ono.split("-")
    pat = re.compile(rf"ORDI\w*\s*(?:NO\.?\s*:?\s*)?{y}\s*-\s*0*{int(n)}\b", re.I)
    for it in items:
        t = it["text"]
        if not pat.search(t[:1500]):
            continue
        if re.search(r"\bwithdr[ae]wn?\b", t, re.I) and not re.search(r"CARRIED", t):
            return "Withdrawn"
        res = None
        for mm in re.finditer(r"Moved by(.{0,600}?)(CARRIED|FAILED|DIED|Motion (?:was )?(?:approved|passe[sd]|carried|failed|denied)[^.]{0,40})",
                              t, re.I | re.S):
            what, outcome = mm.group(1).lower(), mm.group(2).lower()
            failed = bool(re.search(r"fail|died|denied", outcome))
            if re.search(r"continu|table|postpone|defer", what):
                r = None if failed else "Continued"
            elif re.search(r"\bden(y|ial)", what):
                r = None if failed else "Denied"
            else:
                r = "Denied" if failed else "Approved"
            res = r or res
        if res:
            return res
    return None


# ------------------------------------------------------------------ PDFs
_pdf = {}


def pdf_text(url, max_pages=2):
    if pypdf is None:
        return ""
    if url in _pdf:
        return _pdf[url]
    txt = ""
    try:
        r = get(BASE + url if url.startswith("/") else url)
        if r.content[:4] == b"%PDF" and len(r.content) <= MAX_PDF:
            rdr = pypdf.PdfReader(io.BytesIO(r.content))
            txt = "\n".join((p.extract_text() or "") for p in rdr.pages[:max_pages])
    except Exception as e:  # noqa: BLE001
        log(f"pdf {url[:80]}: {e}")
    _pdf[url] = txt
    return txt


# ------------------------------------------------------------------ fetch
def fetch(since, refresh):
    today = dt.date.today()
    refresh_ids = {}
    for r in refresh or []:
        m = re.search(r"(\d{4})\s*-\s*(\d{1,3})", r or "")
        if m:
            refresh_ids[f"{m.group(1)}-{int(m.group(2)):02d}"] = r
    start_d = dt.date.fromisoformat(since) - dt.timedelta(days=LOOKBACK_DAYS)
    if refresh_ids:
        yr = min(int(k[:4]) for k in refresh_ids)
        start_d = min(start_d, max(dt.date(yr - 1, 9, 1), today - dt.timedelta(days=730)))
    end_d = today + dt.timedelta(days=75)
    try:
        meetings = list_meetings()
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Dade City CivicWeb unreachable: {e}")
    meetings = [m for m in meetings if start_d.isoformat() <= m["date"] <= end_d.isoformat()
                and not SKIP_TITLE.search(m["title"])]
    meetings.sort(key=lambda m: m["date"])
    log(f"{len(meetings)} Planning Board / City Commission meetings {start_d}..{end_d}")

    cases = {}
    for mt in meetings:
        try:
            docs = meeting_docs(mt["id"])
        except Exception as e:  # noqa: BLE001
            log(f"meeting {mt['id']}: {e}")
            continue
        agenda = next((d for d in docs if d.get("DocumentType") == 1 and d.get("Html")), None)
        minutes = next((d for d in docs if d.get("DocumentType") == 9 and d.get("Html")), None)
        pdf = next((d for d in docs if d.get("DocumentType") == 4), None)
        if not agenda:
            continue
        items = parse_items(agenda["Html"])
        min_items = minutes_items(minutes["Html"]) if minutes else []
        for it in items:
            c = classify(it["text"])
            if not c:
                continue
            ono, kind = c
            rec = cases.setdefault(ono, {"kind": kind, "apps": []})
            if kind == "CPA":
                rec["kind"] = "CPA"
            res = None
            if mt["date"] <= today.isoformat():
                res = minutes_result(min_items, ono) if min_items else None
                if res is None and re.search(r"\bContinued\b", it["text"][:400], re.I):
                    res = "Continued"
            rec["apps"].append({
                "date": mt["date"], "body": mt["body"], "text": it["text"], "links": it["links"], "kind": kind,
                "reading": reading_of(it["text"], it["section"]) if mt["body"] == CC else None,
                "result": res, "url": f"{BASE}/Portal/MeetingInformation.aspx?Id={mt['id']}",
                "pdf": (f"{BASE}/document/{pdf['Id']}" if pdf else None), "has_minutes": bool(minutes)})
    log(f"{len(cases)} in-scope ordinances")

    out = []
    for ono, c in sorted(cases.items()):
        # an agenda item that carries the wrong ordinance number (e.g. the companion rezoning listed
        # under the FLUM ordinance's number) is dropped when its kind disagrees with the others
        kinds = [a["kind"] for a in c["apps"]]
        main = max(set(kinds), key=kinds.count) if len(set(kinds)) > 1 else kinds[0]
        if len(set(kinds)) > 1 and kinds.count(main) == kinds.count([k for k in set(kinds) if k != main][0]):
            main = c["kind"]
        c["kind"] = main
        apps = sorted([a for a in c["apps"] if a["kind"] == main], key=lambda a: (a["date"], a["body"] != PB))
        first = apps[0]
        older = first["body"] == CC and first["reading"] == "second" and \
            first["date"] < (start_d + dt.timedelta(days=45)).isoformat()
        if (first["date"] < since or older) and ono not in refresh_ids:
            continue
        try:
            out.append(build(ono, c["kind"], apps, today.isoformat()))
        except Exception as e:  # noqa: BLE001
            log(f"Ordinance {ono}: skipped ({e})")
    for r in set(refresh_ids) - set(cases):
        log(f"refresh id {refresh_ids[r]} not on any agenda since {start_d}")
    return out


def from_to(text, kind):
    t = sq(text)
    if kind == "CPA":
        pats = [r"FUTURE LAND USE MAP[^;]{0,400}? FROM (.+?) (?:DESIGNATION )?TO (.+?)(?: DESIGNATION)?(?:;|,? PROVIDING|$)"]
    else:
        pats = [r"(?:ZONING MAP|ZONING DISTRICT)[^;]{0,400}? FROM (.+?) (?:DISTRICT )?TO (.+?)(?: DISTRICT)?(?:,? WITH THE|,? IN ACCORDANCE|;|,? PROVIDING|$)"]
    for p in pats:
        m = re.search(p, t, re.I)
        if m:
            f_, t_ = sq(m.group(1)).strip(" ,.;"), sq(m.group(2)).strip(" ,.;")
            if 0 < len(f_) < 200 and 0 < len(t_) < 200:
                return f_, t_
    return None, None


def build(ono, kind, apps, today):
    texts = [a["text"] for a in apps]
    title = max(texts, key=lambda t: len(t[:1500]))
    acres = None
    m = re.search(r"(?:APPROXIMATELY|CONTAINING)\s*±?\s*([\d,]*\.?\d+)\s*(?:\(MOL\)\s*)?ACRES", title, re.I) or \
        re.search(r"±\s*([\d,]*\.?\d+)\s*ACRES", title, re.I)
    if m:
        try:
            acres = float(m.group(1).replace(",", ""))
        except ValueError:
            acres = None
    f_, t_ = from_to(title, kind)
    # staff analysis memo / ordinance PDF for parcels, address and applicant
    links = []
    for a in reversed(apps):
        for lab, u in a["links"]:
            if u not in [x[1] for x in links]:
                links.append((lab, u))
    staff = [x for x in links if re.search(r"Staff|Analysis", x[0], re.I) and ".pdf" in x[1].lower()]
    ordpdf = [x for x in links if re.search(r"Ordinance", x[0], re.I) and ".pdf" in x[1].lower()
              and not re.search(r"Memo", x[0], re.I)]
    pins, stxt = pins_in(title), ""
    for lab, u in staff + ordpdf:
        t = pdf_text(u, 2)
        if not t:
            continue
        stxt = stxt or t
        pins = pins or pins_in(t)
        if pins:
            break
    address = None
    m = re.search(r"\n\s*(\d{2,6} [A-Z0-9 .]+(?:AVENUE|AVE|STREET|ST|ROAD|RD|DRIVE|DR|BOULEVARD|BLVD|HIGHWAY|HWY|LANE|LN|WAY|"
                  r"COURT|CT|PLACE|PL|TRAIL|PARKWAY|PKWY)\b\.?)\s*\n", stxt or "")
    if m:
        address = sq(m.group(1))
    if not address:
        m = re.search(r"GENERALLY LOCATED (?:ON |AT |ALONG )?(.+?)(?:,? AND MORE SPECIFICALLY|;|,? FROM |$)", title, re.I)
        address = sq(m.group(1))[:150] if m else None
    applicant = None
    m = re.search(r"Applicant(?:\s*/\s*Owner)?\s*:\s*([^\n]{3,80})", stxt or "")
    if m:
        applicant = sq(m.group(1))
    # project name: heading text before "ORDINANCE"
    pname = None
    for t in texts:
        h = re.split(r"\bORDI\w*\s*(?:NO\.?\s*:?\s*)?\d{4}", t, maxsplit=1, flags=re.I)[0]
        h = re.sub(r"^\s*[A-Z]\.\d{1,2}\.\s*", "", h)
        h = re.sub(r"\((?:Legislative|Quasi-?Judicial|Item Continued|Continued[^)]*)\)|-\s*(?:Legislative|Quasi-?Judicial)\b|Introduction of|Adoption of|"
                   r"Large Scale Comprehensive Plan Amendment|Small Scale Comprehensive Plan Amendment|Zoning Amendment|"
                   r"Annexation Petition|Rezone|Rezoning|FLUM|-\s*Continued to a Date Certain[^-]*", " ", h, flags=re.I)
        h = sq(h).strip(" -:,")
        if 2 < len(h) < 80:
            pname = h
            break
    is_pd = kind == "RZ" and bool(re.search(r"PLANNED DEVELOPMENT|\bPD-[A-Z]\b|\bPD\b|SUBSTANTIALLY MODIFY", (t_ or "") + " " + title[:700], re.I))
    category = "Comp Plan Amendment" if kind == "CPA" else ("PD" if is_pd else "Rezoning")
    if kind == "CPA":
        app_type = "Large Scale Comprehensive Plan Amendment (FLUM)" if re.search(r"LARGE[- ]SCALE", title, re.I) else \
            "Small Scale Comprehensive Plan Amendment (FLUM)"
    elif re.search(r"SUBSTANTIALLY MODIFY", title, re.I):
        app_type = "PD Substantial Modification"
    else:
        app_type = "Zoning Map Amendment" + (" (PD)" if is_pd else "")
    m = re.search(r"AN ORDINANCE OF .*", title, re.I)
    desc = sq(m.group(0) if m else title)
    desc = desc[:297] + "..." if len(desc) > 300 else desc

    hearings = []
    for a in apps:
        res, rd = a["result"], a["reading"]
        if a["body"] == PB and res in ("Approved", "Denied"):
            res = "Recommended " + ("Approval" if res == "Approved" else "Denial")
        body = PB if a["body"] == PB else (f"City Commission ({rd} reading)" if rd else CC)
        h = {"body": body, "date": a["date"], "result": res, "_rd": rd, "_body": a["body"]}
        prev = next((x for x in hearings if x["body"] == h["body"] and x["date"] == h["date"]), None)
        if prev:
            prev["result"] = prev["result"] or h["result"]
        else:
            hearings.append(h)
    hearings.sort(key=lambda h: (h["date"], h["_body"] != PB))
    ccs = [h for h in hearings if h["_body"] == CC]
    rank = {"first": 1, "transmittal": 1, "second": 2, None: 0}
    for i, h in enumerate(ccs):
        later = [x for x in ccs[i + 1:] if x["date"] > h["date"]]
        if h["result"] is not None or not later or h["date"] > today:
            continue
        if h["_rd"] and any(x["_rd"] == h["_rd"] for x in later):
            h["result"] = "Continued"
        elif any(rank.get(x["_rd"], 0) > rank.get(h["_rd"], 0) for x in later):
            h["result"] = "Approved" if h["_rd"] in ("first", "transmittal") else None
    for h in hearings:
        if h["_body"] == CC and h["_rd"] in ("first", "transmittal") and h["result"] == "Approved":
            h["result"] = "Approved on first reading" if h["_rd"] == "first" else "Transmitted"
    status, raw, decision = status_from(hearings, today)
    docs = next((u for lab, u in links if re.search(r"Staff|Analysis", lab, re.I)), None)
    return {
        "jurisdiction": JUR, "county": COUNTY, "app_num": f"Ordinance {ono}", "category": category,
        "app_type": app_type, "project_name": pname, "description": desc or None, "applicant": applicant,
        "parcel_ids": pins, "address": address, "acres": acres,
        "from_zoning": f_ if kind == "RZ" else None, "to_zoning": t_ if kind == "RZ" else None,
        "from_flu": f_ if kind == "CPA" else None, "to_flu": t_ if kind == "CPA" else None,
        "filed_date": apps[0]["date"], "status": status, "status_raw": raw,
        "hearings": [{"body": h["body"], "date": h["date"], "result": h["result"]} for h in hearings],
        "decision_date": decision, "url": apps[-1]["url"],
        "docs_url": (BASE + docs) if docs else apps[-1]["pdf"],
        "lat": None, "lon": None, "source": SOURCE,
    }


def status_from(hearings, today):
    past = [h for h in hearings if h["date"] <= today]
    future = [h for h in hearings if h["date"] > today]
    if any(h["result"] == "Withdrawn" for h in past):
        h = [x for x in past if x["result"] == "Withdrawn"][-1]
        return "Withdrawn", f"{h['body']} {h['date']}: withdrawn", None
    den = [h for h in past if h["_body"] == CC and h["result"] == "Denied"]
    if den:
        return "Denied", f"{den[-1]['body']} {den[-1]['date']}: denied", den[-1]["date"]
    fin = [h for h in past if h["_body"] == CC and h["result"] == "Approved" and h["_rd"] not in ("first", "transmittal")]
    if fin:
        return "Approved", f"{fin[-1]['body']} {fin[-1]['date']}: approved (minutes)", fin[-1]["date"]
    if future:
        raw = f"next {future[0]['body']} {future[0]['date']}"
        if past and past[-1]["result"]:
            raw = f"{past[-1]['body']} {past[-1]['date']}: {past[-1]['result']}; " + raw
        return "Hearing Scheduled", raw, None
    if past and past[-1]["result"] == "Continued":
        return "Continued", f"{past[-1]['body']} {past[-1]['date']}: continued", None
    if past:
        lp = past[-1]
        if lp["result"]:
            return "Filed", f"{lp['body']} {lp['date']}: {lp['result']}; next hearing not yet on an agenda", None
        return "Hearing Scheduled", f"{lp['body']} {lp['date']} held; minutes not yet posted", None
    return "Filed", "On agenda", None


if __name__ == "__main__":
    since = sys.argv[1] if len(sys.argv) > 1 else (dt.date.today() - dt.timedelta(days=14)).isoformat()
    refresh = sys.argv[2].split(",") if len(sys.argv) > 2 else []
    t0 = time.time()
    recs = fetch(since, refresh)
    print(json.dumps(recs, indent=2, ensure_ascii=False))
    from collections import Counter
    log(f"{len(recs)} records in {time.time() - t0:.0f}s; status={dict(Counter(r['status'] for r in recs))}; "
        f"category={dict(Counter(r['category'] for r in recs))}")
