# ELC Rezoning Tracker: adapter spec

Goal: every Tuesday, collect rezoning / PD / comp plan amendment applications from Florida
jurisdictions, put each on a map, and follow each application from filing through hearing to
approval or denial. Output layer: `rezonings.json` on the ELC GitHub Pages site.

## What to collect (scope)
Keep only these categories (field `category`):
- `Rezoning`  : standard / conventional rezonings (e.g. Hillsborough RZ-STD, "Rezoning", "Zoning Map Amendment").
- `PD`        : planned development rezonings AND PD amendments / major modifications
                (PD, PUD, MPUD, RPUD, CPD, PDD, MM "major modification", "PD amendment", DRI-to-PD).
- `Comp Plan Amendment` : future land use MAP amendments (small-scale and large-scale CPA / FLUMA /
                LSCPA / SSCPA). Skip text-only amendments that have no parcels.
Skip: variances, special use / special exception / conditional use, plats, site plans, CDDs,
vacations, minor modifications / administrative PD changes, appeals.
Size filter (applied later by the pipeline, NOT by adapters): site >= 0.5 acres. Adapters should
still return `acres` when the source gives it.

## Adapter interface
Each adapter is `adapters/<key>.py` exposing:

```python
JURISDICTIONS = ["Pasco County"]          # display names this module covers
def fetch(since: str, refresh: list[str]) -> list[dict]:
    """since   = 'YYYY-MM-DD'. Return every in-scope application FILED (or first appearing)
                 on/after this date.
       refresh = app_num values (for this adapter's jurisdictions) that are still open in our
                 database; return a current record for each of these too so status/hearings update.
       Must not raise on a single bad record: log to stderr and skip. Raise only if the whole
       source is unreachable (pipeline will report that adapter as failed)."""
```
Use only: python3 stdlib, `requests`, `beautifulsoup4`, `lxml` (pipeline pip-installs them).
No browser automation. Be polite: sequential requests, ~0.3-1 s sleep, real User-Agent,
timeouts, retries x3 with backoff. Runtime target: under ~10 minutes per adapter for a
weekly run (since = 14 days ago plus a few dozen refresh ids). A first backfill run with
since = ~180 days ago may take longer.

## Record schema (one dict per application)
```
jurisdiction   str   "Pasco County" | "City of Tampa" | ... (display name)
county         str   "Pasco" (county the site is in; City of Tampa -> "Hillsborough")
app_num        str   application / case number exactly as the source shows it (unique per jurisdiction)
category       str   "Rezoning" | "PD" | "Comp Plan Amendment"
app_type       str   raw type label from the source ("RZ-PD", "Planned Development Rezoning", ...)
project_name   str|None
description    str|None  short request summary, e.g. "Rezone 42.1 ac from AG to PD for 120 SF lots"
applicant      str|None  applicant / owner / agent (prefer applicant; include agent if that's all there is)
parcel_ids     list[str] parcel/folio IDs in the county property-appraiser format (may be empty)
address        str|None
acres          float|None
from_zoning    str|None   to_zoning   str|None
from_flu       str|None   to_flu      str|None   (future land use, for CPAs)
filed_date     str|None  'YYYY-MM-DD' (application/submittal date; if unknown, earliest known date)
status         str   normalized, one of:
                     "Filed" (submitted / accepted / pending / under review, no hearing set yet)
                     "Hearing Scheduled" (a future hearing date is known)
                     "Continued" (hearing continued / deferred)
                     "Approved" (final approval by the deciding body, incl. approved w/ conditions)
                     "Denied"
                     "Withdrawn" (withdrawn / void / closed-incomplete / expired)
status_raw     str|None  status text exactly as the source shows it
hearings       list[{"body": str, "date": "YYYY-MM-DD", "result": str|None}]
                     body like "Planning Commission", "Zoning Hearing Master", "BOCC", "City Council",
                     "LUHO", "DRC"; result like "Recommended Approval", "Approved", "Continued" or None
decision_date  str|None  date of final decision (Approved/Denied)
url            str   link a broker can click to see the application (record detail page or agenda item)
docs_url       str|None  link to staff report / application packet if easy to get
lat, lon       float|None  WGS84; fill only if the source gives exact geometry, else None
                     (the parcel lookup module fills them from parcel_ids)
source         str   e.g. "Pasco Accela", "Hillsborough Zoning Hearings GIS + Legistar"
```
Unknown values -> None (never empty strings). Dates always ISO.

## Testing
Each adapter file must run standalone: `python3 adapters/<key>.py 2026-04-01` prints a JSON
list to stdout (and a summary to stderr). Save a real sample to `samples/<key>.json`.
