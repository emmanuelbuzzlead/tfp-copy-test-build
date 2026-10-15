"""Add every EmailBison-history company missing from company_funnel, in committed chunks, then tier everything.
Runs on Railway under nohup; progress in /app/logs/history_backfill.log. Safe to re-run (ON CONFLICT DO NOTHING)."""
import time
from app import db
LOG = open("/app/logs/history_backfill.log", "a", buffering=1)
def log(m): LOG.write(f"{time.strftime('%H:%M:%S')} {m}\n")

with db.cursor() as cur:
    cur.execute("ALTER TABLE company_funnel ADD COLUMN IF NOT EXISTS tier INTEGER, ADD COLUMN IF NOT EXISTS days_since_contact INTEGER")
    cur.execute("SELECT DISTINCT company_domain AS d FROM recent_contacts")
    history = {db.normalize_domain(r["d"] or "") for r in cur.fetchall() if r["d"]}
    cur.execute("SELECT domain FROM company_funnel")
    have = {r["domain"] for r in cur.fetchall()}
new = sorted(d for d in history if d and d not in have)
log(f"history {len(history)}, already in funnel {len(history & have)}, to add {len(new)}")
for i in range(0, len(new), 1000):
    chunk = new[i:i + 1000]
    with db.cursor() as cur:   # one transaction per chunk, so a drop loses at most 1,000
        cur.execute("""INSERT INTO company_funnel (domain, name, verticals, sources, label, evidence)
                       SELECT d, NULL, ARRAY[]::text[], ARRAY['emailbison_history'], 'previously_contacted',
                              'in EmailBison send history; not fit-checked under current criteria'
                       FROM unnest(%s::text[]) AS d ON CONFLICT (domain) DO NOTHING""", (chunk,))
    log(f"inserted {min(i + 1000, len(new))}/{len(new)}")

with db.cursor() as cur:
    cur.execute("""WITH lt AS (SELECT company_domain AS domain, max(campaign_date) AS last_date FROM recent_contacts GROUP BY 1)
                   UPDATE company_funnel f SET days_since_contact = (CURRENT_DATE - lt.last_date),
                       tier = CASE WHEN (CURRENT_DATE - lt.last_date) > 120 THEN 2
                                   WHEN (CURRENT_DATE - lt.last_date) > 90 THEN 3
                                   WHEN (CURRENT_DATE - lt.last_date) > 60 THEN 4 ELSE 5 END
                   FROM lt WHERE f.domain = lt.domain AND f.label = 'previously_contacted'""")
    cur.execute("UPDATE company_funnel SET tier = 1, days_since_contact = NULL WHERE label = 'never_contacted'")
    cur.execute("UPDATE company_funnel SET tier = 9 WHERE label IN ('contacts_found_not_sent','suppressed')")
    # Contacted by this pipeline's own pushes, with no campaign date on record: recency unknown,
    # so treat as too recent rather than risk a repeat approach.
    cur.execute("UPDATE company_funnel SET tier = 5 WHERE label = 'previously_contacted' AND tier IS NULL")
    cur.execute("SELECT tier, count(*) n FROM company_funnel GROUP BY 1 ORDER BY 1")
    for r in cur.fetchall(): log(f"tier {r['tier']} -> {r['n']}")
    cur.execute("SELECT count(*) n FROM company_funnel"); log(f"TOTAL {cur.fetchone()['n']}")
log("BACKFILL_DONE")
