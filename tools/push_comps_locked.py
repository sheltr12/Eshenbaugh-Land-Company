#!/usr/bin/env python3
"""
push_comps_locked.py - concurrency-safe push of new comps to the ELC comps database.

Usage:
  python3 push_comps_locked.py <new_comps.json> --label "Osceola" [--file local.html=repo/path.html ...] [--dry-run]

Why this exists:
  Several county tasks can run at the same time. Each one must build on the newest
  Test-patch-N and create Test-patch-(N+1) without two runs claiming the same N.

How it stays safe:
  1. Find the newest Test-patch-N (never branch from Test).
  2. Read comps.json from it and merge, skipping duplicates (parcel_id + sale_date_iso,
     OR Book/Page, or clerk instrument #).
  3. Commit on top of Test-patch-N (base_tree keeps every existing file).
  4. Create refs/heads/Test-patch-(N+1). GitHub refuses to create a ref that already
     exists, so if another run got there first we get HTTP 422, wait, and redo steps 1-4
     on top of the newer branch. Two runs can never both create the same N, and the
     second run always includes the first run's comps.
  5. Check file parity with the base branch.
  6. Point GitHub Pages at our branch only if it is still the newest Test-patch (a newer
     one already contains our records), request a build, wait, and verify the live
     comps.json contains our records.

Token: $GITHUB_TOKEN, else ~/Documents/Claude/.github_token, else the .github_token
next to this scripts folder.
"""
import json, os, re, sys, time, random, base64, argparse, urllib.request, urllib.error

REPO = 'sheltr12/Eshenbaugh-Land-Company'
API = 'https://api.github.com'
LIVE = 'https://sheltr12.github.io/Eshenbaugh-Land-Company/comps.json'
HERE = os.path.dirname(os.path.abspath(__file__))


def load_token():
    if os.environ.get('GITHUB_TOKEN'):
        return os.environ['GITHUB_TOKEN'].strip()
    for p in [os.environ.get('GITHUB_TOKEN_FILE'), os.path.expanduser('~/Documents/Claude/.github_token'),
              os.path.join(HERE, '..', '.github_token')]:
        if p and os.path.exists(p):
            return open(p).read().strip()
    sys.exit('No GitHub token found (GITHUB_TOKEN env or Documents/Claude/.github_token).')


TK = None


def gh(method, path, data=None, ok404=False):
    req = urllib.request.Request(path if path.startswith('http') else API + path,
                                 data=json.dumps(data).encode() if data is not None else None, method=method,
                                 headers={'Authorization': f'token {TK}', 'Accept': 'application/vnd.github+json',
                                          'Content-Type': 'application/json', 'User-Agent': 'elc-comps-bot'})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            body = r.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        msg = e.read().decode(errors='replace')
        if ok404 and e.code == 404:
            return None
        raise RuntimeError(f'{method} {path} -> {e.code}: {msg[:400]}') from None


def patch_branches():
    out, page = {}, 1
    while True:
        b = gh('GET', f'/repos/{REPO}/branches?per_page=100&page={page}')
        for x in b:
            m = re.fullmatch(r'Test-patch-(\d+)', x['name'])
            if m:
                out[int(m.group(1))] = x['commit']['sha']
        if len(b) < 100:
            return out
        page += 1


np_ = lambda s: re.sub(r'[\s\-]', '', str(s or '').upper())


def nbp(s):
    m = re.search(r'(\d{3,6})\s*/\s*(\d{1,6})', str(s or ''))
    return f'{int(m.group(1))}/{int(m.group(2))}' if m else None


def keys_of(c):
    k = {('pd', np_(c.get('parcel_id')), str(c.get('sale_date_iso') or '')[:10])}
    b = nbp(c.get('book_page'))
    if b:
        k.add(('bp', (c.get('county') or '').lower(), b))
    for i in re.findall(r'Instr #?(\d{8,12})', str(c.get('comments') or '')):
        k.add(('in', (c.get('county') or '').lower(), i))
    return k


def read_comps(sha):
    req = urllib.request.Request(f'https://raw.githubusercontent.com/{REPO}/{sha}/comps.json',
                                 headers={'Authorization': f'token {TK}', 'User-Agent': 'elc-comps-bot'})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())


def main():
    global TK
    ap = argparse.ArgumentParser()
    ap.add_argument('pending')
    ap.add_argument('--label', default='county')
    ap.add_argument('--file', action='append', default=[], help='local_path=repo/path')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    TK = load_token()
    cands = json.load(open(a.pending))
    extra = [f.split('=', 1) for f in a.file]

    for attempt in range(1, 13):
        pb = patch_branches()
        if not pb:
            sys.exit('No Test-patch-N branch found; refusing to branch from Test.')
        n = max(pb)
        base, base_sha = f'Test-patch-{n}', pb[n]
        db = read_comps(base_sha)
        comps = db['comps'] if isinstance(db, dict) else db
        existing = set()
        for c in comps:
            existing |= keys_of(c)
        add, dups = [], []
        for c in cands:
            c = dict(c)
            if keys_of(c) & existing:
                dups.append(c.get('parcel_id'))
                continue
            existing |= keys_of(c)
            add.append(c)
        print(f'[attempt {attempt}] base {base}: candidates {len(cands)} | duplicates skipped {len(dups)} | new {len(add)}')
        if not add:
            print('RESULT: No new comps (all already in database). Nothing pushed.')
            return
        nxt = max([int(c.get('num') or 0) for c in comps] + [0]) + 1
        for c in add:
            c['num'] = nxt
            nxt += 1
            if not c.get('price_per_acre') and c.get('price') and c.get('acreage'):
                c['price_per_acre'] = round(float(c['price']) / float(c['acreage']))
        comps.extend(add)
        if isinstance(db, dict):
            db['comps'] = comps
            db.setdefault('meta', {})['generated'] = time.strftime('%Y-%m-%d')
            db['meta']['total_comps'] = len(comps)
        if a.dry_run:
            print(f'DRY RUN: would create Test-patch-{n + 1} on {base} with {len(comps)} comps')
            return
        base_tree = gh('GET', f'/repos/{REPO}/git/commits/{base_sha}')['tree']['sha']
        items = [{'path': 'comps.json', 'mode': '100644', 'type': 'blob',
                  'sha': gh('POST', f'/repos/{REPO}/git/blobs', {'content': json.dumps(db, separators=(',', ':')), 'encoding': 'utf-8'})['sha']}]
        for local, repo_path in extra:
            items.append({'path': repo_path, 'mode': '100644', 'type': 'blob',
                          'sha': gh('POST', f'/repos/{REPO}/git/blobs', {'content': base64.b64encode(open(local, 'rb').read()).decode(), 'encoding': 'base64'})['sha']})
        tree = gh('POST', f'/repos/{REPO}/git/trees', {'base_tree': base_tree, 'tree': items})['sha']
        commit = gh('POST', f'/repos/{REPO}/git/commits', {'message': f'Add {a.label} vacant land comps: {len(add)} new records (from {base})',
                                                           'tree': tree, 'parents': [base_sha]})['sha']
        new_branch = f'Test-patch-{n + 1}'
        try:
            gh('POST', f'/repos/{REPO}/git/refs', {'ref': f'refs/heads/{new_branch}', 'sha': commit})
        except RuntimeError as e:
            if '422' in str(e):
                wait = random.randint(20, 60)
                print(f'{new_branch} was just created by another run; retrying on top of it in {wait}s')
                time.sleep(wait)
                continue
            raise
        print(f'Created {new_branch} on {base} ({commit[:7]}), +{len(add)} comps, total {len(comps)}')
        break
    else:
        sys.exit('Gave up after 12 attempts; nothing pushed.')

    old = {t['path'] for t in gh('GET', f'/repos/{REPO}/git/trees/{base_sha}?recursive=1')['tree']}
    new = {t['path'] for t in gh('GET', f'/repos/{REPO}/git/trees/{commit}?recursive=1')['tree']}
    missing = sorted(old - new)
    print('File parity:', 'OK' if not missing else f'MISSING {missing[:20]}')

    newest = max(patch_branches())
    if newest == n + 1:
        cur = gh('GET', f'/repos/{REPO}/pages')
        if cur.get('source', {}).get('branch') != new_branch:
            gh('PUT', f'/repos/{REPO}/pages', {'source': {'branch': new_branch, 'path': '/'}})
        print(f'Pages source -> {new_branch}')
    else:
        print(f'Test-patch-{newest} already exists on top of {new_branch}; leaving Pages on the newest branch.')
    gh('POST', f'/repos/{REPO}/pages/builds')
    for _ in range(40):
        time.sleep(15)
        st = gh('GET', f'/repos/{REPO}/pages/builds/latest')
        if st.get('status') in ('built', 'errored'):
            print('Pages build:', st.get('status'))
            break
    want = {(np_(c['parcel_id']), c['sale_date_iso']) for c in add}
    for _ in range(12):
        try:
            with urllib.request.urlopen(urllib.request.Request(f'{LIVE}?cb={int(time.time())}', headers={'Cache-Control': 'no-cache'}), timeout=120) as r:
                live = json.loads(r.read())
            lc = live['comps'] if isinstance(live, dict) else live
            have = {(np_(c.get('parcel_id')), c.get('sale_date_iso')) for c in lc if c.get('county') and c.get('county_fips') and c.get('date_added')}
            if want <= have:
                print(f'LIVE VERIFIED: {len(lc)} comps; all {len(want)} new records present with county, county_fips, date_added.')
                break
        except Exception as e:
            print('live check error:', e)
        time.sleep(20)
    else:
        print('LIVE NOT YET UPDATED: records pushed but not visible on the live site yet (CDN cache). Re-check in a few minutes.')
    print(f'NEW_BRANCH={new_branch}')


if __name__ == '__main__':
    main()
