#!/usr/bin/env python3
"""City of St. Petersburg (Pinellas) future land use and zoning map amendments, from City Council
agenda PDFs on stpete.org (best effort).

St. Petersburg has no public case database (its EnerGov portal does not expose planning cases)
and no agenda API, so this reads the Council agenda PDFs the city posts:
  * Council agendas   https://www.stpete.org/government/mayor___city_council/city_council/agendas.php
  * Numbered Action Taken agendas (same items, with outcomes)
    https://www.stpete.org/government/mayor___city_council/city_council/council_minutes_action_taken.php
A map amendment is a numbered item such as
  "9. Private initiated application for a 0.43-acre parcel generally located at the southeast corner
   of 2nd Avenue North and 35th Street North, requesting amendments to the Future Land Use and
   Official Zoning map. (a) Ordinance 766-L, amending the Future Land Use Map ... from Residential
   Medium (RM) to Planned Redevelopment - Mixed Use (PR-MU) ... (b) Ordinance 801-Z, amending the
   Official Zoning Map ... (City File FLUM-80)"
app_num = the city file ("FLUM-80", "Z-34") when given, else the ordinance numbers ("Ord 766-L / 801-Z").
The section the item sits in gives the reading: "New Ordinances - (First Reading of Title and
Setting of Public Hearing)" = first reading; "Public Hearings" / "Quasi-Judicial Proceedings" =
adoption hearing; "First Reading and First Public Hearing" (large-scale) = first reading.
Outcomes: Approved / Denied / Continued / Withdrawn words in the Action Taken text, or a resolution
denying the application or appeal. Cases appear here only once they reach City Council (the
Community Planning & Preservation Commission and DRC stages are not read). Requires pypdf.
"""
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pinellas_common as P  # noqa: E402

JURISDICTIONS = ["City of St. Petersburg"]
JUR = "City of St. Petersburg"
SITE = "https://www.stpete.org"
AGENDAS = SITE + "/government/mayor___city_council/city_council/agendas.php"
ACTIONS = SITE + "/government/mayor___city_council/city_council/council_minutes_action_taken.php"
SOURCE = "St. Petersburg City Council agendas + Action Taken agendas (PDF)"
P.set_tag("st_petersburg")
log = P.log
CC = "City Council"

ORD_RE = re.compile(r"\bOrdinance\s+(\d{3,4})\s*-\s*([LZ])\b", re.I)
FILE_RE = re.compile(r"\b(FLUM|Z|LGCP|ZM|LUZ)\s*[- ]\s*((?:\d{4}\s*-\s*)?\d{1,4})\b")
MAP_RE = re.compile(r"Future Land Use Map|Official Zoning Map|Future Land Use and (?:Official )?Zoning map", re.I)
DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def pdf_links(page, pat):
    h = P.get(page).text
    out = {}
    for href in re.findall(r'href="([^"]+\.pdf[^"]*)"', h, re.I):
        name = urllib.parse.unquote(href)
        if not re.search(pat, name, re.I):
            continue
        m = DATE_RE.search(name.rsplit("/", 1)[-1])
        if not m:
            continue
        d = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        url = href if href.startswith("http") else SITE + "/" + href.lstrip("/")
        url = urllib.parse.quote(urllib.parse.unquote(url), safe=":/?=&%")
        out.setdefault(d, url)
    return out


def layout_text(content):
    """Action Taken PDFs: outcomes sit in a left-hand column ("Approved / 648-H", "Motion / Failed").
    Layout extraction keeps it apart: -> list of (main text line, left column text)."""
    rdr = P.pypdf.PdfReader(P.io.BytesIO(content))
    rows = []
    for pg in rdr.pages[:80]:
        try:
            t = pg.extract_text(extraction_mode="layout") or ""
        except Exception:  # noqa: BLE001
            t = pg.extract_text() or ""
        for line in t.splitlines():
            m = re.match(r"^(\s{0,14}\S.*?)\s{3,}(\S.*)$", line)
            if m and len(line) - len(line.lstrip()) < 15 and len(m.group(1).strip()) <= 16:
                rows.append((m.group(2), m.group(1).strip()))
            else:
                rows.append((line.strip(), ""))
    return rows


def items_of(rows):
    """Split an agenda into numbered items, each tagged with its section and its left-column notes."""
    out, section, cur = [], "", None
    for line, left in rows:
        ls = line.strip()
        if re.match(r"^[A-L]\.\s+\S", ls) or re.match(r"^(Public Hearings|Quasi-Judicial|Quasi-Judicial Appeal)", ls, re.I):
            section = ls
            if cur:
                out.append(cur)
                cur = None
            continue
        m = re.match(r"^(\d{1,2})\.\s+(\S.*)$", ls)
        if m:
            if cur:
                out.append(cur)
            cur = {"num": m.group(1), "section": section, "text": m.group(2), "left": left}
        elif cur is not None:
            cur["text"] += "\n" + line
            if left:
                cur["left"] += " " + left
    if cur:
        out.append(cur)
    return out


def reading_of(section, text):
    s = (section + " " + text[:300]).lower()
    if "second reading" in s or "second public hearing" in s and "setting" not in s:
        return "second"
    if "first reading" in s or "new ordinances" in s:
        return "first"
    if "public hearing" in s or "quasi-judicial" in s:
        return "second"
    return None


def outcome_of(text, left=""):
    lt = P.sq(left)
    if re.search(r"Motion\s+Failed", lt, re.I):
        return "Denied"
    lw = re.findall(r"\b(Approved|Denied|Continued|Deferred|Withdrawn|Tabled)\b", lt)
    if lw:
        return {"Deferred": "Continued", "Tabled": "Continued"}.get(lw[-1], lw[-1])
    t = P.sq(text)
    if re.search(r"RESOLUTION\s+DENYING", t, re.I):
        return "Denied"
    if re.search(r"\bwithdrawn\b", t, re.I):
        return "Withdrawn"
    words = re.findall(r"(?:^|\s)(Approved|Denied|Continued|Deferred|Withdrawn|Failed)(?=\s|$)", t)
    if words:
        w = words[-1]
        return {"Deferred": "Continued", "Failed": None}.get(w, w)
    m = re.search(r"A motion was moved and approved.*?Ayes\.\s*(.*?)\s*Nayes\.\s*(.*?)\s*(?:Absent|$)", t, re.I)
    if m:
        ayes = [x for x in re.split(r"\.\s*", m.group(1)) if x and x.lower() != "none"]
        nays = [x for x in re.split(r"\.\s*", m.group(2)) if x and x.lower() != "none"]
        return "Approved" if len(ayes) > len(nays) else None
    return None


def parse(text):
    u = P.sq(text)
    out = {}
    m = re.search(r"([\d,]*\.?\d+)\s*-?\s*acres?\b", u, re.I)
    if m:
        try:
            out["acres"] = float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    m = re.search(r"(?:parcels?|property|site|located)\s+(?:generally\s+)?located\s+at\s+(.+?)(?:,\s*(?:requesting|from|and)|;|\.|\s+from\s)", u, re.I) \
        or re.search(r"generally\s+located\s+at\s+(.+?)(?:,\s*(?:requesting|from|and)|;|\.|\s+from\s)", u, re.I)
    if m:
        out["location"] = m.group(1).strip(" ,")
    m = re.search(r"Future Land Use Map designation.*?\bfrom\s+(.+?)\s+to\s+(.+?);", u, re.I)
    if m:
        out["from_f"], out["to_f"] = m.group(1).strip(), m.group(2).strip()
    m = re.search(r"Official Zoning Map.*?changing the zoning.*?\bfrom\s+(.+?)\s+to\s+(.+?);", u, re.I)
    if m:
        out["from_z"], out["to_z"] = m.group(1).strip(), m.group(2).strip()
    files = sorted({a.upper() + "-" + re.sub(r"\s+", "", b) for a, b in FILE_RE.findall(u)
                    if a.upper() != "Z" or "zoning" in u.lower()})
    out["files"] = [f for f in files if not f.startswith("LGCP")] or files
    out["ords"] = sorted({f"{a}-{b.upper()}" for a, b in ORD_RE.findall(u)})
    if re.search(r"City[- ]initiated", u, re.I):
        out["applicant"] = "City of St. Petersburg"
    return out


def fetch(since, refresh):
    if P.pypdf is None:
        raise RuntimeError("pypdf is required for the St. Petersburg adapter")
    today = dt.date.today()
    start = (dt.date.fromisoformat(since) - dt.timedelta(days=60)).isoformat()
    refresh = {r.strip().upper() for r in (refresh or []) if r and r.strip()}
    if refresh:
        start = min(start, (today - dt.timedelta(days=540)).isoformat())
    actions = pdf_links(ACTIONS, r"Action Taken Agenda")
    agendas = pdf_links(AGENDAS, r"Agenda Packet")  # the packet opens with the full agenda; only read for meetings
    #                                                 that have no Action Taken agenda yet
    meetings = {}
    for d, u in agendas.items():
        if d not in actions and d >= (today - dt.timedelta(days=21)).isoformat():
            meetings[d] = {"url": u, "kind": "agenda"}
    for d, u in actions.items():  # Action Taken (with outcomes) wins over the plain agenda
        meetings[d] = {"url": u, "kind": "action"}
    dates = sorted(d for d in meetings if start <= d <= (today + dt.timedelta(days=75)).isoformat())
    log(f"{len(dates)} Council meetings since {start} ({sum(meetings[d]['kind'] == 'action' for d in dates)} with Action Taken)")
    cases = {}
    for d in dates:
        mt = meetings[d]
        try:
            content = P.get(mt["url"], timeout=150).content
            rows = layout_text(content) if mt["kind"] == "action" else \
                [(ln, "") for ln in P.pdf_text(content, 16).splitlines()]
        except Exception as e:  # noqa: BLE001
            log(f"{d}: {e}")
            continue
        for it in items_of(rows):
            t = it["text"]
            if not MAP_RE.search(t) or re.search(r"Land Development Regulations|text amendment to the Land Development", t, re.I) \
                    and not ORD_RE.search(t):
                continue
            info = parse(t)
            if not info["ords"] and not info["files"]:
                continue
            key = " / ".join(info["files"]) if info["files"] else "Ord " + " / ".join(info["ords"])
            hit = next((k for k, c in cases.items() if set(c["ords"]) & set(info["ords"]) or
                        (info["files"] and set(c["files"]) & set(info["files"]))), None)
            if hit:
                key = hit
            c = cases.setdefault(key, {"ords": set(), "files": set(), "apps": []})
            c["ords"] |= set(info["ords"])
            c["files"] |= set(info["files"])
            res = outcome_of(t, it.get("left", "")) if mt["kind"] == "action" and d <= today.isoformat() else None
            c["apps"].append({"date": d, "text": t, "info": info, "reading": reading_of(it["section"], t),
                              "result": res, "url": mt["url"]})
    log(f"{len(cases)} map-amendment cases")
    out = []
    for key, c in sorted(cases.items()):
        first = min(a["date"] for a in c["apps"])
        if first < since and key.upper() not in refresh:
            continue
        try:
            out.append(build(key, c, today.isoformat()))
        except Exception as e:  # noqa: BLE001
            log(f"{key}: skipped ({e})")
    return out


def build(key, c, today):
    apps = sorted(c["apps"], key=lambda a: a["date"])
    info = {}
    for a in sorted(apps, key=lambda a: len(a["text"]), reverse=True):
        for k, v in a["info"].items():
            if v and not info.get(k):
                info[k] = v
    hearings = [{"body": CC, "date": a["date"], "result": a["result"], "reading": a["reading"]} for a in apps]
    seen, hl = set(), []
    for h in hearings:
        if (h["date"], h["reading"]) not in seen:
            seen.add((h["date"], h["reading"]))
            hl.append(h)
    hl = P.implied_first_readings(hl, CC)
    status, raw, decision = P.status_from_hearings(hl, today, final_body=CC, final_words=("second",))
    loc = info.get("location")
    pins, acres = [], info.get("acres")
    if loc and re.match(r"\d", loc):
        p = P.parcel_lookup(loc, "ST PETERSBURG") or P.parcel_lookup(loc)
        if p and p.get("pin"):
            pins = [p["pin"]]
            acres = acres if acres is not None else p.get("acres")
    has_l = any(o.endswith("-L") for o in c["ords"]) or info.get("to_f")
    has_z = any(o.endswith("-Z") for o in c["ords"]) or info.get("to_z")
    if has_z:
        category = "PD" if re.search(r"Planned Development|\bPD\b", info.get("to_z") or "") else "Rezoning"
    else:
        category = "Comp Plan Amendment"
    app_type = " + ".join(x for x in (("Future Land Use Map Amendment" if has_l else None),
                                      ("Zoning Map Amendment" if has_z else None)) if x)
    desc = P.sq(apps[-1]["text"])
    desc = desc if len(desc) <= 300 else desc[:297] + "..."
    return {
        "jurisdiction": JUR, "county": "Pinellas", "app_num": key, "category": category, "app_type": app_type,
        "project_name": loc, "description": desc, "applicant": info.get("applicant"),
        "parcel_ids": pins, "address": loc, "acres": acres,
        "from_zoning": info.get("from_z"), "to_zoning": info.get("to_z"),
        "from_flu": info.get("from_f"), "to_flu": info.get("to_f"),
        "filed_date": apps[0]["date"], "status": status, "status_raw": raw,
        "hearings": [{"body": CC + (f" ({h['reading']} reading)" if h["reading"] else ""), "date": h["date"],
                      "result": "Approved on first reading" if h["result"] == "Approved" and h["reading"] == "first" else h["result"]}
                     for h in hl],
        "decision_date": decision, "url": apps[-1]["url"], "docs_url": None,
        "lat": None, "lon": None, "source": SOURCE, "ordinances": sorted(c["ords"]) or None,
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
