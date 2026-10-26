"""The /runway page: one screen, plain words, refreshes itself. The numbers come from app/runway.py (the same ones the Slack report uses)."""
from __future__ import annotations

import tomllib
from datetime import datetime, timezone
from html import escape

from . import client, db
from .runway import _targets, numbers, recommendation

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#14181f;--mute:#5d6675;--line:#e3e6eb;--blue:#2457d6;--green:#1f8a4c;--amber:#b7791f;--red:#c0392b;--bar:#e9edf3}
@media (prefers-color-scheme:dark){:root{--bg:#0f1218;--card:#171c25;--ink:#e8ecf2;--mute:#9aa4b5;--line:#262d3a;--blue:#6e9bff;--green:#4cc27f;--amber:#e0a84a;--red:#ef7b6e;--bar:#232a37}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:1040px;margin:0 auto;padding:20px 16px 60px}
h1{font-size:22px;margin:0 0 2px}h2{font-size:14px;text-transform:uppercase;letter-spacing:.06em;color:var(--mute);margin:30px 0 10px}
.sub{color:var(--mute);font-size:14px}
.hero{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px 20px;margin:16px 0}
.hero .big{font-size:26px;font-weight:700;line-height:1.25}
.pill{display:inline-block;border-radius:99px;padding:3px 12px;font-size:13px;font-weight:600;margin-top:10px}
.pill.ok{background:rgba(31,138,76,.14);color:var(--green)}.pill.warn{background:rgba(183,121,31,.16);color:var(--amber)}.pill.bad{background:rgba(192,57,43,.14);color:var(--red)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px}
.card .n{font-size:30px;font-weight:700;line-height:1.1}.card .l{font-size:14px;margin-top:2px}.card .h{font-size:13px;margin-top:6px;color:var(--mute)}
.row{display:grid;grid-template-columns:minmax(160px,260px) 1fr 130px;gap:12px;align-items:center;margin:10px 0}
.row .name{font-size:14px}.row .val{font-size:14px;text-align:right;font-variant-numeric:tabular-nums}
.bar{background:var(--bar);border-radius:99px;height:14px;overflow:hidden}.bar>i{display:block;height:100%;border-radius:99px;background:var(--blue)}
.bar.g>i{background:var(--green)}.bar.a>i{background:var(--amber)}.bar.r>i{background:var(--red)}
.panel{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:8px 18px}
ul.plain{margin:8px 0;padding-left:20px}ul.plain li{margin:8px 0}
table{width:100%;border-collapse:collapse;font-size:14px;font-variant-numeric:tabular-nums}
th{text-align:left;color:var(--mute);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.05em;padding:8px 6px;border-bottom:1px solid var(--line)}
td{padding:8px 6px;border-bottom:1px solid var(--line)}td.num,th.num{text-align:right}
.tag{font-size:12px;border-radius:6px;padding:1px 7px;margin-left:6px}.tag.g{background:rgba(31,138,76,.14);color:var(--green)}.tag.r{background:rgba(192,57,43,.14);color:var(--red)}
.foot{color:var(--mute);font-size:13px;margin-top:26px}
@media (max-width:640px){.row{grid-template-columns:1fr;gap:4px}.row .val{text-align:left}.hero .big{font-size:22px}}
"""


def _pct(a: float, b: float) -> int:
    return round(100 * a / b) if b else 0


def held_reasons() -> list[tuple[str, int]]:
    """Why contacts did not become leads, in plain words, biggest first."""
    with db.cursor() as cur:
        cur.execute("SELECT coalesce(drop_reason,'') d FROM contacts WHERE status='needs_review'")
        reasons = [r["d"] for r in cur.fetchall()]
    buckets: dict[str, int] = {}
    for d in reasons:
        if not d:
            continue
        if "Mimecast" in d or "gateway" in d:
            k = "Behind a mail security gateway (Mimecast or Barracuda), blocked on purpose"
        elif "already in another active" in d:
            k = "Already in another active EmailBison campaign"
        elif "chars >" in d:
            k = "Email came out longer than the cap, even after retries"
        elif "minimum" in d or "4 chars" in d:
            k = "Email came out too short or empty"
        elif "spam-trigger" in d:
            k = "Email kept using a spam-trigger word"
        elif "not a fit" in d:
            k = "Company judged not a fit"
        else:
            k = "Other copy rule (dashes, capital letters, off-brief text)"
        buckets[k] = buckets.get(k, 0) + 1
    return sorted(buckets.items(), key=lambda kv: -kv[1])


def _vertical_rows() -> list[dict]:
    with db.cursor() as cur:
        cur.execute("""SELECT s.vertical v, count(DISTINCT s.domain) sourced, count(DISTINCT s.domain) FILTER (WHERE s.found_at IS NOT NULL) searched,
                              count(DISTINCT c.company_domain) with_contacts
                       FROM staged_companies s LEFT JOIN contacts c ON c.company_domain = s.domain WHERE s.verdict='KEEP' GROUP BY 1 ORDER BY 2 DESC""")
        return [dict(r) for r in cur.fetchall()]


def _card(num: str, label: str, hint: str) -> str:
    return f'<div class="card"><div class="n">{num}</div><div class="l">{escape(label)}</div><div class="h">{escape(hint)}</div></div>'


def _row(name: str, val: float, of: float, text: str, cls: str = "") -> str:
    w = max(min(100 * val / of, 100), 0) if of else 0
    return f'<div class="row"><div class="name">{escape(name)}</div><div class="bar {cls}"><i style="width:{w:.1f}%"></i></div><div class="val">{escape(text)}</div></div>'


ADD_JS = """<script>
function tfpToken(){try{return localStorage.getItem('tfp_admin')||'';}catch(e){return '';}}
async function addLeads(id){
  const n=document.getElementById('n-'+id).value, msg=document.getElementById('m-'+id);
  let t=tfpToken();
  if(!t){t=prompt('Admin password (the ADMIN_TOKEN value in the Railway variables)')||'';if(!t){return;}}
  if(!confirm('Add '+Number(n).toLocaleString()+' leads to campaign '+id+'? It spends contact search and verification credits.')){return;}
  msg.textContent='working...';
  try{
    const r=await fetch('/api/add-leads',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+t},body:JSON.stringify({campaign:id,leads:Number(n)})});
    const j=await r.json().catch(function(){return {};});
    if(r.status===401){try{localStorage.removeItem('tfp_admin');}catch(e){} msg.textContent='Wrong password.';return;}
    try{localStorage.setItem('tfp_admin',t);}catch(e){}
    msg.textContent=j.message||('Error '+r.status);
  }catch(e){msg.textContent='Could not reach the service.';}
}
var lastTouch=0;document.addEventListener('change',function(){lastTouch=Date.now();});
setInterval(function(){if(Date.now()-lastTouch>180000){location.reload();}},60000);
</script>"""


def _controls_html() -> str:
    """One row per campaign with a 500 to 3,000 picker and an Add leads button. The button asks for the admin password (ADMIN_TOKEN) the first time."""
    try:
        from . import materials_fill
        rows = materials_fill.overview()
    except Exception:
        return ""
    if not rows:
        return ""
    opts = "".join(f'<option value="{v}">{v:,}</option>' for v in (500, 1000, 1500, 2000, 2500, 3000))
    body = "".join(
        f'<div class="row"><div class="name">{escape(str(r["name"]))} (campaign {r["campaign"]}{", copy test " + escape(str(r["angle"])) if r.get("angle") else ""}): '
        f'{r["loaded"]:,} loaded</div><div class="val" style="width:auto"><select id="n-{r["campaign"]}">{opts}</select> '
        f'<button type="button" onclick="addLeads({r["campaign"]})">Add leads</button> <span id="m-{r["campaign"]}" class="sub"></span></div></div>' for r in rows)
    return ('<h2>Add leads to a campaign</h2><div class="panel">' + body +
            '<p class="sub">Pick 500 to 3,000 and press Add leads. It raises the goal of that campaign and finds the contacts, and spends credits. '
            'Copy-test campaigns are slower because copy is written for each lead, and adding to one tips the balance of the test.</p></div>')


def page_html() -> str:
    n = numbers()
    prof = client.profile()
    angle_names: dict[str, str] = {}
    try:
        cfg = tomllib.loads((prof.prompts_dir.parent / "copy_test.toml").read_text(encoding="utf-8"))
        angle_names = dict(cfg.get("angles") or {})
    except Exception:
        pass
    t = _targets()
    goal, loaded = n["goal"], n["loaded"]
    gap = goal - n["potential"]
    if gap <= 0:
        tone, tone_txt = "ok", "On track: the companies already found are enough for the goal"
    elif n["per_hour"] > 0 and n["potential"] >= 0.4 * goal:
        tone, tone_txt = "warn", "Needs another sourcing round to reach the goal (it starts by itself)"
    else:
        tone, tone_txt = "bad", "Behind: pace or supply is short of the goal"
    eta = ""
    if n["per_hour"] > 0:
        remaining = max(min(n["potential"], goal) - loaded, 0)
        eta = f" At the last hour's pace ({n['per_hour']:,} an hour) the current pool lasts about {remaining / n['per_hour']:.1f} more hours."
    no_contact = max(n["searched"] - n["with_contacts"] - n["fit_failed"], 0)

    angle_rows = "".join(
        _row(f"{angle_names.get(a, a)} (campaign {t['angles'].get(a, '?')})", v, t["target"], f"{v:,} of {t['target']:,}", "g")
        for a, v in sorted(n["loaded_by"].items()))
    funnel = "".join([
        _row("Companies found", n["sourced"], n["sourced"], f"{n['sourced']:,}"),
        _row("Searched for contacts so far", n["searched"], n["sourced"], f"{n['searched']:,} ({_pct(n['searched'], n['sourced'])}%)"),
        _row("Gave verified contacts", n["with_contacts"], n["searched"], f"{n['with_contacts']:,} ({_pct(n['with_contacts'], n['searched'])}%)", "g"),
        _row("Failed the website fit check", n["fit_failed"], n["searched"], f"{n['fit_failed']:,} ({_pct(n['fit_failed'], n['searched'])}%)", "r"),
        _row("No usable contact found", no_contact, n["searched"], f"{no_contact:,} ({_pct(no_contact, n['searched'])}%)", "a"),
    ])
    held = held_reasons()
    held_total = sum(v for _, v in held) or 1
    held_html = "".join(_row(k, v, held_total, f"{v:,}", "a") for k, v in held[:6]) or '<p class="sub">Nothing held back yet.</p>'
    vr = _vertical_rows()
    ranked = sorted((r for r in vr if r["searched"] >= 40), key=lambda r: -r["with_contacts"] / r["searched"])
    best_set = {r["v"] for r in ranked[:4]}
    worst_set = {r["v"] for r in ranked[-4:]}
    body = ""
    for r in vr:
        y = _pct(r["with_contacts"], r["searched"])
        left = r["sourced"] - r["searched"]
        tag = '<span class="tag g">best yield</span>' if r["v"] in best_set else ('<span class="tag r">weak yield</span>' if r["v"] in worst_set else "")
        body += (f'<tr><td>{escape(r["v"] or "(none)")}{tag}</td><td class="num">{r["sourced"]:,}</td><td class="num">{r["searched"]:,}</td>'
                 f'<td class="num">{left:,}</td><td class="num">{r["with_contacts"]:,}</td><td class="num">{y}%</td></tr>')
    recs = [recommendation(n),
            "The next sourcing rounds should lean on the best-yield verticals in the table below and shrink the weak ones.",
            "Nothing sends from here: the campaigns are drafts with no senders attached."]
    if n.get("untiered", 0) > 500:
        recs.insert(1, f"{n['untiered']:,} stored companies have not been tiered yet, so the contact search cannot see them. The tiering pass runs at the end of each sourcing round.")
    recs_html = "".join(f"<li>{escape(x)}</li>" for x in recs)
    now = datetime.now(timezone.utc).strftime("%b %d, %H:%M UTC")
    og = n.get("og")
    og_note = ""
    hero_big = f"{loaded:,} of {goal:,} leads loaded ({_pct(loaded, goal)}%)"
    hero_sub = (f"{n['target']:,} per campaign, three campaigns. The companies found so far can supply about {int(n['potential']):,} leads "
                f"({_pct(n['potential'], goal)}% of the goal).{eta}")
    if og:
        # Each campaign the loader fills is one more row under "Leads by campaign", against its own goal (Emmanuel, 2026-10-21).
        notes = []
        for c in og:
            angle_rows += _row(f"{c.get('name') or 'Campaign'} (campaign {c['campaign']})", c["loaded"], c["target"], f"{c['loaded']:,} of {c['target']:,}", "g")
            sent = f"{c['sent']:,} emails sent, {c['interested']:,} interested" if c.get("sent") else "nothing sent yet"
            notes.append(f"Campaign {c['campaign']}: +{c['per_hour']:,} in the last hour, {sent}; held back {c['held_unclear'] + c['held_standard']:,}.")
        og_note = f'<p class="sub">{escape(" ".join(notes))}</p>'
        got, want = sum(c["loaded"] for c in og), sum(c["target"] for c in og)
        hero_big = f"{got:,} of {want:,} leads loaded across {len(og)} fill campaign{'s' if len(og) != 1 else ''} ({_pct(got, want)}%)"
        hero_sub = (f"The three angle campaigns are done at {loaded:,} of {goal:,}. {n['bank']:,} ready contacts are waiting and "
                    f"{n['left']:,} companies are not searched yet.")
    cards = "".join([
        _card(f"{loaded:,}", "Leads loaded", "in the three draft campaigns"),
        _card(f"{n['bank']:,}", "Contacts ready for copy", "verified, at companies that passed the fit check"),
        _card(f"{n['left']:,}", "Companies ready to search", f"never emailed, passed fit, in a declared vertical; {n['sourced']:,} found in all"),
        _card(f"{n['per_hour']:,}", "Leads an hour", "loaded in the last 60 minutes"),
    ])
    controls = _controls_html()
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TFP runway</title><style>{CSS}</style></head><body><div class="wrap">
<h1>Truck Fall Prevention: scale-up runway</h1><div class="sub">Updated {now}. This page refreshes itself every minute.</div>
<div class="hero"><div class="big">{hero_big}</div>
<div class="sub">{escape(hero_sub)}</div>
<span class="pill {tone}">{escape(tone_txt)}</span></div>
<div class="cards">{cards}</div>
<h2>Leads by campaign</h2><div class="panel">{angle_rows}{og_note}</div>
{controls}
<h2>From companies to leads</h2><div class="panel">{funnel}
<p class="sub">About {n['cps']:.1f} contacts come out of every company searched, so the {n['left']:,} companies left are worth roughly {int(n['left'] * n['cps']):,} contacts.</p></div>
<h2>What is holding leads back</h2><div class="panel">{held_html}</div>
<h2>What I recommend</h2><div class="panel"><ul class="plain">{recs_html}</ul></div>
<h2>By vertical</h2><div class="panel"><table><tr><th>Vertical</th><th class="num">Found</th><th class="num">Searched</th><th class="num">Left</th><th class="num">Gave contacts</th><th class="num">Yield</th></tr>{body}</table>
<p class="sub">Yield = companies that gave verified contacts, out of those searched.</p></div>
<div class="foot"><b>Words used here.</b> Found: sourced from QuickEnrich or Lead Ocean. Searched: we looked for people there. Ready for copy: verified email, passed the website fit check, no email written yet.
Held: the email broke a copy rule and was set aside. Plain text version: <a href="/runway.txt">/runway.txt</a>.</div>
</div>{ADD_JS}</body></html>"""
