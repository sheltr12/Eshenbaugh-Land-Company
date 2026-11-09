#!/usr/bin/env python3
"""City of Sarasota (Sarasota County) rezonings and future land use map amendments.

Source: the city's public Granicus meeting site (sarasota.granicusideas.com): City Commission
meeting list, each meeting's agenda items and each candidate item's page (full title). The city's
own website (sarasotafl.gov, Planning Board agendas, minutes) refuses automated requests (Akamai
403) and its permit portal (Mitchell Humphrey "Citizen Services Portal") shows development
applications without type or description, so City Commission agenda titles are the usable source.
Agenda items read like
  "XI.1) Public Hearing Re: Proposed Ordinance No. 26-5612, to rezone that certain vacant parcel of
   real property totaling 2.8 +/- acres located on the west side of Beneva Road ... from the
   Residential Single Family 2 (RSF-2) Zone District to the Residential Multiple Family 3 (RMF-3)
   Zone District ... (Application No. 26-REN-04) (Applicant Brian Lichterman, agent for ...)"
app_num = the application number. Kept prefixes: REN (rezone ordinance) and RE (rezone with site
plan) -> Rezoning (PD when the proposed district is a planned district); PA (comprehensive plan
amendment) -> Comp Plan Amendment only when it changes the Future Land Use Map (text-only plan
amendments are skipped). Skipped: ZTA (zoning text), SP, CU, SUB, GZW, ROA, ADP, appeals.
Readings: "Public Hearing Re: Proposed Ordinance" = first reading (quasi-judicial / legislative
hearing; for a PA the transmittal hearing); "Adoption Re: Second Reading of Proposed Ordinance" or
"Second Public Hearing" = adoption. Minutes are not reachable, so an adoption item on a past
agenda is reported as Approved (outcome inferred from the agenda; status_raw says so) and a
first reading followed by a second reading as approved on first reading.
Parcel IDs: 10-digit Sarasota County PA ids, from the title when posted, else from the site address
(Sarasota County "Parcels" layer, city parcels only, single exact match).
"""
import datetime as dt
import html as _html
import json
import re
import sys
import time

import requests

JURISDICTIONS = ["City of Sarasota"]
JUR = "City of Sarasota"
BASE = "https://sarasota.granicusideas.com"
PARCELS = "https://ags3.scgov.net/server/rest/services/Hosted/Parcels/FeatureServer/0/query"
SOURCE = "City of Sarasota Granicus meeting pages (City Commission agendas)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
CC = "City Commission"
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
APP_RX = re.compile(r"\b(\d{2})-(REN|RE|PA|ZTA|SP|CU|MCU|SUB|GZW|ROA|ADP|APP|RZ|PUD)-(\d{1,3})\b", re.I)
KEEP = {"REN", "RE", "PA", "RZ", "PUD"}
KW = re.compile(r"rezon|zone district|future land use|comprehensive plan|planned development|\bPUD\b|"
                r"zoning (map|atlas)|sarasota city plan", re.I)
ORD_RX = re.compile(r"Ordinance No\.?\s*(\d{2}-\d{4})", re.I)
PD_RX = re.compile(r"\bPUD\b|planned|\bP(C|I|R|M)D\b", re.I)

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


def log(*a):
    print("[city_of_sarasota]", *a, file=sys.stderr, flush=True)


def get(url, params=None, tries=3, sleep=0.35, timeout=60):
    last = None
    for i in range(tries):
        try:
            r = _s.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            time.sleep(sleep)
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


def clean(s):
    s = _html.unescape(re.sub(r"<[^>]+>", " ", s or "")).replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def meetings():
    out = {}
    for url in (f"{BASE}/meetings?scope=past", f"{BASE}/meetings"):
        h = get(url).text
        for sec in re.split(r"<section\s+class=\"meeting\"", h)[1:]:
            m = re.search(r'id="(\d+)-([a-z0-9-]+)"', sec)
            t = re.search(rf"Meeting Time:\s*({MONTHS})\s+(\d{{1,2}}),\s+(\d{{4}})", sec)
            if not m or not t:
                continue
            d = dt.datetime.strptime(f"{t.group(1)} {t.group(2)} {t.group(3)}", "%B %d %Y").date().isoformat()
            out[m.group(1)] = {"id": m.group(1), "slug": m.group(2), "date": d}
    return list(out.values())


def is_commission(slug):
    return "city-commission" in slug and not re.search(r"attorney-client|workshop|budget|city-manager-search", slug)


def agenda_items(mt):
    items, seen = [], set()
    for page in range(1, 8):
        h = get(f"{BASE}/meetings/{mt['id']}-{mt['slug']}/agenda_items", params={"page": page}).text
        for u, t in re.findall(r'href="(/meetings/%s[^"]*/agenda_items/[^"]+)"[^>]*>(.*?)</a>' % mt["id"], h, re.S):
            t = clean(t)
            if u in seen or not t:
                continue
            seen.add(u)
            items.append({"url": BASE + u, "title": t})
        if f"page={page + 1}" not in h:
            break
    return items


def full_title(url):
    h = get(url).text
    m = re.search(r"<title>(.*?)</title>", h, re.S)
    t = clean(m.group(1)) if m else ""
    return re.sub(r"\s*-\s*City of Sarasota\s*$", "", t)


def reading_of(t):
    if re.search(r"Second Reading|Second Public Hearing|Adoption Re:", t, re.I):
        return "second"
    if re.search(r"Public Hearing Re:|First Reading", t, re.I):
        return "first"
    return None


def parse(t):
    u = re.sub(r"\s+", " ", t)
    out = {}
    m = re.search(r"(?:\+/-\s*)?(\d[\d,]*\.?\d*|\.\d+)\s*(?:\+/-|±)?\s*acres?\b", u, re.I)
    if m:
        try:
            out["acres"] = float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    m = re.search(r"(?:street address(?:es)? of|located at|address(?:es)? of)\s+(\d.+?)(?:,?\s+from\s+the\s|,?\s+from\s|;|"
                  r",\s*all as|\s+all as|,\s*(?:such|being|and having)|\)|$)", u, re.I)
    if m and len(m.group(1)) < 120:
        out["address"] = m.group(1).strip(" ,.")
    m = re.search(r"from (?:the )?([^;]{2,90}?)\s+Zone District\s+to\s+(?:the\s+)?([^;]{2,90}?)\s+Zone District", u, re.I)
    if m:
        out["from_z"], out["to_z"] = m.group(1).strip(" ,"), m.group(2).strip(" ,")
    m = re.search(r"Future Land Use (?:Map )?Classification[^;]*?from\s+(?:the\s+)?(.{2,90}?)\s+to\s+(?:the\s+)?(.{2,90}?)"
                  r"(?:\s+(?:Future Land Use|classification)|;|,\s*(?:all|and|as)|$)", u, re.I)
    if m:
        out["from_f"], out["to_f"] = _flu(m.group(1)), _flu(m.group(2))
    m = re.search(r"\(\s*Applicants?:?\s+([^)]{3,200})\)", u, re.I) or \
        re.search(r"Applicants?:\s+([^)]{3,200})\)", u, re.I)
    if not m:
        m = re.search(r"\(([^()]{3,200}?(?:agent|representing)[^()]{0,200}?),?\s*(?:Applicant|Property Owner|Owner)s?\)", u, re.I)
    if m:
        out["applicant"] = m.group(1).strip(" ,.;")
    out["pids"] = re.findall(r"Parcel (?:ID|Identification)(?: No\.?| Nos?\.?| Numbers?)?:?\s*(\d{10})", u, re.I)
    return out


def _flu(v):
    v = re.sub(r"\s+(?:Future Land Use(?: Map)?)?\s*(?:Classification|Category|designation)s?$", "", (v or "").strip(" ,"), flags=re.I)
    return re.sub(r"\s+Future Land Use$", "", v, flags=re.I).strip(" ,") or None


def parcel_by_address(addr):
    """Sarasota County Parcels layer: exact house number + street name start, inside the city."""
    if re.search(r"\d+\s*,\s*\d+|\band\s+\d", addr or ""):
        return None  # several addresses
    m = re.match(r"(\d+)\s+(?:(N|S|E|W|NORTH|SOUTH|EAST|WEST)\.?\s+)?(.+)$", (addr or "").strip(), re.I)
    if not m:
        return None
    num, street = m.group(1), m.group(3).upper()
    street = re.sub(r"\b(AVENUE|AVE|STREET|ST|BOULEVARD|BLVD|ROAD|RD|DRIVE|DR|WAY|LANE|LN|PLACE|PL|COURT|CT|"
                     r"PARKWAY|PKWY|TRAIL|TRL|CIRCLE|CIR|TERRACE|TER)\.?$", "", street).strip(" .")
    if not street:
        return None
    where = (f"locn='{num}' AND UPPER(locs) LIKE '{street.replace(chr(39), chr(39) * 2)}%' "
             f"AND UPPER(municipality) LIKE '%SARASOTA%' AND UPPER(municipality) NOT LIKE '%COUNTY%'")
    try:
        d = get(PARCELS, {"where": where, "outFields": "account,fulladdress,measuredacreage,name1",
                          "returnGeometry": "false", "f": "json"}, tries=2).json()
    except Exception as e:  # noqa: BLE001
        log(f"parcel lookup {addr}: {e}")
        return None
    fs = [f["attributes"] for f in d.get("features", [])]
    accts = {f.get("account") for f in fs if f.get("account")}
    if len(accts) != 1:
        return None
    f = fs[0]
    return {"pin": f.get("account"), "acres": f.get("measuredacreage"), "owner": f.get("name1")}


def fetch(since, refresh=None):
    today = dt.date.today()
    refresh = {r.strip().upper() for r in (refresh or []) if r and r.strip()}
    # a fixed long lookback keeps app_num stable for reused application numbers (split_reused)
    # and lets refresh ids be found; ~40 meetings, about two minutes
    start = min(dt.date.fromisoformat(since) - dt.timedelta(days=150), today - dt.timedelta(days=540))
    try:
        allm = meetings()
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"City of Sarasota meeting site unreachable: {e}") from e
    mts = [m for m in allm if is_commission(m["slug"]) and start.isoformat() <= m["date"] <= (today + dt.timedelta(days=60)).isoformat()]
    log(f"{len(mts)} City Commission meetings since {start}")
    apps, ord_map, loose = {}, {}, []
    for mt in sorted(mts, key=lambda m: m["date"]):
        try:
            items = agenda_items(mt)
        except Exception as e:  # noqa: BLE001
            log(f"meeting {mt['id']}: {e}")
            continue
        for it in items:
            if not KW.search(it["title"]) and not re.search(r"Ordinance No", it["title"], re.I):
                continue
            if not KW.search(it["title"]):
                continue
            try:
                t = full_title(it["url"])
            except Exception as e:  # noqa: BLE001
                log(f"item {it['url'][-60:]}: {e}")
                continue
            if not re.search(r"Ordinance", t, re.I):
                continue
            nums = [f"{a}-{b.upper()}-{int(c):02d}" for a, b, c in APP_RX.findall(t)]
            ords = ORD_RX.findall(t)
            ap = {"date": mt["date"], "url": it["url"], "title": t, "reading": reading_of(t), "ords": ords}
            keep = [n for n in nums if n.split("-")[1] in KEEP]
            if keep:
                for n in keep:
                    apps.setdefault(n, []).append(ap)
                    for o in ords:
                        ord_map.setdefault(o, n)
            elif not nums:
                loose.append(ap)
    for ap in loose:  # items that name only the ordinance
        n = next((ord_map[o] for o in ap["ords"] if o in ord_map), None)
        if n:
            apps[n].append(ap)
    apps = split_reused(apps)
    log(f"{len(apps)} rezoning / plan amendment applications on agendas")
    out = []
    for n, lst in sorted(apps.items()):
        first = min(a["date"] for a in lst)
        if first < since and n.upper() not in refresh:
            continue
        try:
            rec = build(n, lst, today.isoformat())
        except Exception as e:  # noqa: BLE001
            log(f"{n}: skipped ({type(e).__name__}: {e})")
            continue
        if rec:
            out.append(rec)
    missing = refresh - {r["app_num"].upper() for r in out}
    if missing:
        log(f"refresh ids not on City Commission agendas in the window: {sorted(missing)}")
    return out


def _addr_key(t):
    a = parse(t).get("address") or ""
    m = re.match(r"\s*(\d+)\s+(?:[NSEW]\.?\s+)?(\w+)", a)
    return f"{m.group(1)} {m.group(2).upper()}" if m else None


def split_reused(apps):
    """The city occasionally reuses an application number for a different site (e.g. 26-RE-01 for
    2283 Ringling Blvd and later for 2250 Fruitville Rd). Appearances are clustered by site
    address; the earliest cluster keeps the number, later ones get ' (Ord. <ordinance no.>)'."""
    out = {}
    for n, lst in apps.items():
        clusters = []
        for a in sorted(lst, key=lambda a: a["date"]):
            k = _addr_key(a["title"])
            for c in clusters:
                if k is None or c["key"] is None or c["key"] == k or set(a["ords"]) & c["ords"]:
                    c["items"].append(a)
                    c["ords"] |= set(a["ords"])
                    c["key"] = c["key"] or k
                    break
            else:
                clusters.append({"key": k, "items": [a], "ords": set(a["ords"])})
        for i, c in enumerate(clusters):
            key = n if i == 0 else f"{n} (Ord. {sorted(c['ords'])[0]})" if c["ords"] else f"{n} ({c['key']})"
            out[key] = c["items"]
    return out


def build(n, lst, today):
    kind = n.split(" ")[0].split("-")[1]
    lst = sorted({a["url"]: a for a in lst}.values(), key=lambda a: a["date"])
    alltext = " ".join(a["title"] for a in lst)
    if kind == "PA" and not re.search(r"Future Land Use Map|Future Land Use Classification|FLUM", alltext, re.I):
        return None  # text-only plan amendment
    info = {}
    for a in sorted(lst, key=lambda a: len(a["title"]), reverse=True):
        for k, v in parse(a["title"]).items():
            if v and not info.get(k):
                info[k] = v
    ords = sorted({o for a in lst for o in a["ords"]})
    # hearings: one per agenda date
    hs = {}
    for a in lst:
        h = hs.setdefault(a["date"], {"body": CC, "date": a["date"], "reading": a["reading"], "url": a["url"]})
        if a["reading"] == "second":
            h["reading"] = "second"
    hl = sorted(hs.values(), key=lambda h: h["date"])
    past = [h for h in hl if h["date"] < today]
    future = [h for h in hl if h["date"] >= today]
    adopted = [h for h in past if h["reading"] == "second"]
    for h in hl:
        h["result"] = None
        if h["date"] < today:
            if h["reading"] == "second":
                h["result"] = "Approved"
            elif any(x["date"] > h["date"] for x in hl):
                h["result"] = "Approved on first reading"
    if adopted:
        status, decision = "Approved", adopted[-1]["date"]
        raw = f"Second reading / adoption on {decision} City Commission agenda (outcome inferred from agenda)"
    elif future:
        status, decision = "Hearing Scheduled", None
        raw = f"{future[0]['reading'] or 'public'} reading scheduled {future[0]['date']}"
    elif past:
        status, decision = "Filed", None
        raw = f"City Commission {past[-1]['reading'] or ''} reading {past[-1]['date']}; next hearing not yet scheduled".replace("  ", " ")
    else:
        status, decision, raw = "Filed", None, None
    if kind == "PA":
        category, app_type = "Comp Plan Amendment", "Comprehensive Plan Amendment (Future Land Use Map)"
    else:
        category = "PD" if PD_RX.search(info.get("to_z") or "") else "Rezoning"
        app_type = "Rezone Ordinance" if kind == "REN" else ("Rezone with Site Plan" if kind == "RE" else "Rezoning")
    pids = list(dict.fromkeys(info.get("pids", [])))
    acres = info.get("acres")
    if not pids and info.get("address"):
        p = parcel_by_address(info["address"])
        if p and p.get("pin"):
            pids = [p["pin"]]
            if acres is None and p.get("acres"):
                acres = round(float(p["acres"]), 3)
    last = lst[-1]
    desc = re.sub(r"^\s*[IVXL]+\.\d+\)\s*", "", last["title"])
    desc = desc if len(desc) <= 600 else desc[:597] + "..."
    rec = {
        "jurisdiction": JUR, "county": "Sarasota", "app_num": n, "category": category,
        "app_type": app_type + (f" (Ordinance No. {', '.join(ords)})" if ords else ""),
        "project_name": info.get("address"), "description": desc, "applicant": info.get("applicant"),
        "parcel_ids": pids, "address": info.get("address"), "acres": acres,
        "from_zoning": info.get("from_z") if kind != "PA" else None,
        "to_zoning": info.get("to_z") if kind != "PA" else None,
        "from_flu": info.get("from_f") if kind == "PA" else None,
        "to_flu": info.get("to_f") if kind == "PA" else None,
        "filed_date": lst[0]["date"], "status": status, "status_raw": raw,
        "hearings": [{"body": CC + (f" ({h['reading']} reading)" if h["reading"] else ""), "date": h["date"],
                      "result": h["result"]} for h in hl],
        "decision_date": decision, "url": last["url"], "docs_url": None,
        "lat": None, "lon": None, "source": SOURCE,
    }
    return {k: (None if v == "" else v) for k, v in rec.items()}


if __name__ == "__main__":
    since = sys.argv[1] if len(sys.argv) > 1 else (dt.date.today() - dt.timedelta(days=14)).isoformat()
    refresh = sys.argv[2].split(",") if len(sys.argv) > 2 and sys.argv[2] else []
    t0 = time.time()
    recs = fetch(since, refresh)
    print(json.dumps(recs, indent=2, ensure_ascii=False))
    from collections import Counter
    log(f"{len(recs)} records in {time.time() - t0:.0f}s; status={dict(Counter(r['status'] for r in recs))}; "
        f"category={dict(Counter(r['category'] for r in recs))}")
