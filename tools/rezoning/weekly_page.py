#!/usr/bin/env python3
"""Build the weekly advisors page (Tampa and Orlando tabs) and the short advisors email.

Run after team_email.py has been run with --market tampa and --market orlando:
  python3 weekly_page.py --out-dir ~/rz/out --run-date 2026-11-02

Writes:
  out/weekly-page-<date>.html      page content for the permanent Claude page (no doctype; the Artifact tool wraps it)
  out/advisors-email-<date>.json   {"subject", "body", "htmlBody", "counts"} for the Gmail send
Prints "done: ..." at the end. No em dashes anywhere.
"""
import argparse, collections, html, json, os, re, sys, datetime as dt

PAGE_URL = "https://claude.ai/artifact/CqhHWm29kub8AwrBFLfCfp"   # permanent link, shared as "anyone with the link"
MAP_URL = "https://sheltr12.github.io/Eshenbaugh-Land-Company/rezonings_map.html"
MARKETS = [("tampa", "Tampa", ""), ("orlando", "Orlando", "Includes Lake County. ")]

HEAD = '<title>ELC Weekly Rezoning Update</title>\n<meta name="robots" content="noindex,nofollow">\n<style>\n:root{--bg:#f2f4f7;--card:#ffffff;--ink:#101828;--muted:#475467;--line:#d0d5dd;--red:#c4161c;color-scheme:light}\nbody{margin:0;padding:0;background:var(--bg);color:var(--ink);font-family:Arial,Helvetica,sans-serif}\n.bar{position:sticky;top:env(safe-area-inset-top,0px);z-index:5;background:var(--bg);border-bottom:1px solid var(--line)}\nbody::before{content:"";position:fixed;inset:0 0 auto;height:env(safe-area-inset-top,0px);z-index:10;background:var(--bg)}\n.tabs{max-width:680px;margin:0 auto;display:flex;gap:8px;padding:12px 12px 0}\n.tab{flex:1;appearance:none;border:1px solid var(--line);border-bottom:none;background:#e4e7ec;color:var(--muted);font:700 15px Arial,Helvetica,sans-serif;padding:12px 8px;border-radius:10px 10px 0 0;cursor:pointer}\n.tab small{display:block;font-weight:400;font-size:12px;margin-top:3px}\n.tab[aria-selected="true"]{background:var(--card);color:var(--red);border-color:var(--line);box-shadow:inset 0 3px 0 var(--red)}\n.tab:focus-visible{outline:3px solid var(--red);outline-offset:2px}\n.wrap{overflow-x:hidden}\n.jump{cursor:pointer;transition:transform .12s ease,box-shadow .12s ease}\n.jump:hover{transform:translateY(-2px);box-shadow:0 4px 10px rgba(16,24,40,.12)}\n.jump:focus-visible{outline:3px solid var(--red);outline-offset:2px}\n.jump-zero{opacity:.55}\n.sec-target{scroll-margin-top:96px}\n@media (prefers-reduced-motion:reduce){.jump{transition:none}.jump:hover{transform:none}}\n</style>\n<div class="bar"><div class="tabs" role="tablist" aria-label="Market">\n<button class="tab" role="tab" id="tab-tampa" aria-controls="tampa" aria-selected="true">Tampa<small>Tampa Bay, Nature Coast, SW Florida</small></button>\n<button class="tab" role="tab" id="tab-orlando" aria-controls="orlando" aria-selected="false">Orlando<small>Orlando and Central Florida, incl. Lake County</small></button>\n</div></div>\n'
TAIL = "<script>\n(function(){\n  const ids=['tampa','orlando'];\n  function show(id,push){ if(!ids.includes(id)) id='tampa';\n    ids.forEach(k=>{document.getElementById(k).hidden=(k!==id);document.getElementById('tab-'+k).setAttribute('aria-selected',k===id?'true':'false');});\n    if(push){try{history.replaceState(null,'','#'+id)}catch(e){}} }\n  ids.forEach(k=>document.getElementById('tab-'+k).addEventListener('click',()=>{show(k,true);window.scrollTo(0,0);}));\n  show((location.hash||'').replace('#',''),false);\n  window.addEventListener('hashchange',()=>show(location.hash.replace('#',''),false));\n})();\n</script>\n<script>\n(function(){\n  const labels=['approved','denied','withdrawn','hearing updates','new filings'];\n  const reduce=window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches;\n  ['tampa','orlando'].forEach(function(id){\n    const panel=document.getElementById(id); if(!panel) return;\n    const heads={};\n    panel.querySelectorAll('span').forEach(function(s){\n      const st=s.getAttribute('style')||'';\n      if(st.indexOf('font-size:21px')<0) return;\n      const key=s.textContent.trim().toLowerCase();\n      if(labels.indexOf(key)<0||heads[key]) return;\n      const t=s.closest('table')||s; t.classList.add('sec-target'); heads[key]=t;\n    });\n    panel.querySelectorAll('td').forEach(function(td){\n      const divs=td.querySelectorAll(':scope > div'); if(divs.length!==2) return;\n      const key=divs[1].textContent.trim().toLowerCase(); if(labels.indexOf(key)<0) return;\n      const target=heads[key];\n      if(!target){ td.classList.add('jump-zero'); td.title='None this week'; return; }\n      td.classList.add('jump'); td.setAttribute('role','button'); td.setAttribute('tabindex','0');\n      td.setAttribute('aria-label','Jump to '+divs[1].textContent.trim());\n      const go=function(){ target.scrollIntoView({behavior:reduce?'auto':'smooth',block:'start'}); };\n      td.addEventListener('click',go);\n      td.addEventListener('keydown',function(e){ if(e.key==='Enter'||e.key===' '){ e.preventDefault(); go(); } });\n    });\n  });\n})();\n</script>\n"


def body_of(path):
    h = open(path, encoding="utf-8").read()
    m = re.search(r"<body[^>]*>(.*)</body>", h, re.S)
    if not m:
        sys.exit(f"no <body> in {path}")
    if re.search(r"<style", h, re.I) or "Who is behind it" not in h and "nothing" not in h.lower():
        print(f"warning: {path} may not be the approved card design", file=sys.stderr)
    return m.group(1)


def market_counts(js):
    st = collections.Counter()
    for it in js.get("items") or []:
        if it.get("kind") == "change":
            n = len(it["cases"]) if isinstance(it.get("cases"), list) else 1
            st[it.get("status")] += n
    c = dict(js.get("counts") or {})
    c.update({"approved": st.get("Approved", 0), "denied": st.get("Denied", 0), "withdrawn": st.get("Withdrawn", 0)})
    for k in ("decisions", "new", "hearing_updates"):
        c.setdefault(k, 0)
    return c


def plural(n, one, many):
    return f"{n} {one if n == 1 else many}"


def line(c):
    if not (c["decisions"] or c["new"] or c["hearing_updates"]):
        return "No decisions, hearing updates or new filings this week"
    return (f'{plural(c["decisions"], "decision", "decisions")} ({c["approved"]} approved, {c["denied"]} denied, '
            f'{c["withdrawn"]} withdrawn), {plural(c["new"], "new filing", "new filings")}, '
            f'{plural(c["hearing_updates"], "hearing update", "hearing updates")}')


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--run-date", required=True)
    a = ap.parse_args(argv)
    od, d = os.path.expanduser(a.out_dir), a.run_date
    week = dt.date.fromisoformat(d).strftime("%b %-d, %Y")
    bodies, counts = {}, {}
    for key, _, _ in MARKETS:
        hp = os.path.join(od, f"team-email-{d}-{key}.html")
        jp = os.path.join(od, f"team-email-{d}-{key}.json")
        if not (os.path.exists(hp) and os.path.exists(jp)):
            sys.exit(f"missing {hp} or {jp}: run team_email.py --market {key} first")
        bodies[key] = body_of(hp)
        counts[key] = market_counts(json.load(open(jp)))

    page = (HEAD + '<div class="wrap" id="tampa" role="tabpanel" aria-labelledby="tab-tampa">'
            + bodies["tampa"] + "</div>\n"
            + '<div class="wrap" id="orlando" role="tabpanel" aria-labelledby="tab-orlando" hidden>'
            + bodies["orlando"] + "</div>\n" + TAIL)
    page = page.replace("\u2014", ", ")
    pp = os.path.join(od, f"weekly-page-{d}.html")
    open(pp, "w", encoding="utf-8").write(page)

    tot = {k: sum(counts[m][k] for m in counts) for k in ("decisions", "new", "hearing_updates")}
    subject = (f'Rezoning Update, week of {week}: {plural(tot["decisions"], "decision", "decisions")}, '
               f'{plural(tot["new"], "new filing", "new filings")}, {plural(tot["hearing_updates"], "hearing update", "hearing updates")}')
    txt = [f"ELC Rezoning Update, week of {week}", ""]
    blocks = []
    for key, name, note in MARKETS:
        c = counts[key]
        txt += [f"{name}: {note}{line(c)}", f"{PAGE_URL}#{key}", ""]
        blocks.append(
            f'<table cellpadding="0" cellspacing="0" style="margin:18px 0 0"><tr><td style="border:2px solid #c4161c;'
            f'border-radius:6px;padding:10px 16px;background:#fff5f5"><a href="{PAGE_URL}#{key}" style="color:#c4161c;'
            f'font-weight:700;font-size:15px;text-decoration:underline">Open {name} update</a></td></tr></table>'
            f'<p style="margin:6px 0 0;color:#475467">{html.escape(note + line(c))}</p>')
    txt += ["Each tab has one card per application showing The request and Who is behind it. "
            "Contacts tagged \"not the owner\" are engineers, attorneys, planners or other representatives.", "",
            f"Live map: {MAP_URL}"]
    html_body = (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#101828;max-width:620px">'
        '<div style="font-size:12px;font-weight:700;color:#c4161c;letter-spacing:.1em;text-transform:uppercase">Eshenbaugh Land Company</div>'
        '<div style="font-size:22px;font-weight:800;margin-top:6px">Rezoning Update</div>'
        f'<div style="color:#475467;margin-top:4px">Week of {week}</div>'
        '<p style="margin:16px 0 0">This week\'s decisions, hearing updates and new filings, with one card per application '
        'showing The request and Who is behind it. Contacts tagged "not the owner" are engineers, attorneys, planners or other representatives.</p>'
        + "".join(blocks) +
        f'<p style="margin:20px 0 0"><a href="{MAP_URL}" style="color:#c4161c;font-weight:700;text-decoration:underline">Live map</a></p>'
        '</div>')
    out = {"subject": subject, "body": "\n".join(txt), "htmlBody": html_body, "counts": counts, "page_url": PAGE_URL}
    for k in ("subject", "body", "htmlBody"):
        out[k] = out[k].replace("\u2014", ", ")
    ep = os.path.join(od, f"advisors-email-{d}.json")
    json.dump(out, open(ep, "w", encoding="utf-8"), indent=1)
    print(f"done: {pp} ({len(page):,} bytes), {ep}; subject={subject!r}")


if __name__ == "__main__":
    main()
