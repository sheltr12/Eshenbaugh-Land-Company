#!/usr/bin/env python3
"""Extra views for the weekly advisors page (used by weekly_page.py in split mode).

For each market (Tampa, Orlando) builds:
  glance_html  "This week at a glance" bullets + "Largest pending cases" row, shown in the Cases view
  coming_html  the Coming up view: hearings in the next 21 days, cases continued 2+ times,
               denials in the last 90 days
All facts come from out/rezonings.json and the team-email-<date>-<market>.json written by
team_email.py. Owner of record comes from the parcel cache / parcels.py (time-boxed).
No em dashes anywhere.
"""
import datetime as dt
import html
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

OPEN = ("Filed", "Hearing Scheduled", "Continued")
UPCOMING_DAYS = 21
DENIED_DAYS = 90
STALLED_MIN = 2


def esc(s):
    return html.escape(str(s or ""), quote=True)


def fac(a):
    if a is None:
        return None
    a = float(a)
    return f"{a:,.0f} ac" if a >= 100 else f"{a:,.2f}".rstrip("0").rstrip(".") + " ac"


def fday(s, wd=True):
    try:
        d = dt.date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return str(s or "")
    return d.strftime("%a, %b %-d") if wd else d.strftime("%b %-d, %Y")


def title_of(r):
    t = r.get("project_name") or r.get("address")
    if not t or re.fullmatch(r"[A-Z]{0,6}[\s-]*[0-9][0-9A-Z/ -]*", str(t).strip()):
        t = f"{r.get('category')} case {r.get('app_num')}"
        return re.sub(r"\s+", " ", t).strip()[:120]
    t = re.sub(r"\s+", " ", str(t)).strip()
    if t.isupper() and len(t) > 6 and re.search(r"[A-Z]{3}", t):
        t = re.sub(r"\b(Fl|Us|Sr|Cr|Pd|Pud|Mpud|Cpa|Llc)\b", lambda m: m.group(1).upper(), t.title())
    return t[:120]


def request_of(r):
    parts = []
    for lab, a, b in (("Zoning", r.get("from_zoning"), r.get("to_zoning")),
                      ("Future land use", r.get("from_flu"), r.get("to_flu"))):
        if a and b:
            parts.append(f"{lab} {a} to {b}")
        elif b:
            parts.append(f"{lab} to {b}")
        elif a:
            parts.append(f"{lab} change from {a}")
    s = "; ".join(parts) or (r.get("app_type") or r.get("category") or "")
    return re.sub(r"\s+", " ", s)[:220]


def n_continued(r):
    a = sum(1 for h in r.get("hearings") or []
            if re.search(r"contin|defer|tabled|postpon", (h.get("result") or ""), re.I))
    b = sum(1 for h in r.get("history") or [] if h.get("status") == "Continued")
    return max(a, b)


def norm_key(r):
    return re.sub(r"[^a-z0-9]", "", (r.get("project_name") or r.get("address") or r.get("app_num") or "").lower())[:40]


class Owners:
    """Owner of record: week items first, then parcel cache / parcels.py within a time budget."""

    def __init__(self, out_dir, week_owners, budget_s=300):
        os.environ.setdefault("RZ_PARCEL_CACHE", os.path.join(out_dir, "owner_cache.json"))
        self.week = week_owners
        self.deadline = time.time() + budget_s
        self.memo = {}
        try:
            import team_email  # noqa: E402
            self.lookup = team_email.owners_for
        except Exception as e:  # noqa: BLE001
            print(f"[page_extras] owner lookup unavailable: {e}", file=sys.stderr)
            self.lookup = None

    def __call__(self, r):
        rid = r["id"]
        if rid in self.memo:
            return self.memo[rid]
        names = [o.get("owner") for o in self.week.get(rid) or [] if o.get("owner")]
        if not names and self.lookup and (r.get("parcel_ids") or r.get("apns_src")) and time.time() < self.deadline:
            try:
                owners, _ = self.lookup(r, self.deadline)
                names = [o.get("owner") for o in owners if o.get("owner")]
            except Exception as e:  # noqa: BLE001
                print(f"[page_extras] owner lookup failed for {rid}: {e}", file=sys.stderr)
        seen, out = set(), []
        for n in names:
            if n.upper() not in seen:
                seen.add(n.upper())
                out.append(n)
        self.memo[rid] = out
        return out


def chips(*items):
    return "".join(f'<span class="cu-chip">{esc(x)}</span>' for x in items if x)


def row_html(group, owners, extra="", accent="#9a5b00"):
    r = group[0]
    cases = " / ".join(f"#{x['app_num']}" for x in group)
    acres = max((x.get("acres") for x in group if x.get("acres") is not None), default=None)
    own = []
    for x in group:
        for n in owners(x):
            if n not in own:
                own.append(n)
    app = r.get("applicant")
    lines = []
    req = request_of(r)
    if req:
        lines.append(f'<div class="cu-line"><b>Request</b> {esc(req)}</div>')
    if own:
        lines.append(f'<div class="cu-line"><b>Owner of record</b> {esc(", ".join(own[:3]))}'
                     f'{" +" + str(len(own) - 3) + " more" if len(own) > 3 else ""}</div>')
    if app:
        lines.append(f'<div class="cu-line"><b>Applicant</b> {esc(app[:160])}</div>')
    ss = (r.get("site_status") or {}).get("status")
    link = f'<a href="{esc(r.get("url"))}">View application</a>' if r.get("url") else ""
    return (f'<div class="cu-row" data-jur="{esc(r.get("jurisdiction"))}" style="border-left-color:{accent}">'
            f'<div class="cu-meta">{esc(r.get("jurisdiction"))} &middot; {esc(cases)}</div>'
            f'<div class="cu-title">{esc(title_of(r))}</div>'
            f'<div class="cu-chips">{chips(fac(acres), r.get("category"), ss and ("Site: " + ss))}{extra}</div>'
            + "".join(lines) + (f'<div class="cu-link">{link}</div>' if link else "") + "</div>")


def group_by(recs, keyf):
    out, order = {}, []
    for r in recs:
        k = keyf(r)
        if k not in out:
            out[k] = []
            order.append(k)
        out[k].append(r)
    return [out[k] for k in order]


def build(out_dir, run_date, market, market_of, owner_budget=300):
    db = json.load(open(os.path.join(out_dir, "rezonings.json"), encoding="utf-8"))
    recs = [r for r in db["records"] if market_of(r.get("county")) == market]
    by_id = {r["id"]: r for r in db["records"]}
    tj_path = os.path.join(out_dir, f"team-email-{run_date}-{market}.json")
    tj = json.load(open(tj_path, encoding="utf-8")) if os.path.exists(tj_path) else {"items": [], "counts": {}}
    week_owners = {}
    week = []
    for it in tj.get("items") or []:
        for cid in it.get("cases") or [it.get("id")]:
            if cid in by_id:
                week_owners[cid] = it.get("owners") or []
        if it.get("id") in by_id:
            week.append((it["kind"], by_id[it["id"]], [by_id[c] for c in it.get("cases") or [] if c in by_id]))
    owners = Owners(out_dir, week_owners, owner_budget)
    rd = dt.date.fromisoformat(run_date)
    end = (rd + dt.timedelta(days=UPCOMING_DAYS)).isoformat()
    wk7 = (rd + dt.timedelta(days=6)).isoformat()

    # ---------------- coming up: hearings
    up = [r for r in recs if r.get("status") in OPEN and r.get("next_hearing")
          and run_date <= (r["next_hearing"].get("date") or "") <= end]
    up.sort(key=lambda r: (r["next_hearing"]["date"], r["next_hearing"].get("body") or "", -(r.get("acres") or 0)))
    groups = group_by(up, lambda r: (r["next_hearing"]["date"], r.get("jurisdiction"), r["next_hearing"].get("body"), norm_key(r)))
    days = {}
    for g in groups:
        days.setdefault(g[0]["next_hearing"]["date"], []).append(g)
    hear_html = []
    for d in sorted(days):
        rows = "".join(row_html(g, owners, chips(g[0]["next_hearing"].get("body"))) for g in days[d])
        hear_html.append(f'<div class="cu-group"><div class="cu-day">{esc(fday(d))}'
                         f'<span class="cu-count">{len(days[d])}</span></div>{rows}</div>')

    # ---------------- stalled
    st = [r for r in recs if r.get("status") in OPEN and n_continued(r) >= STALLED_MIN]
    st.sort(key=lambda r: (-n_continued(r), -(r.get("acres") or 0)))
    st_groups = group_by(st, lambda r: (r.get("jurisdiction"), norm_key(r)))
    st_html = []
    for g in st_groups:
        r = g[0]
        nh = r.get("next_hearing")
        extra = chips(f"Continued {n_continued(r)} times",
                      nh and f"Next: {fday(nh.get('date'), False)}, {nh.get('body')}" or "No hearing set")
        st_html.append(row_html(g, owners, extra, "#5b21b6"))

    # ---------------- recently denied
    cut = (rd - dt.timedelta(days=DENIED_DAYS)).isoformat()
    den = [r for r in recs if r.get("status") == "Denied"
           and cut <= (r.get("decision_date") or r.get("status_changed") or "") <= run_date]
    den.sort(key=lambda r: (r.get("decision_date") or r.get("status_changed") or ""), reverse=True)
    den_groups = group_by(den, lambda r: (r.get("jurisdiction"), norm_key(r)))
    den_html = [row_html(g, owners, chips(f"Denied {fday(g[0].get('decision_date') or g[0].get('status_changed'), False)}"), "#b42318")
                for g in den_groups]

    def section(title, sub, body, empty):
        return (f'<div class="cu-sec"><h2>{esc(title)}</h2><div class="cu-sub">{esc(sub)}</div>'
                + ("".join(body) if body else f'<div class="cu-empty">{esc(empty)}</div>') + "</div>")

    coming = ('<div class="cu-wrap"><div class="cu-head"><div class="cu-kicker">Eshenbaugh Land Company</div>'
              f'<div class="cu-h1">Coming up</div><div class="cu-sub">As of {esc(fday(run_date, False))}. '
              'Open cases with a hearing set, cases that keep getting continued, and recent denials. '
              'Owner of record is from the county property appraiser.</div></div>'
              + section(f"Hearings in the next {UPCOMING_DAYS} days ({len(groups)})",
                        "Reach owners before the vote. Grouped by hearing date; companion cases are combined.",
                        hear_html, "No hearings scheduled in the next 21 days.")
              + section(f"Stalled: continued {STALLED_MIN} or more times ({len(st_groups)})",
                        "Repeated continuances often mean a contested or stuck deal, and sometimes an owner open to other options.",
                        st_html, "No open cases have been continued twice or more.")
              + section(f"Denied in the last {DENIED_DAYS} days ({len(den_groups)})",
                        "An owner whose request was denied may be open to selling or to a different plan.",
                        den_html, "No denials in the last 90 days.")
              + "</div>")

    # ---------------- glance
    c = tj.get("counts") or {}
    bullets = []
    apr = sum(1 for k, r, g in week if k == "change" and r.get("status") == "Approved")
    dn = [r for k, r, g in week if k == "change" and r.get("status") == "Denied"]
    wd = sum(1 for k, r, g in week if k == "change" and r.get("status") == "Withdrawn")
    pl = lambda n, w: f"{n} {w}" + ("" if n == 1 else "s")  # noqa: E731
    bullets.append(f"<b>{pl(c.get('new', 0), 'new filing')}, {pl(c.get('decisions', 0), 'decision')}</b> ({apr} approved, "
                   f"{len(dn)} denied, {wd} withdrawn) and <b>{pl(c.get('hearing_updates', 0), 'hearing update')}</b> in the last 7 days.")

    def big(lst):
        lst = [x for x in lst if x[1].get("acres") is not None]
        return max(lst, key=lambda x: x[1]["acres"]) if lst else None

    def describe(r, g):
        ac = max((x.get("acres") or 0 for x in g or [r]), default=0) or r.get("acres")
        bits = [f"<b>{esc(title_of(r))}</b> ({esc(fac(ac))}, {esc(r.get('jurisdiction'))})"]
        req = request_of(r)
        if req:
            bits.append(esc(req))
        if r.get("applicant"):
            bits.append("applicant " + esc(r["applicant"][:90]))
        return ": ".join(bits[:2]) + (f"; {bits[2]}" if len(bits) > 2 else "")

    b = big([x for x in week if x[0] == "new"])
    if b:
        bullets.append("Largest new filing: " + describe(b[1], b[2]) + ".")
    b = big([x for x in week if x[0] == "change" and x[1].get("status") == "Approved"])
    if b:
        bullets.append("Largest approval: " + describe(b[1], b[2]) + ".")
    if dn:
        bullets.append("Denied: " + "; ".join(f"<b>{esc(title_of(r))}</b> ({esc(r.get('jurisdiction'))}"
                                               f"{', ' + esc(fac(r.get('acres'))) if r.get('acres') else ''})" for r in dn[:3]) + ".")
    nxt = [g for g in groups if g[0]["next_hearing"]["date"] <= wk7]
    if nxt:
        bg = max(nxt, key=lambda g: max((x.get("acres") or 0) for x in g))
        r = bg[0]
        bullets.append(f"<b>{len(nxt)} hearings in the next 7 days</b>. Biggest: <b>{esc(title_of(r))}</b> "
                       f"({esc(fac(max((x.get('acres') or 0) for x in bg)))}, {esc(r.get('jurisdiction'))}) at "
                       f"{esc(r['next_hearing'].get('body'))} on {esc(fday(r['next_hearing']['date']))}.")
    if st_groups:
        r = st_groups[0][0]
        bullets.append(f"Longest delay: <b>{esc(title_of(r))}</b> ({esc(r.get('jurisdiction'))}) has been continued "
                       f"{n_continued(r)} times. See Coming up for all {pl(len(st_groups), 'stalled case')}.")

    pend = [r for r in recs if r.get("status") in OPEN and r.get("acres")]
    pend.sort(key=lambda r: -r["acres"])
    tops, seen = [], set()
    for r in pend:
        k = (r.get("jurisdiction"), norm_key(r))
        pids = set(r.get("parcel_ids") or [])
        if k in seen or any(t.get("jurisdiction") == r.get("jurisdiction") and
                            (abs(t["acres"] - r["acres"]) <= 0.02 * t["acres"] or pids & set(t.get("parcel_ids") or []))
                            for t in tops):
            continue
        seen.add(k)
        tops.append(r)
        if len(tops) == 3:
            break
    big_html = "".join(
        f'<div class="big-item"><div class="big-n">{esc(fac(r["acres"]))}</div>'
        f'<div class="big-t">{esc(title_of(r))}</div>'
        f'<div class="big-m">{esc(r.get("jurisdiction"))} &middot; {esc(r.get("category"))}<br>'
        + (f'Next hearing {esc(fday(r["next_hearing"]["date"], False))}, {esc(r["next_hearing"].get("body"))}'
           if r.get("next_hearing") else esc({"Filed": "Filed, no hearing set yet", "Continued": "Continued, no new date yet", "Hearing Scheduled": "Hearing scheduled, date not posted"}.get(r["status"], r["status"])))
        + (f'<br><a href="{esc(r.get("url"))}">View application</a>' if r.get("url") else "") + "</div></div>"
        for r in tops)
    glance = ('<div class="glance"><div class="g-h">This week at a glance</div><ul>'
              + "".join(f"<li>{x}</li>" for x in bullets) + "</ul>"
              + (f'<div class="g-h">Largest pending cases</div><div class="big">{big_html}</div>' if tops else "")
              + "</div>")
    stats = {"hearings_21d": len(groups), "stalled": len(st_groups), "denied_90d": len(den_groups)}
    return glance.replace("\u2014", ", "), coming.replace("\u2014", ", "), stats


EXTRA_CSS = """.wrap td{overflow-wrap:anywhere}
@media (max-width:560px){.big-item{flex-basis:100%}}
.views{max-width:680px;margin:14px auto 0;padding:0 12px;display:flex;gap:8px}
.views button{flex:1;appearance:none;border:1px solid var(--line);background:#fff;color:var(--muted);font:700 14px Arial,Helvetica,sans-serif;padding:10px 8px;border-radius:999px;cursor:pointer}
.views button[aria-selected="true"]{background:var(--ink);color:#fff;border-color:var(--ink)}
.views button:focus-visible{outline:3px solid var(--red);outline-offset:2px}
.views small{font-weight:400;opacity:.85}
.glance{margin-top:16px;padding-top:14px;border-top:1px solid #eaecf0;text-align:left;font:14px/1.5 Arial,Helvetica,sans-serif;color:var(--ink)}
.glance ul{margin:6px 0 4px;padding-left:20px}.glance li{margin:6px 0}
.g-h{font:700 11px Arial,Helvetica,sans-serif;color:#98a2b3;letter-spacing:.1em;text-transform:uppercase;margin-top:8px}
.big{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
.big-item{flex:1 1 170px;min-width:0;border:1px solid var(--line);border-radius:10px;padding:10px 12px;background:#fafbfc}
.big-n{font:800 22px Arial,Helvetica,sans-serif;color:var(--ink)}
.big-t{font-weight:700;margin-top:2px;overflow-wrap:anywhere}
.big-m{font-size:12px;color:var(--muted);margin-top:4px}
.big-m a,.cu-link a{color:#1f4fb5;font-weight:700}
.cu-wrap{max-width:680px;margin:0 auto;padding:16px 12px 32px;font:14px/1.45 Arial,Helvetica,sans-serif;color:var(--ink)}
.cu-head{background:#fff;border:1px solid var(--line);border-top:6px solid #ef1f25;border-radius:10px;padding:18px 20px}
.cu-kicker{font:700 12px Arial,Helvetica,sans-serif;color:#ef1f25;letter-spacing:.1em;text-transform:uppercase}
.cu-h1{font:800 26px Arial,Helvetica,sans-serif;margin-top:6px}
.cu-sub{color:var(--muted);font-size:13px;margin-top:4px}
.cu-sec{margin-top:26px}.cu-sec h2{font:800 20px Arial,Helvetica,sans-serif;margin:0}
.cu-day{display:flex;justify-content:space-between;align-items:center;margin:16px 0 4px;padding:6px 10px;background:#e4e7ec;border-radius:6px;font-weight:700}
.cu-count{font:700 12px Arial,Helvetica,sans-serif;color:var(--muted)}
.cu-row{background:#fff;border:1px solid var(--line);border-left:4px solid #9a5b00;border-radius:8px;padding:10px 12px;margin:8px 0}
.cu-meta{font-size:12px;color:var(--muted)}
.cu-title{font-weight:800;font-size:16px;margin-top:2px;overflow-wrap:anywhere}
.cu-chips{margin:6px 0 2px}
.cu-chip{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:1px 8px;font-size:12px;color:#344054;margin:2px 4px 2px 0;background:#f9fafb}
.cu-line{font-size:13px;margin-top:3px;overflow-wrap:anywhere}.cu-line b{color:var(--muted);font-size:12px;margin-right:4px}
.cu-link{font-size:13px;margin-top:6px}
.cu-empty{color:var(--muted);margin-top:8px}
"""

VIEW_JS = r"""<script type="text/x-elc-after">
(function(){
  function views(panel){ return {cases: panel.querySelector('.view-cases'), coming: panel.querySelector('.view-coming')}; }
  function setView(id, v, scroll){
    var panel = document.getElementById(id); if (!panel) return;
    var w = views(panel); if (!w.coming) return;
    w.cases.hidden = (v === 'coming'); w.coming.hidden = (v !== 'coming');
    panel.querySelectorAll('.views button').forEach(function(b){ b.setAttribute('aria-selected', b.dataset.view === v ? 'true' : 'false'); });
    if (window.__elcRefresh) window.__elcRefresh();
    if (scroll) window.scrollTo(0, 0);
  }
  document.querySelectorAll('.views button').forEach(function(b){
    b.addEventListener('click', function(){ var id = b.closest('.wrap').id; setView(id, b.dataset.view, true);
      try { history.replaceState(null, '', '#' + id + (b.dataset.view === 'coming' ? '-coming' : '')); } catch(e) {} });
  });
  function fromHash(){ var h = (location.hash || '').replace('#',''); var m = h.match(/^(tampa|orlando)-coming$/);
    if (m) setView(m[1], 'coming', false); }
  fromHash(); window.addEventListener('hashchange', fromHash);
})();
</script>
"""
