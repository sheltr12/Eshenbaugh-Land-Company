#!/usr/bin/env python3
"""
pasco_pipeline.py — Pasco County vacant-land comps, end to end (no browser needed).

  python3 pasco_pipeline.py --start YYYY-MM-DD --end YYYY-MM-DD --run-date YYYY-MM-DD --out pasco-YYYY-MM-DD.json

Steps: bulk CSV (sales_last_10years.csv + parcel.csv, already unzipped in cwd)
  -> group ALL sales rows by OR Book/Page (deed)
  -> keep deeds with >=1 row: sale date in window, price > 500k, vacant-at-sale ('V')
  -> sum acreage across every parcel on the deed; keep summed acreage > 0.5
  -> dedup against live comps.json (book/page, parcel_id+sale_date_iso)
  -> enrich: GIS layer 4 (address/owner/centroid), parcel.aspx (DOR, zoning, grantor, instrument)
  -> write comps JSON in the ELC schema
"""
import argparse, csv, json, re, time, html, urllib.request, urllib.parse
from datetime import date
from collections import defaultdict

UA = 'elc-comps-bot'
GIS = "https://maps.pascopa.com/arcgis/rest/services/Parcels/MapServer/4/query?"
DEED_MAP = {'WD':'Warranty Deed','QC':'Quit Claim Deed','TR':"Trustee's Deed",'SW':'Special Warranty Deed',
            'PR':'Personal Rep Deed','CT':'Certificate of Title','LA':'Land Contract','AL':'Agreement for Deed',
            'GD':'Guardian Deed','TD':'Tax Deed'}

def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def retry(fn, *a, tries=6, **kw):
    last = None
    for i in range(tries):
        try:
            return fn(*a, **kw)
        except Exception as e:
            last = e; time.sleep(1.2 * (i + 1))
    print("  giving up:", fn.__name__, last)
    return None

def clean(s):
    if s is None: return None
    s = re.sub(r'\s+', ' ', html.unescape(str(s))).strip()
    return s or None

def parcel_to_strap(pid):
    p = pid.split('-')
    if len(p) != 6: return None
    sc, tw, rg, sub, blk, lot = p
    return f"{rg.zfill(2)}{tw.zfill(2)}{sc.zfill(2)}{sub.zfill(4)}{blk.zfill(5)}{lot.zfill(4)}"

def norm_bp(s):
    return str(s).upper().replace(' ', '').replace('-', '/') if s else None

# ---------- GIS ----------
def gis_batch(pids):
    where = "ParcelID IN (" + ",".join(f"'{i}'" for i in pids) + ")"
    url = GIS + urllib.parse.urlencode({'where': where, 'returnGeometry': 'false', 'f': 'json',
        'outFields': 'ParcelID,PHYS_STREET,PHYS_CITY,PHYS_ZIP,VAL_ACRES,NAD_NAME_1,NAD_NAME_2'})
    d = json.loads(http_get(url))
    return {f['attributes']['ParcelID']: f['attributes'] for f in d.get('features', [])}

def gis_batch_chunked(pids, chunk=15):
    out = {}
    for i in range(0, len(pids), chunk):
        r = retry(gis_batch, pids[i:i+chunk], tries=4)
        if r: out.update(r)
        time.sleep(0.15)
    return out

def gis_centroid(pid):
    url = GIS + urllib.parse.urlencode({'where': f"ParcelID='{pid}'", 'outFields': 'ParcelID',
                                        'returnGeometry': 'true', 'outSR': '4326', 'f': 'json'})
    d = json.loads(http_get(url))
    feats = d.get('features', [])
    if not feats: return (None, None)
    rings = feats[0].get('geometry', {}).get('rings') or []
    if not rings: return (None, None)
    ring = max(rings, key=lambda r: abs(sum(r[i][0]*r[i+1][1]-r[i+1][0]*r[i][1] for i in range(len(r)-1))))
    A = Cx = Cy = 0.0
    for i in range(len(ring)-1):
        x0, y0 = ring[i]; x1, y1 = ring[i+1]
        cr = x0*y1 - x1*y0; A += cr; Cx += (x0+x1)*cr; Cy += (y0+y1)*cr
    A *= 0.5
    if A == 0:
        return (sum(p[1] for p in ring)/len(ring), sum(p[0] for p in ring)/len(ring))
    return (Cy/(6*A), Cx/(6*A))

# ---------- parcel.aspx ----------
def fetch_parcel_detail(pid):
    strap = parcel_to_strap(pid)
    raw = http_get(f"https://search.pascopa.com/parcel.aspx?parcel={strap}").decode('utf-8', 'replace')
    out = {}
    m = re.search(r'id="lblDORClass">([^<]*)<', raw);             out['dor_class'] = clean(m.group(1)) if m else None
    m = re.search(r'id="lblMailingAddress"[^>]*>(.*?)</span>', raw, re.S)
    if m:
        parts = [clean(x) for x in m.group(1).split('<br/>')]; parts = [p for p in parts if p]
        out['mailing_name'] = parts[0] if parts else None
    m = re.search(r'id="lblPhysicalAddress">([^<]*)<', raw);      out['physical_address'] = clean(m.group(1)) if m else None
    m = re.search(r'id="lblPreviousOwnerName">([^<]*)<', raw);    out['previous_owner'] = clean(m.group(1)) if m else None
    land_rows = []
    t = re.search(r'id="tblLandLines".*?<tbody>(.*?)</tbody>', raw, re.S)
    if t:
        for rm in re.finditer(r'<tr>(.*?)</tr>', t.group(1), re.S):
            cells = [clean(re.sub(r'<[^>]+>', '', c)) for c in re.findall(r'<t[hd][^>]*>(.*?)</t[hd]>', rm.group(1), re.S)]
            if len(cells) >= 5: land_rows.append(cells)
    out['land_rows'] = land_rows
    sale_rows = []
    t = re.search(r'id="tblSaleLines".*?<tbody>(.*?)</tbody>', raw, re.S)
    if t:
        for rm in re.finditer(r'<tr>(.*?)</tr>', t.group(1), re.S):
            rh = rm.group(1)
            im = re.search(r'instrument=(\d+)', rh)
            texts = [clean(re.sub(r'<[^>]+>', ' ', c)) for c in re.findall(r'<t[hd][^>]*>(.*?)</t[hd]>', rh, re.S)]
            sale_rows.append({'raw': texts, 'instrument': im.group(1) if im else None})
    out['sale_rows'] = sale_rows
    if not sale_rows and not out.get('physical_address'):
        raise RuntimeError("parcel.aspx returned no detail (unknown strap?)")
    return out

# ---------- main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', required=True); ap.add_argument('--end', required=True)
    ap.add_argument('--run-date', required=True); ap.add_argument('--out', required=True)
    ap.add_argument('--live', default='https://sheltr12.github.io/Eshenbaugh-Land-Company/comps.json')
    ap.add_argument('--min-price', type=float, default=500000); ap.add_argument('--min-acres', type=float, default=0.5)
    a = ap.parse_args()
    ws = date.fromisoformat(a.start); we = date.fromisoformat(a.end)

    parcel = {}
    with open('parcel.csv', newline='', encoding='utf-8', errors='replace') as f:
        for row in csv.DictReader(f): parcel[row['Parcel_Num']] = row

    by_bp = defaultdict(list)
    with open('sales_last_10years.csv', newline='', encoding='utf-8', errors='replace') as f:
        for row in csv.DictReader(f):
            try:
                m, d, y = row['Sale_Date'].split('/'); sd = date(int(y), int(m), int(d))
            except Exception: continue
            bp = (row['Sale_Book'].strip(), row['Sale_Page'].strip())
            if bp[0] and bp[1]: by_bp[bp].append({**row, 'sale_date': sd})

    def price_of(r):
        try: return float(r['Sale_Price'])
        except Exception: return 0.0

    deeds = []
    for bp, rows in by_bp.items():
        if not any(ws <= r['sale_date'] <= we and price_of(r) > a.min_price and r['Sale_VacImp'] == 'V' for r in rows):
            continue
        pin = {}
        for r in rows:
            pn = r['Parcel_Num']
            if pn not in pin or (ws <= r['sale_date'] <= we): pin[pn] = r
        tot = 0.0; plist = []; price = None; sd = None; dt = None
        for pn, r in pin.items():
            try: ac = float(parcel.get(pn, {}).get('Acres') or 0)
            except Exception: ac = 0.0
            tot += ac
            plist.append({'parcel_id': pn, 'acres': ac, 'land_use_desc': parcel.get(pn, {}).get('Prop_Use_Desc'),
                          'land_use_code': parcel.get(pn, {}).get('Prop_Use_Code'), 'vac_imp': r['Sale_VacImp']})
            price = price_of(r) or price; sd = r['sale_date']; dt = r['Sale_Deed_Type']
        if tot > a.min_acres:
            deeds.append({'book_page': f'{bp[0]}/{bp[1]}', 'price': price, 'sale_date_iso': sd.isoformat(),
                          'sale_date': sd.strftime('%b %d %Y'), 'deed_code': dt, 'parcels': plist, 'total_acres': round(tot, 3)})
    deeds.sort(key=lambda c: -(c['price'] or 0))
    print(f"candidates (window {a.start}..{a.end}): {len(deeds)}")

    live = json.loads(http_get(a.live, timeout=60))
    ex_bp = {norm_bp(c.get('book_page')) for c in live['comps'] if c.get('book_page')}
    ex_pd = {(str(c.get('parcel_id','')).upper().replace(' ','').replace('-',''), c.get('sale_date_iso'))
             for c in live['comps'] if c.get('parcel_id') and c.get('sale_date_iso')}
    new = []; dups = 0
    for c in deeds:
        if norm_bp(c['book_page']) in ex_bp or any((p['parcel_id'].upper().replace('-',''), c['sale_date_iso']) in ex_pd for p in c['parcels']):
            dups += 1
        else:
            new.append(c)
    print(f"duplicates already in DB: {dups}; new: {len(new)}")
    if not new:
        json.dump([], open(a.out, 'w')); print("SUMMARY candidates=%d dups=%d new=0" % (len(deeds), dups)); return

    out = []
    for i, c in enumerate(new, 1):
        pids = [p['parcel_id'] for p in c['parcels']]
        gis = gis_batch_chunked(pids)
        cands = [(p, gis.get(p['parcel_id'], {}), bool(clean(gis.get(p['parcel_id'], {}).get('PHYS_STREET')))) for p in c['parcels']]
        pool = [t for t in cands if t[2] and t[0]['vac_imp'] == 'V'] or [t for t in cands if t[2]] or cands
        pp, pg, _ = max(pool, key=lambda t: t[0]['acres'])
        pid = pp['parcel_id']
        detail = retry(fetch_parcel_detail, pid) or {}
        lat, lon = retry(gis_centroid, pid, tries=8) or (None, None)
        print(f"{i}/{len(new)} {c['book_page']} {pid} ${c['price']:,.0f} {c['total_acres']}ac lat={lat}")

        address = detail.get('physical_address')
        if not address:
            st, city = clean(pg.get('PHYS_STREET')), clean(pg.get('PHYS_CITY'))
            address = f"{st}, {city}, FL" if st and city else (st or f"Parcel {pid} (no assigned address)")

        sale_rows = detail.get('sale_rows', [])
        bk, pg_ = c['book_page'].split('/')
        matched = next((sr for sr in sale_rows if bk in ' '.join(x or '' for x in sr['raw']) and pg_ in ' '.join(x or '' for x in sr['raw'])), None)
        is_latest = bool(sale_rows) and matched is sale_rows[0]
        instrument = matched['instrument'] if matched else None
        deed_desc = (clean(matched['raw'][2]) if matched and len(matched['raw']) >= 3 else None) or DEED_MAP.get(c['deed_code'], c['deed_code'])
        grantor = detail.get('previous_owner') if (matched is None or is_latest) else None
        grantee = None; resale_note = None
        if is_latest or not sale_rows:
            n1, n2 = clean(pg.get('NAD_NAME_1')), clean(pg.get('NAD_NAME_2'))
            grantee = (n1 + (' ' + n2 if n2 else '')) if n1 else detail.get('mailing_name')
        elif matched is not None:
            resale_note = f"Parcel resold since this transaction (newer deed {clean(sale_rows[0]['raw'][0])}); grantee/grantor left blank rather than attributed to the later buyer."
        zoning = next((r[4] for r in detail.get('land_rows', []) if len(r) >= 5 and r[4]), None)
        land_use = (f"{pp['land_use_code']}-{pp['land_use_desc']}" if pp.get('land_use_code') and pp.get('land_use_desc') else detail.get('dor_class'))

        sec = [f"{p['parcel_id']} ({p.get('land_use_desc') or ''} ~{p['acres']}ac)" for p in c['parcels'] if p['parcel_id'] != pid]
        if len(c['parcels']) > 1:
            comments = f"{len(c['parcels'])} parcels under one deed (instrument {instrument or 'n/a'}, OR {c['book_page']}); also includes: " + ", ".join(sec[:8]) + (f" and {len(sec)-8} more" if len(sec) > 8 else "")
        else:
            comments = f"Single parcel deed (instrument {instrument or 'n/a'}, OR {c['book_page']})"
        if lat is None: comments += " | NOTE: exact GIS centroid unavailable after repeated attempts; lat/lon null per fallback rule."
        if resale_note: comments += " | NOTE: " + resale_note

        out.append({'parcel_id': pid, 'property_name': address, 'address': address, 'sale_date': c['sale_date'],
                    'sale_date_iso': c['sale_date_iso'], 'price': c['price'], 'acreage': c['total_acres'],
                    'price_per_acre': round(c['price'] / c['total_acres']), 'transaction_type': 'Sale',
                    'grantor': grantor, 'grantee': grantee, 'deed_type': deed_desc, 'property_type': 'Land',
                    'zoning': zoning, 'land_use': land_use, 'source': 'Pasco County PA', 'county': 'Pasco',
                    'county_fips': '12101', 'date_added': a.run_date, 'lat': lat, 'lon': lon,
                    'aerial': f"https://search.pascopa.com/parcel.aspx?parcel={parcel_to_strap(pid)}",
                    'book_page': c['book_page'], 'elc_deal': None, 'comments': comments})
        time.sleep(0.15)
    json.dump(out, open(a.out, 'w'), indent=2)
    print(f"SUMMARY candidates={len(deeds)} dups={dups} new={len(out)} -> {a.out}")

if __name__ == '__main__':
    main()
