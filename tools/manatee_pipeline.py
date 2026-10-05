#!/usr/bin/env python3
"""
manatee_pipeline.py - build new Manatee County vacant-land comps for the ELC comps database.

Usage:
  python3 manatee_pipeline.py --out-dir <dir> [--run-date YYYY-MM-DD] [--since YYYY-MM-DD] [--margin 6000]

Window (automatic, so missed weeks fill themselves in):
  The PA sales CSV has SALE_RECORD_DATE but it lags 4-6+ weeks behind the run date, so a strict
  "recorded in the last 7 days" window is almost always empty. Instead the window is keyed on the
  Clerk instrument number (assigned in recording order): every deed whose instrument is above the
  highest Manatee PA instrument already in comps.json on the newest Test-patch-N, minus a margin
  (default 6,000, about 2 weeks) for deeds the PA posts out of order. Deeds without an instrument
  number are included when recorded within 21 days of the high-water record date. --since forces a
  record-date start instead (used for backfills). Dedup against the database removes anything already there.

Rules (from the task spec):
  - Group the FULL unfiltered sales list into deeds by instrument number (fallback: sale date + price + grantee).
  - Keep a deed if price > $500,000, at least one parcel has a vacant land-use code, and the SUMMED
    acreage of every parcel on the deed (any land use) > 0.5 ac.
  - Exclude deeds where improved parcels make up more than half the acreage, and instruments in
    manatee_skiplist.json. Deeds with some improved parcels are kept but flagged MIXED.
  - Primary parcel = largest vacant parcel with a street address, else largest parcel.
  - Exact coordinates from the PA GIS endpoint (no geocoding). The endpoint returns WGS84 lon/lat for
    addressed parcels and Florida State Plane West feet (EPSG:2237) for unaddressed ones; both handled.
  - Dedup vs newest Test-patch-N: parcel_id + sale_date_iso, instrument number, or (Manatee) same price
    with sale date within 5 days (catches CoStar/Buildout copies that lack a parcel ID).
Outputs in --out-dir: manatee-<date>.json, manatee-<date>-summary.json, manatee-comps-map-<date>.html
Prints "RESULT: No new comps" when nothing is new.
"""
import argparse, concurrent.futures as cf, datetime as dt, io, json, os, re, subprocess, sys, urllib.request

REPO = 'sheltr12/Eshenbaugh-Land-Company'
CSV_URL = 'https://www.manateepao.gov/data/Manateesales.csv'
GIS_URL = 'https://www.manateepao.gov/wp-content/themes/frontier-child/models/pao-model-gis.php?parid={}'
VACANT = set('0000 0001 0002 0003 0009 0010 0040 0050 0055 0900 0940 1000 1001 1009 1033 1040 1041 4000 '
             '7000 8082 8083 8086 8087 8089 9900 9901 9909'.split())
BUILDERS = ['LENNAR', 'HORTON', 'PULTE', 'WEEKLEY', 'WEST BAY', 'PERRY HOMES', 'STARLIGHT', 'CARDEL', 'DRB GROUP',
            'DREAM FINDERS', 'ASHTON', 'ADAMS HOMES', 'CLAYTON PROP', 'ISSA HOMES', 'TAYLOR MORRISON', 'NEAL COMMUN',
            'MATTAMY', 'MERITAGE', 'KB HOME', 'TOLL BROS', 'M/I HOMES', 'DREES', 'CENTEX', 'DEL WEBB', 'NVR', 'RYAN HOMES',
            'HOMES BY WEST BAY', 'PARK SQUARE', 'STOCK DEVELOPMENT', 'DR HORTON', 'D R HORTON', 'MARONDA', 'WILLIAMS HOMES']
HERE = os.path.dirname(os.path.abspath(__file__))
UA = {'User-Agent': 'Mozilla/5.0 (elc-comps)'}


def get(url, timeout=180):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def token():
    t = os.environ.get('GITHUB_TOKEN')
    if t:
        return t.strip()
    for p in [os.path.expanduser('~/Documents/Claude/.github_token'), os.path.join(HERE, '..', '.github_token')]:
        if os.path.exists(p):
            return open(p).read().strip()
    return None


def newest_patch():
    tk, best, page = token(), (-1, None), 1
    h = dict(UA, Accept='application/vnd.github+json')
    if tk:
        h['Authorization'] = f'token {tk}'
    while True:
        b = json.loads(urllib.request.urlopen(urllib.request.Request(
            f'https://api.github.com/repos/{REPO}/branches?per_page=100&page={page}', headers=h), timeout=60).read())
        for x in b:
            m = re.fullmatch(r'Test-patch-(\d+)', x['name'])
            if m and int(m.group(1)) > best[0]:
                best = (int(m.group(1)), x['commit']['sha'])
        if len(b) < 100:
            return best
        page += 1


def main():
    import pandas as pd
    try:
        from pyproj import Transformer
    except ImportError:
        subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', 'pyproj', '--break-system-packages'], check=False)
        from pyproj import Transformer
    ap = argparse.ArgumentParser()
    ap.add_argument('--out-dir', required=True)
    ap.add_argument('--run-date', default=dt.date.today().isoformat())
    ap.add_argument('--since', help='record-date start (YYYY-MM-DD); overrides the instrument high-water window')
    ap.add_argument('--margin', type=int, default=6000)
    a = ap.parse_args()
    os.makedirs(a.out_dir, exist_ok=True)
    T = Transformer.from_crs(2237, 4326, always_xy=True)

    n, sha = newest_patch()
    print(f'Database: Test-patch-{n}')
    db = json.loads(get(f'https://raw.githubusercontent.com/{REPO}/{sha}/comps.json'))
    comps = db['comps'] if isinstance(db, dict) else db
    nid = lambda s: re.sub(r'[\s-]', '', str(s or '')).upper()
    keys = {(nid(c.get('parcel_id')), c.get('sale_date_iso')) for c in comps}
    instr_db = set()
    for c in comps:
        instr_db |= set(re.findall(r'\b(20\d{10})\b', str(c.get('book_page') or '') + ' ' + str(c.get('comments') or '')))
    man = [c for c in comps if c.get('county') == 'Manatee' and c.get('sale_date_iso') and c.get('price')]
    man_pa = [int(c['book_page']) for c in man if c.get('source') == 'Manatee County PA' and re.fullmatch(r'20\d{10}', str(c.get('book_page') or ''))]

    df = pd.read_csv(io.BytesIO(get(CSV_URL, 300)), dtype=str).fillna('')
    df['rd'] = pd.to_datetime(df.SALE_RECORD_DATE, format='%m/%d/%Y', errors='coerce')
    df['p'] = pd.to_numeric(df.SALE_PRICE, errors='coerce').fillna(0)
    df['a'] = pd.to_numeric(df.LAND_ACREAGE, errors='coerce').fillna(0)
    df['inum'] = pd.to_numeric(df.SALE_INSTR_NUMB, errors='coerce').fillna(0).astype('int64')
    print(f'PA file: {len(df)} rows, latest record date {df.rd.max().date()}')
    norm = lambda s: ' '.join(s.upper().split())
    df['gk'] = df.SALE_INSTR_NUMB.where(df.inum > 0, df.SALE_DATE + '|' + df.SALE_PRICE + '|' + df.SALE_GRANTEE.map(norm))

    if a.since:
        win = df.rd >= a.since
        wdesc = f'recorded {a.since} or later'
    else:
        hw = max(man_pa) if man_pa else 0
        lo = hw - a.margin
        hw_rd = df.loc[df.inum == hw, 'rd'].min() if hw else None
        rd_lo = (hw_rd - pd.Timedelta(days=21)) if hw_rd is not None and not pd.isna(hw_rd) else pd.Timestamp(a.run_date) - pd.Timedelta(days=60)
        win = (df.inum > lo) | ((df.inum == 0) & (df.rd >= rd_lo))
        wdesc = f'instrument > {lo} (DB high-water {hw} minus {a.margin})'
    print('Window:', wdesc)
    skip = {}
    sp = os.path.join(HERE, 'manatee_skiplist.json')
    if os.path.exists(sp):
        skip = json.load(open(sp))

    # candidate deed keys come from the window, but each deed is rebuilt from the FULL file
    cand_keys = set(df.loc[win & (df.p > 500000), 'gk'])
    full = df[df.gk.isin(cand_keys)]
    out, dups, excluded = [], [], []
    for k, x in full.groupby('gk'):
        if not x.LAND_USE_CODE.isin(VACANT).any() or x.a.sum() <= 0.5:
            continue
        p = int(x.p.max())
        sd = pd.to_datetime(x.SALE_DATE.iloc[0])
        sdi = sd.strftime('%Y-%m-%d')
        if (any((nid(pid), sdi) in keys for pid in x.PARID) or k in instr_db
                or any(c['price'] == p and abs((dt.date.fromisoformat(c['sale_date_iso'][:10]) - sd.date()).days) <= 5 for c in man)):
            dups.append(k)
            continue
        if k in skip:
            excluded.append({'instrument': k, 'reason': 'skiplist: ' + str(skip[k])})
            continue
        imp = x[(x.BLDG1_YEAR_BUILT.str.strip() != '') | (pd.to_numeric(x.BLDGS_SQFT_LIVING, errors='coerce').fillna(0) > 0)
                | (pd.to_numeric(x.BLDGS_SQFT_UNROOF, errors='coerce').fillna(0) > 0)]
        ac = round(float(x.a.sum()), 4)
        if len(imp) and imp.a.sum() > ac / 2:
            excluded.append({'instrument': k, 'parcel': x.PARID.iloc[0], 'address': x.SITUS_ADDRESS.iloc[0], 'price': p,
                             'reason': f'mostly improved ({round(imp.a.sum(), 2)} of {ac} ac carry buildings)'})
            continue
        v = x[x.LAND_USE_CODE.isin(VACANT)]
        va = v[v.SITUS_ADDRESS.str.strip() != '']
        prim = (va if len(va) else v).sort_values('a', ascending=False).iloc[0]
        others = x[x.PARID != prim.PARID]
        lu = lambda o: o.LAND_USE_DESC.split(' (')[0]
        notes = []
        if len(imp):
            notes.append(f'MIXED: {len(imp)} improved parcel(s) on deed ({round(imp.a.sum(), 2)} ac); price includes improvements.')
        if len(x) > 1:
            notes.append(f'{len(x)} parcels under one deed (Instrument {k}); summed acreage {ac}. Primary {prim.PARID}. Also incl: '
                         + '; '.join(f'{o.PARID} ({lu(o)} ~{o.a}ac)' for _, o in others.head(8).iterrows())
                         + (f' (+{len(others) - 8} more)' if len(others) > 8 else ''))
        else:
            notes.append(f'Single-parcel deed (Instrument {k}).')
        if len(x) >= 3 and any(b in prim.SALE_GRANTEE.upper() for b in BUILDERS):
            notes.append('Homebuilder lot takedown.')
        if len(x) == 1 and ac < 1 and p >= 1000000 and 'Residential' in prim.LAND_USE_DESC:
            notes.append('CHECK: high price for a single residential lot; may be a new home that was vacant on the 1/1 assessment.')
        addr = (f'{prim.SITUS_ADDRESS.strip()}, {prim.SITUS_POSTAL_CITY.strip()}, FL {prim.SITUS_POSTAL_ZIP.strip()}'
                if prim.SITUS_ADDRESS.strip() else None)
        out.append(dict(parcel_id=prim.PARID, property_name=addr, address=addr, sale_date=sd.strftime('%b %d %Y'), sale_date_iso=sdi,
                        price=p, acreage=ac, price_per_acre=round(p / ac), transaction_type='Sale',
                        grantor=prim.SALE_GRANTOR or None, grantee=prim.SALE_GRANTEE or None, deed_type=prim.SALE_INSTR_TYPE or None,
                        property_type='Land', zoning=None, land_use=prim.LAND_USE_DESC, source='Manatee County PA', county='Manatee',
                        county_fips='12081', date_added=a.run_date, lat=None, lon=None,
                        aerial=f'https://www.manateepao.gov/parcel/?parid={prim.PARID}', book_page=k if k.isdigit() else None,
                        elc_deal=None, comments=' '.join(notes), _rec=str(x.rd.min().date()), _sub=prim.SUBDIVISION_NAME))

    def gis(o):
        try:
            row = json.loads(get(GIS_URL.format(o['parcel_id']), 30))['rows'][0]
            X, Y = float(row[1] or 0), float(row[2] or 0)
            if X and Y:
                lon, lat = (X, Y) if abs(X) <= 180 else T.transform(X, Y)
                o['lat'], o['lon'] = round(lat, 7), round(lon, 7)
            if not o['address'] and row[3]:
                o['address'] = o['property_name'] = row[3]
        except Exception as e:
            print('GIS miss', o['parcel_id'], e)
        if not o['address']:
            o['address'] = o['property_name'] = f"Parcel {o['parcel_id']}" + (f" ({o['_sub']})" if o['_sub'] else '')
        return o

    with cf.ThreadPoolExecutor(8) as ex:
        out = list(ex.map(gis, out))
    out.sort(key=lambda o: (o['sale_date_iso'], o['parcel_id']))
    summary = dict(run_date=a.run_date, base=f'Test-patch-{n}', window=wdesc, pa_latest_record=str(df.rd.max().date()),
                   candidates=len(out) + len(dups) + len(excluded), duplicates_skipped=len(dups), excluded=excluded, new=len(out),
                   no_coords=[o['parcel_id'] for o in out if o['lat'] is None])
    json.dump(summary, open(os.path.join(a.out_dir, f'manatee-{a.run_date}-summary.json'), 'w'), indent=1)
    print(json.dumps({k: v for k, v in summary.items() if k != 'excluded'}), '\nexcluded:', excluded)
    if not out:
        print(f'RESULT: No new comps ({summary["candidates"]} candidates, all already in database or excluded).')
        return
    clean = [{k: v for k, v in o.items() if not k.startswith('_')} for o in out]
    jp = os.path.join(a.out_dir, f'manatee-{a.run_date}.json')
    json.dump(clean, open(jp, 'w'), indent=1)
    mp = os.path.join(a.out_dir, f'manatee-comps-map-{a.run_date}.html')
    subprocess.run([sys.executable, os.path.join(HERE, 'polk_make_map.py'), 'Manatee', a.run_date,
                    f"{out[0]['sale_date_iso']} to {out[-1]['sale_date_iso']}", 'Manatee County PA sales file + PA GIS', jp, mp], check=True)
    for o in out:
        print(f"{o['sale_date_iso']} {o['parcel_id']:>11} ${o['price']:>11,} {o['acreage']:>9} ac ${o['price_per_acre']:>10,}/ac "
              f"{(o['grantee'] or '')[:28]:28} | {o['comments'][:90]}")
    print(f'WROTE {jp}\nWROTE {mp}')


if __name__ == '__main__':
    main()
