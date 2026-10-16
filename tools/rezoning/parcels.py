#!/usr/bin/env python3
"""
parcels.py - parcel lookup for the ELC rezoning tracker.

    lookup(county, parcel_ids) -> {input_id: {"lat","lon","acres","address","owner","pa_url","norm_id", ...}}
    site(county, parcel_ids)   -> {"lat","lon","acres","found","missing"} | None

lat/lon are the area-weighted centroid of the parcel polygon(s) in WGS84, computed here from the
exact GIS geometry (requested with outSR=4326; area and centroid are computed on a local
ellipsoidal tangent plane, error < 1e-6 for parcel-sized shapes). acres = polygon area; PA
acreage only when a layer has no geometry for the parcel. Nothing is ever geocoded.

Sources (each county: its own PA/county layer first, then the FGIO/FDOR Florida Statewide
Cadastral layer as a fallback; the statewide layer also fills missing site addresses / owners):

  Hillsborough  maps.hillsboroughcounty.org InfoLayers/HC_Parcels/MapServer/0  (FOLIO, PIN, STRAP)
                gis.hcpafl.org Webmaps/HillsboroughFL_WebParcels/MapServer/0   (folio, strap)
  Pasco         maps.pascopa.com Parcels/MapServer/4                           (ParcelID dashed)
  Pinellas      egis.pinellas.gov PublicWebGIS/Parcels/MapServer/1             (PARCELID s-t-r, STRAP r-t-s)
  Manatee       gis.manateepao.gov Website/WebLayers/MapServer/0               (PARID)
  Polk          polkflpa.gov/proxy.ashx -> WebSiteQueryNew/MapServer/0         (PARCELID; POST only, no IN())
  Sarasota      ags3.scgov.net Hosted/ParcelProperty/FeatureServer/0           (account)
  Charlotte     agis.charlottecountyfl.gov Essentials/CCGIS_Web_Layers2022/MapServer/17 (ACCOUNT)
  Hernando      services2.arcgis.com (Hernando PA) Parcels/FeatureServer/0     (PARCEL_NUMBER, PARCEL_KEY)
  Lake          gis.lakecountyfl.gov OpenData/OpenData1/MapServer/12           (ParcelNumber, AltKey)
  Marion        gis.marionfl.org General/Parcels/MapServer/0                   (PARCEL, ALT_Key)
  Alachua       services1.arcgis.com (Alachua Co.) Parcels35/FeatureServer/0   (parcel)
  Citrus        statewide layer (PARCELNO fixed-width, ALT_KEY), then county
                maps.citrusbocc.com LandDevelopment/MapServer/0 lots by ALTKEY
  Orange        ocgis4.ocfl.net AGOL_Open_Data/MapServer/56                    (PARCEL rng-twp-sec, SEC_TWN_RNG display)
                vgispublic.ocpafl.org DYNAMIC/Dynamic_Parcels/MapServer/3      (OCPA, same fields)
  Seminole      utility.arcgis.com/usrsvcs/servers/9b9c9fd4.../InformationKiosk/MapServer/1
                (county kiosk via the ArcGIS Online proxy; ParcelNumber, ParcelFormat; the county's own
                gis.seminolecountyfl.gov and scpagis.scpafl.org refuse connections from here)
  Osceola       paags.property-appraiser.org FabricParcel/MapServer/0          (Osceola PA: PARCELID, dsp_strap)
  Volusia       maps5.vcgov.org Open_Data/Open_Data_3/FeatureServer/34         (PID short, ALTKEY)
  Brevard       gis.brevardfl.gov Base_Map/Parcel_New_WKID2881/MapServer/5     (PARCEL_ID 'TT RRSS-..', TaxAcct)
  fallback      services9.arcgis.com Florida_Statewide_Cadastral/FeatureServer/0 (CO_NO + PARCEL_ID / ALT_KEY)

ID normalization (input -> canonical value(s) queried; several candidates are tried in order
when the input is ambiguous, the first one that exists wins and "ambiguous" is set if more did):

  Hillsborough  folio: '36274.0302' | '036274-0302' | '0362740302' | '362740302' -> FOLIO '0362740302'
                (left part zero-padded to 6, right part to 4). PIN 'U-02-27-17-001-000000-00009.1'
                -> queried on PIN; 22-char STRAP '172702001000000000091U' -> queried on STRAP.
                norm_id = 10-digit folio.
  Pasco         'SS-TT-RR-SSSS-BBBBB-LLLL' (sec-twp-rng). Tokens are zero-padded (2,2,2,4,5,4);
                undashed 19-char input is split the same way. The PA strap / DIR_ID order
                (rng-twp-sec, e.g. '2123250000010000010') is tried as a second candidate when the
                first token could be a range (15-22). norm_id = dashed ParcelID.
  Pinellas      'SS-TT-RR-SSSSS-BBB-LLLL' (PA display, sec-twp-rng; '/' or ' ' also accepted),
                tokens padded (2,2,2,5,3,4); STRAP order rng-twp-sec ('16-30-02-...') tried as the
                second candidate when the first token could be a range (14-17). Queried on PARCELID
                (18 chars, sec-twp-rng). norm_id = 'SS-TT-RR-SSSSS-BBB-LLLL'.
  Manatee       digits only, leading zeros dropped (layer stores '153820006' for '0153820006');
                '1538200006', '15382.0006' all work. norm_id = PARID as stored.
  Polk          18 digits, '23-28-19-087501-000013' -> '232819087501000013' (tokens padded 2,2,2,6,6).
  Sarasota      10 digits, zero-padded: '780020114' / '0780-02-0114' -> '0780020114'.
  Charlotte     12 digits: '41-23-14-200-014' -> '412314200014'.
  Hernando      parcel number 'R32 323 17 5230 0000 00AA' (any separators or none, 'R' optional);
                1-8 digit input = PA key (PARCEL_KEY, e.g. '01807478' or '1807478').
                norm_id = PARCEL_NUMBER, alt_key = 8-digit key.
  Lake          'SS-TT-RR-SSSS-BBB-LLLLL' (tokens padded 2,2,2,4,3,5; layer stores 18 chars undashed);
                1-8 digit input = Alternate Key. norm_id = dashed parcel number, alt_key = AltKey.
  Marion        'NNNN-NNN-NNN' (most) or 'NNNNN-NNN-NN' (some) or 'NNNN+NNN-NNN'; a 10-digit
                input without separators is tried in all three shapes (4-3-3 first);
                1-9 digit input = Alternate Key (ALT_Key). norm_id = PARCEL as stored.
  Alachua       'NNNNN-NNN-NNN' (tokens padded 5,3,3; 11 digits undashed accepted).
  Citrus        PA format '18E-20S-06-0020-0040' / '18E20S06 0020 0040' (rng E, twp S, sec, then
                1-3 blank-able fields). Matched against the statewide fixed-width PARCELNO
                ('18E20S060020        0040') by LIKE on the tokens + exact compare with blanks
                removed; 1-7 digit input = AltKey. norm_id = '18E-20S-06-0020-0040', alt_key = AltKey.
  Orange        OCPA display 'SS-TT-RR-SSSS-BB-LLL' (sec-twp-rng, e.g. '28-22-29-0000-00-001'; dashes,
                spaces or none) and the OCPA compact / URL form rng-twp-sec ('292228000000001'). Dashed
                input is read sec-twp-rng first, 15 undashed digits rng-twp-sec first; the other reading is
                tried when its numbers fit (twp 19-25, rng 26-34), so 'ambiguous' is set when both exist
                (e.g. sec 27 rng 30 vs sec 30 rng 27). Queried on PARCEL (rng-twp-sec).
                norm_id = 'SS-TT-RR-SSSS-BB-LLL', alt_key None, pa_url = ocpaweb.ocpafl.org Parcel ID link.
  Seminole      'SS-TT-RR-SSS-BBBB-LLLL' (sec-twp-rng, alphanumeric, e.g. '26-20-30-5AR-0000-0010'),
                tokens padded (2,2,2,3,4,4) or 17 chars undashed. norm_id = PA ParcelFormat, alt_key None.
  Osceola       'SS-TT-RR-SSSS-BBBB-LLLL' (sec-twp-rng, alphanumeric block/lot, e.g. '05-25-27-4092-0001-1890'),
                optional leading 'R' ('R052527-409200011890'), 18 chars undashed accepted.
                norm_id = PA dsp_strap, alt_key None (the statewide ALT_KEY is not used by the PA).
  Volusia       1-8 digit input = altkey ('3967402'). Short PID 'PPPP-SS-BB-LLLL' ('8402-41-00-0160', i.e. twp
                last digit, rng last digit, sec, sub, block, lot; 12 chars undashed, lot may be alphanumeric).
                Full PID sec-twp-rng '02-18-34-41-00-0160' / '021834-41-00-0160' (or DOR rng-twp-sec order) is
                converted to the short PID. norm_id = 'PPPP-SS-BB-LLLL', alt_key = altkey.
  Brevard       1-8 digit input = tax account ('2105702'). BCPAO parcel ID twp-rng-sec-sub-blk-lot
                '21-35-29-02-*-11' (township may be '20G'; lot optional for acreage parcels '28-36-03-00-4'),
                the layer form '21 3529-02-*-11', and the long zero-padded form '24-36-23-00-00001.0-0000.00'
                (block/lot zeros and '.0' / '.00' stripped, the as-typed form tried second). Queried on PARCEL_ID.
                norm_id = '21-35-29-02-*-11', alt_key = tax account, pa_url = bcpao.us account link.

Cache: set RZ_PARCEL_CACHE=/path/cache.json to cache hits (180 d) and misses (5 d).
Only python stdlib + requests.
"""
import json, math, os, re, sys, time
import requests

UA = 'Mozilla/5.0 (compatible; elc-rezoning-tracker/1.0; +https://github.com/sheltr12)'
TIMEOUT = 90
SLEEP = 0.35
HIT_TTL = 180 * 86400
MISS_TTL = 5 * 86400
SW_URL = 'https://services9.arcgis.com/Gh9awoU677aKree0/arcgis/rest/services/Florida_Statewide_Cadastral/FeatureServer/0'

_session = None
_MEMO = {}  # in-process cache (same shape as the file cache)


def _log(*a):
    print('[parcels]', *a, file=sys.stderr)


def _sess():
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers['User-Agent'] = UA
    return _session


def _query(url, params, method='POST', tries=3, page=True):
    """ArcGIS REST query with retries/backoff and offset paging. Returns list of features."""
    feats, offset, seen = [], 0, set()
    while True:
        p = dict(params, f='json')
        if offset:
            p['resultOffset'] = offset
        last = None
        for i in range(tries):
            try:
                time.sleep(SLEEP)
                if method == 'POST':
                    r = _sess().post(url + '/query', data=p, timeout=TIMEOUT)
                else:
                    r = _sess().get(url + '/query', params=p, timeout=TIMEOUT)
                r.raise_for_status()
                d = r.json()
                if 'error' in d:
                    raise RuntimeError(f"ArcGIS error {d['error'].get('code')}: {d['error'].get('message')}")
                break
            except Exception as e:  # noqa: BLE001
                last = e
                if i < tries - 1:
                    time.sleep(2 * 2 ** i)
        else:
            raise RuntimeError(f'query failed after {tries} tries: {url} ({last})')
        fs = d.get('features') or []
        sigs = [json.dumps(f.get('attributes'), sort_keys=True) for f in fs]
        new = [f for f, s in zip(fs, sigs) if s not in seen]
        seen.update(sigs)
        feats += new
        if page and d.get('exceededTransferLimit') and new and offset < 50000:
            offset += len(fs)
            continue
        return feats


# ---------------------------------------------------------------- geometry
_A, _E2 = 6378137.0, 0.00669437999014


def _rings_area_centroid(rings_list):
    """rings_list: list of ring lists ([[lon,lat],...]) from one or more features.
    Returns (area_m2, lat, lon) using a local ellipsoidal tangent plane."""
    pts = [p for rings in rings_list for ring in rings for p in ring]
    if not pts:
        return None
    lon0, lat0 = pts[0][0], pts[0][1]
    s = math.sin(math.radians(lat0))
    w = 1 - _E2 * s * s
    M = _A * (1 - _E2) / w ** 1.5
    N = _A / math.sqrt(w)
    kx = math.radians(1) * N * math.cos(math.radians(lat0))
    ky = math.radians(1) * M
    A = cx = cy = 0.0
    for rings in rings_list:
        for ring in rings:
            xy = [((x - lon0) * kx, (y - lat0) * ky) for x, y, *_ in ring]
            for (x0, y0), (x1, y1) in zip(xy, xy[1:]):
                c = x0 * y1 - x1 * y0
                A += c
                cx += (x0 + x1) * c
                cy += (y0 + y1) * c
    if abs(A) < 1e-9:
        n = len(pts)
        return 0.0, sum(p[1] for p in pts) / n, sum(p[0] for p in pts) / n
    cx, cy = cx / (3 * A), cy / (3 * A)
    return abs(A) / 2, lat0 + cy / ky, lon0 + cx / kx


def _geom_from_feats(feats):
    """Dedupe identical polygons, return (acres, lat, lon) or None."""
    seen, rl = set(), []
    for f in feats:
        g = f.get('geometry') or {}
        rings = g.get('rings')
        if not rings:
            continue
        sig = (len(rings), tuple(round(c, 6) for c in rings[0][0][:2]), sum(len(r) for r in rings))
        if sig in seen:
            continue
        seen.add(sig)
        rl.append(rings)
    if not rl:
        return None
    a, lat, lon = _rings_area_centroid(rl)
    return round(a / 4046.8564224, 4), round(lat, 7), round(lon, 7)


# ---------------------------------------------------------------- helpers
def _sq(s):
    """Uppercase, alphanumerics only."""
    return re.sub(r'[^0-9A-Z]', '', str(s or '').upper())


def _toks(s):
    return [t for t in re.split(r'[^0-9A-Z]+', str(s or '').upper()) if t]


def _pad(tokens, widths):
    if len(tokens) != len(widths) or any(len(t) > w for t, w in zip(tokens, widths)):
        return None
    return [t.zfill(w) for t, w in zip(tokens, widths)]


def _split(s, widths):
    if len(s) != sum(widths):
        return None
    out, i = [], 0
    for w in widths:
        out.append(s[i:i + w])
        i += w
    return out


def _parts(raw, widths):
    """Tokens padded to widths, from a separated or an unseparated input."""
    t = _toks(raw)
    if len(t) == len(widths):
        return _pad(t, widths)
    return _split(_sq(raw), widths)


def _clean(s):
    s = re.sub(r'\s+', ' ', str(s or '')).strip(' ,')
    return s or None


def _addr(street, city=None, zipc=None):
    street = _clean(street)
    if street:
        street = re.sub(r'^0+\s+', '', street) or None
    if not street or street.upper() == 'UNKNOWN':  # Brevard / statewide placeholder
        return None
    tail = ' '.join(x for x in [_clean(city), _clean(zipc)] if x)
    return f'{street}, {tail}' if tail else street


def _int_str(v):
    try:
        return str(int(float(v)))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- sources
class Src:
    """One ArcGIS parcel layer. fields: kind -> field name ('id', 'alt'); numeric: kinds whose field
    is numeric. rec(attrs) -> dict(address, owner, pa_url, norm_id, alt_key, pa_acres)."""

    def __init__(self, name, url, fields, rec, out_fields, numeric=(), where=None, batch=50, method='POST',
                 canon=None, style='or', slow=()):
        self.name, self.url, self.fields, self.rec = name, url, fields, rec
        self.out_fields, self.numeric, self.where, self.batch, self.method = out_fields, set(numeric), where, batch, method
        self.canon = canon or {}  # kind -> fn(stored value) -> comparable value
        self.style = style        # 'or' (OR'd equality; Polk firewall blocks IN) or 'in' (Lake WAF blocks OR)
        self.slow = set(slow)     # kinds on unindexed fields: single try


def _sw_rec(pa_url=None, norm=None, alt=None):
    def rec(a):
        z = _int_str(a.get('PHY_ZIPCD'))
        return dict(address=_addr(a.get('PHY_ADDR1'), a.get('PHY_CITY'), z if z and z != '0' else None),
                    owner=_clean(a.get('OWN_NAME')), pa_url=pa_url(a) if pa_url else None,
                    norm_id=norm(a) if norm else _clean(a.get('PARCEL_ID')),
                    alt_key=alt(a) if alt else _clean(a.get('ALT_KEY')), pa_acres=None)
    return rec


def _sw(co_no, pa_url=None, norm=None, canon=None, alt=None):
    # PARCEL_ID / PARCELNO / CO_NO are indexed; ALT_KEY is not (a CO_NO-filtered ALT_KEY query takes ~45 s)
    # alt: fn(attrs) -> alt_key, for counties whose statewide ALT_KEY is not the PA's alternate key
    return Src('FL Statewide Cadastral', SW_URL, {'id': 'PARCEL_ID', 'alt': 'ALT_KEY'}, _sw_rec(pa_url, norm, alt),
               'PARCEL_ID,ALT_KEY,OWN_NAME,PHY_ADDR1,PHY_CITY,PHY_ZIPCD', where=f'CO_NO={co_no}', canon=canon,
               slow=('alt',))


class County:
    def __init__(self, name, co_no, normalize, sources, sw_map=None):
        self.name, self.co_no, self.normalize, self.sources = name, co_no, normalize, sources
        self.sw_map = sw_map  # fn(kind, value) -> (kind, value) for the statewide layer, or None


# ---- Hillsborough
def _hil_norm(raw):
    s = str(raw).strip().upper()
    if re.match(r'^[A-Z]-?\d{1,2}-', s):  # PIN 'U-02-27-17-001-000000-00009.1' -> STRAP
        t = s.replace(' ', '').split('-')
        if len(t) == 7:
            lot = t[6].split('.')
            p = _pad([t[3], t[2], t[1], t[4], t[5], lot[0], (lot[1] if len(lot) > 1 else '0')], (2, 2, 2, 3, 6, 5, 1))
            if p:
                return [('strap', ''.join(p) + t[0])]
    q = _sq(s)
    if re.fullmatch(r'\d{6}[0-9A-Z]{15}[A-Z]', q):  # 22-char STRAP
        return [('strap', q)]
    t = re.split(r'[.\-\s/]+', s)
    t = [x for x in t if x]
    if len(t) == 2 and all(x.isdigit() for x in t) and len(t[0]) <= 6 and len(t[1]) <= 4:
        return [('id', t[0].zfill(6) + t[1].zfill(4))]
    if q.isdigit() and 6 <= len(q) <= 10:
        return [('id', q.zfill(10))]
    return []


def _hil_pa(folio):
    return f'https://gis.hcpafl.org/propertysearch/#/parcel/basic/{folio}' if folio else None


def _hil_rec(a):
    return dict(address=_addr(a.get('SITE_ADDR'), a.get('SITE_CITY'), a.get('SITE_ZIP')), owner=_clean(a.get('OWNER')),
                pa_url=_hil_pa(a.get('FOLIO')), norm_id=a.get('FOLIO'), alt_key=_clean(a.get('PIN')),
                pa_acres=a.get('ACREAGE'))


def _hcpa_rec(a):
    return dict(address=_addr(a.get('FullAddress'), None, a.get('SiteZip')), owner=_clean(a.get('Owner1')),
                pa_url=_hil_pa(a.get('folio')), norm_id=a.get('folio'), alt_key=_clean(a.get('DisplayStrap')),
                pa_acres=None)


# ---- Pasco
def _str_order_candidates(raw, widths, rng_lo, rng_hi, sep='-'):
    p = _parts(raw, widths)
    if not p:
        return []
    out = []
    a = p                           # as given: sec-twp-rng
    b = [p[2], p[1], p[0]] + p[3:]  # input was in rng-twp-sec (strap) order
    ok = lambda x: x[2].isdigit() and rng_lo <= int(x[2]) <= rng_hi and x[0].isdigit() and 1 <= int(x[0]) <= 36  # noqa: E731
    if ok(a):
        out.append(a)
    if ok(b) and b != a:
        out.append(b)
    if not out:
        out.append(a)
    return [sep.join(x) for x in out]


def _pasco_norm(raw):
    return [('id', v) for v in _str_order_candidates(raw, (2, 2, 2, 4, 5, 4), 15, 22)]


def _pasco_strap(pid):
    t = pid.split('-')
    return ''.join([t[2], t[1], t[0]] + t[3:]) if len(t) == 6 else None


def _pasco_rec(a):
    pid = a.get('ParcelID')
    return dict(address=_addr(a.get('PHYS_STREET'), a.get('PHYS_CITY'), a.get('PHYS_ZIP')),
                owner=_clean(' '.join(x for x in [a.get('NAD_NAME_1'), a.get('NAD_NAME_2')] if x and x.strip())),
                pa_url=f'https://search.pascopa.com/parcel.aspx?parcel={_pasco_strap(pid)}' if pid else None,
                norm_id=pid, alt_key=None, pa_acres=a.get('VAL_ACRES'))


# ---- Pinellas
def _pin_norm(raw):
    return [('id', v) for v in _str_order_candidates(raw, (2, 2, 2, 5, 3, 4), 14, 17, sep='')]


def _pin_dsp(pid):
    p = _split(pid or '', (2, 2, 2, 5, 3, 4))
    return '-'.join(p) if p else pid


def _pin_rec(a):
    strap = a.get('STRAP')
    return dict(address=_addr(a.get('SITE_ADDRESS'), a.get('SITE_CITY'), a.get('SITE_ZIP')),
                owner=_clean(' '.join(x for x in [a.get('OWNER1'), a.get('OWNER2')] if x and x.strip())),
                pa_url=f'https://www.pcpao.gov/property-details?s={strap}' if strap else None,
                norm_id=_pin_dsp(a.get('PARCELID')), alt_key=strap, pa_acres=a.get('Acres'))


def _pin_sw(kind, v):  # PARCELID sec-twp-rng (18) -> statewide 'RR TT SS SSSSS BBB LLLL'
    p = _split(v, (2, 2, 2, 5, 3, 4))
    return ('id', ' '.join([p[2], p[1], p[0]] + p[3:])) if p else None


# ---- Manatee
def _man_norm(raw):
    q = _sq(raw)
    if q.isdigit() and 6 <= len(q) <= 10:
        return [('id', q.lstrip('0'))]
    return []


def _man_rec(a):
    pid = a.get('PARID')
    return dict(address=_addr(a.get('SITUS_ADDRESS'), a.get('SITUS_POSTAL_CITY'), a.get('SITUS_POSTAL_ZIP')),
                owner=_clean(a.get('PAR_OWNER_NAME1')),
                pa_url=f'https://www.manateepao.gov/parcel/?parid={pid}' if pid else None,
                norm_id=pid, alt_key=None, pa_acres=a.get('LAND_ACREAGE_CAMA'))


# ---- Polk
def _polk_norm(raw):
    p = _parts(raw, (2, 2, 2, 6, 6))
    return [('id', ''.join(p))] if p and ''.join(p).isdigit() else []


def _polk_rec(a):
    pid = a.get('PARCELID')
    return dict(address=_clean(a.get('PROP_LOCATION')), owner=_clean(a.get('OWNER1')),
                pa_url=f'https://www.polkflpa.gov/CamaDisplay.aspx?OutputMode=Display&SearchType=RealEstate&ParcelID={pid}' if pid else None,
                norm_id=pid, alt_key=None, pa_acres=a.get('TOT_ACREAGE'))


# ---- Sarasota
def _sar_norm(raw):
    q = _sq(raw)
    return [('id', q.zfill(10))] if q.isdigit() and 4 <= len(q) <= 10 else []


def _sar_rec(a):
    acct = a.get('account')
    return dict(address=_clean(a.get('fulladdress')), owner=_clean(a.get('name1')),
                pa_url=f'https://www.sc-pa.com/propertysearch/parcel/details/{acct}' if acct else None,
                norm_id=acct, alt_key=None, pa_acres=a.get('measuredacreage'))


# ---- Charlotte
def _cha_norm(raw):
    q = _sq(raw)
    return [('id', q)] if q.isdigit() and len(q) == 12 else []


def _cha_rec(a):
    acct = _clean(a.get('ACCOUNT'))
    return dict(address=_clean(a.get('FullPropertyAddress')), owner=_clean(a.get('ownersname')),
                pa_url=(f'https://www.ccappraiser.com/Show_parcel.asp?acct={acct}&gen=T&tax=T&bld=T&oth=T&sal=T&lnd=T&leg=T'
                        if acct else None),
                norm_id=acct, alt_key=None, pa_acres=None)


# ---- Hernando
def _her_norm(raw):
    q = _sq(raw)
    if q.isdigit() and len(q) <= 8:
        return [('alt', str(int(q)))]
    t = _toks(raw)
    if t and re.fullmatch(r'[A-Z]\d{2}', t[0]):          # 'R32 323 17 5230 0000 00AA'
        t = [t[0][0], t[0][1:]] + t[1:]
    elif t and re.fullmatch(r'[A-Z]', t[0]):
        pass
    else:
        t = ['R'] + t
    if len(t) == 7:
        if len(t[2]) <= 2:                              # twp written without its leading digit
            p = _pad(t[1:], (2, 2, 2, 4, 4, 4))
            return [('like', f'{t[0]}{p[0]} _{p[1]} ' + ' '.join(p[2:]))] if p else []
        p = _pad(t[1:], (2, 3, 2, 4, 4, 4))
        if p:
            return [('id', f'{t[0]}{p[0]} ' + ' '.join(p[1:]))]
    s = q if re.match(r'^[A-Z]', q) else 'R' + q
    if len(s) == 20:
        p = _split(s[1:], (2, 3, 2, 4, 4, 4))
        return [('id', f'{s[0]}{p[0]} ' + ' '.join(p[1:]))]
    return []


def _her_rec(a):
    key = _clean(a.get('CHAKEY')) or (str(a.get('PARCEL_KEY')).zfill(8) if a.get('PARCEL_KEY') is not None else None)
    return dict(address=_addr(a.get('SITUS_ADDRESS'), a.get('SITUS_CITY'), a.get('SITUS_ZIP5')),
                owner=_clean(a.get('OWNER_NAME')),
                pa_url=f'https://propsearch.hernandocountypa-florida.us/parcel/{key}' if key else None,
                norm_id=_clean(a.get('PARCEL_NUMBER')), alt_key=key, pa_acres=a.get('ACRES'))


def _her_sw(kind, v):
    if kind == 'alt':
        return ('alt', v.zfill(8))
    return (kind, v)


# ---- Lake
def _lake_norm(raw):
    q = _sq(raw)
    if q.isdigit() and len(q) <= 8:
        return [('alt', q.lstrip('0') or '0')]
    p = _parts(raw, (2, 2, 2, 4, 3, 5))
    return [('id', ''.join(p))] if p else []


def _lake_dsp(pid):
    p = _split(pid or '', (2, 2, 2, 4, 3, 5))
    return '-'.join(p) if p else pid


def _lake_rec(a):
    ak = _clean(a.get('AltKey'))
    return dict(address=_clean(a.get('PropertyAddress')), owner=_clean(a.get('OwnerName')),
                pa_url=f'https://www.lakecopropappr.com/property-details.aspx?AltKey={ak}' if ak else None,
                norm_id=_lake_dsp(_clean(a.get('ParcelNumber'))), alt_key=ak, pa_acres=a.get('Acres'))


def _lake_sw(kind, v):
    return ('id', _lake_dsp(v)) if kind == 'id' else (kind, v)


# ---- Marion
def _mar_norm(raw):
    s = str(raw).strip().upper()
    q = _sq(s)
    if q.isdigit() and len(q) <= 9 and not re.search(r'[-.+ ]', s):
        return [('alt', str(int(q)))]
    m = re.fullmatch(r'(\d+)\s*([-.+ ])\s*(\d+)\s*[-. ]\s*(\d+)', s)
    if m:
        a, sep, b, c = m.groups()
        sep = '+' if sep == '+' else '-'   # '+' marks a separate overlay parcel (e.g. '30367+002-00')
        for w in ((4, 3, 3), (5, 3, 2)):
            if len(a) == w[0] or len(c) == w[2]:
                p = _pad([a, b, c], w)
                if p:
                    return [('id', f'{p[0]}{sep}{p[1]}-{p[2]}')]
        p = _pad([a, b, c], (4, 3, 3))
        return [('id', f'{p[0]}{sep}{p[1]}-{p[2]}')] if p else []
    if q.isdigit() and len(q) == 10:
        return [('id', f'{q[:4]}-{q[4:7]}-{q[7:]}'), ('id', f'{q[:5]}-{q[5:8]}-{q[8:]}'),
                ('id', f'{q[:4]}+{q[4:7]}-{q[7:]}'), ('id', f'{q[:5]}+{q[5:8]}-{q[8:]}')]
    return []


def _mar_rec(a):
    ak = _int_str(a.get('ALT_Key'))
    return dict(address=_addr(a.get('SITUS_1')), owner=_clean(a.get('NAME')),
                pa_url=f'https://www.pa.marion.fl.us/PRC.aspx?key={ak}' if ak else None,
                norm_id=_clean(a.get('PARCEL')), alt_key=ak, pa_acres=a.get('ACRES'))


# ---- Alachua
def _ala_norm(raw):
    p = _parts(raw, (5, 3, 3))
    return [('id', '-'.join(p))] if p else []


def _ala_pa(pid):
    return ('https://qpublic.schneidercorp.com/Application.aspx?AppID=1081&LayerID=26490&PageTypeID=4&PageID=10770'
            f'&KeyValue={pid}') if pid else None


def _ala_rec(a):
    pid = _clean(a.get('parcel'))
    return dict(address=None, owner=_clean(a.get('firstName1')),  # Address1 is the MAILING address -> statewide fills situs
                pa_url=_clean(a.get('Link_ACPA')) or _ala_pa(pid), norm_id=pid, alt_key=None, pa_acres=a.get('acres'))


# ---- Citrus
_CIT_RE = re.compile(r'^(\d{1,2})E[-\s]*(\d{1,2})S[-\s]*(\d{1,2})(.*)$')


def _cit_norm(raw):
    s = str(raw).strip().upper()
    q = _sq(s)
    if q.isdigit() and len(q) <= 7:
        return [('alt', str(int(q)))]
    m = _CIT_RE.match(s)
    if not m:
        return []
    pre = f'{m.group(1).zfill(2)}E{m.group(2).zfill(2)}S{m.group(3).zfill(2)}'
    rest = _toks(m.group(4))
    return [('like', pre + '|' + ' '.join(rest))]


def _cit_dsp(pno):
    s = str(pno or '').upper()
    m = re.match(r'^(\d\d)E(\d\d)S(\d\d)(.*)$', s.strip())
    if not m:
        return _clean(s)
    rest = m.group(4)
    toks = [rest[0:4], rest[4:6], rest[6:11], rest[11:16]] if len(rest) > 4 else [rest]
    toks = [t.strip() for t in toks if t.strip()]
    return '-'.join([f'{m.group(1)}E', f'{m.group(2)}S', m.group(3)] + toks)


def _cit_pa(a):
    ak = _clean(a.get('ALT_KEY')) or _int_str(a.get('ALTKEY'))
    return f'https://www.citruspa.org/_web/datalets/datalet.aspx?mode=profileall&UseSearch=no&pin={ak}' if ak else None


def _cit_lots_rec(a):
    ak = _int_str(a.get('ALTKEY'))
    return dict(address=None, owner=None, pa_url=_cit_pa(a), norm_id=_cit_dsp(a.get('PARCELID')), alt_key=ak, pa_acres=None)


def _names(*xs):
    return _clean(' '.join(str(x).strip() for x in xs if x and str(x).strip()))


def _dashed(pid, widths):
    p = _split(_clean(pid) or '', widths)
    return '-'.join(p) if p else _clean(pid)


# ---- Orange (OCPA display 'SS-TT-RR-SSSS-BB-LLL' sec-twp-rng; county GIS PARCEL / OCPA URL = rng-twp-sec)
_ORA_W = (2, 2, 2, 4, 2, 3)


def _ora_norm(raw):
    p = _parts(raw, _ORA_W)
    if not p or not ''.join(p).isdigit():
        return []
    ok = lambda x: 1 <= int(x[0]) <= 36 and 19 <= int(x[1]) <= 25 and 26 <= int(x[2]) <= 34  # noqa: E731 (sec, twp, rng)
    a = p                           # as given: sec-twp-rng (OCPA display)
    b = [p[2], p[1], p[0]] + p[3:]  # input was rng-twp-sec (OCPA compact / URL form)
    order = [a, b] if len(_toks(raw)) > 1 else [b, a]  # undashed 15 digits: compact PA form first
    out = []
    for x in [x for x in order if ok(x)] or [a]:
        v = ''.join([x[2], x[1], x[0]] + x[3:])  # queried as rng-twp-sec
        if ('id', v) not in out:
            out.append(('id', v))
    return out


def _ora_dsp(pid):  # rng-twp-sec compact -> 'SS-TT-RR-SSSS-BB-LLL'
    p = _split(_clean(pid) or '', _ORA_W)
    return '-'.join([p[2], p[1], p[0]] + p[3:]) if p else _clean(pid)


def _ora_pa(pid):
    return f'https://ocpaweb.ocpafl.org/parcelsearch/Parcel%20ID/{pid}' if pid else None


def _ora_rec(a):
    pid = _clean(a.get('PARCEL'))
    return dict(address=_addr(a.get('SITUS'), a.get('SITUS_CITY'), a.get('SITUS_ZIP')),
                owner=_names(a.get('NAME1'), a.get('NAME2')), pa_url=_ora_pa(pid),
                norm_id=_clean(a.get('SEC_TWN_RNG')) or _ora_dsp(pid), alt_key=None, pa_acres=a.get('ACREAGE'))


def _ora_sw(kind, v):  # county PARCEL rng-twp-sec -> statewide PARCEL_ID sec-twp-rng
    return ('id', v[4:6] + v[2:4] + v[0:2] + v[6:]) if kind == 'id' and len(v) == 15 else None


def _ora_sw_compact(a):  # statewide PARCEL_ID sec-twp-rng -> rng-twp-sec
    v = _clean(a.get('PARCEL_ID')) or ''
    return v[4:6] + v[2:4] + v[0:2] + v[6:] if len(v) == 15 else None


# ---- Seminole ('SS-TT-RR-SSS-BBBB-LLLL', sec-twp-rng, alphanumeric sub/block/lot; layer stores 17 chars)
_SEM_W = (2, 2, 2, 3, 4, 4)


def _sem_norm(raw):
    return [('id', v) for v in _str_order_candidates(raw, _SEM_W, 29, 33, sep='')]


def _sem_pa(pid):
    return f'https://scpafl.org/search/parcels/details/?PID={pid}' if pid else None


def _sem_rec(a):
    pid = _clean(a.get('ParcelNumber'))
    return dict(address=_clean(a.get('PropertyAddress')), owner=_clean(a.get('OwnerName')), pa_url=_sem_pa(pid),
                norm_id=_clean(a.get('ParcelFormat')) or _dashed(pid, _SEM_W), alt_key=None, pa_acres=a.get('GISAcres'))


# ---- Osceola ('SS-TT-RR-SSSS-BBBB-LLLL', sec-twp-rng, alphanumeric block/lot; optional 'R' prefix)
_OSC_W = (2, 2, 2, 4, 4, 4)


def _osc_norm(raw):
    s = re.sub(r'^\s*R(?=[\s\-]*\d)', '', str(raw).upper())
    return [('id', v) for v in _str_order_candidates(s, _OSC_W, 26, 34, sep='')]


def _osc_pa(pid):
    return f'https://search.property-appraiser.org/Search/MainSearch?pin={pid}' if pid else None


def _osc_rec(a):
    pid = _clean(a.get('PARCELID'))
    return dict(address=_addr(a.get('Situs')), owner=_clean('; '.join(x.strip() for x in str(a.get('Owners') or '').split(';') if x.strip())),
                pa_url=_osc_pa(pid), norm_id=_clean(a.get('dsp_strap')) or _dashed(pid, _OSC_W), alt_key=None, pa_acres=None)


# ---- Volusia (short PID 'PPPP-SS-BB-LLLL' = twp digit + rng digit + sec, sub, block, lot; 7-digit altkey)
_VOL_W = (4, 2, 2, 4)


def _vol_norm(raw):
    s = str(raw).strip().upper()
    q = _sq(s)
    t = _toks(s)
    if q.isdigit() and len(q) <= 8 and len(t) == 1:
        return [('alt', str(int(q)))]
    if len(t) == 4 and len(t[0]) == 6:      # full PID '091730-03-00-0190'
        t = [t[0][:2], t[0][2:4], t[0][4:]] + t[1:]
    full = _pad(t, (2, 2, 2, 2, 2, 4)) if len(t) == 6 else (_split(q, (2, 2, 2, 2, 2, 4)) if len(t) == 1 else None)
    if full:                                # full PID sec-twp-rng (or DOR rng-twp-sec) -> short PID
        out = []                            # (land-grant 'sections' run past 36, e.g. '3740-00-00-0010')
        for sec, twp, rng in ((full[0], full[1], full[2]), (full[2], full[1], full[0])):
            if (sec + twp + rng).isdigit() and 1 <= int(sec) <= 99 and 12 <= int(twp) <= 21 and 27 <= int(rng) <= 36:
                v = twp[-1] + rng[-1] + sec + ''.join(full[3:])
                if ('id', v) not in out:
                    out.append(('id', v))
        return out or [('id', full[1][-1] + full[2][-1] + full[0] + ''.join(full[3:]))]
    p = _parts(s, _VOL_W)
    return [('id', ''.join(p))] if p else []


def _vol_pa(ak):
    return f'https://vcpa.vcgov.org/parcel/summary/?altkey={ak}' if ak else None


def _vol_rec(a):
    ak = _int_str(a.get('ALTKEY'))
    return dict(address=_addr(a.get('ADDRFULL'), a.get('CITYNAME'), a.get('ZIP1')), owner=_names(a.get('OWNER1'), a.get('OWNER2')),
                pa_url=_vol_pa(ak), norm_id=_dashed(a.get('PID'), _VOL_W), alt_key=ak, pa_acres=a.get('CALCACRES'))


# ---- Brevard (BCPAO 'TT-RR-SS-SUB-BLK-LOT' twp-rng-sec; layer stores 'TT RRSS-SUB-BLK-LOT', twp may be '20G';
#      long form '24-36-23-00-00001.0-0000.00' zero-pads block/lot; 7-digit tax account)
_BRE_RE = re.compile(r'^(\d{2}[A-Z]?)[\s-]*(\d{2})[\s-]*(\d{2})[\s-]+(\S.*)$')


def _bre_part(tok):
    """'00001.0' -> '1', '0000.00' -> '', '0004.03' -> '4.03', '0000B.0' -> 'B', '*' -> '*'."""
    m = re.fullmatch(r'([0-9A-Z]*?)(\.[0-9A-Z]*)?', tok)
    if not m:
        return tok
    head, frac = m.group(1).lstrip('0'), m.group(2) or ''
    if re.fullmatch(r'\.0*', frac):
        frac = ''
    return head + frac


def _bre_norm(raw):
    s = str(raw).strip().upper()
    q = _sq(s)
    if q.isdigit() and len(q) <= 8 and not re.search(r'[\s\-.*]', s):
        return [('alt', str(int(q)))]
    m = _BRE_RE.match(s)
    if not m:
        return []
    twp, rng, sec, rest = m.groups()
    toks = [x for x in re.split(r'[\s-]+', rest) if x]
    if not 2 <= len(toks) <= 3:
        return []
    head = f"{twp}{' ' if len(twp) == 2 else ''}{rng}{sec}-{toks[0].zfill(2)}-"
    out = []
    for parts in ([_bre_part(x) for x in toks[1:]], toks[1:]):  # zeros stripped first, then as typed
        if len(parts) == 2 and not parts[1]:
            parts = parts[:1]
        if not parts[0]:
            continue
        v = head + '-'.join(parts)
        if ('id', v) not in out:
            out.append(('id', v))
    return out


def _bre_dsp(pid):
    m = re.match(r'^(\d\d[A-Z]?)\s*(\d\d)(\d\d)-(.*)$', str(pid or '').strip())
    return f'{m.group(1)}-{m.group(2)}-{m.group(3)}-{m.group(4)}' if m else _clean(pid)


def _bre_pa(acct):
    return f'https://www.bcpao.us/PropertySearch/#/account/{acct}' if acct else None


def _bre_rec(a):
    acct = _int_str(a.get('TaxAcct'))
    st = _clean(a.get('STREET_NAME'))
    street = None if not st or st == 'UNKNOWN' else _names(a.get('STREET_NUMBER'), a.get('STREET_DIRECTION_PREFIX'), st,
                                                           a.get('STREET_TYPE'))
    return dict(address=_addr(street, a.get('CITY'), a.get('ZIP_CODE')), owner=_names(a.get('OWNER_NAME1'), a.get('OWNER_NAME2')),
                pa_url=_bre_pa(acct), norm_id=_bre_dsp(a.get('PARCEL_ID')), alt_key=acct, pa_acres=a.get('ACRES'))


COUNTIES = {}


def _reg(c):
    COUNTIES[c.name.lower()] = c


_reg(County('Hillsborough', 39, _hil_norm, [
    Src('Hillsborough County HC_Parcels',
        'https://maps.hillsboroughcounty.org/arcgis/rest/services/InfoLayers/HC_Parcels/MapServer/0',
        {'id': 'FOLIO', 'strap': 'STRAP'}, _hil_rec,
        'FOLIO,PIN,STRAP,OWNER,SITE_ADDR,SITE_CITY,SITE_ZIP,ACREAGE'),
    Src('HCPA WebParcels', 'https://gis.hcpafl.org/arcgis/rest/services/Webmaps/HillsboroughFL_WebParcels/MapServer/0',
        {'id': 'folio', 'strap': 'strap'}, _hcpa_rec,
        'folio,strap,DisplayStrap,Owner1,FullAddress,SiteZip'),
    _sw(39, pa_url=lambda a: _hil_pa(_clean(a.get('ALT_KEY'))), norm=lambda a: _clean(a.get('ALT_KEY'))),
], sw_map=lambda k, v: {'id': ('alt', v), 'strap': ('id', v)}.get(k)))

_reg(County('Pasco', 61, _pasco_norm, [
    Src('Pasco PA Parcels', 'https://maps.pascopa.com/arcgis/rest/services/Parcels/MapServer/4',
        {'id': 'ParcelID'}, _pasco_rec,
        'ParcelID,PHYS_STREET,PHYS_CITY,PHYS_ZIP,NAD_NAME_1,NAD_NAME_2,VAL_ACRES'),
    _sw(61, pa_url=lambda a: f"https://search.pascopa.com/parcel.aspx?parcel={_pasco_strap(a.get('PARCEL_ID') or '')}"),
], sw_map=lambda k, v: (k, v)))

_reg(County('Pinellas', 62, _pin_norm, [
    Src('Pinellas County Parcels', 'https://egis.pinellas.gov/gis/rest/services/PublicWebGIS/Parcels/MapServer/1',
        {'id': 'PARCELID'}, _pin_rec,
        'PARCELID,STRAP,SITE_ADDRESS,SITE_CITY,SITE_ZIP,OWNER1,OWNER2,Acres'),
    _sw(62, norm=lambda a: '-'.join(_toks(a.get('PARCEL_ID'))[i] for i in (2, 1, 0, 3, 4, 5))
        if len(_toks(a.get('PARCEL_ID'))) == 6 else _clean(a.get('PARCEL_ID')),
        pa_url=lambda a: f"https://www.pcpao.gov/property-details?s={_sq(a.get('PARCEL_ID'))}"),
], sw_map=_pin_sw))

_reg(County('Manatee', 51, _man_norm, [
    Src('Manatee PAO Parcel Search', 'https://gis.manateepao.gov/arcgis/rest/services/Website/WebLayers/MapServer/0',
        {'id': 'PARID'}, _man_rec,
        'PARID,SITUS_ADDRESS,SITUS_POSTAL_CITY,SITUS_POSTAL_ZIP,PAR_OWNER_NAME1,LAND_ACREAGE_CAMA'),
    _sw(51, pa_url=lambda a: f"https://www.manateepao.gov/parcel/?parid={_clean(a.get('PARCEL_ID'))}"),
], sw_map=lambda k, v: (k, v)))

_reg(County('Polk', 63, _polk_norm, [
    Src('Polk PA PARCELS (proxy)',
        'https://www.polkflpa.gov/proxy.ashx?https://gissrvr/arcgis/rest/services/WebSite/WebSiteQueryNew/MapServer/0',
        {'id': 'PARCELID'}, _polk_rec, 'PARCELID,OWNER1,PROP_LOCATION,TOT_ACREAGE', batch=50),
    _sw(63, pa_url=lambda a: f"https://www.polkflpa.gov/CamaDisplay.aspx?OutputMode=Display&SearchType=RealEstate&ParcelID={_clean(a.get('PARCEL_ID'))}"),
], sw_map=lambda k, v: (k, v)))

_reg(County('Sarasota', 68, _sar_norm, [
    Src('Sarasota County ParcelProperty', 'https://ags3.scgov.net/server/rest/services/Hosted/ParcelProperty/FeatureServer/0',
        {'id': 'account'}, _sar_rec, 'account,fulladdress,name1,measuredacreage'),
    _sw(68, pa_url=lambda a: f"https://www.sc-pa.com/propertysearch/parcel/details/{_clean(a.get('PARCEL_ID'))}"),
], sw_map=lambda k, v: (k, v)))

_reg(County('Charlotte', 18, _cha_norm, [
    Src('Charlotte County Property Ownership',
        'https://agis.charlottecountyfl.gov/arcgis/rest/services/Essentials/CCGIS_Web_Layers2022/MapServer/17',
        {'id': 'ACCOUNT'}, _cha_rec, 'ACCOUNT,ownersname,FullPropertyAddress'),
    _sw(18, pa_url=lambda a: f"https://www.ccappraiser.com/Show_parcel.asp?acct={_clean(a.get('PARCEL_ID'))}&gen=T&tax=T&bld=T&oth=T&sal=T&lnd=T&leg=T"),
], sw_map=lambda k, v: (k, v)))

_reg(County('Hernando', 37, _her_norm, [
    Src('Hernando PA Parcels', 'https://services2.arcgis.com/x5zvhhxfUuRDntRe/arcgis/rest/services/Parcels/FeatureServer/0',
        {'id': 'PARCEL_NUMBER', 'alt': 'PARCEL_KEY', 'like': 'PARCEL_NUMBER'}, _her_rec,
        'PARCEL_KEY,CHAKEY,PARCEL_NUMBER,SITUS_ADDRESS,SITUS_CITY,SITUS_ZIP5,OWNER_NAME,ACRES', numeric=('alt',)),
    _sw(37, pa_url=lambda a: f"https://propsearch.hernandocountypa-florida.us/parcel/{_clean(a.get('ALT_KEY'))}",
        canon={'alt': lambda v: _int_str(v)}),
], sw_map=_her_sw))

_reg(County('Lake', 45, _lake_norm, [
    Src('Lake County Tax Parcels', 'https://gis.lakecountyfl.gov/lakegis/rest/services/OpenData/OpenData1/MapServer/12',
        {'id': 'ParcelNumber', 'alt': 'AltKey'}, _lake_rec, 'AltKey,ParcelNumber,PropertyAddress,OwnerName,Acres',
        style='in'),
    _sw(45, pa_url=lambda a: f"https://www.lakecopropappr.com/property-details.aspx?AltKey={_clean(a.get('ALT_KEY'))}",
        canon={'id': lambda v: _sq(v)}),
], sw_map=_lake_sw))

_reg(County('Marion', 52, _mar_norm, [
    Src('Marion County Parcels', 'https://gis.marionfl.org/public/rest/services/General/Parcels/MapServer/0',
        {'id': 'PARCEL', 'alt': 'ALT_Key'}, _mar_rec, 'PARCEL,ALT_Key,SITUS_1,NAME,ACRES', numeric=('alt',)),
    _sw(52, pa_url=lambda a: f"https://www.pa.marion.fl.us/PRC.aspx?key={_clean(a.get('ALT_KEY'))}"),
], sw_map=lambda k, v: (k, v)))

_reg(County('Alachua', 11, _ala_norm, [
    Src('Alachua County Parcels35', 'https://services1.arcgis.com/MiBZ4u97DWldovjI/ArcGIS/rest/services/Parcels35/FeatureServer/0',
        {'id': 'parcel'}, _ala_rec, 'parcel,firstName1,acres,Link_ACPA'),
    _sw(11, pa_url=lambda a: _ala_pa(_clean(a.get('PARCEL_ID')))),
], sw_map=lambda k, v: (k, v)))

_reg(County('Citrus', 19, _cit_norm, [
    Src('Citrus County LandDevelopment Lots', 'https://maps.citrusbocc.com/server/rest/services/PublicData/LandDevelopment/MapServer/0',
        {'alt': 'ALTKEY'}, _cit_lots_rec, 'ALTKEY,PARCELID', numeric=('alt',), batch=25),
    _sw(19, pa_url=_cit_pa, norm=lambda a: _cit_dsp(a.get('PARCEL_ID'))),
], sw_map=lambda k, v: (k, v)))
# the Citrus statewide source also answers the fixed-width parcel-number ('like') candidates
COUNTIES['citrus'].sources[1].fields['like'] = 'PARCEL_ID'

_ORA_OUT = 'PARCEL,SEC_TWN_RNG,NAME1,NAME2,SITUS,SITUS_CITY,SITUS_ZIP,ACREAGE'
_reg(County('Orange', 58, _ora_norm, [
    Src('Orange County Parcels (AGOL_Open_Data)', 'https://ocgis4.ocfl.net/arcgis/rest/services/AGOL_Open_Data/MapServer/56',
        {'id': 'PARCEL'}, _ora_rec, _ORA_OUT),
    Src('OCPA Parcels_Poly', 'https://vgispublic.ocpafl.org/server/rest/services/DYNAMIC/Dynamic_Parcels/MapServer/3',
        {'id': 'PARCEL'}, _ora_rec, _ORA_OUT),
    _sw(58, pa_url=lambda a: _ora_pa(_ora_sw_compact(a)), norm=lambda a: _dashed(a.get('PARCEL_ID'), _ORA_W),
        alt=lambda a: None),
], sw_map=_ora_sw))

_reg(County('Seminole', 69, _sem_norm, [
    Src('Seminole County Property Details (InformationKiosk)',
        'https://utility.arcgis.com/usrsvcs/servers/9b9c9fd45bdc4c39a2bd518da39d1e1c/rest/services/InformationKiosk/MapServer/1',
        {'id': 'ParcelNumber'}, _sem_rec, 'ParcelNumber,ParcelFormat,OwnerName,PropertyAddress,GISAcres'),
    _sw(69, pa_url=lambda a: _sem_pa(_clean(a.get('PARCEL_ID'))), norm=lambda a: _dashed(a.get('PARCEL_ID'), _SEM_W),
        alt=lambda a: None),
], sw_map=lambda k, v: (k, v)))

_reg(County('Osceola', 59, _osc_norm, [
    Src('Osceola PA FabricParcel', 'https://paags.property-appraiser.org/server/rest/services/FabricParcel/MapServer/0',
        {'id': 'PARCELID'}, _osc_rec, 'PARCELID,dsp_strap,Owners,Situs'),
    _sw(59, pa_url=lambda a: _osc_pa(_clean(a.get('PARCEL_ID'))), norm=lambda a: _dashed(a.get('PARCEL_ID'), _OSC_W),
        alt=lambda a: None),
], sw_map=lambda k, v: (k, v)))

_reg(County('Volusia', 74, _vol_norm, [
    Src('Volusia County Parcel Ownership (Open_Data_3)',
        'https://maps5.vcgov.org/arcgis/rest/services/Open_Data/Open_Data_3/FeatureServer/34',
        {'id': 'PID', 'alt': 'ALTKEY'}, _vol_rec, 'ALTKEY,PID,ADDRFULL,CITYNAME,ZIP1,OWNER1,OWNER2,CALCACRES',
        numeric=('alt',)),
    _sw(74, pa_url=lambda a: _vol_pa(_int_str(a.get('ALT_KEY'))), norm=lambda a: _dashed(a.get('PARCEL_ID'), _VOL_W),
        alt=lambda a: _int_str(a.get('ALT_KEY')), canon={'alt': lambda v: _int_str(v)}),
], sw_map=lambda k, v: (k, v)))

_reg(County('Brevard', 15, _bre_norm, [
    Src('Brevard County Parcel Property', 'https://gis.brevardfl.gov/gissrv/rest/services/Base_Map/Parcel_New_WKID2881/MapServer/5',
        {'id': 'PARCEL_ID', 'alt': 'TaxAcct'}, _bre_rec,
        'TaxAcct,PARCEL_ID,OWNER_NAME1,OWNER_NAME2,STREET_NUMBER,STREET_DIRECTION_PREFIX,STREET_NAME,STREET_TYPE,CITY,ZIP_CODE,ACRES',
        numeric=('alt',)),
    _sw(15, pa_url=lambda a: _bre_pa(_int_str(a.get('ALT_KEY'))), norm=lambda a: _bre_dsp(a.get('PARCEL_ID')),
        alt=lambda a: _int_str(a.get('ALT_KEY')), canon={'alt': lambda v: _int_str(v)}),
], sw_map=lambda k, v: (k, v)))


# ---------------------------------------------------------------- core
def _canon(src, kind, v):
    if v is None:
        return None
    if kind in src.canon:
        return src.canon[kind](v)
    if kind in src.numeric:
        return _int_str(v)
    return str(v).strip().upper()


def _fetch_eq(src, kind, values):
    """Batch OR'd equality query. Returns {canonical value: [features]}."""
    field = src.fields[kind]
    out = {}
    values = list(dict.fromkeys(values))
    tries = 1 if kind in src.slow else 3
    for i in range(0, len(values), src.batch):
        chunk = values[i:i + src.batch]
        if kind in src.numeric:
            lits = [str(int(v)) for v in chunk]
        else:
            lits = ["'{}'".format(v.replace("'", "''")) for v in chunk]
        feats = None
        for style in (src.style, 'in' if src.style == 'or' else 'or'):
            if style == 'or':
                where = '(' + ' OR '.join(f'{field}={x}' for x in lits) + ')'
            else:
                where = f"{field} IN ({','.join(lits)})"
            if src.where:
                where = f'{src.where} AND {where}'
            try:
                feats = _query(src.url, dict(where=where, outFields=src.out_fields, returnGeometry='true', outSR=4326),
                               method=src.method, tries=tries)
                break
            except Exception as e:  # noqa: BLE001
                if len(chunk) == 1 or style != src.style:
                    raise
                _log(f'{src.name}: {style.upper()} batch failed ({e}); retrying with the other clause style')
        if src.style != style:
            src.style = style  # remember what works for this layer
        for f in feats:
            k = _canon(src, kind, f['attributes'].get(field))
            out.setdefault(k, []).append(f)
    return out


def _fetch_like(src, pattern, county_name):
    """Wildcard lookups (Hernando '_' twp digit, Citrus fixed-width). Returns matching features."""
    field = src.fields['like']
    if county_name == 'Citrus':
        pre, rest = pattern.split('|')
        toks = rest.split()
        want = pre + ''.join(toks)
        if len(toks) == 1 and len(toks[0]) > 5:
            # remainder given without separators: try the field splits (sub 4 / block 5 / lot 4) it could be
            t = toks[0]
            splits = [w for w in ((4, 4), (4, 5), (5, 4), (4, 5, 4)) if sum(w) == len(t)]
            likes = [pre + '%' + '%'.join(_split(t, w)) + '%' for w in splits] or [pre + '%']
        else:
            likes = [pre + '%' + '%'.join(toks) + '%']
        pred = lambda v: _sq(v) == want  # noqa: E731
    else:
        likes = [pattern]
        pred = lambda v: True  # noqa: E731
    feats = []
    for like in likes:
        where = f"{field} LIKE '{like}'"
        if src.where:
            where = f'{src.where} AND {where}'
        fs = _query(src.url, dict(where=where, outFields=src.out_fields, returnGeometry='true', outSR=4326),
                    method=src.method)
        feats += [f for f in fs if pred(f['attributes'].get(field))]
        if feats:
            break
    ids = {_canon(src, 'id', f['attributes'].get(src.fields.get('id', field))) for f in feats}
    return (feats if len(ids) == 1 else []), len(ids)


def _build(src, feats):
    a = feats[0]['attributes']
    r = src.rec(a)
    g = _geom_from_feats(feats)
    pa_ac = r.pop('pa_acres', None)
    if g:
        acres, lat, lon = g
        if not acres and pa_ac:
            acres = float(pa_ac)
    else:
        acres, lat, lon = (float(pa_ac) if pa_ac else None), None, None
    r.update(lat=lat, lon=lon, acres=acres, source=src.name)
    if r.get('alt_key') is None:
        r.pop('alt_key', None)
    return r


def _cache_load(path):
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception as e:  # noqa: BLE001
        _log('cache unreadable, ignoring:', e)
        return {}


def _cache_save(path, cache):
    if not path:
        return
    try:
        tmp = path + '.tmp'
        with open(tmp, 'w') as fh:
            json.dump(cache, fh)
        os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001
        _log('cache write failed:', e)


def _county(name):
    k = re.sub(r'\s+county$', '', str(name or '').strip(), flags=re.I).lower()
    if k not in COUNTIES:
        raise ValueError(f'unsupported county: {name!r} (supported: {", ".join(c.name for c in COUNTIES.values())})')
    return COUNTIES[k]


def lookup(county: str, parcel_ids: list) -> dict:
    """county is one of: Hillsborough, Pasco, Pinellas, Manatee, Polk, Sarasota, Charlotte,
    Hernando, Lake, Marion, Citrus, Alachua, Orange, Seminole, Osceola, Volusia, Brevard
    ("X County" also accepted). parcel_ids are in whatever format a county planning
    portal shows them (dashes, dots, spaces vary). Returns {input_id: {"lat": float, "lon": float,
    "acres": float, "address": str|None, "owner": str|None, "pa_url": str|None, "norm_id": str}}
    for every id that was found (plus "alt_key", "source", and "ambiguous" when set).
    lat/lon = parcel polygon centroid in WGS84 (exact, never geocoded), acres = GIS polygon area
    (or PA acreage if no geometry area)."""
    c = _county(county)
    cpath = os.environ.get('RZ_PARCEL_CACHE')
    cache = _cache_load(cpath)
    for k, v in _MEMO.items():
        if k not in cache or cache[k].get('t', 0) < v.get('t', 0):
            cache[k] = v
    now = time.time()
    results, pending = {}, {}
    for raw in dict.fromkeys(str(p) for p in (parcel_ids or []) if p is not None and str(p).strip()):
        key = f'{c.name}|{raw.strip().upper()}'
        ent = cache.get(key)
        if ent and now - ent.get('t', 0) < (HIT_TTL if ent.get('r') else MISS_TTL):
            if ent.get('r'):
                results[raw] = ent['r']
            continue
        cands = c.normalize(raw)
        if not cands:
            _log(f'{c.name}: cannot parse parcel id {raw!r}')
            continue
        pending[raw] = cands

    partial = {}  # raw -> record without geometry (kept if nothing better turns up)
    for si, src in enumerate(c.sources):
        if not pending:
            break
        is_sw = src.url == SW_URL and c.sw_map is not None
        # translate candidates for this source
        tr = {}
        for raw, cands in pending.items():
            lst = []
            for kind, v in cands:
                if is_sw:
                    m = c.sw_map(kind, v)
                    if not m:
                        continue
                    kind, v = m
                if kind in src.fields:
                    lst.append((kind, v))
            tr[raw] = lst
        found = {}
        by_kind = {}
        for raw, lst in tr.items():
            for kind, v in lst:
                if kind != 'like':
                    by_kind.setdefault(kind, set()).add(v)
        hits = {}
        for kind, vals in by_kind.items():
            try:
                hits[kind] = _fetch_eq(src, kind, sorted(vals))
            except Exception as e:  # noqa: BLE001
                _log(f'{c.name}: {src.name} {kind} query failed: {e}')
        for raw, lst in tr.items():
            matches = []
            for kind, v in lst:
                if kind == 'like':
                    try:
                        feats, n = _fetch_like(src, v, c.name)
                    except Exception as e:  # noqa: BLE001
                        _log(f'{c.name}: {src.name} wildcard query failed for {raw!r}: {e}')
                        continue
                    if n > 1:
                        _log(f'{c.name}: {raw!r} matches {n} parcels, skipped')
                else:
                    feats = hits.get(kind, {}).get(_canon(src, kind, v))
                if feats:
                    matches.append(feats)
            if matches:
                rec = _build(src, matches[0])
                if len({_build(src, m)['norm_id'] for m in matches}) > 1:
                    rec['ambiguous'] = True
                found[raw] = rec
        for raw, rec in found.items():
            if rec.get('lat') is None:
                partial.setdefault(raw, rec)
                continue
            if raw in partial:  # keep the richer attributes from the first source
                base = partial.pop(raw)
                for k in ('address', 'owner', 'pa_url', 'norm_id', 'alt_key'):
                    if base.get(k) and not rec.get(k):
                        rec[k] = base[k]
            results[raw] = rec
            pending.pop(raw, None)

    for raw, rec in partial.items():
        results[raw] = rec
        pending.pop(raw, None)

    _fill_from_statewide(c, results)

    for raw in list(pending):
        _log(f'{c.name}: parcel not found: {raw!r}')
        cache[f'{c.name}|{raw.strip().upper()}'] = {'t': now, 'r': None}
    for raw, rec in results.items():
        cache[f'{c.name}|{raw.strip().upper()}'] = {'t': now, 'r': rec}
    _MEMO.update(cache)
    _cache_save(cpath, cache)
    return results


def _fill_from_statewide(c, results):
    """Fill missing address/owner from the statewide layer (e.g. Alachua layer has no situs)."""
    need = {raw: r for raw, r in results.items()
            if (not r.get('address') or not r.get('owner')) and r.get('source') != 'FL Statewide Cadastral'}
    if not need or c.sw_map is None:
        return
    sw = next((s for s in c.sources if s.url == SW_URL), None)
    if not sw:
        return
    if c.name == 'Citrus':
        # Citrus lots-layer hits: statewide ALT_KEY is unindexed (~45 s), so find the statewide parcel
        # polygon containing our centroid (spatial index) and accept it only if its ALT_KEY matches.
        for raw, r in need.items():
            if r.get('lat') is None or not r.get('alt_key'):
                continue
            try:
                fs = _query(sw.url, dict(where=sw.where, geometry=f"{r['lon']},{r['lat']}", geometryType='esriGeometryPoint',
                                         inSR=4326, spatialRel='esriSpatialRelIntersects', outFields=sw.out_fields,
                                         returnGeometry='false'))
            except Exception as e:  # noqa: BLE001
                _log(f'Citrus: statewide point query failed: {e}')
                continue
            for f in fs:
                if _int_str(f['attributes'].get('ALT_KEY')) == _int_str(r['alt_key']):
                    s = sw.rec(f['attributes'])
                    for k in ('address', 'owner', 'norm_id'):
                        if s.get(k) and (k == 'norm_id' or not r.get(k)):
                            r[k] = s[k]
        return
    vals = {}
    for raw, r in need.items():
        cands = c.normalize(r.get('norm_id') or '') or []
        for kind, v in cands:
            m = c.sw_map(kind, v)
            if m and m[0] in sw.fields:
                vals[raw] = m
                break
    try:
        by_kind = {}
        for raw, (k, v) in vals.items():
            by_kind.setdefault(k, set()).add(v)
        hits = {k: _fetch_eq(sw, k, sorted(vs)) for k, vs in by_kind.items()}
    except Exception as e:  # noqa: BLE001
        _log(f'{c.name}: statewide fill failed: {e}')
        return
    for raw, (k, v) in vals.items():
        feats = hits.get(k, {}).get(_canon(sw, k, v))
        if not feats:
            continue
        s = sw.rec(feats[0]['attributes'])
        r = results[raw]
        if not r.get('address') and s.get('address'):
            r['address'] = s['address']
        if not r.get('owner') and s.get('owner'):
            r['owner'] = s['owner']


def site(county, parcel_ids):
    """Combine several parcels of one application: {"lat","lon"} = area-weighted centroid of all
    found parcels, "acres" = sum, "found": n, "missing": [ids]."""
    ids = [str(p) for p in (parcel_ids or []) if p is not None and str(p).strip()]
    if not ids:
        return None
    res = lookup(county, ids)
    found = [res[i] for i in dict.fromkeys(ids) if i in res]
    missing = [i for i in dict.fromkeys(ids) if i not in res]
    if not found:
        return None
    acres = sum(r.get('acres') or 0 for r in found)
    pts = [(r['lat'], r['lon'], r.get('acres') or 0) for r in found if r.get('lat') is not None]
    lat = lon = None
    if pts:
        w = sum(p[2] for p in pts)
        if w > 0:
            lat = sum(p[0] * p[2] for p in pts) / w
            lon = sum(p[1] * p[2] for p in pts) / w
        else:
            lat = sum(p[0] for p in pts) / len(pts)
            lon = sum(p[1] for p in pts) / len(pts)
        lat, lon = round(lat, 7), round(lon, 7)
    return {'lat': lat, 'lon': lon, 'acres': round(acres, 4), 'found': len(found), 'missing': missing}


if __name__ == '__main__':
    if len(sys.argv) < 3:
        print('usage: parcels.py COUNTY ID [ID ...]', file=sys.stderr)
        sys.exit(2)
    out = lookup(sys.argv[1], sys.argv[2:])
    print(json.dumps({'parcels': out, 'site': site(sys.argv[1], sys.argv[2:])}, indent=2))
