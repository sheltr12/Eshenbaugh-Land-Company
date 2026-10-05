#!/usr/bin/env python3
"""
osceola_pipeline.py - Osceola County vacant-land comps for the ELC comps database.

  python3 osceola_pipeline.py --out-dir DIR [--run-date YYYY-MM-DD] [--lookback-books 12]

Writes DIR/osceola-<date>.json (new comps, already deduped against the newest
Test-patch-N comps.json) and DIR/osceola-comps-map-<date>.html (Esri map + list).
Prints a summary. Push afterwards with push_comps_locked.py.

Window: catch-up. Every deed recorded after (latest Osceola OR book in the database
minus --lookback-books) through the run date is considered, so missed weeks are
filled automatically; dedup keeps anything already in the database out.

Sources (no browser needed):
  PA sales     https://search.property-appraiser.org/api/v1/sales       (OData)
  PA parcels   https://search.property-appraiser.org/api/v1/ParcelMarket (dorCode, totalAcres, lat/lon, Situs)
  Clerk        https://officialrecords.osceolaclerk.org/BrowserView/api/search  {BookType:'O',Book,Page} -> rec_date, file_num
"""
import argparse, datetime, json, os, re, sys, urllib.request
from collections import defaultdict

PA = 'https://search.property-appraiser.org/api/v1/'
CLERK = 'https://officialrecords.osceolaclerk.org/BrowserView/api/search'
REPO = 'sheltr12/Eshenbaugh-Land-Company'
UA = {'User-Agent': 'Mozilla/5.0 elc-comps'}


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=180) as r:
        return json.loads(r.read())


def post(url, data):
    req = urllib.request.Request(url, data=json.dumps(data).encode(), headers={**UA, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def odata(ent, params):
    q = urllib.parse.urlencode(params, quote_via=urllib.parse.quote_plus, safe="$'(),")
    url, out = PA + ent + '?' + q, []
    while url:
        d = get(url)
        out += d['value']
        url = d.get('@odata.nextLink')
    return out


import urllib.parse
norm = lambda t: re.sub(r'\s+', ' ', (t or '').upper()).strip()
np_ = lambda s: re.sub(r'[\s\-]', '', str(s or '').upper())


def nbp(s):
    m = re.search(r'(\d{3,6})\s*/\s*(\d{1,6})', str(s or ''))
    return f'{int(m.group(1))}/{int(m.group(2))}' if m else None


def latest_patch_comps():
    page, nums = 1, []
    while True:
        b = get(f'https://api.github.com/repos/{REPO}/branches?per_page=100&page={page}')
        nums += [int(m.group(1)) for x in b for m in [re.fullmatch(r'Test-patch-(\d+)', x['name'])] if m]
        if len(b) < 100:
            break
        page += 1
    n = max(nums)
    d = get(f'https://raw.githubusercontent.com/{REPO}/Test-patch-{n}/comps.json')
    return n, (d['comps'] if isinstance(d, dict) else d)


def fix_addr(s):
    s = (s or '').strip()
    m = re.match(r'(.*?)\s+(KISSIMMEE|SAINT CLOUD|ST CLOUD|CELEBRATION|DAVENPORT|HARMONY|ORLANDO|INTERCITY|HOLOPAW|KENANSVILLE|NARCOOSSEE|POINCIANA|HAINES CITY|LAKE BUENA VISTA)\s+FL\s+(\d{5})', s)
    return f'{m.group(1).title()}, {m.group(2).title()}, FL {m.group(3)}' if m else s.title()


def is_vac(p):
    return 'VAC' in (p.get('dorDesc') or '').upper()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out-dir', required=True)
    ap.add_argument('--run-date', default=datetime.date.today().isoformat())
    ap.add_argument('--lookback-books', type=int, default=12)
    ap.add_argument('--template', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'comps_map_template.html'))
    a = ap.parse_args()
    run = a.run_date

    n, db = latest_patch_comps()
    osc = [c for c in db if (c.get('county') or '') == 'Osceola']
    books = [int(nbp(c.get('book_page')).split('/')[0]) for c in osc if nbp(c.get('book_page'))]
    start_book = (max(books) if books else 7000) - a.lookback_books
    print(f'Database: Test-patch-{n}, {len(db)} comps, {len(osc)} Osceola; scanning OR books >= {start_book}')

    sales = odata('sales', {'$filter': f"or_bk ge '{start_book:04d}' and or_bk le '9999'", '$top': '200000'})
    straps = sorted(set(x['strap'] for x in sales))
    pm = {}
    for i in range(0, len(straps), 40):
        f = ' or '.join(f"strap eq '{t}'" for t in straps[i:i + 40])
        for r in odata('ParcelMarket', {'$filter': f, '$select': 'strap,dsp_strap,dorCode,dorDesc,totalAcres,lat,lon,Situs,subName'}):
            pm[r['strap']] = r
    print(f'PA sales rows: {len(sales)} on {len(straps)} parcels (all property classes)')

    # group into deeds: OR book/page, unioned with sale date + price + grantee for priced sales
    par = {}
    def f_(x):
        while par.setdefault(x, x) != x:
            x = par[x]
        return x
    def u(x, y):
        par[f_(x)] = f_(y)
    for i, x in enumerate(sales):
        u(('R', i), ('BP', x['or_bk'], x['or_pg']) if x['or_bk'] not in ('0000', '', None) else ('ID', i))
        if (x['price'] or 0) > 500000:
            u(('R', i), ('DPG', (x['dos'] or '')[:10], x['price'], norm(x['grantees'])))
    G = defaultdict(dict)
    for i, x in enumerate(sales):
        G[f_(('R', i))].setdefault(x['strap'], x)

    cands, notes = [], []
    for rows in G.values():
        rows = list(rows.values())
        ps = [pm[r['strap']] for r in rows if r['strap'] in pm]
        price = max((r['price'] or 0) for r in rows)
        if price <= 500000 or not any(is_vac(p) for p in ps):
            continue
        acres = sum(float(p['totalAcres'] or 0) for p in ps)
        if acres <= 0.5:
            continue
        r0 = rows[0]
        if r0['or_bk'] in ('0000', '', None):
            continue
        rec = post(CLERK, {'BookType': 'O', 'Book': str(int(r0['or_bk'])), 'Page': str(int(r0['or_pg']))})
        rd = rec[0]['rec_date'][:10] if rec else None
        inst = rec[0]['file_num'] if rec else None
        if rd and rd > run:
            continue
        impr_ac = sum(float(p['totalAcres'] or 0) for p in ps if not is_vac(p))
        bp = f"{r0['or_bk']}/{r0['or_pg']}"
        if impr_ac > acres / 2:
            notes.append(f'Excluded {bp} ${price:,.0f}: mostly improved ({impr_ac:.2f} of {acres:.2f} ac non-vacant)')
            continue
        addr_ok = lambda p: (p.get('Situs') or '').strip() and not (p.get('Situs') or '').strip().startswith('0 ')
        vac = [p for p in ps if is_vac(p)]
        prim = max([p for p in vac if addr_ok(p)] or vac, key=lambda p: float(p['totalAcres'] or 0))
        rp = next(r for r in rows if r['strap'] == prim['strap'])
        dt = datetime.date.fromisoformat(rp['dos'][:10])
        ac = round(acres, 3)
        sec = [p for p in ps if p['strap'] != prim['strap']]
        tail = f" Deed type {rp['trns_cd']} - {(rp.get('trans_dscr') or '').strip()}. Sale qualification flag {rp['qu_flg']}. Coordinates are PA parcel centroid(s), not geocoded."
        if len(ps) > 1:
            cm = (f'Multi-parcel deed: {len(ps)} parcels under one deed (OR {bp}, Instr #{inst}, recorded {rd}). Also includes: '
                  + ', '.join(f"{p['dsp_strap'].strip()} ({p['dorDesc'].strip().lower()} ~{float(p['totalAcres'] or 0):.2f} ac)" for p in sec)
                  + '. Acreage is the sum of PA-assessed acreage across all parcels.' + tail)
        else:
            cm = f'Single-parcel deed (OR {bp}, Instr #{inst}, recorded {rd}).' + tail
        nm = lambda s: ', '.join(t.strip() for t in norm(s).split(',') if t.strip())
        c = dict(parcel_id=prim['dsp_strap'].strip(), property_name=fix_addr(prim['Situs']), address=fix_addr(prim['Situs']),
                 sale_date=dt.strftime('%b %d %Y'), sale_date_iso=dt.isoformat(), price=int(price), acreage=ac,
                 price_per_acre=round(price / ac), transaction_type='Sale',
                 grantor=nm(rp.get('all_grantors') or rp['grantors']), grantee=nm(rp.get('all_grantees') or rp['grantees']),
                 deed_type=rp['trns_cd'], property_type='Land', zoning=None, land_use=prim['dorDesc'].strip(),
                 source='Osceola County PA', county='Osceola', county_fips='12097', date_added=run,
                 lat=round(float(prim['lat']), 6), lon=round(float(prim['lon']), 6),
                 aerial=f"https://search.property-appraiser.org/Search/MainSearch?pin={prim['strap'].strip()}",
                 book_page=bp, elc_deal=None, comments=cm)
        c['_x'] = dict(rec_date=rd, instrument=inst, sub=prim.get('subName'), pset=frozenset(p['strap'] for p in ps),
                       parcels=[dict(id=p['dsp_strap'].strip(), acres=float(p['totalAcres'] or 0), use=p['dorDesc'].strip(),
                                     lat=float(p['lat']), lon=float(p['lon']), pin=p['strap'].strip()) for p in ps])
        cands.append(c)

    # same parcels deeded twice on the same sale date (double close): keep the later deed only
    byk = defaultdict(list)
    for c in cands:
        byk[(c['_x']['pset'], c['sale_date_iso'])].append(c)
    keep = []
    for grp in byk.values():
        grp.sort(key=lambda c: tuple(int(v) for v in c['book_page'].split('/')))
        for g in grp[:-1]:
            notes.append(f"Excluded {g['book_page']} ${g['price']:,.0f}: same-day pass-through, resold in {grp[-1]['book_page']}")
            grp[-1]['comments'] += f" Same-day double close: seller acquired the same land the same day for ${g['price']:,} ({g['deed_type']}, OR {g['book_page']}, {g['grantor']} -> {g['grantee']}); only the end sale is recorded."
        keep.append(grp[-1])

    ex = set()
    for c in db:
        ex.add(('pd', np_(c.get('parcel_id')), str(c.get('sale_date_iso') or '')[:10]))
        if (c.get('county') or '') == 'Osceola':
            if nbp(c.get('book_page')):
                ex.add(('bp', nbp(c.get('book_page'))))
            for i in re.findall(r'Instr #?(\d{10})', str(c.get('comments') or '')):
                ex.add(('in', i))
    new, dups = [], 0
    for c in keep:
        k = {('pd', np_(p['id']), c['sale_date_iso']) for p in c['_x']['parcels']} | {('bp', nbp(c['book_page'])), ('in', c['_x']['instrument'])}
        if k & ex:
            dups += 1
            continue
        new.append(c)
    new.sort(key=lambda c: (c['_x']['rec_date'] or ''), reverse=True)
    print(f'Candidates {len(keep)} | duplicates skipped {dups} | new {len(new)}')
    for s in notes:
        print(s)
    for c in new:
        print(f"  {c['_x']['rec_date']} OR {c['book_page']} ${c['price']:,} {c['acreage']} ac ${c['price_per_acre']:,}/ac {c['deed_type']} | {c['grantor']} -> {c['grantee']} | {c['address']} | {c['parcel_id']} ({len(c['_x']['parcels'])} parcels)")
    if not new:
        print('RESULT: No new comps (all candidates already in database).')
        return
    os.makedirs(a.out_dir, exist_ok=True)
    pj = os.path.join(a.out_dir, f'osceola-{run}.json')
    json.dump([{k: v for k, v in c.items() if k != '_x'} for c in new], open(pj, 'w'), indent=2)
    md = []
    for c in new:
        x = c['_x']
        d = {k: v for k, v in c.items() if k != '_x'}
        d.update(rec_date=x['rec_date'], instrument=x['instrument'], sub=x['sub'], parcels=x['parcels'])
        md.append(d)
    lo, hi = min(c['_x']['rec_date'] for c in new), max(c['_x']['rec_date'] for c in new)
    t = open(a.template).read().replace('__DATA__', json.dumps(md)).replace('__DATE__', run) \
        .replace('__WINDOW__', f'New to the ELC database as of {run} · deeds recorded {lo} to {hi}')
    mh = os.path.join(a.out_dir, f'osceola-comps-map-{run}.html')
    open(mh, 'w').write(t)
    print(f'PENDING_JSON={pj}')
    print(f'MAP_HTML={mh}')


if __name__ == '__main__':
    main()
