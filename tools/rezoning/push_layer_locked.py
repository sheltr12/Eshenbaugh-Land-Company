#!/usr/bin/env python3
"""
push_layer_locked.py - concurrency-safe deploy of a map layer (e.g. the rezoning tracker)
to the ELC GitHub Pages site. Same branching rules as push_comps_locked.py.

Usage:
  python3 push_layer_locked.py --label "Rezoning tracker" \
      --file out/rezonings.json=rezonings.json --file rezonings_map.html=rezonings_map.html \
      [--verify rezonings.json:meta.generated] [--dry-run]

  python3 push_layer_locked.py --branch elc-tools --label "rezoning tools" --file a.py=tools/a.py ...
      (commits straight onto an existing non-Pages branch, fast-forward only)

Rules:
  1. Build on the newest Test-patch-N (never Test / main). Every existing file is kept
     (base_tree); only the --file paths are added or replaced. comps.json is never touched.
  2. Create Test-patch-(N+1). GitHub refuses to create an existing ref, so if another task
     got there first (HTTP 422) we wait and redo the commit on top of the newer branch.
  3. Check file parity, point Pages at the new branch only if it is still the newest,
     request a build, wait, and verify the live copy of the --verify file.

Token: $GITHUB_TOKEN, else ~/Documents/Claude/.github_token, else /sessions/*/mnt/Claude/.github_token,
else .github_token next to this script's folder. Never printed.
"""
import argparse, base64, glob, json, os, random, re, sys, time, urllib.error, urllib.request

REPO = 'sheltr12/Eshenbaugh-Land-Company'
API = 'https://api.github.com'
SITE = 'https://sheltr12.github.io/Eshenbaugh-Land-Company/'
HERE = os.path.dirname(os.path.abspath(__file__))
TK = None


def load_token():
    if os.environ.get('GITHUB_TOKEN'):
        return os.environ['GITHUB_TOKEN'].strip()
    cands = [os.environ.get('GITHUB_TOKEN_FILE'), os.path.expanduser('~/Documents/Claude/.github_token')]
    cands += glob.glob('/sessions/*/mnt/Claude/.github_token') + glob.glob(os.path.expanduser('~/mnt/Claude/.github_token'))
    cands.append(os.path.join(HERE, '..', '.github_token'))
    for p in cands:
        if p and os.path.exists(p):
            return open(p).read().strip()
    sys.exit('No GitHub token found (GITHUB_TOKEN env or Documents/Claude/.github_token).')


def gh(method, path, data=None, ok404=False):
    req = urllib.request.Request(path if path.startswith('http') else API + path,
                                 data=json.dumps(data).encode() if data is not None else None, method=method,
                                 headers={'Authorization': f'token {TK}', 'Accept': 'application/vnd.github+json',
                                          'Content-Type': 'application/json', 'User-Agent': 'elc-layer-bot'})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                body = r.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors='replace')
            if ok404 and e.code == 404:
                return None
            if e.code in (500, 502, 503, 504) and attempt < 3:
                time.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(f'{method} {path} -> {e.code}: {msg[:400]}') from None
        except urllib.error.URLError:
            if attempt < 3:
                time.sleep(5 * (attempt + 1))
                continue
            raise


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


def make_tree(base_sha, files):
    base_tree = gh('GET', f'/repos/{REPO}/git/commits/{base_sha}')['tree']['sha']
    items = []
    for local, repo_path in files:
        blob = gh('POST', f'/repos/{REPO}/git/blobs',
                  {'content': base64.b64encode(open(local, 'rb').read()).decode(), 'encoding': 'base64'})['sha']
        items.append({'path': repo_path, 'mode': '100644', 'type': 'blob', 'sha': blob})
    return gh('POST', f'/repos/{REPO}/git/trees', {'base_tree': base_tree, 'tree': items})['sha']


def dig(obj, dotted):
    for k in dotted.split('.'):
        obj = obj.get(k) if isinstance(obj, dict) else None
    return obj


def main():
    global TK
    ap = argparse.ArgumentParser()
    ap.add_argument('--file', action='append', required=True, help='local_path=repo/path')
    ap.add_argument('--label', default='layer')
    ap.add_argument('--branch', help='commit straight onto this existing branch instead of a new Test-patch-N')
    ap.add_argument('--verify', help='repo_path:dotted.key whose live value must match the pushed file')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    files = [f.split('=', 1) for f in a.file]
    for local, _ in files:
        if not os.path.exists(local):
            sys.exit(f'missing file: {local}')
        if local.endswith('.json'):
            json.load(open(local))  # refuse to push broken JSON
    TK = load_token()

    if a.branch:
        if re.fullmatch(r'Test-patch-\d+|Test|main', a.branch):
            sys.exit('--branch is only for tool branches such as elc-tools')
        ref = gh('GET', f'/repos/{REPO}/git/ref/heads/{a.branch}', ok404=True)
        if not ref:
            sys.exit(f'branch {a.branch} not found')
        parent = ref['object']['sha']
        if a.dry_run:
            print(f'DRY RUN: would commit {len(files)} files onto {a.branch}')
            return
        tree = make_tree(parent, files)
        commit = gh('POST', f'/repos/{REPO}/git/commits', {'message': f'Update {a.label}', 'tree': tree, 'parents': [parent]})['sha']
        gh('PATCH', f'/repos/{REPO}/git/refs/heads/{a.branch}', {'sha': commit, 'force': False})
        print(f'Committed {len(files)} files to {a.branch} ({commit[:7]})')
        return

    for attempt in range(1, 13):
        pb = patch_branches()
        if not pb:
            sys.exit('No Test-patch-N branch found; refusing to branch from Test.')
        n = max(pb)
        base, base_sha = f'Test-patch-{n}', pb[n]
        if a.dry_run:
            print(f'DRY RUN: would create Test-patch-{n + 1} on {base} with {[r for _, r in files]}')
            return
        tree = make_tree(base_sha, files)
        commit = gh('POST', f'/repos/{REPO}/git/commits',
                    {'message': f'Update {a.label} (from {base})', 'tree': tree, 'parents': [base_sha]})['sha']
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
        print(f'Created {new_branch} on {base} ({commit[:7]})')
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
    try:
        gh('POST', f'/repos/{REPO}/pages/builds')
    except RuntimeError as e:
        print('Pages build request:', e)
    for _ in range(40):
        time.sleep(15)
        st = gh('GET', f'/repos/{REPO}/pages/builds/latest')
        if st.get('status') in ('built', 'errored'):
            print('Pages build:', st.get('status'))
            break

    if a.verify:
        path, key = a.verify.split(':', 1)
        local = next(l for l, r in files if r == path)
        want = dig(json.load(open(local)), key)
        for _ in range(12):
            try:
                with urllib.request.urlopen(urllib.request.Request(f'{SITE}{path}?cb={int(time.time())}',
                                                                   headers={'Cache-Control': 'no-cache'}), timeout=120) as r:
                    got = dig(json.loads(r.read()), key)
                if got == want:
                    print(f'LIVE VERIFIED: {path} {key} = {got}')
                    break
                print(f'live {key} = {got}, waiting for {want}')
            except Exception as e:  # noqa: BLE001
                print('live check error:', e)
            time.sleep(20)
        else:
            print('LIVE NOT YET UPDATED: pushed but not visible on the live site yet (CDN cache). Re-check in a few minutes.')
    print(f'NEW_BRANCH={new_branch}')


if __name__ == '__main__':
    main()
