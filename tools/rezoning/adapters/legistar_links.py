"""Working public-site links for Legistar matters.

The public Legistar sites (<client>.legistar.com) do not use the web API's MatterId / MatterGuid:
a LegislationDetail.aspx?ID=<MatterId>&GUID=<MatterGuid> link built from the API answers
"Invalid parameters!". Meeting pages do accept the API EventId (the API's EventInSiteURL), and each
lists its agenda items by File # with the item's real site link. So a matter's working link is read
from the meeting page of an event it was (or will be) heard at:
  1. events from the matter's histories (MatterHistoryEventId), newest first,
  2. events held on the matter's MatterAgendaDate,
and the item whose link text equals the matter's MatterFile is used.
If the matter is on an event but its row cannot be matched, the meeting page itself is returned.
Only stdlib + requests + bs4.
"""
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (compatible; ELC-RezoningTracker/1.0; +https://thedirtdog.com)"
_s = requests.Session()
_s.headers.update({"User-Agent": UA})
_EVENTS = {}      # (api, event id) -> (links {FILE: url}, meeting url)
_ON_DATE = {}     # (api, date) -> [event ids]
_HIST = {}        # (api, matter id) -> histories


def log(*a):
    print("[legistar_links]", *a, file=sys.stderr)


def _get(url, params=None, as_json=True, tries=3):
    last = None
    for i in range(tries):
        try:
            r = _s.get(url, params=params, timeout=60,
                       headers={"Accept": "application/json" if as_json else "text/html"})
            r.raise_for_status()
            time.sleep(0.3)
            return r.json() if as_json else r.text
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


def norm_file(s):
    return re.sub(r"\s+", " ", str(s or "")).strip().upper()


def _site_root(meeting_url):
    m = re.match(r"(https?://[^/]+)", meeting_url or "")
    return m.group(1) if m else ""


def event_links(api, eid):
    key = (api, eid)
    if key in _EVENTS:
        return _EVENTS[key]
    links, murl = {}, None
    try:
        ev = _get(f"{api}/events/{eid}")
        murl = ev.get("EventInSiteURL")
        if murl:
            root = _site_root(murl)
            soup = BeautifulSoup(_get(murl, as_json=False), "lxml")
            for a in soup.select('a[href*="LegislationDetail.aspx"]'):
                f = norm_file(a.get_text())
                href = a["href"].replace("&amp;", "&").replace("&Options=&Search=", "")
                if f:
                    links.setdefault(f, href if href.startswith("http") else f"{root}/{href.lstrip('/')}")
    except Exception as e:  # noqa: BLE001
        log(f"event {eid}: {e}")
    _EVENTS[key] = (links, murl)
    return _EVENTS[key]


def _events_on(api, date_iso):
    key = (api, date_iso)
    if key not in _ON_DATE:
        try:
            _ON_DATE[key] = [e["EventId"] for e in
                             _get(f"{api}/events", {"$filter": f"EventDate eq datetime'{date_iso}'"})]
        except Exception as e:  # noqa: BLE001
            log(f"events on {date_iso}: {e}")
            _ON_DATE[key] = []
    return _ON_DATE[key]


def _histories(api, mid):
    key = (api, mid)
    if key not in _HIST:
        try:
            _HIST[key] = _get(f"{api}/matters/{mid}/histories")
        except Exception as e:  # noqa: BLE001
            log(f"histories {mid}: {e}")
            _HIST[key] = []
    return _HIST[key]


def site_link(api, matters, histories=None, meeting_fallback=True):
    """api = 'https://webapi.legistar.com/v1/<client>'; matters = API matter dicts (main first).
    histories: already-fetched histories for these matters (fetched here when None).
    Returns a working LegislationDetail link, else (meeting_fallback) the meeting page, else None."""
    matters = [m for m in (matters or []) if isinstance(m, dict) and m.get("MatterId")]
    if not matters:
        return None
    files = [f for f in dict.fromkeys(norm_file(m.get("MatterFile")) for m in matters) if f]
    if histories is None:
        histories = [h for m in matters for h in _histories(api, m["MatterId"])]
    eids = [h.get("MatterHistoryEventId") for h in
            sorted(histories, key=lambda h: h.get("MatterHistoryActionDate") or "", reverse=True)
            if h.get("MatterHistoryEventId")]
    for m in matters:
        d = (m.get("MatterAgendaDate") or "")[:10]
        if d:
            eids.extend(_events_on(api, d))
    eids = list(dict.fromkeys(eids))
    first_meeting = None
    for eid in eids:
        links, murl = event_links(api, eid)
        for f in files:
            if f in links:
                return links[f]
        if murl and first_meeting is None and links:
            first_meeting = murl
    return first_meeting if meeting_fallback else None
