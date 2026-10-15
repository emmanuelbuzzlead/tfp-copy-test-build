from app import db
with db.cursor() as cur:
    cur.execute("SELECT tier, count(*) n FROM company_funnel GROUP BY 1 ORDER BY 1")
    for r in cur.fetchall():
        print("tier", r["tier"], "->", r["n"])
    cur.execute("SELECT count(*) n FROM company_funnel WHERE tier IS NULL")
    print("untiered:", cur.fetchone())
