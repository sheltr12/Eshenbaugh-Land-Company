#!/usr/bin/env python3
"""City of Dunedin (Pinellas) land use plan amendments and rezonings (Granicus public meeting pages).

Dunedin's Legistar has no public web API, so this reads the public Granicus meeting site
(dunedin.granicusideas.com): the meeting list, each meeting's agenda items, and each case item's
page, which links the Legistar PDFs (application, staff report, ordinance, draft minutes).
Agenda items read like
  "3.a) Application LUP/ZO-2026-0002 - Request to rezone the 1.07-acre property located at
   968 Douglas Avenue from Downtown Industrial (DI) to Downtown Core (DC). Second Reading of
   Ordinance 26-09 - Rezoning from Downtown Industrial (DI) to Downtown Core (DC)"
app_num = "LUP/ZO-2026-0002" (prefixes: AN annexation, LUP land use plan, ZO zoning, PRD/PUD planned
development). Kept when the prefixes include LUP, ZO, PRD or PUD. Bodies: Local Planning Agency
(advisory) and City Commission (first and second reading). Outcomes come from draft minutes PDFs
(Legistar downloads attached to later agenda items, e.g. "Approve the Minutes for the 7/9/2026
Regular Meeting"); a first reading followed by a second reading is treated as passed. Parcel
numbers come from the ordinance / application PDFs or the site address (Pinellas PA layer).
"""
import datetime as dt
import html as _html
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pinellas_common as P  # noqa: E402

JURISDICTIONS = ["City of Dunedin"]
JUR = "City of Dunedin"
BASE = "https://dunedin.granicusideas.com"
SOURCE = "Dunedin Granicus meeting pages (Local Planning Agency + City Commission)"
P.set_tag("dunedin")
log = P.log
LPA, CC = "Local Planning Agency", "City Commission"
MAX_PDF = 6_000_000

CASE_RE = re.compile(r"\b((?:AN|LUP|LU|ZO|PRD|PUD|CPA|SD|LDO|DR|SP)(?:\s*/\s*(?:AN|LUP|LU|ZO|PRD|PUD|CPA|SD|LDO|DR|SP))*)"
                     r"\s*[- ]\s*(\d{4}|\d{2})\s*-\s*(\d{2,4})\b")
ORD_RE = re.compile(r"\bOrdinance\s+(?:No\.?\s*)?(\d{2})-(\d{2})\b", re.I)
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"


def clean(s):
    return P.sq(_html.unescape(re.sub(r"<[^>]+>", " ", s or "")))


def meetings():
    out = {}
    for url in (f"{BASE}/meetings?scope=past", f"{BASE}/meetings"):
        h = P.get(url).text
        for sec in re.split(r"<section\s+class=\"meeting\"", h)[1:]:
            m = re.search(r'id="(\d+)-([a-z0-9-]+)"', sec)
            t = re.search(rf"Meeting Time:\s*({MONTHS})\s+(\d{{1,2}}),\s+(\d{{4}})", sec)
            if not m or not t:
                continue
            d = dt.datetime.strptime(f"{t.group(1)} {t.group(2)} {t.group(3)}", "%B %d %Y").date().isoformat()
            out[m.group(1)] = {"id": m.group(1), "slug": m.group(2), "date": d}
    return list(out.values())


def body_of(slug):
    if "local-planning" in slug:
        return LPA
    if re.search(r"city-commission-(regular|special)", slug):
        return CC
    return None


def agenda_items(mt):
    items, seen = [], set()
    for page in range(1, 6):
        h = P.get(f"{BASE}/meetings/{mt['id']}-{mt['slug']}/agenda_items", params={"page": page}).text
        for u, t in re.findall(r'href="(/meetings/%s[^"]*/agenda_items/[^"]+)"[^>]*>(.*?)</a>' % mt["id"], h, re.S):
            t = clean(t)
            if u in seen or not t:
                continue
            seen.add(u)
            items.append({"url": BASE + u, "title": t})
        if f"page={page + 1}" not in h:
            break
    return items


def item_pdfs(url):
    try:
        h = P.get(url).text
    except Exception as e:  # noqa: BLE001
        log(f"item page {url[-60:]}: {e}")
        return []
    return list(dict.fromkeys(re.findall(r'href="(https://dunedin\.granicus\.com/services/legistar/download/pdf/\d+/[^"]+\.pdf)"', h)))


def case_key(t):
    for m in CASE_RE.finditer(t or ""):
        pre = re.sub(r"\s+", "", m.group(1)).upper()
        if not re.search(r"\b(LUP|LU|ZO|PRD|PUD|CPA)\b", pre.replace("/", " ")):
            continue
        yr = m.group(2) if len(m.group(2)) == 4 else "20" + m.group(2)
        num = m.group(3).zfill(4) if len(m.group(2)) == 4 else m.group(3).zfill(2)
        return f"{pre}-{yr}-{num}", pre
    return None, None


def minutes_index(items, mt):
    """Draft minutes PDFs attached to an agenda item: {(body, meeting date): url}."""
    out = {}
    for it in items:
        if not re.search(r"minutes", it["title"], re.I):
            continue
        for u in item_pdfs(it["url"]):
            fn = u.rsplit("/", 1)[-1]
            if not re.search(r"minutes", fn, re.I):
                continue
            m = re.search(r"(\d{1,2})[-_.](\d{1,2})[-_.](\d{4}|\d{2})(?!\d)", fn)
            if not m or re.search(r"_WS_|Workshop|Budget", fn, re.I):
                continue
            yr = m.group(3) if len(m.group(3)) == 4 else "20" + m.group(3)
            d = f"{yr}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
            body = LPA if re.search(r"LPA|Planning", fn, re.I) else CC
            out.setdefault((body, d), u)
    return out


def fetch(since, refresh):
    today = dt.date.today()
    start = (dt.date.fromisoformat(since) - dt.timedelta(days=60))
    refresh = {r.strip().upper() for r in (refresh or []) if r and r.strip()}
    if refresh:
        start = min(start, today - dt.timedelta(days=540))
    mts = [m for m in meetings() if body_of(m["slug"]) and start.isoformat() <= m["date"] <= (today + dt.timedelta(days=75)).isoformat()]
    log(f"{len(mts)} meetings since {start}")
    cases, minutes = {}, {}
    for mt in sorted(mts, key=lambda m: m["date"]):
        try:
            items = agenda_items(mt)
        except Exception as e:  # noqa: BLE001
            log(f"meeting {mt['id']}: {e}")
            continue
        minutes.update(minutes_index(items, mt))
        for it in items:
            key, pre = case_key(it["title"])
            if not key:
                continue
            c = cases.setdefault(key, {"pre": pre, "apps": []})
            c["apps"].append({"date": mt["date"], "body": body_of(mt["slug"]), "title": it["title"], "url": it["url"],
                              "reading": P.reading_of(it["title"]),
                              "ords": sorted({f"{a}-{b}" for a, b in ORD_RE.findall(it["title"])})})
    log(f"{len(cases)} in-scope cases; {len(minutes)} minutes files")
    out = []
    for key, c in sorted(cases.items()):
        first = min(a["date"] for a in c["apps"])
        if first < since and key not in refresh:
            continue
        try:
            out.append(build(key, c, minutes, today.isoformat()))
        except Exception as e:  # noqa: BLE001
            log(f"{key}: skipped ({e})")
    return out


def parse(t):
    u = P.sq(t)
    out = {}
    m = re.search(r"(?:property|parcels?|site)\s+(?:generally\s+)?located\s+at\s+(\d+[^.;(]+?)(?:\s+from\s|\.|;|\s+First|\s+Second|$)",
                  u, re.I)
    if m:
        out["address"] = m.group(1).strip(" ,")
    m = re.search(r"([\d,]*\.?\d+)[- ]acres?\b", u, re.I)
    if m:
        try:
            out["acres"] = float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    m = re.search(r"Rezoning from\s+(.+?)\s+to\s+(.+?)(?:\s+(?:First|Second|Third)\s+Reading|\.|;|$)", u, re.I) or \
        re.search(r"rezone\b.*?\bfrom\s+(.+?)\s+to\s+(.+?)(?:\.|;|$)", u, re.I)
    if m:
        out["from_z"], out["to_z"] = m.group(1).strip(" ,"), m.group(2).strip(" ,")
    m = re.search(r"Land Use Plan Amendment (?:from\s+(.+?)\s+)?to\s+(.+?)(?:\s+(?:First|Second|Third)\s+Reading|\s+Rezoning|\.|;|$)", u, re.I)
    if m:
        out["from_f"], out["to_f"] = (m.group(1) or "").strip(" ,") or None, m.group(2).strip(" ,")
    m = re.search(r"Zoning District Designation (?:of|to)\s+(.+?)(?:\s+(?:First|Second|Third)\s+Reading|\.|;|$)", u, re.I)
    if m and not out.get("to_z"):
        out["to_z"] = m.group(1).strip(" ,")
    if re.search(r"City[- ]initiated", u, re.I):
        out["applicant"] = "City of Dunedin"
    return out


def build(key, c, minutes, today):
    apps = sorted(c["apps"], key=lambda a: a["date"])
    info = {}
    for a in sorted(apps, key=lambda a: len(a["title"]), reverse=True):
        for k, v in parse(a["title"]).items():
            if v and not info.get(k):
                info[k] = v
    ords = sorted({o for a in apps for o in a["ords"]})
    pins = []
    pdfs = item_pdfs(apps[-1]["url"])
    for u in sorted(pdfs, key=lambda x: (not re.search(r"Ordinance", x, re.I), not re.search(r"Application", x, re.I))):
        if re.search(r"Minutes|Notice|Business_Impact|Map", u, re.I):
            continue
        try:
            r = P.get(u, timeout=120)
            if len(r.content) > MAX_PDF:
                continue
            pins = P.pins_in(P.pdf_text(r.content, 8))
        except Exception as e:  # noqa: BLE001
            log(f"{key}: pdf {u[-50:]}: {e}")
            continue
        if pins:
            break
    pins = P.valid_pins(pins)
    acres = info.get("acres")
    if not pins and info.get("address"):
        p = P.parcel_lookup(info["address"], "DUNEDIN") or P.parcel_lookup(info["address"])
        if p and p.get("pin"):
            pins = [p["pin"]]
            acres = acres if acres is not None else p.get("acres")
    docs = next((u for u in pdfs if re.search(r"Staff_Report", u, re.I)), pdfs[0] if pdfs else None)
    keys = [key, key.replace("-", " ", 1)] + [f"Ordinance {o}" for o in ords]
    hearings = {}
    for a in apps:
        res = None
        mu = minutes.get((a["body"], a["date"]))
        if mu and a["date"] <= today:
            try:
                own = re.escape(P._ns(key))
                res = P.outcome_near(P.pdf_text(P.get(mu, timeout=120).content), keys, window=20000, gap=15000,
                                     stop=rf"application(?!{own})[a-z/]+-\d{{4}}-\d{{4}}|\d+\.[a-z]\.(?:application|resolution|ordinance|award|approve)")
            except Exception as e:  # noqa: BLE001
                log(f"{key}: minutes {mu[-50:]}: {e}")
        if a["body"] == LPA and res in ("Approved", "Denied"):
            res = "Recommended " + ("Approval" if res == "Approved" else "Denial")
        k = (a["body"], a["date"])
        cur = hearings.get(k)
        if cur is None:
            hearings[k] = {"body": a["body"], "date": a["date"], "result": res, "reading": a["reading"], "url": a["url"]}
        else:
            cur["result"] = cur["result"] or res
    hl = P.implied_first_readings(sorted(hearings.values(), key=lambda h: (h["date"], h["body"] != LPA)), CC)
    status, raw, decision = P.status_from_hearings(hl, today, final_body=CC, final_words=("second", "final"))
    pre = c["pre"].replace("/", " ")
    has_z = bool(re.search(r"\b(ZO|PRD|PUD)\b", pre))
    has_l = bool(re.search(r"\b(LUP|LU|CPA)\b", pre))
    to_z = info.get("to_z")
    if has_z:
        category = "PD" if re.search(r"\b(PRD|PUD)\b", pre) or re.search(r"Planned", to_z or "", re.I) else "Rezoning"
    else:
        category = "Comp Plan Amendment"
    app_type = " + ".join(x for x in (("Annexation" if re.search(r"\bAN\b", pre) else None),
                                      ("Land Use Plan Amendment" if has_l else None),
                                      ("Rezoning" if has_z else None)) if x)
    t = apps[-1]["title"]
    desc = re.sub(r"^\s*\d+\.[a-z]\)\s*", "", t)
    desc = desc if len(desc) <= 300 else desc[:297] + "..."
    return {
        "jurisdiction": JUR, "county": "Pinellas", "app_num": key, "category": category, "app_type": app_type,
        "project_name": info.get("address"), "description": desc, "applicant": info.get("applicant"),
        "parcel_ids": pins, "address": info.get("address"), "acres": acres,
        "from_zoning": info.get("from_z"), "to_zoning": to_z,
        "from_flu": info.get("from_f"), "to_flu": info.get("to_f"),
        "filed_date": apps[0]["date"], "status": status, "status_raw": raw,
        "hearings": [{"body": h["body"] + (f" ({h['reading']} reading)" if h["body"] == CC and h["reading"] else ""),
                      "date": h["date"],
                      "result": "Approved on first reading" if h["result"] == "Approved" and h["reading"] == "first" else h["result"]}
                     for h in hl],
        "decision_date": decision, "url": apps[-1]["url"], "docs_url": docs,
        "lat": None, "lon": None, "source": SOURCE, "ordinances": ords or None,
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
