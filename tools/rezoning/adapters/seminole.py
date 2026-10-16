#!/usr/bin/env python3
"""Seminole County (unincorporated) rezoning / PD / future land use map amendment adapter.

Source: Seminole County Legistar web API (webapi.legistar.com/v1/seminolecountyfl).
 * Development Review Committee (body 183) matters = formal applications under staff review.
   Their titles are structured:
       "LAVENDER WAY - REZONE
        Project Number: 26-20000002
        Project Description: Proposed Rezone from A-1 to PLI on 7.84 acres ...
        Project Manager: ...   Parcel ID: 23-21-30-300-0070-0000+   Applicant: ...   Consultant: ..."
   Project number type codes: 200xxxxx rezone / LSFLUA, 205xxxxx PD (rezone, major amendment,
   or PD final development plan = site plan, skipped), 100xxxxx small-scale FLU amendment,
   800xxxxx pre-application (skipped). A trailing "+" on the parcel ID means more parcels.
 * Planning & Zoning Commission (body 184, acting as LPA) and BCC (body 138) public hearing matters:
   "Greeneway Preserve Small Scale Future Land Use Map Amendment and Rezone - Consider ...
    on approximately 23.42 acres ...; (Z2026-08/02.20SS.02) (Brent Lenzen, ..., Applicant) ..."
   Matter histories carry the motion and vote (approve / deny / continue / first reading).
 * Hearing cases are tied to their DRC application by project name, parcel IDs, acreage and
   planner. app_num is the DRC project number when linked (it exists from filing on), otherwise
   the hearing case number (Z2026-08, 02.26SS.01) or, when the title has none, the project name.
 * Full parcel lists come from the applicant narrative / community meeting attachments of the
   hearing matter (pypdf, first pages only) when the DRC title shows only the first parcel.
The county's Accela portal (aca-prod.accela.com/SEMINOLECO) is still a vendor placeholder
("City of Metropolis") as of Oct 2026, so it is not used.
"""
import datetime as dt
import io
import json
import logging
import re
import sys
import time

import requests

try:
    import pypdf
    logging.getLogger("pypdf").setLevel(logging.ERROR)
except ImportError:  # pragma: no cover
    pypdf = None

JURISDICTIONS = ["Seminole County"]

LEG = "https://webapi.legistar.com/v1/seminolecountyfl"
SITE = "https://seminolecountyfl.legistar.com"
SOURCE = "Seminole County Legistar (DRC applications + P&Z Commission/LPA + BCC hearings)"
UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
BODY_DRC, BODY_PZ, BODY_BCC = 183, 184, 138
LOOKBACK_DAYS = 730          # history scanned before `since` to link hearings to applications
MAX_PDF_BYTES = 15 * 1024 * 1024
HEARING_TYPES = ("Public Hearing Items", "Public Hearings - Quasi-Judicial", "Public Hearings - Legislative")

_s = requests.Session()
_s.headers.update({"User-Agent": UA, "Accept": "application/json"})


def log(*a):
    print("[seminole]", *a, file=sys.stderr)


def get(url, params=None, tries=3, sleep=0.3, **kw):
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


def get_all(path, flt):
    out, skip = [], 0
    while True:
        d = get(f"{LEG}/{path}", {"$filter": flt, "$top": 1000, "$skip": skip}).json()
        out.extend(d)
        if len(d) < 1000:
            return out
        skip += 1000


def esc(s):
    return s.replace("'", "''")


def d10(s):
    return (s or "")[:10] or None


def clean(s):
    s = re.sub(r"\s+", " ", s or "").strip(" .,;")
    return s or None


# ------------------------------------------------------------------ parcels / numbers
PID_RE = re.compile(r"\b\d{2}\s?-\s?\d{2}\s?-\s?\d{2}\s?-\s?[0-9A-Z]{3}\s?-\s?[0-9A-Z]{4}\s?-\s?[0-9A-Z]{4}\b")


def parcels_in(text):
    out = []
    for m in PID_RE.findall(text or ""):
        p = re.sub(r"\s", "", m)
        if p not in out:
            out.append(p)
    return out


def to_float(s):
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


ACRES_RE = re.compile(r"([\d,]*\.?\d+)\s*(?:\+/-\s*|±\s*)?(?:gross\s+|net\s+)?acres?\b", re.I)


def acres_in(text):
    m = ACRES_RE.search(text or "")
    return to_float(m.group(1)) if m else None


# ------------------------------------------------------------------ names
STOP = {"rezone", "rezoning", "pd", "major", "minor", "amendment", "small", "scale", "large", "future",
        "land", "use", "map", "and", "ssflua", "lsflua", "flua", "the", "of", "at", "a", "to", "for",
        "on", "planned", "development", "sslua", "&"}


def tokens(name):
    return {w for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if w not in STOP and len(w) > 1}


def name_sim(a, b):
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


# ------------------------------------------------------------------ DRC applications
DRC_SCOPE = re.compile(r"REZON|FLUA|LAND USE (?:MAP )?AMEND|PD MAJOR|PD AMENDMENT", re.I)
DRC_SKIP = re.compile(r"PRE-?APPLI|FINAL DEVELOPMENT|\bFDP\b|SITE PLAN|SPECIAL EXCEPTION|MINOR|SUBDIVISION PLAN|"
                      r"\bPSP\b|FINAL ENGINEERING|BILLBOARD", re.I)


def parse_drc(m):
    t = m.get("MatterTitle") or ""
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    first = re.sub(r"^\s*\d{1,2}:\d{2}\s*[AP]\.?M\.?\s*(?:\([^)]*\))?\s*", "", lines[0] if lines else "", flags=re.I)
    name, kind = (first.rsplit(" - ", 1) + [""])[:2] if " - " in first else (first, "")

    def fld(label):
        mm = re.search(rf"{label}:\s*(.+)", t)
        return clean(mm.group(1)) if mm else None
    pn = fld("Project Number")
    pn = pn.split()[0] if pn else None
    pid_raw = fld("Parcel ID") or ""
    desc = fld("Project Description")
    strip_phone = lambda v: clean(re.sub(r"\(?\d{3}\)?[\s.-]*\d{3}[\s.-]*\d{4}|\S+@\S+", "", v or ""))  # noqa: E731
    return {
        "matter": m, "name": clean(name), "kind": clean(kind) or "", "pn": pn, "desc": desc,
        "pm": clean(re.sub(r"\(.*", "", fld("Project Manager") or "")),
        "parcels": parcels_in(pid_raw), "more_parcels": "+" in pid_raw,
        "applicant": strip_phone(fld("Applicant")), "consultant": strip_phone(fld("Consultant")),
        "date": d10(m.get("MatterIntroDate")), "agenda": d10(m.get("MatterAgendaDate")),
        "is_meeting": (m.get("MatterTypeName") or "") == "DRC",
    }


def drc_in_scope(d):
    if not d["pn"] or not re.match(r"\d{2}-(?:10|20)\d", d["pn"]):
        return False
    k = d["kind"] or ""
    return bool(DRC_SCOPE.search(k)) and not DRC_SKIP.search(k)


# ------------------------------------------------------------------ hearing matters
H_SCOPE = re.compile(r"\bRezon|Future Land Use Map Amendment|Land Use Map Amendment|PD Major Amendment|"
                     r"\bFLUM?A\b|Land Use Amendment", re.I)
H_SKIP = re.compile(r"Special Exception|Text Amendment|Right-of-Way|Vacat|Plat\b|Land Development Code|"
                    r"Boat Dock|Code of Ordinances", re.I)
CASE_RE = re.compile(r"\((Z\d{4}-\d{1,3}(?:\s*/\s*\d{2}\.\d{2}(?:SS|LS)\.?\d{1,2})?|\d{2}\.\d{2}(?:SS|LS)\.?\d{1,2})\)")


def h_in_scope(title):
    t = title or ""
    if not H_SCOPE.search(t):
        return False
    if H_SKIP.search(t) and not re.search(r"\bRezone\b|Future Land Use Map Amendment", t):
        return False
    if re.search(r"Comprehensive Plan (?:Text )?Amendment", t, re.I) and not re.search(r"Map Amendment|Rezone", t, re.I):
        return False
    return True


def h_case(title):
    m = CASE_RE.search(title or "")
    return re.sub(r"\s", "", m.group(1)) if m else None


def h_name(title):
    t = re.sub(r"\s+", " ", title or "").strip()
    t = re.split(r"\s*[-\u2013]\s*(?=Consider|Request|\*|CONTINUED|Continued|This item|Staff|The Applicant)|"
                 r"\s[-\u2013]\s", t)[0]
    t = re.sub(r"^(?:Request for Continuance[^A-Z]*|Continued from[^A-Z]*?\d{4}\s*P&Z meeting)\s*", "", t, flags=re.I)
    name = re.split(r"\s+(?:Small Scale|Large Scale|PD Major|PD Rezone|Rezone|Future Land Use|Major Amendment)\b", t, maxsplit=1)[0]
    return clean(name) or clean(t)


def body_label(body_id):
    return {BODY_PZ: "Planning & Zoning Commission", BODY_BCC: "BCC", BODY_DRC: "DRC"}.get(body_id)


def classify(body, action, passed, text, title):
    a = (action or "").lower()
    x = re.sub(r"\s+", " ", text or "").lower()
    pz = body == "Planning & Zoning Commission"
    if re.search(r"applicant (?:has )?withdr|withdrawn by the applicant|request(?:ed)? to withdraw|"
                 r"accept(?:ed)? the withdrawal", x) or ("withdr" in a and passed and passed.lower() == "pass"):
        return "Withdrawn"
    if "withdr" in a:
        return None  # a motion that was withdrawn, not the application
    if passed and passed.lower().startswith("fail"):
        return f"Motion to {action} failed" if action else "Motion failed"
    if "contin" in a or "table" in a or "postpone" in a or "defer" in a or \
            re.search(r"\bto continue\b|\bcontinue (?:the|this|to|consideration)", x):
        return "Continued"
    if "transmit" in a or "transmit" in x:
        return "Transmitted"
    if re.search(r"\bden(?:y|ial|ied)\b", x) or "deny" in a or "denial" in a:
        return "Recommended Denial" if pz else "Denied"
    if "first reading" in x and not pz:
        return "Approved on first reading"
    if re.search(r"approv|adopt|enact|recommend", a + " " + x):
        return "Recommended Approval" if pz else "Approved"
    return action or None


# ------------------------------------------------------------------ PDF attachments
def pdf_text(url, pages=6):
    r = _s.get(url, timeout=120, stream=True, headers={"Accept": "application/pdf,*/*"})
    r.raise_for_status()
    buf = io.BytesIO()
    for chunk in r.iter_content(256 * 1024):
        buf.write(chunk)
        if buf.tell() > MAX_PDF_BYTES:
            r.close()
            raise RuntimeError("attachment too large")
    time.sleep(0.3)
    rdr = pypdf.PdfReader(buf)
    return "\n".join((p.extract_text() or "") for p in rdr.pages[:pages])


ATT_ORDER = [r"NARRATIVE", r"COMMUNITY MEETING", r"APPLICATION", r"SURVEY", r"LEGAL", r"BEI|BUSINESS AND ECONOMIC"]


def attachment_info(matters):
    """Parcel IDs and a staff/packet document link from the hearing matters' attachments."""
    atts = []
    for m in sorted(matters, key=lambda m: m["MatterId"], reverse=True):
        try:
            atts += [a for a in get(f"{LEG}/matters/{m['MatterId']}/attachments").json()]
        except Exception as e:  # noqa: BLE001
            log(f"attachments {m['MatterId']}: {e}")
    docs = None
    for a in atts:
        if re.search(r"ITEM\s*#|STAFF REPORT", a.get("MatterAttachmentName") or "", re.I):
            docs = a.get("MatterAttachmentHyperlink")
            break
    parcels, seen = [], set()
    if pypdf is None:
        return parcels, docs
    tried = 0
    for pat in ATT_ORDER:
        for a in atts:
            nm = a.get("MatterAttachmentName") or ""
            url = a.get("MatterAttachmentHyperlink")
            if not url or url in seen or not re.search(pat, nm, re.I) or not url.lower().endswith(".pdf"):
                continue
            seen.add(url)
            tried += 1
            try:
                ps = parcels_in(pdf_text(url))
            except Exception as e:  # noqa: BLE001
                log(f"attachment {nm}: {e}")
                ps = []
            for p in ps:
                if p not in parcels:
                    parcels.append(p)
            if parcels or tried >= 3:
                return parcels, docs
    return parcels, docs


# ------------------------------------------------------------------ fetch
def fetch(since, refresh):
    today = dt.date.today().isoformat()
    start = (dt.date.fromisoformat(since) - dt.timedelta(days=LOOKBACK_DAYS)).isoformat()
    refresh = [r.strip() for r in (refresh or []) if r and r.strip()]

    # DRC applications
    drc_raw = get_all("matters", f"MatterBodyId eq {BODY_DRC} and MatterIntroDate ge datetime'{start}'")
    for r in refresh:
        if re.fullmatch(r"\d{2}-\d{8}", r) and not any(r in (m.get("MatterTitle") or "") for m in drc_raw):
            drc_raw += get_all("matters", f"MatterBodyId eq {BODY_DRC} and substringof('{esc(r)}',MatterTitle)")
    apps = {}
    for m in drc_raw:
        d = parse_drc(m)
        if drc_in_scope(d):
            apps.setdefault(d["pn"], []).append(d)
    log(f"{len(drc_raw)} DRC matters since {start}; {len(apps)} in-scope applications")

    # hearing matters (P&Z / BCC)
    hm = get_all("matters", f"(MatterBodyId eq {BODY_PZ} or MatterBodyId eq {BODY_BCC}) and "
                            f"MatterIntroDate ge datetime'{start}'")
    for r in refresh:
        if not re.fullmatch(r"\d{2}-\d{8}", r) and not any(r in (m.get("MatterTitle") or "") for m in hm):
            hm += get_all("matters", f"(MatterBodyId eq {BODY_PZ} or MatterBodyId eq {BODY_BCC}) and "
                                     f"substringof('{esc(r)}',MatterTitle)")
    hm = [m for m in {m["MatterId"]: m for m in hm}.values()
          if (m.get("MatterTypeName") in HEARING_TYPES or m.get("MatterBodyId") == BODY_PZ)
          and h_in_scope(m.get("MatterTitle"))]
    groups = {}
    for m in hm:
        case = h_case(m["MatterTitle"])
        zkey = case.split("/")[0] if case else None
        key = zkey or ("name:" + " ".join(sorted(tokens(h_name(m["MatterTitle"])))))
        # a later matter may carry the Z number while an earlier one only has the SS number
        if not zkey and case:
            for k, g in groups.items():
                if any(case in (h_case(x["MatterTitle"]) or "") for x in g):
                    key = k
                    break
        groups.setdefault(key, []).append(m)
    log(f"{len(hm)} in-scope hearing matters in {len(groups)} cases")

    # link hearing cases to DRC applications
    links = {}
    for key, ms in groups.items():
        names = {h_name(m["MatterTitle"]) for m in ms}
        first_h = min(d10(m.get("MatterIntroDate")) or "9999" for m in ms)
        acres = next((acres_in(m["MatterTitle"]) for m in ms if acres_in(m["MatterTitle"])), None)
        planners = " ".join(re.findall(r"\(([A-Z][a-z]+ [A-Z][A-Za-z'-]+),\s*[^()]*Planner[^()]*\)",
                                       " ".join(m["MatterTitle"] for m in ms)))
        best, bscore = None, 0.0
        for pn, ds in apps.items():
            if min(d["date"] for d in ds) > first_h:
                continue
            sim = max(name_sim(n, d["name"]) for n in names for d in ds)
            a2 = next((acres_in(d["desc"]) for d in ds if acres_in(d["desc"])), None)
            score = sim
            if acres and a2 and abs(acres - a2) <= max(0.05, 0.02 * acres):
                score += 0.4
            if planners and any(d["pm"] and d["pm"] in planners for d in ds):
                score += 0.2
            if score > bscore:
                best, bscore = pn, score
        if best and bscore >= 0.6:
            links[key] = best
    # parcel-based linking for the rest needs attachment parcels; done lazily below

    cases = {}  # app_num -> {"drc": [...], "hearing": [...]}
    for pn, ds in apps.items():
        cases[pn] = {"drc": ds, "hearing": [], "hkeys": []}
    unlinked = []
    for key, ms in groups.items():
        pn = links.get(key)
        if pn:
            cases[pn]["hearing"] += ms
            cases[pn]["hkeys"].append(key)
        else:
            unlinked.append(key)

    att_cache = {}
    for key in unlinked:
        ms = groups[key]
        first_h = min(d10(m.get("MatterIntroDate")) or "9999" for m in ms)
        hit_refresh = any((h_case(m["MatterTitle"]) or "").split("/")[0] in refresh or
                          h_name(m["MatterTitle"]) in refresh for m in ms)
        if first_h < since and not hit_refresh:
            continue  # first appeared before the window; its application (if any) is older too
        try:
            att_cache[key] = attachment_info(ms)
        except Exception as e:  # noqa: BLE001
            log(f"{key}: attachments failed: {e}")
            att_cache[key] = ([], None)
        ps = set(att_cache[key][0])
        hit = None
        if ps:
            for pn, ds in apps.items():
                if min(d["date"] for d in ds) <= first_h and any(set(d["parcels"]) & ps for d in ds):
                    hit = pn
                    break
        if hit:
            cases[hit]["hearing"] += ms
            cases[hit]["hkeys"].append(key)
            cases[hit]["att"] = att_cache[key]
        else:
            case = next((h_case(m["MatterTitle"]) for m in ms if h_case(m["MatterTitle"])), None)
            app_num = case or h_name(ms[0]["MatterTitle"])
            log(f"hearing case {app_num!r} not linked to a DRC application")
            cases.setdefault(app_num, {"drc": [], "hearing": [], "hkeys": []})
            cases[app_num]["hearing"] += ms
            cases[app_num]["hkeys"].append(key)
            cases[app_num]["att"] = att_cache[key]

    out = []
    for app_num, c in sorted(cases.items()):
        first = min([d["date"] for d in c["drc"]] + [d10(m.get("MatterIntroDate")) or "9999" for m in c["hearing"]])
        in_refresh = app_num in refresh or any(
            (h_case(m["MatterTitle"]) or "").split("/")[0] in refresh for m in c["hearing"])
        if first < since and not in_refresh:
            continue
        try:
            rec = build(app_num, c, today)
        except Exception as e:  # noqa: BLE001
            log(f"{app_num}: skipped ({e})")
            continue
        if rec:
            out.append(rec)
    return out


def histories(m):
    try:
        return get(f"{LEG}/matters/{m['MatterId']}/histories").json()
    except Exception as e:  # noqa: BLE001
        log(f"histories {m['MatterId']}: {e}")
        return []


def build(app_num, c, today):
    drc = sorted(c["drc"], key=lambda d: d["date"])
    hms = sorted(c["hearing"], key=lambda m: (d10(m.get("MatterAgendaDate")) or d10(m.get("MatterIntroDate")) or "",
                                               m["MatterId"]))
    if "att" not in c and hms:
        need = not drc or any(d["more_parcels"] for d in drc) or not any(d["parcels"] for d in drc)
        c["att"] = attachment_info(hms) if need else ([], None)
    att_parcels, docs = c.get("att") or ([], None)

    # hearings
    hmap = {}
    withdrawn_title = False
    for m in hms:
        body = body_label(m.get("MatterBodyId"))
        title = m.get("MatterTitle") or ""
        if re.search(r"\bWITHDRAWN\b|withdrawn by the applicant", title, re.I):
            withdrawn_title = True
        hs = histories(m)
        for h in hs:
            date = d10(h.get("MatterHistoryActionDate"))
            if not date:
                continue
            res = classify(body, h.get("MatterHistoryActionName"), h.get("MatterHistoryPassedFlagName"),
                           h.get("MatterHistoryActionText"), title)
            pf = (h.get("MatterHistoryPassedFlagName") or "").lower()
            rank = (0 if res is None else 1 + (pf == "pass") * 2 + (pf == "") * 1
                    + (res in ("Approved", "Denied", "Withdrawn")) * 0.5)
            k = (body, date)
            cur = hmap.get(k)
            if cur is None or rank >= cur["_rank"]:
                hmap[k] = {"body": body, "date": date, "result": res, "_rank": rank}
        ad = d10(m.get("MatterAgendaDate"))
        if ad and not hs:
            hmap.setdefault((body, ad), {"body": body, "date": ad, "result": None, "_rank": 0})
    for d in drc:
        if d["is_meeting"] and d["agenda"]:
            hmap.setdefault(("DRC", d["agenda"]), {"body": "DRC", "date": d["agenda"], "result": None})
    hearings = sorted(({k: v for k, v in h.items() if k != "_rank"} for h in hmap.values()),
                      key=lambda h: (h["date"], h["body"] != "DRC", h["body"] == "BCC"))
    public = [h for h in hearings if h["body"] != "DRC"]

    # status
    past = [h for h in public if h["date"] <= today]
    future = [h for h in public if h["date"] > today]
    final = [h for h in public if h["body"] == "BCC" and h["result"] in ("Approved", "Denied")]
    wd = [h for h in public if h["result"] == "Withdrawn"]
    decision = None
    if wd or withdrawn_title:
        status, raw = "Withdrawn", (f"{wd[-1]['body']} {wd[-1]['date']}: withdrawn" if wd else "Withdrawn (agenda title)")
    elif final and not future:
        status, decision = final[-1]["result"], final[-1]["date"]
        raw = f"BCC {decision}: {final[-1]['result']}"
    elif past and past[-1]["result"] == "Continued":
        status, raw = "Continued", f"{past[-1]['body']} {past[-1]['date']}: continued"
        if future:
            raw += f"; next {future[0]['body']} {future[0]['date']}"
    elif future:
        status, raw = "Hearing Scheduled", f"next {future[0]['body']} {future[0]['date']}"
        if past and past[-1]["result"]:
            raw = f"{past[-1]['body']} {past[-1]['date']}: {past[-1]['result']}; " + raw
    elif past and past[-1]["result"] is None:
        status, raw = "Hearing Scheduled", f"{past[-1]['body']} {past[-1]['date']}: result not yet posted"
    elif past:
        status, raw = "Filed", f"{past[-1]['body']} {past[-1]['date']}: {past[-1]['result']}; next hearing not yet scheduled"
    else:
        dm = [h for h in hearings if h["body"] == "DRC"]
        status, raw = "Filed", (f"Under DRC review (DRC {dm[-1]['date']})" if dm else "Under staff review (DRC comments)")

    # descriptive fields: prefer the latest hearing title, fall back to the DRC application
    main_t = re.sub(r"\s+", " ", hms[-1]["MatterTitle"]).strip() if hms else None
    d0 = drc[-1] if drc else None
    kind_txt = " ".join(filter(None, [main_t, " ".join(d["kind"] + " " + (d["desc"] or "") for d in drc)]))
    is_flu = bool(re.search(r"Future Land Use (?:Map )?Amendment|\bSSFLUA\b|\bLSFLUA\b|Land Use Amendment|\d{2}\.\d{2}(?:SS|LS)",
                            kind_txt, re.I))
    frm_z = to_z = frm_f = to_f = None
    src = main_t or (d0["desc"] if d0 else "") or ""
    m = re.search(r"Rezone\s+from\s+(.+?)\s+to\s+(.+?)(?=\s+(?:for|to allow|in order|on|located|at)\b|[;,]|$)",
                  src, re.I)
    if m:
        frm_z, to_z = clean(m.group(1)), clean(m.group(2))
        cut = re.match(r"(.+?\))\s+(?:to|for|in|with|as)\b", to_z or "")
        if cut:
            to_z = cut.group(1)
    m = re.search(r"(?:Future Land Use (?:Map )?Amendment|SSFLUA|LSFLUA|Land Use Amendment)\s+from\s+(.+?)\s+to\s+(.+?)"
                  r"(?=\s+and\s+(?:a\s+)?(?:Rezone|PD)|\s+(?:for|on|located)\b|[;,]|$)", src, re.I)
    if m:
        frm_f, to_f = clean(m.group(1)), clean(m.group(2))
    is_pd = bool(to_z and re.search(r"\bPD\b|Planned Development|\bPUD\b", to_z)) or \
        bool(re.search(r"PD Major Amendment|PD Rezone|PD Amendment", kind_txt, re.I))
    category = "Comp Plan Amendment" if is_flu else ("PD" if is_pd else "Rezoning")

    acres = acres_in(main_t) if main_t else None
    tot = re.search(r"total (?:land )?area of (?:approximately )?([\d,]*\.?\d+)\s*acres", main_t or "", re.I)
    if tot:
        acres = to_float(tot.group(1))
    if acres is None:
        acres = next((acres_in(d["desc"]) for d in reversed(drc) if acres_in(d["desc"])), None)
    parcels = []
    for d in drc:
        for p in d["parcels"]:
            if p not in parcels:
                parcels.append(p)
    for p in att_parcels:
        if p not in parcels:
            parcels.append(p)

    applicant = None
    if main_t:
        mm = re.findall(r"\(([^()]*(?:Applicant|Applicants|Appellant)[^()]*)\)", main_t)
        if mm:
            applicant = clean(re.sub(r",?\s*Applicants?$", "", mm[-1]))
    if not applicant and d0:
        applicant = d0["applicant"] or d0["consultant"]
    address = None
    if main_t:
        mm = re.search(r"located (?:at|on) (.+?);", main_t)
        address = clean(mm.group(1)) if mm else None
    if not address and d0 and d0["desc"]:
        mm = re.search(r"\b(?:located )?(?:on|at) (the (?:north|south|east|west)[a-z]*\s+(?:side|corner)\b.*)$", d0["desc"])
        address = clean(mm.group(1)) if mm else None
    hcase = next((h_case(m["MatterTitle"]) for m in reversed(hms) if h_case(m["MatterTitle"])), None)
    if main_t:
        desc = re.sub(r"^.*?[-\u2013]\s*(?=Consider)", "", main_t)
        desc = re.sub(r"\s*\(Z\d{4}.*$|\s*;\s*\(.*$", "", desc)
    else:
        desc = d0["desc"] if d0 else None
    if desc and hcase and hcase not in desc:
        desc = f"{desc} [case {hcase}]"
    if desc and len(desc) > 400:
        desc = desc[:397] + "..."
    app_type = d0["kind"] if d0 else None
    if not app_type and main_t:
        mm = re.match(r".*?((?:Small Scale |Large Scale )?Future Land Use Map Amendment.*?Rezone|PD Major Amendment Rezone|"
                      r"PD Rezone|Rezone)\b", h_name_full(main_t), re.I)
        app_type = clean(mm.group(1)) if mm else None

    if hms:
        mm_ = next((m for m in reversed(hms) if m.get("MatterBodyId") == BODY_BCC), hms[-1])
        url = f"{SITE}/LegislationDetail.aspx?ID={mm_['MatterId']}&GUID={mm_['MatterGuid']}"
    else:
        url = f"{SITE}/LegislationDetail.aspx?ID={d0['matter']['MatterId']}&GUID={d0['matter']['MatterGuid']}"
    filed = min([d["date"] for d in drc] + [d10(m.get("MatterIntroDate")) for m in hms if m.get("MatterIntroDate")])
    pname = None
    if hms:
        pname = h_name(hms[-1]["MatterTitle"])
    elif d0:
        pname = d0["name"].title() if d0["name"] and d0["name"].isupper() else d0["name"]
    return {
        "jurisdiction": "Seminole County", "county": "Seminole", "app_num": app_num, "category": category,
        "app_type": app_type, "project_name": pname, "description": desc, "applicant": applicant,
        "parcel_ids": parcels, "address": address, "acres": acres,
        "from_zoning": frm_z, "to_zoning": to_z,
        "from_flu": frm_f if category == "Comp Plan Amendment" else None,
        "to_flu": to_f if category == "Comp Plan Amendment" else None,
        "filed_date": filed, "status": status, "status_raw": raw, "hearings": hearings,
        "decision_date": decision, "url": url, "docs_url": docs,
        "lat": None, "lon": None, "source": SOURCE,
    }


def h_name_full(title):
    return re.split(r"\s*[-\u2013]\s*(?=Consider)", re.sub(r"\s+", " ", title or ""))[0]


if __name__ == "__main__":
    since = sys.argv[1] if len(sys.argv) > 1 else (dt.date.today() - dt.timedelta(days=14)).isoformat()
    refresh = sys.argv[2].split(",") if len(sys.argv) > 2 else []
    t0 = time.time()
    recs = fetch(since, refresh)
    print(json.dumps(recs, indent=2, ensure_ascii=False))
    from collections import Counter
    log(f"{len(recs)} records in {time.time() - t0:.0f}s; status={dict(Counter(r['status'] for r in recs))}; "
        f"category={dict(Counter(r['category'] for r in recs))}")
