"""City of Bradenton (Manatee County) - Accela Citizen Access (agency BRADENTON, module Planning)
plus the city's Granicus agendas (cityofbradenton.granicus.com) for hearing dates and details.

Accela record types (search-form "Record Type" dropdown) and mapping:
  Planning/Zoning Map Amendment/NA/NA                    "Zoning Map Amendment" (PLN-ZON-yy-nnnn)
        -> PD        when the ordinance designates a Planned Development (PDP) zoning category
        -> Rezoning  otherwise
  Planning/Comprehensive Plan Amendment/Small Scale/NA   (PLN-CPS-)  -> Comp Plan Amendment
  Planning/Comprehensive Plan Amendment/Large Scale/NA   (PLN-CPL-)  -> Comp Plan Amendment
  Planning/PDP/Preliminary/NA     "Preliminary Planned Development Project"          -> PD
  Planning/PDP/Major Amendment/NA "Major Planned Development Project Amendment" (PLN-MAJ-) -> PD
Out of scope / not searched: Final PDP (final site plan), Minor PDP Amendment (administrative),
  Annexation Request (the concurrent CPS / ZON records are kept), Variances, Special Use, Plats,
  Zoning Text Amendment, Form Based Code reviews, Site Improvement Permits, etc.

The Accela records carry case number, status, parcel and owner, but little else and no hearing
dates. Planning Commission and City Council agendas (Granicus AgendaViewer HTML) add:
  * "ANNOUNCEMENT OF CITY COUNCIL PUBLIC HEARING DATES" tables on Planning Commission agendas:
    "Ord. 4084 | PLN-ZON-26-0004 | 2520 13th St. W. | Humane Society | Zoning Map Amendment |
    1st Reading (Tentative)" under "City Council Meeting - November 18, 2026:" (ordinance <-> case
    number, project name, scheduled City Council readings);
  * agenda items "ORDINANCE 4084 - PUBLIC HEARING" (Planning Commission) and "ORDINANCE 4070 -
    FIRST READING" / "- SECOND READING AND PUBLIC HEARING" (City Council) with the ordinance
    title (acreage, parcel ids, owner, from/to zoning or future land use).
Outcomes: the Accela status (Approved / Denied / Closed-Withdrawn). Planning Commission
recommendations and first readings carry no result (minutes are PDFs and not parsed).
Parcel IDs: 10-digit Manatee PA ids as posted (e.g. 4486300009); Accela's placeholder 9999999999
is dropped.
"""
import datetime as dt
import html as _html
import os
import re
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import accela_core as C  # noqa: E402

JURISDICTIONS = ["City of Bradenton"]
GRANICUS = "https://cityofbradenton.granicus.com"
PC, CC = "Planning Commission", "City Council"

CFG = {
    "agency": "BRADENTON", "module": "Planning", "tab": "Planning",
    "jurisdiction": "City of Bradenton", "county": "Manatee",
    "source": "Bradenton Accela + Granicus agendas",
    "types": {
        "Planning/Zoning Map Amendment/NA/NA": None,
        "Planning/Comprehensive Plan Amendment/Small Scale/NA": "Comp Plan Amendment",
        "Planning/Comprehensive Plan Amendment/Large Scale/NA": "Comp Plan Amendment",
        "Planning/PDP/Preliminary/NA": "PD",
        "Planning/PDP/Major Amendment/NA": "PD",
    },
    "chunk_days": 400,
}

CASE_RX = re.compile(r"\bPLN-[A-Z]{2,4}-\d{2}-\d{4}\b")
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
_g = requests.Session()
_g.headers.update({"User-Agent": C.UA})
_AG = {"start": None, "data": None}


def log(*a):
    print("[bradenton]", *a, file=sys.stderr, flush=True)


def _s3_path_style(u):
    """granicus_production_attachments.s3.amazonaws.com has an underscore, so its TLS certificate
    does not match virtual-host style URLs; the path-style URL serves the same object."""
    m = re.match(r"https?://([a-z0-9_.-]+)\.s3[.-]?([a-z0-9-]*)\.?amazonaws\.com/(.*)", u or "")
    if m and "_" in m.group(1):
        return f"https://s3.amazonaws.com/{m.group(1)}/{m.group(3)}"
    return u


def gget(url, tries=3):
    last = None
    for i in range(tries):
        try:
            r = _g.get(url, timeout=90, allow_redirects=False)
            hops = 0
            while r.is_redirect and hops < 5:
                url2 = _s3_path_style(requests.compat.urljoin(r.url, r.headers.get("Location", "")))
                r = _g.get(url2, timeout=90, allow_redirects=False)
                hops += 1
            r.raise_for_status()
            time.sleep(0.4)
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


def html_text(h):
    t = re.sub(r"<script.*?</script>|<style.*?</style>", "", h, flags=re.S | re.I)
    t = re.sub(r"<sup>(.*?)</sup>", r"\1", t, flags=re.S | re.I)
    t = _html.unescape(re.sub(r"<[^>]+>", "\n", t)).replace("\xa0", " ")
    return re.sub(r"[ \t]*\n\s*", "\n", t)


def sq(s):
    return re.sub(r"\s+", " ", s or "").strip()


def mdy_long(s):
    try:
        return dt.datetime.strptime(sq(s).replace(",", ""), "%B %d %Y").date().isoformat()
    except ValueError:
        return None


# ---------------------------------------------------------------- Granicus agendas
def meetings(start, end):
    h = gget(f"{GRANICUS}/ViewPublisher.php?view_id=1").text
    out = []
    for row in re.findall(r'<tr class="listingRow".*?</tr>', h, re.S):
        t = sq(_html.unescape(re.sub(r"<[^>]+>", " ", row)))
        m = re.match(r"(.*?)\s+(\w{3}) (\d{1,2}), (\d{4})", t)
        a = re.search(r'href="([^"]*AgendaViewer\.php[^"]*)"', row)
        if not m or not a:
            continue
        name = m.group(1)
        if re.search(r"Planning Commission", name, re.I):
            body = PC
        elif re.search(r"^City Council(?! (Workshop|Budget))", name, re.I):
            body = CC
        else:
            continue
        try:
            d = dt.datetime.strptime(f"{m.group(2)} {m.group(3)} {m.group(4)}", "%b %d %Y").date()
        except ValueError:
            continue
        if not (start <= d <= end):
            continue
        u = a.group(1).replace("&amp;", "&")
        out.append({"body": body, "date": d.isoformat(), "url": ("https:" + u) if u.startswith("//") else u})
    return out


def parse_agenda(mt, text, data):
    """Fill data: ords {ord: {...}}, cases {case: {...}}."""
    # 1) hearing-date announcement tables
    cur = None
    for line in text.split("\n"):
        m = re.search(rf"City Council Meeting\s*[-:\u2013]\s*((?:{MONTHS})\s+\d{{1,2}},\s*\d{{4}})", line)
        if m:
            cur = mdy_long(m.group(1))
            continue
        m = re.match(r"\s*(Ord|Res)\.?\s*(?:No\.?\s*)?([\d-]{3,8})\s*\|\s*(PLN-[A-Z]+-\d{2}-\d{4})\s*\|(.*)$", line, re.I)
        if m and cur:
            o, case = m.group(2), m.group(3)
            f = [sq(x) for x in m.group(4).split("|")]
            rd = f[-1] if len(f) >= 2 and re.search(r"reading|hearing|tentative", f[-1], re.I) else None
            if rd:
                f = f[:-1]
            addr = f[0] if f else None
            name = f[1] if len(f) >= 3 else None
            if name and re.search(r"amendment|annexation|rezon|comp\.? plan|zoning|variance|special use", name, re.I):
                name = None
            kind = f[-1] if len(f) >= 2 else None
            data["case_ord"].setdefault(case, set()).add(o)
            c = data["cases"].setdefault(case, {"name": None, "addr": None, "kind": None})
            c["name"] = c["name"] or name or None
            c["addr"] = c["addr"] or addr or None
            c["kind"] = c["kind"] or kind or None
            data["sched"].setdefault(o, []).append({"body": CC, "date": cur, "reading": rd, "src": mt["url"]})
    # 2) agenda items
    parts = re.split(r"\n[a-z]\)\n", "\n" + text)
    for p in parts[1:]:
        m = re.search(r"(?:^|\n)(?:Quasi-Judicial:\s*\n?)?(ORDINANCE|FINAL ORDER|RESOLUTION)\s+(?:NO\.?\s*)?"
                      r"([A-Z0-9-]+)\s*-\s*([^\n]+)\n(.+?)(?:\n-->|\nMotion|\nNo action|$)", p, re.I | re.S)
        if not m:
            continue
        kind, num, head, title = m.group(1).upper(), m.group(2).upper(), sq(m.group(3)), sq(m.group(4))
        cases = set(CASE_RX.findall(m.group(0)))
        if kind in ("ORDINANCE", "RESOLUTION"):
            o = num
            ent = data["ords"].setdefault(o, {"title": None, "apps": []})
            ent["apps"].append({"body": mt["body"], "date": mt["date"], "head": head, "url": mt["url"], "title": title})
            for c in cases:
                data["case_ord"].setdefault(c, set()).add(o)
        else:
            for c in cases:
                ent = data["ords"].setdefault(c, {"title": None, "apps": []})
                ent["apps"].append({"body": mt["body"], "date": mt["date"], "head": head, "url": mt["url"], "title": title})
                data["case_ord"].setdefault(c, set()).add(c)


def agenda_data(start):
    if _AG["data"] is not None and _AG["start"] <= start:
        return _AG["data"]
    data = {"ords": {}, "cases": {}, "case_ord": {}, "sched": {}}
    today = dt.date.today()
    try:
        mts = meetings(start, today + dt.timedelta(days=60))
    except Exception as e:  # noqa: BLE001
        log(f"agenda list failed: {e}")
        mts = []
    log(f"{len(mts)} Planning Commission / City Council agendas since {start}")
    for mt in sorted(mts, key=lambda x: x["date"]):
        try:
            r = gget(mt["url"])
            mt["url"] = r.url if r.url.startswith("http") else mt["url"]
            parse_agenda(mt, html_text(r.text), data)
        except Exception as e:  # noqa: BLE001
            log(f"agenda {mt['date']} {mt['body']}: {e}")
    # The city sometimes re-numbers a pending ordinance (e.g. a number first used on a Planning
    # Commission agenda for one parcel is later given to another case). The latest appearance's
    # title is taken as the ordinance's; earlier appearances with a different title are dropped.
    for o, ent in data["ords"].items():
        apps = sorted(ent["apps"], key=lambda a: a["date"])
        canon = next((a["title"] for a in reversed(apps) if a.get("title")), None)
        ent["title"] = canon
        ent["apps"] = [a for a in apps if not a.get("title") or not canon or _similar(a["title"], canon)]
    _AG["start"], _AG["data"] = start, data
    return data


def _words(t):
    return set(re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", (t or "").lower()))


def _similar(a, b, thr=0.8):
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return True
    return len(wa & wb) / float(len(wa | wb)) >= thr


# ---------------------------------------------------------------- ordinance title parsing
def parse_title(t):
    out = {}
    u = sq(t)
    m = re.search(r"CONTAINING\s+(?:APPROXIMATELY\s+)?([\d,]*\.?\d+)\s*(?:\+/-\s*)?ACRES", u, re.I)
    if m:
        out["acres"] = C.to_float(m.group(1))
    pids = []
    for grp in re.findall(r"PARCEL\s+(?:ID|I\.D\.)\s*(?:NOS?\.?|NUMBERS?)?\s*([0-9 ,AND&]+)", u, re.I):
        pids += re.findall(r"\d{9,10}", grp)
    out["pids"] = list(dict.fromkeys(pids))
    m = re.search(r"OWNED BY\s+(?:THE\s+)?(.+?)(?:,\s*(?:AND|UNDER|TO|FROM)\b|;|\s+UNDER THE|\s+TO THE)", u, re.I)
    if m:
        out["owner"] = m.group(1).strip(" ,")
    m = re.search(r"FROM\s+(.+?)\s+TO\s+(.+?)(?:,|;|\s+FOR THE PROPERT|\s+FOR PROPERT|\s+ON\s)", u, re.I)
    if m and len(m.group(1)) < 90 and len(m.group(2)) < 90:
        out["from"], out["to"] = _desig(m.group(1)), _desig(m.group(2))
    for kind, rx in (("to_z", r"ZONING"), ("to_f", r"FUTURE LAND USE")):
        m = re.search(rf"(?:UNDER|TO) THE\s+([^;,]+?)\s+{rx}\s+CATEGORY", u, re.I)
        if m:
            out[kind] = m.group(1).strip(" ,")
    m = re.search(r"LOCATED AT\s+(.+?)(?:\s*\(PARCEL|,\s*BRADENTON|;|\s+CONTAINING)", u, re.I)
    if m:
        out["address"] = m.group(1).strip(" ,")
    return out


def _desig(v):
    v = re.sub(r"^(?:THE|A)\s+", "", (v or "").strip(" ,"), flags=re.I)
    v = re.sub(r"\s+(?:FUTURE LAND USE|ZONING)(?:\s+(?:CATEGORY|DESIGNATION|DISTRICT))?$", "", v, flags=re.I)
    return v.strip(" ,") or None


def _reading_result(head, body, date, today):
    h = (head or "").lower()
    if body == CC and "first reading" in h:
        return "First reading" if date < today else None
    return None


# ---------------------------------------------------------------- Accela build
def _kind(row, d):
    t = row.get("_tval")
    if t:
        return t
    ty = (d.get("type") or row.get("type") or "").lower()
    if "zoning map" in ty:
        return "Planning/Zoning Map Amendment/NA/NA"
    if "comprehensive plan" in ty:
        return "Planning/Comprehensive Plan Amendment/Small Scale/NA"
    if "major planned development" in ty:
        return "Planning/PDP/Major Amendment/NA"
    if "preliminary planned development" in ty:
        return "Planning/PDP/Preliminary/NA"
    return None


def build(row, d, aca):
    k = _kind(row, d)
    if k is None:
        return None
    cat = CFG["types"].get(k) or "Rezoning"
    rec = C.default_record(row, d, CFG, cat)
    num = rec["app_num"]
    today = dt.date.today().isoformat()
    data = agenda_data(_AG["start"] or (dt.date.today() - dt.timedelta(days=400)))
    ords = sorted(data["case_ord"].get(num, set()))
    info, hs, urls = {}, [], []
    for o in ords:
        ent = data["ords"].get(o) or {}
        if ent.get("title"):
            for kk, vv in parse_title(ent["title"]).items():
                if vv and not info.get(kk):
                    info[kk] = vv
            info.setdefault("title", ent["title"])
        for a in ent.get("apps", []):
            hs.append({"body": a["body"], "date": a["date"], "result": _reading_result(a["head"], a["body"], a["date"], today)})
            urls.append((a["date"], a["url"]))
        for s in data["sched"].get(o, []):
            if not any(h["body"] == s["body"] and h["date"] == s["date"] for h in hs):
                hs.append({"body": s["body"], "date": s["date"], "result": None})
    hs = C.dedup_hearings(hs)
    c = data["cases"].get(num, {})
    rd = d.get("record_details", {})
    pdesc = rd.get("Project Description")
    rec["project_name"] = c.get("name") or rec.get("project_name")
    rec["description"] = C.one_line(pdesc) or (info.get("title")[:400] if info.get("title") else None)
    if ords and not CASE_RX.fullmatch(ords[0]):
        lab = ", ".join(("Resolution " if "-" in o else "Ordinance ") + o for o in ords)
        rec["app_type"] = f"{rec['app_type']} ({lab})"
    rec["applicant"] = rd.get("Applicant") or C.first_contact(d, r"applicant|agent") or rd.get("Owner") or info.get("owner")
    pids = [p for p in d.get("parcels", []) if re.fullmatch(r"\d{9,10}", p or "") and not re.fullmatch(r"9+|0+", p)]
    for p in info.get("pids", []):
        if p not in pids:
            pids.append(p)
    rec["parcel_ids"] = pids
    rec["acres"] = info.get("acres") or C.acres_from_text(pdesc)
    rec["address"] = rec.get("address") or c.get("addr") or info.get("address")
    if row.get("address") and not rec.get("address"):
        rec["address"] = C.clean_address(row["address"])
    fz, tz = C.zoning_from_text(pdesc) if "Zoning Map" in k else (None, None)
    is_flum = bool(re.search(r"FUTURE LAND USE MAP", info.get("title") or "", re.I))
    if "Zoning Map" not in k and cat != "Comp Plan Amendment":
        info.pop("from", None)
        info.pop("to", None)
    if cat == "Comp Plan Amendment":
        rec["from_flu"] = info.get("from") if is_flum else None
        rec["to_flu"] = (info.get("to") if is_flum else None) or info.get("to_f")
    else:
        rec["from_zoning"] = (info.get("from") if not is_flum else None) or fz
        rec["to_zoning"] = (info.get("to") if not is_flum else None) or info.get("to_z") or tz
        if re.search(r"planned development|\bPDP\b|\bPD\b", rec["to_zoning"] or "", re.I) or \
                re.search(r"PLANNED DEVELOPMENT PROJECT ZONING", info.get("title") or "", re.I):
            rec["category"] = "PD"
    rec["hearings"] = hs
    raw = rec["status_raw"] or ""
    st = C.norm_status(raw, hs, final_bodies=(CC,), raw_map={
        r"^Scheduled": "Hearing!",
        r"^Closed-Withdrawn|Withdrawn|Void|Expired": "Withdrawn",
        r"^Closed$": None,
        r"^(New|Submitted|In Review|Under Review|Ready|Pending|Comments|Additional Info)": None,
    })
    if st == "Filed" and re.search(r"^Scheduled", raw, re.I):
        st = "Hearing Scheduled"
    rec["status"] = st
    dec = None
    if st in ("Approved", "Denied"):
        past_cc = [h["date"] for h in hs if h["body"] == CC and h["date"] <= today]
        if past_cc:
            dec = max(past_cc)
        else:
            ds = [e["date"] for t in d.get("workflow", []) for e in t.get("entries", [])
                  if e.get("date") and re.search(r"approv|den", e.get("status") or "", re.I)]
            dec = max(ds) if ds else None
        if dec:
            for h in hs:
                if h["body"] == CC and h["date"] == dec and not h.get("result"):
                    h["result"] = st
    rec["decision_date"] = dec
    if urls:
        rec["docs_url"] = sorted(urls)[-1][1]
    return rec


_inner = C.make_fetch(CFG, build)


def fetch(since, refresh=None):
    today = dt.date.today()
    start = dt.date.fromisoformat(since) - dt.timedelta(days=150)
    if refresh:
        start = min(start, today - dt.timedelta(days=400))
    _AG["start"], _AG["data"] = start, None
    return _inner(since, refresh or [])


if __name__ == "__main__":
    C.run_cli(fetch)
