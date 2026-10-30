"""Which of the three jobs this run is doing, and how to choose.

Troy, 2026-09-18: one script you hand a client and a mode, and it runs.

    python -m app.runner client --list
    python -m app.runner client --client productevo                 (mode from evidence)
    python -m app.runner client --client productevo --mode copy-test
    python -m app.runner client --client acme --mode source --audience "boutique law firms"

The three modes exist because a client's unknown is different at each stage:

  source     Nothing has been sent and there is no message yet. Source companies and
             contacts, hand back a verified list, stop before copy. No sending, so no
             domain risk and no approval gate. The safe way to onboard.

  copy-test  There is history and it is underperforming. The audience is held FIXED and
             the copy is the variable: one vertical, split across the client's declared
             copy variants, judged on reply rate because interested replies are far too
             sparse to separate two arms at one vertical's volume.

  expand     The copy works. Hold it fixed and search for audiences -- the weekly loop.

Running copy-test and expand at once is the mistake to avoid: varying message and
audience together means a bad result attributes to neither.
"""

from __future__ import annotations

import json
import os
import statistics as st
import threading
from dataclasses import dataclass

import httpx

from . import client
# Imported for its side effect as much as its value: config loads the .env,
# and every client key here is read through os.getenv. Without it an entry
# point that never touches settings sees no keys and reports 'no history'.
from .config import settings  # noqa: F401

MODES = ("source", "copy-test", "expand")

# One id for the whole copy test, written on every exposure so the
# comparison and the learning page can find it.
COPY_TEST_EXPERIMENT = "copy-test-v1"

# Below this share of replies being interested, a client's copy is the problem worth
# solving before its audiences are. IDPR sits near 8% and is expanding; ProductEVO's
# most recent campaigns sit near 1.3%.
WEAK_INTERESTED_OF_REPLIES = 0.04
# Enough sending to have an opinion at all.
MIN_SENDS_FOR_EVIDENCE = 5_000

_FOLLOWUP = ("interested", "reply", "replies", "follow", "subsequence", "nurture",
             "re-engage", "booked", "meeting", "no-show", "unsubscribe", "exclusion", "test")


@dataclass(frozen=True)
class Evidence:
    campaigns: int
    sends: int
    reply_rate: float
    interested_of_replies: float
    recent_interested_of_replies: float
    mode: str
    why: str


def _campaigns(profile) -> list[dict]:
    """Prospecting campaigns from the client's own EmailBison workspace."""
    key = profile.emailbison_key()
    if not key or not profile.workspace_id:
        return []
    base = os.getenv("EMAILBISON_BASE", "https://send.buzzlead.io")
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json",
               "X-Workspace-Id": str(profile.workspace_id)}
    out: list[dict] = []
    page, last = 1, 1
    while page <= last and page <= 20:
        try:
            r = httpx.get(f"{base}/api/campaigns", headers=headers,
                          params={"page": page, "per_page": 100}, timeout=60).json()
        except Exception:
            break
        out += [d for d in (r.get("data") or []) if isinstance(d, dict)]
        last = int((r.get("meta") or {}).get("last_page") or 1)
        page += 1
    rows = []
    for c in out:
        sends = c.get("emails_sent") or 0
        leads = c.get("total_leads") or 0
        name = str(c.get("name") or "").lower()
        if sends < 1000 or leads <= 0:
            continue
        if (c.get("interested") or 0) > leads:          # counters reset; impossible
            continue
        if any(w in name for w in _FOLLOWUP):           # not a prospecting campaign
            continue
        rows.append({"sends": sends, "replies": c.get("replied") or 0,
                     "interested": c.get("interested") or 0,
                     "created": str(c.get("created_at") or "")})
    rows.sort(key=lambda x: x["created"])
    return rows


def detect(profile=None) -> Evidence:
    """Pick the mode from the client's own sending history."""
    profile = profile or client.profile()
    rows = _campaigns(profile)
    if not rows:
        return Evidence(0, 0, 0.0, 0.0, 0.0, "source",
                        "no sending history in this workspace, so there is no message "
                        "to test and nothing to expand from")

    sends = sum(r["sends"] for r in rows)
    replies = sum(r["replies"] for r in rows)
    interested = sum(r["interested"] for r in rows)
    reply_rate = replies / sends if sends else 0.0
    ofr = interested / replies if replies else 0.0

    recent = rows[-6:]
    r_rep = sum(r["replies"] for r in recent)
    r_int = sum(r["interested"] for r in recent)
    recent_ofr = r_int / r_rep if r_rep else 0.0

    if sends < MIN_SENDS_FOR_EVIDENCE:
        mode, why = "source", (f"only {sends:,} sends on record, too little to judge the "
                               f"copy; build the list first")
    elif recent_ofr < WEAK_INTERESTED_OF_REPLIES:
        mode, why = "copy-test", (
            f"recent campaigns convert {recent_ofr:.1%} of replies against "
            f"{ofr:.1%} lifetime; the message is the problem to solve before the audience")
    else:
        mode, why = "expand", (f"recent campaigns convert {recent_ofr:.1%} of replies, "
                               f"so the copy holds; search for audiences")
    return Evidence(len(rows), sends, reply_rate, ofr, recent_ofr, mode, why)


def resolve(name: str | None, mode: str | None) -> tuple[object, str, Evidence | None]:
    """(profile, mode, evidence). Explicit mode wins; profile default beats detection."""
    profile = client.load(name) if name else client.profile()
    if mode and mode not in MODES:
        raise SystemExit(f"unknown mode {mode!r}; choose one of {', '.join(MODES)}")
    if mode:
        return profile, mode, None
    ev = detect(profile)
    return profile, (profile.default_mode or ev.mode), ev


def vendors_down() -> list[str]:
    """Accounts that will refuse this run, checked before it spends anything.

    One cheap call each. On 2026-09-21 four accounts were dry at once -- Anthropic,
    Apollo, Concentrate and AI Ark -- and a run started in that state does the whole
    sourcing dance before dying at the first company search. Worse, it dies far enough
    in that it looks like a targeting problem rather than a billing one.
    """
    import httpx

    from .config import settings

    out: list[str] = []
    if settings.aiark_api_key:
        try:
            r = httpx.post(
                "https://api.ai-ark.com/api/developer-portal/v1/companies",
                headers={"X-TOKEN": settings.aiark_api_key,
                         "Content-Type": "application/json"},
                json={"account": {"location": {"any": {"include": ["United States"]}}},
                      "page": 0, "size": 1}, timeout=25)
            if r.status_code in (401, 402, 403):
                out.append(f"company search (AI Ark): HTTP {r.status_code} "
                           f"{r.text[:90]}")
        except Exception:
            pass                       # a network blip is not a billing problem
    if settings.concentrate_api_key:
        try:
            r = httpx.post(
                "https://api.concentrate.ai/v1/messages",
                headers={"x-api-key": settings.concentrate_api_key,
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 4,
                      "messages": [{"role": "user", "content": "ok"}]}, timeout=25)
            if r.status_code in (401, 402, 403):
                out.append(f"copy (Concentrate): HTTP {r.status_code} {r.text[:90]}")
        except Exception:
            pass
    return out


def _has_company_source() -> bool:
    """QuickEnrich or Lead Ocean is configured, so companies can still be sourced without AI Ark."""
    from .config import settings as _s
    return bool(_s.quickenrich_api_key or _s.leadocean_api_key)


def preflight(profile, mode: str) -> list[str]:
    """Reasons this client must not run in this mode yet. Empty means go."""
    problems: list[str] = []
    if mode in ("copy-test", "expand"):
        if not profile.is_complete_for_sending:
            problems.append(
                f"profile is incomplete for sending: {', '.join(profile.fell_back)} "
                f"fell back to another client's files. Sending would use the wrong "
                f"positioning.")
        if not profile.emailbison_key():
            problems.append(
                f"no EmailBison key: profile names {profile.emailbison_key_env!r} "
                f"and it is not set in this environment")
    if mode == "copy-test" and len(profile.variant_names()) < 2:
        problems.append("copy-test needs at least two copy variants declared in "
                        "client.toml, each with its own prompt folder")

    # STANDING RULES for a copy test, locked in 2026-09-21 after ProductEVO's first
    # sample came back with the wrong audience and banned personas in it.
    #
    # A copy test holds the AUDIENCE fixed and varies the message. That only means
    # anything if the audience is the client's real one. Inferring it from an ICP
    # paragraph is how ProductEVO ended up in Medical Devices -- their LAST priority,
    # against a pitch that never mentions it -- with 17 of 20 follow-ups correctly
    # telling the prospect there was no fit. Every one of these is written down in a
    # normal onboarding package, so requiring them costs nothing and catches the case
    # where nobody read it.
    if mode == "copy-test":
        if not getattr(profile, "verticals", ()):
            problems.append(
                "no verticals declared: add `verticals = [...]` to client.toml in the "
                "client's own priority order, from their onboarding package. Without "
                "them the engine infers audiences from icp.md and can pick the one "
                "they rank last")
        if not profile.excluded_personas():
            problems.append(
                "no excluded personas: end exclusions.md with a line like 'Excluded "
                "personas: Marketing, Sales, Finance, HR, Legal'. Without it the "
                "generic title ladder applies, and that ladder was built for a PR firm "
                "and ranks Marketing highest")
        if "titles" in (profile.fell_back or ()):
            problems.append(
                "no titles.txt of their own: buyer titles fell back to another "
                "client's list")
    if not profile.titles:
        problems.append("no titles.txt, so there is no buyer to search for")

    # Billing last, because it is the only check that costs a round trip, and a profile
    # fault should be reported even when the accounts are fine.
    #
    # An account being dry is only FATAL when nothing else can do its job. Troy,
    # 2026-09-21: "why doesn't it use the BuzzLead route to source companies too?" --
    # it does, and BuzzLead is the PRIMARY source. So a dry company search degrades a
    # run, it does not stop one, and blocking on it would have refused a run BuzzLead
    # could have filled. Copy has no second route by design, so that one is fatal.
    if mode in ("source", "copy-test", "expand"):
        from .clients import buzzlead as _bl
        from .clients import llm as _llm
        # Fatal only when NOTHING can do the job. A dry account behind a working
        # stand-in degrades a run; it does not stop one. Measured the hard way on
        # 2026-09-21: blocking on the company search would have refused a run BuzzLead,
        # the PRIMARY source, could have filled; and blocking on Concentrate would have
        # refused one OpenRouter is still behind in the copy chain.
        copy_backup = any(_llm._has(n) and not _llm._is_down(n)
                          for n in _llm.CHAINS["copy"] if n != "concentrate")
        for line in vendors_down():
            if line.startswith("company search") and (_bl.configured() or (profile.use_leadocean and _has_company_source())):
                # BuzzLead, OR (for a client that sources through QuickEnrich and Lead Ocean first) any working
                # company source: a dead AI Ark, the LAST source in that waterfall, degrades a run, it does not stop one.
                continue
            if line.startswith("copy") and copy_backup:
                continue
            problems.append(f"out of credit -- {line}")
    return problems


def degraded(mode: str) -> list[str]:
    """Accounts that are dry but have a stand-in. Worth saying, not worth blocking."""
    from .clients import buzzlead as _bl
    if mode not in ("source", "copy-test", "expand") or not _bl.configured():
        return []
    out = []
    for line in vendors_down():
        if line.startswith("company search"):
            out.append(f"{line[:90]} -- BuzzLead is the primary company source and will "
                       f"carry this run, but nothing sits behind it")
        elif line.startswith("copy"):
            out.append(f"{line[:90]} -- copy will be written by OpenRouter, NOT "
                       f"Concentrate, which is not where you asked copy to run")
    return out


def measured_menu(profile=None, run_id: str | None = None) -> str:
    """The measured taxonomy menu for this client, bootstrapping it if there is none.

    Every path that asks a model to name industry labels comes through here. Without a
    menu the model invents them: 36 of the 48 labels proposed for ProductEVO on
    2026-09-20 matched no companies at all, which left its audiences anchored on
    catch-alls and its first two runs finding nothing at all.

    A helper rather than the same check written at each call site, because the first
    version of this fix guarded one of the two call sites and the unguarded one was the
    one that ran.
    """
    from . import taxonomy
    menu = ""
    try:
        menu = taxonomy.menu_text()
        if not menu and profile is not None:
            print("no measured taxonomy for this client yet; measuring labels first",
                  flush=True)
            taxonomy.bootstrap(profile, run_id)
            menu = taxonomy.menu_text()
    except Exception as exc:
        print(f"taxonomy unavailable: {exc}", flush=True)
    if not menu:
        from . import db as _db
        _db.log(run_id, "stage2",
                "no measured taxonomy labels for this client: audiences would be "
                "guesses and sourcing would return noise", level="warn")
    return menu


def vertical_from_description(text: str, run_id: str | None = None, profile=None):
    """Turn "boutique law firms doing personal injury" into a searchable vertical.

    Reuses stage 2's system prompt, so the client's ICP, exclusions and the measured
    supply map all apply. The operator never types industry labels or NAICS codes.
    """
    from .stages.s2_expansion import Vertical, MODEL, TEMPERATURE, _system
    from .clients import llm
    # A vertical the client DECLARED, with its provider labels, is used as written: no model, no AI Ark.
    if profile is not None:
        low = (text or "").strip().lower()
        for d in getattr(profile, "vertical_defs", ()) or ():
            nm = str(d.get("name", "")).strip().lower()
            if nm and (low == nm or low.startswith(nm)):
                return Vertical(
                    name=d["name"], rationale=d.get("rationale") or f"declared by the client: {d['name']}",
                    industries=list(d.get("industries") or []), keywords=list(d.get("keywords") or []),
                    employee_min=int(d.get("employee_min") or profile.employee_min),
                    employee_max=int(d.get("employee_max") or profile.employee_max),
                    disqualifiers=list(d.get("disqualifiers") or []), naics=[],
                    seed_campaign="declared: " + d["name"], exact_headcount=bool(d.get("exact_headcount")))
    # Measure labels for THIS vertical first. Without it the model picks the nearest
    # entry in a generic menu, and the nearest is not the same as the right one.
    unavailable = None
    if profile is not None:
        try:
            from . import taxonomy
            res = taxonomy.bootstrap(profile, run_id, want=16, topic=text)
            unavailable = (res or {}).get("unavailable")
        except Exception as exc:
            unavailable = str(exc)
            print(f"could not measure labels for {text!r}: {exc}", flush=True)
    menu = measured_menu(profile, run_id)
    # Refuse to resolve an audience BLIND. With no measured menu the model invents
    # industry labels that match nothing, and the audience falls back to whatever
    # catch-all survives calibration -- which is how ProductEVO spent a day sourcing
    # medical device makers for a consumer-goods pitch. When the company database is
    # simply unreachable, stopping costs a restart; carrying on costs a run.
    if unavailable and not menu:
        raise RuntimeError(
            f"cannot resolve the audience {text!r}: the company database could not be "
            f"asked which industry labels are real ({unavailable[:160]}), and nothing "
            f"has been measured for this client before. Sourcing now would search "
            f"invented labels. Top the account up and run it again.")
    user = (
        "The operator has asked for ONE vertical, described in their own words:\n"
        "  " + text + "\n\n"
        "Return exactly one vertical in the same JSON shape, faithful to that "
        "description rather than an adjacent idea.\n"
    )
    if menu:
        user += ("\nMEASURED SUPPLY (labels with real unworked companies behind them; "
                 "anchor on these where they fit):\n" + menu)
    # 8000, the same budget propose_from_icp has always had. At 2000 the reply was
    # truncated mid-string and the whole run died three seconds in (2026-09-21), and it
    # only started mattering when declared verticals began routing through HERE rather
    # than through propose_from_icp. A vertical carries a rationale, industries, NAICS
    # and a disqualifier list, which is simply more than 2000 tokens of JSON.
    payload = llm.complete_json_retrying(MODEL, _system(), user, TEMPERATURE,
                                         max_tokens=8000, provider="precopy")
    raw = payload.get("verticals") if isinstance(payload, dict) else payload
    items = [v for v in (raw or []) if isinstance(v, dict)]
    if not items:
        raise SystemExit("could not turn " + repr(text) + " into a vertical")
    v = Vertical.from_json(items[0])
    v.seed_campaign = "operator: " + text[:60]
    return v


def assign_variants(contacts: list[dict], variants: list[str],
                    experiment_id: str = COPY_TEST_EXPERIMENT) -> dict[str, list[dict]]:
    """Split one audience evenly across the arms, deterministically by email.

    Deterministic so a resume puts the same person in the same arm, and so a retry
    never moves someone between arms mid-test. Split at the CONTACT rather than the
    company, because company size varies and an even company split is an uneven
    contact split.
    """
    import hashlib
    buckets: dict[str, list[dict]] = {v: [] for v in variants}
    for c in contacts:
        email = (c.get("email") or "").strip().lower()
        digest = hashlib.sha256(f"{experiment_id}:{email}".encode()).digest()
        arm = variants[int.from_bytes(digest[:8], "big") % len(variants)]
        row = dict(c)
        row["copy_variant"] = arm
        row["experiment_id"] = experiment_id
        buckets[arm].append(row)
    return buckets


def _verticals_for(audiences: list[str] | None, count: int, run_id: str,
                   profile=None) -> list:
    """The audiences to work: the operator's words, or the top of the client's queue."""
    from . import db
    from .stages.s2_expansion import Vertical
    import json as _json
    if audiences:
        return [vertical_from_description(a, run_id, profile) for a in audiences]
    # A client that NAMED its verticals gets those verticals, in the order it named
    # them. Troy, 2026-09-20: "it looks like we're inferring just like with IDPR vs
    # actually using their onboarding form." He was right, and the cost was concrete:
    # ProductEVO's onboarding package lists six verticals in the client's own priority
    # order, and the engine invented three of its own instead -- running Medical
    # Devices, which that package ranks LAST and scores 13/25 with the note "volume yes,
    # same conversion no", as the very first audience. Inference is for a client that
    # has not told us; it is not a substitute for reading what they did tell us.
    if profile is not None and getattr(profile, "verticals", ()):
        declared = list(profile.verticals)
        done = set(db.verticals_already_run())
        todo = [v for v in declared if v.lower() not in done][:count] or declared[:count]
        db.log(run_id, "stage2",
               f"using the client's own verticals, in their order: {todo}")
        out = []
        for name in todo:
            v = vertical_from_description(name, run_id, profile)
            # KEEP THE CLIENT'S NAME. The resolver renames the audience every run --
            # "Consumer Electronics" became "Consumer Electronics Brands" and then
            # "Consumer Electronics (Founder-Led DTC Hardware Brands)" -- and staged
            # companies are looked up by EXACT vertical name, so every run found nothing
            # banked and re-sourced from scratch. 17,981 companies staged across eight
            # names for three audiences, 2,234 of them through the gate and unused
            # (found 2026-09-22). The client's own name is stable by definition.
            try:
                v.name = name
            except Exception:
                pass
            out.append(v)
        return out

    claimed = db.take_from_backlog(count, run_id)
    if claimed:
        out = []
        for row in claimed:
            cfg = row["config"]
            out.append(Vertical.from_json(cfg if isinstance(cfg, dict) else _json.loads(cfg)))
        return out
    # A client with nothing queued is the NORMAL state for a new one: the backlog is
    # filled by the weekly run, which only exists once there are winners to expand from.
    # So propose audiences straight from the client's own ICP instead of stopping. This
    # is what ProductEVO hit on its first run (2026-09-18): the run died silently with
    # nothing queued and no audience named.
    # Take the caller's client. client.profile() reads the CLIENT environment variable,
    # which in the long-lived web process is whichever client it booted as -- so a run
    # launched for a second client would have proposed audiences from the FIRST one's
    # ICP. Only fall back when the caller genuinely had none.
    if profile is None:
        from . import client
        profile = client.profile()
    if not profile.icp:
        raise SystemExit(
            "no --audience given, nothing queued, and this client has no icp.md to "
            "propose from. Name an audience: /ve run <client> audience <description>")
    db.log(run_id, "stage2", f"nothing queued; proposing {count} audiences from the ICP")
    return propose_from_icp(profile, count, run_id)


def propose_from_icp(profile, count: int, run_id: str) -> list:
    """Audiences straight from the client's own ICP, for a client with nothing queued."""
    from .stages.s2_expansion import Vertical, MODEL, TEMPERATURE, _system
    from .clients import llm
    menu = measured_menu(profile, run_id)
    user = (
        "This client has no campaign history to expand from. Propose "
        + str(count) + " distinct verticals straight from the ICP above: each a real "
        "slice of their buyer, not a restatement of the whole market, and no two "
        "overlapping."
    )
    if menu:
        user += ("\n\nMEASURED SUPPLY (labels with real unworked companies behind "
                 "them; anchor on these where they fit):\n" + menu)
    payload = llm.complete_json_retrying(MODEL, _system(), user, TEMPERATURE,
                                         max_tokens=8000, provider="precopy")
    raw = payload.get("verticals") if isinstance(payload, dict) else payload
    out = [Vertical.from_json(v) for v in (raw or []) if isinstance(v, dict)]
    if not out:
        raise SystemExit("could not propose any audience from this client's ICP")
    for v in out:
        v.seed_campaign = "proposed from ICP"
    return out[:count]


def _vertical_for(audience: str | None, run_id: str, profile=None):
    """The audience to work, from the operator's words or the client's queue."""
    from . import db
    from .stages.s2_expansion import Vertical
    import json as _json
    if audience:
        v = vertical_from_description(audience, run_id, profile)
        # Same rule the declared-vertical path applies (see _audiences): the resolver
        # renames the audience every run, and staged companies are looked up by EXACT
        # vertical name, so a hand-typed audience that IS one of the client's declared
        # verticals must keep the declared name or every run re-sources from scratch.
        # Found 2026-09-26: "Sporting & Outdoor" was banked under three names.
        if profile is not None:
            declared = {d.lower(): d for d in getattr(profile, "verticals", ())}
            if audience.strip().lower() in declared:
                v.name = declared[audience.strip().lower()]
        return v
    claimed = db.take_from_backlog(1, run_id)
    if not claimed:
        raise SystemExit("no --audience given and nothing queued for this client")
    cfg = claimed[0]["config"]
    return Vertical.from_json(cfg if isinstance(cfg, dict) else _json.loads(cfg))


def heartbeat(run_id: str, every: int = 60):
    """Write a liveness row, with memory, into the DATABASE every minute.

    Twice a ProductEVO run has stopped dead with no reason recorded (2026-09-20, 16:09
    and 16:54). The crash handler never fired either time, so the process was KILLED
    rather than raised -- and the second time the web CONTAINER had restarted, taking
    with it the run-log file added an hour earlier. A file cannot outlive the container
    that holds it, so the record has to go somewhere that does.

    Memory is on the row because a container restart with no deploy and no error is what
    running out of it looks like, and that stays a guess until something measures it.
    A last heartbeat showing memory climbing is the answer; one showing it flat sends us
    to look somewhere else instead of guessing again.
    """
    import threading

    from . import db

    def _rss_mb() -> float:
        try:
            with open("/proc/self/status", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) / 1024
        except Exception:
            pass
        try:
            import resource
            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        except Exception:
            return 0.0

    stop = threading.Event()

    def _beat() -> None:
        while not stop.wait(every):
            try:
                db.log(run_id, "alive", f"working, {_rss_mb():.0f} MB in use")
            except Exception:
                pass                        # a heartbeat must never break the run

    threading.Thread(target=_beat, daemon=True).start()
    return stop


def run_source(profile, audience: str | None, contacts: int, dry_run: bool = False,
              companies_only: bool = False) -> dict:
    """Mode 1: companies and contacts, then stop. No copy, no sending, no gate.

    `companies_only` stops after stage3 (company sourcing + the fit gate) and never
    calls stage4 (contact finding). Operator request 2026-09-25: build the company
    database first, contact-finding needs a separate explicit go-ahead per batch --
    the two spend completely different budgets (QuickEnrich/AI Ark/Lead Ocean company
    search vs. the contact finders + verification waterfall) and this mode used to
    always run both, so the only way to stop between them was killing the process
    mid-run.
    """
    from . import db
    from .stages import s3_companies, s4_people
    from .runner import _run_id

    run_id = _run_id()
    db.init_schema()
    db.start_run(run_id)
    heartbeat(run_id)
    vertical = _vertical_for(audience, run_id, profile)
    db.log(run_id, "start", f"source-only for {profile.display_name}: {vertical.name}")
    print(f"audience: {vertical.name}")
    print(f"  industries: {', '.join(vertical.industries[:4])}")
    print(f"  keywords  : {', '.join(vertical.keywords[:6])}")
    if dry_run:
        db.finish_run(run_id, "complete", "source dry run")
        return {"run_id": run_id, "vertical": vertical.name, "dry_run": True}

    companies, stats3 = s3_companies.find(vertical, run_id, target_contacts=contacts)
    if not companies:
        db.finish_run(run_id, "no_supply", "no companies survived the gate")
        return {"run_id": run_id, "vertical": vertical.name, "companies": 0, "contacts": 0}
    try:
        from . import funnel
        funnel.refresh(run_id)        # newly sourced companies enter the tier table as tier 1 (never contacted)
    except Exception as exc:
        db.log(run_id, "stage3", f"funnel refresh failed: {exc}", level="warn")
    if companies_only:
        db.finish_run(run_id, "complete", f"sourced {len(companies)} companies "
                      f"(companies-only, no contact finding)")
        return {"run_id": run_id, "vertical": vertical.name, "companies": len(companies),
                "contacts": 0, "companies_only": True,
                **{k: v for k, v in stats3.items() if "sample" not in k}}
    people, stats4 = s4_people.build(companies, run_id)
    people = people[:contacts]
    try:
        db.reserve_contacts(people, run_id, None, status="verified")
    except Exception as exc:
        db.log(run_id, "stage4", f"contacts not persisted: {exc}", level="warn")
    db.finish_run(run_id, "complete", f"sourced {len(people)} contacts")
    return {"run_id": run_id, "vertical": vertical.name, "companies": len(companies),
            "contacts": len(people), **{k: v for k, v in stats4.items() if "sample" not in k}}


def run_copy_test(profile, audience, contacts: int, variants: list[str],
                  dry_run: bool = False, verticals: int = 1, sample: int = 0) -> dict:
    """ONE experiment across one or more audiences, split across copy arms.

    Troy, 2026-09-18: test three verticals like IDPR does. That is the better design and
    it costs nothing extra, PROVIDED the total contact count is held constant and the
    arms are balanced WITHIN each vertical:

      - the copy answer pools every vertical, so both arms keep the full sample
      - the audience answer falls out for free, from our own contact-level reply data

    Splitting the same 3,000 contacts across three audiences therefore buys a second
    answer at no cost in statistical power for the first. What must NOT happen is one
    arm landing mostly in one vertical, which would confound message with audience --
    the assignment hashes the email, so every vertical splits evenly on its own.

    One batch PER ARM, not per arm per vertical. Two campaigns keep the arm comparison
    readable in EmailBison's own stats; six would fragment the sending capacity and the
    per-vertical read is available from our contacts table anyway.
    """
    from . import db
    from .stages import s3_companies, s4_people, s5_copy, s6_approval
    from .runner import _run_id

    run_id = _run_id()
    db.init_schema()
    db.start_run(run_id)
    heartbeat(run_id)
    audiences = audience if isinstance(audience, list) else ([audience] if audience else None)
    picked = _verticals_for(audiences, verticals, run_id, profile)
    per_vertical = max(contacts // max(len(picked), 1), 1)
    if sample:
        # Sourcing used to run to the FULL target before writing 20 emails, on the logic
        # that the contacts are banked for the real run anyway. That makes a "one dollar
        # sample" take as long as a full run, which is the wrong trade when the point is
        # to read copy quickly (Troy, 2026-09-22: "sample copy should not take that
        # long"). Enough contacts for the sample, with headroom for verification losses.
        per_vertical = max(sample * 4, 40)
    db.open_copy_test(run_id, profile.name, variants, [v.name for v in picked],
                      contacts, sample)
    db.log(run_id, "start",
           f"copy-test for {profile.display_name}: {len(picked)} audiences, arms {variants}")
    print(f"audiences: {', '.join(v.name for v in picked)}")
    print(f"arms     : {' vs '.join(variants)}")
    print(f"split    : {per_vertical:,} contacts per audience, "
          f"about {per_vertical * len(picked) // len(variants):,} per arm overall")
    if dry_run:
        db.finish_run(run_id, "complete", "copy-test dry run")
        return {"run_id": run_id, "audiences": [v.name for v in picked], "dry_run": True}

    if sample:
        # A look before the spend. 20 per arm costs about a dollar and catches the
        # systematic faults, which are the only kind that matter at this stage: on
        # 2026-09-15, 15,159 emails carried a defect nobody saw until one reached a
        # person. Sourcing still runs to the full target, because the expensive part
        # is the copy and the contacts are banked for the real run either way.
        print(f"SAMPLE RUN: {sample} contacts per arm, then stop for review")

    # Audiences run CONCURRENTLY, as the weekly expansion run has always done.
    # Troy, 2026-09-20: "for Interdependence we made sure company sourcing and contact
    # sourcing ran at the same time, why am I seeing queued for ProductEVO." He was
    # right: this loop was serial, so a three-audience copy test took three times as
    # long as the identical work does on the expand path. Same worker count
    # (PARALLEL_VERTICALS, default 3), and the shared gates below it are unchanged --
    # the company-search semaphore and the model client's own limit still cap how much
    # is in flight, so this adds no load on any vendor, only overlap.
    from concurrent.futures import ThreadPoolExecutor

    # Contacts are saved and handed to copy in CHUNKS, not once per audience. Troy,
    # 2026-09-22: "can't we save the contacts?" -- a run killed mid-audience used to lose
    # every contact it had verified, because reserve_contacts only fired after the whole
    # audience finished. 416 companies is ~45 minutes of waterfall to throw away.
    # Chunking also starts copy within a minute instead of after the audience completes.
    CHUNK = 50

    def _one(v, emit=None):
        """Source one audience. Saves and emits each chunk as it verifies."""
        try:
            cos, _ = s3_companies.find(v, run_id, target_contacts=per_vertical)
            if not cos:
                db.log(run_id, "stage3", f"{v.name}: no companies survived", level="warn")
                return []
            got: list[dict] = []
            for i in range(0, len(cos), CHUNK):
                if len(got) >= per_vertical:
                    break
                rows, _ = s4_people.build(cos[i:i + CHUNK], run_id)
                room = per_vertical - len(got)
                rows = rows[:room]
                if not rows:
                    continue
                for r in rows:
                    r["vertical"] = v.name
                try:
                    db.reserve_contacts(rows, run_id, None, status="verified")
                except Exception as exc:
                    db.log(run_id, "stage4", f"{v.name}: contacts not persisted: {exc}",
                           level="warn")
                got += rows
                print(f"  {v.name}: {len(got):,} contacts so far "
                      f"({min(i + CHUNK, len(cos))}/{len(cos)} companies)", flush=True)
                if emit is not None:
                    emit(list(rows))
            return got
        except Exception as exc:
            # One audience failing must not lose the other two. The expand path files
            # a failure per vertical for the same reason.
            db.log(run_id, "stage3", f"{v.name}: failed: {exc}", level="error")
            return []

    people: list[dict] = []
    companies_seen: set = set()
    workers = max(min(settings.parallel_verticals, len(picked)), 1)
    from .clients.http import VendorOutage

    # COPY OVERLAPS SOURCING. Troy, 2026-09-22: "we can run copy concurrently like idpr
    # does." This path used to drain every audience before writing one email, so the
    # whole copy stage waited on the SLOWEST vertical -- six audiences sourcing for an
    # hour meant an hour of idle model capacity, then copy from a standing start.
    # Now each audience's contacts go to copy the moment they land, while the rest are
    # still sourcing. Sourcing is network-bound and copy is model-bound, so they do not
    # contend: the company-search semaphore and _LLM_GATE still cap each side.
    #
    # A SAMPLE keeps the old serial shape on purpose. Sampling truncates to N per arm
    # ACROSS all audiences, which cannot be decided until every audience is in, and a
    # sample is small enough that the overlap would buy nothing.
    ready_by_arm: dict[str, list] = {n: [] for n in variants}
    stats_by_arm: dict[str, dict] = {n: {} for n in variants}
    spread_by_arm: dict[str, dict] = {n: {} for n in variants}
    paused: str | None = None
    _lock = threading.Lock()

    def _write_copy(rows: list) -> None:
        """Assign arms for ONE audience's contacts, stamp them, and write their copy."""
        nonlocal paused
        if not rows:
            return
        local = assign_variants(rows, variants)
        for _arm, _rows in local.items():
            if not _rows:
                continue
            # Stamp the arm BEFORE any copy is attempted, so a vendor going dry costs
            # the copy and not the assignment: resume_copy_test finds rows that carry an
            # arm and still owe copy, and finishes them on the two-hourly sweep.
            try:
                db.set_contact_variants(_rows, _arm, COPY_TEST_EXPERIMENT)
            except Exception as exc:
                db.log(run_id, "stage5", f"could not stamp arm {_arm}: {exc}", level="warn")
        for _arm, _rows in local.items():
            if not _rows or paused:
                continue
            try:
                got, st = s5_copy.run(_rows, run_id)
            except VendorOutage as exc:
                with _lock:
                    paused = str(exc)[:200]
                db.log(run_id, "stage5",
                       f"paused on arm {_arm}: {str(exc)[:120]}; the 2-hourly resume "
                       f"finishes it once the vendor is back", level="warn")
                return
            with _lock:
                ready_by_arm[_arm].extend(got)
                for k, v in (st or {}).items():
                    if isinstance(v, (int, float)):
                        stats_by_arm[_arm][k] = stats_by_arm[_arm].get(k, 0) + v
                for r in _rows:
                    vn = r.get("vertical")
                    spread_by_arm[_arm][vn] = spread_by_arm[_arm].get(vn, 0) + 1

    if sample:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for rows in pool.map(_one, picked):
                people += rows
                companies_seen.update(r.get("company_domain") for r in rows)
    else:
        copy_pool = ThreadPoolExecutor(max_workers=max(workers * 2, 2))
        copy_jobs: list = []
        _jobs_lock = threading.Lock()

        def _emit(rows):
            """A verified chunk goes to copy immediately, while sourcing continues."""
            if not rows:
                return
            with _jobs_lock:
                copy_jobs.append(copy_pool.submit(_write_copy, rows))

        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for rows in pool.map(lambda v: _one(v, _emit), picked):
                    people += rows
                    companies_seen.update(r.get("company_domain") for r in rows)
            while True:
                with _jobs_lock:
                    pending = [j for j in copy_jobs if not j.done()]
                if not pending:
                    break
                for j in pending:
                    j.result()
        finally:
            copy_pool.shutdown(wait=True)

    if not people:
        db.finish_run(run_id, "no_supply", "no contacts across any audience")
        return {"run_id": run_id, "audiences": [v.name for v in picked], "contacts": 0}

    out: dict = {"run_id": run_id, "audiences": [v.name for v in picked],
                 "contacts": len(people), "companies": len(companies_seen), "arms": {}}

    if sample:
        arms = assign_variants(people, variants)
        arms = {k: v[:sample] for k, v in arms.items()}
        for _arm_name, _rows in arms.items():
            try:
                db.set_contact_variants(_rows, _arm_name, COPY_TEST_EXPERIMENT)
            except Exception as exc:
                db.log(run_id, "stage5", f"could not stamp arm {_arm_name}: {exc}",
                       level="warn")
        for name in variants:
            rows = arms.get(name) or []
            if not rows:
                continue
            spread = {}
            for r in rows:
                spread[r.get("vertical")] = spread.get(r.get("vertical"), 0) + 1
            print(f"  writing {len(rows):,} for arm {name}  {spread}")
            try:
                got, st = s5_copy.run(rows, run_id)
            except VendorOutage as exc:
                db.log(run_id, "stage5",
                       f"paused on arm {name}: {str(exc)[:120]}; the 2-hourly resume "
                       f"finishes it once the vendor is back", level="warn")
                paused = str(exc)[:200]
                break
            ready_by_arm[name] = got
            stats_by_arm[name] = st or {}
            spread_by_arm[name] = spread

    if paused:
        try:
            from .clients import slack as _slack
            _slack.post(
                ":hourglass: *" + profile.display_name
                + "* copy test paused: " + paused[:160]
                + " -- the two-hourly resume picks it up, nothing is lost.")
        except Exception:
            pass
        out["paused"] = paused
        return out

    for name in variants:
        ready = ready_by_arm.get(name) or []
        if not ready:
            continue
        stats = stats_by_arm.get(name) or {}
        spread = spread_by_arm.get(name) or {}
        used = {c.get("company_domain") for c in ready if c.get("company_domain")}
        cos = [{"domain": d, "name": None, "vertical": f"copy-test {name}"} for d in used]
        arm_vertical = _ArmVertical(picked[0], name, len(picked))
        batch_id = s6_approval.open_batch(run_id, arm_vertical, cos, ready,
                                          {**stats, "copy_variant": name,
                                           "experiment_id": COPY_TEST_EXPERIMENT,
                                           "sample": bool(sample),
                                           "audiences": [v.name for v in picked]})
        out["arms"][name] = {"contacts": sum(spread.values()), "sendable": len(ready),
                             "batch_id": batch_id, "by_audience": spread}
        db.log(run_id, "stage6", f"arm {name}: {len(ready)} sendable, batch {batch_id}")
    if sample and out["arms"]:
        try:
            from .clients import slack
            slack.sample_ready(
                profile.name,
                [{"name": k, **v} for k, v in out["arms"].items()],
                [v.name for v in picked],
                contacts)
        except Exception as exc:
            db.log(run_id, "stage6", f"sample summary not posted: {exc}", level="warn")
    db.finish_copy_test(run_id)
    db.finish_run(run_id, "complete",
                  f"copy-test{' SAMPLE' if sample else ''}: {len(variants)} arms across "
                  f"{len(picked)} audiences")
    out["sample"] = bool(sample)
    return out


class _ArmVertical:
    """One arm of a copy test, named so its batch and campaign are distinguishable."""

    def __init__(self, vertical, arm: str, audiences: int = 1):
        self._v = vertical
        self.arm = arm
        self.name = f"copy-test {arm}"
        extra = f" across {audiences} audiences" if audiences > 1 else ""
        self.rationale = f"Copy test arm {arm}{extra}. {vertical.rationale}"

    def __getattr__(self, item):
        return getattr(self._v, item)

    def as_dict(self) -> dict:
        d = dict(self._v.as_dict())
        d["name"] = self.name
        d["copy_variant"] = self.arm
        return d


def resume_copy_test(profile=None) -> dict:
    """Finish a copy test a restart interrupted.

    Nothing is re-sourced and nobody moves arm: the contacts are already banked with
    their arm written on them, so the work left is exactly the rows that have a variant
    and no copy yet. Each arm's batch is topped up rather than duplicated, because a
    second batch per arm would become a second campaign and split the very comparison
    the test exists to make.
    """
    from . import client, db
    from .stages import s5_copy, s6_approval
    from .stages.s2_expansion import Vertical

    profile = profile or client.profile()
    test = db.unfinished_copy_test(profile.name)
    if not test:
        return {"status": "no_copy_test"}
    arms = test["arms"] if isinstance(test["arms"], list) else json.loads(test["arms"])
    run_id = test["run_id"]
    owed = db.uncopied_by_variant(arms)
    total = sum(len(v) for v in owed.values())

    # A test killed DURING SOURCING has contacts with no arm on them, so nothing "owes
    # copy" and this used to declare the test complete -- closing a run that had produced
    # nothing and quietly throwing away the contacts it had already paid for. That is
    # exactly where ProductEVO's run of 2026-09-20 16:54 died: 500 verified contacts
    # banked, none assigned, and the next two-hourly sweep would have filed it as done.
    #
    # So: if no contact carries an arm but the run banked some, assign them here and
    # carry on. Assignment hashes the email, so a contact lands in the same arm it would
    # have landed in during the original run.
    if not total:
        unassigned = db.contacts_without_variant(run_id)
        if unassigned:
            sample = int(test["sample"] or 0)
            buckets = assign_variants(unassigned, arms)
            if sample:
                buckets = {k: v[:sample] for k, v in buckets.items()}
            db.log(run_id, "start",
                   f"resuming copy test after a sourcing restart: {len(unassigned):,} "
                   f"banked contacts had no arm; assigning them now")
            for name, rows in buckets.items():
                db.set_contact_variants(rows, name, COPY_TEST_EXPERIMENT)
            owed = db.uncopied_by_variant(arms)
            total = sum(len(v) for v in owed.values())
    if not total:
        db.finish_copy_test(run_id)
        return {"status": "copy_test_already_complete", "run_id": run_id}

    db.log(run_id, "start", f"resuming copy test: {total} contacts still owe copy "
                            f"across {len(arms)} arms")
    out: dict = {"status": "resumed_copy_test", "run_id": run_id, "arms": {}}
    for name in arms:
        rows = owed.get(name) or []
        if not rows:
            continue
        ready, stats = s5_copy.run(rows, run_id)
        if not ready:
            continue
        used = {c.get("company_domain") for c in ready if c.get("company_domain")}
        cos = [{"domain": d, "name": None, "vertical": f"copy-test {name}"} for d in used]
        arm_name = f"copy-test {name}"
        existing = db.pending_batch_for(arm_name)
        if existing:
            added = db.add_to_batch(existing["batch_id"], run_id, ready, cos, arm_name)
            out["arms"][name] = {"added": added, "batch_id": existing["batch_id"]}
        else:
            stub = Vertical.from_json({"name": arm_name, "rationale": f"Copy test arm {name}",
                                       "industries": [], "keywords": []})
            batch_id = s6_approval.open_batch(run_id, stub, cos, ready,
                                              {**stats, "copy_variant": name,
                                               "experiment_id": COPY_TEST_EXPERIMENT})
            out["arms"][name] = {"added": len(ready), "batch_id": batch_id}
    db.finish_copy_test(run_id)
    return out


def preview(profile=None, arm: str | None = None, email: str | None = None) -> dict:
    """Write one email with the prompts as they stand right now.

    This is the loop for changing copy. A prompt is a text file in the client's folder,
    so a wording change, a new ban or a different angle is an edit anyone can make -- but
    editing blind and finding out across three thousand contacts is how the 2026-09-15
    incident happened. This writes a single email against a REAL contact from the
    client's own database and reports it with every rule applied, for a few cents.

    Edit the prompt, deploy, preview, repeat.
    """
    from . import client, db
    from .stages import s5_copy
    from . import copy_rules

    profile = profile or client.profile()
    with db.cursor() as cur:
        if email:
            cur.execute("""SELECT email, first_name, last_name, title, company_domain,
                                  company_name, vertical FROM contacts
                            WHERE lower(email)=lower(%s) LIMIT 1""", (email,))
        else:
            cur.execute("""SELECT email, first_name, last_name, title, company_domain,
                                  company_name, vertical FROM contacts
                            WHERE company_domain IS NOT NULL AND first_name <> ''
                              AND title IS NOT NULL
                            ORDER BY updated_at DESC LIMIT 1""")
        row = cur.fetchone()
    if not row:
        return {"error": "no contact in this client's database to preview against; "
                         "run a sample first, or pass an email"}

    contact = dict(row)
    contact["copy_variant"] = arm or (profile.variant_names()[0]
                                      if profile.variant_names() else None)
    out, reason = s5_copy.generate(contact, run_id=None)
    if not out:
        return {"contact": contact["email"], "arm": contact["copy_variant"],
                "rejected": reason}
    report = out.get("research_report") or ""
    return {
        "contact": contact["email"],
        "company": contact.get("company_name") or contact.get("company_domain"),
        "title": contact.get("title"),
        "arm": contact["copy_variant"],
        "prompt_version": out.get("prompt_version"),
        "email1": out.get("personalization_1"),
        "email2": out.get("personalization_2"),
        "email1_chars": len(out.get("personalization_1") or ""),
        "email2_chars": len(out.get("personalization_2") or ""),
        "reading_grade": copy_rules.grade_level(out.get("personalization_1") or ""),
        "rule_violations": copy_rules.check(out.get("personalization_1") or "", report)
                           + copy_rules.check(out.get("personalization_2") or "", report),
    }


def calibrate(profile=None, keep_share: float = 0.33) -> dict:
    """What bar would let the best `keep_share` of this client's own campaigns scale.

    A bar inherited from another client is a guess. IDPR's 6% of replies is the 60th
    percentile of IDPR's history; applied to ProductEVO, whose lifetime median is 4.9%
    and whose recent campaigns sit near 1.3%, it would kill everything including a
    winner. The same arithmetic run against each client's own campaigns gives each a bar
    that means the same thing: "better than most of what you have already done".
    """
    profile = profile or client.profile()
    rows = _campaigns(profile)
    if len(rows) < 8:
        return {"client": profile.name, "campaigns": len(rows),
                "error": "too little history to calibrate; leave the bar unset and "
                         "judge nothing automatically until there is more"}
    of_replies = sorted((r["interested"] / r["replies"]) for r in rows if r["replies"] >= 30)
    of_sends = sorted((r["interested"] / r["sends"]) for r in rows if r["sends"])
    def pick(vals):
        if not vals:
            return None
        idx = max(0, min(len(vals) - 1, int(round((1 - keep_share) * (len(vals) - 1)))))
        return round(vals[idx], 5)
    return {
        "client": profile.name,
        "campaigns": len(rows),
        "keep_share": keep_share,
        "of_replies_bar": pick(of_replies),
        "of_replies_median": round(of_replies[len(of_replies) // 2], 5) if of_replies else None,
        "of_sends_bar": pick(of_sends),
        "of_sends_median": round(of_sends[len(of_sends) // 2], 5) if of_sends else None,
        "n_with_30_replies": len(of_replies),
    }
