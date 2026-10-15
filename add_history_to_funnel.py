"""Add every EmailBison history company that ISN'T already in company_funnel.

company_funnel so far only holds companies we sourced and fit-checked ourselves (the KEEP
set), cross-referenced against send history. That leaves out every company we've emailed
before that never came back through this pipeline's own sourcing -- the majority of the
44,334 in recent_contacts. Troy, 2026-09-27: the funnel is meant to be the whole mappable
universe, not just the overlap.

These rows get label='previously_contacted', source='emailbison_history', and no
verticals (we never fit-checked them under current criteria -- that's a separate,
optional pass, not assumed here). Tiered the same way as everything else.
"""
from app import db
with db.cursor() as cur:
    cur.execute("SELECT DISTINCT company_domain AS d FROM recent_contacts")
    history = {db.normalize_domain(r["d"] or "") for r in cur.fetchall() if r["d"]}
    cur.execute("SELECT domain FROM company_funnel")
    have = {r["domain"] for r in cur.fetchall()}
new = sorted(d for d in history if d and d not in have)
print(f"recent_contacts distinct domains: {len(history)} | already in funnel: {len(history & have)} | to add: {len(new)}")
rows = [(d, None, [], ["emailbison_history"], "previously_contacted", "in EmailBison send history; not re-sourced under current fit criteria") for d in new]
with db.cursor() as cur:
    cur.executemany("""INSERT INTO company_funnel (domain,name,verticals,sources,label,evidence)
                       VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (domain) DO NOTHING""", rows)
    cur.execute("SELECT count(*) n FROM company_funnel")
    print("company_funnel total rows now:", cur.fetchone())
