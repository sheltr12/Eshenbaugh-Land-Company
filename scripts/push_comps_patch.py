#!/usr/bin/env python3
"""
Race-safe push of new comps to the ELC comps database (GitHub Pages site).

  GITHUB_TOKEN=... python3 push_comps_patch.py pending.json [--map map.html] [--meta-key key --meta-json '{...}']

What it does, every time, with no assumptions about which branch is latest:
  1. Finds the highest Test-patch-N branch (never branches from Test).
  2. Reads comps.json at that branch's head commit.
  3. Merges the pending comps, skipping any whose dedup key already exists
     (parcel_id + sale_date_iso, or OR Book/Page / instrument found in book_page or comments).
  4. Builds a new commit ON TOP of that head (git data API, so branch N itself is never modified)
     and creates branch Test-patch-N+1 from it. Creating the ref is atomic: if another county
     task created N+1 first, GitHub returns 422 and this script re-reads the new latest branch
     and merges again on top of it, so two tasks can never overlap or lose each other's comps.
  5. Verifies N+1 carries every file N had, switches GitHub Pages to N+1, requests a build,
     waits for it, and checks the live comps.json for the new records.
"""
import argparse, base64, json, os, re, sys, time
import requests

REPO = "sheltr12/Eshenbaugh-Land-Company"
API = "https://api.github.com"
LIVE = "https://sheltr12.github.io/Eshenbaugh-Land-Company/comps.json"


def gh():
    tok = os.environ.get("GITHUB_TOKEN")
    if not tok:
        sys.exit("GITHUB_TOKEN not set")
    s = requests.Session()
    s.headers.update({"Authorization": f"token {tok}", "Accept": "application/vnd.github+json"})
    return s


def norm_pid(s):
    return re.sub(r"[\s\-]", "", (s or "").upper())


def dedup_keys(comps):
    keys, insts = set(), set()
    for c in comps:
        if c.get("parcel_id") and c.get("sale_date_iso"):
            keys.add(norm_pid(c["parcel_id"]) + "|" + c["sale_date_iso"])
        blob = (c.get("book_page") or "") + " " + (c.get("comments") or "")
        insts.update(re.findall(r"\b\d{4,5}/\d{4}\b", blob))          # OR Book/Page
        insts.update(re.findall(r"\b20\d{2}\d{5,8}\b", blob))          # instrument numbers
        if c.get("book_page"):
            insts.add(re.sub(r"^OR\s*", "", str(c["book_page"]).strip()))
    return keys, insts


def latest_patch(s):
    names, page = [], 1
    while True:
        r = s.get(f"{API}/repos/{REPO}/branches", params={"per_page": 100, "page": page}); r.raise_for_status()
        b = r.json()
        if not b: break
        names += [x["name"] for x in b]; page += 1
    nums = [int(m.group(1)) for n in names if (m := re.match(r"Test-patch-(\d+)$", n))]
    return max(nums)


def tree_paths(s, sha):
    r = s.get(f"{API}/repos/{REPO}/git/trees/{sha}", params={"recursive": 1}); r.raise_for_status()
    return {t["path"] for t in r.json()["tree"] if t["type"] == "blob"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pending")
    ap.add_argument("--map", help="HTML map file to publish alongside comps.json")
    ap.add_argument("--meta-key"); ap.add_argument("--meta-json")
    ap.add_argument("--message", default=None)
    a = ap.parse_args()
    pending = json.load(open(a.pending))
    s = gh()

    for attempt in range(6):
        n = latest_patch(s)
        base, new_branch = f"Test-patch-{n}", f"Test-patch-{n + 1}"
        head = s.get(f"{API}/repos/{REPO}/git/ref/heads/{base}").json()["object"]["sha"]
        commit = s.get(f"{API}/repos/{REPO}/git/commits/{head}").json()
        base_tree = commit["tree"]["sha"]
        # read comps.json at that exact commit
        raw = s.get(f"https://raw.githubusercontent.com/{REPO}/{head}/comps.json").text
        db = json.loads(raw)
        comps = db["comps"] if isinstance(db, dict) else db
        before = len(comps)
        keys, insts = dedup_keys(comps)
        max_num = max((c.get("num") or 0) for c in comps)
        added, skipped = [], []
        for c in pending:
            k = norm_pid(c["parcel_id"]) + "|" + c["sale_date_iso"]
            bp = (c.get("book_page") or "").strip()
            if k in keys or (bp and bp in insts):
                skipped.append(c); continue
            max_num += 1
            c = dict(c); c["num"] = max_num
            comps.append(c); added.append(c)
            keys.add(k)
            if bp: insts.add(bp)
        print(f"[{attempt}] base={base} comps_before={before} pending={len(pending)} skipped={len(skipped)} added={len(added)}")
        if not added:
            print("RESULT: nothing new (all pending comps already in database). No branch created."); return
        if isinstance(db, dict):
            db["comps"] = comps
            meta = db.setdefault("meta", {})
            meta["count"] = meta["total_comps"] = len(comps)
            meta["last_updated"] = meta["generated"] = time.strftime("%Y-%m-%d")
            if a.meta_key and a.meta_json:
                mj = json.loads(a.meta_json); mj["branch"] = new_branch
                mj["dedup"] = {"candidates": len(pending), "duplicates_skipped": len(skipped), "new_added": len(added)}
                meta[a.meta_key] = mj
        content = json.dumps(db, indent=2, ensure_ascii=False)

        # build commit on top of base head
        def blob(text):
            r = s.post(f"{API}/repos/{REPO}/git/blobs", json={"content": base64.b64encode(text.encode()).decode(), "encoding": "base64"}); r.raise_for_status(); return r.json()["sha"]
        tree = [{"path": "comps.json", "mode": "100644", "type": "blob", "sha": blob(content)}]
        if a.map:
            tree.append({"path": os.path.basename(a.map), "mode": "100644", "type": "blob", "sha": blob(open(a.map).read())})
        r = s.post(f"{API}/repos/{REPO}/git/trees", json={"base_tree": base_tree, "tree": tree}); r.raise_for_status()
        new_tree = r.json()["sha"]
        msg = a.message or f"Add {len(added)} comps ({time.strftime('%Y-%m-%d')}) on top of {base}"
        r = s.post(f"{API}/repos/{REPO}/git/commits", json={"message": msg, "tree": new_tree, "parents": [head]}); r.raise_for_status()
        new_commit = r.json()["sha"]
        r = s.post(f"{API}/repos/{REPO}/git/refs", json={"ref": f"refs/heads/{new_branch}", "sha": new_commit})
        if r.status_code == 422:
            print(f"{new_branch} was created by another run meanwhile; re-reading latest and retrying"); time.sleep(5); continue
        r.raise_for_status()
        print("created", new_branch, new_commit)
        break
    else:
        sys.exit("gave up after repeated branch collisions")

    missing = tree_paths(s, head) - tree_paths(s, new_commit)
    print("files missing vs prior branch:", missing or "none")
    if missing: sys.exit("ABORT: new branch lost files")

    # Pages: switch source, request build, wait, verify live
    r = s.put(f"{API}/repos/{REPO}/pages", json={"source": {"branch": new_branch, "path": "/"}}); print("pages source ->", new_branch, r.status_code)
    r = s.post(f"{API}/repos/{REPO}/pages/builds"); print("build requested", r.status_code)
    status = None
    for _ in range(40):
        time.sleep(10)
        b = s.get(f"{API}/repos/{REPO}/pages/builds/latest").json()
        status = b.get("status")
        if status == "built" and b.get("commit") == new_commit: break
    print("build status:", status)
    live = None
    for _ in range(12):
        live = requests.get(LIVE, headers={"Cache-Control": "no-cache"}, params={"v": int(time.time())}).json()
        lc = live["comps"] if isinstance(live, dict) else live
        if len(lc) == len(comps): break
        time.sleep(10)
    lc = live["comps"] if isinstance(live, dict) else live
    today = time.strftime("%Y-%m-%d")
    new_live = [c for c in lc if c.get("date_added") == today and c.get("county_fips") == added[0].get("county_fips")]
    ok = len(lc) == len(comps) and len(new_live) >= len(added)
    print(f"live comps={len(lc)} expected={len(comps)}; live new today for county={len(new_live)}; VERIFIED={ok}")
    print(f"RESULT: branch={new_branch} candidates={len(pending)} duplicates_skipped={len(skipped)} new_added={len(added)} live_verified={ok}")


if __name__ == "__main__":
    main()
