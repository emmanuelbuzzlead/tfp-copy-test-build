"""Environment + tunables. Import this, never os.getenv scattered through stages.

Nothing here prints a secret. `describe()` reports presence and length only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Local dev convenience; on Railway the env is injected directly and no file exists.
# ENV_FILE lets a dev point at an existing secrets file (e.g. the training repo's .env)
# without copying secrets into this repo. Values are never echoed -- see describe().
try:
    from dotenv import load_dotenv

    _env_file = os.getenv("ENV_FILE")
    load_dotenv(
        Path(_env_file) if _env_file else Path(__file__).resolve().parent.parent / ".env",
        override=False,
    )
except ImportError:  # pragma: no cover
    pass

REPO_ROOT = Path(__file__).resolve().parent.parent


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    try:
        return float(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def _client_database_url() -> str | None:
    """DATABASE_URL, redirected to the active client's own database.

    One Postgres server, one database per client. Separate databases rather than a
    client column on every table: the pipeline's safety rests on dedup, suppression and
    the recency index, and every one of those becomes a shared-state bug the moment two
    clients share tables. A missed WHERE clause would email one client's list under
    another's name, which cannot be walked back.

    Creating one costs nothing and needs no dashboard: CREATE DATABASE on the server
    that is already running. The URL is the same, with the path swapped, so no new
    secret is introduced.
    """
    base = os.getenv("DATABASE_URL")
    if not base:
        return None
    try:
        from . import client
        want = client.profile().database
    except Exception:
        want = None
    if not want:
        return base
    from urllib.parse import urlsplit, urlunsplit
    parts = urlsplit(base)
    if parts.path.lstrip("/") == want:
        return base
    return urlunsplit(parts._replace(path="/" + want))


def _client_emailbison_key() -> str | None:
    """The active client's EmailBison key, by the env var their profile names.

    Reading one client's workspace with another's key is how a sync would index the
    wrong history, so the key follows the client rather than the environment.
    """
    try:
        from . import client
        key = client.profile().emailbison_key()
        if key:
            return key
    except Exception:
        pass
    # No fallback to any other client's key: this build serves ProQ and MasterNow only.
    return None


def _client_first(key: str, env_name: str, default):
    """A per-client setting: the PROFILE wins, the environment is only a fallback.

    The usual precedence is inverted here on purpose. These values mean different things
    for different clients -- IDPR's 6% of replies is the 60th percentile of IDPR's
    history and would be a different bar on anyone else's distribution -- so one global
    environment variable silently applying to every client is a footgun, not a control.
    It was exactly that on 2026-09-19: SCALE_RATE=0.06 was set on all four services and
    would have judged ProductEVO by IDPR's number. To change one client, change that
    client's profile.
    """
    val = _profile_val(key, None)
    if val is not None:
        return val
    raw = os.getenv(env_name)
    if raw not in (None, ""):
        try:
            return type(default)(raw) if default is not None else raw
        except (TypeError, ValueError):
            pass
    return default


def _profile_val(key, default):
    """A value from the active client's profile, or the single-client default."""
    try:
        from . import client
        return getattr(client.profile(), key, None) or default
    except Exception:
        return default


@dataclass(frozen=True)
class Settings:
    # --- kill switch -----------------------------------------------------
    # Spec §2: PIPELINE_ENABLED=false halts everything at next tick.
    pipeline_enabled: bool = field(default_factory=lambda: _flag("PIPELINE_ENABLED", True))

    # --- EmailBison ------------------------------------------------------
    # Verified 2026-09-07: this key sees exactly one workspace, id=10 "Interdependence",
    # on the send.buzzlead.io base (NOT the personal.buzzlead.io default in the
    # older repo client). Used for campaign name/ID listing and push only --
    # all performance metrics come from CSV (spec §4 Stage 1 decision).
    emailbison_api_key: str | None = field(default_factory=_client_emailbison_key)
    # Env still wins. A client whose workspace lives on the other host (MasterNow is on
    # personal.buzzlead.io) declares `emailbison_base` in its client.toml, so the host
    # follows the client like the key does instead of depending on a deploy variable.
    emailbison_base: str = field(
        default_factory=lambda: os.getenv("EMAILBISON_BASE_URL")
        or _profile_val("emailbison_base", "https://send.buzzlead.io")
    )
    workspace_id: int = field(
        default_factory=lambda: _int("WORKSPACE_ID", _profile_val("workspace_id", 0)))
    workspace_name: str = field(
        default_factory=lambda: os.getenv("WORKSPACE_NAME") or _profile_val("display_name", "")
    )
    push_campaign_id: int | None = field(
        default_factory=lambda: _int("PUSH_CAMPAIGN_ID", 0) or None
    )
    # Approved batches get a COPY of this campaign, not a blank one: the model carries
    # the sequence, plain-text setting and send limits that have been tuned in use.
    #
    # 1084 since 2026-09-22, Troy: "we can switch it to any active ve campaign, its the
    # same sequence." Verified before the switch -- all nine live VE campaigns and the
    # old model 1036 share ONE sequence fingerprint (3 steps, waits 3/3/1, plain_text
    # true, 2400 max/day), so the model is interchangeable. The old model was holding
    # 121 sender accounts worth 1,788 sends/day hostage: a campaign cannot be archived
    # while it is the model, and until it is archived its accounts are never free.
    # 1084 is the SCALE winner, so it is the VE campaign that will outlive the others.
    #
    # SUPERSEDED as the live source since 2026-09-22: s7_push asks
    # emailbison.resolve_model_campaign(), which returns whichever VE campaign is
    # actually running, so no campaign is special and archiving one just moves the
    # template. This value is now only the fallback for when nothing live can be read,
    # and archive_campaign() guards solely against archiving the LAST usable template.
    model_campaign_id: int = field(
        default_factory=lambda: _int("MODEL_CAMPAIGN_ID",
                                     _profile_val("model_campaign_id", 0)))
    # Days between email 1 and email 2. Verified after every duplication.
    step_wait_days: int = field(
        default_factory=lambda: _int("STEP_WAIT_DAYS", _profile_val("step_wait_days", 3)))
    # The model's own window is 09:32-17:48, which a duplicate would silently inherit.
    # Sender alignment (Ivan's rules, Troy 2026-09-13/14): ~1,000 sends/day per
    # campaign; an account may sit on at most 3 active campaigns.
    # Troy, 2026-09-14: "3000 leads in campaign should have 1000 target sends and 4000
    # 1200" -- 1,000/day at 3,000 leads, +200/day for every 1,000 leads above that.
    # Troy, 2026-09-15 ("do all 7"): a pushed campaign is started, so approve + push
    # means sending begins in the next schedule window. Set AUTO_START_CAMPAIGNS=false
    # to leave campaigns in draft for a human to start.
    auto_start_campaigns: bool = field(
        default_factory=lambda: os.getenv("AUTO_START_CAMPAIGNS", "true").lower() == "true")
    sender_target_daily: int = field(
        default_factory=lambda: _client_first("sender_target_daily", "SENDER_TARGET_DAILY", 1000))
    # The least DEDICATED capacity a new campaign must be able to get before it launches;
    # below this the push is held and retried as other campaigns finish.
    sender_min_daily: int = field(
        default_factory=lambda: _client_first("sender_min_daily", "SENDER_MIN_DAILY", 600))
    # How long a wu-YYYY-MM-DD cohort stamp still means "warming". Inside this window a
    # stamp outranks the operator's Active tag; outside it the tag wins, because this
    # workspace stamps a cohort and never clears it. Every Active account carrying a
    # stamp on 2026-09-22 was stamped 80-90 days ago and had been sending for months.
    # 14 since 2026-09-30 (was 30). Troy: "the warmups take 2 weeks." Measured against
    # the OLDEST stamp on the account (senders._warmup_stamp_age_days), since most
    # Active accounts carry two.
    warmup_stamp_fresh_days: int = field(
        default_factory=lambda: _int("WARMUP_STAMP_FRESH_DAYS", 14))
    # Whether a TOP-UP may take more sender accounts. Off by default. Troy, 2026-09-23:
    # "when we push recovery we dont need to add sender accounts, we just push to the
    # running campaign." Alignment used to run on every push and re-target on the new
    # lead total: the Biotechnology recovery moved 176 accounts onto campaign 1084
    # (304 -> 480) on its way in. Those come out of the free pool, and the free pool is
    # the only thing that lets a NEW campaign launch -- a top-up of already-sourced
    # leads has no claim on it. Turn on only to deliberately speed a specific campaign.
    align_on_topup: bool = field(
        default_factory=lambda: _flag("ALIGN_ON_TOPUP", False))
    # Emmanuel, 2026-10-08: ProQ and MasterNow senders are attached by hand in EmailBison, so
    # a push must neither wait on free-sender capacity nor attach accounts itself.
    skip_sender_attach: bool = field(
        default_factory=lambda: _flag("SKIP_SENDER_ATTACH", False))
    sender_base_leads: int = field(default_factory=lambda: _int("SENDER_BASE_LEADS", 3000))
    sender_per_extra_1000: int = field(default_factory=lambda: _int("SENDER_PER_EXTRA_1000", 200))
    # 3 since 2026-09-30 (was 2). Troy: "an account can be on 3 campaigns." An
    # account's daily limit is split across its campaigns, not added -- free_capacity
    # counts limit / (n + 1) for an account already on n.
    sender_max_active_campaigns: int = field(
        default_factory=lambda: _int("SENDER_MAX_ACTIVE_CAMPAIGNS", 3))
    # Completed campaigns are archived automatically to release their accounts
    # (senders.archive_completed_campaigns). Anything listed here is left alone.
    never_archive_campaign_ids: tuple = field(
        default_factory=lambda: tuple(int(x) for x in
                                      os.getenv("NEVER_ARCHIVE_CAMPAIGN_IDS", "").split(",")
                                      if x.strip().isdigit()))
    send_start_time: str = field(default_factory=lambda: os.getenv("SEND_START", "09:00:00"))
    send_end_time: str = field(default_factory=lambda: os.getenv("SEND_END", "19:00:00"))
    send_timezone: str = field(
        default_factory=lambda: os.getenv("SEND_TIMEZONE", "America/New_York"))

    # --- data vendors ----------------------------------------------------
    # BuzzLead pipeline API: DiscoLike lookalikes + TAM + the contact waterfall.
    # Primary company/contact source; AI Ark is the fallback when this is unset
    # or a job comes back thin. Phase 1 only -- we never approve copy or send
    # from this key.
    buzzlead_api_key: str | None = field(
        default_factory=lambda: os.getenv("BUZZLEAD_API_KEY") or os.getenv("BLZ_API_KEY")
    )
    buzzlead_base: str = field(default_factory=lambda: os.getenv(
        "BUZZLEAD_BASE_URL", "https://research-615339619342.us-east4.run.app"
    ))
    aiark_api_key: str | None = field(default_factory=lambda: os.getenv("AIARK_API_KEY"))
    quickenrich_api_key: str | None = field(default_factory=lambda: os.getenv("QUICKENRICH_API_KEY"))
    quickenrich_base: str = field(
        default_factory=lambda: os.getenv("QUICKENRICH_BASE_URL", "https://app.quickenrich.io")
    )
    # ProductEVO only -- see app/clients/leadocean.py and finders.py's find_leadocean().
    leadocean_api_key: str | None = field(default_factory=lambda: os.getenv("LEADOCEAN_API_KEY"))
    # Email verification chain: MillionVerifier -> TryKit -> BounceBan (Troy, 2026-09-09).
    # Hunter is kept only as an optional extra finder inside the vendored waterfall; it
    # is no longer the verification gate, because it cannot resolve catch-all domains.
    millionverifier_api_key: str | None = field(
        default_factory=lambda: os.getenv("MILLIONVERIFIER_API_KEY"))
    trykitt_api_key: str | None = field(
        default_factory=lambda: os.getenv("TRYKITT_API_KEY") or os.getenv("TRYKIT_API_KEY"))
    bounceban_api_key: str | None = field(
        default_factory=lambda: os.getenv("BOUNCEBAN_API_KEY"))
    apollo_api_key: str | None = field(default_factory=lambda: os.getenv("APOLLO_API_KEY"))
    hunter_api_key: str | None = field(default_factory=lambda: os.getenv("HUNTER_API_KEY"))
    waterfall_workers: int = field(default_factory=lambda: _int("WATERFALL_WORKERS", 8))
    # Candidates collected per company before the title ladder ranks them. Larger than
    # contacts_per_company on purpose: the ladder needs choices, and verification drops some.
    candidates_per_company: int = field(
        default_factory=lambda: _int("CANDIDATES_PER_COMPANY", 8))
    serper_api_key: str | None = field(default_factory=lambda: os.getenv("SERPER_API_KEY"))
    # Concentrate is an Anthropic-compatible gateway and is the primary LLM provider.
    # OpenRouter is used only when a real sk-or- key is present (see clients/llm.py).
    concentrate_api_key: str | None = field(
        default_factory=lambda: os.getenv("CONCENTRATE_AI_API_KEY")
        or os.getenv("CONCENTRATEAI_API_KEY")
    )
    openrouter_api_key: str | None = field(default_factory=lambda: os.getenv("OPENROUTER_API_KEY"))
    anthropic_api_key: str | None = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY"))
    gemini_api_key: str | None = field(default_factory=lambda: os.getenv("GEMINI_API_KEY"))
    # TypeSafe (Jev) -- the enforcement gate as a typed judgment rather than parsed JSON.
    # Absent key means clients/typesafe.configured() is False and nothing calls it; the
    # Haiku gate stays the only judge. See scripts/shadow_enforce.py.
    typesafe_api_key: str | None = field(default_factory=lambda: os.getenv("TYPESAFE_API_KEY"))
    # Default gateway for calls that do not name one; the other configured gateway is
    # the fallback. Enforcement names openrouter explicitly (Troy, 2026-09-13).
    llm_primary: str = field(default_factory=lambda: os.getenv("LLM_PRIMARY", "concentrate"))

    # --- Slack (slash-command mode, per decision #4) ---------------------
    slack_bot_token: str | None = field(default_factory=lambda: os.getenv("SLACK_BOT_TOKEN"))
    slack_signing_secret: str | None = field(
        default_factory=lambda: os.getenv("SLACK_SIGNING_SECRET")
    )
    # A CHANNEL IS AN IDENTITY, not a global. os.getenv won here, and SLACK_CHANNEL is
    # set on all four Railway services, so every client's messages landed in IDPR's
    # channel no matter what its profile said. Troy, 2026-09-20: "why did the IDPR Slack
    # channel get the copy test arm?" -- because the profile never got a say.
    slack_channel: str = field(
        default_factory=lambda: _client_first(
            "slack_channel", "SLACK_CHANNEL", "#idpr-expander"))

    # --- storage / web ---------------------------------------------------
    database_url: str | None = field(default_factory=_client_database_url)
    upload_token: str | None = field(default_factory=lambda: os.getenv("UPLOAD_TOKEN"))
    # Where the durable review pages live. Slack clears its own history on a retention
    # policy, so a batch posted there can disappear before anyone reads it; these pages
    # are served from Postgres and do not expire.
    # No default to another build's address. PUBLIC_URL, else this service's own Railway
    # domain, else empty (links are then omitted rather than pointing at someone else's pages).
    public_url: str = field(default_factory=lambda: (
        os.getenv("PUBLIC_URL")
        or (f"https://{os.getenv('RAILWAY_PUBLIC_DOMAIN')}" if os.getenv("RAILWAY_PUBLIC_DOMAIN") else "")))

    # --- tunables (spec §2 / §7) -----------------------------------------
    lookback_days: int = field(default_factory=lambda: _int("LOOKBACK_DAYS", 90))
    # Kept as a fallback alias: Stage 1 used to gate on leads, not sends. The live
    # winner contract (app/scoring.py) uses SCALE_MIN_SENDS. Read so an old env
    # still means something if SCALE_MIN_SENDS is unset.
    min_campaign_leads: int = field(default_factory=lambda: _int("MIN_CAMPAIGN_LEADS", 1000))
    # Legacy of-leads floor. Scoring no longer ranks on this; SCALE_RATE is the bar.
    min_interested_rate: float = field(
        default_factory=lambda: _float(
            "MIN_INTERESTED_RATE", _float("MIN_POSITIVE_REPLY_RATE", 0.06)
        )
    )
    top_winners: int = field(default_factory=lambda: _int("TOP_WINNERS", 3))
    # Day 1: EACH winning seed campaign is split 3-5 ways, not 3-5 splits in total.
    # Three seeds therefore yields 9-15 verticals per run, and at leads_per_vertical
    # that is 22,500-37,500 contacts -- the single biggest cost lever in the pipeline.
    splits_per_seed_min: int = field(default_factory=lambda: _int("SPLITS_PER_SEED_MIN", 3))
    splits_per_seed_max: int = field(default_factory=lambda: _int("SPLITS_PER_SEED_MAX", 5))
    # Ceiling across the whole run, so a run can never quietly become unbounded.
    # How many verticals ONE run actually works. The gate routinely passes more than
    # this; the rest are parked in vertical_backlog and picked up by later runs, highest
    # projected first. At target_contacts each, running every survivor at once would be
    # tens of thousands of contacts in a night.
    # Troy, 2026-09-22: "open to 2 a week and it goes up to 3 if we have less than 5 ve
    # campaigns running." Was a flat 3, which oversubscribed the workspace: 15 active
    # campaigns sharing 7,486 sends/day against a ~1,000/day target each means every
    # campaign delivers at about half speed, so verdicts arrive late and capacity never
    # recycles. The step-up needs free sender capacity too, or it walks into the launch
    # gate and the extra vertical is built and then held.
    # 3 base, 5 ceiling since 2026-09-23. Troy, on being shown last week's six
    # campaigns all delivering: "set to 3-5."
    #
    # 2-3 was set from a capacity figure that was wrong. The sender fetch stopped at
    # page 200, so the pipeline saw 3,000 accounts of 4,603 and the whole roster read
    # 7,486/day. It is 9,718. And the 1,255 graduated accounts that had sat at 3/day
    # were raised to 5, which is where most of the difference came from -- not new
    # accounts. All 1,488 Active-tagged accounts carry no warmup stamp under 30 days.
    #
    # One campaign at 3,000 leads is ~8,100 sends, eight workdays at 1,000/day, about
    # 1.6 weeks. So N per week occupies N x 1.6 x 1,000 per day, and 9,718 supports
    # 6.0 -- which is exactly what last week's six did, at 503-1,284 sends per workday
    # each and 51-80% delivered after six days. 5 keeps a margin; 6 has none.
    #
    # The real limiter is not total capacity but FREE accounts, since a new campaign
    # cannot launch onto accounts another campaign already holds. _intake_size gates on
    # that, so this ceiling only binds when the free pool can actually carry it.
    verticals_per_run: int = field(
        default_factory=lambda: _client_first("verticals_per_run", "VERTICALS_PER_RUN", 3))
    verticals_per_run_max: int = field(
        default_factory=lambda: _client_first(
            "verticals_per_run_max", "VERTICALS_PER_RUN_MAX", 5))
    verticals_step_up_below: int = field(
        default_factory=lambda: _int("VERTICALS_STEP_UP_BELOW", 5))
    # TEST SEATS. Troy, 2026-10-05: the flat 3-a-week intake never checked whether
    # anything already running had room -- it launched 3 new verticals every Monday
    # regardless of how many of the last 3-5 launches were STILL being judged, so a
    # vertical's first week of sending competed with winners for the same shared
    # accounts no matter how crowded the workspace already was. A seat is a reserved,
    # modest sending allocation (sender_min_daily) held by one vertical from launch
    # until its FIRST verdict (scale or drop) -- at which point it leaves the seat
    # either way, win or lose, and the seat is free again. Intake claims exactly as
    # many new verticals as seats are empty, never a flat guess; winners never hold a
    # seat, so a crowded winners pool cannot starve new testing the way it did before.
    test_seats: int = field(
        default_factory=lambda: _client_first("test_seats", "TEST_SEATS", 3))
    # FLOOR, not a ceiling override. Troy, 2026-10-07: "ideally i would like some
    # launches every week regardless of the seat. some new activity is always good."
    # Seats cap how many verticals get tested AT ONCE; this guarantees at least one
    # new one gets a shot even on a week where every seat is still occupied by age
    # (a vertical under 7 days old has not failed anything -- it just has not had
    # time to earn a verdict yet). Still gated by real dedicated sender capacity --
    # this raises the desired count, it never bypasses the capacity check that was
    # the whole point of moving off the old flat-3 rule.
    min_weekly_launches: int = field(
        default_factory=lambda: _client_first(
            "min_weekly_launches", "MIN_WEEKLY_LAUNCHES", 1))
    # A vertical that cannot reach this many leads does not get its own campaign; it is
    # pooled. Troy, 2026-09-22: "it should be wise to loop campaigns where we cannot find
    # up to 3000 together." A campaign needs scale_min_sends (2,000) to be judged at all,
    # and at ~2.7 sends per lead that is ~740 leads; Conversational AI shipped 237 and
    # can therefore NEVER earn a verdict on its own, while still holding a campaign slot,
    # its own sender accounts and the 600/day launch gate. 0 disables pooling.
    pool_below_leads: int = field(
        default_factory=lambda: _client_first("pool_below_leads", "POOL_BELOW_LEADS", 1000))
    # Ceiling on a pool. NOT scale_target_leads (10,000): that is the top-up target for
    # a vertical that has already WON, and a construct for bundling sub-1,000-lead
    # verticals has no business growing that big on the senders it launched with.
    # Troy's own figure -- "where we cannot find up to 3000 together".
    pool_max_leads: int = field(
        default_factory=lambda: _int("POOL_MAX_LEADS", 3500))
    # Shout when a pool's attached capacity falls below this share of what its lead
    # count calls for. A join skips the launch gate by design, so this is the only thing
    # standing between a growing pool and the 2026-09-15 failure: campaigns live, loaded
    # and sending almost nothing, with no alarm.
    pool_capacity_warn_ratio: float = field(
        default_factory=lambda: _float("POOL_CAPACITY_WARN_RATIO", 0.6))
    # Interested replies that must be OBSERVED across a pool before our data is trusted
    # to split them. Below it every vertical is credited by lead share instead, so one
    # lucky reply in a small vertical cannot read as 100% of the pool's interest and
    # manufacture a winner.
    pool_min_observed_interested: int = field(
        default_factory=lambda: _int("POOL_MIN_OBSERVED_INTERESTED", 5))
    # Propose fresh verticals only when the queue is thinner than this, so a healthy
    # backlog is worked through instead of being regenerated every week.
    backlog_floor: int = field(default_factory=lambda: _int("BACKLOG_FLOOR", 6))
    # Target list size per vertical, so each split gets a fair test at the same scale.
    leads_per_vertical: int = field(default_factory=lambda: _int("LEADS_PER_VERTICAL", 2500))
    # What a vertical run is FOR. Stage 3 sizes itself against this rather than against a
    # fixed company count: the cap counts FRESH companies, enforcement then keeps roughly
    # a third of them, and contact density varies ten-fold by vertical (medical device
    # ~1.9 verified contacts per company, law firms ~0.6), so any single company number
    # lands far off the mark in one vertical or the other.
    # Troy, 2026-09-13: "let contact cap be 3000".
    target_contacts: int = field(default_factory=lambda: _int("TARGET_CONTACTS", 3000))
    # Starting assumption for contacts per kept company, refined from the live keep rate
    # as Stage 3 runs. Deliberately pessimistic so a sparse vertical fetches enough.
    assumed_contacts_per_company: float = field(
        default_factory=lambda: _float("ASSUMED_CONTACTS_PER_COMPANY", 1.2))
    # Floor under a MEASURED density when sizing a company pull (Troy, 2026-09-30).
    # A vertical that measured 0.05 on one bad night would otherwise ask for 60,000
    # companies; 0.1 caps the ask at 30,000 for a 3,000-contact target.
    min_sizing_density: float = field(
        default_factory=lambda: _float("MIN_SIZING_DENSITY", 0.1))
    # Hard ceiling on fresh companies fetched for one vertical, so an adaptive loop in a
    # near-virgin vertical cannot run away. Stage 3 raises it above this when the
    # measured density calls for more, never above max_fresh_absolute.
    max_fresh_per_vertical: int = field(default_factory=lambda: _int("MAX_FRESH", 9000))
    max_fresh_absolute: int = field(default_factory=lambda: _int("MAX_FRESH_ABSOLUTE", 40000))
    # The weekly lead target the whole pipeline is built around. Troy, 2026-09-30:
    # 200,000 sends a month is ~6,700/day; at ~2.7 sends per lead that is ~15,000 new
    # leads a week -- five 3,000-lead launches, plus scale-ups. Tracked Monday to
    # Sunday in WAT and surfaced on /live and the Friday report, so a short week is
    # visible on Wednesday, not the following Monday.
    weekly_lead_quota: int = field(
        default_factory=lambda: _client_first("weekly_lead_quota", "WEEKLY_LEAD_QUOTA", 15000))
    # Vendor unit prices for the spend ledger (USD). Not on file as of 2026-09-30 --
    # 0 means "count units, show no dollar figure" rather than invent a price.
    aiark_cost_per_company_page: float = field(
        default_factory=lambda: _float("AIARK_COST_PER_COMPANY_PAGE", 0.0))
    aiark_cost_per_person: float = field(
        default_factory=lambda: _float("AIARK_COST_PER_PERSON", 0.0))
    leadocean_cost_per_person: float = field(
        default_factory=lambda: _float("LEADOCEAN_COST_PER_PERSON", 0.0))

    # --- winner contract (Stage 1 seeds AND Stage 8 scale/drop) -------------
    # The business outcome is interested replies per send, with a stronger bonus for a
    # booked meeting. Share-of-replies is diagnostic only: it can prefer a campaign that
    # creates fewer interested prospects merely because it also creates fewer replies.
    # SCALE_METRIC flips the denominator (of_replies | of_leads | of_sends).
    scale_metric: str = field(
        default_factory=lambda: _client_first("scale_metric", "SCALE_METRIC", "of_replies"))
    scale_rate: float = field(
        default_factory=lambda: _client_first("scale_rate", "SCALE_RATE", 0.06))
    scale_min_sends: int = field(
        default_factory=lambda: _client_first("scale_min_sends", "SCALE_MIN_SENDS", 2000))
    scale_min_days: int = field(
        default_factory=lambda: _client_first("scale_min_days", "SCALE_MIN_DAYS", 7))
    # Percent of the campaign's sequence actually delivered before it may be judged, as
    # an AND with scale_min_days. Troy, 2026-09-22.
    #
    # An absolute send floor stops meaning anything once campaigns grow by top-up: 2,000
    # sends is most of a 1,000-lead campaign and a fraction of one topped up to 10,000,
    # yet both would clear the same gate. completion_percentage is the only signal that
    # scales with the campaign, so it asks "how far through is this?" rather than "how
    # much has happened?". A campaign 7 days old but 20% delivered still has most of its
    # sends ahead of it, and any verdict on it is a guess.
    #
    # 0 disables the gate. Campaigns starved of senders deliver slowly, so this gate and
    # sender capacity push against each other: if decisions stall here, add senders to
    # the campaign rather than lowering the bar.
    # How many winners may be SCALED in one daily review. Troy, 2026-09-23: "since scale
    # ups run every day wouldnt it make sense to cap it at 2-3 campaigns".
    #
    # It is not a throttle on ambition, it is the number that fits. Scaling takes a
    # vertical to scale_target_leads (10,000), and a 10,000-lead campaign asks for
    # 2,400 sends/day. The four winners of 2026-09-23 would therefore need 9,600/day
    # against a workspace that can send 7,486 in total -- before the five other VE
    # campaigns and the legacy ones. Uncapped, the pipeline sources 26,000 contacts to
    # build something it physically cannot deliver, and every campaign crawls.
    #
    # Winners are taken highest-rate first, so the two best scale today and the rest
    # wait for tomorrow's review rather than being forgotten.
    # Fewest banked contacts worth a batch of their own. Below this they wait for the
    # vertical's next run rather than each opening a 72h approval a human must read.
    banked_min_contacts: int = field(
        default_factory=lambda: _int("BANKED_MIN_CONTACTS", 50))
    # A pending batch's own growth is batched into ONE Slack notification instead of
    # pinging on every top-up. Troy, 2026-09-25: "rather than having to push a couple
    # hundred leads everytime" -- a batch adding 56, then 57, then 119 pinged Slack
    # three times for under 250 contacts total.
    notify_added_threshold: int = field(
        default_factory=lambda: _int("NOTIFY_ADDED_THRESHOLD", 300))
    # 5 since 2026-09-24, Troy: "can scale up to 5 campaigns." Was 3, matching
    # parallel_verticals' three lanes; 5 runs two of those lanes twice within the same
    # scale_deadline_minutes budget rather than adding lanes, so it costs more wall-clock
    # per run, not more concurrent load.
    #
    # It was 2 on the belief that a 10,000-lead campaign draws 2,400/day and three would
    # eat all 4,163/day of headroom. That was wrong -- align_on_topup is off, so a
    # scale-up attaches NO sender accounts and costs nothing in daily sends.
    #
    # What it really costs is TIME, and through time, account lock-up: Medical Device at
    # 3,463 leads finishes in 1.8 weeks and at 10,000 in 5.2, on the same 1,048 sends a
    # workday. A campaign holds its accounts for its whole life -- 1084 alone holds 304
    # of 1,490 usable -- and free accounts are what let a NEW campaign launch. So
    # scaling competes with discovery for account TURNOVER, not for sends, which is why
    # this is paired with archiving a campaign as soon as it is delivered.
    scale_max_per_run: int = field(
        default_factory=lambda: _client_first("scale_max_per_run", "SCALE_MAX_PER_RUN", 5))
    # How long one scale-up may spend SOURCING before it stops and banks what it has.
    # Copy is never cut off by this: it is ~95% of the model spend and those contacts
    # are already paid for.
    #
    # Two reasons. A scale-up runs until it finishes or the container kills it -- 90
    # minutes to walk 1,050 of 2,817 companies on 2026-09-23 -- and since the review now
    # refuses to start while another run is alive, ONE long scale-up would block every
    # following day's review. And a run that stops cleanly leaves a batch rather than a
    # pile of raw contacts; s4 banks every slice, so the deadline costs nothing already
    # found. The vertical is marked partial, not exhausted, so tomorrow simply scales it
    # again from the higher base -- the daily cadence IS the resume.
    scale_deadline_minutes: int = field(
        default_factory=lambda: _int("SCALE_DEADLINE_MINUTES", 240))
    # Weekday (Mon=0) the review judges and splits as usual but does NOT scale. Monday
    # is the weekly seed run's day: it starts at 13:00, the review at 12:00, and a
    # review still scaling at 13:00 meets it head on -- both competing for QuickEnrich,
    # which does 66% of contact sourcing. The weekly run is the only thing that brings
    # NEW audiences in; scale-ups only enlarge what already exists. Monday belongs to
    # discovery. -1 disables the skip.
    scale_skip_weekday: int = field(
        default_factory=lambda: _int("SCALE_SKIP_WEEKDAY", 0))
    scale_min_progress: float = field(
        default_factory=lambda: float(
            _client_first("scale_min_progress", "SCALE_MIN_PROGRESS", 30.0)))
    # After this many days the progress gate is ignored and the campaign is judged on
    # what it has. Troy, 2026-09-22: "at this pace none of our campaigns would drop."
    #
    # The progress gate can deadlock, and the deadlock feeds itself: a campaign starved
    # of senders never reaches 60%, so it is never judged, so it is never dropped, so it
    # never hands its accounts back, so every other campaign stays starved. Measured the
    # same day: 15 active campaigns sharing 7,486 sends/day against a ~1,000/day target
    # each, so everything delivers at about half speed.
    #
    # Troy asked whether the two gates should be OR instead. They should not: progress
    # measures throughput, the day count measures how long humans take to reply, and a
    # well-fed campaign can hit 60% in three days when replies take three to seven. OR
    # would kill good verticals for sending fast. A deadline gives what OR was reaching
    # for -- a verdict in bounded time -- without judging before the replies exist.
    scale_max_days: int = field(
        default_factory=lambda: _client_first("scale_max_days", "SCALE_MAX_DAYS", 14))
    # Noise floor so 1 interested out of 6 replies cannot promote a 10k scale-up.
    scale_min_replies: int = field(
        default_factory=lambda: _client_first("scale_min_replies", "SCALE_MIN_REPLIES", 30))
    scale_decision_sends: int = field(
        default_factory=lambda: _client_first("scale_decision_sends", "SCALE_DECISION_SENDS", 3000))
    booking_bonus: int = field(default_factory=lambda: _int("BOOKING_BONUS", 3))
    scale_target_leads: int = field(
        default_factory=lambda: _client_first("scale_target_leads", "SCALE_TARGET_LEADS", 10000))
    # When a scaled vertical runs dry, contacts untouched for this long may be re-used.
    recycle_after_days: int = field(default_factory=lambda: _int("RECYCLE_AFTER_DAYS", 90))
    # Who to tell when a SCALE vertical cannot reach scale_target_leads.
    gap_notify_slack: str = field(
        default_factory=lambda: os.getenv("GAP_NOTIFY_SLACK", "@emmanuel"))
    company_cap_per_vertical: int = field(default_factory=lambda: _int("COMPANY_CAP", 1500))
    contact_cap_per_vertical: int = field(default_factory=lambda: _int("CONTACT_CAP", 3000))
    # A LAUNCHED campaign under this many leads can never clear scale_min_sends
    # (2,000) on its own and nothing else tops it up -- scale_vertical() only starts
    # after a 'scale' verdict, which needs sends this campaign does not have yet.
    # Troy, 2026-09-29: the automatic top-up loop keeps sourcing for it, same as a
    # pre-launch short batch, until it clears this floor.
    auto_topup_launched_floor: int = field(
        default_factory=lambda: _int("AUTO_TOPUP_LAUNCHED_FLOOR", 2000))
    # The MOST one auto-topup round may add to a single vertical. Troy, 2026-10-02:
    # "auto top ups should not surpass a number, their primary aim is to get smaller
    # verticals up a bit." Before this, a top-up sized itself the same way a fresh
    # launch does -- against the WHOLE contact_cap_per_vertical room left -- so a
    # vertical that had room for 2,941 more got 2,941 more in one pass (Specialized
    # Management Consulting, 2026-10-01 night, after a day of missed ticks let it
    # catch up all at once). A nudge, not a refill.
    auto_topup_increment: int = field(
        default_factory=lambda: _int("AUTO_TOPUP_INCREMENT", 400))
    # The automatic top-up must never collide with the daily review. 2026-09-29: the
    # 22:04 UTC tick was still sourcing at 23:02 and the review skipped the whole
    # day. So: a per-tick wall-clock budget, and no top-up at all in the tick that
    # lands inside the quiet window before the review's 23:00 UTC fire.
    auto_topup_budget_minutes: int = field(
        default_factory=lambda: _int("AUTO_TOPUP_BUDGET_MINUTES", 50))
    # The WHOLE two-hourly tick's wall-clock budget. Railway replaces a cron service's
    # container when the next schedule fires, so any tick still working at the 2-hour
    # mark is killed mid-slice and its successor sees the corpse's fresh run row and
    # backs off "already running" -- a 50% duty cycle with a dead body every cycle.
    # Measured on Consumer Edtech, 2026-10-04: its continuation died at 22:07, exactly
    # two hours after its 20:07 tick began. 100 minutes leaves 20 for startup drift.
    resume_tick_budget_minutes: int = field(
        default_factory=lambda: _int("RESUME_TICK_BUDGET_MINUTES", 100))
    # Warn when MillionVerifier's balance drops below this. It gates every contact's
    # verification; Serper-style surprise zero here would stall the whole waterfall.
    mv_credits_warn: int = field(default_factory=lambda: _int("MV_CREDITS_WARN", 10000))
    auto_topup_quiet_before_review_minutes: int = field(
        default_factory=lambda: _int("AUTO_TOPUP_QUIET_BEFORE_REVIEW_MINUTES", 90))
    review_cron_utc_hour: int = field(default_factory=lambda: _int("REVIEW_CRON_UTC_HOUR", 23))
    # Up to 4 people per company, chosen by the title priority ladder. Paired with
    # company-level stop-on-reply: the first reply from a company pulls its siblings.
    # Per client, not global. Troy, 2026-09-22: "5 contacts per company max" for
    # ProductEVO, whose buying decision spans product, sourcing and operations.
    # IDPR keeps 4 -- a PR buyer is one or two people, and a wider net there just
    # emails the same firm more often. A ceiling, not a target: the measured yield
    # is ~1 per company, so this only binds on the companies that actually have depth.
    contacts_per_company: int = field(default_factory=lambda: _client_first(
        "contacts_per_company", "CONTACTS_PER_COMPANY", 4))
    # How many candidates per company go to VERIFICATION. 0 = the seat count (verify only
    # the people we intend to keep). Above the seat count, the extra are verified too and
    # the best-ranked seats that PASS are kept: measured 2026-09-27 on ProductEVO, 561
    # candidates were cut before verification and then only 43% of the chosen passed, so
    # seats sat empty at companies that had more people to offer.
    verify_pool_per_company: int = field(default_factory=lambda: _client_first(
        "verify_pool_per_company", "VERIFY_POOL_PER_COMPANY", 0))
    killed_dedup_days: int = field(default_factory=lambda: _int("KILLED_DEDUP_DAYS", 60))
    # How long a company stays off limits after IDPR last emailed it. Troy, 2026-09-11:
    # a company that has been contacted before is not permanently spent -- what matters
    # is recency. Only membership in a campaign created inside this window blocks it.
    # Measured the same day: 21 campaigns fall inside 90 days (83,502 lead slots) and 59
    # fall outside it (273,806 slots), so this is the difference between a mined-out
    # workspace and a reusable one.
    recontact_days: int = field(
        default_factory=lambda: _int("RECONTACT_DAYS", _profile_val("recontact_days", 90)))
    # Troy, 2026-09-15: no copy for contacts behind Barracuda or Mimecast; Proofpoint
    # stays. Applied at company entry in stage 4, before verification (app/mx.py).
    blocked_mail_gateways: frozenset = field(default_factory=lambda: frozenset(
        g.strip().lower() for g in os.getenv("BLOCKED_MAIL_GATEWAYS", "barracuda,mimecast").split(",")
        if g.strip()))
    # A vertical with at least this many verified-but-uncopied contacts left behind by a
    # halted run resumes at the copy stage instead of re-sourcing.
    resume_min_contacts: int = field(default_factory=lambda: _int("RESUME_MIN_CONTACTS", 100))
    approval_timeout_hours: int = field(default_factory=lambda: _int("APPROVAL_TIMEOUT_HOURS", 72))
    hunter_reverify_days: int = field(default_factory=lambda: _int("HUNTER_REVERIFY_DAYS", 30))
    email1_max_chars: int = field(
        default_factory=lambda: _client_first("email1_max_chars", "EMAIL1_MAX_CHARS", 500))
    # 1100, not 800. The Clay Email 2 prompt mandates an opener, a site reference,
    # 2-3 publication bullets each carrying a reason AND an angle, and a CTA. Six live
    # generations landed 816-1291 chars and never approached 800: the structure does not
    # fit. Troy's call was to raise the gate rather than cut bullets, since the 3-pitch
    # structure is approved copy.
    email2_max_chars: int = field(
        default_factory=lambda: _client_first("email2_max_chars", "EMAIL2_MAX_CHARS", 1100))
    # Raised from 3. The compression path converges instead of re-rolling, so extra
    # attempts now finish the job rather than repeating it: measured 2026-09-11 a
    # draft walked 1126 -> 842 -> 642 -> 532 against a 500 limit and was binned one
    # attempt short. Each retry is one LLM call against a contact already paid for
    # through sourcing and verification.
    copy_max_retries: int = field(default_factory=lambda: _int("COPY_MAX_RETRIES", 6))
    # Copy is ~80s of waiting per contact (2 Serper + a site fetch + 3 LLM calls), so it
    # runs in parallel; sequentially one 2,500-lead vertical would take 56 hours.
    # 16 per vertical, 3 verticals = 48 in flight against an LLM gate of 24. Copy is
    # pure API waiting, so the gate is the real limit and the workers just keep it fed.
    copy_workers: int = field(default_factory=lambda: _int("COPY_WORKERS", 16))
    research_max_tokens: int = field(default_factory=lambda: _int("RESEARCH_MAX_TOKENS", 2200))
    # Contact finding is also mostly waiting on vendors (AI.ARK's email-finder polls an
    # async job), so companies are processed in parallel too.
    # 16, up from 8. Finders are pure vendor-API waiting; with QuickEnrich titles now
    # queried in concurrent waves the per-company cost is a few seconds, so more workers
    # simply means more companies in flight. Troy, 2026-09-13: "make finders way faster".
    finder_workers: int = field(default_factory=lambda: _int("FINDER_WORKERS", 12))
    # Verticals worked at the same time. 3 verticals x 12 finder workers x 8 titles a
    # wave is ~290 QuickEnrich requests in flight; the http helper's bounded retries
    # absorb a 429 if the plan throttles.
    # 5 since 2026-09-30 (was 3): with intake stepping up to 5 verticals when senders
    # allow, three lanes left two verticals queued behind the first three for the whole
    # run. QuickEnrich is unlimited and the LLM semaphore is global; Lead Ocean's shared
    # 1 req/s clock is the only per-vendor throttle and it paces across lanes already.
    parallel_verticals: int = field(default_factory=lambda: _int("PARALLEL_VERTICALS", 5))

    # --- ICP lock (spec §2) ----------------------------------------------
    employee_min: int = field(default_factory=lambda: _int("EMPLOYEE_MIN", 11))
    employee_max: int = field(default_factory=lambda: _int("EMPLOYEE_MAX", 500))

    def describe(self) -> dict[str, object]:
        """Presence-only view for preflight. Never returns a secret value."""
        secrets = {
            "EMAILBISON_API_KEY (this client)": self.emailbison_api_key,
            "BUZZLEAD_API_KEY": self.buzzlead_api_key,
            "AIARK_API_KEY": self.aiark_api_key,
            "QUICKENRICH_API_KEY": self.quickenrich_api_key,
            "MILLIONVERIFIER_API_KEY": self.millionverifier_api_key,
            "TRYKITT_API_KEY": self.trykitt_api_key,
            "BOUNCEBAN_API_KEY": self.bounceban_api_key,
            "APOLLO_API_KEY": self.apollo_api_key,
            "SERPER_API_KEY": self.serper_api_key,
            "CONCENTRATE_AI_API_KEY": self.concentrate_api_key,
            "OPENROUTER_API_KEY": self.openrouter_api_key,
            "SLACK_BOT_TOKEN": self.slack_bot_token,
            "SLACK_SIGNING_SECRET": self.slack_signing_secret,
            "DATABASE_URL": self.database_url,
            "UPLOAD_TOKEN": self.upload_token,
        }
        return {
            "secrets": {k: (f"set (len {len(v)})" if v else "MISSING") for k, v in secrets.items()},
            "workspace": f"{self.workspace_name} (id={self.workspace_id}) @ {self.emailbison_base}",
            "pipeline_enabled": self.pipeline_enabled,
            "winner_gate": (f">={self.scale_min_sends} sends, "
                            f">={self.scale_min_days}d, "
                            + (f">={self.scale_min_progress:.0f}% delivered, "
                               if self.scale_min_progress > 0 else "")
                            + f">={self.scale_rate:.2%} {self.scale_metric}"),
        }

    def missing_for_live_run(self) -> list[str]:
        required = {
            "EMAILBISON_API_KEY (this client)": self.emailbison_api_key,
            "AIARK_API_KEY": self.aiark_api_key,
            "QUICKENRICH_API_KEY": self.quickenrich_api_key,
            "MILLIONVERIFIER_API_KEY": self.millionverifier_api_key,
            "TRYKITT_API_KEY": self.trykitt_api_key,
            "BOUNCEBAN_API_KEY": self.bounceban_api_key,
            "APOLLO_API_KEY": self.apollo_api_key,
            "SERPER_API_KEY": self.serper_api_key,
            "CONCENTRATE_AI_API_KEY": self.concentrate_api_key,
            "DATABASE_URL": self.database_url,
        }
        return sorted(name for name, value in required.items() if not value)


settings = Settings()
