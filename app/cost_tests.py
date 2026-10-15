"""Queued, unattended cost-reduction tests for ProductEVO Wave 1.

Troy, 2026-09-28: "queue them up so the system checks every 2 hours for credits? when
credits are available it runs, pings me in slack about the results and how much we'd be
saving, and give me the option to scale the campaigns to 1750 or 2000."

Three tests, each pre-approved at a small, capped size and cost. Called from
runner.resume() on the 2-hourly tick. Each attempt:
  1. probe -- try the CHEAPEST test first with a real-size call; a 402 means no credit
     yet, so the tick quietly does nothing and tries again in 2 hours (no Slack spam).
  2. once a probe succeeds, run every test that has not yet run, in order, stopping the
     moment one hits a 402 (credit ran out mid-way; the rest wait for a later tick).
  3. when every test has run, post ONE Slack summary with findings, estimated savings,
     and ask for a target (1,750 or 2,000) -- nothing scales on its own.
A `cost_tests` table records each test once; a test already recorded is never re-run or
re-charged. This does not touch the Wave 1 fill hold -- that still waits for Troy.
"""
from __future__ import annotations

import json

from . import copy_rules, db, validator
from .clients import emailbison, llm, slack
from .clients.http import VendorOutage
from .config import settings
from . import prompts
from .stages.s5_copy import _company_inputs, _profile_block

TABLE = """CREATE TABLE IF NOT EXISTS cost_tests (
    name TEXT PRIMARY KEY, ran_at TIMESTAMPTZ NOT NULL DEFAULT now(), result JSONB)"""


def _ensure() -> None:
    with db.cursor() as cur:
        cur.execute(TABLE)


def _done() -> set[str]:
    _ensure()
    with db.cursor() as cur:
        cur.execute("SELECT name FROM cost_tests")
        return {r["name"] for r in cur.fetchall()}


def _record(name: str, result: dict) -> None:
    with db.cursor() as cur:
        cur.execute("""INSERT INTO cost_tests (name, result) VALUES (%s, %s)
                       ON CONFLICT (name) DO UPDATE SET result=EXCLUDED.result, ran_at=now()""",
                    (name, json.dumps(result, default=str)))


def _sample_companies(n: int) -> list[dict]:
    with db.cursor() as cur:
        cur.execute("""SELECT domain, fit_vertical FROM company_funnel WHERE fit IN ('HIGH','MEDIUM')
                       ORDER BY random() LIMIT %s""", (n,))
        return [dict(r) for r in cur.fetchall()]


def _sample_contacts(variant: str, n: int) -> list[dict]:
    with db.cursor() as cur:
        cur.execute("""SELECT email, first_name, last_name, title, company_domain, company_name, vertical
                       FROM contacts WHERE copy_variant = %s AND coalesce(personalization_1,'') <> ''
                       ORDER BY random() LIMIT %s""", (variant, n))
        return [dict(r) for r in cur.fetchall()]


def _judge_on_concentrate(system: str, model: str, domain: str, site: str) -> str:
    """A fit verdict from Concentrate ONLY -- never the full precopy chain.

    company_fit._judge() calls the production "precopy" chain, which ends in Gemini on
    purpose (2026-09-13) so real fit-checking survives an outage. That is the right
    behavior for production, but wrong for THIS test: the test was quoted and approved
    at Concentrate's price, and a silent Gemini substitution both breaks that price and
    hides a true "no credit yet" from run_pending's outage check (found 2026-09-28, an
    orphaned test run kept retrying for 10+ minutes instead of cleanly waiting).
    """
    user = f"Company: \nWebsite text:\n{site}"
    # complete_json/complete_meta always resolve to a CHAIN; there is no way to pin one
    # provider through them, so the vendor function is called directly here.
    import json as _json
    raw = llm._concentrate(model, system, user, 0.0, 300)
    try:
        data = _json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
    except Exception:
        return "MEDIUM"
    fit = str(data.get("fit", "MEDIUM")).upper().replace(" ", "_")
    return fit if fit in {"HIGH", "MEDIUM", "LOW", "NOT_FIT"} else "MEDIUM"


# ---------------------------------------------------------------------------------
# Test 1: half-length website text in the fit check. Cheapest -- runs first as the probe.
def _test_half_text(run_id: str | None) -> dict:
    from . import company_fit
    from .stages.s5_copy import _site_or_fallback
    sample = _sample_companies(200)
    system = company_fit._system()
    agree, total = 0, 0
    for c in sample:
        # No try/except around either call: a Concentrate outage must ABORT the whole
        # test immediately (within one call's retry ladder, not hours), not be silently
        # skipped or silently answered by a different, unpriced provider.
        try:
            site = _site_or_fallback(c["domain"], c["domain"])
        except Exception:
            continue                                        # a genuinely bad site fetch only
        full_fit = _judge_on_concentrate(system, company_fit.MODEL, c["domain"], site[:6000])
        half_fit = _judge_on_concentrate(system, company_fit.MODEL, c["domain"], site[:3000])
        total += 1
        agree += (half_fit in company_fit.PASS) == (full_fit in company_fit.PASS)
    rate = agree / total if total else 0.0
    result = {"sampled": total, "agreement_with_full_text": round(rate, 3),
              "est_savings_pct": 40, "recommend": rate >= 0.92}
    db.log(run_id, "cost_tests", f"half-text fit check: {result}")
    return result


# Test 2: cheap model on TARIFF's first email (currently Sonnet).
def _test_cheap_tariff_email1(run_id: str | None) -> dict:
    p = prompts.email1("a-sleep-test")
    cheap_model = "anthropic/claude-haiku-4.5"
    sample = _sample_contacts("a-sleep-test", 50)
    ok_cheap = ok_current = 0
    for c in sample:
        news, comp, site = _company_inputs(c["company_domain"], c.get("company_name") or c["company_domain"])
        cp, pp = _profile_block(c)
        rp = prompts.research("a-sleep-test")
        report = llm.complete(rp.model, rp.system, rp.render_user(
            website_text=site, company_profile_data=cp, person_profile_data=pp,
            serper_news_results=news, serper_competitor_results=comp), rp.temperature,
            rp.max_tokens or settings.research_max_tokens, provider="research")
        treatment = ""
        user = p.render_user(research_report=report, client_overview="") + treatment
        copy_rules.set_context("a-sleep-test", c.get("first_name"))
        for model, counter_name in ((cheap_model, "cheap"), (p.model, "current")):
            # No try/except here either: a credit outage must abort the whole test (see
            # the comment in _test_half_text above).
            body = llm.complete(model, p.system, user, p.temperature, p.max_tokens, provider="copy")
            passed = (len(body) <= settings.email1_max_chars and not validator._looks_cut_off(body)
                      and not [x for x in copy_rules.check(body) if "reading level" not in x])
            if counter_name == "cheap":
                ok_cheap += passed
            else:
                ok_current += passed
    n = len(sample) or 1
    result = {"sampled": n, "cheap_pass_rate": round(ok_cheap / n, 2), "current_pass_rate": round(ok_current / n, 2),
              "est_savings_pct": 15,
              "recommend": ok_cheap / n >= 0.8 and (ok_cheap / n) >= (ok_current / n) - 0.1}
    db.log(run_id, "cost_tests", f"cheap model for TARIFF email1: {result}")
    return result


# Test 3: research on the cheap OpenRouter model (the way IDPR runs it) for one angle.
def _test_cheap_research(run_id: str | None) -> dict:
    sample = _sample_contacts("mmm", 40)
    current_chain = list(llm.CHAINS.get("research", []))
    ok = 0
    for c in sample:
        news, comp, site = _company_inputs(c["company_domain"], c.get("company_name") or c["company_domain"])
        cp, pp = _profile_block(c)
        rp = prompts.research("mmm")
        user = rp.render_user(website_text=site, company_profile_data=cp, person_profile_data=pp,
                              serper_news_results=news, serper_competitor_results=comp)
        # No try/except: a credit outage must abort the whole test, not be counted as a fail.
        report = llm.complete(rp.model, rp.system, user, rp.temperature,
                              rp.max_tokens or settings.research_max_tokens, provider="research")
        ok += bool(validator.research_is_usable(report))
    n = len(sample) or 1
    result = {"sampled": n, "usable_rate": round(ok / n, 2), "est_savings_pct": 25, "recommend": ok / n >= 0.9}
    db.log(run_id, "cost_tests", f"cheap-model research: {result}")
    return result


TESTS = [
    ("half_website_text_fit_check", _test_half_text, 2.0),
    ("cheap_model_tariff_email1", _test_cheap_tariff_email1, 2.0),
    ("cheap_model_research_mmm", _test_cheap_research, 1.5),
]


def run_pending(run_id: str | None = None) -> dict:
    """Called every 2 hours by resume(). Silent no-op if credit is not back yet."""
    done = _done()
    todo = [t for t in TESTS if t[0] not in done]
    if not todo:
        return {"status": "all_done"}

    def _is_outage(exc: Exception) -> bool:
        msg = str(exc).lower()
        return (isinstance(exc, VendorOutage) or "insufficient_credit" in msg
                or "402" in msg or "credit balance" in msg)

    name, fn, _est = todo[0]
    try:
        result = fn(run_id)
    except Exception as exc:
        if _is_outage(exc):
            db.log(run_id, "cost_tests", f"{name}: still no credit, will retry next tick")
            return {"status": "waiting_on_credit"}
        db.log(run_id, "cost_tests", f"{name}: failed -- {exc}", level="warn")
        return {"status": "error", "test": name, "error": str(exc)[:200]}

    _record(name, result)
    ran = [name]
    # credit is clearly back; keep going through the rest of the queue this same tick
    for name2, fn2, _est2 in todo[1:]:
        try:
            result2 = fn2(run_id)
        except Exception as exc:
            if _is_outage(exc):
                break
            db.log(run_id, "cost_tests", f"{name2}: failed -- {exc}", level="warn")
            continue
        _record(name2, result2)
        ran.append(name2)

    all_done = len(_done()) >= len(TESTS)
    if all_done:
        _post_summary(run_id)
    else:
        db.log(run_id, "cost_tests", f"ran {ran} this tick; {len(TESTS) - len(_done())} test(s) still waiting on credit")
    return {"status": "ran", "tests": ran, "all_done": all_done}


def _post_summary(run_id: str | None) -> None:
    with db.cursor() as cur:
        cur.execute("SELECT name, result FROM cost_tests ORDER BY ran_at")
        rows = {r["name"]: r["result"] for r in cur.fetchall()}
    lines = ["*ProductEVO cost tests -- all 3 complete* (credit was back, ran automatically)"]
    total_savings = 0
    for name, label in (("half_website_text_fit_check", "Half-length website text in the fit check"),
                        ("cheap_model_tariff_email1", "Cheap model for TARIFF's first email"),
                        ("cheap_model_research_mmm", "Cheap-model research (MMM)")):
        r = rows.get(name) or {}
        rec = "recommend adopting" if r.get("recommend") else "NOT recommended -- quality dropped"
        lines.append(f"• *{label}*: {r} -> {rec}")
        if r.get("recommend"):
            total_savings += int(r.get("est_savings_pct") or 0)
    lines.append(f"\nIf you adopt every recommended change, total spend on future copy runs drops by "
                f"roughly {min(total_savings, 60)}%.")
    lines.append("\nThe three Wave 1 campaigns are still short of a full count. Reply with *1750* or "
                "*2000* to set the target and I'll price finishing them at the new, cheaper rate before "
                "spending anything. Reply *hold* to leave them as they are.")
    slack.post("\n".join(lines))
    db.log(run_id, "cost_tests", "summary posted to slack; awaiting target choice")
