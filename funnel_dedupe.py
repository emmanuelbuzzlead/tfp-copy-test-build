"""Dedupe every KEEP company and label whether we have EVER been to it before.

Writes the result to the live table company_funnel so it survives any machine shutdown.
Idempotent: re-run after new companies are judged or the Amazon size cleanup, it rebuilds labels.

Labels (first match wins):
  suppressed              on the do-not-contact list
  previously_contacted    a campaign has emailed someone there (recent_contacts, ANY age), or a pushed contact/company
  contacts_found_not_sent we hold contacts for it (contacts table) but never sent
  never_contacted         none of the above  <- the funnel starts here
"""
import json, time
from collections import Counter, defaultdict
from app import db, dnc

DDL = """
CREATE TABLE IF NOT EXISTS company_funnel (
    domain        TEXT PRIMARY KEY,
    name          TEXT,
    verticals     TEXT[],
    sources       TEXT[],
    label         TEXT NOT NULL,
    evidence      TEXT,
    labeled_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS company_funnel_label_idx ON company_funnel (label);
"""
with db.cursor() as cur:
    cur.execute(DDL)
    cur.execute("SELECT vertical, domain, payload, coalesce(source,'unknown') src FROM staged_companies WHERE verdict='KEEP'")
    keep_rows = cur.fetchall()
    cur.execute("SELECT count(*) n FROM staged_companies WHERE verdict='IDPR'"); idpr = cur.fetchone()["n"]
    cur.execute("SELECT DISTINCT company_domain d FROM recent_contacts"); sent = {db.normalize_domain(r["d"] or "") for r in cur.fetchall()}
    cur.execute("SELECT company_domain d, bool_or(status='pushed') pushed FROM contacts GROUP BY 1"); ct = {db.normalize_domain(r["d"] or ""): r["pushed"] for r in cur.fetchall()}
    cur.execute("SELECT domain d, status FROM companies"); co = {db.normalize_domain(r["d"] or ""): r["status"] for r in cur.fetchall()}

agg = defaultdict(lambda: {"name": None, "verticals": set(), "sources": set()})
for r in keep_rows:
    d = db.normalize_domain(r["domain"] or "")
    if not d: continue
    a = agg[d]; a["verticals"].add(r["vertical"]); a["sources"].add(r["src"])
    p = r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"] or "{}")
    a["name"] = a["name"] or p.get("name")

blocked = dnc.load()
def is_dnc(d):
    for f in ("is_blocked", "blocked", "matches", "domain_blocked"):
        fn = getattr(blocked, f, None)
        if callable(fn):
            try: return bool(fn(d))
            except TypeError: continue
    return d in getattr(blocked, "domains", set())

out, label_of = [], {}
for d, a in agg.items():
    if is_dnc(d): lab, ev = "suppressed", "do-not-contact list"
    elif d in sent: lab, ev = "previously_contacted", "in a past EmailBison campaign"
    elif ct.get(d): lab, ev = "previously_contacted", "contact pushed by this pipeline"
    elif co.get(d) in ("pushed", "approved", "killed"): lab, ev = "previously_contacted", f"company status {co.get(d)}"
    elif d in ct or d in co: lab, ev = "contacts_found_not_sent", "contacts found earlier, never sent"
    else: lab, ev = "never_contacted", None
    label_of[d] = lab
    out.append((d, a["name"], sorted(v for v in a["verticals"] if v), sorted(a["sources"]), lab, ev))

with db.cursor() as cur:
    cur.executemany("""INSERT INTO company_funnel (domain,name,verticals,sources,label,evidence)
                       VALUES (%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (domain) DO UPDATE SET name=EXCLUDED.name, verticals=EXCLUDED.verticals,
                         sources=EXCLUDED.sources, label=EXCLUDED.label, evidence=EXCLUDED.evidence, labeled_at=now()""", out)
    # A company that stops being KEEP leaves the funnel -- unless it is in the send history, in
    # which case it stays as a previously-contacted record with no vertical.
    cur.execute("""DELETE FROM company_funnel WHERE domain <> ALL(%s)
                   AND NOT ('emailbison_history' = ANY(sources)) AND label <> 'previously_contacted'""", (list(agg),))
    cur.execute("""UPDATE company_funnel SET verticals = ARRAY[]::text[], sources = ARRAY['emailbison_history']
                   WHERE domain <> ALL(%s) AND label = 'previously_contacted' AND cardinality(verticals) > 0""", (list(agg),))
    cur.execute("ALTER TABLE company_funnel ADD COLUMN IF NOT EXISTS site_status TEXT")
    cur.execute("""UPDATE company_funnel f SET site_status = s.st FROM (
                       SELECT domain, min(payload->>'_site_fit') st FROM staged_companies
                       WHERE verdict='KEEP' AND payload ? '_site_fit' GROUP BY 1) s
                   WHERE f.domain = s.domain""")

c = Counter(label_of.values())
print(f"KEEP rows {len(keep_rows)} -> unique companies {len(agg)} (duplicates removed {len(keep_rows)-len(agg)}); IDPR-tier rows not counted: {idpr}")
for k in ("never_contacted", "contacts_found_not_sent", "previously_contacted", "suppressed"): print(f"  {k:24} {c.get(k,0)}")
by_src = Counter()
for d, a in agg.items():
    for s in a["sources"]: by_src[(s, label_of[d])] += 1
print("by source x label:", dict(by_src))
