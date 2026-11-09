#!/usr/bin/env python3
"""City of Winter Springs (Seminole County) rezoning / PUD / future land use map amendment adapter.

Source: the city's Granicus meeting portal (winterspringsfl.granicus.com, ViewPublisher view_id=1), whose
agendas are produced by Legistar (Granicus "Peak"); there is no public Legistar Web API for the city.
 * Meetings: "Planning and Zoning Board/Local Planning Agency" and "City Commission". The agenda PDF lists
   numbered items ("400. Ordinance 2026-03: Small-Scale Future Land Use Map Amendment of three (3)
   parcels located at ...", "402. First Reading of Ordinance 2026-04: Rezoning of ...") followed by the
   attachment names; the PDF carries a link for every attachment.
 * app_num = "Ordinance YYYY-NN". Kept: rezoning / zoning district assignment / PUD and future land use map
   amendments. Skipped: annexation, text amendments (Town Center code, LDC chapters), conditional uses,
   site plans, waivers, development agreements.
 * The item "Staff Report" attachment gives applicant, location, tract size, Seminole County parcel IDs
   (01-21-30-501-0000-0210 style) and the from/to designations.
 * Outcomes: the draft minutes attached to later agendas ("2026_03_09_City_Commission_Regular_Meeting_
   Minutes.pdf": "MOTION TO APPROVE ITEM 402 (FIRST READING OF ORDINANCE 2026-04) ... MOTION CARRIED: 5-0").
   The signed minutes in the MinutesViewer are scanned, so they are not used.
Granicus redirects agenda downloads to an S3 host whose name contains "_" (TLS hostname check fails in
Python), so those are fetched through the path-style S3 URL. Requires pypdf.
"""
import datetime as dt
import io
import json
import re
import sys
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

try:
    import pypdf
except ImportError:  # pragma: no cover
    pypdf = None

JURISDICTIONS = ["City of Winter Springs"]
JUR = "City of Winter Springs"
COUNTY = "Seminole"
GRANICUS = "https://winterspringsfl.granicus.com"
PUBLISHER = GRANICUS + "/ViewPublisher.php?view_id=1"
SOURCE = "Winter Springs Granicus/Legistar (P&Z Board/LPA + City Commission agendas, staff reports, minutes)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
LOOKBACK_DAYS = 150
MAX_ATT = 15_000_000
PZ = "Planning & Zoning Board/LPA"
CC = "City Commission"
ITEM_RE = re.compile(r"\n\s*(\d{3})\.\s*\n")

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


def log(*a):
    print("[winter_springs]", *a, file=sys.stderr)


def get(url, tries=4, sleep=0.4):
    """GET following redirects by hand (path-style URL for S3 bucket names with '_')."""
    if url.startswith("//"):
        url = "https:" + url
    last = None
    for i in range(tries):
        try:
            u = url
            for _ in range(8):
                r = _s.get(u, allow_redirects=False, timeout=120)
                if r.status_code in (301, 302, 303, 307, 308):
                    loc = urljoin(u, r.headers["Location"])
                    m = re.match(r"https?://([^/]*_[^/]*)\.s3\.amazonaws\.com/(.*)", loc)
                    u = f"https://s3.amazonaws.com/{m.group(1)}/{m.group(2)}" if m else loc
                    continue
                r.raise_for_status()
                time.sleep(sleep)
                return r
            raise RuntimeError("too many redirects")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


def sq(s):
    return re.sub(r"\s+", " ", s or "").strip()


def pdf_reader(content):
    return pypdf.PdfReader(io.BytesIO(content))


def pdf_text(content, max_pages=None):
    rdr = pdf_reader(content)
    out = []
    for p in rdr.pages[:max_pages] if max_pages else rdr.pages:
        try:
            out.append(p.extract_text() or "")
        except Exception:  # noqa: BLE001
            out.append("")
    return "\n".join(out)


def pdf_links(rdr):
    out = []
    for p in rdr.pages:
        for a in p.get("/Annots") or []:
            try:
                A = a.get_object().get("/A")
                if A and A.get("/URI"):
                    out.append(str(A["/URI"]))
            except Exception:  # noqa: BLE001
                pass
    return out


def norm_name(s):
    s = re.sub(r"\.(pdf|docx?|xlsx?|pptx?|PDF)$", "", (s or "").strip())
    return re.sub(r"[^a-z0-9]", "", s.lower())


def download_url(link):
    """Attachment link -> direct Granicus download URL."""
    m = re.search(r"/(?:attachment/pdf|download/pdf)/(\d+)/([^?#]+)", link)
    if m:
        return f"{GRANICUS}/services/legistar/download/pdf/{m.group(1)}/{m.group(2)}"
    return link


# ------------------------------------------------------------------ meetings
def list_meetings():
    s = BeautifulSoup(get(PUBLISHER).text, "lxml")
    out = []
    for r in s.select("tr"):
        tds = r.find_all("td")
        if len(tds) < 2:
            continue
        name = sq(tds[0].get_text(" "))
        m = re.search(r"(\w{3})\s+(\d{1,2}),\s+(\d{4})", tds[1].get_text(" "))
        if not m:
            continue
        try:
            date = dt.datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%b %d %Y").date().isoformat()
        except ValueError:
            continue
        hrefs = [a["href"] for a in r.find_all("a", href=True)]
        ag = next((h for h in hrefs if "AgendaViewer" in h), None)
        pk = next((h for h in hrefs if h.lower().endswith(".pdf") and "AgendaViewer" not in h), None)
        out.append({"name": name, "date": date, "agenda": ag, "packet": pk})
    return out


def body_of(name):
    if re.search(r"CANCEL|Workshop|Budget|Strategic", name, re.I):
        return None
    if re.search(r"Planning and Zoning|Local Planning", name, re.I):
        return PZ
    if re.search(r"^City Commission", name, re.I):
        return CC
    return None


# ------------------------------------------------------------------ parsing
ORD_RE = re.compile(r"\bOrdinances?\s*(?:No\.?\s*)?(\d{4})\s*-\s*(\d{1,3})\b", re.I)
CPA_RE = re.compile(r"Future\s+Land\s+Use|Comprehensive\s+Plan\s+(?:Map\s+)?Amendment|Small[- ]Scale|Large[- ]Scale|\bFLUM?\b",
                    re.I)
RZ_RE = re.compile(r"Rezon|Zoning\s+District|Zoning\s+Map|Planned\s+Unit\s+Development|\bPUD\b", re.I)
OUT_RE = re.compile(r"Annex|Text|Chapter\s+\d|Section\s+\d|Transect\s+Setbacks|Town\s+Center\s+(?:District\s+)?Code|"
                    r"Requirements|Regulations|Evaluation\s+and\s+Appraisal|\bEAR\b|Chickens|Conditional\s+Use", re.I)


def okey(m):
    return f"{m.group(1)}-{int(m.group(2)):02d}"


def agenda_items(text):
    """[(item_no, title, [attachment names])]."""
    parts = ITEM_RE.split("\n" + text)
    out = []
    for i in range(1, len(parts) - 1, 2):
        no, block = parts[i], parts[i + 1]
        block = re.split(r"\n\s*(?:REGULAR AGENDA|REPORTS|PUBLIC INPUT|ADJOURNMENT|PUBLIC NOTICE|CONSENT AGENDA|"
                         r"PUBLIC HEARINGS AGENDA)\b", block)[0]
        chunks = [c for c in re.split(r"\n[ \t]*\n", block) if c.strip()]
        if not chunks:
            continue
        title = sq(chunks[0])
        atts = []
        for c in chunks[1:]:
            cur = ""
            for line in c.split("\n"):
                cur = (cur + " " + line.strip()).strip()
                if re.search(r"\.(pdf|docx?|xlsx?|pptx?)\s*$", cur, re.I):
                    atts.append(cur)
                    cur = ""
            if cur:
                atts.append(cur)
        out.append((no, title, atts))
    return out


def classify(title):
    m = ORD_RE.search(title)
    if not m:
        return None
    rest = title[m.end():]
    if OUT_RE.search(rest):
        return None
    if CPA_RE.search(rest):
        return okey(m), "CPA"
    if RZ_RE.search(rest):
        return okey(m), "RZ"
    return None


PID_RE = re.compile(r"\b\d{2}\s*-\s*\d{2}\s*-\s*\d{2}\s*-\s*[0-9A-Z]{3}\s*-\s*[0-9A-Z]{4}\s*-\s*[0-9A-Z]{4}\b")


def staff_fields(text):
    d = sq(text)
    out = {}
    out["ids"] = list(dict.fromkeys(re.sub(r"\s+", "", x) for x in PID_RE.findall(d)))
    m = re.search(r"(?:Tract\s+Size|totaling|Site\s+Area:?)\s*(?:±|\+/-)?\s*([\d,]*\.?\d+)\s*(?:\+/-\s*)?acres", d, re.I) or \
        re.search(r"(?:±|\+/-)\s*([\d,]*\.?\d+)\s*acres", d, re.I)
    if m:
        try:
            out["acres"] = float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    m = re.search(r"\bApplicant\s+(.{3,120}?)\s+Property\s+Owner", d)
    if m:
        out["applicant"] = m.group(1).strip(" ,")
    m = re.search(r"\bLocation\s+(.{3,120}?)\s+Tract\s+Size", d)
    if m:
        out["location"] = m.group(1).strip(" ,")
    m = re.search(r"\bfrom\s+((?:Seminole\s+County|City\s+of\s+Winter\s+Springs)?\s*[“\"][^”\"]{1,60}[”\"][^.,;]{0,40}?)"
                  r"\s+to\s+((?:the\s+)?(?:City\s+of\s+Winter\s+Springs)?\s*[“\"][^”\"]{1,60}[”\"])", d)
    if m:
        out["from"] = sq(m.group(1).replace("“", "").replace("”", "").replace('"', ""))
        out["to"] = sq(m.group(2).replace("“", "").replace("”", "").replace('"', ""))
    return out


def _verdict(motion_text):
    t = motion_text
    if re.search(r"DENY|DENIAL", t):
        return "Denied"
    if re.search(r"CONTINU|TABLE|POSTPON|DEFER", t):
        return "Continued"
    if re.search(r"APPROV|ADOPT|RECOMMEND|PASS", t):
        return "Approved"
    return None


def minutes_results(text, item_keys):
    """{key: result} from draft minutes text; item_keys maps agenda item numbers to ordinance keys."""
    d = re.sub(r"\s+", "", text.upper()).replace("\u201c", '"').replace("\u201d", '"')
    res = {}
    # 1) explicit motions naming an item or ordinance
    for m in re.finditer(r"MOTIONTO(APPROVE|ADOPT|DENY|CONTINUE|TABLE|POSTPONE|DEFER|RECOMMEND(?:APPROVAL|DENIAL)?)"
                         r"(.{0,600}?)MOTION(CARRIED|PASSED|FAILED)", d):
        if m.group(3) == "FAILED":
            continue
        span = m.group(2)[:200]
        keys = [f"{x.group(1)}-{int(x.group(2)):02d}" for x in re.finditer(r"ORDINANCE(?:NO\.?)?(\d{4})-(\d{1,3})", span)]
        keys += [item_keys[x] for x in re.findall(r"ITEMS?(\d{3})", span) if x in item_keys]
        val = _verdict(m.group(1))
        for k in keys:
            if val:
                res[k] = val
    # 2) item sections ("400)FIRSTREADINGOFORDINANCE2025-04 ... MOTIONCARRIED5-0.")
    heads = list(re.finditer(r"(?<![\d,$.])(\d{3})[.)](?=[A-Z\"])", d))
    for i, h in enumerate(heads):
        seg = d[h.start(): heads[i + 1].start() if i + 1 < len(heads) else len(d)]
        keys = [f"{x.group(1)}-{int(x.group(2)):02d}" for x in re.finditer(r"ORDINANCE(?:NO\.?)?(\d{4})-(\d{1,3})", seg[:250])]
        if h.group(1) in item_keys:
            keys.append(item_keys[h.group(1)])
        keys = [k for k in dict.fromkeys(keys) if k not in res]
        if not keys:
            continue
        outs = list(re.finditer(r"MOTION(CARRIED|PASSED|FAILED)", seg))
        if not outs or outs[-1].group(1) == "FAILED":
            continue
        val = _verdict(seg[max(0, outs[-1].start() - 900): outs[-1].start()])
        if val:
            for k in keys:
                res[k] = val
    return res


def minutes_meeting(name):
    """('YYYY-MM-DD', body) from a minutes attachment name, else None."""
    if not re.search(r"Minutes", name, re.I):
        return None
    m = re.search(r"(20\d\d)[_\-. ](\d{1,2})[_\-. ](\d{1,2})", name)
    if m:
        y, mo, da = m.groups()
    else:
        m = re.search(r"(\d{1,2})[_\-. ](\d{1,2})[_\-. ](20\d\d)", name)
        if not m:
            return None
        mo, da, y = m.groups()
    try:
        d = dt.date(int(y), int(mo), int(da)).isoformat()
    except ValueError:
        return None
    if re.search(r"Planning|PZB|P&Z|LPA", name, re.I):
        return d, PZ
    if re.search(r"Workshop|Work[_ ]Session|(?<![A-Za-z])WS(?![A-Za-z])|Special|Districting|Board_of|Trustees|"
                 r"Committee|Code_Enforcement|Code Enforcement", name, re.I):
        return None
    return d, CC


# ------------------------------------------------------------------ fetch
def fetch(since, refresh):
    if pypdf is None:
        raise RuntimeError("pypdf is required for the Winter Springs adapter")
    today = dt.date.today()
    refresh_ids = {}
    for r in refresh or []:
        m = re.search(r"(\d{4})\s*-\s*(\d{1,3})\b", r or "")
        if m:
            refresh_ids[okey(m)] = r
    start_d = dt.date.fromisoformat(since) - dt.timedelta(days=LOOKBACK_DAYS)
    if refresh_ids:
        yr = min(int(k[:4]) for k in refresh_ids)
        start_d = min(start_d, max(dt.date(yr - 1, 9, 1), today - dt.timedelta(days=730)))
    meetings = [m for m in list_meetings() if m["agenda"] and m["date"] >= start_d.isoformat() and body_of(m["name"])]
    if not meetings:
        raise RuntimeError("no Winter Springs meetings found on Granicus")
    log(f"{len(meetings)} P&Z/Commission agendas since {start_d}")

    cases, minutes_att, item_map = {}, {}, {}
    for mt in sorted(meetings, key=lambda m: m["date"]):
        body = body_of(mt["name"])
        try:
            content = get(mt["agenda"]).content
            rdr = pdf_reader(content)
            text = "\n".join((p.extract_text() or "") for p in rdr.pages)
            links = pdf_links(rdr)
        except Exception as e:  # noqa: BLE001
            log(f"agenda {mt['date']} {body}: {e}")
            continue
        by_name = {}
        for ln in links:
            base = ln.rsplit("/", 1)[-1].split("?")[0]
            by_name.setdefault(norm_name(base), ln)
        for ln in links:
            mm = minutes_meeting(ln.rsplit("/", 1)[-1].split("?")[0])
            if mm:
                minutes_att.setdefault(mm, download_url(ln))
        for no, title, atts in agenda_items(text):
            c = classify(title)
            if not c:
                continue
            key, kind = c
            item_map.setdefault((body, mt["date"]), {})[no] = key
            att_links = [(a, download_url(by_name[norm_name(a)])) for a in atts if norm_name(a) in by_name]
            rd = re.search(r"\b(First|Second)\s+Reading", title, re.I)
            reading = rd.group(1).lower() if (rd and body == CC) else None
            cases.setdefault(key, {"kind": kind, "apps": []})["apps"].append(
                {"date": mt["date"], "body": body, "reading": reading, "title": title, "item": no,
                 "atts": att_links, "agenda": mt["agenda"], "packet": mt["packet"]})
    log(f"{len(cases)} in-scope ordinances; {len(minutes_att)} draft minutes attachments")

    results = {}
    for k in sorted({(a["body"], a["date"]) for c in cases.values() for a in c["apps"]}):
        u = minutes_att.get((k[1], k[0]))
        if not u:
            continue
        try:
            results[k] = minutes_results(pdf_text(get(u).content), item_map.get(k, {}))
        except Exception as e:  # noqa: BLE001
            log(f"minutes {k}: {e}")

    out = []
    for key, c in sorted(cases.items()):
        first = min(a["date"] for a in c["apps"])
        if first < since and key not in refresh_ids:
            continue
        try:
            out.append(build(key, c, results, today.isoformat()))
        except Exception as e:  # noqa: BLE001
            log(f"Ordinance {key}: skipped ({e})")
    for r in set(refresh_ids) - set(cases):
        log(f"refresh id {refresh_ids[r]} not found on any agenda since {start_d}")
    return out


def build(key, c, results, today):
    apps = sorted(c["apps"], key=lambda a: (a["date"], a["body"] == CC))
    latest = apps[-1]
    kind = c["kind"]
    staff, staff_url = {}, None
    for a in reversed(apps):
        cands = [u for n, u in a["atts"] if re.search(r"Staff\s*Report|Memo", n, re.I)]
        for u in cands[:1]:
            try:
                r = get(u)
                if len(r.content) <= MAX_ATT:
                    staff, staff_url = staff_fields(pdf_text(r.content, 6)), u
            except Exception as e:  # noqa: BLE001
                log(f"{key}: staff report: {e}")
        if staff:
            break
    title = max((a["title"] for a in apps), key=len)
    m = ORD_RE.search(title)
    subject = sq(title[m.end():]).strip(" :-,") if m else title
    am = re.search(r"located\s+at\s+(.+?)$", subject, re.I)
    address = staff.get("location") or (am.group(1).strip(" .") if am else None)
    is_pd = bool(re.search(r"\bPUD\b|Planned\s+Unit", subject + " " + (staff.get("to") or ""), re.I))
    category = "Comp Plan Amendment" if kind == "CPA" else ("PD" if is_pd else "Rezoning")
    if kind == "CPA":
        app_type = "Large-Scale FLUM Amendment" if re.search(r"Large", subject, re.I) else "Small-Scale FLUM Amendment"
    else:
        app_type = "PUD Rezoning" if is_pd else "Rezoning"
    f_, t_ = staff.get("from"), staff.get("to")
    f_, t_ = (f_.rstrip(" .,") if f_ else None), (t_.rstrip(" .,") if t_ else None)

    hearings = []
    for i, a in enumerate(apps):
        res = results.get((a["body"], a["date"]), {}).get(key)
        later = apps[i + 1:]
        if res is None and a["body"] == CC and a["reading"] == "first" and \
                any(b["body"] == CC and b["reading"] == "second" for b in later):
            res = "Approved"
        if a["body"] == CC and a["reading"] == "first" and res == "Approved":
            res = "Approved (first reading)"
        if a["body"] == PZ and res in ("Approved", "Denied"):
            res = "Recommended " + ("Approval" if res == "Approved" else "Denial")
        body = a["body"] + (f" ({a['reading']} reading)" if a["reading"] else "")
        h = {"body": body, "date": a["date"], "result": res}
        if h not in hearings:
            hearings.append(h)
    status, raw, decision = status_from(hearings, today)
    desc = subject
    if len(desc) > 300:
        desc = desc[:297] + "..."
    return {
        "jurisdiction": JUR, "county": COUNTY, "app_num": f"Ordinance {key}", "category": category,
        "app_type": app_type, "project_name": address, "description": desc or None,
        "applicant": staff.get("applicant"), "parcel_ids": staff.get("ids") or [], "address": address,
        "acres": staff.get("acres"),
        "from_zoning": f_ if kind == "RZ" else None, "to_zoning": t_ if kind == "RZ" else None,
        "from_flu": f_ if kind == "CPA" else None, "to_flu": t_ if kind == "CPA" else None,
        "filed_date": min(a["date"] for a in apps), "status": status, "status_raw": raw, "hearings": hearings,
        "decision_date": decision,
        "url": ("https:" + latest["agenda"]) if latest["agenda"].startswith("//") else latest["agenda"],
        "docs_url": staff_url or latest["packet"],
        "lat": None, "lon": None, "source": SOURCE,
    }


def status_from(hearings, today):
    past = [h for h in hearings if h["date"] <= today]
    future = [h for h in hearings if h["date"] > today]
    if any(h["result"] == "Withdrawn" for h in hearings):
        h = next(h for h in hearings if h["result"] == "Withdrawn")
        return "Withdrawn", f"{h['body']} {h['date']}: withdrawn", None
    final = [h for h in past if h["result"] in ("Approved", "Denied") and h["body"].startswith(CC)]
    if final:
        h = final[-1]
        return h["result"], f"{h['body']} {h['date']}: {h['result']} (minutes)", h["date"]
    if future:
        return "Hearing Scheduled", f"{future[0]['body']} {future[0]['date']}", None
    if past and past[-1]["result"] == "Continued":
        return "Continued", f"{past[-1]['body']} {past[-1]['date']}: continued", None
    if past:
        lp = past[-1]
        if lp["body"].startswith(CC) and lp["result"] is None:
            return "Hearing Scheduled", f"{lp['body']} {lp['date']} held; outcome not yet in published minutes", None
        return "Filed", f"{lp['body']} {lp['date']}" + (f": {lp['result']}" if lp["result"] else "") + \
            "; next hearing not yet on an agenda", None
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
