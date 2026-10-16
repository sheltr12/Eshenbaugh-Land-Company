#!/usr/bin/env python3
"""ELC Rezoning Tracker pipeline.

Discovers adapters in ./adapters/*.py, runs each one in an isolated subprocess with a
timeout, enriches records with parcel centroids/acreage (parcels.py), filters to
Rezoning / PD / Comp Plan Amendment >= 0.5 ac, merges into the existing rezonings.json
(history, first_seen/last_seen, never deletes), and writes:

    OUT/rezonings.json
    OUT/rezoning-digest-<run-date>.txt
    OUT/rezoning-digest-<run-date>.json

Usage:
    python3 rezoning_pipeline.py --db <rezonings.json | URL | none> --out-dir DIR \
        --run-date YYYY-MM-DD [--since YYYY-MM-DD] [--only key1,key2] [--backfill-days 180]
"""
import argparse
import ast
import datetime as dt
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- dependencies
def ensure_deps():
    missing = []
    for mod, pkg in (("requests", "requests"), ("bs4", "beautifulsoup4"), ("lxml", "lxml"), ("pypdf", "pypdf")):
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    if not missing:
        return
    base = [sys.executable, "-m", "pip", "install", "--quiet"] + missing
    for extra in ([], ["--break-system-packages"]):
        try:
            r = subprocess.run(base + extra, capture_output=True, text=True, timeout=600)
            if r.returncode == 0:
                return
        except Exception as e:  # noqa: BLE001
            log(f"pip install failed: {e}")
    log(f"WARNING: could not install {missing}; adapters needing them will fail")


def log(msg):
    print(f"[pipeline] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- constants
CATEGORIES = ("Rezoning", "PD", "Comp Plan Amendment")
STATUSES = ("Filed", "Hearing Scheduled", "Continued", "Approved", "Denied", "Withdrawn")
CLOSED = ("Approved", "Denied", "Withdrawn")
MIN_ACRES = 0.5
DIGEST_WINDOW_DAYS = 21      # new-filing window for the digest + hearings look-ahead
DEFAULT_SINCE_DAYS = 21
REFRESH_DECIDED_DAYS = 30
SKIP_FILES = {"accela_core.py"}
ENRICH_PARALLEL = 4

STR_FIELDS = ("jurisdiction", "county", "app_num", "category", "app_type", "project_name",
              "description", "applicant", "address", "from_zoning", "to_zoning", "from_flu",
              "to_flu", "filed_date", "status", "status_raw", "decision_date", "url",
              "docs_url", "source")
DATE_FIELDS = ("filed_date", "decision_date")


# ---------------------------------------------------------------- small helpers
def parse_date(s):
    if not s:
        return None
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def iso(d):
    return d.isoformat() if d else None


def slugify(s):
    s = re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")
    return re.sub(r"-{2,}", "-", s)


def rec_id(r):
    return f"{r['jurisdiction']}|{r['app_num']}"


def clean_text(v):
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        v = str(v)
    if not isinstance(v, str):
        v = str(v)
    v = re.sub(r"\s+", " ", v).strip()
    return v or None


def to_float(v):
    if v is None or v == "":
        return None
    try:
        f = float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def norm_status(status, status_raw):
    s = clean_text(status)
    if s in STATUSES:
        return s
    t = (s or status_raw or "").lower()
    if not t:
        return "Filed"
    if "withdr" in t or "void" in t or "expired" in t or "incomplete" in t and "closed" in t:
        return "Withdrawn"
    if "deni" in t or "disapprov" in t:
        return "Denied"
    if "approv" in t or "adopted" in t:
        return "Approved"
    if "contin" in t or "defer" in t or "postpon" in t:
        return "Continued"
    if "hearing" in t or "scheduled" in t:
        return "Hearing Scheduled"
    return "Filed"


def norm_category(c, app_type):
    c = clean_text(c)
    if c in CATEGORIES:
        return c
    t = (c or app_type or "").lower()
    if re.search(r"\b(cpa|comp|fluma|flu|land use)", t):
        return "Comp Plan Amendment"
    if re.search(r"\b(pd|pud|mpud|rpud|cpd|pdd|planned|major mod)", t):
        return "PD"
    if "rezon" in t or "zoning map" in t:
        return "Rezoning"
    return c  # out of scope; filtered later


def normalize(raw, adapter_key):
    """Coerce an adapter record into schema shape. Returns None if unusable."""
    if not isinstance(raw, dict):
        return None
    r = {}
    for f in STR_FIELDS:
        r[f] = clean_text(raw.get(f))
    for f in DATE_FIELDS:
        d = parse_date(r[f])
        r[f] = iso(d)
    if not r["jurisdiction"] or not r["app_num"]:
        return None
    r["category"] = norm_category(r["category"], r["app_type"])
    r["status"] = norm_status(r["status"], r["status_raw"])
    pids = raw.get("parcel_ids") or []
    if isinstance(pids, str):
        pids = re.split(r"[;,\s]+", pids)
    r["parcel_ids"] = [p for p in (clean_text(x) for x in pids) if p]
    r["acres"] = to_float(raw.get("acres"))
    if r["acres"] is not None and r["acres"] <= 0:
        r["acres"] = None
    lat, lon = to_float(raw.get("lat")), to_float(raw.get("lon"))
    if lat is None or lon is None or not (24 <= lat <= 31.5 and -88 <= lon <= -79.5):
        lat = lon = None  # outside Florida = bad geometry
    r["lat"], r["lon"] = lat, lon
    hearings = []
    for h in raw.get("hearings") or []:
        if not isinstance(h, dict):
            continue
        d = parse_date(h.get("date"))
        if not d:
            continue
        hearings.append({"body": clean_text(h.get("body")) or "Hearing", "date": iso(d),
                         "result": clean_text(h.get("result"))})
    hearings.sort(key=lambda h: (h["date"], h["body"]))
    r["hearings"] = hearings
    r["acres_source"] = "application" if r["acres"] is not None else None
    r["location_source"] = "source" if r["lat"] is not None else None
    r["adapter"] = adapter_key
    if not r["source"]:
        r["source"] = adapter_key
    return r


# ---------------------------------------------------------------- db io
def load_db(spec):
    if not spec or spec.lower() == "none":
        return {"meta": {}, "records": []}, "none"
    if spec.lower() == "latest":
        # newest Test-patch-N branch of the ELC repo (public, no token needed)
        import requests
        best = None
        try:  # git ls-remote works without API rate limits
            out = subprocess.run(["git", "ls-remote", "--heads",
                                  "https://github.com/sheltr12/Eshenbaugh-Land-Company.git"],
                                 capture_output=True, text=True, timeout=120).stdout
            for line in out.splitlines():
                sha, ref = line.split("\t")
                m = re.fullmatch(r"refs/heads/Test-patch-(\d+)", ref)
                if m and (best is None or int(m.group(1)) > best[0]):
                    best = (int(m.group(1)), sha)
        except Exception as e:  # noqa: BLE001
            log(f"git ls-remote failed ({e}); trying the GitHub API")
        page = 1
        while best is None:
            b = requests.get("https://api.github.com/repos/sheltr12/Eshenbaugh-Land-Company/branches",
                             params={"per_page": 100, "page": page}, timeout=60,
                             headers={"User-Agent": "ELC-Rezoning-Pipeline/1.0"}).json()
            if not isinstance(b, list):
                raise RuntimeError(f"GitHub branch list failed: {str(b)[:200]}")
            for x in b:
                m = re.fullmatch(r"Test-patch-(\d+)", x.get("name", ""))
                if m and (best is None or int(m.group(1)) > best[0]):
                    best = (int(m.group(1)), x["commit"]["sha"])
            if len(b) < 100:
                break
            page += 1
        if not best:
            raise RuntimeError("no Test-patch-N branch found")
        log(f"db: reading rezonings.json from Test-patch-{best[0]}")
        spec = f"https://raw.githubusercontent.com/sheltr12/Eshenbaugh-Land-Company/{best[1]}/rezonings.json"
    if re.match(r"^https?://", spec):
        import requests
        url = spec + ("&" if "?" in spec else "?") + f"cb={int(time.time())}"
        resp = requests.get(url, timeout=60, headers={"User-Agent": "ELC-Rezoning-Pipeline/1.0"})
        if resp.status_code == 404:
            log(f"db URL returned 404, starting with an empty database: {spec}")
            return {"meta": {}, "records": []}, "url-404"
        resp.raise_for_status()
        data = resp.json()
    else:
        with open(spec, encoding="utf-8") as fh:
            data = json.load(fh)
    if isinstance(data, list):
        data = {"meta": {}, "records": data}
    data.setdefault("meta", {})
    data.setdefault("records", [])
    return data, "loaded"


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ---------------------------------------------------------------- adapter discovery
def discover_adapters(adapters_dir, only=None):
    out = []
    if not os.path.isdir(adapters_dir):
        return out
    for fn in sorted(os.listdir(adapters_dir)):
        if not fn.endswith(".py") or fn.startswith("_") or fn in SKIP_FILES:
            continue
        key = fn[:-3]
        if only and key not in only:
            continue
        path = os.path.join(adapters_dir, fn)
        try:
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=path)
        except Exception as e:  # noqa: BLE001
            out.append({"key": key, "path": path, "jurisdictions": [], "parse_error": str(e)})
            continue
        has_fetch = False
        for n in tree.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "fetch":
                has_fetch = True
            elif isinstance(n, (ast.Assign, ast.AnnAssign)):
                tg = n.targets if isinstance(n, ast.Assign) else [n.target]
                has_fetch |= any(getattr(t, "id", None) == "fetch" for t in tg)
            elif isinstance(n, ast.ImportFrom):
                has_fetch |= any((a.asname or a.name) == "fetch" for a in n.names)
        if not has_fetch:
            continue
        juris = None
        for n in tree.body:
            if isinstance(n, ast.Assign) and any(getattr(t, "id", None) == "JURISDICTIONS" for t in n.targets):
                try:
                    juris = list(ast.literal_eval(n.value))
                except Exception:  # noqa: BLE001
                    juris = None
        out.append({"key": key, "path": path, "jurisdictions": juris})
    return out


RUNNER = r"""
import importlib.util, json, sys, traceback, os
mode, path, argfile, outfile = sys.argv[1:5]
sys.path[:0] = [os.path.dirname(path), os.path.dirname(os.path.dirname(path))]
spec = importlib.util.spec_from_file_location(os.path.basename(path)[:-3], path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
if mode == "juris":
    json.dump(list(getattr(mod, "JURISDICTIONS", []) or []), open(outfile, "w"))
    sys.exit(0)
args = json.load(open(argfile))
res = mod.fetch(args["since"], args["refresh"])
res = list(res or [])
with open(outfile, "w") as fh:
    json.dump({"jurisdictions": list(getattr(mod, "JURISDICTIONS", []) or []), "records": res}, fh, default=str)
"""

ENRICH_RUNNER = r"""
import json, sys, os, traceback
rzdir, taskfile, outfile = sys.argv[1:4]
sys.path.insert(0, rzdir)
import parcels
tasks = json.load(open(taskfile))
with open(outfile, "a") as fh:
    for t in tasks:
        try:
            res = parcels.site(t["county"], t["ids"])
        except Exception as e:
            print(f"parcels.site({t['county']}, {t['ids']}) failed: {e}", file=sys.stderr, flush=True)
            res = None
        fh.write(json.dumps({"key": t["key"], "res": res}, default=str) + "\n")
        fh.flush()
"""


APN_RUNNER = r"""
import json, sys, os
rzdir, county, idfile, outfile = sys.argv[1:5]
sys.path.insert(0, rzdir)
import parcels
ids = json.load(open(idfile))
res = parcels.lookup(county, ids) or {}
json.dump({k: {"norm_id": v.get("norm_id"), "alt_key": v.get("alt_key")} for k, v in res.items()},
          open(outfile, "w"), default=str)
"""

def run_subprocess(argv, timeout, log_path, cwd):
    """Run argv in its own process group; kill the whole group on timeout.
    Returns (returncode or None on timeout, stderr tail)."""
    with open(log_path, "w") as logf:
        p = subprocess.Popen(argv, cwd=cwd, stdout=logf, stderr=subprocess.STDOUT,
                             start_new_session=True, env=dict(os.environ, PYTHONUNBUFFERED="1"))
        try:
            rc = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except Exception:  # noqa: BLE001
                p.kill()
            p.wait()
            rc = None
    try:
        with open(log_path, errors="replace") as fh:
            tail = fh.read()[-1500:]
    except Exception:  # noqa: BLE001
        tail = ""
    return rc, tail


def adapter_jurisdictions(ad, workdir):
    if ad.get("jurisdictions"):
        return ad["jurisdictions"]
    out = os.path.join(workdir, f"{ad['key']}.juris.json")
    rc, _ = run_subprocess([sys.executable, "-c", RUNNER, "juris", ad["path"], "-", out], 120,
                           os.path.join(workdir, f"{ad['key']}.juris.log"), HERE)
    if rc == 0 and os.path.exists(out):
        try:
            return json.load(open(out))
        except Exception:  # noqa: BLE001
            pass
    return []


def is_refreshable(rec, run_date):
    if rec.get("status") not in CLOSED:
        return True
    dd = parse_date(rec.get("decision_date")) or parse_date(rec.get("status_changed"))
    return bool(dd and (run_date - dd).days <= REFRESH_DECIDED_DAYS)


def run_adapters(adapters, db_records, known_juris, covered_adapters, run_date, since_override,
                 backfill_days, timeout, workdir, parallel=1):
    """Run every adapter in its own subprocess (killed after `timeout` s). Up to `parallel`
    adapters run at once; each talks to a different source, so this stays polite per host."""
    from concurrent.futures import ThreadPoolExecutor

    def one(ad):
        key = ad["key"]
        t0 = time.time()
        h = {"adapter": key, "jurisdictions": [], "status": "failed", "count": 0, "error": None,
             "seconds": 0, "since": None, "refresh_count": 0}
        recs = []
        if ad.get("parse_error"):
            h["error"] = "syntax error: " + ad["parse_error"][:300]
            return recs, h
        juris = adapter_jurisdictions(ad, workdir)
        h["jurisdictions"] = juris
        if covered_adapters:
            known = key in covered_adapters
        else:  # legacy db without adapter coverage info
            known = bool(juris) and all(j in known_juris for j in juris)
        if since_override:
            since = since_override
        elif known:
            since = run_date - dt.timedelta(days=DEFAULT_SINCE_DAYS)
        else:
            since = run_date - dt.timedelta(days=backfill_days)
        refresh = sorted({r["app_num"] for r in db_records
                          if (r.get("adapter") == key or (not r.get("adapter") and r.get("jurisdiction") in juris))
                          and is_refreshable(r, run_date)})
        h["since"], h["refresh_count"] = iso(since), len(refresh)
        argfile = os.path.join(workdir, f"{key}.args.json")
        outfile = os.path.join(workdir, f"{key}.out.json")
        with open(argfile, "w") as fh:
            json.dump({"since": iso(since), "refresh": refresh}, fh)
        log(f"adapter {key}: start since={iso(since)} refresh={len(refresh)} juris={juris}")
        rc, tail = run_subprocess([sys.executable, "-c", RUNNER, "fetch", ad["path"], argfile, outfile],
                                  timeout, os.path.join(workdir, f"{key}.log"), HERE)
        h["seconds"] = round(time.time() - t0, 1)
        if rc is None:
            h["error"] = f"timed out after {timeout}s"
        elif rc != 0 or not os.path.exists(outfile):
            lines = [ln for ln in tail.strip().splitlines() if ln.strip()]
            h["error"] = (lines[-1] if lines else f"exit code {rc}")[:400]
        else:
            try:
                with open(outfile) as fh:
                    data = json.load(fh)
                raw = data.get("records") or []
                h["jurisdictions"] = data.get("jurisdictions") or juris
                for x in raw:
                    try:
                        n = normalize(x, key)
                    except Exception as e:  # noqa: BLE001
                        log(f"adapter {key}: bad record skipped: {e}")
                        n = None
                    if n:
                        recs.append(n)
                h.update(status="ok", count=len(recs))
                if len(recs) < len(raw):
                    h["error"] = f"{len(raw) - len(recs)} malformed records skipped"
            except Exception as e:  # noqa: BLE001
                h["error"] = f"bad output: {e}"[:400]
        log(f"adapter {key}: {h['status']} count={h['count']} {h['seconds']}s {h['error'] or ''}")
        return recs, h

    # adapters sharing a key prefix (accela_pasco, accela_tampa, ...) usually share a host,
    # so they run one after another in the same lane
    lanes = {}
    for ad in adapters:
        lanes.setdefault(ad["key"].split("_")[0] if "_" in ad["key"] else ad["key"], []).append(ad)
    done = {}
    with ThreadPoolExecutor(max_workers=max(1, parallel)) as ex:
        for lane_out in ex.map(lambda lane: [(ad["key"], one(ad)) for ad in lane], lanes.values()):
            done.update(dict(lane_out))
    results, health = [], []
    for ad in adapters:  # keep adapter order
        recs, h = done[ad["key"]]
        results.extend(recs)
        health.append(h)
    return results, health


# ---------------------------------------------------------------- enrichment
def enrich(records, existing, timeout, workdir, parcels_dir=HERE):
    """Fill lat/lon (and acres if missing) from parcels.site for records lacking geometry."""
    if not os.path.exists(os.path.join(parcels_dir, "parcels.py")):
        log("parcels.py not found; skipping enrichment")
        return {"status": "skipped", "reason": "parcels.py missing", "lookups": 0, "located": 0}
    tasks, by_key = [], {}
    for r in records:
        old = existing.get(rec_id(r))
        # reuse a previous parcel lookup if parcel_ids unchanged
        if old and r["lat"] is None and old.get("lat") is not None and \
                sorted(old.get("parcel_ids") or []) == sorted(r["parcel_ids"]):
            continue
        need_loc = r["lat"] is None
        need_ac = r["acres"] is None and not (old and old.get("acres") is not None)
        if not (need_loc or need_ac) or not r["parcel_ids"] or not r["county"]:
            continue
        k = f"{r['county']}::{'|'.join(sorted(r['parcel_ids']))}"
        if k not in by_key:
            by_key[k] = []
            tasks.append({"key": k, "county": r["county"], "ids": r["parcel_ids"]})
        by_key[k].append(r)
    if not tasks:
        return {"status": "ok", "lookups": 0, "located": 0}
    # one subprocess per county (different servers), up to ENRICH_PARALLEL at once, shared deadline
    by_county = {}
    for t in tasks:
        by_county.setdefault(t["county"], []).append(t)
    t0 = time.time()
    deadline = t0 + timeout
    pending = sorted(by_county.items(), key=lambda kv: -len(kv[1]))
    running, outfiles, problems = [], [], []
    while pending or running:
        while pending and len(running) < ENRICH_PARALLEL:
            county, ctasks = pending.pop(0)
            slug = slugify(county) or "unknown"
            taskfile = os.path.join(workdir, f"enrich.{slug}.tasks.json")
            outfile = os.path.join(workdir, f"enrich.{slug}.out.jsonl")
            json.dump(ctasks, open(taskfile, "w"))
            open(outfile, "w").close()
            logf = open(os.path.join(workdir, f"enrich.{slug}.log"), "w")
            proc = subprocess.Popen([sys.executable, "-c", ENRICH_RUNNER, parcels_dir, taskfile, outfile],
                                    cwd=HERE, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
            running.append((county, proc, logf))
            outfiles.append(outfile)
        time.sleep(0.5)
        for item in list(running):
            county, proc, logf = item
            rc = proc.poll()
            if rc is None and time.time() > deadline:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:  # noqa: BLE001
                    proc.kill()
                proc.wait()
                rc = "timeout"
            if rc is not None:
                logf.close()
                running.remove(item)
                if rc != 0:
                    problems.append(f"{county}: {'timed out' if rc == 'timeout' else 'exit ' + str(rc)}")
        if time.time() > deadline and pending:
            problems += [f"{c}: not started (deadline)" for c, _ in pending]
            pending = []
    located = done = 0
    for outfile in outfiles:
        with open(outfile) as fh:
            lines = fh.readlines()
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            done += 1
            res = row.get("res")
            if not isinstance(res, dict):
                continue
            lat, lon, ac = to_float(res.get("lat")), to_float(res.get("lon")), to_float(res.get("acres"))
            for r in by_key.get(row["key"], []):
                if r["lat"] is None and lat is not None and lon is not None:
                    r["lat"], r["lon"], r["location_source"] = lat, lon, "parcels"
                    located += 1
                if r["acres"] is None and ac:
                    r["acres"], r["acres_source"] = round(ac, 2), "parcels"
                if res.get("missing"):
                    r["parcels_missing"] = list(res["missing"])
    info = {"status": "ok" if not problems else "partial", "lookups": len(tasks), "completed": done,
            "located": located, "seconds": round(time.time() - t0, 1)}
    if problems:
        info["error"] = "; ".join(problems)[:300]
    log(f"enrichment: {info}")
    return info


# ---------------------------------------------------------------- full parcel numbers (APNs)
# parcel_ids are kept exactly as the source posted them. apns holds the full parcel number for
# each one (same order) for display: Hillsborough folio -> PIN; a short Alternate Key / PA key /
# tax account in Lake, Citrus, Marion, Hernando, Volusia or Brevard -> that county's parcel number;
# an undashed Osceola / Orange / Seminole number -> the PA's dashed display format. Everything else is already the
# full number and is copied as is. apns_src records the parcel_ids the apns were built from, so a
# record is only looked up again when its parcel_ids change or a lookup was incomplete.
APN_PIN_RE = re.compile(r"^[A-Z]-\d{2}-\d{2}-\d{2}-")
APN_ALTKEY_COUNTIES = {"Lake", "Citrus", "Marion", "Hernando", "Volusia", "Brevard"}
# counties whose sources sometimes print the parcel number with no separators
APN_COMPACT_COUNTIES = {"Osceola", "Orange", "Seminole"}


def _apn_needs_lookup(county, pid):
    if county == "Hillsborough":
        return not APN_PIN_RE.match(pid)
    if county in APN_ALTKEY_COUNTIES:
        return bool(re.fullmatch(r"\d{1,9}", re.sub(r"[\s-]", "", pid)))
    if county in APN_COMPACT_COUNTIES:
        return bool(re.fullmatch(r"[0-9A-Za-z]{12,}", pid))
    return False


def _apn_value(county, hit):
    if not isinstance(hit, dict):
        return None
    if county == "Hillsborough":
        v = hit.get("alt_key")
        return v if v and APN_PIN_RE.match(str(v)) else None
    v = hit.get("norm_id")
    return v if v and not re.fullmatch(r"\d{1,9}", re.sub(r"[\s-]", "", str(v))) else None


def fill_apns(records, timeout, workdir, parcels_dir=HERE):
    """Set r['apns'] (full parcel numbers, aligned with r['parcel_ids']) where missing or stale."""
    if not os.path.exists(os.path.join(parcels_dir, "parcels.py")):
        return {"status": "skipped", "reason": "parcels.py missing"}
    todo, need = [], {}
    for r in records:
        pids = list(r.get("parcel_ids") or [])
        if not pids:
            r.pop("apns", None)
            r.pop("apns_src", None)
            continue
        if r.get("apns_src") == pids and len(r.get("apns") or []) == len(pids):
            continue
        county = r.get("county") or ""
        todo.append(r)
        for pid in pids:
            if _apn_needs_lookup(county, pid):
                need.setdefault(county, set()).add(pid)
    found, problems, t0 = {}, [], time.time()
    deadline = t0 + timeout
    procs = []
    for county, ids in need.items():
        slug = slugify(county) or "unknown"
        idfile = os.path.join(workdir, f"apns.{slug}.ids.json")
        outfile = os.path.join(workdir, f"apns.{slug}.out.json")
        json.dump(sorted(ids), open(idfile, "w"))
        if os.path.exists(outfile):
            os.remove(outfile)
        logf = open(os.path.join(workdir, f"apns.{slug}.log"), "w")
        proc = subprocess.Popen([sys.executable, "-c", APN_RUNNER, parcels_dir, county, idfile, outfile],
                                cwd=HERE, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
        procs.append((county, proc, logf, outfile))
    for county, proc, logf, outfile in procs:
        try:
            rc = proc.wait(timeout=max(1, deadline - time.time()))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:  # noqa: BLE001
                proc.kill()
            proc.wait()
            rc = "timeout"
        logf.close()
        if rc != 0:
            problems.append(f"{county}: {'timed out' if rc == 'timeout' else 'exit ' + str(rc)}")
            continue
        try:
            for k, v in (json.load(open(outfile)) or {}).items():
                found[(county, k)] = v
        except (OSError, ValueError) as e:
            problems.append(f"{county}: bad output ({e})")
    converted = unresolved = 0
    for r in todo:
        county = r.get("county") or ""
        pids = list(r["parcel_ids"])
        out, complete = [], True
        for pid in pids:
            if _apn_needs_lookup(county, pid):
                v = _apn_value(county, found.get((county, pid)))
                if v:
                    converted += 1
                else:
                    complete = False
                    unresolved += 1
                out.append(v or pid)
            else:
                out.append(pid)
        r["apns"] = out
        if complete:
            r["apns_src"] = pids
        else:
            r.pop("apns_src", None)  # try again next run
    info = {"status": "ok" if not problems else "partial", "records": len(todo),
            "converted": converted, "unresolved": unresolved, "seconds": round(time.time() - t0, 1)}
    if problems:
        info["error"] = "; ".join(problems)[:300]
    log(f"apns: {info}")
    return info

# ---------------------------------------------------------------- merge
MERGE_FIELDS = STR_FIELDS + ("parcel_ids", "acres", "lat", "lon", "acres_source", "location_source",
                             "adapter")


def merge_hearings(old, new, run_date):
    if not new:
        return old or []
    keyed = {(h["body"], h["date"]): h for h in new}
    for h in old or []:
        k = (h.get("body"), h.get("date"))
        if k in keyed:
            if not keyed[k].get("result") and h.get("result"):
                keyed[k]["result"] = h["result"]
            continue
        # keep past hearings the source dropped (history); drop stale future dates (rescheduled)
        d = parse_date(h.get("date"))
        if d and d < run_date:
            keyed[k] = h
    return sorted(keyed.values(), key=lambda h: (h["date"], h["body"] or ""))


def derive(rec, run_date):
    rec["id"] = rec_id(rec)
    rec["slug"] = slugify(f"{rec['jurisdiction']}-{rec['app_num']}")
    rec["acres_unknown"] = rec.get("acres") is None
    future = [h for h in rec.get("hearings") or [] if (parse_date(h["date"]) or dt.date.min) >= run_date]
    rec["next_hearing"] = {"body": future[0]["body"], "date": future[0]["date"]} if future and \
        rec.get("status") not in CLOSED else None
    past = [h for h in rec.get("hearings") or [] if (parse_date(h["date"]) or dt.date.max) < run_date]
    rec["last_hearing"] = past[-1] if past else None


def merge(db_records, fresh, run_date):
    rd = iso(run_date)
    by_id = {r["id"]: r for r in db_records if r.get("id")}
    for r in db_records:  # tolerate older dbs without id
        if not r.get("id") and r.get("jurisdiction") and r.get("app_num"):
            r["id"] = rec_id(r)
            by_id[r["id"]] = r
    new_ids, changes = [], []
    seen_now = set()
    for f in fresh:
        rid = rec_id(f)
        seen_now.add(rid)  # a duplicate within one run simply merges into the first copy
        old = by_id.get(rid)
        if old is None:
            rec = dict(f)
            rec["first_seen"] = rd
            rec["last_seen"] = rd
            rec["status_changed"] = None
            rec["history"] = [{"date": rd, "status": rec["status"], "from": None}]
            fd = parse_date(rec.get("filed_date"))
            rec["backfilled"] = bool(fd and (run_date - fd).days > DIGEST_WINDOW_DAYS)
            by_id[rid] = rec
            new_ids.append(rid)
        else:
            rec = old
            prev_status = old.get("status")
            for k in MERGE_FIELDS:
                if k in ("hearings",):
                    continue
                v = f.get(k)
                if v is None or v == []:
                    continue
                if k in ("acres_source", "location_source"):
                    continue  # set alongside their values below
                if k == "acres" and old.get("acres") is not None and f.get("acres_source") == "parcels" \
                        and old.get("acres_source") == "application":
                    continue  # application acreage beats parcel acreage
                rec[k] = v
                if k == "acres":
                    rec["acres_source"] = f.get("acres_source")
                if k == "lat":
                    rec["location_source"] = f.get("location_source")
            rec["hearings"] = merge_hearings(old.get("hearings"), f.get("hearings"), run_date)
            rec["last_seen"] = rd
            if f["status"] != prev_status and rid not in new_ids:
                rec["status"] = f["status"]
                rec.setdefault("history", [])
                rec["history"].append({"date": rd, "status": f["status"], "from": prev_status})
                rec["status_changed"] = rd
                if rid not in changes:
                    changes.append(rid)
            if f["status"] in ("Approved", "Denied") and not rec.get("decision_date"):
                rec["decision_date"] = rd if f["status"] != prev_status else rec.get("decision_date")
    for r in by_id.values():
        derive(r, run_date)  # (re)compute id/slug/next_hearing relative to this run date
    return list(by_id.values()), new_ids, changes


def apply_filters(fresh):
    kept, dropped_cat, dropped_small = [], 0, 0
    for r in fresh:
        if r["category"] not in CATEGORIES:
            dropped_cat += 1
            continue
        if r["acres"] is not None and r["acres"] < MIN_ACRES:
            dropped_small += 1
            continue
        kept.append(r)
    return kept, dropped_cat, dropped_small


# ---------------------------------------------------------------- meta + digest
def count_by(records, key):
    out = {}
    for r in records:
        k = r.get(key) or "Unknown"
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def sort_records(records):
    return sorted(records, key=lambda r: (r.get("filed_date") or r.get("first_seen") or "",
                                          r.get("jurisdiction") or "", r.get("app_num") or ""),
                  reverse=True)


def digest_item(r, hearing=None):
    nh = hearing or r.get("next_hearing")
    return {
        "id": r["id"], "jurisdiction": r.get("jurisdiction"), "county": r.get("county"),
        "app_num": r.get("app_num"), "category": r.get("category"), "app_type": r.get("app_type"),
        "project_name": r.get("project_name"), "applicant": r.get("applicant"),
        "description": r.get("description"),
        "address": r.get("address"), "acres": r.get("acres"),
        "from_zoning": r.get("from_zoning"), "to_zoning": r.get("to_zoning"),
        "from_flu": r.get("from_flu"), "to_flu": r.get("to_flu"),
        "status": r.get("status"), "filed_date": r.get("filed_date"),
        "next_hearing": {"body": nh.get("body"), "date": nh.get("date")} if nh else None,
        "url": r.get("url"),
    }


def build_digest(records, new_ids, changes, health, run_date, enrich_info):
    by_id = {r["id"]: r for r in records}
    new_recent, backfill_counts = {}, {}
    for rid in new_ids:
        r = by_id[rid]
        if r.get("backfilled"):
            j = r.get("jurisdiction") or "Unknown"
            backfill_counts[j] = backfill_counts.get(j, 0) + 1
            continue
        new_recent.setdefault(r.get("county") or "Unknown", []).append(r)
    new_filings = {c: [digest_item(r) for r in sort_records(v)]
                   for c, v in sorted(new_recent.items())}
    status_changes = []
    for rid in changes:
        r = by_id[rid]
        h = r["history"][-1]
        it = digest_item(r)
        it.update({"from_status": h.get("from"), "to_status": h.get("status")})
        status_changes.append(it)
    order = {s: i for i, s in enumerate(("Approved", "Denied", "Withdrawn", "Continued",
                                         "Hearing Scheduled", "Filed"))}
    status_changes.sort(key=lambda x: (order.get(x["to_status"], 9), x["jurisdiction"] or "",
                                       x["app_num"] or ""))
    end = run_date + dt.timedelta(days=DIGEST_WINDOW_DAYS)
    upcoming = []
    for r in records:
        if r.get("status") in CLOSED:
            continue
        for h in r.get("hearings") or []:
            d = parse_date(h.get("date"))
            if d and run_date <= d <= end:
                upcoming.append(digest_item(r, h))
    upcoming.sort(key=lambda x: (x["next_hearing"]["date"], x["jurisdiction"] or "", x["app_num"] or ""))
    problems = [h for h in health if h["status"] != "ok" or h["count"] == 0]
    return {
        "title": "ELC Rezoning Tracker Weekly Digest",
        "run_date": iso(run_date),
        "generated": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "summary": {
            "new_filings": sum(len(v) for v in new_filings.values()),
            "backfilled_new_records": sum(backfill_counts.values()),
            "status_changes": len(status_changes),
            "approvals": sum(1 for s in status_changes if s["to_status"] == "Approved"),
            "denials": sum(1 for s in status_changes if s["to_status"] == "Denied"),
            "upcoming_hearings": len(upcoming),
            "adapters_ok": sum(1 for h in health if h["status"] == "ok"),
            "adapters_failed": sum(1 for h in health if h["status"] != "ok"),
        },
        "new_filings": new_filings,
        "backfilled_by_jurisdiction": backfill_counts,
        "status_changes": status_changes,
        "upcoming_hearings": upcoming,
        "source_health": health,
        "source_problems": problems,
        "enrichment": enrich_info,
    }


def fmt_acres(a):
    if a is None:
        return "acres unknown"
    return f"{a:,.2f} ac" if a < 10 else (f"{a:,.1f} ac" if a < 1000 else f"{a:,.0f} ac")


def fmt_change(a, b):
    if not a and not b:
        return None
    return f"{a or '?'} -> {b or '?'}"


def fmt_item(it, extra_status=None):
    title = " / ".join(x for x in (it.get("project_name"), it.get("applicant")) if x) or \
        (it.get("description") or "")[:140] or "(no name given)"
    cat = it.get("category") or ""
    if it.get("app_type") and it["app_type"] != cat:
        cat = f"{cat} ({it['app_type']})"
    lines = [f"  * {it['jurisdiction']}  #{it['app_num']}  [{cat}]",
             f"    {title}"]
    det = [fmt_acres(it.get("acres"))]
    z = fmt_change(it.get("from_zoning"), it.get("to_zoning"))
    if z:
        det.append(f"Zoning {z}")
    f = fmt_change(it.get("from_flu"), it.get("to_flu"))
    if f:
        det.append(f"FLU {f}")
    lines.append("    " + "  |  ".join(det))
    if it.get("address"):
        lines.append(f"    Address: {it['address']}")
    st = extra_status or it.get("status")
    nh = it.get("next_hearing")
    nh_s = f"{nh['date']} {nh['body']}" if nh else "none set"
    lines.append(f"    Status: {st}  |  Next hearing: {nh_s}")
    if it.get("url"):
        lines.append(f"    Link: {it['url']}")
    return "\n".join(lines)


def digest_text(d):
    s = d["summary"]
    out = [f"ELC REZONING TRACKER: WEEKLY DIGEST FOR {d['run_date']}",
           "=" * 60,
           f"{s['new_filings']} new filings, {s['status_changes']} status changes "
           f"({s['approvals']} approved, {s['denials']} denied), {s['upcoming_hearings']} hearings "
           f"in the next {DIGEST_WINDOW_DAYS} days. Sources: {s['adapters_ok']} ok, "
           f"{s['adapters_failed']} failed.", ""]
    out += ["NEW FILINGS", "-" * 60]
    if not d["new_filings"]:
        out.append("  None this week.")
    for county, items in d["new_filings"].items():
        out.append(f"{county} County ({len(items)})" if county != "Unknown" else f"County unknown ({len(items)})")
        out += [fmt_item(it) for it in items]
        out.append("")
    if d["backfilled_by_jurisdiction"]:
        out.append("  First-run backfill (older filings added to the map, not listed here): " +
                   ", ".join(f"{j} {n}" for j, n in sorted(d["backfilled_by_jurisdiction"].items())))
    out += ["", "STATUS CHANGES", "-" * 60]
    if not d["status_changes"]:
        out.append("  None this week.")
    for it in d["status_changes"]:
        out.append(fmt_item(it, extra_status=f"{it['from_status']} -> {it['to_status']}"))
    out += ["", f"UPCOMING HEARINGS (NEXT {DIGEST_WINDOW_DAYS} DAYS)", "-" * 60]
    if not d["upcoming_hearings"]:
        out.append("  None scheduled.")
    last = None
    for it in d["upcoming_hearings"]:
        date = it["next_hearing"]["date"]
        if date != last:
            out.append(f"{date} ({dt.date.fromisoformat(date).strftime('%A')})")
            last = date
        out.append(fmt_item(it))
    out += ["", "SOURCE HEALTH", "-" * 60]
    if not d["source_health"]:
        out.append("  No adapters ran.")
    elif not d["source_problems"]:
        out.append(f"  All {len(d['source_health'])} sources ran ok.")
    for h in d["source_problems"]:
        j = ", ".join(h.get("jurisdictions") or []) or h["adapter"]
        if h["status"] != "ok":
            out.append(f"  FAILED  {h['adapter']} ({j}): {h.get('error') or 'unknown error'}")
        else:
            out.append(f"  ZERO    {h['adapter']} ({j}): returned 0 records "
                       f"(since {h.get('since')}, {h.get('refresh_count')} refresh ids)")
    ok = [h for h in d["source_health"] if h["status"] == "ok" and h["count"] > 0]
    if ok and d["source_problems"]:
        out.append("  Ok: " + ", ".join(f"{h['adapter']} ({h['count']})" for h in ok))
    e = d.get("enrichment") or {}
    if e.get("status") not in (None, "ok"):
        out.append(f"  Parcel lookup: {e.get('status')} {e.get('error') or e.get('reason') or ''}".rstrip())
    out.append("")
    return no_dashes("\n".join(out))


def no_dashes(s):
    s = re.sub("\\s*\u2014\\s*", ", ", s)
    return s.replace("\u2013", "-").replace("\u2012", "-").replace("\u2015", "-")


# ---------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description="ELC Rezoning Tracker pipeline")
    ap.add_argument("--db", required=True, help="existing rezonings.json path, URL, 'latest' (newest Test-patch-N), or 'none'")
    ap.add_argument("--require-db", action="store_true", help="abort if the db is missing or empty (protects against re-backfilling)")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--run-date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--since", help="override since date for every adapter")
    ap.add_argument("--only", help="comma-separated adapter keys")
    ap.add_argument("--backfill-days", type=int, default=180)
    ap.add_argument("--adapters-dir", default=os.path.join(HERE, "adapters"))
    ap.add_argument("--adapter-timeout", type=int, default=900, help="seconds per adapter")
    ap.add_argument("--parallel", type=int, default=4, help="adapters to run at once (1 = sequential)")
    ap.add_argument("--enrich-timeout", type=int, default=1200, help="seconds for all parcel lookups")
    ap.add_argument("--parcels-dir", default=HERE, help="directory holding parcels.py")
    ap.add_argument("--apn-timeout", type=int, default=900, help="seconds for full parcel number lookups")
    ap.add_argument("--log-dir", help="keep adapter logs here (default: temp dir, deleted)")
    a = ap.parse_args(argv)

    ensure_deps()
    run_date = dt.date.fromisoformat(a.run_date)
    since_override = dt.date.fromisoformat(a.since) if a.since else None
    only = {x.strip() for x in a.only.split(",") if x.strip()} if a.only else None
    os.makedirs(a.out_dir, exist_ok=True)

    db, db_state = load_db(a.db)  # raises (aborts run) if an existing db can't be read
    if a.require_db and not db.get("records"):
        log(f"ABORT: --require-db set but the database is empty ({db_state}); nothing written")
        sys.exit(3)
    db_records = db["records"]
    old_meta = db.get("meta") or {}
    known_juris = set(old_meta.get("covered_jurisdictions") or []) | \
        {r.get("jurisdiction") for r in db_records if r.get("jurisdiction")}
    log(f"db: {db_state}, {len(db_records)} records, {len(known_juris)} known jurisdictions")

    tmp = tempfile.TemporaryDirectory(prefix="rz-pipeline-")
    workdir = a.log_dir or tmp.name
    os.makedirs(workdir, exist_ok=True)

    adapters = discover_adapters(a.adapters_dir, only)
    log(f"adapters: {[x['key'] for x in adapters]}")
    covered_adapters = set(old_meta.get("covered_adapters") or [])
    fresh, health = run_adapters(adapters, db_records, known_juris, covered_adapters, run_date,
                                 since_override, a.backfill_days, a.adapter_timeout, workdir,
                                 a.parallel)
    fetched = len(fresh)
    fresh = [r for r in fresh if r["category"] in CATEGORIES]
    existing = {r.get("id") or rec_id(r): r for r in db_records if r.get("jurisdiction")}
    try:
        enrich_info = enrich(fresh, existing, a.enrich_timeout, workdir, a.parcels_dir)
    except Exception as e:  # noqa: BLE001
        enrich_info = {"status": "failed", "error": str(e)[:300]}
        log(f"enrichment crashed: {e}")
    # existing record acreage counts when deciding the size filter
    for r in fresh:
        old = existing.get(rec_id(r))
        if r["acres"] is None and old and old.get("acres") is not None:
            r["acres"] = old["acres"]
            r["acres_source"] = old.get("acres_source")
    fresh, dropped_cat, dropped_small = apply_filters(fresh)
    records, new_ids, changes = merge(db_records, fresh, run_date)
    records = sort_records(records)
    try:
        apn_info = fill_apns(records, a.apn_timeout, workdir, a.parcels_dir)
    except Exception as e:  # noqa: BLE001
        apn_info = {"status": "failed", "error": str(e)[:300]}
        log(f"apn lookup crashed: {e}")

    covered = set(old_meta.get("covered_jurisdictions") or [])
    for h in health:
        if h["status"] == "ok":
            covered.update(h.get("jurisdictions") or [])
            covered_adapters.add(h["adapter"])

    generated = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    failed = [h["adapter"] for h in health if h["status"] != "ok"]
    meta = {
        "title": "ELC Rezoning Tracker",
        "generated": generated,
        "run_date": iso(run_date),
        "record_count": len(records),
        "min_acres": MIN_ACRES,
        "categories": list(CATEGORIES),
        "statuses": list(STATUSES),
        "counts": {
            "jurisdiction": count_by(records, "jurisdiction"),
            "county": count_by(records, "county"),
            "category": count_by(records, "category"),
            "status": count_by(records, "status"),
        },
        "new_this_run": len(new_ids),
        "new_this_run_recent": sum(1 for r in records if r["id"] in set(new_ids)
                                   and not r.get("backfilled")),
        "status_changes_this_run": len(changes),
        "fetched_this_run": fetched,
        "dropped_small_this_run": dropped_small,
        "adapter_health": health,
        "failed_sources": failed,
        "enrichment": enrich_info,
        "apns": apn_info,
        "covered_jurisdictions": sorted(covered),
        "covered_adapters": sorted(covered_adapters),
        "located_count": sum(1 for r in records if r.get("lat") is not None),
        "db_source": db_state,
        "previous_run_date": old_meta.get("run_date"),
    }
    write_json(os.path.join(a.out_dir, "rezonings.json"), {"meta": meta, "records": records})

    digest = build_digest(records, new_ids, changes, health, run_date, enrich_info)
    with open(os.path.join(a.out_dir, f"rezoning-digest-{iso(run_date)}.json"), "w", encoding="utf-8") as fh:
        fh.write(no_dashes(json.dumps(digest, ensure_ascii=False, indent=1)))
    with open(os.path.join(a.out_dir, f"rezoning-digest-{iso(run_date)}.txt"), "w", encoding="utf-8") as fh:
        fh.write(digest_text(digest))
    log(f"done: {len(records)} records, {len(new_ids)} new, {len(changes)} status changes, "
        f"failed sources: {failed or 'none'}")
    tmp.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
