#!/usr/bin/env python3
"""City of Venice (Sarasota County) rezonings, PUD amendments and future land use map amendments.

Source: the city's Legistar web API (webapi.legistar.com/v1/venice).
Venice files every land use petition as a Legistar matter with the petition number as File #:
  "26-42RZ"  (Rezoning matter type, Planning Commission)  zoning map amendment or PUD amendment
  "26-15CP"  (Comprehensive Plan Amendment type)            future land use map / plan amendment
  "26-14AN"  annexation (skipped; its concurrent CP / RZ petitions are kept)
followed by the City Council ordinance(s) ("ORD. NO. 2026-40 ... Pursuant to Zoning Map Amendment
Petition No. 26-42RZ ..."). Matters re-agendaed under a temporary file number (TMP-11557) or none
are tied to the petition by their Name ("Venice Yacht Club") and kind.
app_num = petition number. Category: CP -> Comp Plan Amendment (only Future Land Use MAP
amendments; text-only plan amendments such as the annual data update or new policies are
skipped); RZ -> PD when it is a PUD / planned district amendment or the proposed district is
planned (PUD, PCD, PID, PPH, ...), else Rezoning.
Hearings and outcomes: matter histories (Planning Commission recommendation, City Council first
reading, "approved and adopted", "denied", "continued"), plus MatterAgendaDate for a scheduled
hearing. Details (address, owner, agent, parcel ids, acreage, current / proposed zoning and FLU,
application date) come from the petition's Staff Report PDF (page 1 "GENERAL INFORMATION" block),
with the ordinance title as fallback.
Parcel IDs: 10-digit Sarasota County PA ids as posted (e.g. 0173140001).
Requires pypdf.
"""
import datetime as dt
import io
import json
import os
import re
import sys
import time

import requests

try:
    import pypdf
except ImportError:  # pragma: no cover
    pypdf = None

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

JURISDICTIONS = ["City of Venice"]
JUR = "City of Venice"
API = "https://webapi.legistar.com/v1/venice"
SITE = "https://venice.legistar.com"
SOURCE = "Venice Legistar (Planning Commission + City Council, staff reports)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
PC, CC = "Planning Commission", "City Council"
TYPES = {72: "RZ", 77: "CP", 53: "ORD"}
PET_RX = re.compile(r"\b(\d{2})-(\d{1,3})\s?(RZ|CP)\b", re.I)
PD_RX = re.compile(r"\bPUD\b|planned|\bP(C|I|R|M|E)D\b|\bPPH\b", re.I)
MAX_PDF = 15_000_000

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


def log(*a):
    print("[venice]", *a, file=sys.stderr, flush=True)


def get(url, params=None, tries=3, sleep=0.3, as_json=True, timeout=60):
    last = None
    for i in range(tries):
        try:
            r = _s.get(url, params=params, timeout=timeout,
                       headers={"Accept": "application/json"} if as_json else None)
            r.raise_for_status()
            time.sleep(sleep)
            return r.json() if as_json else r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


def sq(s):
    return re.sub(r"\s+", " ", s or "").strip()


def pet_key(m):
    return f"{m.group(1)}-{int(m.group(2)):02d}{m.group(3).upper()}"


def matters_since(start):
    out, skip = [], 0
    types = " or ".join(f"MatterTypeId eq {t}" for t in TYPES)
    while True:
        batch = get(f"{API}/matters", {"$filter": f"MatterIntroDate ge datetime'{start}' and ({types})",
                                       "$orderby": "MatterId", "$top": 1000, "$skip": skip})
        out.extend(batch)
        if len(batch) < 1000:
            break
        skip += 1000
    return out


def matter_pets(m):
    """Petition numbers a matter belongs to, read from File #, name and title."""
    pets = []
    for src in (m.get("MatterFile"), m.get("MatterName"), m.get("MatterTitle")):
        for x in PET_RX.finditer(src or ""):
            k = pet_key(x)
            if k not in pets:
                pets.append(k)
    # "24-6CP" typo in the name of 24-60CP: trust File # first
    f = PET_RX.fullmatch((m.get("MatterFile") or "").strip())
    if f:
        k = pet_key(f)
        pets = [k] + [p for p in pets if p != k and p[-2:] != k[-2:]]
    return pets


def group_matters(ms):
    groups, loose = {}, []
    for m in ms:
        kind = TYPES.get(m.get("MatterTypeId"))
        pets = matter_pets(m)
        if kind == "RZ":
            pets = [p for p in pets if p.endswith("RZ")]
        elif kind == "CP":
            pets = [p for p in pets if p.endswith("CP")]
        if not pets:
            if kind in ("RZ", "CP"):
                loose.append(m)
            continue
        for p in pets[:1] if kind in ("RZ", "CP") else pets:
            groups.setdefault(p, []).append(m)
    # matters filed under TMP-numbers: attach by name + kind
    for m in loose:
        kind = TYPES[m["MatterTypeId"]]
        name = sq(m.get("MatterName")).lower()
        if not name:
            continue
        cands = []
        for p, lst in groups.items():
            if not p.endswith(kind):
                continue
            names = {sq(x.get("MatterName")).lower() for x in lst}
            names |= {re.sub(r"^\d{2}-\d{1,3}(rz|cp)\s+", "", n) for n in names}
            if name in names or any(name and (name in n or n in name) for n in names if len(n) > 5):
                cands.append(p)
        if len(cands) >= 1:
            p = sorted(cands)[-1]
            groups[p].append(m)
    return groups


def histories(mid):
    try:
        return get(f"{API}/matters/{mid}/histories")
    except Exception as e:  # noqa: BLE001
        log(f"histories {mid}: {e}")
        return []


def attachments(mid):
    try:
        return get(f"{API}/matters/{mid}/attachments")
    except Exception as e:  # noqa: BLE001
        log(f"attachments {mid}: {e}")
        return []


def pdf_text(url, pages=3):
    if pypdf is None:
        return ""
    try:
        r = get(url, as_json=False, timeout=120)
        if len(r.content) > MAX_PDF:
            return ""
        rdr = pypdf.PdfReader(io.BytesIO(r.content))
        return "\n".join((p.extract_text() or "") for p in rdr.pages[:pages])
    except Exception as e:  # noqa: BLE001
        log(f"pdf {url[-50:]}: {e}")
        return ""


def body_of(name):
    n = (name or "").lower()
    if "planning" in n:
        return PC
    if "council" in n:
        return CC
    return None


def result_of(action, body):
    a = sq(action).lower()
    if not a:
        return None
    if re.search(r"withdr", a):
        return "Withdrawn"
    if re.search(r"continu|defer|postpone|tabled|table\b", a):
        return "Continued"
    if body == PC:
        if "approv" in a:
            return "Recommended Approval"
        if "den" in a:
            return "Recommended Denial"
        return None
    if "first reading" in a and "approv" in a:
        return "Approved on first reading"
    if re.search(r"adopt|approv|passed", a):
        return "Approved"
    if re.search(r"\bden|fail", a):
        return "Denied"
    return None


LABELS = ["Address", "Request", "Owner", "Owners", "Applicant", "Agent", "Parcel ID", "Parcel IDs",
          "Parcel ID Numbers", "Parcel Size", "Future Land Use", "Proposed Future Land Use", "Zoning",
          "Existing Zoning", "Proposed Zoning", "Comprehensive Plan Neighborhood", "Application Date",
          "Current Future Land Use", "Current Zoning", "Annexation"]
_LAB_RX = re.compile(r"(?:^|\n)\s*(" + "|".join(sorted((re.escape(x) for x in LABELS), key=len, reverse=True)) +
                     r")\s*(?:\(s\))?\s*:\s*", re.I)


def staff_info(text):
    """Parse the GENERAL INFORMATION block of a Venice staff report ("Label: value" lines; some
    reports put all labels first and the values after them in the same order)."""
    head = text[:5000]
    i = head.find("GENERAL INFORMATION")
    if i >= 0:
        head = head[i + len("GENERAL INFORMATION"):]
    lab_rx = re.compile(r"^\s*(" + "|".join(sorted((re.escape(x) for x in LABELS), key=len, reverse=True)) +
                        r"|Owner\s*/\s*Applicant|Applicant\s*/\s*Owner)\s*(?:\(s\))?\s*:\s*(.*)$", re.I)
    out, pending, cur = {}, [], None
    for line in head.split("\n"):
        line = line.strip()
        if not line:
            continue
        if re.search(r"\|\s*P\s?a\s?g\s?e|^I\.\s|PROJECT DESCRIPTION", line):
            break
        m = lab_rx.match(line)
        if m:
            lab = re.sub(r"\s+", "", m.group(1).lower()).replace("/", " / ")
            lab = {"owner / applicant": "owner", "applicant / owner": "owner"}.get(lab, sq(m.group(1)).lower())
            if m.group(2).strip():
                out.setdefault(lab, sq(m.group(2)))
                cur = lab
            else:
                pending.append(lab)
                cur = None
            continue
        if pending:
            lab = pending.pop(0)
            out.setdefault(lab, sq(line))
            cur = None
        elif cur:
            out[cur] = sq(out[cur] + " " + line)
    return out


def parse_title(t):
    out = {}
    u = sq(t)
    m = re.search(r"\(([\d,]*\.?\d+)\s*(?:\+/-|±|\+/−)?\s*Acres?\)", u, re.I) or \
        re.search(r"([\d,]*\.?\d+)\s*(?:\+/-|±)\s*Acres?", u, re.I)
    if m:
        try:
            out["acres"] = float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    out["pids"] = re.findall(r"Parcel ID(?: No\.?|s)?\s*(\d{9,10})", u, re.I)
    m = re.search(r"Located at\s+(.+?)(?:\s*\(|,\s*from|\s+from\s|\s+by\s|;|$)", u, re.I)
    if m:
        out["address"] = m.group(1).strip(" ,")
    m = re.search(r"\bfrom\s+(.+?)\s+to\s+(.+?)(?:;|,\s*(?:and|providing)|\s+and\s+(?:providing|finding)|$)", u, re.I)
    if m and len(m.group(1)) < 120 and len(m.group(2)) < 120:
        out["from"], out["to"] = m.group(1).strip(" ,"), m.group(2).strip(" ,")
    m = re.search(r"(?:Zoning|Future Land Use) Designation of\s+(.+?)\s+to\s+Parcel", u, re.I)
    if m and not out.get("to"):
        out["to"] = m.group(1).strip(" ,")
    return out


def _date(s):
    if not s:
        return None
    s = sq(s)
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%B %d %Y"):
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    m = re.search(r"([A-Z][a-z]+ \d{1,2}, \d{4})", s)
    if m:
        return _date(m.group(1))
    return None


def _pids(s):
    """Sarasota PA ids as posted: '0173140001' or dashed '0413-05-0001'."""
    out = []
    for p in re.findall(r"(?<![\d-])(\d{4}-\d{2}-\d{4}|\d{9,10})(?![\d-])", s or ""):
        out.append(p)
    return out


def build(pet, ms, today):
    kind = pet[-2:]
    ms = sorted(ms, key=lambda m: (m.get("MatterIntroDate") or "", m["MatterId"]))
    titles = " ".join(sq(m.get("MatterTitle")) for m in ms)
    main = next((m for m in ms if TYPES.get(m["MatterTypeId"]) == kind and sq(m.get("MatterFile")).upper() == pet), None) \
        or next((m for m in ms if TYPES.get(m["MatterTypeId"]) == kind), ms[0])
    ords = [m for m in ms if TYPES.get(m["MatterTypeId"]) == "ORD"]
    if kind == "CP":
        if not re.search(r"Future Land Use Map|Future Land Use (Designation|Amendment)|FLUM", titles, re.I):
            return None
    # details: staff report of the petition matter(s)
    info, docs = {}, None
    for m in [x for x in ms if TYPES.get(x["MatterTypeId"]) == kind][::-1] + ords[::-1]:
        att = attachments(m["MatterId"])
        sr = next((a for a in att if re.search(r"staff report", a.get("MatterAttachmentName") or "", re.I)), None)
        if sr and sr.get("MatterAttachmentHyperlink"):
            docs = docs or sr["MatterAttachmentHyperlink"]
            info = staff_info(pdf_text(sr["MatterAttachmentHyperlink"]))
            if info:
                break
    tinfo = {}
    for m in ords + ms:
        for k, v in parse_title(m.get("MatterTitle")).items():
            if v and not tinfo.get(k):
                tinfo[k] = v
    # hearings
    hs = {}
    for m in ms:
        b0 = body_of(m.get("MatterBodyName"))
        seen = set()
        for h in histories(m["MatterId"]):
            d = (h.get("MatterHistoryActionDate") or "")[:10]
            b = body_of(h.get("MatterHistoryActionBodyName")) or b0
            if not d or not b:
                continue
            res = result_of(h.get("MatterHistoryActionName"), b)
            k = (b, d)
            if k not in hs or (res and not hs[k]["result"]):
                hs[k] = {"body": b, "date": d, "result": res}
            seen.add(d)
        ad = (m.get("MatterAgendaDate") or "")[:10]
        if ad and b0 and ad >= today and ad not in seen and (b0, ad) not in hs:
            hs[(b0, ad)] = {"body": b0, "date": ad, "result": None}
    hl = sorted(hs.values(), key=lambda h: (h["date"], h["body"] != PC))
    # status
    final = [h for h in hl if h["body"] == CC and h["result"] in ("Approved", "Denied", "Withdrawn") and h["date"] <= today]
    passed = [m for m in ords if sq(m.get("MatterStatusName")) == "Passed" and m.get("MatterPassedDate")]
    failed = [m for m in ords if sq(m.get("MatterStatusName")) in ("Failed", "Killed")]
    future = [h for h in hl if h["date"] >= today]
    status_raw = sq((ords[-1] if ords else main).get("MatterStatusName")) or None
    decision = None
    if final:
        status, decision = final[-1]["result"], final[-1]["date"]
    elif passed and len(passed) == len(ords):
        status, decision = "Approved", max(m["MatterPassedDate"][:10] for m in passed)
    elif failed:
        status = "Denied"
    elif future:
        status = "Hearing Scheduled"
    elif hl and hl[-1]["result"] == "Continued":
        status = "Continued"
    elif hl:
        # heard, no final action recorded yet (e.g. PC recommendation; Council not yet scheduled)
        status = "Hearing Scheduled" if any(h["body"] == CC and not h["result"] for h in hl) else "Filed"
    else:
        status = "Filed" if not re.search(r"public hearing|agenda ready", status_raw or "", re.I) else "Hearing Scheduled"
    if status not in ("Approved", "Denied"):
        decision = None
    # fields
    g = lambda *ks: next((info[k] for k in ks if info.get(k)), None)  # noqa: E731
    pids = []
    for p in _pids(g("parcel id", "parcel ids", "parcel id numbers")) or \
            [x for x in tinfo.get("pids", []) if len(x) == 10]:
        if p not in pids:
            pids.append(p)
    acres = None
    ps = g("parcel size")
    if ps:
        m = re.search(r"([\d,]*\.?\d+)\s*(?:±|\+/-|\+)?\s*(acres?|ac\b|sq)", ps, re.I)
        if m:
            try:
                acres = float(m.group(1).replace(",", ""))
                if m.group(2).lower().startswith("sq"):
                    acres = round(acres / 43560.0, 3)
            except ValueError:
                acres = None
    acres = acres or tinfo.get("acres")
    from_z = g("zoning", "existing zoning", "current zoning")
    to_z = g("proposed zoning")
    from_f = g("future land use", "current future land use")
    to_f = g("proposed future land use")
    req = g("request")
    if kind == "RZ" and not to_z and tinfo.get("to"):
        from_z, to_z = from_z or tinfo.get("from"), tinfo.get("to")
    if kind == "CP" and not to_f and tinfo.get("to"):
        from_f, to_f = from_f or tinfo.get("from"), tinfo.get("to")
    if kind == "RZ" and not to_z and re.search(r"PUD Amendment|Planned District Zoning Amendment|Binding Master Plan",
                                               titles + " " + (req or ""), re.I):
        to_z = from_z  # PUD amendment: the district stays the same
    if kind == "CP":
        category = "Comp Plan Amendment"
        app_type = "Comprehensive Plan Amendment (Future Land Use Map)"
    else:
        pd = bool(PD_RX.search(to_z or "") or re.search(r"\bPUD Amendment|Planned (Unit )?Development|Planned District", titles + " " + (req or ""), re.I))
        category = "PD" if pd else "Rezoning"
        app_type = "PUD Amendment" if re.search(r"PUD Amendment|Planned District Zoning Amendment", titles, re.I) else "Zoning Map Amendment"
    tl = sq(main.get("MatterTitle"))
    appl = None
    m = re.search(r"(?:Owner\s*/\s*Applicant|Applicant\s*/\s*Owner|Owner/\s*Applicant|Applicant)\s*:\s*(.+?)(?:$|\s+Agent\s*:|\s+Owner\s*:)", tl, re.I)
    if m:
        appl = m.group(1).strip(" ,;")
    agent = g("agent")
    if not agent:
        m = re.search(r"Agent\s*[:;]\s*(.+?)(?:\s+(?:Owner|Applicant)|$)", tl, re.I)
        agent = m.group(1).strip(" ,;") if m else None
    applicant = appl or g("applicant", "owner", "owners")
    if applicant and agent and agent.lower() not in applicant.lower():
        applicant = f"{applicant} (agent: {agent})"
    elif not applicant and agent:
        applicant = agent
    name = sq(main.get("MatterName"))
    name = re.sub(r"^\d{1,2}-\d{1,3}(RZ|CP)\s*", "", name, flags=re.I).strip() or None
    desc = req or re.split(r"\s+Staff:", tl)[0]
    if ords:
        desc = (desc + " | " if desc else "") + "; ".join(f"{sq(o.get('MatterFile'))}" for o in ords)
    filed = _date(g("application date")) or min((m.get("MatterIntroDate") or "")[:10] for m in ms)
    try:
        import legistar_links as LL  # noqa: E402
        url = LL.site_link(API, [main] + ords, meeting_fallback=True)
    except Exception as e:  # noqa: BLE001
        log(f"{pet}: link failed: {e}")
        url = None
    return {
        "jurisdiction": JUR, "county": "Sarasota", "app_num": pet, "category": category, "app_type": app_type,
        "project_name": name, "description": desc[:600] if desc else None, "applicant": applicant,
        "parcel_ids": pids, "address": g("address") or tinfo.get("address"), "acres": acres,
        "from_zoning": from_z if kind == "RZ" else None, "to_zoning": to_z if kind == "RZ" else None,
        "from_flu": from_f if kind == "CP" else None, "to_flu": to_f if kind == "CP" else None,
        "filed_date": filed, "status": status, "status_raw": status_raw,
        "hearings": hl, "decision_date": decision,
        "url": url or f"{SITE}/Legislation.aspx", "docs_url": docs,
        "lat": None, "lon": None, "source": SOURCE,
    }


def fetch(since, refresh=None):
    today = dt.date.today()
    refresh = {r.strip().upper() for r in (refresh or []) if r and r.strip()}
    start = dt.date.fromisoformat(since) - dt.timedelta(days=120)
    if refresh:
        start = min(start, today - dt.timedelta(days=730))
    try:
        ms = matters_since(start.isoformat())
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Venice Legistar unreachable: {e}") from e
    groups = group_matters(ms)
    log(f"{len(ms)} matters since {start}; {len(groups)} petitions")
    out = []
    for pet, lst in sorted(groups.items()):
        if not any(TYPES.get(m["MatterTypeId"]) in ("RZ", "CP") for m in lst) and \
                not any(re.search(r"Petition No", m.get("MatterTitle") or "", re.I) for m in lst):
            continue
        first = min((m.get("MatterIntroDate") or "")[:10] for m in lst)
        if pet.upper() not in refresh:
            # a petition first appearing on/after `since` (Legistar shows it once it is agendaed)
            if first < since:
                continue
        try:
            rec = build(pet, lst, today.isoformat())
        except Exception as e:  # noqa: BLE001
            log(f"{pet}: skipped ({type(e).__name__}: {e})")
            continue
        if rec is None:
            continue
        out.append({k: (None if v == "" else v) for k, v in rec.items()})
    missing = refresh - {r["app_num"].upper() for r in out}
    if missing:
        log(f"refresh ids not found: {sorted(missing)}")
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
