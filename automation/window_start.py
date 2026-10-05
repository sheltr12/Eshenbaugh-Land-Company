#!/usr/bin/env python3
"""Print the window start date: latest <county> sale_date_iso in the live comps.json
minus OVERLAP_DAYS (default 21), never earlier than FLOOR_DAYS (default 120) ago.
Dedup makes the overlap harmless; the overlap covers the PA's 2–6 week posting lag."""
import json, sys, os, urllib.request
from datetime import date, timedelta
county = sys.argv[1]
live = os.environ.get('LIVE_COMPS', 'https://sheltr12.github.io/Eshenbaugh-Land-Company/comps.json')
overlap = int(os.environ.get('OVERLAP_DAYS', '21')); floor = int(os.environ.get('FLOOR_DAYS', '120'))
d = json.loads(urllib.request.urlopen(live, timeout=60).read())
dates = [c.get('sale_date_iso') for c in d['comps'] if str(c.get('county','')).lower() == county.lower() and c.get('sale_date_iso')]
latest = date.fromisoformat(max(dates)) if dates else date.today() - timedelta(days=floor)
start = max(latest - timedelta(days=overlap), date.today() - timedelta(days=floor))
print(start.isoformat())
