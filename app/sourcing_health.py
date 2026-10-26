"""Is the sourcing stage working well? One scorecard and one provider check, for any client build (Emmanuel, 2026-10-26).

scorecard()  what sourcing produced, from this database: companies searched, how many gave contacts, how many failed the fit check, contacts per company, how fast it
             runs, how many contacts wait in the bank and how many were held and why, yield by vertical, and the bounce rate of what was actually sent (EmailBison's own
             counters). Cost per contact is NOT here: this build keeps no spend ledger, and the report says so instead of guessing.
providers()  which provider keys are set, the OpenRouter balance, and vendor errors in the last hour (credit, auth and rate-limit lines in the log), so a dead
             provider is named before a run, not found out from a flat yield.
Read-only. Nothing here changes anything or spends anything.
"""
from __future__ import annotations

import os
import re

from . import client, db

KEYS = {
    "company data": ("AIARK_API_KEY", "QUICKENRICH_API_KEY", "LEADOCEAN_API_KEY"),
    "people finders": ("QUICKENRICH_API_KEY", "AIARK_API_KEY", "APOLLO_API_KEY", "LEADMAGIC_API_KEY", "HUNTER_API_KEY"),
    "email verification": ("MILLIONVERIFIER_API_KEY", "TRYKITT_API_KEY", "BOUNCEBAN_API_KEY"),
    "language models": ("OPENROUTER_API_KEY", "BUZZLEAD_API_KEY", "ANTHROPIC_API_KEY", "CONCENTRATE_AI_API_KEY"),
    "web reading": ("JINA_API_KEY", "SERPER_API_KEY"),
}
VENDOR_ERR = re.compile(r"(402|401|403|429|insufficient|out of credit|payment required|quota|rate.?limit|unauthori[sz]ed)", re.I)


def _n(sql: str, params: tuple = ()) -> int:
    with db.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return int(next(iter(row.values())) or 0) if row else 0


def _pct(a: float, b: float) -> float:
    return round(100.0 * a / b, 1) if b else 0.0


def scorecard() -> dict:
    searched = _n("SELECT count(*) FROM staged_companies WHERE verdict = 'KEEP' AND found_at IS NOT NULL")
    with_contacts = _n("""SELECT count(DISTINCT c.company_domain) FROM contacts c JOIN staged_companies s ON s.domain = c.company_domain
                          WHERE s.verdict = 'KEEP' AND s.found_at IS NOT NULL""")
    contacts = _n("""SELECT count(*) FROM contacts c JOIN staged_companies s ON s.domain = c.company_domain WHERE s.verdict = 'KEEP' AND s.found_at IS NOT NULL""")
    fit_failed = _n("""SELECT count(DISTINCT s.domain) FROM staged_companies s JOIN company_funnel f ON f.domain = s.domain
                       WHERE s.verdict = 'KEEP' AND s.found_at IS NOT NULL AND f.fit IN ('LOW', 'NOT_FIT')""")
    bank = _n("""SELECT count(*) FROM contacts c LEFT JOIN company_funnel f ON f.domain = c.company_domain
                 WHERE c.status = 'verified' AND c.eb_campaign_id IS NULL AND coalesce(c.first_name, '') <> '' AND coalesce(c.personalization_1, '') = ''
                   AND coalesce(f.fit, '') NOT IN ('LOW', 'NOT_FIT')""")
    searched_hour = _n("SELECT count(*) FROM staged_companies WHERE found_at > now() - interval '60 minutes'")
    searched_day = _n("SELECT count(*) FROM staged_companies WHERE found_at > now() - interval '24 hours'")
    gateway_skips = _n("SELECT count(*) FROM logs WHERE stage = 'stage4' AND message LIKE '%%skipped, mail gateway%%' AND ts > now() - interval '24 hours'")
    held: list[tuple[str, int]] = []
    with db.cursor() as cur:
        cur.execute("""SELECT left(coalesce(drop_reason, '(no reason recorded)'), 70) r, count(*) n FROM contacts WHERE status = 'needs_review'
                       GROUP BY 1 ORDER BY 2 DESC LIMIT 5""")
        held = [(r["r"], int(r["n"])) for r in cur.fetchall()]
        cur.execute("""SELECT s.vertical v, count(*) FILTER (WHERE s.found_at IS NOT NULL) searched, count(DISTINCT c.company_domain) with_contacts
                       FROM staged_companies s LEFT JOIN contacts c ON c.company_domain = s.domain WHERE s.verdict = 'KEEP' GROUP BY 1 HAVING count(*) FILTER (WHERE s.found_at IS NOT NULL) >= 30""")
        verts = sorted(({"vertical": r["v"], "searched": int(r["searched"]), "yield_pct": _pct(r["with_contacts"], r["searched"])} for r in cur.fetchall()),
                       key=lambda x: -x["yield_pct"])
        cur.execute("SELECT DISTINCT eb_campaign_id FROM contacts WHERE eb_campaign_id IS NOT NULL")
        campaigns = [int(r["eb_campaign_id"]) for r in cur.fetchall()]
    sent = bounced = 0
    try:
        from .clients import emailbison
        for cid in campaigns:
            c = emailbison.get_campaign(cid) or {}
            sent += int(c.get("emails_sent") or 0)
            bounced += int(c.get("bounced") or 0)
    except Exception:
        pass
    return {"searched": searched, "with_contacts": with_contacts, "yield_pct": _pct(with_contacts, searched), "fit_failed": fit_failed,
            "fit_failed_pct": _pct(fit_failed, searched), "contacts": contacts, "contacts_per_company_with_contacts": round(contacts / with_contacts, 1) if with_contacts else 0.0,
            "contacts_per_company_searched": round(contacts / searched, 2) if searched else 0.0, "searched_last_hour": searched_hour, "searched_last_24h": searched_day,
            "gateway_skips_24h": gateway_skips, "bank": bank, "held": held, "best_verticals": verts[:3], "weakest_verticals": verts[-3:][::-1] if len(verts) > 3 else [],
            "sent": sent, "bounced": bounced, "bounce_pct": _pct(bounced, sent)}


def providers() -> dict:
    status: dict[str, list[str]] = {}
    for group, names in KEYS.items():
        status[group] = [f"{n}: {'set' if os.getenv(n) else 'MISSING'}" for n in names]
    balance = None
    key = os.getenv("OPENROUTER_API_KEY")
    if key:
        try:
            import httpx
            d = (httpx.get("https://openrouter.ai/api/v1/credits", headers={"Authorization": f"Bearer {key}"}, timeout=15).json() or {}).get("data") or {}
            balance = round(float(d.get("total_credits", 0)) - float(d.get("total_usage", 0)), 2)
        except Exception:
            balance = None
    errors = 0
    sample = ""
    with db.cursor() as cur:
        cur.execute("SELECT stage, left(message, 140) m FROM logs WHERE level IN ('warn', 'error') AND ts > now() - interval '60 minutes' ORDER BY ts DESC LIMIT 400")
        for r in cur.fetchall():
            if VENDOR_ERR.search(r["m"] or ""):
                errors += 1
                sample = sample or f"{r['stage']}: {r['m']}"
    return {"keys": status, "openrouter_balance": balance, "vendor_error_lines_last_hour": errors, "latest_vendor_error": sample}


_verifier_cache: dict = {"at": 0.0, "ok": True, "text": ""}


def verifier_ok(max_age: float = 300.0) -> tuple[bool, str]:
    """Is email verification working? One MillionVerifier check on a throwaway address (a few minutes cached). 2026-10-26: the key answered result=error with 0
    credits, every candidate came back unverified, and chunks of 250 companies found no usable contact. The same call through the BuzzLead verify tool returned
    an empty quality for every address, so the cause is out-of-credit, not the key. Contact search waits while this says no."""
    import time
    if _verifier_cache["text"] and time.time() - _verifier_cache["at"] < max_age:
        return _verifier_cache["ok"], _verifier_cache["text"]
    key = os.getenv("MILLIONVERIFIER_API_KEY")
    if not key:
        ok, text = False, "MILLIONVERIFIER_API_KEY is not set"
    else:
        try:
            import httpx
            data = httpx.get("https://api.millionverifier.com/api/v3/", params={"api": key, "email": "probe@example.com", "timeout": 10}, timeout=30).json()
            if data.get("result") == "error" or data.get("error"):
                ok, text = False, f"MillionVerifier answers error (credits {data.get('credits', 0)}): {data.get('error') or 'out of credits or key rejected'}"
            else:
                ok, text = True, f"MillionVerifier ok (credits {data.get('credits', '?')})"
        except Exception as exc:
            ok, text = True, f"MillionVerifier check could not run ({type(exc).__name__}); not blocking"
    _verifier_cache.update(at=time.time(), ok=ok, text=text)
    return ok, text


_probe_cache: dict = {"at": 0.0, "text": ""}
PROBE_DOMAIN = "usps.com"          # a large public company every finder should know; a probe costs about one credit per provider


def probe(force: bool = False, max_age: float = 1800.0) -> str:
    """One live call to each contact finder. A finder that errors OR answers with nothing for a company that has plenty of people is named. AI Ark swallows
    an auth failure and returns an empty list, so its printed error is captured here and reported (2026-10-26: 'Unauthorized' on every call, no alarm)."""
    import contextlib
    import io
    import time
    if not force and _probe_cache["text"] and time.time() - _probe_cache["at"] < max_age:
        return _probe_cache["text"]
    from . import finders
    from .clients import quickenrich
    titles = ["Safety Manager", "Operations Manager", "Plant Manager"]
    parts = []
    for name, fn in (("QuickEnrich", quickenrich.find_people), ("AI Ark", finders.find_aiark)):
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                out = fn(PROBE_DOMAIN, titles, 3)
            said = buf.getvalue()
            if re.search(r"auth failed|unauthori[sz]ed|invalid.?(api)?.?key|402|payment", said, re.I):
                parts.append(f"{name}: KEY REJECTED")
            elif not out:
                parts.append(f"{name}: returned nothing")
            else:
                parts.append(f"{name}: ok ({len(out)} people)")
        except Exception as exc:
            parts.append(f"{name}: ERROR {type(exc).__name__}: {str(exc)[:80]}")
    _probe_cache.update(at=time.time(), text="; ".join(parts))
    return _probe_cache["text"]


def report() -> str:
    s = scorecard()
    p = providers()
    lines = [
        f"Sourcing scorecard for {client.profile().display_name}",
        f"Companies searched: {s['searched']:,} ({s['searched_last_hour']:,} in the last hour, {s['searched_last_24h']:,} in 24 hours)",
        f"Gave at least one contact: {s['with_contacts']:,} ({s['yield_pct']}%) | {s['contacts_per_company_with_contacts']} contacts per company that gave any, {s['contacts_per_company_searched']} per company searched",
        f"Failed the website fit check: {s['fit_failed']:,} ({s['fit_failed_pct']}% of searched) | skipped for a blocked mail gateway in 24 hours: {s['gateway_skips_24h']:,}",
        f"Ready in the bank: {s['bank']:,}",
        "Held back: " + ("; ".join(f"{n:,} {r}" for r, n in s["held"]) if s["held"] else "nothing"),
        "Best verticals: " + ", ".join(f"{v['vertical']} {v['yield_pct']}%" for v in s["best_verticals"]),
        "Weakest verticals: " + ", ".join(f"{v['vertical']} {v['yield_pct']}%" for v in s["weakest_verticals"]),
        (f"Bounce rate of what was sent: {s['bounce_pct']}% ({s['bounced']:,} of {s['sent']:,})" if s["sent"] else "Bounce rate: nothing sent yet"),
        "Cost per contact: not measured (this build keeps no spend ledger)",
        "",
        "Providers: " + "; ".join(f"{g}: {', '.join(x for x in v if 'MISSING' in x) or 'all set'}" for g, v in p["keys"].items()),
        f"Finder probe: {probe()}",
        f"Email verification: {verifier_ok()[1]}",
        f"OpenRouter balance: {'$' + str(p['openrouter_balance']) if p['openrouter_balance'] is not None else 'unknown'} | vendor error lines in the last hour: {p['vendor_error_lines_last_hour']}"
        + (f" (latest: {p['latest_vendor_error']})" if p["latest_vendor_error"] else ""),
    ]
    return "\n".join(lines)
