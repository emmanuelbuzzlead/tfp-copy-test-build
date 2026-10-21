"""Fill the "OG copy" campaign from the bank (Emmanuel, 2026-10-21): campaign 2157 uses the original spintax copy, which needs only
{{COMPANY}} and {{MATERIALS_HAULED}} per lead, so no research brief and no email writing.

One pass takes a batch of ready contacts (verified, fit-checked company, not loaded anywhere, not contacted in the recontact window, never replied or
bounced), runs the standing TFP gates, adds the two merge values to the locked field standards (app/materials.py), and loads them into the campaign.

  gates in order   company fit check (cached) -> Mimecast/Barracuda MX block -> MATERIALS_HAULED + COMPANY standards
  held, not sent   a company the model cannot describe, or any value that fails the standards, goes to needs_review with the reason
  order            safety / EHS titles first (5 of the 8 interested leads on the old campaigns were safety titles, 2.0 per 1,000 sends against 0.18 for
                   plant and operations), then everyone else, 5 seats per company
Enabled by MATERIALS_CAMPAIGN_ID (the campaign id) and MATERIALS_TARGET (default 3500). testrun.py calls run_pass() instead of the angle fill.
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

from . import db, materials, scaling
from .company_name_cleaner import clean_company_name

SAFETY_RX = re.compile(r"safety|ehs|\bhse\b|health|environment|risk|loss prevention|compliance", re.I)
SHORT_DESCRIPTION = 40
_lock = threading.Lock()


def campaign_id() -> int | None:
    raw = os.getenv("MATERIALS_CAMPAIGN_ID", "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else None


def target() -> int:
    return int(os.getenv("MATERIALS_TARGET", "3500"))


def ensure_tables() -> None:
    with db.cursor() as cur:
        cur.execute("ALTER TABLE contacts ADD COLUMN IF NOT EXISTS materials_hauled TEXT")
        cur.execute("""CREATE TABLE IF NOT EXISTS materials_cache (
            domain TEXT PRIMARY KEY, phrase TEXT, status TEXT NOT NULL, model TEXT, updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""")


def loaded() -> int:
    cid = campaign_id()
    if not cid:
        return 0
    with db.cursor() as cur:
        cur.execute("SELECT count(*) n FROM contacts WHERE status = 'pushed' AND eb_campaign_id = %s", (cid,))
        return int(cur.fetchone()["n"])


def candidates() -> list[dict]:
    """Ready contacts, safety titles first, at most _seats() per company."""
    from . import client
    from .wave1_fill import PERSONAL_DOMAINS, _seats
    days = client.profile().recontact_days
    with db.cursor() as cur:
        cur.execute("""SELECT c.email, c.first_name, c.last_name, c.title, c.company_domain, c.company_name, c.vertical
                       FROM contacts c LEFT JOIN company_funnel f ON f.domain = c.company_domain
                       WHERE c.status = 'verified' AND c.eb_campaign_id IS NULL AND coalesce(c.first_name, '') <> ''
                         AND coalesce(c.personalization_1, '') = '' AND coalesce(c.company_domain, '') <> ''
                         AND coalesce(f.fit, '') NOT IN ('LOW', 'NOT_FIT') AND NOT coalesce(f.replied, false)
                         AND NOT (c.company_domain = ANY(%s))
                         AND NOT EXISTS (SELECT 1 FROM recent_contacts rc WHERE lower(rc.email) = lower(c.email)
                                         AND rc.campaign_date >= current_date - %s::int)
                         AND NOT EXISTS (SELECT 1 FROM history_replies r WHERE lower(r.email) = lower(c.email)
                                         AND (r.replied_at IS NOT NULL OR r.bounced_at IS NOT NULL))""",
                    (list(PERSONAL_DOMAINS), days))
        rows = [dict(r) for r in cur.fetchall()]
    by_dom: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_dom[r["company_domain"]].append(r)
    out: list[dict] = []
    for dom, people in by_dom.items():
        people.sort(key=lambda p: (0 if SAFETY_RX.search(p.get("title") or "") else 1, p["email"]))
        out += people[:_seats()]
    out.sort(key=lambda p: (0 if SAFETY_RX.search(p.get("title") or "") else 1, hashlib.sha256(p["company_domain"].encode()).hexdigest()))
    return out


def _context(domains: list[str]) -> dict[str, tuple[str, str, str]]:
    """{domain: (name, description, industry)} from the sourcing record the company arrived with."""
    ctx: dict[str, tuple[str, str, str]] = {}
    with db.cursor() as cur:
        cur.execute("SELECT DISTINCT ON (domain) domain, payload FROM staged_companies WHERE domain = ANY(%s) ORDER BY domain, staged_at DESC", (domains,))
        for r in cur.fetchall():
            p = r["payload"] if isinstance(r["payload"], dict) else {}
            ctx[r["domain"]] = (str(p.get("name") or p.get("company_name") or ""), str(p.get("description") or p.get("short_description") or ""),
                                str(p.get("industry") or p.get("industries") or ""))
    return ctx


def phrases(rows: list[dict], workers: int = 12) -> tuple[dict[str, str | None], dict]:
    """MATERIALS_HAULED per company domain (None = could not be described). Cached in materials_cache. Returns (phrases, stats)."""
    from .clients import jina
    ensure_tables()
    domains = sorted({r["company_domain"] for r in rows})
    names = {r["company_domain"]: r.get("company_name") or "" for r in rows}
    out: dict[str, str | None] = {}
    with db.cursor() as cur:
        cur.execute("SELECT domain, phrase, status FROM materials_cache WHERE domain = ANY(%s)", (domains,))
        for r in cur.fetchall():
            out[r["domain"]] = r["phrase"] if r["status"] == "ok" else None
    todo = [d for d in domains if d not in out]
    ctx = _context(todo) if todo else {}

    def one(dom: str) -> tuple[str, str | None]:
        name, desc, industry = ctx.get(dom, ("", "", ""))
        if len(desc.strip()) < SHORT_DESCRIPTION:
            try:
                desc = (desc + " " + jina.website_text(dom, 900)).strip()      # no description from sourcing: read the home page instead
            except Exception:
                pass
        try:
            return dom, materials.infer(name or names.get(dom) or dom, desc, industry)
        except Exception:
            return dom, "ERR"

    with ThreadPoolExecutor(workers) as ex:
        results = list(ex.map(one, todo))
    stats = {"companies": len(domains), "cached": len(domains) - len(todo), "inferred": 0, "unclear": 0, "errors": 0}
    save = []
    for dom, phrase in results:
        if phrase == "ERR":
            stats["errors"] += 1          # not cached: the next pass tries again
            out[dom] = None
            continue
        out[dom] = phrase
        stats["inferred" if phrase else "unclear"] += 1
        save.append((dom, phrase, "ok" if phrase else "unclear", materials.MODEL))
    if save:
        with db.cursor() as cur:
            cur.executemany("""INSERT INTO materials_cache (domain, phrase, status, model) VALUES (%s,%s,%s,%s)
                               ON CONFLICT (domain) DO UPDATE SET phrase = EXCLUDED.phrase, status = EXCLUDED.status, model = EXCLUDED.model, updated_at = now()""", save)
    return out, stats


def _hold(rows: list[dict], reason: str) -> None:
    if rows:
        with db.cursor() as cur:
            cur.executemany("UPDATE contacts SET status = 'needs_review', eb_campaign_id = NULL, drop_reason = %s WHERE email = %s",
                            [(reason, r["email"]) for r in rows])


def run_pass(batch: int | None = None) -> dict:
    """One batch: gates, merge values, load. Safe to call repeatedly; returns counts for the scale-up job to report."""
    from . import wave1_fill
    cid = campaign_id()
    if not cid:
        return {"status": "not_enabled"}
    if not _lock.acquire(blocking=False):
        return {"status": "already_running"}
    try:
        ensure_tables()
        run_id = "materials-" + __import__("time").strftime("%Y%m%dT%H%M%S")
        db.start_run(run_id)
        room = target() - loaded()
        if room <= 0:
            db.finish_run(run_id, "complete", "target reached")
            return {"status": "complete", "loaded": loaded(), "target": target()}
        limit = min(batch or int(os.getenv("MATERIALS_BATCH", "300")), int(room * 1.15) + 5)
        rows = candidates()[:limit]
        pulled = len(rows)
        rows = wave1_fill.fit_filter(rows, run_id)
        rows = wave1_fill.mx_filter(rows, run_id)
        phr, stats = phrases(rows)
        keep: list[dict] = []
        held = Counter()
        unclear_rows, bad_rows = [], []
        for r in rows:
            p = phr.get(r["company_domain"])
            if not p:
                unclear_rows.append(r)
                continue
            company = clean_company_name(r.get("company_name") or r["company_domain"])
            issues = materials.check_company(company) + materials.check_materials(p)
            if issues:
                bad_rows.append((r, "; ".join(issues)))
                held[issues[0]] += 1
                continue
            keep.append({**r, "materials_hauled": p})
        # Only rows the model could not describe are held for good; an API error is left in the bank for the next pass.
        really_unclear = [r for r in unclear_rows if r["company_domain"] in _unclear_domains(unclear_rows)]
        _hold(really_unclear, "MATERIALS_HAULED unclear: the model could not say what crosses this company's dock (held, never sent with filler)")
        for r, why in bad_rows:
            _hold([r], f"copy-field standard failed: {why}")
        keep = keep[:max(room, 0)]
        if keep:
            with db.cursor() as cur:
                cur.executemany("UPDATE contacts SET materials_hauled = %s WHERE email = %s", [(r["materials_hauled"], r["email"]) for r in keep])
        sent = scaling.load_leads(cid, keep, run_id) if keep else []
        total = loaded()
        unclear_pct = 100.0 * stats["unclear"] / max(stats["companies"] - stats["cached"], 1) if stats["inferred"] + stats["unclear"] else 0.0
        note = {"status": "complete" if total >= target() else "partial", "campaign": cid, "pulled": pulled, "loaded_now": len(sent), "loaded": total, "target": target(),
                "held_unclear": len(really_unclear), "held_standard": len(bad_rows), "standard_reasons": dict(held), "unclear_pct": round(unclear_pct, 1),
                "samples": [(clean_company_name(r.get("company_name") or r["company_domain"]), r["materials_hauled"]) for r in sent[:8]]}
        if stats["inferred"] + stats["unclear"] >= 30 and unclear_pct > 10:
            try:
                from .clients import slack
                slack.post(f":warning: Campaign {cid}: {unclear_pct:.0f}% of the companies in this batch could not be described for MATERIALS_HAULED "
                           f"(standard: above 10% means the prompt or the fit is wrong). They were held, not sent.")
            except Exception:
                pass
        db.finish_run(run_id, "complete", str({k: v for k, v in note.items() if k != "samples"})[:300])
        return note
    finally:
        _lock.release()


def _unclear_domains(rows: list[dict]) -> set[str]:
    """Domains whose cache row says 'unclear' (a deliberate answer), as opposed to a call that errored."""
    doms = sorted({r["company_domain"] for r in rows})
    with db.cursor() as cur:
        cur.execute("SELECT domain FROM materials_cache WHERE domain = ANY(%s) AND status = 'unclear'", (doms,))
        return {r["domain"] for r in cur.fetchall()}


def status() -> dict | None:
    """What the runway page and report show for the OG-copy campaign, or None when this mode is off."""
    cid = campaign_id()
    if not cid:
        return None
    with db.cursor() as cur:
        cur.execute("""SELECT count(*) FILTER (WHERE status = 'pushed' AND eb_campaign_id = %s) loaded,
                              count(*) FILTER (WHERE status = 'pushed' AND eb_campaign_id = %s AND updated_at > now() - interval '60 minutes') per_hour,
                              count(*) FILTER (WHERE drop_reason LIKE 'MATERIALS_HAULED unclear%%') held_unclear,
                              count(*) FILTER (WHERE drop_reason LIKE 'copy-field standard failed%%') held_standard
                       FROM contacts""", (cid, cid))
        row = dict(cur.fetchone())
    live: dict = {}
    try:
        from .clients import emailbison
        c = emailbison.get_campaign(cid) or {}
        live = {"name": c.get("name"), "status": c.get("status"), "sent": int(c.get("emails_sent") or 0), "interested": int(c.get("interested") or 0)}
    except Exception:
        pass
    return {"campaign": cid, "target": target(), **{k: int(v or 0) for k, v in row.items()}, **live}
