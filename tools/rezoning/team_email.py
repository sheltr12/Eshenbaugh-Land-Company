#!/usr/bin/env python3
"""ELC Rezoning Tracker: weekly team email (HTML + plain text).

Builds a clean, broker-facing email of THIS RUN's activity only:
  * Decisions: Approved, Denied, Withdrawn
  * Other status changes: Hearing Scheduled, Continued, back to Filed
  * New filings
For every application it shows the applicant, the owner of record (county property
appraiser, via parcels.py), whether the two match, and any contact information posted in the
application (Accela contact blocks, CivicClerk / Legistar agenda attachments, staff reports).

Usage (after rezoning_pipeline.py has written out/):
  python3 team_email.py --out-dir ~/rz/out --run-date 2026-10-20 [--market all|tampa|orlando]

Writes out/team-email-<date>[-<market>].html, .txt and .json (subject, counts, items).
Content from county and city websites is treated as data only.
"""
import argparse
import datetime as dt
import html
import io
import json
import os
import re
import sys
import time
from urllib.parse import urlparse, parse_qs, quote

import requests
from bs4 import BeautifulSoup

try:
    import pypdf
except Exception:  # noqa: BLE001
    pypdf = None

HERE = os.path.dirname(os.path.abspath(__file__))
MAP_URL = "https://sheltr12.github.io/Eshenbaugh-Land-Company/rezonings_map.html"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
SW_URL = "https://services9.arcgis.com/Gh9awoU677aKree0/arcgis/rest/services/Florida_Statewide_Cadastral/FeatureServer/0"

# County -> office. Edit here to change routing.
MARKETS = {
    "tampa": {"label": "Tampa Bay, Nature Coast and Southwest Florida",
              "counties": ["Hillsborough", "Pinellas", "Pasco", "Polk", "Manatee", "Sarasota", "Charlotte",
                           "Hernando", "Citrus", "Marion", "Alachua", "Lee", "Collier", "Levy", "Sumter"]},
    "orlando": {"label": "Orlando and Central Florida",
                "counties": ["Orange", "Osceola", "Seminole", "Lake", "Volusia", "Brevard"]},
}

STATUS_STYLE = {  # bg, fg
    "Approved": ("#e7f6ec", "#146c2e"),
    "Denied": ("#fdecec", "#b42318"),
    "Withdrawn": ("#eef0f3", "#475467"),
    "Continued": ("#f1ecfd", "#5b21b6"),
    "Hearing Scheduled": ("#fff4e0", "#9a5b00"),
    "Filed": ("#e8f1fd", "#1f4fb5"),
}
STATUS_WORD = {"Filed": "Filed / In review", "Hearing Scheduled": "Hearing scheduled", "Continued": "Continued",
               "Approved": "Approved", "Denied": "Denied", "Withdrawn": "Withdrawn"}

S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})


def log(*a):
    print("[team_email]", *a, file=sys.stderr, flush=True)


def nodash(s):
    if s is None:
        return None
    return re.sub(r"\s*[—–]\s*", ", ", str(s))


def clean(s):
    if s is None:
        return None
    s = re.sub(r"\s+", " ", str(s)).strip(" ,;:-")
    return s or None


def get(url, tries=3, timeout=40, **kw):
    last = None
    for i in range(tries):
        try:
            r = S.get(url, timeout=timeout, **kw)
            if r.status_code == 200:
                return r
            last = f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001
            last = str(e)
        time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"GET failed {url[:120]}: {last}")


# ----------------------------------------------------------------- owner of record

def owners_for(rec, budget):
    """[{'owner','parcel','pa_url','how'}] from parcels.lookup, else statewide layer at the point."""
    out = []
    ids = rec.get("apns_src") or rec.get("parcel_ids") or []
    county = rec.get("county")
    if ids and county and time.time() < budget:
        try:
            sys.path.insert(0, HERE)
            import parcels  # noqa: E402
            res = parcels.lookup(county, ids[:25]) or {}
            disp = dict(zip(rec.get("apns_src") or [], rec.get("apns") or []))
            for raw in ids[:25]:
                r = res.get(raw)
                if r and r.get("owner"):
                    out.append({"owner": clean(r["owner"]), "parcel": disp.get(raw) or raw,
                                "pa_url": r.get("pa_url"), "how": "parcel"})
        except Exception as e:  # noqa: BLE001
            log(f"owner lookup failed {rec.get('id')}: {e}")
    if not out and rec.get("lat") is not None and rec.get("lon") is not None and time.time() < budget:
        try:
            r = S.post(SW_URL + "/query", data={
                "geometry": f"{rec['lon']},{rec['lat']}", "geometryType": "esriGeometryPoint", "inSR": 4326,
                "spatialRel": "esriSpatialRelIntersects", "outFields": "OWN_NAME,PARCEL_ID,PHY_ADDR1",
                "returnGeometry": "false", "f": "json"}, timeout=40)
            for f in (r.json().get("features") or [])[:1]:
                a = f.get("attributes") or {}
                if a.get("OWN_NAME"):
                    out.append({"owner": clean(a["OWN_NAME"]), "parcel": clean(a.get("PARCEL_ID")),
                                "pa_url": None, "how": "point"})
        except Exception as e:  # noqa: BLE001
            log(f"statewide point lookup failed {rec.get('id')}: {e}")
    seen, uniq = set(), []
    for o in out:
        k = norm_name(o["owner"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(o)
    return uniq, len(ids)


# ----------------------------------------------------------------- name matching

STOP = set("""LLC L L C INC INCORPORATED CORP CORPORATION CO COMPANY LP LLLP LTD LIMITED PARTNERSHIP TRUST TRUSTEE
TRUSTEES TR TRS TTEE THE OF AND A AN ETAL ET AL FAMILY REVOCABLE LIVING AS PA P PLLC PL ESTATE FBO DTD UAD
U/A AGREEMENT DATED JR SR II III IV MR MRS MS H W HW JTWRS JT WROS TEN ENT RLT FL FLORIDA""".split())
CONSULTANT_RX = re.compile(r"\b(engineer\w*|associates|consult\w*|kimley|planning|design|survey\w*|law|attorney|"
                           r"esq|p\.?\s?a\.?$|pllc|partners\s+inc|group|land\s+use|permitting|horn|"
                           r"\bP\.?E\.?\b|AICP|architect\w*)\b", re.I)
GOV_RX = re.compile(r"\b(city of|county|board of county|school board|district|state of|town of|authority|"
                    r"department of|trustees of the internal)\b", re.I)


def norm_name(s):
    s = (s or "").upper().replace("&", " AND ")
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    return " ".join(t for t in s.split() if t not in STOP)


def toks(s):
    return set(norm_name(s).split())


def name_match(a, b):
    ta, tb = toks(a), toks(b)
    if not ta or not tb:
        return False
    if ta <= tb or tb <= ta:
        return min(len(ta), len(tb)) >= 1 and (len(ta & tb) >= 2 or max(len(ta), len(tb)) == 1
                                                or len(ta & tb) == min(len(ta), len(tb)) and min(len(ta), len(tb)) >= 1
                                                and any(len(t) > 3 for t in ta & tb))
    inter = len(ta & tb)
    return inter >= 2 and inter / min(len(ta), len(tb)) >= 0.66


SPLIT_RX = re.compile(r"\s+(?:as\s+agent\s+for|agent\s+for(?:\s+owners?)?|on\s+behalf\s+of|for|c/o|representing)\s+"
                      r"|\s*/\s*|\s*;\s*|\(\s*owners?\s*:?\s*|\)\s*", re.I)


def parties(applicant):
    """'Kimley Horn and Associates, Inc. for DRJ Land Company, LLC' -> ['Kimley Horn...', 'DRJ Land Company, LLC']"""
    if not applicant:
        return []
    parts = [clean(p) for p in SPLIT_RX.split(applicant)]
    out = []
    for p in parts:
        if not p:
            continue
        p = re.sub(r"^(?:the\s+)?(?:owners?|applicants?|agent)\s*:?\s+", "", p, flags=re.I)
        # "Christopher Crosby Johnson, Albert McKimmie, Susan Incatasciato" -> separate people
        if p.count(",") >= 2 and not re.search(r"\b(LLC|Inc|Corp|LP|Ltd|P\.A)\b", p, re.I):
            out += [clean(x) for x in p.split(",") if clean(x)]
        else:
            out.append(p)
    return [p for p in out if p and len(p) > 2]


COMMON = set("""ROAD DRIVE STREET AVENUE BLVD HIGHWAY PARK LAKE LAKES NORTH SOUTH EAST WEST PROJECT REZONING PUD
PROPERTY PARCELS ADDITIONAL CITY COUNTY FLORIDA PLACE POINTE PLAZA CENTER COMMONS VILLAGE ESTATES PRESERVE""".split())


def ownership_check(applicant, owners, app_owner=None, project=None):
    """-> (code, headline, detail). code in match | agent | different | unknown"""
    owner_names = [o["owner"] for o in owners] + ([app_owner] if app_owner else [])
    if not applicant:
        pt = {t for t in toks(project) if t not in COMMON and len(t) >= 3}
        for o in owner_names:
            common = pt & toks(o)
            if common:
                return ("unknown", "Applicant not listed online (likely the owner)",
                        f"Project name shares \"{' '.join(sorted(common))}\" with owner of record {o}.")
        if owner_names:
            return ("unknown", "Applicant not listed online", "The posted record does not name an applicant.")
        return ("unknown", "Could not verify", "No applicant or owner of record found.")
    if not owner_names:
        return ("unknown", "Owner of record not found", "No parcel ID or mapped location to check against.")
    ps = parties(applicant) or [applicant]
    hits = [(i, p, o) for i, p in enumerate(ps) for o in owner_names if name_match(p, o)]
    if hits:
        i, p, o = hits[0]
        if i == 0:
            return ("match", "Applicant IS the owner of record", f"{p} matches owner {o}.")
        return ("agent", "Filed by an agent for the owner of record",
                f"{ps[0]} filed on behalf of {p}, which matches owner {o}.")
    first = ps[0]
    if GOV_RX.search(applicant) and any(GOV_RX.search(o) for o in owner_names):
        return ("match", "Government applicant on public land", f"{applicant} / {owner_names[0]}")
    if CONSULTANT_RX.search(first):
        return ("different", "Applicant is NOT the owner of record",
                f"{first} appears to be a consultant or agent; the owner was not named as the applicant.")
    return ("different", "Applicant is NOT the owner of record",
            "Applicant could be a contract buyer, developer or agent. Worth a call.")


# ----------------------------------------------------------------- contact extraction

EMAIL_RX = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
PHONE_RX = re.compile(r"(?<!\d)(?:\+?1[\s.\-]?)?\(?([2-9]\d{2})\)?[\s.\-]*(\d{3})[\s.\-]*(\d{4})(?!\d)")
GOV_MAIL = re.compile(r"(\.gov$|\.fl\.us$|\.us$|ocfl\.net$|myfloridacounty|hcfl|pascocountyfl|polk-county|"
                      r"citrusbocc|marionfl\.org|lakecountyfl|seminolecountyfl|osceola\.org|volusia\.org|"
                      r"brevardfl|stcloudfl|leesburgflorida|apopka\.net|clermontfl|cityoforlando\.net|"
                      r"mypinellas|scgov\.net|charlottecountyfl|mymanatee|hernandocounty|alachuacounty|"
                      r"tampagov|planhillsborough|kissimmee|wintergarden|minneola|groveland|oviedo|ocoee|"
                      r"safety-harbor|civicclerk|accela|legistar|granicus|myfwc|swfwmd|sjrwmd|floridadep|fdot)", re.I)
ROLE_LABELS = (r"Applicant\s*/\s*Owner|Owner\s*/\s*Applicant|Applicant\s*/\s*Agent|Agent\s*/\s*Applicant|"
               r"Applicant\(s\)|Applicants?|Property\s+Owners?|Owners?\s+of\s+Record|Owners?|Authorized\s+Agent|"
               r"Agent|Representative|Developer|Petitioner|Engineer\s+of\s+Record|Contact\s+Person|Consultant")
LABEL_LINE = re.compile(rf"(?im)^[ \t]*(?:[A-Z0-9]\.\s*)?({ROLE_LABELS})[ \t]*(?:\(s\))?[ \t]*(?::|\-|–)[ \t]*(.{{2,160}})$")


def fmt_phone(m):
    return f"({m.group(1)}) {m.group(2)}-{m.group(3)}"


def norm_role(r):
    r = re.sub(r"\s+", " ", (r or "").strip()).title()
    r = r.replace("Applicant(S)", "Applicant").replace("Owners Of Record", "Owner").replace("Owner Of Record", "Owner")
    return r


def text_contacts(text, where):
    """Labeled contacts (role: name) with phones/emails found in the next few lines."""
    out = []
    if not text:
        return out
    text = text.replace("\r", "")
    for m in LABEL_LINE.finditer(text):
        role, val = norm_role(m.group(1)), clean(m.group(2))
        if not val or re.match(r"(?i)(n/?a|none|tbd|same|see\b|the\b|is\b|has\b|shall|will|requests?|staff)", val):
            continue
        if len(val) > 140 or val.lower().startswith(("the applicant", "applicant")):
            continue
        if re.search(r"(?i)density|\bunits?\b|\bacres?\b|stories|allowed|zoning|land use|exhibit|section|ordinance|"
                     r"\b(?:yes|no)\b\s*$|\d+\s*/\s*\d", val):
            continue
        tail = text[m.end(): m.end() + 260]
        nxt = LABEL_LINE.search(tail)
        if nxt:
            tail = tail[: nxt.start()]
        emails = [e for e in EMAIL_RX.findall(val + " " + tail) if not GOV_MAIL.search(e.split("@")[1])]
        phones = [fmt_phone(p) for p in PHONE_RX.finditer(val + " " + tail)]
        name = clean(EMAIL_RX.sub("", PHONE_RX.sub("", val)))
        name = re.sub(r"(?i)\b(phone|tel|telephone|email|e-mail|fax|ph)\b\s*:?.*$", "", name or "").strip(" ,;:")
        if not name or len(name) < 3:
            continue
        out.append({"role": role, "name": name, "company": None, "phones": phones[:2], "emails": emails[:2],
                    "address": None, "where": where})
    return out


def loose_emails(text):
    return [e for e in dict.fromkeys(EMAIL_RX.findall(text or "")) if not GOV_MAIL.search(e.split("@")[1])]


def pdf_text(url, needles, max_bytes=160_000_000, max_pages=14):
    if pypdf is None:
        return ""
    r = get(url, timeout=90, stream=True)
    buf = io.BytesIO()
    for chunk in r.iter_content(256 * 1024):
        buf.write(chunk)
        if buf.tell() > max_bytes:
            log(f"pdf too big, truncated: {url[:90]}")
            return ""
    buf.seek(0)
    if not buf.getvalue()[:5].startswith(b"%PDF"):
        return BeautifulSoup(buf.getvalue(), "lxml").get_text("\n")
    rd = pypdf.PdfReader(buf)
    n = len(rd.pages)
    if n <= max_pages:
        idx = list(range(n))
    else:  # packet: pages mentioning the case, plus the two after each
        idx, hits = [], []
        for i in range(n):
            try:
                t = rd.pages[i].extract_text() or ""
            except Exception:  # noqa: BLE001
                t = ""
            if any(nd and nd.lower() in t.lower() for nd in needles):
                hits.append(i)
        for h in hits[:6]:
            idx += [h, h + 1, h + 2]
        idx = sorted({i for i in idx if i < n})[:max_pages]
    parts = []
    for i in idx:
        try:
            parts.append(rd.pages[i].extract_text() or "")
        except Exception:  # noqa: BLE001
            pass
    return "\n".join(parts)


DOC_GOOD = re.compile(r"staff|report|application|summary|memo|narrative|justification|agreement|letter|affidavit|"
                      r"authoriz|request|backup|packet|exhibit\s*a\b|ordinance|resolution", re.I)
DOC_BAD = re.compile(r"aerial|photo|map|zoning\s*-|land use\s*-|flood|wetland|legal desc|survey|concept|site plan|"
                     r"surrounding|district|location|vicinity|ad\b|notice|sign|traffic|soil|plat", re.I)


def needles_of(rec):
    n = [rec.get("app_num")]
    m = re.search(r"([A-Z]{2,6}\s?-?\d{2,4}-?\d{2,6})", rec.get("app_num") or "")
    if m:
        n.append(m.group(1))
    if rec.get("project_name") and len(rec["project_name"]) > 6:
        n.append(rec["project_name"][:40])
    return [x for x in n if x]


def accela_contacts(url):
    sys.path.insert(0, os.path.join(HERE, "adapters"))
    h = get(url, timeout=60).text
    s = BeautifulSoup(h, "lxml")
    out, app_owner = [], None
    blocks = []
    rd = s.find(id=re.compile(r"PermitDetailList1_TBPermitDetailTest$"))
    if rd:
        for td in rd.find_all("td", class_="td_parent_left"):
            hh = td.find("h1")
            if not hh:
                continue
            label = clean(hh.get_text(" ")).rstrip(":") if hh.get_text(strip=True) else ""
            content = hh.find_next_sibling()
            if content is None:
                continue
            if label.lower().startswith("owner"):
                t = clean(content.get_text(" "))
                app_owner = clean(re.split(r"\s{2,}|\*|\d{2,}\s", content.get_text("\n").strip().split("\n")[0])[0]) if t else None
            if label.lower().startswith(("applicant", "licensed professional", "owner")):
                blocks.append((label, content))
    rc = s.find(id=re.compile(r"PermitDetailList1_RelatContactList$"))
    if rc is not None:
        for blk in rc.find_all("div", class_="MoreDetail_ItemCol1"):
            h2 = blk.find("h2")
            blocks.append((clean(h2.get_text(" ")) if h2 else "Contact", blk))
    for label, b in blocks:
        def g(rx):
            return [clean(e.get_text(" ")) for e in b.find_all(class_=re.compile(rx)) if clean(e.get_text(" "))]
        person = " ".join(g(r"^contactinfo_(firstname|middlename|lastname|fullname)$"))
        biz = (g(r"^contactinfo_businessname") or [None])[0]
        btxt = b.get_text("\n")
        emails = list(dict.fromkeys(e.lower() for e in EMAIL_RX.findall(" ".join(g(r"^contactinfo_email")) + " " + btxt)))
        phones = []
        for ptxt in g(r"^contactinfo_phone") or [btxt]:
            phones += [fmt_phone(m) for m in PHONE_RX.finditer(ptxt)]
        addr = None
        am = re.search(r"Mailing\s+(.+?)(?:United States|$)", re.sub(r"\s+", " ", btxt))
        if am:
            addr = clean(am.group(1))
        if not person and not biz:
            continue
        role = norm_role(re.sub(r"(?i)\s*information$", "", label or "Contact"))
        out.append({"role": role, "name": person or biz, "company": biz if person else None,
                    "phones": list(dict.fromkeys(phones))[:2], "emails": emails[:2], "address": addr,
                    "where": "Application record"})
    return out, app_owner


def civicclerk_texts(url, rec):
    m = re.match(r"https?://([a-z0-9]+)\.portal\.civicclerk\.com/event/(\d+)", url or "")
    if not m:
        return []
    tenant, eid = m.groups()
    api = f"https://{tenant}.api.civicclerk.com/v1"
    ev = get(f"{api}/Events/{eid}").json()
    if not ev.get("agendaId"):
        return []
    mt = get(f"{api}/Meetings/{ev['agendaId']}").json()
    pk = [f for f in (ev.get("publishedFiles") or []) if re.search(r"packet", f.get("type") or "", re.I) and f.get("fileId")]
    pk_texts = []
    if pk and not rec.get("docs_url"):
        try:
            pk_texts.append(("Agenda packet", get(f"{api}/Meetings/GetMeetingFileStream(fileId={pk[0]['fileId']},plainText=true)",
                                                  timeout=120).text))
        except Exception as e:  # noqa: BLE001
            log(f"packet text failed: {e}")
    needles = [n.lower() for n in needles_of(rec)]
    words = [w for w in re.findall(r"[A-Za-z]{4,}", rec.get("project_name") or "") if w.lower() not in
             ("road", "drive", "street", "avenue", "project", "rezoning", "property", "parcels", "additional")]

    def walk(items):
        for it in items or []:
            yield it
            yield from walk(it.get("childItems"))
    texts = []
    for it in walk(mt.get("items")):
        blob = " ".join(str(it.get(k) or "") for k in ("agendaObjectItemName", "agendaObjectItemDescription",
                                                         "agendaObjectItemHtmlContent"))
        low = blob.lower()
        hit = any(n in low for n in needles) or (words and sum(w.lower() in low for w in words) >= max(2, len(words) - 1))
        if not hit:
            continue
        texts.append(("Agenda item", BeautifulSoup(blob, "lxml").get_text("\n")))
        atts = [a for a in (it.get("attachmentsList") or []) if a.get("pdfVersionFullPath")]
        atts.sort(key=lambda a: (not DOC_GOOD.search(a.get("fileName") or ""), bool(DOC_BAD.search(a.get("fileName") or ""))))
        for a in [a for a in atts if not DOC_BAD.search(a.get("fileName") or "") or DOC_GOOD.search(a.get("fileName") or "")][:4]:
            if (a.get("fileSize") or 0) > 25_000_000:
                continue
            try:
                texts.append((f"Agenda attachment: {a.get('fileName')}", pdf_text(a["pdfVersionFullPath"], needles_of(rec))))
            except Exception as e:  # noqa: BLE001
                log(f"civicclerk attachment failed: {e}")
        if len(texts) >= 8:
            break
    return texts + pk_texts


def legistar_texts(url, rec):
    h = get(url).text
    s = BeautifulSoup(h, "lxml")
    texts = [("Agenda item", s.get_text("\n"))]
    links = []
    for a in s.find_all("a", href=True):
        t = clean(a.get_text(" ")) or ""
        if "View.ashx?M=F" in a["href"] or a["href"].lower().endswith(".pdf"):
            links.append((t, requests.compat.urljoin(url, a["href"])))
    links.sort(key=lambda x: (not DOC_GOOD.search(x[0]), bool(DOC_BAD.search(x[0]))))
    for t, href in [l for l in links if not DOC_BAD.search(l[0]) or DOC_GOOD.search(l[0])][:4]:
        try:
            texts.append((f"Agenda attachment: {t}", pdf_text(href, needles_of(rec))))
        except Exception as e:  # noqa: BLE001
            log(f"legistar attachment failed: {e}")
    return texts


def windows(text, needles, before=1500, after=4500, cap=8):
    """Long documents (packets) -> only the stretches around mentions of this case."""
    if not text or len(text) < 20000:
        return text or ""
    low = text.lower()
    spans = []
    for nd in needles:
        nd = (nd or "").lower().strip()
        if len(nd) < 5:
            continue
        for m in re.finditer(re.escape(nd), low):
            spans.append((max(0, m.start() - before), m.end() + after))
    if not spans:
        return ""
    spans.sort()
    merged = [list(spans[0])]
    for a, b in spans[1:]:
        if a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return "\n...\n".join(text[a:b] for a, b in merged[:cap])


def title_if_caps(n):
    if n and n.upper() == n and re.search(r"[A-Z]{3}", n):
        return " ".join(w if re.fullmatch(r"(LLC|LP|LLLP|INC|II|III|IV|PA|PE|USA|FL)", w) else w.capitalize() for w in n.split())
    return n


def attribute_emails(emails, texts, rec, contacts):
    """Emails found loose in documents: tie to the applicant's firm when the domain matches."""
    out, rest = [], []
    ps = parties(rec.get("applicant")) or []
    for e in emails:
        local, dom = e.split("@", 1)
        dtok = re.sub(r"[^a-z]", " ", dom.split(".")[0].lower()).split()
        firm = next((p for p in ps if any(t.lower() in dtok or any(t.lower().startswith(d) and len(d) >= 4 for d in dtok)
                                          for t in toks(p) if len(t) >= 4)), None)
        if not firm:
            rest.append(e)
            continue
        person = None
        m = re.fullmatch(r"([a-z]+)[._]([a-z]+)", local, re.I)
        if m:
            person = f"{m.group(1).capitalize()} {m.group(2).capitalize()}"
        phone = None
        for _, t in texts:
            i = t.find(e)
            if i >= 0:
                pm = PHONE_RX.search(t[max(0, i - 250): i + 250])
                if pm:
                    phone = fmt_phone(pm)
                    break
        out.append({"role": "Agent" if firm != ps[0] or len(ps) > 1 else "Applicant", "name": person or firm,
                    "company": firm if person else None, "phones": [phone] if phone else [], "emails": [e],
                    "address": None, "where": "Agenda documents"})
    return out, rest


def contacts_for(rec):
    """-> (contacts, app_owner, note)"""
    contacts, app_owner, texts, errors = [], None, [], []
    url, docs = rec.get("url") or "", rec.get("docs_url") or ""
    try:
        if "accela.com" in url:
            c, app_owner = accela_contacts(url)
            contacts += c
        elif "portal.civicclerk.com" in url:
            texts += civicclerk_texts(url, rec)
        elif "legistar.com/LegislationDetail" in url:
            texts += legistar_texts(url, rec)
        elif url and not re.search(r"fasttrack\.ocfl|experience\.arcgis|data\.cityoforlando|connectlivepermits|"
                                   r"planhillsborough\.org|tylerhost", url):
            r = get(url)
            texts.append(("Application page", BeautifulSoup(r.text, "lxml").get_text("\n")))
    except Exception as e:  # noqa: BLE001
        errors.append(str(e)[:120])
    try:
        if docs and "sharepoint.com" not in docs:
            if "legistar.com/LegislationDetail" in docs:
                texts += legistar_texts(docs, rec)
            elif "GetMeetingFileStream" in docs:
                t = get(docs.replace("plainText=false", "plainText=true"), timeout=120).text
                texts.append(("Agenda packet", t))
            elif docs != url:
                texts.append(("Staff report / agenda document", pdf_text(docs, needles_of(rec))))
    except Exception as e:  # noqa: BLE001
        errors.append(str(e)[:120])
    loose, prose = [], None
    texts = [(w, windows(t, needles_of(rec))) for w, t in texts]
    for where, t in texts:
        contacts += text_contacts(t, where)
        loose += loose_emails(t)
        if not prose:
            prose = prose_applicant(t)
    # dedupe by name / email
    seen, uniq = set(), []
    for c in contacts:
        k = (norm_name(c["name"]), tuple(sorted(e.lower() for e in c["emails"])))
        if k in seen or not norm_name(c["name"]):
            continue
        seen.add(k)
        uniq.append(c)
    have = {e.lower() for c in uniq for e in c["emails"]}
    extra = [e for e in dict.fromkeys(loose) if e.lower() not in have]
    attributed, extra = attribute_emails(extra, texts, rec, uniq)
    uniq += attributed
    extra = extra[:3]
    for c in uniq:
        c["name"] = title_if_caps(c["name"])
    note = "; ".join(errors) if errors else None
    if prose and not rec.get("applicant") and not any(c["role"].lower().startswith("applicant") for c in uniq):
        uniq.insert(0, {"role": "Applicant", "name": prose, "company": None, "phones": [], "emails": [],
                        "address": None, "where": "Agenda text"})
    # contacts with nothing but a name already shown elsewhere add nothing
    with_info = [c for c in uniq if c["phones"] or c["emails"]]
    uniq = with_info + [c for c in uniq if not (c["phones"] or c["emails"])
                        and not any(name_match(c["name"], w["name"]) for w in with_info)
                        and not (rec.get("applicant") and (name_match(c["name"], rec["applicant"])
                                                           or norm_name(c["name"]) in norm_name(rec["applicant"])))]
    return uniq[:6], app_owner, extra, note


PROSE_RX = [
    re.compile(r"petition of\s+([A-Z][\w.' \-]{2,60}?)\s*\((?:[^()]|\([^()]*\)){0,80}?Petitioner\)\s+for\s+([A-Z][\w&.,' \-]{2,60}?(?:LLC|Inc\.?|LP|LLLP|Corp\.?|Company|Trust))", re.I),
    re.compile(r"on behalf of\s+(?:the\s+)?(?:property\s+owners?,?\s+)?([A-Z][\w&.,' \-]{2,70}?(?:LLC|L\.L\.C\.|Inc\.?|LP|LLLP|Corp\.?|Company|Trust|Partners))", re.I),
    re.compile(r"\b(?:the\s+)?applicant,?\s+([A-Z][\w&./' \-]{2,60}?(?:LLC|L\.L\.C\.|Inc\.?|LP|LLLP|Corp\.?|Company|Trust|Partners))\b"),
    re.compile(r"\b([A-Z][\w&.' \-]{2,60}?(?:LLC|L\.L\.C\.|Inc\.?|LP|LLLP|Corp\.?|Company))\s*,?\s*\(?(?:the\s+)?[\u201c\"]?(?:Applicant|Owner|Developer)[\u201d\"]?\)?"),
]


def prose_applicant(t):
    t = re.sub(r"\s+", " ", t or "")
    for rx in PROSE_RX:
        m = rx.search(t)
        if m:
            v = clean(m.group(1)) if rx.groups == 1 else f"{clean(m.group(1))} for {clean(m.group(2))}"
            if v and not GOV_RX.search(v) and len(v) < 80 and not re.search(r"(?i)exhibit|title insurance|ordinance|section|resolution", v):
                return v
    return None


# ----------------------------------------------------------------- render

def esc(s):
    return html.escape(nodash(s) or "", quote=True)


def fdate(s, year=True):
    if not s:
        return ""
    try:
        d = dt.date.fromisoformat(str(s)[:10])
    except ValueError:
        return str(s)
    return d.strftime("%b %-d, %Y") if year else d.strftime("%b %-d")


def fac(a):
    if a is None:
        return "Acreage not listed"
    a = float(a)
    return f"{a:,.0f} ac" if a >= 100 else f"{a:,.2f}".rstrip("0").rstrip(".") + " ac"


def request_line(r):
    parts = []
    if r.get("from_zoning") or r.get("to_zoning"):
        parts.append(f"Zoning: {r.get('from_zoning') or '?'} → {r.get('to_zoning') or '?'}")
    if r.get("from_flu") or r.get("to_flu"):
        parts.append(f"Future land use: {r.get('from_flu') or '?'} → {r.get('to_flu') or '?'}")
    if not parts and r.get("description"):
        d = r["description"]
        parts.append(d if len(d) <= 220 else d[:217].rsplit(" ", 1)[0] + "...")
    return parts


def market_of(county):
    for k, m in MARKETS.items():
        if county in m["counties"]:
            return k
    return "tampa"


def pill(status, big=False):
    bg, fg = STATUS_STYLE.get(status, ("#eef0f3", "#344054"))
    fs = "13px" if big else "11px"
    return (f'<span style="display:inline-block;background:{bg};color:{fg};font-weight:700;font-size:{fs};'
            f'padding:3px 9px;border-radius:999px;letter-spacing:.02em;white-space:nowrap">{esc(STATUS_WORD.get(status, status)).upper()}</span>')


CHECK_STYLE = {"match": ("#e7f6ec", "#146c2e", "✔"), "agent": ("#e8f1fd", "#1f4fb5", "✔"),
               "different": ("#fff4e0", "#9a5b00", "!"), "unknown": ("#eef0f3", "#475467", "?")}


def card_html(it):
    r = it["rec"]
    recs = it.get("recs") or [r]
    status = r["status"]
    accent = STATUS_STYLE.get(status, ("#eef0f3", "#475467"))[1]
    title = r.get("project_name") or r.get("address") or r.get("app_num")
    if len(recs) == 1:
        cases = f"#{esc(r['app_num'])} &nbsp;|&nbsp; {esc(r.get('category'))}"
    else:
        cases = " + ".join(f"#{esc(x['app_num'])} ({esc(x.get('category'))})" for x in recs)
    acres = max((x.get("acres") for x in recs if x.get("acres") is not None), default=None)
    sub = " &nbsp;|&nbsp; ".join([esc(r["jurisdiction"]), cases, f"<b>{esc(fac(acres))}</b>"])
    status_html = pill(status, True)
    if it.get("from_status"):
        status_html = (f'<span style="color:#667085;font-size:12px">{esc(STATUS_WORD.get(it["from_status"], it["from_status"]))}'
                       f' &rarr; </span>' + status_html)
    rows = []

    def row(label, val_html):
        rows.append(f'<tr><td style="padding:3px 10px 3px 0;color:#667085;font-size:12px;vertical-align:top;'
                    f'white-space:nowrap;width:92px">{label}</td><td style="padding:3px 0;font-size:13px;'
                    f'color:#101828;vertical-align:top;word-break:break-word">{val_html}</td></tr>')
    reqs = []
    for x in recs:
        for line in request_line(x):
            if line not in reqs:
                reqs.append(line)
    for line in reqs:
        row("Request", esc(line))
    loc = esc(r.get("address")) if r.get("address") else ""
    if r.get("lat") is not None:
        g = f"https://www.google.com/maps?q={r['lat']},{r['lon']}"
        loc = (loc + " &nbsp;" if loc else "") + f'<a href="{g}" style="color:#1f4fb5;font-size:12px">Google Maps</a>'
    if loc:
        row("Location", loc)
    pids = list(dict.fromkeys(p for x in recs for p in (x.get("apns") or x.get("parcel_ids") or [])))
    if pids:
        row("Parcel" + ("s" if len(pids) > 1 else ""), esc(", ".join(pids[:6]) + (f" +{len(pids) - 6} more" if len(pids) > 6 else "")))
    dates = []
    if r.get("filed_date"):
        dates.append(f"Filed {fdate(r['filed_date'])}")
    if status in ("Approved", "Denied") and r.get("decision_date"):
        dates.append(f"Decided {fdate(r['decision_date'])}")
    nh = r.get("next_hearing")
    if isinstance(nh, dict) and nh.get("date") and status not in ("Approved", "Denied", "Withdrawn"):
        dates.append(f"<b>Next hearing {fdate(nh['date'])}</b> ({esc(nh.get('body'))})")
    if dates:
        row("Dates", " &nbsp;|&nbsp; ".join(dates))

    # people box
    code, head, detail = it["check"]
    cbg, cfg, ico = CHECK_STYLE[code]
    owners = it["owners"]
    if owners:
        olines = []
        for o in owners[:4]:
            nm = esc(o["owner"])
            if o.get("pa_url"):
                nm = f'<a href="{esc(o["pa_url"])}" style="color:#101828;text-decoration:underline">{nm}</a>'
            olines.append(nm + (' <span style="color:#98a2b3;font-size:11px">(parcel at mapped location)</span>'
                                if o["how"] == "point" else ""))
        if len(owners) > 4:
            olines.append(f'<span style="color:#667085">+{len(owners) - 4} more owners</span>')
        owner_html = "<br>".join(olines)
    elif it.get("app_owner"):
        owner_html = esc(it["app_owner"]) + ' <span style="color:#98a2b3;font-size:11px">(as listed on application)</span>'
    else:
        owner_html = '<span style="color:#98a2b3">Not found</span>'
    clines = []
    for c in it["contacts"]:
        if not (c["phones"] or c["emails"]) and r.get("applicant") and norm_name(c["name"]) == norm_name(r["applicant"]):
            continue
        bits = [f"<b>{esc(c['name'])}</b>"]
        if c.get("company"):
            bits.append(esc(c["company"]))
        line = f'<span style="color:#667085">{esc(c["role"])}:</span> ' + ", ".join(bits)
        sub2 = []
        for p in c["phones"]:
            sub2.append(f'<a href="tel:{re.sub(r"[^0-9]", "", p)}" style="color:#1f4fb5;text-decoration:none">{esc(p)}</a>')
        for e in c["emails"]:
            sub2.append(f'<a href="mailto:{esc(e)}" style="color:#1f4fb5;text-decoration:none">{esc(e)}</a>')
        if c.get("address"):
            sub2.append(f'<span style="color:#667085">{esc(c["address"])}</span>')
        if sub2:
            line += "<br>" + " &nbsp;&middot;&nbsp; ".join(sub2)
        clines.append(f'<div style="margin:0 0 6px">{line}</div>')
    for e in it.get("extra_emails") or []:
        clines.append(f'<div style="margin:0 0 6px"><span style="color:#667085">Email in documents:</span> '
                      f'<a href="mailto:{esc(e)}" style="color:#1f4fb5;text-decoration:none">{esc(e)}</a></div>')
    contacts_html = "".join(clines) or '<span style="color:#98a2b3">No contact info posted online</span>'
    applicant_html = esc(r.get("applicant")) if r.get("applicant") else '<span style="color:#98a2b3">Not listed</span>'

    people = f'''
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f8fafc;border:1px solid #e4e7ec;border-radius:8px;margin-top:10px">
<tr><td style="padding:10px 12px">
 <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
  <tr><td style="padding:2px 10px 4px 0;color:#667085;font-size:12px;width:92px;vertical-align:top">Applicant</td><td style="padding:2px 0 4px;font-size:13px;font-weight:700;color:#101828">{applicant_html}</td></tr>
  <tr><td style="padding:2px 10px 4px 0;color:#667085;font-size:12px;vertical-align:top">Owner of record</td><td style="padding:2px 0 4px;font-size:13px;color:#101828">{owner_html}</td></tr>
  <tr><td style="padding:4px 10px 6px 0;color:#667085;font-size:12px;vertical-align:top">Ownership check</td><td style="padding:4px 0 6px">
   <span style="display:inline-block;background:{cbg};color:{cfg};font-weight:700;font-size:12px;padding:3px 9px;border-radius:6px">{ico} {esc(head)}</span>
   <div style="color:#475467;font-size:12px;margin-top:3px">{esc(detail)}</div></td></tr>
  <tr><td style="padding:4px 10px 2px 0;color:#667085;font-size:12px;vertical-align:top;border-top:1px solid #e4e7ec">Contacts</td><td style="padding:6px 0 0;font-size:13px;color:#101828;border-top:1px solid #e4e7ec">{contacts_html}</td></tr>
 </table>
</td></tr></table>'''
    btn = ""
    for x in recs:
        lab = "View application" if len(recs) == 1 else f"View {x['app_num']}"
        btn += (f'<a href="{esc(x.get("url"))}" style="display:inline-block;background:#101828;color:#ffffff;text-decoration:none;'
                f'font-size:12px;font-weight:700;padding:7px 12px;border-radius:6px;margin:0 6px 6px 0">{esc(lab)}</a>')
    if r.get("docs_url") and "sharepoint" not in (r.get("docs_url") or ""):
        btn += (f'<a href="{esc(r["docs_url"])}" style="display:inline-block;background:#ffffff;color:#101828;border:1px solid #d0d5dd;'
                f'text-decoration:none;font-size:12px;font-weight:700;padding:6px 12px;border-radius:6px;margin:0 6px 6px 0">Staff report / agenda</a>')
    return f'''
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#ffffff;border:1px solid #e4e7ec;border-left:5px solid {accent};border-radius:10px;margin:0 0 14px">
<tr><td style="padding:14px 16px">
 <div style="margin:0 0 6px">{status_html}</div>
 <div style="font-size:17px;font-weight:800;color:#101828;line-height:1.3">{esc(title)}</div>
 <div style="font-size:12px;color:#475467;margin:3px 0 8px">{sub}</div>
 <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{"".join(rows)}</table>
 {people}
 <div style="margin-top:12px">{btn}</div>
</td></tr></table>'''


def section_html(title, subtitle, color, items):
    if not items:
        return ""
    body = "".join(card_html(i) for i in items)
    n_cases = sum(len(i.get("recs") or [1]) for i in items)
    return f'''
<tr><td style="padding:22px 0 8px">
 <div style="font-size:19px;font-weight:800;color:#101828"><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:{color};margin-right:8px"></span>{esc(title)} <span style="color:#98a2b3;font-weight:700">({n_cases} case{"s" if n_cases != 1 else ""})</span></div>
 <div style="font-size:12px;color:#667085;margin-top:2px">{esc(subtitle)}</div>
</td></tr>
<tr><td>{body}</td></tr>'''


def tile(n, label, bg, fg):
    return (f'<td align="center" style="padding:4px"><div style="background:{bg};border-radius:10px;padding:10px 2px">'
            f'<div style="font-size:24px;font-weight:800;color:{fg};line-height:1">{n}</div>'
            f'<div style="font-size:11px;font-weight:700;color:{fg};margin-top:4px;text-transform:uppercase;letter-spacing:.03em">{label}</div></div></td>')


def build(items, run_date, market_label, map_url):
    groups = {k: [i for i in items if i["kind"] == "change" and i["rec"]["status"] == k]
              for k in ("Approved", "Denied", "Withdrawn")}
    other = [i for i in items if i["kind"] == "change" and i["rec"]["status"] not in ("Approved", "Denied", "Withdrawn")]
    new = [i for i in items if i["kind"] == "new"]
    for lst in list(groups.values()) + [other, new]:
        lst.sort(key=lambda i: (-(i["rec"].get("acres") or 0)))
    d = dt.date.fromisoformat(run_date)
    week = d.strftime("%b %-d, %Y")
    def nc(lst):
        return sum(len(i.get("recs") or [1]) for i in lst)
    tiles = "".join([
        tile(nc(groups["Approved"]), "Approved", *STATUS_STYLE["Approved"]),
        tile(nc(groups["Denied"]), "Denied", *STATUS_STYLE["Denied"]),
        tile(nc(groups["Withdrawn"]), "Withdrawn", *STATUS_STYLE["Withdrawn"]),
        tile(nc(other), "Hearing updates", *STATUS_STYLE["Hearing Scheduled"]),
        tile(nc(new), "New filings", *STATUS_STYLE["Filed"]),
    ])
    secs = "".join([
        section_html("Approved", "Final approval by the deciding board this week.", STATUS_STYLE["Approved"][1], groups["Approved"]),
        section_html("Denied", "Denied by the deciding board this week.", STATUS_STYLE["Denied"][1], groups["Denied"]),
        section_html("Withdrawn", "Withdrawn, voided or closed by the applicant or county.", STATUS_STYLE["Withdrawn"][1], groups["Withdrawn"]),
        section_html("Hearing updates", "Hearings newly scheduled or continued.", STATUS_STYLE["Hearing Scheduled"][1], other),
        section_html("New filings", "Applications that appeared for the first time this week.", STATUS_STYLE["Filed"][1], new),
    ])
    if not items:
        secs = ('<tr><td style="padding:30px 0;text-align:center;color:#667085;font-size:14px">'
                'No decisions, status changes or new filings this week.</td></tr>')
    html_out = f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f2f4f7">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f2f4f7"><tr><td align="center" style="padding:20px 10px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:680px;font-family:Arial,Helvetica,sans-serif">
<tr><td style="background:#101828;border-radius:12px 12px 0 0;padding:18px 20px;border-bottom:4px solid #ef1f25">
 <div style="font-size:12px;font-weight:700;color:#fda29b;letter-spacing:.08em;text-transform:uppercase">Eshenbaugh Land Company</div>
 <div style="font-size:22px;font-weight:800;color:#ffffff;margin-top:4px">Rezoning Update</div>
 <div style="font-size:13px;color:#d0d5dd;margin-top:2px">Week of {week} &nbsp;|&nbsp; {esc(market_label)}</div>
</td></tr>
<tr><td style="background:#ffffff;padding:10px 12px;border-radius:0 0 12px 12px">
 <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>{tiles}</tr></table>
 <div style="font-size:12px;color:#667085;text-align:center;padding:6px 0 2px">Only this week's decisions, status changes and new filings (0.5 ac and up). <a href="{esc(map_url)}" style="color:#1f4fb5">Open the live map</a></div>
</td></tr>
<tr><td><table role="presentation" width="100%" cellpadding="0" cellspacing="0">{secs}</table></td></tr>
<tr><td style="padding:16px 4px 4px;font-size:11px;color:#98a2b3;line-height:1.5">
 Owner of record comes from the county property appraiser parcel data; applicant and contacts come from the public application, agenda and staff report postings. "Applicant is NOT the owner" usually means an agent, engineer or contract buyer filed the case. Contact details are only what the government posted publicly. Verify before relying on them.
</td></tr>
</table></td></tr></table></body></html>'''
    return compact(nodash(html_out))


def compact(doc):
    """Repeated inline styles -> classes in one <style> block (Gmail supports class selectors in head)."""
    styles = re.findall(r' style="([^"]*)"', doc)
    counts = {}
    for st in styles:
        counts[st] = counts.get(st, 0) + 1
    names, css = {}, []
    for st, n in sorted(counts.items(), key=lambda x: -x[1]):
        if n >= 2 and len(st) > 12:
            nm = f"c{len(names)}"
            names[st] = nm
            css.append(f".{nm}{{{st}}}")
    doc = re.sub(r' style="([^"]*)"', lambda m: f' class="{names[m.group(1)]}"' if m.group(1) in names else m.group(0), doc)
    doc = re.sub(r"\n\s*", "\n", doc)
    return doc.replace("</head>", "<style>" + "".join(css) + "</style></head>", 1)


def build_text(items, run_date, market_label, map_url):
    L = [f"ELC REZONING UPDATE, WEEK OF {dt.date.fromisoformat(run_date).strftime('%b %-d, %Y').upper()}",
         market_label, f"Live map: {map_url}", ""]
    order = [("APPROVED", lambda i: i["kind"] == "change" and i["rec"]["status"] == "Approved"),
             ("DENIED", lambda i: i["kind"] == "change" and i["rec"]["status"] == "Denied"),
             ("WITHDRAWN", lambda i: i["kind"] == "change" and i["rec"]["status"] == "Withdrawn"),
             ("HEARING UPDATES", lambda i: i["kind"] == "change" and i["rec"]["status"] not in ("Approved", "Denied", "Withdrawn")),
             ("NEW FILINGS", lambda i: i["kind"] == "new")]
    for title, f in order:
        sel = [i for i in items if f(i)]
        L.append(f"{title} ({len(sel)})")
        L.append("-" * 40)
        for it in sel:
            r = it["rec"]
            nums = " + #".join(x["app_num"] for x in it.get("recs") or [r])
            L.append(f"* {r.get('project_name') or r.get('app_num')}  [{r['jurisdiction']} #{nums}]  {fac(r.get('acres'))}")
            for line in request_line(r):
                L.append(f"  {line}")
            if r.get("address"):
                L.append(f"  Location: {r['address']}")
            L.append(f"  Applicant: {r.get('applicant') or 'Not listed'}")
            L.append("  Owner of record: " + (", ".join(o["owner"] for o in it["owners"][:4]) or it.get("app_owner") or "Not found"))
            L.append(f"  Ownership check: {it['check'][1]}. {it['check'][2]}")
            for c in it["contacts"]:
                if not (c["phones"] or c["emails"]) and r.get("applicant") and norm_name(c["name"]) == norm_name(r["applicant"]):
                    continue
                L.append(f"  {c['role']}: {c['name']}" + (f", {c['company']}" if c.get("company") else "")
                         + "".join(f" | {p}" for p in c["phones"]) + "".join(f" | {e}" for e in c["emails"]))
            L.append(f"  Link: {r.get('url')}")
            L.append("")
        L.append("")
    return nodash("\n".join(L))


def group_companions(items):
    """Companion cases (e.g. a comp plan amendment + rezoning for the same project) -> one card."""
    out, by = [], {}
    for it in items:
        r = it["rec"]
        pn = norm_name(r.get("project_name"))
        if not pn or len(pn) < 6:
            out.append(dict(it, recs=[r]))
            continue
        k = (it["kind"], r["status"], r["jurisdiction"], pn)
        if k in by:
            g = by[k]
            g["recs"].append(r)
            seen = {norm_name(o["owner"]) for o in g["owners"]}
            g["owners"] += [o for o in it["owners"] if norm_name(o["owner"]) not in seen]
            seen = {(norm_name(c["name"]), tuple(c["emails"])) for c in g["contacts"]}
            g["contacts"] += [c for c in it["contacts"] if (norm_name(c["name"]), tuple(c["emails"])) not in seen]
            if g["check"][0] == "unknown" and it["check"][0] != "unknown":
                g["check"] = it["check"]
            continue
        g = dict(it, recs=[r])
        by[k] = g
        out.append(g)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--run-date", required=True)
    ap.add_argument("--market", default="all", choices=["all"] + list(MARKETS))
    ap.add_argument("--map-url", default=MAP_URL)
    ap.add_argument("--budget", type=int, default=900, help="seconds for owner + contact lookups")
    a = ap.parse_args(argv)
    db = json.load(open(os.path.join(a.out_dir, "rezonings.json")))
    recs = {r["id"]: r for r in db["records"]}
    dg = json.load(open(os.path.join(a.out_dir, f"rezoning-digest-{a.run_date}.json")))
    items = []
    for county, lst in (dg.get("new_filings") or {}).items():
        for x in lst:
            if x["id"] in recs:
                items.append({"kind": "new", "rec": recs[x["id"]]})
    for x in dg.get("status_changes") or []:
        if x["id"] in recs:
            items.append({"kind": "change", "rec": recs[x["id"]], "from_status": x.get("from_status")})
    if a.market != "all":
        items = [i for i in items if market_of(i["rec"].get("county")) == a.market]
    os.environ.setdefault("RZ_PARCEL_CACHE", os.path.join(a.out_dir, "owner_cache.json"))
    budget = time.time() + a.budget
    for it in items:
        r = it["rec"]
        it["owners"], it["n_parcels"] = owners_for(r, budget)
        it["contacts"], it["app_owner"], it["extra_emails"], it["note"] = ([], None, [], "time budget used") \
            if time.time() > budget else contacts_for(r)
        if not r.get("applicant"):
            ap_c = next((c for c in it["contacts"] if c["role"].lower().startswith("applicant")), None)
            if ap_c:
                r = dict(r, applicant=ap_c["name"] + (f" ({ap_c['company']})" if ap_c.get("company") else ""))
                it["rec"] = r
        it["check"] = ownership_check(r.get("applicant"), it["owners"], it.get("app_owner"), r.get("project_name"))
        log(f"{r['id']}: owners={len(it['owners'])} contacts={len(it['contacts'])} check={it['check'][0]}")
    items = group_companions(items)
    label = "All markets" if a.market == "all" else MARKETS[a.market]["label"]
    suffix = "" if a.market == "all" else f"-{a.market}"
    html_out = build(items, a.run_date, label, a.map_url)
    txt_out = build_text(items, a.run_date, label, a.map_url)
    n_dec = sum(len(i["recs"]) for i in items if i["kind"] == "change" and i["rec"]["status"] in ("Approved", "Denied", "Withdrawn"))
    n_new = sum(len(i["recs"]) for i in items if i["kind"] == "new")
    n_oth = sum(len(i["recs"]) for i in items if i["kind"] == "change") - n_dec
    week = dt.date.fromisoformat(a.run_date).strftime("%b %-d, %Y")
    subject = f"Rezoning Update, week of {week}: {n_dec} decisions, {n_new} new filings, {n_oth} hearing updates"
    base = os.path.join(a.out_dir, f"team-email-{a.run_date}{suffix}")
    open(base + ".html", "w", encoding="utf-8").write(html_out)
    open(base + ".txt", "w", encoding="utf-8").write(txt_out)
    json.dump({"subject": nodash(subject), "market": a.market, "counts": {"decisions": n_dec, "new": n_new, "hearing_updates": n_oth},
               "items": [{"id": i["rec"]["id"], "cases": [x["id"] for x in i["recs"]], "kind": i["kind"], "status": i["rec"]["status"], "applicant": i["rec"].get("applicant"),
                          "owners": i["owners"], "check": i["check"], "contacts": i["contacts"], "extra_emails": i["extra_emails"],
                          "note": i.get("note")} for i in items]},
              open(base + ".json", "w"), indent=1)
    print(f"done: {base}.html ({len(html_out):,} bytes) subject={subject!r}")


if __name__ == "__main__":
    main()
