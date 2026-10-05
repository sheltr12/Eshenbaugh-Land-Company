#!/usr/bin/env python3
"""
citrus_pipeline.py - Citrus County vacant-land comps for the ELC comps database (no browser needed).

  python3 citrus_pipeline.py --out-dir DIR [--run-date YYYY-MM-DD] [--margin-books 40] [--since-book N]

Writes DIR/citrus-<date>.json (new comps in the ELC schema, deduped against the newest Test-patch-N
comps.json), DIR/citrus-<date>-summary.json, and <Claude folder>/citrus-comps-map-<date>.html.
Push afterwards with push_comps_locked.py (concurrency-safe Test-patch-N+1 + Pages deploy).

Window (catch-up, self-healing):
  The PA "Property Transfer" CSV (citruspa.org > Downloads > Sales Download, hosted on Google Drive)
  has no recorded/posted date, and PA posts sales 4-10 weeks after recording. OR Book numbers rise
  with recording date, so every run scans every deed whose OR Book is >= (highest Citrus OR Book
  already in the database - margin-books). Missed weeks fill themselves in; dedup stops repeats.

Sources (plain HTTPS):
  PA sales CSV        ALTKEY, PARCEL ID, PC, OWNER (=grantee), BOOK, PAGE, SALE_DATE, VAC-IMP, INCD, PRICE
  PA parcel page      https://www.citruspa.org/_web/datalets/datalet.aspx?mode=profileall&UseSearch=no&pin=<ALTKEY>
                      -> situs address, PC code, Est. Parcel Acres, zoning, land-line description
  County GIS          maps.citrusbocc.com/server/rest/services/PublicData/LandDevelopment/MapServer/0 (LOTS)
                      ALTKEY -> parcel polygons -> exact area-weighted centroid (WGS84)
  Clerk LandmarkWeb   search.citrusclerk.org Book/Page search -> grantor, grantee, record date, instrument #

Rules:
  * Group ALL CSV rows (every property class) into deeds by OR Book/Page.
  * Deed qualifies if >= 1 parcel was vacant at sale (VAC-IMP = 'V') and price > $500,000.
  * Every parcel on the deed is included; the > 0.5 ac test applies to the SUMMED PA acreage.
  * Deeds where improved-at-sale parcels make up more than half the summed acreage are excluded
    ("mostly improved", same rule as the Polk/Osceola/Manatee pipelines). Others carrying improved
    parcels are kept and flagged "MIXED SALE".
  * Primary parcel = largest vacant parcel with a street-numbered address, else largest vacant.
  * citrus_skiplist.json (next to this script) lists Book/Pages never to add, with reasons.
"""
import argparse, collections, csv, datetime, html, io, json, os, re, subprocess, sys, time, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REPO = 'sheltr12/Eshenbaugh-Land-Company'
CSV_IDS = {2026: '1F93cN4g4kIbxFL7HXux1dytTeBiXy2Oi', 2025: '1Cdzi6F7-7HjmxcyLELfMBdg5JwKKMqyW'}
DL_PAGE = 'https://www.citruspa.org/_dnn/Downloads'
PA_PAGE = 'https://www.citruspa.org/_web/datalets/datalet.aspx?mode=profileall&UseSearch=no&pin={}'
GIS = 'https://maps.citrusbocc.com/server/rest/services/PublicData/LandDevelopment/MapServer/0/query'
CLERK = 'https://search.citrusclerk.org/LandmarkWeb'
UA = {'User-Agent': 'Mozilla/5.0 (elc-comps-bot)'}
squash = lambda s: re.sub(r'\s+', ' ', str(s or '').strip())
INSTR = {'00': 'WD', '01': 'QC/Corrective', '13': 'WD (from developer)'}


def load_token():
    if os.environ.get('GITHUB_TOKEN'):
        return os.environ['GITHUB_TOKEN'].strip()
    for p in [os.environ.get('GITHUB_TOKEN_FILE'), os.path.join(ROOT, '.github_token'),
              os.path.expanduser('~/Documents/Claude/.github_token')]:
        if p and os.path.exists(p):
            return open(p).read().strip()
    return None


def get(url, headers=None, data=None, tries=4, timeout=90):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=dict(UA, **(headers or {})))
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f'GET {url} failed: {last}')


def newest_patch_comps(tk):
    hdr = {'Authorization': f'token {tk}'} if tk else {}
    ns, page = {}, 1
    while True:
        b = json.loads(get(f'https://api.github.com/repos/{REPO}/branches?per_page=100&page={page}', hdr))
        for x in b:
            m = re.fullmatch(r'Test-patch-(\d+)', x['name'])
            if m:
                ns[int(m.group(1))] = x['commit']['sha']
        if len(b) < 100:
            break
        page += 1
    n = max(ns)
    d = json.loads(get(f'https://raw.githubusercontent.com/{REPO}/{ns[n]}/comps.json', hdr))
    return n, (d['comps'] if isinstance(d, dict) else d)


def csv_ids():
    """Year -> Google Drive file id, read live from the PA Downloads page (falls back to known ids)."""
    ids = dict(CSV_IDS)
    try:
        h = get(DL_PAGE).decode('utf8', 'ignore')
        for m in re.finditer(r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', h, re.S):
            t = html.unescape(re.sub('<[^>]+>', '', m.group(2)))
            y = re.search(r'(20\d\d)\s*Property Transfer CSV', t)
            fid = re.search(r'(?:/d/|id=)([\w-]{20,})', m.group(1))
            if y and fid:
                ids[int(y.group(1))] = fid.group(1)
    except Exception as e:
        print('WARN: could not read PA Downloads page, using known CSV ids:', e)
    return ids


def load_csv(fid):
    raw = get(f'https://drive.google.com/uc?export=download&id={fid}', timeout=180).decode('latin1')
    if not raw.startswith('ALTKEY'):
        raise RuntimeError('PA sales CSV download did not return CSV (Drive link changed?)')
    c = lambda x: re.sub(r'^="|"$', '', (x or '').strip())
    rows = []
    for r in csv.DictReader(io.StringIO(raw)):
        r = {k: c(v) for k, v in r.items()}
        if r['BOOK'].isdigit() and r['PAGE'].isdigit():
            r['bk'], r['pg'] = int(r['BOOK']), int(r['PAGE'])
            r['price'] = float(r['PRICE'] or 0)
            r['dt'] = datetime.datetime.strptime(r['SALE_DATE'], '%d-%b-%Y').date().isoformat()
            rows.append(r)
    return rows


def pa_parcel(altkey):
    h = get(PA_PAGE.format(altkey)).decode('utf8', 'ignore')
    t = html.unescape(re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' | ', h)))
    sq = lambda s: re.sub(r'[|\s]+', ' ', s or '').strip()
    i = t.find('Parcel ID:')
    s = t[i:i + 1500]
    hdr = re.search(r'Parcel ID:\s*\|?\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]*?)\s*\|', s)
    situs = ''
    m = re.search(r'\|\s*([^|]*,\s*[A-Z .]+,\s*\d{5})\s*\|', s)
    if m:
        situs = sq(m.group(1))
    pc = re.search(r'PC Code \|[ |]*([^|]+)', s)
    ac = re.search(r'Est\. Parcel Acres \|[ |]*([\d.,]+)', s)
    z = re.search(r'Zoning \| (.{0,400})', t)
    zoning, land = None, None
    if z:
        seg = [x.strip() for x in z.group(1).split('|') if x.strip()]
        if len(seg) >= 2 and re.fullmatch(r'\d+', seg[0]):
            land = seg[1]
            for x in seg[2:9]:
                if re.fullmatch(r'[A-Z][A-Z0-9\-/]{0,9}', x) and x not in ('A-ACREAGE', 'F-FRONT', 'S-SQUARE'):
                    zoning = x
                    break
    return dict(pid=sq(hdr.group(1)) if hdr else None, situs=situs,
                pc=sq(pc.group(1)) if pc else None, ac=float(ac.group(1).replace(',', '')) if ac else None,
                zoning=zoning, land=land)


def addr_of(situs):
    m = re.match(r'^(\d+)\s+(.*?)\s*,\s*([A-Z .]+?)\s*,\s*(\d{5})$', situs or '')
    if not m or m.group(1) == '0':
        return None
    title = lambda s: ' '.join(w if re.fullmatch(r'(NW|NE|SW|SE|N|S|E|W|US|SR|CR)', w) else w.title() for w in s.split())
    return f'{m.group(1)} {title(m.group(2))}, {title(m.group(3))}, FL {m.group(4)}'


def centroid(altkey):
    q = urllib.parse.urlencode({'where': f'ALTKEY={int(altkey)}', 'outFields': 'ALTKEY', 'returnGeometry': 'true',
                                'outSR': 4326, 'f': 'json'})
    j = json.loads(get(GIS + '?' + q))
    A = cx = cy = 0.0
    for f in j.get('features', []):
        for ring in f['geometry']['rings']:
            for (x0, y0), (x1, y1) in zip(ring, ring[1:]):
                c = x0 * y1 - x1 * y0
                A += c; cx += (x0 + x1) * c; cy += (y0 + y1) * c
    if abs(A) < 1e-14:
        return None, None
    return round(cy / (3 * A), 6), round(cx / (3 * A), 6)


class Clerk:
    def __init__(self):
        import http.cookiejar
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.op.addheaders = list(UA.items())
        self.op.open(CLERK, timeout=60).read()
        self.op.open(urllib.request.Request(CLERK + '/Search/SetDisclaimer', data=b'', method='POST'), timeout=60).read()
        self.op.open(CLERK + '/search/index?theme=.blue&section=searchCriteriaBookPage&quickSearchSelection=', timeout=60).read()

    def book_page(self, bk, pg):
        x = {'X-Requested-With': 'XMLHttpRequest', 'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'}
        body = f'bookType=4&book={bk}&page={pg}&exclude=false&ReturnIndexGroups=false&recordCount=100&mobileHomesOnly=false'
        self.op.open(urllib.request.Request(CLERK + '/Search/BookPageSearch', data=body.encode(), headers=x), timeout=60).read()
        d = json.loads(self.op.open(urllib.request.Request(CLERK + '/Search/GetSearchResults',
                                                          data=b'draw=1&start=0&length=10', headers=x), timeout=60).read())
        cl = lambda s: re.sub(r'^(nobreak_|hidden_|unclickable_)', '', html.unescape(str(s)))
        def names(s):
            ns = [re.sub(r'<[^>]+>', '', n).strip() for n in re.split(r"<div class='nameSeperator'></div>", cl(s)) if n.strip()]
            ns = [x for x in ns if not any(y != x and x.startswith(y) for y in ns)]  # drop 'X TRUSTEE' when 'X' is listed
            return ns[:4] + (['ET AL'] if len(ns) > 4 else [])
        for r in d.get('data', []):
            if int(r['10']) == bk and int(r['11']) == pg:
                return dict(grantor=' & '.join(names(r['5'])), grantee=' & '.join(names(r['6'])),
                            rec_date=cl(r['7']), doc_type=cl(r['8']), instr=cl(r['12']))
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out-dir', required=True)
    ap.add_argument('--map-dir', default=ROOT)
    ap.add_argument('--run-date', default=datetime.date.today().isoformat())
    ap.add_argument('--margin-books', type=int, default=40)
    ap.add_argument('--since-book', type=int)
    a = ap.parse_args()
    run = a.run_date
    tk = load_token()
    n, comps = newest_patch_comps(tk)
    ci = [c for c in comps if (c.get('county') or '').lower() == 'citrus']

    def nbp(s):
        m = re.search(r'(\d{3,5})\s*/\s*(?:Page\s*)?(\d{1,5})', str(s or ''))
        return (int(m.group(1)), int(m.group(2))) if m else None
    have_bp = {nbp(c.get('book_page')) for c in ci} - {None}
    npid = lambda p: re.sub(r'[\s\-]', '', str(p or '')).upper()
    have_pd = {(npid(c.get('parcel_id')), str(c.get('sale_date_iso'))[:10]) for c in ci}
    hw = max([b for b, _ in have_bp if b < 5000] or [0])
    since = a.since_book or max(hw - a.margin_books, 1)
    print(f'Base Test-patch-{n}: {len(ci)} Citrus comps; highest OR Book in DB {hw}; scanning OR Book >= {since}')

    ids = csv_ids()
    yr = int(run[:4])
    rows = []
    for y in sorted({yr, yr - 1} if int(run[5:7]) <= 3 else {yr}):
        if y in ids:
            rows += load_csv(ids[y])
    print(f'PA CSV rows: {len(rows)}; latest OR Book in CSV: {max(r["bk"] for r in rows if r["bk"] < 5000)}')
    skip = {}
    sp = os.path.join(HERE, 'citrus_skiplist.json')
    if os.path.exists(sp):
        skip = {nbp(k): v for k, v in json.load(open(sp)).items()}

    deeds = collections.defaultdict(list)
    for r in rows:
        if r['bk'] >= since and r['bk'] < 5000:
            deeds[(r['bk'], r['pg'])].append(r)
    cands, dups, excl = [], [], []
    for k, rs in sorted(deeds.items()):
        uniq = list({r['ALTKEY']: r for r in rs}.values())
        price = max(r['price'] for r in uniq)
        if price <= 500000 or not any(r['VAC-IMP'] == 'V' for r in uniq):
            continue
        info = dict(bp=f'{k[0]}/{k[1]:04d}', price=int(price), dt=collections.Counter(r['dt'] for r in uniq).most_common(1)[0][0])
        if k in have_bp or any((npid(r['PARCEL ID']), info['dt']) in have_pd for r in uniq):
            dups.append((info, 'already in database')); continue
        if k in skip:
            excl.append((info, 'skiplist: ' + skip[k])); continue
        for r in uniq:
            r['pa'] = pa_parcel(r['ALTKEY'])
            r['ac'] = r['pa']['ac'] or 0.0
        tot = round(sum(r['ac'] for r in uniq), 2)
        imp = [r for r in uniq if r['VAC-IMP'] != 'V']
        info['ac'] = tot
        if tot <= 0.5:
            excl.append((info, f'summed acreage {tot} <= 0.5')); continue
        if imp and sum(r['ac'] for r in imp) > tot / 2:
            excl.append((info, f'mostly improved ({sum(r["ac"] for r in imp):.2f} of {tot} ac improved at sale)')); continue
        cands.append(dict(info, k=k, rows=uniq, imp=imp))

    clerk = None
    try:
        clerk = Clerk()
    except Exception as e:
        print('WARN: Clerk LandmarkWeb unavailable:', e)
    out = []
    for c in sorted(cands, key=lambda c: -c['price']):
        vac = sorted([r for r in c['rows'] if r['VAC-IMP'] == 'V'], key=lambda r: -r['ac'])
        p = next((r for r in vac if addr_of(r['pa']['situs'])), vac[0])
        addr = addr_of(p['pa']['situs']) or f"Parcel {p['PARCEL ID'].strip()} ({p['pa']['situs'] or 'no site address'})"
        lat, lon = centroid(p['ALTKEY'])
        cr = None
        if clerk:
            try:
                cr = clerk.book_page(*c['k'])
            except Exception as e:
                print('WARN clerk', c['bp'], e)
        bk, pg = c['k']
        cm = ('MIXED SALE (vacant + improved parcels) - ' if c['imp'] else '')
        if cr:
            cm += f"Instr #{cr['instr']} (OR {bk}/{pg:04d}), Clerk doc type {cr['doc_type']}, recorded {cr['rec_date']} (PA sale date {c['dt']}). "
        else:
            cm += f'OR {bk}/{pg:04d} (PA sale date {c["dt"]}); Clerk record not retrieved - grantor unknown. '
        cm += f"PA instrument code {p['INCD']}. "
        if len(c['rows']) == 1:
            cm += f"Single parcel, vacant at sale, PA-assessed {c['ac']} ac."
        else:
            cm += f"{len(c['rows'])} parcels under one deed, PA-assessed acreage summed ({c['ac']} ac): " + '; '.join(
                f"{r['ALTKEY']} / {squash(r['PARCEL ID'])} ({r['pa']['pc']}, {r['ac']} ac{', improved at sale' if r['VAC-IMP'] != 'V' else ''}{', primary' if r is p else ''})"
                for r in sorted(c['rows'], key=lambda r: -r['ac'])) + '.'
        if c['imp']:
            cm += ' NOTE: price includes improvements; not a pure land comp.'
        cm += f" Coordinates: Citrus GIS LandDevelopment LOTS polygon centroid. ALTKEY {p['ALTKEY']}."
        out.append(dict(
            parcel_id=re.sub(r'\s+', ' ', p['PARCEL ID'].strip()), property_name=addr, address=addr,
            sale_date=datetime.date.fromisoformat(c['dt']).strftime('%b %d %Y'), sale_date_iso=c['dt'],
            price=c['price'], acreage=c['ac'], price_per_acre=round(c['price'] / c['ac']), transaction_type='Sale',
            grantor=cr['grantor'] if cr else None, grantee=cr['grantee'] if cr else p['OWNER'].strip(),
            deed_type=INSTR.get(p['INCD'], 'Deed'), property_type='Land', zoning=p['pa']['zoning'],
            land_use=f"{p['pa']['pc']}" + (f" ({p['pa']['land']})" if p['pa']['land'] else ''),
            source='Citrus County PA', county='Citrus', county_fips='12017', date_added=run, lat=lat, lon=lon,
            aerial=PA_PAGE.format(p['ALTKEY']), book_page=f'OR Book {bk} / Page {pg:04d}', elc_deal=None, comments=cm))

    total = len(cands) + len(dups) + len(excl)
    print(f'Candidates {total} | duplicates skipped {len(dups)} | excluded {len(excl)} | new {len(out)}')
    for i, w in dups:
        print(f"  DUP  OR {i['bp']} ${i['price']:,} {i['dt']} - {w}")
    for i, w in excl:
        print(f"  EXCL OR {i['bp']} ${i['price']:,} {i.get('ac')} ac {i['dt']} - {w}")
    for r in out:
        print(f"  NEW  {r['sale_date_iso']} {r['book_page']} ${r['price']:,} {r['acreage']} ac ${r['price_per_acre']:,}/ac {r['deed_type']} | "
              f"{r['grantor']} -> {r['grantee']} | {r['address']} | {r['parcel_id']}" + ('' if r['lat'] else ' | NO CENTROID')
              + (' [MIXED]' if r['comments'].startswith('MIXED') else ''))
    os.makedirs(a.out_dir, exist_ok=True)
    json.dump(dict(run_date=run, base_branch=f'Test-patch-{n}', since_book=since, db_high_water_book=hw, candidates=total,
                   duplicates=[dict(i, why=w) for i, w in dups], excluded=[dict(i, why=w) for i, w in excl], new=len(out)),
              open(os.path.join(a.out_dir, f'citrus-{run}-summary.json'), 'w'), indent=1)
    if not out:
        print(f'RESULT: No new comps this week ({total} candidates, {len(dups)} already in database).')
        return
    pj = os.path.join(a.out_dir, f'citrus-{run}.json')
    json.dump(out, open(pj, 'w'), indent=1)
    mh = os.path.join(a.map_dir, f'citrus-comps-map-{run}.html')
    rng = f"{min(r['sale_date_iso'] for r in out)} &ndash; {max(r['sale_date_iso'] for r in out)}"
    subprocess.check_call([sys.executable, os.path.join(HERE, 'make_map.py'), 'Citrus', run, rng,
                           'Citrus County PA sales CSV + Citrus GIS + Clerk LandmarkWeb', pj, mh])
    print(f'PENDING_JSON={pj}')
    print(f'MAP_HTML={mh}')


if __name__ == '__main__':
    main()
