"""The self-learning piece: a winning angle proposes its own successor.

Troy, 2026-10-06: "build it, it keeps the winners structure but uses a new angle and gets
tested with 1500 leads." Two stages, cleanly separated:

  1. propose_new_test_angle(parent_angle) -- free-ish (a handful of LLM calls). A strong
     model reads the winning angle's own prompts and ProductEVO's pitch, proposes ONE new
     angle that is genuinely different (a different psychological lever) but still true
     to what ProductEVO actually sells, drafts 2-3 real sample emails against real
     companies, and posts all of it to Slack. Nothing is registered, nothing spends.
  2. launch_new_test_angle(angle_id, parent_angle) -- the real spend (sourcing + copy for
     up to 1,500 leads, a brand new EmailBison campaign). Only runs when Troy says go;
     this is new, unproven creative, not "more of a known winner," so it keeps the
     approval gate the scaling step itself no longer has.

The angle then plugs into the EXACT same machinery MMM/TARIFF/TIME already use --
copy_test_campaigns, copy_test.toml's [angles]/[angle_prompts], rollup()/decide() -- so it
is scored by the same rules from day one, not a special case.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .clients import llm

_CLIENTS_DIR = Path(__file__).resolve().parent.parent / "clients"


def _client_dir() -> Path:
    """Troy, 2026-10-08: "if i were to use it for another client how do i go about it."
    These four used to be fixed at clients/productevo/, the one thing in this module
    that didn't already follow the rest of the engine's pattern (testgrid.load_config()
    resolves the ACTIVE client the same way, lazily, every call -- a module-level
    constant would freeze whichever client happened to be active at import time, wrong
    the moment two clients share this codebase, which IDPR and ProductEVO already do).
    """
    from . import client
    return _CLIENTS_DIR / client.current_name()


def _prompts_root() -> Path:
    return _client_dir() / "prompts"


def _copy_test_toml_path() -> Path:
    return _client_dir() / "copy_test.toml"


def _pitch_md_path() -> Path:
    return _client_dir() / "pitch.md"


def _exclusions_md_path() -> Path:
    return _client_dir() / "exclusions.md"

_BRAINSTORM_SYSTEM = """You are a senior B2B cold-email strategist. ProductEVO is a fractional
product development and manufacturing team: it takes US consumer product brands from idea to
production, factory direct, flat fees, no markups, month-to-month, about 120 to 150 days from
idea to production. Its buyer is a founder or product operator personally trapped in their own
supply chain, chasing samples, requoting, catching defects.

You will be shown a WINNING angle: its full research/email1/email2 prompts. Your job is to
propose TWO new angles to test next, for the operator to pick one, pick the other, or launch both.

Both candidates must target the EXACT SAME pain point and the exact same core promise as the
winner, not merely a nearby lever -- stricter than a family resemblance. What must NOT differ:
the underlying pain, the core promise, or anything ProductEVO actually sells. If a reader who
liked the winner would find either candidate a strange pivot, it is wrong -- redo it closer.

What MUST differ, and differ enough to justify running it as its own standalone test, not just a
cosmetic variant: each candidate needs its own distinct RHETORICAL MECHANISM for making the same
promise land, not merely a different opening line or trigger. Examples of a real mechanism shift:
loss framing (this exact sale is already happening, to someone else, right now) versus gain
framing (you could capture this additional sale going forward); a concrete, present-tense observed
fact versus a forward-looking opportunity; third-party proof versus a direct observation about
their own company. A difference that is ONLY a different trigger or hook, with the same underlying
mechanism as the winner, is NOT enough -- it risks being too similar to the winner to produce a
result that is actually distinguishable from it. The two candidates must also use two DIFFERENT
mechanisms from each other, not the same mechanism twice with different words. State the mechanism
explicitly in "reasoning" so it's clear why this candidate is a real, separable test and not a
reskin. Read the exclusions below; never violate them.

Think it through first, briefly, then answer. Reply with JSON only:
{
  "angles": [
    {
      "reasoning": "name the rhetorical mechanism this candidate uses (not just the hook), and "
                   "1 sentence on why that mechanism could move reply/interest rate versus the "
                   "winner's mechanism, given both share the same pain point and promise",
      "angle_id": "kebab-case-id, 2-4 words, no 'test' or 'angle' in it",
      "display_name": "Short Human Name",
      "pitch_one_liner": "the one-sentence pitch, for a human reading a report",
      "email1_voice": "a tight paragraph of concrete instructions for email 1's system prompt: "
                      "what to open with, what tone, what NOT to say, how it should feel reading it",
      "email2_voice": "same, for email 2 (the follow-up, always opens admitting email 1 didn't land)",
      "research_focus": "what this angle's research brief needs to extract from a company's "
                        "website/profile that the other angles do not care about"
    },
    { ... second candidate, same shape, a different execution of the SAME pain point/promise ... }
  ]
}"""

# Troy, 2026-10-08: earlier derived angles never identified ProductEVO anywhere in the
# email -- the STRUCTURE section below now makes the paragraph-3 pivot mandatory.
# Found the same day, the hard way: a draft generation for companion-gear-gap echoed a
# quoted fragment of Troy's own feedback back as literal email copy, because an earlier
# version of this fix put that quote INSIDE the "SYSTEM PROMPT (verbatim):" block below --
# anything between those --- markers is sent to the LLM as-is, so a quoted human comment
# there is live input the model can regurgitate, not inert documentation. Rationale for
# any instruction change belongs here, in a Python comment outside the string, never
# quoted inside the wrapper itself.
_EMAIL1_WRAPPER = """SOURCE: self-learning angle derivation, proposed {date} off the winning
        angle {parent}. Reasoning: {reasoning}
VARIANT: {angle_id} -- {pitch}
model: anthropic/claude-sonnet-5
temperature: 0.7
max_tokens: 800
SYSTEM PROMPT (verbatim):
---
You are writing cold email 1 for ProductEVO, a fractional product development and
manufacturing team (flat fees, no markups, month-to-month, 120-150 days idea to
production). {voice}

STRUCTURE (strict order, required on every email, not optional, not part of the voice):
1. TRIGGER/HOOK -- one sentence, the specific thing this angle leans on.
2. THE PROMISE plus CHECK-IN -- one sentence making this angle's promise concrete, then
   one short question.
3. PIVOT plus OFFER -- a pivot line that explicitly names ProductEVO, then one sentence
   on building it (the 120-150 day timeline, flat fee, or margin of owning it). Use
   one of, or a close variant of: "Reason I ask is I am with ProductEVO." / "Asking
   because that is what we handle at ProductEVO." / "If so, that is where we come in.
   We are ProductEVO." This paragraph is NOT optional -- an email that never identifies
   ProductEVO is wrong no matter how good the hook is.
4. CLOSE -- one short question, digital only.

PHYSICAL PRODUCTS ONLY: never suggest or imply software, apps, platforms, services,
training, subscriptions, or anything ProductEVO does not build. Never invent a stat;
the only numbers ever allowed are 17 years in business, 1,000+ products launched,
5.6M+ units shipped, 0.005% defect rate, factory direct, flat fee, no markups,
month-to-month, 120 to 150 days idea to production -- and only if the research
actually calls for one of them.

Keep it under 500 characters. Plain text, no greeting, no sign-off, no subject line.
Output the email body only.
---
USER MESSAGE TEMPLATE:
RESEARCH DATA
{{research_report}}

ABOUT THE SENDER'S COMPANY:
{{client_overview}}

Write the email body only. Count characters. Output nothing else.
"""

_EMAIL2_WRAPPER = """SOURCE: self-learning angle derivation, proposed {date} off the winning
        angle {parent}. Reasoning: {reasoning}
VARIANT: {angle_id} -- {pitch}
model: anthropic/claude-haiku-4.5
temperature: 0.6
max_tokens: 700
SYSTEM PROMPT (verbatim):
---
You are writing cold email 2 for ProductEVO (the follow-up to email 1, which got no
reply). Open by admitting the first note didn't land well, then make the same point
again, more directly. {voice}

PHYSICAL PRODUCTS ONLY, same rule as email 1. Only the same verified numbers may ever
appear, and only if relevant: 17 years in business, 1,000+ products launched, 5.6M+
units shipped, 0.005% defect rate, factory direct, flat fee, no markups, month-to-month,
120 to 150 days idea to production.

Do NOT exceed 700 characters. Plain text, no greeting, no sign-off. Output the email
body only.
---
USER MESSAGE TEMPLATE:
Personalization 1 (Email 1, already sent):
{{email_1_output}}

Prospect Research:
{{research_report}}

ABOUT THE SENDER'S COMPANY:
{{client_overview}}

Output nothing else.
"""

_RESEARCH_WRAPPER = """SOURCE: self-learning angle derivation, proposed {date} off the winning
        angle {parent}. Reasoning: {reasoning}
VARIANT: {angle_id} -- {pitch}
model: anthropic/claude-haiku-4.5
temperature: 0.3
max_tokens: 1200
SYSTEM PROMPT (verbatim):
---
You are researching a company for a ProductEVO cold email. Write a compact brief
(120-200 words). {research_focus}

Extract:
1. TRIGGER: one specific, publicly observable signal -- a launch, a hire, a retail
   placement, a trade show. Never funding news. If nothing specific exists, write
   "no trigger found" plainly.
2. PRODUCTS: what they actually make, specific nouns, never "consumer products."
3. SPECIFIC DETAIL: one concrete, personal detail about the company or founder.
4. icp_fit: HIGH, MEDIUM, LOW or NOT_FIT, then one short reason. NOT_FIT only on
   positive evidence the company is a retailer, distributor, service business,
   contract manufacturer, or a brand whose whole line is apparel, food, supplements,
   cosmetics or jewelry. A brand that designs and sells its own products is a fit
   even if a factory builds them, and even if it sells only to other businesses.
   If you cannot tell, write MEDIUM. Never NOT_FIT for missing information alone.

FALLBACK: if the website could not be fetched, use the name-search result instead;
if neither gives you anything, build PRODUCTS from the company profile fields alone
and write TRIGGER: no trigger found.

OUTPUT FORMAT: plain text, labeled lines: icp_fit / TRIGGER / PRODUCTS / SPECIFIC DETAIL.
The icp_fit line comes FIRST.
---
USER MESSAGE TEMPLATE:
Website text:
{{website_text}}

Company profile:
{{company_profile_data}}

Person profile:
{{person_profile_data}}

Recent news:
{{serper_news_results}}

Competitors:
{{serper_competitor_results}}
"""


def _parent_reference(parent_angle: str) -> str:
    from . import testgrid
    cfg = testgrid.load_config()
    variant = (cfg.get("angle_prompts") or {}).get(parent_angle, parent_angle.lower())
    out = []
    for stem in ("prompt_research.txt", "prompt_email1.txt", "prompt_email2.txt"):
        p = _prompts_root() / variant / stem
        if p.exists():
            out.append(f"--- {stem} ---\n{p.read_text(encoding='utf-8')[:3000]}")
    return "\n\n".join(out)


def _brainstorm(parent_angle: str) -> list[dict]:
    """Troy, 2026-10-07: "can we use a more expensive version to decide?" -- this is the
    one real judgment call in the whole derivation pipeline (what's a genuine sibling of
    the winner vs. a pivot), so it's worth Opus over Sonnet here, unlike the bulk/
    mechanical steps (company enforcement, per-company fit) that stay on cheap models on
    purpose. Returns exactly two candidates, both held to the same pain-point/promise
    as the winner (Troy: "angles must be similar in terms of pain point and everything").
    """
    exclusions = _exclusions_md_path().read_text(encoding="utf-8") if _exclusions_md_path().exists() else ""
    user = (f"WINNING ANGLE ({parent_angle}):\n{_parent_reference(parent_angle)}\n\n"
           f"EXCLUSIONS (never violate these):\n{exclusions}")
    # "precopy" (not "copy"): that chain tries Anthropic direct first, which is the only
    # provider confirmed to serve Opus 5.5 by model id in this codebase; "copy" tries
    # Concentrate first and has no Anthropic fallback, an unnecessary risk for a one-off
    # strategic call that isn't on the hot path.
    out = llm.complete_json("anthropic/claude-opus-5.5", _BRAINSTORM_SYSTEM, user, 0.8, max_tokens=4000,
                            provider="precopy")
    angles = (out or {}).get("angles") if isinstance(out, dict) else None
    if not isinstance(angles, list) or len(angles) < 2:
        raise RuntimeError(f"brainstorm did not return two usable angles: {str(out)[:300]}")
    cleaned = []
    seen_ids = set()
    for a in angles[:2]:
        if not isinstance(a, dict) or not a.get("angle_id"):
            raise RuntimeError(f"brainstorm angle missing angle_id: {str(a)[:300]}")
        a["angle_id"] = re.sub(r"[^a-z0-9-]+", "-", a["angle_id"].lower()).strip("-")
        if a["angle_id"] in seen_ids:
            a["angle_id"] = a["angle_id"] + "-b"
        seen_ids.add(a["angle_id"])
        cleaned.append(a)
    return cleaned


def _write_prompt_files(angle_id: str, parent_angle: str, proposal: dict) -> Path:
    folder = _prompts_root() / angle_id
    folder.mkdir(parents=True, exist_ok=True)
    today = time.strftime("%Y-%m-%d")
    ctx = {"date": today, "parent": parent_angle, "angle_id": angle_id,
          "pitch": proposal["pitch_one_liner"], "reasoning": proposal["reasoning"]}
    files = {
        "prompt_email1.txt": _EMAIL1_WRAPPER.format(**ctx, voice=proposal["email1_voice"]),
        "prompt_email2.txt": _EMAIL2_WRAPPER.format(**ctx, voice=proposal["email2_voice"]),
        "prompt_research.txt": _RESEARCH_WRAPPER.format(**ctx, research_focus=proposal["research_focus"]),
    }
    for name, content in files.items():
        (folder / name).write_text(content, encoding="utf-8")
    _persist_prompt_files(angle_id, files)
    return folder


def _persist_prompt_files(angle_id: str, files: dict[str, str]) -> None:
    """Troy, 2026-10-08: twice now a proposed-but-unlaunched angle's prompt files got
    silently destroyed by an unrelated deploy (they only ever lived on the container's
    ephemeral disk) -- once discovered as copy that leaked a quoted comment, once as
    "Next Season Lineup" getting approved verbally with nothing left to actually launch.
    Persisting the content here means a deploy can no longer erase a pending proposal."""
    with db.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS angle_prompt_files (
            angle_id TEXT, filename TEXT, content TEXT, saved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (angle_id, filename))""")
        cur.executemany(
            """INSERT INTO angle_prompt_files (angle_id, filename, content) VALUES (%s,%s,%s)
               ON CONFLICT (angle_id, filename) DO UPDATE SET content=EXCLUDED.content, saved_at=now()""",
            [(angle_id, name, content) for name, content in files.items()])


def _restore_prompt_files_if_missing(angle_id: str) -> bool:
    """Self-heals a wiped proposal before it's used: if the folder a deploy erased has a
    saved copy in the database, rewrite it to disk. Returns True if the files are present
    (restored or already there), False if nothing was ever saved for this angle_id."""
    folder = _prompts_root() / angle_id
    if (folder / "prompt_email1.txt").exists():
        return True
    with db.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS angle_prompt_files (
            angle_id TEXT, filename TEXT, content TEXT, saved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (angle_id, filename))""")
        cur.execute("SELECT filename, content FROM angle_prompt_files WHERE angle_id = %s", (angle_id,))
        rows = cur.fetchall()
    if not rows:
        return False
    folder.mkdir(parents=True, exist_ok=True)
    for r in rows:
        (folder / r["filename"]).write_text(r["content"], encoding="utf-8")
    return True


def _sample_companies(parent_angle: str, n: int = 3) -> list[dict]:
    """Real companies the WINNING angle already researched, so samples are grounded in
    real data instead of invented."""
    from . import testgrid
    cfg = testgrid.load_config()
    variant = (cfg.get("angle_prompts") or {}).get(parent_angle)
    with db.cursor() as cur:
        cur.execute("""SELECT email, first_name, last_name, title, company_name, company_domain,
                              vertical, research_report
                       FROM contacts WHERE copy_variant = %s AND coalesce(research_report,'') <> ''
                       ORDER BY random() LIMIT %s""", (variant, n))
        return [dict(r) for r in cur.fetchall()]


def _draft_sample(angle_id: str, company: dict) -> dict:
    from . import prompts as _prompts
    overview = _pitch_md_path().read_text(encoding="utf-8") if _pitch_md_path().exists() else ""
    p1 = _prompts.email1(angle_id)
    report = company["research_report"]
    user1 = p1.render_user(research_report=report, client_overview=overview)
    body1 = llm.complete(p1.model, p1.system, user1, p1.temperature, p1.max_tokens, provider="copy")
    p2 = _prompts.email2(angle_id)
    user2 = p2.render_user(email_1_output=body1, research_report=report, client_overview=overview)
    body2 = llm.complete(p2.model, p2.system, user2, p2.temperature, p2.max_tokens, provider="copy")
    return {"company": company["company_name"] or company["company_domain"],
            "title": company.get("title"), "email1": body1, "email2": body2}


def propose_new_test_angle(parent_angle: str, run_id: str | None = None) -> dict:
    """Stage 1: propose TWO candidates + draft samples for each + post both to Slack,
    side by side, so Troy can pick one, pick the other, or launch both (Troy, 2026-10-07:
    "we want 2 angles so i can pick one or maybe both"). No registration, no spend beyond
    the handful of LLM calls this makes."""
    from .clients import slack
    proposals = _brainstorm(parent_angle)
    companies = _sample_companies(parent_angle, 3)

    results = []
    lines = [f":bulb: *Two angles proposed off {parent_angle}, both close to it on purpose* "
            f"-- pick one, pick the other, or launch both. Reply *launch <id>* with a lead "
            f"count (default 1,500) once you're happy; nothing is registered or spent until then."]
    for proposal in proposals:
        angle_id = proposal["angle_id"]
        folder = _write_prompt_files(angle_id, parent_angle, proposal)
        samples = [_draft_sample(angle_id, c) for c in companies]
        results.append({"angle_id": angle_id, "proposal": proposal, "samples": samples,
                        "folder": str(folder)})
        lines.append("")
        lines.append(f"---- *{proposal['display_name']}* (`{angle_id}`) ----")
        lines.append(f"_{proposal['reasoning']}_")
        lines.append(f"Pitch: {proposal['pitch_one_liner']}")
        for s in samples:
            lines.append(f"*{s['company']}* ({s['title'] or 'unknown title'})")
            lines.append(f"Email 1: {s['email1']}")
            lines.append(f"Email 2: {s['email2']}")
            lines.append("")
    slack.post("\n".join(lines))
    # Troy, 2026-10-08: "there should be a button to approve the lookalike campaigns" --
    # "Reply launch <id>" above was prose nothing ever parsed. A real click now does
    # exactly what that reply was supposed to do: launch_new_test_angle at 1,500 leads.
    slack.post("Approve a lookalike to launch (1,500 leads), or launch both:", blocks=[
        {"type": "actions", "elements": [
            {"type": "button", "style": "primary",
             "text": {"type": "plain_text", "text": f"Launch {r['proposal']['display_name']}"},
             # Slack requires a unique action_id per element in one message (found
             # 2026-10-08: two buttons sharing "launch_lookalike" got the whole message
             # rejected with invalid_blocks) -- suffix it, and match on the PREFIX in
             # interaction_to_command instead of an exact id.
             "action_id": f"launch_lookalike:{r['angle_id']}", "value": f"{r['angle_id']}|{parent_angle}"}
            for r in results
        ]},
    ])
    ids = [r["angle_id"] for r in results]
    db.log(run_id, "angle_derivation", f"proposed {ids} off {parent_angle}")
    return {"status": "proposed", "parent": parent_angle, "angles": results}


def launch_new_test_angle(angle_id: str, parent_angle: str, target_leads: int = 1500,
                          run_id: str | None = None) -> dict:
    """Stage 2: the real spend. Only call this after Troy has said go."""
    from . import scaling, testgrid
    from .clients import emailbison, slack
    run_id = run_id or "launch-angle-" + time.strftime("%Y%m%dT%H%M%S")
    db.start_run(run_id)

    _restore_prompt_files_if_missing(angle_id)
    folder = _prompts_root() / angle_id
    if not (folder / "prompt_email1.txt").exists():
        return {"status": "error", "reason": f"no prompt files staged for {angle_id} -- propose it first"}

    with db.cursor() as cur:
        cur.execute("SELECT campaign_id FROM copy_test_campaigns WHERE angle=%s AND is_primary", (parent_angle,))
        row = cur.fetchone()
    if not row:
        return {"status": "error", "reason": f"no primary campaign registered for {parent_angle}"}
    model_id = int(row["campaign_id"])

    display = angle_id.replace("-", " ").title()
    created = emailbison.duplicate_campaign(model_id, f"PEVO_W1_{angle_id.upper().replace('-', '_')}")
    new_campaign_id = int(created["id"])

    # Wire the angle into copy_test.toml and register it -- same structure the other
    # three angles use, so scoring treats it identically from day one.
    cfg_text = _copy_test_toml_path().read_text(encoding="utf-8")
    angle_key = angle_id.upper().replace("-", "_")
    if f"\n{angle_key} " not in cfg_text and f"\n{angle_key}=" not in cfg_text:
        cfg_text = cfg_text.replace("[angles]\n", f"[angles]\n{angle_key} = \"{display}\"\n", 1)
    if f'{angle_key} = "{angle_id}"' not in cfg_text:
        cfg_text = re.sub(r"(\[angle_prompts\]\n)", rf'\1{angle_key} = "{angle_id}"\n', cfg_text, count=1)
    _copy_test_toml_path().write_text(cfg_text, encoding="utf-8")
    from datetime import datetime, timezone
    testgrid.register_campaign(angle_key, new_campaign_id, launched_at=datetime.now(timezone.utc))

    cfg = testgrid.load_config()
    tested = [v for verts in cfg["segments"].values() for v in verts]
    result = scaling.feed_campaign(new_campaign_id, angle_id, tested, target_leads, run_id)

    # Troy, 2026-10-08: found while launching Next Season Lineup -- this one call to
    # feed_campaign (capped at 4 rounds) can fall short of target_leads with nothing left
    # to continue it; the winner-scaling path already solved exactly this with
    # scale_targets + tick_scale_ups, so register here too instead of leaving a launch
    # permanently short.
    testgrid.auto_expand_champion(angle_key, run_id, target_leads=target_leads, requires_approval=False)

    slack.post(f":rocket: *{display} launched* (campaign {new_campaign_id}, off the back of "
              f"{parent_angle}): {result.get('loaded', 0)} leads loaded so far, target {target_leads}. "
              f"Ticks every 2 hours until it reaches target, same as a winner scale-up. "
              f"It's now scored by the same rules as every other angle.")
    db.finish_run(run_id, "complete", f"loaded={result.get('loaded', 0)}")
    # The angle this one is derived FROM, kept so the 60%-mark checkpoint knows what
    # "similar performance" should be measured against.
    with db.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS angle_derivations (
            angle TEXT PRIMARY KEY, parent_angle TEXT NOT NULL, checked_60pct BOOLEAN NOT NULL DEFAULT FALSE)""")
        cur.execute("""INSERT INTO angle_derivations (angle, parent_angle) VALUES (%s,%s)
                       ON CONFLICT (angle) DO UPDATE SET parent_angle=EXCLUDED.parent_angle""",
                    (angle_key, parent_angle))
    return {"status": "launched", "angle": angle_key, "campaign_id": new_campaign_id, "result": result}


INTERESTED_BAR = 0.06   # Troy, 2026-10-07: "if above six percent we scale, if not we kill"


def check_derived_angles(run_id: str | None = None) -> dict:
    """Troy, 2026-10-07: "after 7 days is when the test on lookalike angles work." Call
    nightly. For every launched lookalike angle at least 7 days old and not yet checked:
    interested-as-a-percentage-of-replies >= 6% -> scale it (same exponential doubling as
    the main winner, via auto_expand_champion); below 6% -> kill it outright (paused, like
    a day-10 loser). Flat bar, not relative to the parent -- replaces the 2026-10-06
    60%-sent/parent-relative design (retired same day it shipped, before it ever fired).

    Troy, 2026-10-07: "no keep emailbison's count" -- uses EmailBison's own campaign
    totals (unique_replies, interested), not our own filtered reply count. EmailBison's
    replied count includes automated out-of-office replies ours deliberately excludes;
    that's a known, deliberate difference, and Troy wants EmailBison's number here anyway.
    """
    from . import testgrid
    from .clients import emailbison, slack
    with db.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS angle_derivations (
            angle TEXT PRIMARY KEY, parent_angle TEXT NOT NULL, checked_60pct BOOLEAN NOT NULL DEFAULT FALSE)""")
        cur.execute("SELECT angle, parent_angle FROM angle_derivations WHERE NOT checked_60pct")
        rows = [dict(r) for r in cur.fetchall()]
    if not rows:
        return {"status": "nothing_to_check"}

    checked = []
    for r in rows:
        angle, parent = r["angle"], r["parent_angle"]
        with db.cursor() as cur:
            cur.execute("SELECT campaign_id, launched_at FROM copy_test_campaigns WHERE angle=%s AND is_primary", (angle,))
            row = cur.fetchone()
        if not row or not row["launched_at"]:
            continue
        launched_at = row["launched_at"]
        if launched_at.tzinfo is None:
            launched_at = launched_at.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - launched_at).days
        if age_days < 7:
            continue
        c = emailbison.get_campaign(int(row["campaign_id"])) or {}
        replied = int(c.get("unique_replies") or 0)
        interested_pct = (int(c.get("interested") or 0) / replied) if replied else 0.0
        passed = interested_pct >= INTERESTED_BAR
        with db.cursor() as cur:
            cur.execute("UPDATE angle_derivations SET checked_60pct = TRUE WHERE angle=%s", (angle,))
        checked.append({"angle": angle, "day": age_days, "interested_pct": interested_pct, "passed": passed})
        if passed:
            slack.post(f":white_check_mark: *{angle}* at day {age_days}: {interested_pct:.1%} interested-of-replies, "
                       f"clears the 6% bar. Scaling it to {testgrid.LOOKALIKE_AUTOSCALE_TARGET:,} -- it'll check "
                       f"in with you before going further.")
            try:
                # Troy, 2026-10-08: a lookalike auto-scales to 5k, then stops and asks a
                # human before the final push to 10k -- it hasn't earned the main winner's
                # no-questions-asked ceiling yet, just a clean pass on the 6% bar.
                testgrid.auto_expand_champion(angle, run_id, target_leads=testgrid.LOOKALIKE_AUTOSCALE_TARGET,
                                              requires_approval=True)
            except Exception as exc:
                db.log(run_id, "angle_derivation", f"could not scale {angle}: {exc}", level="warn")
        else:
            slack.post(f":axe: *{angle}* at day {age_days}: only {interested_pct:.1%} interested-of-replies, "
                       f"below the 6% bar. Killing it.")
            try:
                testgrid.pause_angle(angle, int(row["campaign_id"]))
            except Exception as exc:
                db.log(run_id, "angle_derivation", f"could not pause {angle}: {exc}", level="warn")
    db.log(run_id, "angle_derivation", f"day-7 checkpoint: {checked}")
    return {"status": "checked", "results": checked}


# Lumion build: the brainstorm, email and research prompts above are written for ProductEVO. A client opts in with
#     [angle_learning]
#     enabled = true
# in its copy_test.toml, after those prompts have been adapted to its own pitch. Until then every entry point is a no-op
# that returns a status, so a day-10 verdict or the nightly review cannot spend money or post ProductEVO wording.
def _enabled() -> bool:
    try:
        from . import testgrid
        return bool(((testgrid.load_config() or {}).get("angle_learning") or {}).get("enabled", False))
    except Exception:
        return False


def _guard(fn):
    def wrapper(*args, **kwargs):
        if not _enabled():
            return {"status": "disabled_for_client"}
        return fn(*args, **kwargs)
    wrapper.__name__, wrapper.__doc__ = fn.__name__, fn.__doc__
    return wrapper


propose_new_test_angle = _guard(propose_new_test_angle)
launch_new_test_angle = _guard(launch_new_test_angle)
check_derived_angles = _guard(check_derived_angles)
