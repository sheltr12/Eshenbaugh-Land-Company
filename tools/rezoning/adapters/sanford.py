#!/usr/bin/env python3
"""City of Sanford (Seminole County) rezoning / PD / future land use map amendment adapter.

Source: the city's Granicus meeting portal (sanfordfl.granicus.com, ViewPublisher view_id=4, "Commission
Meetings"), whose agendas are produced by Legistar (Granicus "Peak"); there is no public Legistar Web API.
Sanford moved its City Commission agendas to Granicus in August 2026; earlier agendas, and all Planning &
Zoning Commission agendas, are not published in a machine-readable form (P&Z meetings are only posted as
AV Capture All videos behind an API key and a bot challenge), so cases are picked up when they reach the
City Commission.
 * Agenda items: "7.A CONTINUATION of First Reading of Ordinance No. 4865 rezoning 30.88 acres from Medium
   Density Residential MR-2, and Medium Density Residential MR-3, to Planned Development PD to establish a
   122-lot single-family subdivision at project address 401 Placid Lake Drive. [Memo No. 26-203]".
   app_num = "Ordinance No. NNNN" (the city writes it as 4865 or 26-4865). Kept: rezoning / PD rezoning /
   PD amendments and future land use map amendments. Skipped: annexation, vacations, LDR / text
   amendments, conditional uses, plats, site plans.
 * The "CC Memo" attachment gives applicant, owner, parcel numbers (02-20-30-300-0180-0000 style), and the
   Planning and Zoning Commission hearing date and recommendation ("On August 6, 2026 ... the Planning
   and Zoning Commission unanimously recommended ...").
 * Outcomes: draft minutes attached to later agendas ("09-28-2026 Regular Meeting Minutes.pdf":
   "Commissioner Britton moved to approve Ordinance No. 4868, which was seconded ... and carried").
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

JURISDICTIONS = ["City of Sanford"]
JUR = "City of Sanford"
COUNTY = "Seminole"
GRANICUS = "https://sanfordfl.granicus.com"
PUBLISHER = GRANICUS + "/ViewPublisher.php?view_id=4"
SOURCE = "Sanford Granicus/Legistar (City Commission agendas, memos and minutes)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
LOOKBACK_DAYS = 150
MAX_ATT = 15_000_000
PZ = "Planning and Zoning Commission"
CC = "City Commission"
ITEM_RE = re.compile(r"\n\s*(\d{1,2}\.[A-Z])\s*\n")

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


def log(*a):
    print("[sanford]", *a, file=sys.stderr)


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
    if re.search(r"CANCEL|Work\s*Session|Workshop|Budget\s+Workshop|Strategic|TEST", name, re.I):
        return None
    if re.search(r"City Commission", name, re.I):
        return CC
    return None


# ------------------------------------------------------------------ parsing
ORD_RE = re.compile(r"\bOrdinances?\s*(?:No\.?|Number)?\s*(?:(?:20)?\d{2}\s*-\s*)?(\d{4})\b(?!\s*-\s*\d)", re.I)
CPA_RE = re.compile(r"Future\s+Land\s+Use|Comprehensive\s+Plan\s+(?:Map\s+)?Amendment|Small[- ]Scale|Large[- ]Scale|\bFLUM?\b|"
                    r"land\s+use\s+designation", re.I)
RZ_RE = re.compile(r"Rezon|Zoning\s+(?:District|Map)|Planned\s+Development|\bPD\b|Planned\s+Unit\s+Development|\bPUD\b", re.I)
OUT_RE = re.compile(r"\bAnnex|Vacat|Land\s+Development\s+Regulations|\bLDR\b|Schedule\s+[A-Z]\b|Text\s+Amendment|Chapter\s+\d|"
                    r"Conditional\s+Use|Special\s+Exception|Plat\b|Site\s+Plan|Evaluation\s+and\s+Appraisal|\bEAR\b|"
                    r"Capital\s+Improvement|Budget|Millage|Franchise|Impact\s+Fee", re.I)


def okey(m):
    return m.group(1)


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
    if OUT_RE.search(rest[:160]):
        return None
    if CPA_RE.search(rest[:200]):
        return okey(m), "CPA"
    if RZ_RE.search(rest[:200]):
        return okey(m), "RZ"
    return None


def title_fields(title):
    t = sq(title)
    out = {}
    m = re.search(r"([\d,]*\.?\d+)\s*(?:\+/-|\u00b1)?\s*acres", t, re.I)
    if m:
        try:
            out["acres"] = float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    m = re.search(r"\bfrom\s+(.{2,140}?),?\s+to\s+(.{2,100}?)(?:\s+to\s+(?:establish|allow|permit|develop)|\s+for\s+|\s+at\s+|;|\s*\[|"
                  r"\.\s|$)", t, re.I)
    if m:
        out["from"], out["to"] = sq(m.group(1)).strip(" ,"), sq(m.group(2)).strip(" ,.")
    m = re.search(r"(?:project\s+)?address(?:es)?\s+(?:of\s+)?(\d[^;\[]{3,80}?)(?:\s+from\s|;|\s*\[|\.\s|,\s*Owner|$)", t, re.I)
    if m:
        out["address"] = m.group(1).strip(" ,.")
    m = re.search(r"Owner:\s*([^\[;]{3,80}?)\s*(?:\[|;|$)", t)
    if m:
        out["owner"] = m.group(1).strip(" ,.")
    return out


PID_RE = re.compile(r"\b\d{2}\s*-\s*\d{2}\s*-\s*\d{2}\s*-\s*[0-9A-Z]{3}\s*-\s*[0-9A-Z]{4}\s*-\s*[0-9A-Z]{4}\b")


def staff_fields(text):
    d = sq(text)
    out = {}
    out["ids"] = list(dict.fromkeys(re.sub(r"\s+", "", x) for x in PID_RE.findall(d)))
    m = re.search(r"The\s+Applicant\s+is\s+(.{3,120}?),?\s+who\s+(?:has|is)", d, re.I) or \
        re.search(r"Applicant/Agent:\s*(.{3,80}?)\s+\d", d)
    if m:
        out["applicant"] = m.group(1).strip(" ,")
    m = re.search(r"The\s+property\s+is\s+owned\s+by\s+(.{3,120}?)\.", d, re.I)
    if m:
        out["owner"] = m.group(1).strip(" ,")
    pz = []
    for m in re.finditer(r"On\s+(\w+\s+\d{1,2},\s+\d{4}),[^.]{0,200}?Planning\s+and\s+Zoning\s+Commission\s+(?:\w+\s+){0,3}?"
                         r"(recommended|voted|denied|continued|tabled)\b([^.]{0,200})", d, re.I):
        try:
            pdate = dt.datetime.strptime(m.group(1), "%B %d, %Y").date().isoformat()
        except ValueError:
            continue
        w = (m.group(2) + " " + m.group(3)).lower()
        pz.append((pdate, "Recommended Denial" if re.search(r"\bden", w) else
                   "Continued" if re.search(r"continu|tabl", w) else "Recommended Approval"))
    out["pz"] = sorted(set(pz))
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


MOTION_RE = re.compile(r"(?:motion|moved)\b.{0,250}?\bto\s+(approv\w*|adopt\w*|den\w+|tabl\w*|continu\w*|postpon\w*|"
                       r"defer\w*)\b", re.I)


def minutes_results(text, item_keys):
    """{key: result} from minutes text ("... moved to approve Ordinance No. 4868 ... carried by vote")."""
    d = sq(text)
    res = {}
    for m in MOTION_RE.finditer(d):
        after = d[m.end(): m.end() + 900]
        o = re.search(r"\b(carried|passed|failed)\b", after, re.I)
        if not o or o.group(1).lower() == "failed":
            continue
        keys = [okey(x) for x in ORD_RE.finditer(after[:min(o.start(), 300)])]
        v = m.group(1).lower()
        val = ("Continued" if v.startswith(("tabl", "contin", "postp", "defer")) else
               "Denied" if v.startswith("den") else "Approved")
        for k in keys:
            res[k] = val
    for m in re.finditer(r"withdr[ae]w\w*[^.]{0,150}?Ordinance\s*(?:No\.?)?\s*(?:\d{2}\s*-\s*)?(\d{4})", d, re.I):
        res[m.group(1)] = "Withdrawn"
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
        raise RuntimeError("pypdf is required for the Sanford adapter")
    today = dt.date.today()
    refresh_ids = {}
    for r in refresh or []:
        m = re.search(r"(\d{4})\s*$", (r or "").strip())
        if m:
            refresh_ids[m.group(1)] = r
    start_d = dt.date.fromisoformat(since) - dt.timedelta(days=LOOKBACK_DAYS)
    if refresh_ids:
        start_d = min(start_d, today - dt.timedelta(days=730))
    meetings = [m for m in list_meetings() if m["agenda"] and m["date"] >= start_d.isoformat() and body_of(m["name"])]
    if not meetings:
        raise RuntimeError("no Sanford meetings found on Granicus")
    log(f"{len(meetings)} City Commission agendas since {start_d}")

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
            notes = ""
            am = re.search(r"([\d,]*\.?\d+)\s*(?:\+/-\s*)?acres", title, re.I)
            if am:
                flat = sq(text)
                for sm in re.finditer(r"On\s+\w+\s+\d{1,2},\s+\d{4},[^.]{0,250}?Planning\s+and\s+Zoning\s+Commission(?:[^.]|\.(?=\d)){0,400}",
                                      flat, re.I):
                    if am.group(1) in sm.group(0):
                        notes += " " + sm.group(0) + "."
            cases.setdefault(key, {"kind": kind, "apps": []})["apps"].append(
                {"date": mt["date"], "body": body, "reading": reading, "title": title, "item": no,
                 "atts": att_links, "agenda": mt["agenda"], "packet": mt["packet"], "notes": notes})
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
            log(f"Ordinance No. {key}: skipped ({e})")
    for r in set(refresh_ids) - set(cases):
        log(f"refresh id {refresh_ids[r]} not found on any agenda since {start_d}")
    return out


def build(key, c, results, today):
    apps = sorted(c["apps"], key=lambda a: a["date"])
    latest = apps[-1]
    kind = c["kind"]
    staff, staff_url = {}, None
    for a in reversed(apps):
        cands = [u for n, u in a["atts"] if re.search(r"Memo", n, re.I)] or \
                [u for n, u in a["atts"] if re.search(r"Staff\s*Report|Project\s*Information", n, re.I)]
        for u in cands[:1]:
            try:
                r = get(u)
                if len(r.content) <= MAX_ATT:
                    staff, staff_url = staff_fields(pdf_text(r.content, 6)), u
            except Exception as e:  # noqa: BLE001
                log(f"Ordinance No. {key}: memo: {e}")
        if staff:
            break
    notes = " ".join(a.get("notes") or "" for a in apps)
    if notes.strip():
        staff["pz"] = sorted(set(staff.get("pz", []) + staff_fields(notes).get("pz", [])))
    title = max((a["title"] for a in apps), key=len)
    tf = title_fields(re.sub(r"(\w)-\s+(\w)", r"\1-\2", title))
    m = ORD_RE.search(title)
    subject = sq(title[m.end():]).strip(" :-,;") if m else title
    subject = re.sub(r"\s*\[Memo[^\]]*\].*$", "", subject, flags=re.I | re.S)
    subject = re.sub(r"(\w)-\s+(\w)", r"\1-\2", subject)
    subject = re.sub(r"^(?:Request\s+to\s+)", "", subject, flags=re.I)
    subject = subject[:1].upper() + subject[1:] if subject else subject
    is_pd = bool(re.search(r"\bPD\b|Planned\s+Development|\bPUD\b", (tf.get("to") or "") + " " + subject, re.I))
    category = "Comp Plan Amendment" if kind == "CPA" else ("PD" if is_pd else "Rezoning")
    if kind == "CPA":
        app_type = "Large-Scale FLUM Amendment" if re.search(r"Large", subject, re.I) else "Small-Scale FLUM Amendment"
    else:
        app_type = ("PD Amendment" if re.search(r"amend\w*\s+(?:the\s+)?.{0,40}(?:\bPD\b|Planned\s+Development)", subject, re.I)
                    else ("PD Rezoning" if is_pd else "Rezoning"))

    hearings = []
    first_cc = min(a["date"] for a in apps)
    lo = (dt.date.fromisoformat(first_cc) - dt.timedelta(days=270)).isoformat()
    pz = [p for p in staff.get("pz", []) if lo <= p[0] <= first_cc]
    if pz:
        staff["pz_date"] = pz[-1][0]
        hearings.append({"body": PZ, "date": pz[-1][0], "result": pz[-1][1]})
    for i, a in enumerate(apps):
        res = results.get((a["body"], a["date"]), {}).get(key)
        later = apps[i + 1:]
        if res is None and a["reading"] == "first" and any(b["reading"] == "second" for b in later):
            res = "Approved"
        if res is None and a["date"] <= today and any(b["reading"] == a["reading"] for b in later):
            res = "Continued"
        if a["reading"] == "first" and res == "Approved":
            res = "Approved (first reading)"
        body = a["body"] + (f" ({a['reading']} reading)" if a["reading"] else "")
        h = {"body": body, "date": a["date"], "result": res}
        if h not in hearings:
            hearings.append(h)
    hearings.sort(key=lambda h: h["date"])
    status, raw, decision = status_from(hearings, today)
    desc = subject
    if len(desc) > 300:
        desc = desc[:297] + "..."
    filed = min([a["date"] for a in apps] + ([staff["pz_date"]] if staff.get("pz_date") else []))
    return {
        "jurisdiction": JUR, "county": COUNTY, "app_num": f"Ordinance No. {key}", "category": category,
        "app_type": app_type, "project_name": tf.get("address"), "description": desc or None,
        "applicant": staff.get("applicant") or tf.get("owner") or staff.get("owner"),
        "parcel_ids": staff.get("ids") or [], "address": tf.get("address"), "acres": tf.get("acres"),
        "from_zoning": tf.get("from") if kind == "RZ" else None, "to_zoning": tf.get("to") if kind == "RZ" else None,
        "from_flu": tf.get("from") if kind == "CPA" else None, "to_flu": tf.get("to") if kind == "CPA" else None,
        "filed_date": filed, "status": status, "status_raw": raw, "hearings": hearings,
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
