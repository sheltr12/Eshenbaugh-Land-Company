#!/usr/bin/env python3
"""
polk_pipeline.py - Polk County vacant-land comps for the ELC comps database (no browser needed).

  python3 polk_pipeline.py --out-dir DIR [--run-date YYYY-MM-DD] [--start YYYY-MM-DD] [--lookback-days 60]

Writes DIR/polk-<date>.json (new comps in the ELC schema, already deduped against the newest
Test-patch-N comps.json) and DIR/polk-comps-map-<date>.html (Esri imagery map + list).
Prints a summary. Push afterwards with push_comps_locked.py (concurrency-safe).

Window (catch-up, self-healing):
  The Polk PA SALES layer has no recorded/posted date, only SALEDT (closing date), and PA
  data lags several weeks. So every run scans from (latest Polk PA sale date already in the
  database - lookback-days) through the run date. Missed weeks are filled automatically;
  dedup keeps anything already in the database from repeating.

Sources (all plain HTTPS through the county's own map proxy, https://www.polkflpa.gov/proxy.ashx):
  WebSiteQueryNew/MapServer/3  SALES     PARCEL_ID, SALEDT, PRICE, BOOK, PAGE, SALETYPE (V/I), TRNS_DSCR,
                                         INSTRTYP_DSCR (qualification), GRANTOR, GRANTEE, shape area (sq ft)
  WebSiteQueryNew/MapServer/4  WEBEXPORT SITE_ADDR_1/2, DOR_CD, DOR_DSCR
  WebSiteQueryNew/MapServer/0  PARCELS   polygon geometry -> exact area-weighted centroid (WGS84)
  NOTE: the county firewall (WebKnight) blocks SQL "IN (...)" - use OR'd equality clauses, small batches.
  NOTE: CamaDisplay.aspx (parcel detail page) blocks scripted requests -> zoning is left null.

Rules (from the polk-county-comps task):
  * Group ALL sales rows (every property class) into deeds by exact OR Book/Page.
  * A deed qualifies if >= 1 parcel was vacant at sale (SALETYPE 'V') and price > $500,000.
  * Every parcel on a qualifying deed is re-queried (no price/use filter) and its acreage summed;
    the > 0.5 ac test applies to the SUMMED acreage.
  * Primary parcel = largest vacant parcel with a street-numbered address, else largest overall.
  * Deeds where improved-at-sale parcels make up more than half the summed acreage are excluded
    ("mostly improved", same rule as osceola_pipeline.py). Other deeds that also carry improved
    parcels are kept but flagged "MIXED SALE" in comments.
  * Split-parcel artifact: if every vacant parcel on a deed with improved parcels also sits on
    another deed recorded the same day, the deed is an improved sale -> excluded.
  * polk_skiplist.json (next to this script) lists Book/Pages to never add, with reasons.
"""
import argparse, collections, datetime, json, os, re, subprocess, sys, time, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = 'sheltr12/Eshenbaugh-Land-Company'
GIS = 'https://www.polkflpa.gov/proxy.ashx?https://gissrvr/arcgis/rest/services/WebSite/WebSiteQueryNew/MapServer'
PA_PAGE = 'https://www.polkflpa.gov/CamaDisplay.aspx?OutputMode=Display&SearchType=RealEstate&ParcelID={}'
UA = {'User-Agent': 'elc-comps-bot'}


def load_token():
    if os.environ.get('GITHUB_TOKEN'):
        return os.environ['GITHUB_TOKEN'].strip()
    for p in [os.environ.get('GITHUB_TOKEN_FILE'), os.path.expanduser('~/Documents/Claude/.github_token'),
              os.path.join(HERE, '..', '.github_token')]:
        if p and os.path.exists(p):
            return open(p).read().strip()
    return None


def http_json(url, headers=None, tries=5):
    last = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers or UA), timeout=180) as r:
                return json.loads(r.read())
        except Exception as e:
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f'GET failed after {tries} tries: {url[:160]} ({last})')


def gis(layer, **params):
    d = http_json(f'{GIS}/{layer}/query?' + urllib.parse.urlencode(params))
    if 'error' in d:
        raise RuntimeError(f'GIS error layer {layer}: {d["error"]}')
    return d


def gis_or(layer, field, values, out_fields, geometry=False, batch=8):
    """Query by OR'd equality clauses (the county firewall blocks IN (...))."""
    feats = []
    values = list(values)
    for i in range(0, len(values), batch):
        where = ' OR '.join(f"{field}='{v}'" for v in values[i:i + batch])
        p = dict(f='json', where=where, outFields=out_fields, returnGeometry='true' if geometry else 'false')
        if geometry:
            p['outSR'] = '4326'
        feats += gis(layer, **p)['features']
    return feats


def newest_patch_comps(tk):
    hdr = {'Authorization': f'token {tk}', 'User-Agent': 'elc-comps-bot'} if tk else UA
    ns, page = {}, 1
    while True:
        b = http_json(f'https://api.github.com/repos/{REPO}/branches?per_page=100&page={page}', hdr)
        for x in b:
            m = re.fullmatch(r'Test-patch-(\d+)', x['name'])
            if m:
                ns[int(m.group(1))] = x['commit']['sha']
        if len(b) < 100:
            break
        page += 1
    n = max(ns)
    d = http_json(f'https://raw.githubusercontent.com/{REPO}/{ns[n]}/comps.json', hdr)
    return n, (d['comps'] if isinstance(d, dict) else d)


npid = lambda p: re.sub(r'[\s\-]', '', str(p or '')).upper()
dash = lambda p: f'{p[0:2]}-{p[2:4]}-{p[4:6]}-{p[6:12]}-{p[12:18]}'
sq = lambda s: re.sub(r'\s+', ' ', str(s or '')).strip()


def nbp(s):
    m = re.search(r'(\d{3,6})\s*/\s*(\d{1,6})', str(s or ''))
    return f'{int(m.group(1))}/{int(m.group(2))}' if m else None


def fmt_addr(we, pid):
    a1, a2 = sq(we.get('SITE_ADDR_1')), sq(we.get('SITE_ADDR_2'))
    m = re.match(r'^(.*?)\s+FL\s+(\d{5})', a2)
    city = f'{m.group(1)}, FL {m.group(2)}' if m else a2
    a1 = re.sub(r'^0+\s+', '', a1)
    if a1:
        return f'{a1}, {city}' if city else a1
    return f'Parcel {dash(pid)} (no site address)' + (f', {city}' if city else '')


def centroid(rings):
    A = cx = cy = 0.0
    for ring in rings:
        for (x0, y0), (x1, y1) in zip(ring, ring[1:]):
            c = x0 * y1 - x1 * y0
            A += c; cx += (x0 + x1) * c; cy += (y0 + y1) * c
    if abs(A) > 1e-14:
        return round(cy / (3 * A), 7), round(cx / (3 * A), 7)
    pts = [p for r in rings for p in r]
    return round(sum(p[1] for p in pts) / len(pts), 7), round(sum(p[0] for p in pts) / len(pts), 7)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out-dir', required=True)
    ap.add_argument('--run-date', default=datetime.date.today().isoformat())
    ap.add_argument('--start', help='override window start (sale date, YYYY-MM-DD)')
    ap.add_argument('--lookback-days', type=int, default=60)
    a = ap.parse_args()
    run = a.run_date
    os.makedirs(a.out_dir, exist_ok=True)

    # ---- database (newest Test-patch-N) ----
    n, db = newest_patch_comps(load_token())
    polk = [c for c in db if c.get('county') == 'Polk']
    pa_dates = [c['sale_date_iso'] for c in polk if c.get('source') == 'Polk County PA' and c.get('sale_date_iso')]
    latest = max(pa_dates) if pa_dates else '2026-06-01'
    start = a.start or (datetime.date.fromisoformat(latest) - datetime.timedelta(days=a.lookback_days)).isoformat()
    print(f'Database: Test-patch-{n}, {len(db)} comps, {len(polk)} Polk; latest Polk PA sale {latest}')
    print(f'Window (sale date): {start} .. {run}')

    db_bp = {nbp(c.get('book_page')) for c in polk if nbp(c.get('book_page'))}
    db_pid = collections.defaultdict(list)
    for c in polk:
        if c.get('parcel_id') and c.get('sale_date_iso'):
            db_pid[npid(c['parcel_id'])].append(c)
    skip = {}
    sp = os.path.join(HERE, 'polk_skiplist.json')
    if os.path.exists(sp):
        skip = {nbp(k): v for k, v in json.load(open(sp)).items()}

    # ---- 1. all sales in window over $500k (every property class) ----
    rows = gis(3, f='json', where=f"SALEDT>=DATE '{start}' AND SALEDT<=DATE '{run}' AND PRICE>500000",
               outFields='*', returnGeometry='false', resultRecordCount=5000)['features']
    rows = [r['attributes'] for r in rows]
    deeds = collections.defaultdict(list)
    for r in rows:
        deeds[(r['BOOK'], r['PAGE'])].append(r)
    qual = [k for k, v in deeds.items() if any(r['SALETYPE'] == 'V' for r in v)]
    print(f'Sales rows {len(rows)} on {len(deeds)} deeds; {len(qual)} deeds with >=1 vacant-at-sale parcel')

    # ---- 2. every parcel on each qualifying deed (no price / use filter) ----
    allrows = []
    for i in range(0, len(qual), 8):
        where = ' OR '.join(f"(BOOK='{b}' AND PAGE='{p}')" for b, p in qual[i:i + 8])
        allrows += [f['attributes'] for f in gis(3, f='json', where=where, outFields='*', returnGeometry='false')['features']]
    g = collections.defaultdict(dict)
    for r in allrows:
        r['dt'] = datetime.datetime.utcfromtimestamp(r['SALEDT'] / 1000).date().isoformat()
        r['ac'] = (r.get('shape.STArea()') or 0) / 43560
        g[(r['BOOK'], r['PAGE'])][r['PARCEL_ID']] = r

    # ---- 3. address + land use ----
    pids = sorted({r['PARCEL_ID'] for v in g.values() for r in v.values()})
    we = {}
    for f in gis_or(4, 'PARCELID', pids, 'PARCELID,SITE_ADDR_1,SITE_ADDR_2,DOR_CD,DOR_DSCR,LN_NUM', batch=10):
        x = f['attributes']
        if x['PARCELID'] not in we or (x.get('LN_NUM') or 9) < (we[x['PARCELID']].get('LN_NUM') or 9):
            we[x['PARCELID']] = x
    for v in g.values():
        for r in v.values():
            r['we'] = we.get(r['PARCEL_ID'], {})

    # vacant parcels conveyed by >1 deed on the same day (split-parcel artifacts)
    vac_on = collections.defaultdict(set)
    for k, v in g.items():
        for r in v.values():
            if r['SALETYPE'] == 'V':
                vac_on[(r['PARCEL_ID'], r['dt'])].add(k)

    # ---- 4. consolidate, filter, dedup ----
    cands, dups, excl = [], [], []
    for k, v in sorted(g.items()):
        bp = f'{k[0]}/{k[1]}'
        ps = list(v.values())
        price = max(r['PRICE'] for r in ps)
        dt = max(collections.Counter(r['dt'] for r in ps).items(), key=lambda kv: (kv[1], kv[0]))[0]  # most common sale date on the deed
        ac = sum(r['ac'] for r in ps)
        info = dict(bp=bp, price=price, ac=round(ac, 2), n=len(ps), dt=dt, grantee=sq(ps[0]['GRANTEE']))
        why = None
        if nbp(bp) in db_bp:
            why = 'Book/Page already in database'
        else:
            for r in ps:
                for c in db_pid.get(npid(r['PARCEL_ID']), []):
                    d = abs((datetime.date.fromisoformat(c['sale_date_iso']) - datetime.date.fromisoformat(dt)).days)
                    if d <= 14:
                        why = f"parcel {dash(r['PARCEL_ID'])} sold {c['sale_date_iso']} already in database ({c.get('source')})"
                        break
                if why:
                    break
            if not why:  # same deal entered from CoStar/Compfolio/etc under another parcel number
                g1 = info['grantee'].upper().split(' ')[0] if info['grantee'] else ''
                for c in polk:
                    if not (c.get('sale_date_iso') and c.get('price')):
                        continue
                    d = abs((datetime.date.fromisoformat(c['sale_date_iso']) - datetime.date.fromisoformat(dt)).days)
                    if d <= 14 and abs(float(c['price']) - price) <= 0.01 * price and g1 and \
                            str(c.get('grantee') or '').upper().split(' ')[0] == g1:
                        why = f"same deal already in database from {c.get('source')} (parcel {c.get('parcel_id')}, {c['sale_date_iso']}, ${int(c['price']):,})"
                        break
        if why:
            dups.append((info, why)); continue
        if nbp(bp) in skip:
            excl.append((info, 'skiplist: ' + skip[nbp(bp)])); continue
        if ac <= 0.5:
            excl.append((info, 'summed acreage <= 0.5 ac')); continue
        vac = [r for r in ps if r['SALETYPE'] == 'V']
        imp = [r for r in ps if r['SALETYPE'] == 'I']
        impr_ac = sum(r['ac'] for r in imp)
        if impr_ac > ac / 2:
            excl.append((info, f'mostly improved ({impr_ac:.2f} of {ac:.2f} ac improved at sale) - same rule as the Osceola pipeline')); continue
        if imp and all(len(vac_on[(r['PARCEL_ID'], r['dt'])]) > 1 for r in vac):
            excl.append((info, 'only vacant parcel(s) were conveyed on a sibling deed the same day (parcel split); remainder improved')); continue
        addr = [r for r in vac if re.match(r'^[1-9]\d*\s', sq(r['we'].get('SITE_ADDR_1')))]
        prim = max(addr, key=lambda r: r['ac']) if addr else max(ps, key=lambda r: r['ac'])
        cands.append(dict(bp=bp, rows=ps, prim=prim, ac=ac, price=price, dt=dt, imp=imp, dts=sorted({r['dt'] for r in ps})))

    # ---- 5. centroids for primary parcels ----
    geo = {}
    for f in gis_or(0, 'parcelid', sorted({c['prim']['PARCEL_ID'] for c in cands}), 'parcelid', geometry=True, batch=5):
        pid = list(f['attributes'].values())[0]
        if f.get('geometry', {}).get('rings'):
            geo[pid] = centroid(f['geometry']['rings'])

    # ---- 6. ELC records ----
    out = []
    for c in sorted(cands, key=lambda c: -c['price']):
        p, rows_, bp = c['prim'], c['rows'], c['bp']
        ac = round(c['ac'], 4)
        others = sorted([r for r in rows_ if r is not p], key=lambda r: -r['ac'])
        cm = ('MIXED SALE (vacant + improved parcels) - ' if c['imp'] else '') + f"Deed OR {bp} ({p['TRNS_DSCR'].strip().title()}); "
        if len(rows_) == 1:
            cm += 'single parcel, vacant at sale.'
        else:
            cm += f'{len(rows_)} parcels under one deed, acreage summed ({ac:.2f} ac total, GIS parcel-polygon area). '
            if len(others) <= 12:
                cm += 'Also includes: ' + '; '.join(
                    f"{dash(r['PARCEL_ID'])} ({r['we'].get('DOR_DSCR') or '?'}{', improved at sale' if r['SALETYPE'] == 'I' else ''}, ~{r['ac']:.2f} ac)"
                    for r in others) + '.'
            else:
                mix = collections.Counter(r['we'].get('DOR_DSCR') or '?' for r in rows_)
                cm += 'Bulk lot takedown. Land-use mix: ' + ', '.join(f'{k2} x{v2}' for k2, v2 in mix.most_common()) + \
                      '. Other parcel IDs: ' + ', '.join(dash(r['PARCEL_ID']) for r in sorted(others, key=lambda r: r['PARCEL_ID'])) + '.'
        if c['imp']:
            cm += f" NOTE: {len(c['imp'])} of {len(rows_)} parcels were IMPROVED at sale (" + \
                  '; '.join(sorted({r['we'].get('DOR_DSCR') or '?' for r in c['imp']})) + ') - price includes improvements; not a pure land comp.'
        q = p.get('INSTRTYP_DSCR') or ''
        if q and not q.startswith('Q-'):
            cm += f" PA sale qualification: '{q}' (unqualified / non-arm's-length category - use with caution)."
        elif q and q not in ('Q-Multiple parcels', 'Q-Per examination of deed'):
            cm += f" PA qualification: '{q}'."
        if len(c['dts']) > 1:
            cm += f" PA shows differing sale dates across the deed's parcels ({', '.join(c['dts'])}); most common used."
        addr = fmt_addr(p['we'], p['PARCEL_ID'])
        lat, lon = geo.get(p['PARCEL_ID'], (None, None))
        deed = p['TRNS_DSCR'].strip()
        out.append(dict(
            parcel_id=dash(p['PARCEL_ID']), property_name=addr, address=addr,
            sale_date=datetime.date.fromisoformat(c['dt']).strftime('%b %d %Y'), sale_date_iso=c['dt'],
            price=int(c['price']), acreage=ac, price_per_acre=round(c['price'] / ac), transaction_type='Sale',
            grantor=sq(p['GRANTOR']), grantee=sq(p['GRANTEE']),
            deed_type={'WARRANTY DEED': 'WD', 'SPECIAL WARRANTY DEED': 'SW', 'QUIT CLAIM DEED': 'QC'}.get(deed, deed),
            property_type='Land', zoning=None,
            land_use=f"{p['we'].get('DOR_CD')} - {p['we'].get('DOR_DSCR')}" if p['we'].get('DOR_CD') else None,
            source='Polk County PA', county='Polk', county_fips='12105', date_added=run, lat=lat, lon=lon,
            aerial=PA_PAGE.format(p['PARCEL_ID']), book_page=bp, elc_deal=None, comments=cm.strip()))

    total = len(cands) + len(dups) + len([e for e in excl])
    print(f'Candidates {total} | duplicates skipped {len(dups)} | excluded {len(excl)} | new {len(out)}')
    for i, w in dups:
        print(f"  DUP  OR {i['bp']} ${i['price']:,} {i['ac']} ac {i['dt']} - {w}")
    for i, w in excl:
        print(f"  EXCL OR {i['bp']} ${i['price']:,} {i['ac']} ac {i['dt']} - {w}")
    for r in out:
        flag = ' [MIXED]' if r['comments'].startswith('MIXED') else ''
        print(f"  NEW  {r['sale_date_iso']} OR {r['book_page']} ${r['price']:,} {r['acreage']:.2f} ac ${r['price_per_acre']:,}/ac "
              f"{r['deed_type']} | {r['grantor']} -> {r['grantee']} | {r['address']} | {r['parcel_id']}{flag}"
              + ('' if r['lat'] else ' | NO CENTROID'))
    summary = dict(run_date=run, window=[start, run], base_branch=f'Test-patch-{n}', candidates=total,
                   duplicates=[dict(i, why=w) for i, w in dups], excluded=[dict(i, why=w) for i, w in excl], new=len(out))
    json.dump(summary, open(os.path.join(a.out_dir, f'polk-{run}-summary.json'), 'w'), indent=1)
    if not out:
        print(f'RESULT: No new comps this week ({total} candidates, {len(dups)} already in database).')
        return
    pj = os.path.join(a.out_dir, f'polk-{run}.json')
    json.dump(out, open(pj, 'w'), indent=1)
    mh = os.path.join(a.out_dir, f'polk-comps-map-{run}.html')
    rng = f"{min(r['sale_date_iso'] for r in out)} &ndash; {max(r['sale_date_iso'] for r in out)}"
    subprocess.check_call([sys.executable, os.path.join(HERE, 'polk_make_map.py'), 'Polk', run, rng,
                           'Polk County PA GIS (SALES / WEBEXPORT / PARCELS layers)', pj, mh])
    print(f'PENDING_JSON={pj}')
    print(f'MAP_HTML={mh}')


if __name__ == '__main__':
    main()
