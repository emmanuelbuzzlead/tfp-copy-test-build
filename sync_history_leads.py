"""Copy every lead in the workspace's EmailBison campaigns (name, title, company) into the
history_leads table once, so filling from tiers 2-4 is a database lookup, not a 10-minute
API crawl each time. Read-only against EmailBison. Resumable: campaigns already copied are
skipped. Log: /app/logs/history_leads.log"""
import time
from concurrent.futures import ThreadPoolExecutor
from app import db
from app.clients import emailbison

LOG = open("/app/logs/history_leads.log", "a", buffering=1)
def log(m): LOG.write(f"{time.strftime('%H:%M:%S')} {m}\n")

with db.cursor() as cur:
    cur.execute("""CREATE TABLE IF NOT EXISTS history_leads (
        email TEXT NOT NULL, campaign_id BIGINT NOT NULL, first_name TEXT, last_name TEXT,
        title TEXT, company TEXT, company_domain TEXT,
        PRIMARY KEY (email, campaign_id));
        CREATE INDEX IF NOT EXISTS history_leads_domain_idx ON history_leads (company_domain)""")
    cur.execute("SELECT DISTINCT campaign_id FROM history_leads")
    done = {r["campaign_id"] for r in cur.fetchall()}
    cur.execute("SELECT DISTINCT campaign_id FROM recent_contacts")
    campaigns = [r["campaign_id"] for r in cur.fetchall() if r["campaign_id"] not in done]
log(f"start: {len(campaigns)} campaigns to copy ({len(done)} already done)")


def page(args):
    cid, p = args
    for attempt in range(3):
        try:
            return (emailbison._get(f"/api/campaigns/{cid}/leads", {"page": p}) or {}).get("data") or []
        except Exception:
            time.sleep(1 + attempt)
    return []


with ThreadPoolExecutor(max_workers=12) as pool:
    for cid in campaigns:
        total = int((emailbison.get_campaign(cid) or {}).get("total_leads") or 0)
        pages = max((total + 14) // 15, 1)
        rows = []
        for got in pool.map(page, [(cid, p) for p in range(1, pages + 1)]):
            for lead in got:
                email = (lead.get("email") or "").strip().lower()
                if "@" not in email:
                    continue
                rows.append((email, cid, lead.get("first_name"), lead.get("last_name"),
                             lead.get("title") or lead.get("job_title"), lead.get("company"),
                             db.normalize_domain(email.rsplit("@", 1)[1])))
        with db.cursor() as cur:
            cur.executemany("""INSERT INTO history_leads (email, campaign_id, first_name, last_name, title,
                               company, company_domain) VALUES (%s,%s,%s,%s,%s,%s,%s)
                               ON CONFLICT DO NOTHING""", rows)
        log(f"campaign {cid}: {len(rows)} of {total} leads copied")
with db.cursor() as cur:
    cur.execute("SELECT count(*) n, count(DISTINCT company_domain) d FROM history_leads")
    r = cur.fetchone()
log(f"HISTORY_DONE: {r['n']} leads at {r['d']} companies")
