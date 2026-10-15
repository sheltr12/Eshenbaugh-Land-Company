#!/usr/bin/env python3
"""City of Safety Harbor (Pinellas) rezonings / future land use amendments - best-effort
agenda scraper.

Safety Harbor has no public machine-readable case list (its BS&A portal needs a login), so this
adapter reads public meeting records:
  * Granicus meeting list + HTML agendas for City Commission and Planning & Zoning Board
    https://safetyharbor.granicus.com/ViewPublisher.php?view_id=6
    Agenda items carry the city case number ("2026-06 REZ/LUP", "2026-02 AN/ZO"), applicant,
    address, request text and ordinance number; "LINK TO PUBLIC HEARING" = staff backup.
  * Adopted minutes PDFs in the city's CivicPlus Archive Center (AMID 33 = City Commission,
    43 = P&Z Board) for hearing outcomes ("Motion passed 5-0", first vs. second reading).
    Needs `pypdf`; without it, outcomes are left as None and status falls back to
    Filed / Hearing Scheduled.
  * Pinellas County ParcelPropertyInfo layer (ArcGIS Online) to turn the site address into a
    PA parcel id (04-29-16-51822-002-0140), acreage and centroid.
An application only becomes visible here once it is on a P&Z or Commission agenda, so
filed_date = date of the first meeting where the case appears.
"""
import datetime as _dt
import html as _html
import io
import json
import re
import sys
import time

import requests

try:
    import pypdf  # optional, for minutes outcomes
except Exception:  # pragma: no cover
    pypdf = None

JURISDICTIONS = ["City of Safety Harbor"]

UA = "Mozilla/5.0 (X11; Linux x86_64) ELC-RezoningTracker/1.0 (+https://thedirtdog.com)"
GRANICUS = "https://safetyharbor.granicus.com"
PUBLISHER = GRANICUS + "/ViewPublisher.php?view_id=6"
CITY = "https://www.cityofsafetyharbor.com"
MINUTES_AMID = {"City Commission": 33, "Planning & Zoning Board": 43}
PARCELS = ("https://services.arcgis.com/f5HgUpxURgEzTccH/arcgis/rest/services/"
           "Pinellas_ParcelPropertyInfo/FeatureServer/0/query")
SLEEP = 0.6
TODAY = _dt.date.today().isoformat()

IN_SCOPE = re.compile(r"\bREZ\b|\bLUP\b|/ZO\b|ZONING ATLAS AMENDMENT|FUTURE LAND USE|LAND USE PLAN|"
                      r"REZONING|ZONING (DISTRICT )?ASSIGNMENT|PLANNED (UNIT |RESIDENTIAL )?DEVELOPMENT|"
                      r"\bPD\b|\bPUD\b|COUNTYWIDE PLAN MAP", re.I)
OUT_SCOPE = re.compile(r"CODE AMENDMENT|CONDITIONAL USE|WAIVER|VARIANCE|APPOINTMENT|APPROVAL OF MINUTES|"
                       r"SITE PLAN|CODE OF ORDINANCES|TEXT AMENDMENT|ELECTION", re.I)
CASE_RE = re.compile(r"\b(20\d\d-\d{2,3})\b(?:\s*([A-Z]{2,4}(?:\s*/\s*[A-Z]{1,4})*))?")
ADDR_RE = re.compile(r"\b(\d{2,6}\s+(?:[NSEW]\.?\s+)?[0-9A-Z][A-Z0-9 .]*?\s(?:ST|STREET|AVE|AVENUE|RD|ROAD|DR|DRIVE|BLVD|"
                     r"LN|LANE|CT|COURT|WAY|PL|PLACE|HWY|CIR|TER|TRL|PKWY)\b\.?(?:\s+[NSEW]\b\.?)?)", re.I)

_s = requests.Session()
_s.headers["User-Agent"] = UA


def log(*a):
    print("[safety_harbor]", *a, file=sys.stderr)


def _get(url, **kw):
    last = None
    for i in range(3):
        try:
            r = _s.get(url, timeout=kw.pop("timeout", 60), **kw)
            r.raise_for_status()
            time.sleep(SLEEP)
            return r
        except Exception as e:  # noqa
            last = e
            time.sleep(2 * (i + 1))
    raise last


def _clean(s):
    if s is None:
        return None
    s = re.sub(r"\s+", " ", _html.unescape(str(s)).replace("\xa0", " ")).strip()
    return s or None


def _strip_tags(h):
    return _clean(re.sub(r"<[^>]+>", " ", h or ""))


# ----------------------------------------------------------------------------- meetings
def list_meetings(start):
    h = _get(PUBLISHER).text
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", h, re.S | re.I):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S | re.I)
        if len(cells) < 2:
            continue
        name = _strip_tags(cells[0]) or ""
        ts = re.search(r"\b(1[5-9]\d{8})\b", row)
        link = re.search(r'href="([^"]*AgendaViewer\.php\?[^"]+)"', row)
        if not ts or not link:
            continue
        up = name.upper()
        if "PLANNING" in up and "ZONING" in up:
            body = "Planning & Zoning Board"
        elif "CITY COMMISSION" in up and "WORKSHOP" not in up and "REDEVELOPMENT" not in up:
            body = "City Commission"
        else:
            continue
        d = _dt.datetime.fromtimestamp(int(ts.group(1)), _dt.timezone(_dt.timedelta(hours=-5))).date()
        if d.isoformat() < start:
            continue
        url = _html.unescape(link.group(1))
        url = ("https:" + url) if url.startswith("//") else url
        out.append({"body": body, "date": d.isoformat(), "name": name, "url": url})
    uniq = {(m["body"], m["date"], m["url"]): m for m in out}
    return sorted(uniq.values(), key=lambda m: m["date"])


def agenda_items(meeting):
    h = _get(meeting["url"]).text
    chunks = re.split(r"<td width=40>", h, flags=re.I)[1:]
    items = []
    for ch in chunks:
        links = [_html.unescape(u) for u in re.findall(r'href="(https?://[^"]*MetaViewer\.php[^"]+)"', ch)]
        body = re.sub(r"<a [^>]*>.*?</a>", " ", ch, flags=re.S | re.I)
        txt = _strip_tags(body)
        if not txt:
            continue
        txt = re.sub(r"^\d+\.\s*", "", txt)
        txt = re.split(r"\s\*{4,}\s|BE ADVISED THAT IF A PERSON", txt)[0].strip()
        items.append({"text": txt, "links": links})
    return items


# ----------------------------------------------------------------------------- minutes
_minutes_index, _minutes_text = {}, {}


def minutes_text(body, date):
    if pypdf is None:
        return None
    amid = MINUTES_AMID[body]
    if amid not in _minutes_index:
        idx = {}
        try:
            h = _get(f"{CITY}/Archive.aspx?AMID={amid}").text
            for adid, label in re.findall(r'href="Archive\.aspx\?ADID=(\d+)"[^>]*>(.*?)</a>', h, re.S | re.I):
                lab = _strip_tags(label) or ""
                m = re.match(r"([A-Z][a-z]+)\s+(\d{1,2}),\s*(\d{4})", lab)
                if not m or re.search(r"workshop|CRA|redevelopment|agenda", lab, re.I):
                    continue
                try:
                    d = _dt.datetime.strptime(" ".join(m.groups()), "%B %d %Y").date().isoformat()
                except ValueError:
                    continue
                idx.setdefault(d, adid)
        except Exception as e:
            log("minutes index failed", body, e)
        _minutes_index[amid] = idx
    adid = _minutes_index[amid].get(date)
    if not adid:
        return None
    if adid not in _minutes_text:
        try:
            r = _get(f"{CITY}/Archive.aspx?ADID={adid}", allow_redirects=True)
            rd = pypdf.PdfReader(io.BytesIO(r.content))
            _minutes_text[adid] = "\n".join((p.extract_text() or "") for p in rd.pages)
        except Exception as e:
            log("minutes pdf failed", body, date, e)
            _minutes_text[adid] = None
    return _minutes_text[adid]


def outcome_from_minutes(text, ordinance, address, body):
    """Return (result, reading) for the item in the minutes, or (None, None)."""
    if not text:
        return None, None
    t = re.sub(r"[ \t]+", " ", text)
    pos = -1
    keys = []
    if ordinance:
        o = re.escape(ordinance)
        keys += [rf"read\s+Ordinance\s+No\.?\s*{o}", rf"Ordinance\s+No\.?\s*{o}"]
    if address:
        keys.append(re.escape(address))
    for key in keys:
        mm = re.search(key, t, re.I)
        if mm:
            pos = mm.start()
            break
    if pos < 0:
        return None, None
    seg = t[pos:pos + 6000]
    m = re.search(r"Motion (passed|failed|carried)[^\n]*", seg, re.I)
    if not m:
        return None, None
    seg = seg[:m.end()]
    reading = "first" if re.search(r"on first reading", seg, re.I) else (
        "second" if re.search(r"second (and final )?reading", seg, re.I) else None)
    mv = re.findall(r"moved,?(.*?)(?:Motion (?:passed|failed|carried))", seg, re.I | re.S)
    # PDF text extraction splits words ("to a pprove"), so match on whitespace-free text
    motion = re.sub(r"\s+", "", (mv[-1] if mv else seg[-400:]).lower())
    failed = m.group(1).lower() == "failed"
    if re.search(r"to(continue|postpone|table|defer)", motion):
        res = "Continued"
    elif re.search(r"to(deny|denial)", motion):
        res = "Denied" if not failed else None
    elif re.search(r"to(approve|adopt|recommend)", motion):
        res = "Approved" if not failed else "Motion failed"
    else:
        res = None
    if body == "Planning & Zoning Board" and res in ("Approved", "Denied"):
        res = "Recommended " + ("Approval" if res == "Approved" else "Denial")
    elif res == "Approved" and reading == "first":
        res = "Approved on first reading"
    return res, reading


# ----------------------------------------------------------------------------- parcels
def parcel_lookup(address):
    m = re.match(r"(\d+)\s+(?:([NSEW])\.?\s+)?([0-9A-Z]+)", (address or "").upper())
    if not m:
        return None
    num, name = m.group(1), m.group(3)
    try:
        d = _get(PARCELS, params={
            "where": f"STR_NUM = {int(num)} AND STR_NAME LIKE '{name}%' AND STR_CITY LIKE 'SAFETY HARBOR%'",
            "outFields": "PARCELID_DSP1,SITE_ADDRESS,ACREAGE", "returnCentroid": "true",
            "returnGeometry": "false", "outSR": 4326, "f": "json"}).json()
    except Exception as e:
        log("parcel lookup failed", address, e)
        return None
    feats = d.get("features") or []
    if len(feats) != 1:
        return None
    a, c = feats[0]["attributes"], feats[0].get("centroid") or {}
    return {"pin": a.get("PARCELID_DSP1"), "acres": a.get("ACREAGE"),
            "lat": c.get("y"), "lon": c.get("x")}


# ----------------------------------------------------------------------------- main
def _case_of(text):
    for m in CASE_RE.finditer(text):
        base, suf = m.group(1), m.group(2)
        ctx = text[max(0, m.start() - 15):m.start()].upper()
        if "ORDINANCE" in ctx or "RESOLUTION" in ctx:
            continue
        return base, (re.sub(r"\s+", "", suf) if suf and suf not in ("APPLICANT", "ADDRESS") else None)
    return None, None


def _addr_of(text):
    m = re.search(r"ADDRESS:\s*(.+?)(?:\s+REQUEST:|$)", text, re.I)
    if m:
        return _clean(m.group(1).rstrip(". "))
    m = ADDR_RE.search(text)
    return _clean(m.group(1).rstrip(". ")) if m else None


def _from_to(text):
    m = re.search(r"FROM\s+(.+?)\s+TO\s+(.+?)(?:[.;]|\s+FOLLOWING|$)", text, re.I)
    if m:
        return _clean(m.group(1)), _clean(m.group(2))
    m = re.search(r"APPLY THE\s+(.+?)\s+ZONING DESIGNATION", text, re.I)
    return (None, _clean(m.group(1))) if m else (None, None)


def fetch(since, refresh):
    refresh = [r.strip() for r in (refresh or []) if r and r.strip()]
    since_d = _dt.date.fromisoformat(since)
    start = (since_d - _dt.timedelta(days=400)).isoformat()
    meetings = list_meetings(start)  # raises if Granicus is unreachable
    cases, addr_to_case = {}, {}
    for mt in meetings:
        try:
            items = agenda_items(mt)
        except Exception as e:
            log("agenda failed", mt["date"], mt["body"], e)
            continue
        for it in items:
            txt = it["text"]
            if not IN_SCOPE.search(txt) or OUT_SCOPE.search(txt):
                continue
            base, suf = _case_of(txt)
            addr = _addr_of(txt)
            if not base and addr:
                base = addr_to_case.get(addr.upper().rstrip("."))
            if not base:
                log("no case number:", mt["date"], txt[:90])
                continue
            c = cases.setdefault(base, {"base": base, "suffix": None, "items": []})
            if suf and (not c["suffix"] or len(suf) > len(c["suffix"])):
                c["suffix"] = suf
            if addr:
                c.setdefault("address", addr)
                addr_to_case[addr.upper().rstrip(".")] = base
            ordn = re.search(r"Ordinance No\.?\s*(\d{4}-\d{2,3})", txt, re.I)
            c["items"].append({"meeting": mt, "text": txt, "links": it["links"],
                               "ordinance": ordn.group(1) if ordn else None})
    out = []
    for base, c in cases.items():
        app_num = f"{base} {c['suffix']}" if c["suffix"] else base
        first = min(i["meeting"]["date"] for i in c["items"])
        if first < since and app_num not in refresh and base not in refresh:
            continue
        try:
            out.append(_record(app_num, c))
        except Exception as e:
            log("skip", app_num, repr(e))
    return out


def _record(app_num, c):
    items = sorted(c["items"], key=lambda i: i["meeting"]["date"])
    alltxt = " ".join(i["text"] for i in items)
    suf = (c["suffix"] or "").upper()
    if "LUP" in suf or re.search(r"FUTURE LAND USE|LAND USE PLAN", alltxt, re.I):
        cat = "Comp Plan Amendment"
    elif re.search(r"\bPD\b|\bPUD\b|PLANNED (UNIT |RESIDENTIAL )?DEVELOPMENT", suf + " " + alltxt, re.I):
        cat = "PD"
    else:
        cat = "Rezoning"
    fz = tz = ff = tf = None
    for i in items:
        f, t = _from_to(i["text"])
        if re.search(r"FUTURE LAND USE|LAND USE PLAN", i["text"], re.I):
            ff, tf = ff or f, tf or t
        elif re.search(r"ZONING", i["text"], re.I):
            fz, tz = fz or f, tz or t
    appl = re.search(r"APPLICANT:\s*(.+?)(?:\s+ADDRESS:|\s+REQUEST:|$)", alltxt)
    hearings, seen = [], {}
    for i in items:
        mt = i["meeting"]
        res = None
        if mt["date"] < TODAY:
            res, reading = outcome_from_minutes(minutes_text(mt["body"], mt["date"]), i["ordinance"],
                                                c.get("address"), mt["body"])
            # agenda wording: "Approval/Denial of Ordinance" = 1st reading, "Adoption/Denial" = 2nd
            if res == "Approved" and mt["body"] == "City Commission" and reading != "second" and (
                    reading == "first" or re.search(r"on first reading|Approval/Denial", i["text"], re.I)):
                res = "Approved on first reading"
        key = (mt["body"], mt["date"])
        if key in seen:  # FLU + zoning items at same meeting: keep the most informative result
            h = seen[key]
            if res and (h["result"] is None or (h["result"] == "Approved" and res != "Approved")):
                h["result"] = res if h["result"] is None else h["result"]
            continue
        h = {"body": mt["body"], "date": mt["date"], "result": res}
        seen[key] = h
        hearings.append(h)
    status, decision = "Filed", None
    final = [h for h in hearings if h["body"] == "City Commission" and h["result"] in ("Approved", "Denied")]
    if final:
        status, decision = final[-1]["result"], final[-1]["date"]
    elif any(h["result"] == "Withdrawn" for h in hearings):
        status = "Withdrawn"
    elif any(h["date"] >= TODAY for h in hearings):
        status = "Hearing Scheduled"
    elif hearings and hearings[-1]["result"] == "Continued":
        status = "Continued"
    last = items[-1]
    raw = last["text"][:160]
    parcel = parcel_lookup(c.get("address"))
    docs = next((l for i in reversed(items) for l in i["links"]), None)
    desc = _clean(" / ".join(dict.fromkeys(
        re.sub(r"^.*?REQUEST:\s*", "", i["text"]) if "REQUEST:" in i["text"] else i["text"] for i in items
        if i["meeting"]["body"] == "Planning & Zoning Board"))) or _clean(items[0]["text"])
    return {
        "jurisdiction": "City of Safety Harbor", "county": "Pinellas", "app_num": app_num,
        "category": cat,
        "app_type": {"REZ/LUP": "Rezoning / Future Land Use Amendment", "AN/ZO": "Zoning assignment after annexation",
                     "LUP": "Future Land Use Amendment", "REZ": "Rezoning"}.get(suf, suf or cat),
        "project_name": None,
        "description": desc[:500] if desc else None,
        "applicant": _clean(appl.group(1)) if appl else None,
        "parcel_ids": [parcel["pin"]] if parcel and parcel.get("pin") else [],
        "address": (c.get("address") + ", Safety Harbor, FL") if c.get("address") else None,
        "acres": round(parcel["acres"], 2) if parcel and parcel.get("acres") else None,
        "from_zoning": fz, "to_zoning": tz, "from_flu": ff, "to_flu": tf,
        "filed_date": items[0]["meeting"]["date"],
        "status": status, "status_raw": f"{last['meeting']['body']} {last['meeting']['date']} agenda: {raw}",
        "hearings": hearings, "decision_date": decision,
        "url": last["meeting"]["url"], "docs_url": docs,
        "lat": round(parcel["lat"], 6) if parcel and parcel.get("lat") else None,
        "lon": round(parcel["lon"], 6) if parcel and parcel.get("lon") else None,
        "source": "Safety Harbor Granicus agendas + Archive Center minutes",
    }


if __name__ == "__main__":
    since = sys.argv[1] if len(sys.argv) > 1 else (_dt.date.today() - _dt.timedelta(days=14)).isoformat()
    refresh = sys.argv[2].split(",") if len(sys.argv) > 2 else []
    t0 = time.time()
    recs = fetch(since, refresh)
    print(json.dumps(recs, indent=1))
    log(f"{len(recs)} records since {since} in {time.time() - t0:.0f}s")
