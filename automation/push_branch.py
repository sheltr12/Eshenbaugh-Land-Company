#!/usr/bin/env python3
"""
push_branch.py — GitHub Actions version of the weekly comps push.

Merges a pending comps JSON into comps.json on top of the LATEST Test-patch-N
branch and creates Test-patch-N+1 (never branches from `Test` or `main`).
Also adds any extra files given as path=repo/path pairs (e.g. the week's map).

  python3 push_branch.py --county Pasco --pending pasco-2026-10-05.json \
      --add pasco-comps-map-2026-10-05.html=pasco-comps-map-2026-10-05.html

Auth: GITHUB_TOKEN env (the workflow's built-in token). Writes outputs to
$GITHUB_OUTPUT: new_branch, base_branch, added, skipped, total.
"""
import argparse, json, os, re, urllib.request, urllib.error
from datetime import datetime

REPO = os.environ.get('GITHUB_REPOSITORY', 'sheltr12/Eshenbaugh-Land-Company')
API = 'https://api.github.com'
TK = os.environ['GITHUB_TOKEN']
FILE_PATH = 'comps.json'

def gh(method, path, data=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(API + path, data=body, method=method, headers={
        'Authorization': f'Bearer {TK}', 'Accept': 'application/vnd.github+json',
        'Content-Type': 'application/json', 'User-Agent': 'elc-comps-bot'})
    try:
        with urllib.request.urlopen(req) as r:
            b = r.read(); return json.loads(b) if b else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'{method} {path} -> {e.code}: {e.read().decode()[:400]}')

def norm_bp(s): return str(s).upper().replace(' ', '').replace('-', '/') if s else None
def pid_key(c): return (str(c.get('parcel_id', '')).upper().replace(' ', '').replace('-', ''), c.get('sale_date_iso'))

def set_output(k, v):
    p = os.environ.get('GITHUB_OUTPUT')
    if p:
        with open(p, 'a') as f: f.write(f'{k}={v}\n')
    print(f'{k}={v}')

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--county', required=True); ap.add_argument('--pending', required=True)
    ap.add_argument('--add', action='append', default=[], help='localpath=repopath')
    a = ap.parse_args()
    new_comps = json.load(open(a.pending)); assert isinstance(new_comps, list)

    patch, page = {}, 1
    while True:
        b = gh('GET', f'/repos/{REPO}/branches?per_page=100&page={page}')
        for x in b:
            m = re.match(r'Test-patch-(\d+)$', x['name'])
            if m: patch[int(m.group(1))] = x['name']
        if len(b) < 100: break
        page += 1
    if not patch: raise SystemExit('No Test-patch-N branch found; refusing to branch from Test/main.')
    n = max(patch); base = patch[n]
    print(f'Base branch: {base}')

    raw = urllib.request.Request(f'https://raw.githubusercontent.com/{REPO}/{base}/{FILE_PATH}',
                                 headers={'Authorization': f'Bearer {TK}', 'User-Agent': 'elc-comps-bot'})
    data = json.loads(urllib.request.urlopen(raw).read()); comps = data.get('comps', [])
    ex_bp = {norm_bp(c.get('book_page')) for c in comps if c.get('book_page')}
    ex_pd = {pid_key(c) for c in comps if c.get('parcel_id') and c.get('sale_date_iso')}
    max_num = max((int(c.get('num') or 0) for c in comps), default=0)
    added = skipped = 0
    for c in new_comps:
        bp = norm_bp(c.get('book_page'))
        if (bp and bp in ex_bp) or pid_key(c) in ex_pd:
            skipped += 1; continue
        max_num += 1; c['num'] = max_num
        if not c.get('price_per_acre') and c.get('price') and c.get('acreage'):
            c['price_per_acre'] = round(float(c['price']) / float(c['acreage']))
        comps.append(c); added += 1
        if bp: ex_bp.add(bp)
        ex_pd.add(pid_key(c))
    print(f'Merge: {added} added, {skipped} duplicates skipped. Total {len(comps)}.')
    set_output('added', added); set_output('skipped', skipped); set_output('total', len(comps)); set_output('base_branch', base)
    if added == 0:
        set_output('new_branch', ''); return

    data['comps'] = comps
    data.setdefault('meta', {})['generated'] = datetime.utcnow().strftime('%Y-%m-%d')
    data['meta']['total_comps'] = len(comps)

    base_sha = gh('GET', f'/repos/{REPO}/git/ref/heads/{base}')['object']['sha']
    tree_sha = gh('GET', f'/repos/{REPO}/git/commits/{base_sha}')['tree']['sha']
    entries = []
    blob = gh('POST', f'/repos/{REPO}/git/blobs', {'content': json.dumps(data, separators=(',', ':')), 'encoding': 'utf-8'})
    entries.append({'path': FILE_PATH, 'mode': '100644', 'type': 'blob', 'sha': blob['sha']})
    for spec in a.add:
        local, repo_path = spec.split('=', 1)
        content = open(local, encoding='utf-8').read()
        b = gh('POST', f'/repos/{REPO}/git/blobs', {'content': content, 'encoding': 'utf-8'})
        entries.append({'path': repo_path, 'mode': '100644', 'type': 'blob', 'sha': b['sha']})
    tree = gh('POST', f'/repos/{REPO}/git/trees', {'base_tree': tree_sha, 'tree': entries})
    commit = gh('POST', f'/repos/{REPO}/git/commits', {
        'message': f'Add {a.county} County vacant land comps: {added} new records (from {base}) [automated]',
        'tree': tree['sha'], 'parents': [base_sha]})
    new_branch = f'Test-patch-{n + 1}'
    gh('POST', f'/repos/{REPO}/git/refs', {'ref': f'refs/heads/{new_branch}', 'sha': commit['sha']})
    print(f'Created {new_branch} from {base}')
    set_output('new_branch', new_branch)

if __name__ == '__main__':
    main()
