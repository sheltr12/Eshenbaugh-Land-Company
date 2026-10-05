# ELC weekly comps automation

Runs entirely on GitHub Actions (`.github/workflows/pasco-comps.yml`) — no laptop,
no Claude session, no personal access token. Mondays 11:00 UTC, or on demand from
the Actions tab ("Run workflow", optional window-start override).

What a run does:
1. `window_start.py` — latest Pasco sale date already in the live `comps.json`, minus 21 days.
2. Downloads Pasco PA bulk export (`sales.zip`, `parcel.zip`) from ftp01.pascopa.com.
3. `pasco_pipeline.py` — groups all sales by OR book/page (one deed = one comp), keeps deeds with a
   vacant-at-sale parcel priced > $500k, sums acreage across every parcel on the deed (> 0.5 ac),
   dedups against the live DB, then enriches from the PA GIS REST layer (exact centroid, address,
   current owner) and `parcel.aspx` (DOR class, zoning, previous owner = grantor, deed instrument).
4. `make_map.py` — self-contained Leaflet/Esri map + table (`pasco-comps-map-YYYY-MM-DD.html`).
5. `push_branch.py` — merges into `comps.json` on top of the latest `Test-patch-N`, creates
   `Test-patch-N+1` (never branches from `Test`/`main`), adds the map and the pending JSON.
6. Deploys that branch to GitHub Pages and verifies the live `comps.json` count.

Zero new comps => nothing is pushed and Pages is left alone.
Failures email the repo owner automatically (GitHub's default for scheduled workflows).
