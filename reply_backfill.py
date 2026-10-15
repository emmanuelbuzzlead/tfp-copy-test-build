"""Tag every company that has ever replied, across ALL past EmailBison campaigns. READ-ONLY against
EmailBison: it lists replies and never removes a lead or touches a campaign.

Writes history_replies (one row per person per campaign) and sets company_funnel.replied /
.interested / .bounced_only, so sourcing can skip companies that already answered.
Out-of-office auto-replies are not counted as replies. Progress: /app/logs/reply_backfill.log"""
import time
from app import db
from app.clients import emailbison
from app.stages.stop_on_reply import contact_outcomes, _domain_of
LOG = open("/app/logs/reply_backfill.log", "a", buffering=1)
def log(m): LOG.write(f"{time.strftime('%H:%M:%S')} {m}\n")

with db.cursor() as cur:
    cur.execute("""CREATE TABLE IF NOT EXISTS history_replies (
        email TEXT NOT NULL, campaign_id BIGINT NOT NULL, company_domain TEXT, replied_at TIMESTAMPTZ,
        interested BOOLEAN NOT NULL DEFAULT false, bounced_at TIMESTAMPTZ, PRIMARY KEY (email, campaign_id));
        ALTER TABLE company_funnel ADD COLUMN IF NOT EXISTS replied BOOLEAN NOT NULL DEFAULT false,
            ADD COLUMN IF NOT EXISTS interested BOOLEAN NOT NULL DEFAULT false,
            ADD COLUMN IF NOT EXISTS last_reply_at TIMESTAMPTZ""")
    cur.execute("SELECT campaign_id, max(campaign_name) n, count(*) c FROM recent_contacts GROUP BY 1 ORDER BY 3 DESC")
    camps = cur.fetchall()
with db.cursor() as cur:
    cur.execute("SELECT DISTINCT campaign_id FROM history_replies")
    done_ids = {r["campaign_id"] for r in cur.fetchall()}
# The last campaign reached before an interruption may be partial; redo it.
camps = [c for c in camps if c["campaign_id"] not in done_ids or c is camps[-1]]
log(f"start: {len(camps)} campaigns to read ({len(done_ids)} already done)")
for c in camps:
    try:
        replies = emailbison.list_replies(int(c["campaign_id"]))
    except Exception as e:
        log(f"FAILED {c['campaign_id']} {str(c['n'])[:40]}: {str(e)[:120]}"); continue
    rows = [(o["email"], int(c["campaign_id"]), _domain_of(o["email"]), o["replied_at"], o["interested"], o["bounced_at"])
            for o in contact_outcomes(replies)]
    with db.cursor() as cur:
        cur.executemany("""INSERT INTO history_replies (email, campaign_id, company_domain, replied_at, interested, bounced_at)
                           VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (email, campaign_id) DO UPDATE
                           SET interested = history_replies.interested OR EXCLUDED.interested,
                               replied_at = COALESCE(history_replies.replied_at, EXCLUDED.replied_at)""", rows)
    real = sum(1 for r in rows if r[3]); hot = sum(1 for r in rows if r[4])
    log(f"{str(c['n'])[:44]:46} raw={len(replies):>5} replied={real:>4} interested={hot:>3} bounced={sum(1 for r in rows if r[5] and not r[3])}")

with db.cursor() as cur:
    cur.execute("""UPDATE company_funnel f SET replied = true, interested = h.hot, last_reply_at = h.last
                   FROM (SELECT company_domain, bool_or(interested) hot, max(replied_at) last
                         FROM history_replies WHERE replied_at IS NOT NULL AND company_domain <> '' GROUP BY 1) h
                   WHERE f.domain = h.company_domain""")
    cur.execute("SELECT count(*) FILTER (WHERE replied) r, count(*) FILTER (WHERE interested) i, count(*) t FROM company_funnel")
    x = cur.fetchone(); log(f"company_funnel: {x['r']} companies have replied, {x['i']} of them interested, of {x['t']} total")
    cur.execute("SELECT tier, count(*) FILTER (WHERE replied) r FROM company_funnel GROUP BY 1 ORDER BY 1")
    for r in cur.fetchall(): log(f"  tier {r['tier']}: {r['r']} replied")
log("REPLIES_DONE")
