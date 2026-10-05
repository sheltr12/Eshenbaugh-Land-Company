#!/usr/bin/env python3
"""
sarasota_pipeline.py - Sarasota County vacant land comps for the ELC comps database (no browser).

Usage:
  python3 sarasota_pipeline.py --out-dir DIR [--run-date YYYY-MM-DD] [--since-instrument N] [--margin 6000] [--cache DIR]

What it does:
  1. Downloads the SCPA nightly "Parcel and Sales" CSV bundle
     (https://www.sc-pa.com/downloads/SCPA_Parcels_Sales_CSV.zip): ParcelSales.csv (every recorded
     sale, all property classes, with Clerk instrument # in LegalReference) and Sarasota.csv (parcel roll).
  2. Window = deeds whose Clerk instrument number is above the database high-water mark (the highest
     Sarasota instrument already in comps.json on the newest Test-patch-N) minus --margin instruments
     (~1.5-2 weeks of recordings, to catch deeds the PA posts out of order). Instrument numbers are
     assigned in recording order, so consecutive runs tile the calendar with no gaps even when a week
     is missed. The PA file has no recorded/posted date column; recording dates shown are estimates
     (running max of sale dates in instrument order).
  3. Groups the FULL unfiltered sales list by instrument (one recorded deed), keeps deeds with
     price > $500,000 and at least one vacant-land parcel, sums exact GIS acreage over EVERY parcel on
     the deed (any land use), and applies the > 0.5 ac test to the summed acreage.
  4. Excludes mostly-improved deeds (improved parcels > half of the summed acreage; matches the Polk and
     Osceola rule). Deeds with some improved parcels are kept and flagged "MIXED" in comments.
  5. Dedups against comps.json on the newest Test-patch-N (parcel + sale date +/-14 days, or instrument #).
  6. Pulls exact parcel centroids + measured acreage from Sarasota County GIS
     (ags3.scgov.net Hosted/ParcelProperty FeatureServer/0), never geocoded.
  7. Writes sarasota-<date>.json (DB schema), sarasota-<date>-summary.json and sarasota-comps-map-<date>.html.
     Prints "RESULT: No new comps" when nothing is new.

Token (read-only use here, for reading the newest branch): $GITHUB_TOKEN, else ~/Documents/Claude/.github_token,
else ../.github_token. Works without a token too (public repo), just with lower API rate limits.
"""
import argparse, datetime, glob, io, json, os, re, subprocess, sys, time, urllib.parse, urllib.request, zipfile

import pandas as pd

REPO = 'sheltr12/Eshenbaugh-Land-Company'
ZIP_URL = 'https://www.sc-pa.com/downloads/SCPA_Parcels_Sales_CSV.zip'
GIS = 'https://ags3.scgov.net/server/rest/services/Hosted/ParcelProperty/FeatureServer/0/query'
HERE = os.path.dirname(os.path.abspath(__file__))
AG = re.compile(r'^(5[0-9]|6[0-9])')  # DOR 50-69 agricultural classes count as land, not improvements
BUILDERS = re.compile(r'LENNAR|WEEKLEY|HORTON|NEAL COMMUNITIES|ICI HOMES|M/?I HOMES|HOMES BY WEST BAY|PULTE|'
                      r'TAYLOR MORRISON|DIVOSTA|MATTAMY|KB HOME|MERITAGE|TOLL BROTHERS|DREES|STOCK DEVELOPMENT', re.I)


def load_token():
    if os.environ.get('GITHUB_TOKEN'):
        return os.environ['GITHUB_TOKEN'].strip()
    paths = [os.path.expanduser('~/Documents/Claude/.github_token'), os.path.join(HERE, '..', '.github_token')]
    for p in paths + glob.glob('/sessions/*/mnt/Claude/.github_token'):
        if os.path.exists(p):
            return open(p).read().strip()
    return None


def http(url, data=None, tk=None, tries=5, raw=False):
    h = {'User-Agent': 'elc-comps-bot'}
    if tk:
        h['Authorization'] = f'token {tk}'
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode() if data else None, headers=h)
            with urllib.request.urlopen(req, timeout=300) as r:
                b = r.read()
                return b if raw else json.loads(b)
        except Exception as e:
            if i == tries - 1:
                raise
            time.sleep(3 * (i + 1))


def newest_patch(tk):
    best, page = None, 1
    while True:
        b = http(f'https://api.github.com/repos/{REPO}/branches?per_page=100&page={page}', tk=tk)
        for x in b:
            m = re.fullmatch(r'Test-patch-(\d+)', x['name'])
            if m and (best is None or int(m.group(1)) > best[0]):
                best = (int(m.group(1)), x['commit']['sha'])
        if len(b) < 100:
            break
        page += 1
    if not best:
        sys.exit('No Test-patch-N branch found.')
    db = json.loads(http(f'https://raw.githubusercontent.com/{REPO}/{best[1]}/comps.json', tk=tk, raw=True))
    return best[0], (db['comps'] if isinstance(db, dict) else db)


npid = lambda s: re.sub(r'[\s\-]', '', str(s or '')).upper()
clean = lambda s: re.sub(r'\s+', ' ', str(s or '')).strip() if str(s or '').strip().lower() != 'nan' else ''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out-dir', required=True)
    ap.add_argument('--run-date', default=datetime.date.today().isoformat())
    ap.add_argument('--since-instrument', type=int, help='override: only deeds with instrument > N')
    ap.add_argument('--margin', type=int, default=6000, help='instrument look-back below the DB high-water mark')
    ap.add_argument('--cache', default=os.path.expanduser('~/scpa_cache'))
    a = ap.parse_args()
    run = a.run_date
    os.makedirs(a.out_dir, exist_ok=True)
    os.makedirs(a.cache, exist_ok=True)
    tk = load_token()

    # ---- database state ----
    n, db = newest_patch(tk)
    sar = [c for c in db if (c.get('county') or '').lower() == 'sarasota']
    db_inst = set()
    for c in sar:
        for v in (c.get('book_page'), c.get('comments')):
            db_inst.update(int(x) for x in re.findall(r'\b(20\d{8})\b', str(v or '')))
    pa_inst = [int(re.sub(r'\D', '', str(c.get('book_page')))) for c in sar
               if c.get('source') == 'Sarasota County PA' and re.fullmatch(r'20\d{8}', str(c.get('book_page') or ''))]
    hw = max(pa_inst) if pa_inst else max(db_inst)
    since = a.since_instrument if a.since_instrument is not None else hw - a.margin
    print(f'Base Test-patch-{n}: {len(db)} comps, {len(sar)} Sarasota. DB high-water instrument {hw}; window instrument > {since}')

    # ---- PA bulk data ----
    zp = os.path.join(a.cache, 'SCPA_Parcels_Sales_CSV.zip')
    if not (os.path.exists(zp) and time.time() - os.path.getmtime(zp) < 6 * 3600):
        open(zp, 'wb').write(http(ZIP_URL, raw=True))
    z = zipfile.ZipFile(zp)
    rd = lambda name, **k: pd.read_csv(io.BytesIO(z.read(f'Parcel_Sales_CSV/{name}')), dtype=str, encoding='latin-1', **k).apply(lambda c: c.str.strip())
    s = rd('ParcelSales.csv')
    s['inst'] = pd.to_numeric(s.LegalReference, errors='coerce')
    s['price'] = pd.to_numeric(s.SalePrice, errors='coerce')
    s['sd'] = pd.to_datetime(s.SaleDate.str[:10], errors='coerce')
    yr = s[(s.inst >= 2000000000) & (s.inst < 2100000000)].copy()
    env = yr.groupby('inst').sd.max().sort_index().cummax()  # estimated recording date (lower bound)
    yr['rec_est'] = yr.inst.map(env)
    data_through = str(yr.sd.max().date())
    w = yr[yr.inst > since].copy()
    lu = rd('LandUseCodes.csv'); lud = dict(zip(lu.Code, lu.Description))
    vac = {c for c, d in lud.items() if re.search(r'\bvac', str(d), re.I)}
    p = rd('Sarasota.csv', usecols=['ACCOUNT', 'NAME1', 'LOCN', 'LOCS', 'LOCD', 'LOCCITY', 'LOCZIP', 'STCD', 'ZONING_1',
                                    'LEGALREFER', 'LSQFT', 'IMPROVEMT', 'GRND_AREA']).drop_duplicates('ACCOUNT').set_index('ACCOUNT')
    w = w.join(p, on='Account')
    w['vac'] = w.STCD.isin(vac)
    num = lambda col: pd.to_numeric(w[col], errors='coerce').fillna(0)
    w['improved'] = (~w.vac) & (~w.STCD.fillna('').str.match(AG)) & ((num('IMPROVEMT') > 0) | (num('GRND_AREA') > 0))
    w['ac_pa'] = num('LSQFT') / 43560
    w['grantee'] = [r.NAME1 if str(r.LEGALREFER) == str(r.LegalReference) else None for r in w.itertuples()]
    print(f'PA data through sale date {data_through}; {w.LegalReference.nunique()} deeds / {len(w)} parcel rows in window (all classes)')

    deeds = [ins for ins, d in w.groupby('LegalReference') if d.price.max() > 500000 and d.vac.any()]
    q = w[w.LegalReference.isin(deeds)].drop_duplicates(['LegalReference', 'Account']).copy()

    # ---- GIS acreage + centroids ----
    gis = {}
    accts = sorted(set(q.Account))
    for i in range(0, len(accts), 50):
        ch = accts[i:i + 50]
        r = http(GIS, data=dict(where='account in (%s)' % ','.join(f"'{x}'" for x in ch),
                                outFields='account,measuredacreage', returnCentroid='true', returnGeometry='false',
                                outSR=4326, f='json'))
        for f in r.get('features', []):
            c = f.get('centroid') or {}
            gis[f['attributes']['account']] = (f['attributes'].get('measuredacreage'), c.get('y'), c.get('x'))
    q['acre'] = [gis.get(x, (None,))[0] or pa for x, pa in zip(q.Account, q.ac_pa)]

    skp = os.path.join(HERE, 'sarasota_skiplist.json')  # {"<instrument>": "reason"} for deeds that are not land sales
    skip = json.load(open(skp)) if os.path.exists(skp) else {}
    out, excluded, dups = [], [], []
    for ins, d in q.groupby('LegalReference'):
        price = float(d.price.max()); tot = round(float(d.acre.sum()), 3)
        if tot <= 0.5:
            continue  # summed deed acreage test (silently dropped: single small lots, mostly builder home sales)
        imp_ac = float(d[d.improved].acre.sum())
        has = lambda r: clean(r.LOCN) not in ('', '0')
        d = d.assign(has=[has(r) for r in d.itertuples()])
        pv = d[d.vac & d.has]
        prim = (pv if len(pv) else d).sort_values('acre', ascending=False).iloc[0]
        street = ' '.join(x for x in [clean(prim.LOCN) if prim.has else '', clean(prim.LOCS), clean(prim.LOCD)] if x)
        addr = f"{street}, {clean(prim.LOCCITY).title()}, FL {clean(prim.LOCZIP)[:5]}"
        sd = prim.sd
        tag = f'Instrument {ins} | {addr} | ${price:,.0f} | {tot} ac'
        # dedup first, so records already in the database are counted as duplicates
        if int(ins) in db_inst or any(npid(c.get('parcel_id')) in set(map(npid, d.Account)) and c.get('sale_date_iso')
                                      and abs((datetime.date.fromisoformat(c['sale_date_iso'][:10]) - sd.date()).days) <= 14 for c in sar):
            dups.append(tag)
            continue
        if str(ins) in skip:
            excluded.append(f'{tag}: skiplist ({skip[str(ins)]})')
            continue
        if imp_ac > tot / 2:
            excluded.append(f'{tag}: mostly improved ({imp_ac:.2f} of {tot} ac improved)')
            continue
        g = gis.get(prim.Account, (None, None, None))
        grantee = next((x for x in d.grantee if x), None)
        parts = []
        if d.improved.any():
            parts.append('MIXED (vacant + improved parcels; price includes improvements): ' +
                         ', '.join(f'{r.Account} {clean(lud.get(r.STCD, r.STCD))} {r.acre:.2f} ac' for r in d[d.improved].itertuples()))
        parts.append(f'Clerk instrument {ins}')
        if len(d) > 1:
            parts.append(f'{len(d)} parcels under one deed; also includes: ' + ', '.join(
                f'{r.Account} ({clean(lud.get(r.STCD, r.STCD))} ~{r.acre:.2f} ac)' for r in d[d.Account != prim.Account].itertuples()))
        if prim.QualCode not in ('01', '02', '03', '04', '05', '06'):
            parts.append(f'PA qual code {prim.QualCode} (not a qualified arm\'s-length sale per PA)')
        if grantee and BUILDERS.search(grantee):
            parts.append('Homebuilder lot takedown')
        if not grantee:
            parts.append('Grantee not yet posted by PA')
        out.append(dict(
            parcel_id=prim.Account, property_name=addr, address=addr,
            sale_date=sd.strftime('%b %d %Y'), sale_date_iso=sd.strftime('%Y-%m-%d'),
            price=int(price), acreage=tot, price_per_acre=round(price / tot),
            transaction_type='Sale', grantor=clean(prim.Grantor) or None, grantee=clean(grantee) or None,
            deed_type=prim.DeedType or None, property_type='Land', zoning=clean(prim.ZONING_1) or None,
            land_use=clean(lud.get(prim.STCD, prim.STCD)), source='Sarasota County PA', county='Sarasota',
            county_fips='12115', date_added=run,
            lat=round(g[1], 6) if g[1] else None, lon=round(g[2], 6) if g[2] else None,
            aerial=f'https://www.sc-pa.com/propertysearch/parcel/details/{prim.Account}',
            book_page=str(ins), elc_deal=None, comments='; '.join(parts),
            _rec_est=str(d.rec_est.max().date())))

    out.sort(key=lambda c: c['_rec_est'], reverse=True)
    recs = [{k: v for k, v in c.items() if not k.startswith('_')} for c in out]
    base = os.path.join(a.out_dir, f'sarasota-{run}')
    window = f"instrument &gt; {since} (recorded ~{min([c['_rec_est'] for c in out], default='n/a')} to ~{max([c['_rec_est'] for c in out], default='n/a')})"
    summary = dict(run_date=run, base_branch=f'Test-patch-{n}', db_high_water_instrument=hw, window_instrument_gt=since,
                   pa_data_through_sale_date=data_through, candidates=len(out) + len(dups) + len(excluded),
                   duplicates_skipped=len(dups), excluded=excluded, duplicates=dups, new=len(out))
    json.dump(summary, open(base + '-summary.json', 'w'), indent=1)
    print(f"Candidates {summary['candidates']} | duplicates skipped {len(dups)} | excluded {len(excluded)} | new {len(out)}")
    for e in excluded:
        print('  EXCLUDED', e)
    if not out:
        print('RESULT: No new comps')
        return
    json.dump(recs, open(base + '.json', 'w'), indent=1)
    mm = os.path.join(HERE, 'polk_make_map.py')
    subprocess.run([sys.executable, mm, 'Sarasota', run, window, 'Sarasota County PA nightly sales file + County GIS',
                    base + '.json', os.path.join(a.out_dir, f'sarasota-comps-map-{run}.html')], check=True)
    for i, c in enumerate(out, 1):
        print(f"{i:>2}. {c['sale_date_iso']} rec~{c['_rec_est']} {c['book_page']} {c['parcel_id']} ${c['price']:,} {c['acreage']} ac "
              f"${c['price_per_acre']:,}/ac {c['deed_type']} | {c['grantor']} -> {c['grantee']} | {c['address']} | {c['land_use']} | {c['comments']}")
    print(f'WROTE {base}.json')


if __name__ == '__main__':
    main()
