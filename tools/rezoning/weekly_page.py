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

HEAD = '<title>ELC Weekly Rezoning Update</title>\n<meta name="robots" content="noindex,nofollow">\n<style>\n:root{--bg:#f2f4f7;--card:#ffffff;--ink:#101828;--muted:#475467;--line:#d0d5dd;--red:#c4161c;color-scheme:light}\nbody{margin:0;padding:0;background:var(--bg);color:var(--ink);font-family:Arial,Helvetica,sans-serif}\n.bar{position:sticky;top:env(safe-area-inset-top,0px);z-index:5;background:var(--bg);border-bottom:1px solid var(--line)}\nbody::before{content:"";position:fixed;inset:0 0 auto;height:env(safe-area-inset-top,0px);z-index:10;background:var(--bg)}\n.tabs{max-width:680px;margin:0 auto;display:flex;gap:8px;padding:12px 12px 0}\n.tab{flex:1;appearance:none;border:1px solid var(--line);border-bottom:none;background:#e4e7ec;color:var(--muted);font:700 15px Arial,Helvetica,sans-serif;padding:12px 8px;border-radius:10px 10px 0 0;cursor:pointer}\n.tab small{display:block;font-weight:400;font-size:12px;margin-top:3px}\n.tab[aria-selected="true"]{background:var(--card);color:var(--red);border-color:var(--line);box-shadow:inset 0 3px 0 var(--red)}\n.tab:focus-visible{outline:3px solid var(--red);outline-offset:2px}\n.wrap{overflow-x:clip}\n.jump{cursor:pointer;transition:transform .12s ease,box-shadow .12s ease}\n.jump:hover{transform:translateY(-2px);box-shadow:0 4px 10px rgba(16,24,40,.12)}\n.jump:focus-visible{outline:3px solid var(--red);outline-offset:2px}\n.jump-zero{opacity:.55}\n.sec-target{scroll-margin-top:96px}\n@media (prefers-reduced-motion:reduce){.jump{transition:none}.jump:hover{transform:none}}\n</style>\n<div class="bar"><div class="tabs" role="tablist" aria-label="Market">\n<button class="tab" role="tab" id="tab-tampa" aria-controls="tampa" aria-selected="true">Tampa<small>Tampa Bay, Nature Coast, SW Florida</small></button>\n<button class="tab" role="tab" id="tab-orlando" aria-controls="orlando" aria-selected="false">Orlando<small>Orlando and Central Florida, incl. Lake County</small></button>\n</div></div>\n'
TAIL = "<script>\n(function(){\n  const ids=['tampa','orlando'];\n  function show(id,push){ if(!ids.includes(id)) id='tampa';\n    ids.forEach(k=>{document.getElementById(k).hidden=(k!==id);document.getElementById('tab-'+k).setAttribute('aria-selected',k===id?'true':'false');});\n    if(push){try{history.replaceState(null,'','#'+id)}catch(e){}} }\n  ids.forEach(k=>document.getElementById('tab-'+k).addEventListener('click',()=>{show(k,true);window.scrollTo(0,0);}));\n  show((location.hash||'').replace('#',''),false);\n  window.addEventListener('hashchange',()=>show(location.hash.replace('#',''),false));\n})();\n</script>\n<script>\n(function(){\n  const labels=['approved','denied','withdrawn','hearing updates','new filings'];\n  const reduce=window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches;\n  ['tampa','orlando'].forEach(function(id){\n    const panel=document.getElementById(id); if(!panel) return;\n    const heads={};\n    panel.querySelectorAll('span').forEach(function(s){\n      const st=s.getAttribute('style')||'';\n      if(st.indexOf('font-size:21px')<0) return;\n      const key=s.textContent.trim().toLowerCase();\n      if(labels.indexOf(key)<0||heads[key]) return;\n      const t=s.closest('table')||s; t.classList.add('sec-target'); heads[key]=t;\n    });\n    panel.querySelectorAll('td').forEach(function(td){\n      const divs=td.querySelectorAll(':scope > div'); if(divs.length!==2) return;\n      const key=divs[1].textContent.trim().toLowerCase(); if(labels.indexOf(key)<0) return;\n      const target=heads[key];\n      if(!target){ td.classList.add('jump-zero'); td.title='None this week'; return; }\n      td.classList.add('jump'); td.setAttribute('role','button'); td.setAttribute('tabindex','0');\n      td.setAttribute('aria-label','Jump to '+divs[1].textContent.trim());\n      const go=function(){ target.scrollIntoView({behavior:reduce?'auto':'smooth',block:'start'}); };\n      td.addEventListener('click',go);\n      td.addEventListener('keydown',function(e){ if(e.key==='Enter'||e.key===' '){ e.preventDefault(); go(); } });\n    });\n  });\n})();\n</script>\n"

FILTER_CSS = """.filter{max-width:680px;margin:0 auto;padding:10px 12px 10px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;background:var(--card);border:1px solid var(--line);border-top:none}
.filter label{font:700 12px Arial,Helvetica,sans-serif;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
.filter select{flex:1;min-width:0;font:15px Arial,Helvetica,sans-serif;color:var(--ink);background:#fff;border:1px solid #98a2b3;border-radius:6px;padding:8px 10px}
.filter select:focus-visible{outline:3px solid var(--red);outline-offset:1px}
.filter button{font:700 13px Arial,Helvetica,sans-serif;color:var(--red);background:none;border:none;text-decoration:underline;cursor:pointer;padding:6px 2px}
.filter .note{flex-basis:100%;font:12px Arial,Helvetica,sans-serif;color:var(--muted)}
.bar .tabs{padding-bottom:0}
"""

FILTER_HTML = """<div class="filter"><label for="jfilter">Show</label>
<select id="jfilter" aria-label="Filter by county or city"></select>
<button type="button" id="jclear" hidden>Show all</button>
<div class="note" id="jnote" aria-live="polite"></div></div>
"""

FILTER_JS = """<script>
(function(){
  var COUNTY = __COUNTY_MAP__;
  var sel = document.getElementById('jfilter'), clr = document.getElementById('jclear'), note = document.getElementById('jnote');
  var bar = document.querySelector('.bar');
  var state = {tampa:'', orlando:''};
  function setMargin(){ var h = (bar ? bar.getBoundingClientRect().height : 90) + 12;
    document.querySelectorAll('.sec-target').forEach(function(t){ t.style.scrollMarginTop = h + 'px'; }); }
  function active(){ var o = document.getElementById('orlando'); return (o && !o.hidden) ? 'orlando' : 'tampa'; }
  function cardsOf(panel){ return Array.prototype.filter.call(panel.querySelectorAll('table'), function(t){
      return (t.getAttribute('style') || '').indexOf('border-left:6px solid') >= 0; }); }
  function jurOf(card){ var d = Array.prototype.find.call(card.querySelectorAll('div'), function(x){
      return (x.getAttribute('style') || '').indexOf('line-height:1.5') >= 0 && x.textContent.indexOf('\\u00b7') >= 0; });
    return d ? d.textContent.split('\\u00b7')[0].replace(/\\s+/g,' ').trim() : ''; }
  function casesOf(card){ var m = card.textContent.match(/Case #/g); return m ? m.length : 1; }
  function countyOf(j){ return COUNTY[j] || j.replace(/^City of /,'').replace(/ County$/,''); }
  function matches(card, f){ if(!f) return true; var j = card.dataset.jur;
    return f.indexOf('county:') === 0 ? countyOf(j) === f.slice(7) : j === f; }
  function build(id){
    var panel = document.getElementById(id), cards = cardsOf(panel), byCounty = {};
    cards.forEach(function(c){ c.dataset.jur = c.dataset.jur || jurOf(c); var j = c.dataset.jur, k = countyOf(j);
      byCounty[k] = byCounty[k] || {}; byCounty[k][j] = (byCounty[k][j] || 0) + casesOf(c); });
    var total = cards.reduce(function(a,c){ return a + casesOf(c); }, 0);
    var h = '<option value="">All counties and cities (' + total + ')</option>';
    Object.keys(byCounty).sort().forEach(function(k){
      var js = Object.keys(byCounty[k]).sort(function(a,b){ return (/County$/.test(b)) - (/County$/.test(a)) || a.localeCompare(b); });
      var n = js.reduce(function(a,j){ return a + byCounty[k][j]; }, 0);
      h += '<optgroup label="' + k + ' County">';
      h += '<option value="county:' + k + '">All of ' + k + ' County (' + n + ')</option>';
      if (js.length > 1 || js[0] !== k + ' County') js.forEach(function(j){
        h += '<option value="' + j.replace(/"/g,'&quot;') + '">' + (j === k + ' County' ? 'Unincorporated ' + j : j) + ' (' + byCounty[k][j] + ')</option>'; });
      h += '</optgroup>';
    });
    sel.innerHTML = h; sel.value = state[id] || '';
  }
  function apply(){
    var id = active(), panel = document.getElementById(id), f = state[id] || '', cards = cardsOf(panel);
    cards.forEach(function(c){ var show = matches(c, f); c.hidden = !show;
      var sp = c.nextElementSibling; if (sp && sp.tagName === 'TABLE' && !(sp.getAttribute('style') || '').length && sp.textContent.trim() === '') sp.hidden = !show; });
    var counts = {};
    panel.querySelectorAll('.sec-target').forEach(function(head){
      var key = head.textContent.replace(/\\d+ cases?/,'').trim().split(/\\s{2,}|\\n/)[0].trim().toLowerCase(), n = 0, el = head.nextElementSibling;
      while (el && !el.classList.contains('sec-target')) {
        if (el.tagName === 'TABLE' && (el.getAttribute('style') || '').indexOf('border-left:6px solid') >= 0 && !el.hidden) n += casesOf(el);
        el = el.nextElementSibling; }
      counts[key] = n; head.hidden = (n === 0);
      var sub = head.querySelector('span:nth-of-type(2)'); if (sub) sub.innerHTML = '&nbsp;&nbsp;' + n + (n === 1 ? ' case' : ' cases');
      var prev = head.previousElementSibling; if (prev && prev.tagName === 'TABLE' && prev.textContent.trim() === '') prev.hidden = (n === 0);
    });
    panel.querySelectorAll('td.jump, td.jump-zero').forEach(function(td){
      var d = td.querySelectorAll(':scope > div'); if (d.length !== 2) return;
      var key = d[1].textContent.trim().toLowerCase(), n = counts[key] || 0;
      d[0].textContent = n; td.classList.toggle('jump-zero', n === 0); td.classList.toggle('jump', n > 0);
      td.title = n === 0 ? 'None for this selection' : ''; td.tabIndex = n === 0 ? -1 : 0; });
    clr.hidden = !f;
    note.textContent = f ? 'Showing ' + sel.options[sel.selectedIndex].text.replace(/ \\(\\d+\\)$/,'') + ' only. Counts above reflect this filter.' : '';
    setMargin();
  }
  sel.addEventListener('change', function(){ state[active()] = sel.value; apply(); window.scrollTo(0,0); });
  clr.addEventListener('click', function(){ state[active()] = ''; sel.value = ''; apply(); });
  document.querySelectorAll('.tab').forEach(function(t){ t.addEventListener('click', function(){ build(active()); apply(); }); });
  window.addEventListener('hashchange', function(){ build(active()); apply(); });
  window.addEventListener('resize', setMargin);
  build(active()); apply();
})();
</script>
"""

# One color per section, used for the sticky section bar, each card's ribbon, border and tint.
SECTION_COLORS = {"approved": ("#146c2e", "#f1f9f3", "#b7dfc1", "Approved"),
                  "denied": ("#b42318", "#fdf3f2", "#f2c4bf", "Denied"),
                  "withdrawn": ("#475467", "#f4f5f7", "#cfd4dc", "Withdrawn"),
                  "hearing updates": ("#9a5b00", "#fff8ec", "#efd29b", "Hearing update"),
                  "new filings": ("#1f4fb5", "#f2f6fe", "#bcd0f5", "New filing")}


def section_css():
    out = [".sec-head{position:sticky;top:var(--barh,96px);z-index:4}",
           ".sec-head>tbody>tr>td,.sec-head>tr>td{background:var(--sc)!important;border-bottom:none!important;"
           "padding:10px 14px!important;border-radius:8px;box-shadow:0 2px 6px rgba(16,24,40,.15)}",
           ".sec-head span,.sec-head div{color:#fff!important}",
           ".sec-card{background:var(--st)!important;border-color:var(--sb)!important;border-left-color:var(--sc)!important}",
           ".sec-ribbon{margin:-18px -20px 12px;padding:6px 20px;background:var(--sc);color:#fff;font:700 11px Arial,Helvetica,sans-serif;"
           "letter-spacing:.12em;text-transform:uppercase;border-radius:0 9px 0 0}",
           ".pill-neutral{background:#fff!important;color:#344054!important;border-color:#98a2b3!important}"]
    for k, (c, t, b, _) in SECTION_COLORS.items():
        cls = k.replace(" ", "-")
        out.append(f".sc-{cls}{{--sc:{c};--st:{t};--sb:{b}}}")
    return "\n".join(out) + "\n"


SECTION_JS = """<script>
(function(){
  var LABEL = __SECTION_LABELS__;
  var bar = document.querySelector('.bar');
  function setBar(){ var h = bar ? Math.round(bar.getBoundingClientRect().height) : 96;
    document.documentElement.style.setProperty('--barh', h + 'px'); }
  ['tampa','orlando'].forEach(function(id){
    var panel = document.getElementById(id); if (!panel) return;
    panel.querySelectorAll('.sec-target').forEach(function(head){
      var key = head.textContent.replace(/\\d+ cases?/,'').trim().split(/\\s{2,}|\\n/)[0].trim().toLowerCase();
      if (!LABEL[key]) return;
      var cls = 'sc-' + key.replace(/ /g,'-');
      head.classList.add('sec-head', cls);
      var el = head.nextElementSibling;
      while (el && !el.classList.contains('sec-target')) {
        if (el.tagName === 'TABLE' && (el.getAttribute('style') || '').indexOf('border-left:6px solid') >= 0) {
          el.classList.add('sec-card', cls);
          var td = el.querySelector('td');
          if (td && !td.querySelector('.sec-ribbon')) {
            var r = document.createElement('div'); r.className = 'sec-ribbon'; r.textContent = LABEL[key];
            td.insertBefore(r, td.firstChild); }
          if (key === 'hearing updates' || key === 'new filings')
            el.querySelectorAll('span').forEach(function(sp){
              if ((sp.getAttribute('style') || '').indexOf('border-radius:999px') >= 0) sp.classList.add('pill-neutral'); });
        }
        el = el.nextElementSibling; }
    });
  });
  setBar();
  window.addEventListener('resize', setBar);
  if (window.ResizeObserver && bar) new ResizeObserver(setBar).observe(bar);
})();
</script>
"""


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
    county_map = {}
    try:
        for r in json.load(open(os.path.join(od, "rezonings.json")))["records"]:
            if r.get("jurisdiction") and r.get("county"):
                county_map[r["jurisdiction"]] = r["county"]
    except Exception as e:  # noqa: BLE001
        print(f"warning: no county map ({e}); cities will be grouped by name", file=sys.stderr)
    page = page.replace("</style>", FILTER_CSS + "</style>", 1)
    page = page.replace('</div></div>\n<div class="wrap" id="tampa"', '</div>' + FILTER_HTML + '</div>\n<div class="wrap" id="tampa"', 1)
    if FILTER_HTML not in page:
        sys.exit("could not place the county filter in the page")
    page = page.replace("</style>", section_css() + "</style>", 1)
    page += SECTION_JS.replace("__SECTION_LABELS__", json.dumps({k: v[3] for k, v in SECTION_COLORS.items()}))
    page += FILTER_JS.replace("__COUNTY_MAP__", json.dumps(county_map, sort_keys=True))
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
