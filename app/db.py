"""Postgres state store: dedup history, run logs, batch approvals, ingested stats.

Dedup is the load-bearing part of this module. It must run BEFORE enrichment so
finder credits are only spent on people we are actually allowed to email.

A company or contact is blocked when it was emailed RECENTLY -- not because it was
ever emailed at all. Troy, 2026-09-11 and again 2026-09-17: re-engage after 90 days;
do not permanently write anyone off. `blocked_domains` / `blocked_contacts` read
`recent_contacts` (EmailBison campaigns inside recontact_days) plus in-flight
reserved/approved rows. A `pushed` backfill row is history, not a life sentence.

  reserved / approved  -- in a batch awaiting a decision; skip until it settles
  killed               -- honoured for KILLED_DEDUP_DAYS (default 60)
  recent send          -- appeared in an EmailBison campaign inside recontact_days
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Iterator, Sequence

import psycopg
from psycopg.rows import dict_row

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    status        TEXT NOT NULL DEFAULT 'running',
    winners       JSONB,
    verticals     JSONB,
    notes         TEXT
);

CREATE TABLE IF NOT EXISTS companies (
    domain        TEXT PRIMARY KEY,
    name          TEXT,
    vertical      TEXT,
    run_id        TEXT,
    batch_id      TEXT,
    status        TEXT NOT NULL,
    drop_reason   TEXT,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    killed_at     TIMESTAMPTZ,
    source        TEXT NOT NULL DEFAULT 'pipeline'
);
CREATE INDEX IF NOT EXISTS companies_status_idx ON companies (status);
CREATE INDEX IF NOT EXISTS companies_batch_idx  ON companies (batch_id);

CREATE TABLE IF NOT EXISTS recent_contacts (
    company_domain TEXT NOT NULL,
    email          TEXT NOT NULL,
    campaign_id    BIGINT NOT NULL,
    campaign_name  TEXT,
    campaign_date  DATE,
    synced_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (company_domain, email, campaign_id)
);
CREATE INDEX IF NOT EXISTS recent_contacts_domain_idx ON recent_contacts (company_domain);
CREATE INDEX IF NOT EXISTS recent_contacts_date_idx   ON recent_contacts (campaign_date);

CREATE TABLE IF NOT EXISTS contacts (
    email             TEXT PRIMARY KEY,
    linkedin_url      TEXT,
    company_domain    TEXT,
    first_name        TEXT,
    last_name         TEXT,
    title             TEXT,
    run_id            TEXT,
    batch_id          TEXT,
    status            TEXT NOT NULL,
    drop_reason       TEXT,
    email_verified_at TIMESTAMPTZ,
    personalization_1 TEXT,
    personalization_2 TEXT,
    research_report   TEXT,
    first_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    killed_at         TIMESTAMPTZ,
    source            TEXT NOT NULL DEFAULT 'pipeline',
    eb_lead_id        BIGINT,      -- EmailBison lead id, needed to remove siblings
    eb_campaign_id    BIGINT
);
CREATE INDEX IF NOT EXISTS contacts_domain_idx ON contacts (company_domain);
CREATE INDEX IF NOT EXISTS contacts_batch_idx  ON contacts (batch_id);
-- LinkedIn URL is the second dedup key (spec Stage 4). Partial unique index so the
-- many NULLs (contacts with no LinkedIn) do not collide with each other.
CREATE UNIQUE INDEX IF NOT EXISTS contacts_linkedin_key
    ON contacts (linkedin_url) WHERE linkedin_url IS NOT NULL;

CREATE TABLE IF NOT EXISTS batches (
    batch_id        TEXT PRIMARY KEY,
    run_id          TEXT,
    vertical        TEXT NOT NULL,
    slack_ts        TEXT,
    approval_status TEXT NOT NULL DEFAULT 'pending',
    approved_by     TEXT,
    company_count   INTEGER NOT NULL DEFAULT 0,
    contact_count   INTEGER NOT NULL DEFAULT 0,
    summary         JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at      TIMESTAMPTZ,
    expires_at      TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS batches_status_idx ON batches (approval_status);

CREATE TABLE IF NOT EXISTS logs (
    id       BIGSERIAL PRIMARY KEY,
    run_id   TEXT,
    stage    TEXT,
    level    TEXT NOT NULL DEFAULT 'info',
    message  TEXT NOT NULL,
    detail   JSONB,
    ts       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS logs_run_idx ON logs (run_id, ts);

-- Stage 1 CSV override. The API supplies these figures every run; a row here means a
-- human uploaded an export for that campaign, and it wins over the API value.
CREATE TABLE IF NOT EXISTS campaign_stats (
    campaign_id      BIGINT NOT NULL,
    campaign_name    TEXT,
    as_of            DATE NOT NULL,
    total_leads      INTEGER,
    sends            INTEGER,
    replies          INTEGER,
    interested       INTEGER,
    vertical_label   TEXT,
    upload_id        TEXT,
    PRIMARY KEY (campaign_id, as_of)
);

CREATE TABLE IF NOT EXISTS uploads (
    upload_id   TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    filename    TEXT,
    row_count   INTEGER,
    detail      JSONB,
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Runtime additions to the hard-block list in assets/do_not_contact.txt.
CREATE TABLE IF NOT EXISTS do_not_contact (
    entry      TEXT PRIMARY KEY,
    kind       TEXT NOT NULL DEFAULT 'domain',   -- domain | name
    note       TEXT,
    added_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per campaign this pipeline creates. pushed_lead_count is the denominator
-- Stage 1 scores against: company-level stop-on-reply REMOVES sibling leads from the
-- campaign, which shrinks EmailBison's own total_leads and would inflate the interested
-- rate of exactly the campaigns that are working best.
CREATE TABLE IF NOT EXISTS ve_campaigns (
    campaign_id       BIGINT PRIMARY KEY,
    campaign_name     TEXT NOT NULL,
    vertical          TEXT,
    batch_id          TEXT,
    run_id            TEXT,
    pushed_lead_count INTEGER NOT NULL DEFAULT 0,
    removed_count     INTEGER NOT NULL DEFAULT 0,
    seed_industry     TEXT,
    -- The Stage 2 filter set that built this vertical. Kept so a SCALE verdict can
    -- re-source against the SAME definition months later; without it a top-up would
    -- have to re-invent the targeting and would no longer be the audience we judged.
    vertical_config   JSONB,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Companies whose sequence has been stopped because someone there replied.
CREATE TABLE IF NOT EXISTS replied_companies (
    company_domain TEXT NOT NULL,
    campaign_id    BIGINT NOT NULL,
    replied_email  TEXT,
    interested     BOOLEAN,
    siblings_removed INTEGER NOT NULL DEFAULT 0,
    detected_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (company_domain, campaign_id)
);

-- The QUEUE. Stage 2 proposes 9-15 verticals a run and the gate passes several, but a
-- run works only a few of them: at target_contacts each, running every survivor at once
-- would be tens of thousands of contacts in one night. The rest are parked here rather
-- than discarded, so next week starts from verticals already proposed, scored and paid
-- for instead of re-deriving them and getting a different answer.
CREATE TABLE IF NOT EXISTS vertical_backlog (
    vertical      TEXT PRIMARY KEY,
    config        JSONB NOT NULL,
    seed_campaign TEXT,
    projected     INTEGER NOT NULL DEFAULT 0,
    density       NUMERIC(6,2),
    fresh_rate    NUMERIC(6,4),
    status        TEXT NOT NULL DEFAULT 'queued',
    queued_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at    TIMESTAMPTZ,
    run_id        TEXT,
    slack_ts      TEXT                   -- the vertical's Slack thread
);
CREATE INDEX IF NOT EXISTS vertical_backlog_status_idx
    ON vertical_backlog (status, projected DESC);

-- The memory. One row per vertical we have actually tested, carrying the verdict so
-- Stage 2 can stop proposing avatars that already failed. Without this the pipeline
-- re-proposes a dud 90 days later, having learned nothing from having run it.
CREATE TABLE IF NOT EXISTS vertical_yields (
    vertical        TEXT NOT NULL,
    run_id          TEXT NOT NULL,
    universe        INTEGER,
    fresh           INTEGER,
    kept            INTEGER,
    keep_rate       NUMERIC(6,4),
    candidates      INTEGER,
    verified        INTEGER,
    density         NUMERIC(6,3),   -- verified contacts per kept company
    sendable        INTEGER,
    copy_pass       NUMERIC(6,4),
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (vertical, run_id)
);

CREATE TABLE IF NOT EXISTS staged_companies (
    vertical     TEXT NOT NULL,
    domain       TEXT NOT NULL,
    payload      JSONB NOT NULL,          -- the normalised AI Ark record
    verdict      TEXT,                    -- NULL until enforced; then KEEP | DROP
    reason       TEXT,
    run_id       TEXT,
    staged_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    consumed_at  TIMESTAMPTZ,              -- set when the company went into a batch
    found_at     TIMESTAMPTZ,              -- set when the finders have processed it
    PRIMARY KEY (vertical, domain)
);

CREATE TABLE IF NOT EXISTS vertical_outcomes (
    vertical          TEXT PRIMARY KEY,
    verdict           TEXT NOT NULL,          -- scale | drop | pending
    campaign_id       BIGINT,
    run_id            TEXT,
    leads             INTEGER,
    sends             INTEGER,
    replies           INTEGER,
    interested        INTEGER,
    rate_of_replies   NUMERIC(6,5),
    rate_of_leads     NUMERIC(6,5),
    seed_industry     TEXT,
    reason            TEXT,
    decided_at        TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS vertical_outcomes_verdict_idx ON vertical_outcomes (verdict);

-- Append-only evidence. vertical_outcomes is the current projection; this table keeps
-- every observation and decision so a policy change can be audited or replayed.
CREATE TABLE IF NOT EXISTS decision_snapshots (
    id                BIGSERIAL PRIMARY KEY,
    vertical          TEXT NOT NULL,
    campaign_id       BIGINT,
    run_id            TEXT,
    verdict           TEXT NOT NULL,
    sends             INTEGER NOT NULL DEFAULT 0,
    replies           INTEGER NOT NULL DEFAULT 0,
    interested        INTEGER NOT NULL DEFAULT 0,
    booked            INTEGER NOT NULL DEFAULT 0,
    objective_rate    NUMERIC(10,7),
    confidence_low    NUMERIC(10,7),
    confidence_high   NUMERIC(10,7),
    reason             TEXT,
    observed_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS send_exposures (
    exposure_id       TEXT PRIMARY KEY,
    email             TEXT NOT NULL,
    campaign_id       BIGINT NOT NULL,
    experiment_id     TEXT NOT NULL,
    variant_id        TEXT NOT NULL,
    vertical          TEXT,
    title_segment     TEXT,
    prompt_version    TEXT,
    exposed_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS send_exposures_experiment_idx
    ON send_exposures (experiment_id, variant_id);

CREATE TABLE IF NOT EXISTS outcome_events (
    event_key         TEXT PRIMARY KEY,
    email             TEXT NOT NULL,
    campaign_id       BIGINT,
    event_type        TEXT NOT NULL,
    occurred_at       TIMESTAMPTZ,
    source            TEXT NOT NULL,
    actor             TEXT,
    note              TEXT,
    recorded_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS outcome_events_email_idx ON outcome_events (email, event_type);

-- Verticals already worked, so Stage 2 can skip anything run in the lookback window.
CREATE TABLE IF NOT EXISTS verticals_run (
    vertical TEXT NOT NULL,
    run_id   TEXT NOT NULL,
    ran_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (vertical, run_id)
);

CREATE TABLE IF NOT EXISTS mx_cache (
    domain     TEXT PRIMARY KEY,
    gateway    TEXT,
    hosts      TEXT,
    checked_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A copy test in flight. Everything it produces is already durable -- contacts are
-- banked as 'verified', each arm is written on the contact, and drafts save per email --
-- but nothing recorded that a test was RUNNING, so a restart left the copy half written
-- with no way to know the rest was owed.
CREATE TABLE IF NOT EXISTS copy_tests (
    run_id      TEXT PRIMARY KEY,
    client      TEXT NOT NULL,
    arms        JSONB NOT NULL,
    audiences   JSONB,
    target      INTEGER,
    sample      INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'running',
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);

-- What a finished experiment decided. A copy test that cannot record its own winner
-- is not a learning loop, it is a report someone has to read and act on by hand.
CREATE TABLE IF NOT EXISTS client_state (
    client            TEXT PRIMARY KEY,
    active_variant    TEXT,
    variant_set_at    TIMESTAMPTZ,
    variant_set_by    TEXT,
    variant_evidence  TEXT,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A copy arm handed over from Slack. It cannot be a file: the running service has an
-- ephemeral filesystem, so anything it wrote would vanish on the next deploy, and a
-- file would also need a commit and a redeploy before it could be tested at all.
CREATE TABLE IF NOT EXISTS client_arms (
    client      TEXT NOT NULL,
    name        TEXT NOT NULL,
    angle       TEXT NOT NULL,
    bet         TEXT,
    created_by  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    retired_at  TIMESTAMPTZ,
    PRIMARY KEY (client, name)
);

-- MIGRATIONS.
ALTER TABLE ve_campaigns ADD COLUMN IF NOT EXISTS copy_variant  TEXT;
ALTER TABLE ve_campaigns ADD COLUMN IF NOT EXISTS experiment_id TEXT;
ALTER TABLE vertical_backlog ADD COLUMN IF NOT EXISTS slack_ts TEXT;
-- The projection AS MEASURED, before MIN_PROJECTED floors it. `projected` is a go/no-go
-- value: s2b raises anything under 2,000 up to 2,000 when BuzzLead backs the vertical,
-- because the probe understates a lookalike/TAM universe it cannot see. Useful as a
-- gate, ruinous as a rank -- on 2026-09-22 five queued verticals were tied at exactly
-- 2,000 and Behavioral & Mental Health, which the probe measured at 8,156, was
-- indistinguishable from Clinical Trial's real 2,000. Ties then fell to row order.
ALTER TABLE vertical_backlog ADD COLUMN IF NOT EXISTS projected_raw INTEGER;
-- This verdict came from a POOLED campaign, where the vertical's sends and replies were
-- apportioned from the campaign total rather than measured. Good enough to report, not
-- good enough to bar a vertical from ever being tested properly on its own -- which is
-- what an outcome row otherwise does, for lookback_days, via take_from_backlog.
ALTER TABLE vertical_outcomes ADD COLUMN IF NOT EXISTS pooled BOOLEAN NOT NULL DEFAULT FALSE;
-- This vertical has been scaled as far as its supply goes. report_gap only ever posted
-- a Slack warning, so a winner that could not reach scale_target_leads re-sourced its
-- whole exhausted universe again on EVERY daily review, paying for the search each time
-- to find the same nothing. Troy, 2026-09-23: "if we dont have up to that then its the
-- end of the scaling for that campaign and we dont bother scaling again."
ALTER TABLE ve_campaigns ADD COLUMN IF NOT EXISTS scale_exhausted_at TIMESTAMPTZ;
ALTER TABLE ve_campaigns ADD COLUMN IF NOT EXISTS scale_exhausted_at_leads INTEGER;

-- Marks a scale-up IN PROGRESS: set right before scale_vertical starts real work,
-- cleared in a `finally` when it returns. Troy, 2026-09-24, after a deploy killed
-- Medical Device and Venture Capital mid-sourcing and neither resumed until the next
-- day's review: "runs should also resume if they were failed right?" A killed
-- container skips the `finally` -- that is the point, it is what makes the stamp mean
-- "died mid-flight" instead of "finished cleanly" -- so the two-hourly resume can find
-- exactly these rows and continue them, the same guarantee vertical_backlog already
-- gives the weekly run's verticals.
ALTER TABLE ve_campaigns ADD COLUMN IF NOT EXISTS scaling_run_id    TEXT;
ALTER TABLE ve_campaigns ADD COLUMN IF NOT EXISTS scaling_started_at TIMESTAMPTZ;
ALTER TABLE batches ADD COLUMN IF NOT EXISTS expiry_warned_at TIMESTAMPTZ;
ALTER TABLE staged_companies ADD COLUMN IF NOT EXISTS found_at TIMESTAMPTZ;
-- Which source produced this company. Without it the two sources cannot be
-- compared, and 'is BuzzLead as good as AI Ark' has no answer but an opinion.
ALTER TABLE staged_companies ADD COLUMN IF NOT EXISTS source TEXT;
ALTER TABLE staged_companies ADD COLUMN IF NOT EXISTS consumed_at TIMESTAMPTZ;
-- CREATE TABLE IF NOT EXISTS does nothing to a table that already exists, so a column
-- added to a definition above is silently absent on any database created before it --
-- init_schema() reports success and the column is simply not there. Every column added
-- after a table first shipped therefore needs an explicit ADD COLUMN IF NOT EXISTS here.
ALTER TABLE contacts     ADD COLUMN IF NOT EXISTS company_name    TEXT;
ALTER TABLE contacts     ADD COLUMN IF NOT EXISTS vertical        TEXT;
ALTER TABLE ve_campaigns ADD COLUMN IF NOT EXISTS seed_industry   TEXT;
ALTER TABLE ve_campaigns ADD COLUMN IF NOT EXISTS vertical_config JSONB;
ALTER TABLE contacts     ADD COLUMN IF NOT EXISTS eb_lead_id      BIGINT;
ALTER TABLE contacts     ADD COLUMN IF NOT EXISTS eb_campaign_id  BIGINT;
ALTER TABLE campaign_stats ADD COLUMN IF NOT EXISTS interested    INTEGER;
-- Provenance of the copy on each row: which prompt files, which provider and model
-- wrote it. Without these a Concentrate half and an OpenRouter-fallback half of one run
-- are indistinguishable afterwards, and no copy change can ever be measured.
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS prompt_version  TEXT;
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS research_model  TEXT;
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS copy_model      TEXT;
-- What happened after the send. Written back by the stop-on-reply sweep, which already
-- reads every reply; until 2026-09-14 it kept only the company domain.
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS replied_at        TIMESTAMPTZ;
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS interested        BOOLEAN;
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS bounced_at        TIMESTAMPTZ;
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS outcome_synced_at TIMESTAMPTZ;
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS experiment_id      TEXT;
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS copy_variant       TEXT;
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS title_segment      TEXT;
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS booked_at          TIMESTAMPTZ;
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS booked_by          TEXT;
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS booked_note        TEXT;
ALTER TABLE vertical_outcomes ADD COLUMN IF NOT EXISTS booked          INTEGER DEFAULT 0;
ALTER TABLE vertical_outcomes ADD COLUMN IF NOT EXISTS objective_rate  NUMERIC(10,7);
ALTER TABLE vertical_outcomes ADD COLUMN IF NOT EXISTS confidence_low  NUMERIC(10,7);
ALTER TABLE vertical_outcomes ADD COLUMN IF NOT EXISTS confidence_high NUMERIC(10,7);
-- Why a human approved or killed a batch. The richest signal in the system.
ALTER TABLE batches  ADD COLUMN IF NOT EXISTS decision_reason TEXT;
ALTER TABLE batches  ADD COLUMN IF NOT EXISTS renewals INTEGER NOT NULL DEFAULT 0;
-- Contacts added to a pending batch since the last Slack notification about it. Troy,
-- 2026-09-25: "rather than having to push a couple hundred leads everytime" -- a batch
-- that grows by 56, then 57, then 119 was pinging Slack every single time. Batched up
-- and fired once the running total crosses NOTIFY_ADDED_THRESHOLD instead.
ALTER TABLE batches ADD COLUMN IF NOT EXISTS unnotified_added_count INTEGER NOT NULL DEFAULT 0;
-- How many times a needs_review contact has been given a fresh copy attempt. Troy,
-- 2026-09-25: "imagine how many needs review we've lost, how can we save them?" --
-- 2,889 all-time, most from fixable copy QA failures (wrong company name, length,
-- placeholders) rather than the unfixable ones (no first name, EmailBison declined).
-- Caps a genuinely-unfixable contact from being requeued forever.
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS requeue_count INTEGER NOT NULL DEFAULT 0;
-- A pause for a human decision, distinct from scale_exhausted_at (a permanent close).
-- Troy, 2026-09-25: 5 consecutive nights of a poor label match (not a genuine "the
-- fetch returned nothing" signal) pauses auto-scaling instead of grinding forever or
-- wrongly declaring the vertical exhausted.
ALTER TABLE ve_campaigns ADD COLUMN IF NOT EXISTS poor_result_streak INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ve_campaigns ADD COLUMN IF NOT EXISTS scale_needs_human_at TIMESTAMPTZ;
-- Troy, 2026-09-25: "surface % of real market explored on the live dashboard... so you
-- can see a vertical is genuinely under-explored before it ever reaches the 5-night
-- pause point." staged_companies is a rolling 14-day window and never carried a market
-- SIZE at all, only what was found -- these two are cumulative, all-time, per campaign.
ALTER TABLE ve_campaigns ADD COLUMN IF NOT EXISTS market_scanned_total INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ve_campaigns ADD COLUMN IF NOT EXISTS market_available INTEGER;

-- Troy, 2026-10-08: "the live page should be synced daily with bison, the numbers are
-- not tallying." pushed_lead_count is our own push history -- it only ever grows, and
-- never reflects what EmailBison later removes (sender pulled, domain blacklisted,
-- manually stopped: Venture Capital alone showed 1,810 stopped leads none of our own
-- bookkeeping knew about). bison_total_leads is the live total_leads EmailBison itself
-- reports, refreshed once a day, so the two numbers can be shown side by side instead
-- of silently disagreeing.
ALTER TABLE ve_campaigns ADD COLUMN IF NOT EXISTS bison_total_leads INTEGER;
ALTER TABLE ve_campaigns ADD COLUMN IF NOT EXISTS bison_synced_at TIMESTAMPTZ;

-- The automatic top-up loop's own streak, distinct from poor_result_streak (which is
-- specifically a scale-up's label-precision signal). Troy, 2026-09-29: a vertical
-- short of its ceiling -- pre-launch (a pending batch under contact_cap_per_vertical)
-- or launched-but-thin (a campaign under the judge-me floor) -- is retried every 2h
-- indefinitely; five straight attempts that add NOTHING pause it for a human rather
-- than burn vendor calls on a vertical that will never grow. Lives on
-- vertical_backlog because every vertical has a row there whether or not it has
-- launched, unlike ve_campaigns which only exists after a push.
ALTER TABLE vertical_backlog ADD COLUMN IF NOT EXISTS topup_streak INTEGER NOT NULL DEFAULT 0;
ALTER TABLE vertical_backlog ADD COLUMN IF NOT EXISTS topup_needs_human_at TIMESTAMPTZ;

-- Which vendors were OUT when this company was searched, so it can be re-searched
-- through exactly those once they are back. Troy, 2026-09-30: a company that got one
-- person from QuickEnrich while Apollo and AI Ark were out of credit was marked found
-- forever; restoring the credit changed nothing for it.
ALTER TABLE staged_companies ADD COLUMN IF NOT EXISTS missed_vendors TEXT[];
ALTER TABLE staged_companies ADD COLUMN IF NOT EXISTS seats_filled INTEGER;
ALTER TABLE batches ADD COLUMN IF NOT EXISTS expiry_warned_at TIMESTAMPTZ;

-- Every paid vendor call, so spend is a number on /live and in the Friday report
-- instead of a surprise 402. Troy, 2026-09-30: "a calculated ai ark spend update so
-- i can track it." Units are the vendor's own (AI Ark: company pages and people
-- returned); price per unit is config, since it is not on file.
CREATE TABLE IF NOT EXISTS vendor_usage (
    id        BIGSERIAL PRIMARY KEY,
    vendor    TEXT NOT NULL,
    kind      TEXT NOT NULL,            -- e.g. company_page | people_search | people_returned
    units     INTEGER NOT NULL DEFAULT 1,
    status    TEXT,                     -- http status or 'ok'
    run_id    TEXT,
    vertical  TEXT,
    ts        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS vendor_usage_ts_idx ON vendor_usage (vendor, ts);
"""

# 'approved' is a distinct state from 'pushed': the Slack gate has passed but the
# EmailBison load has not run yet. Collapsing the two marked contacts as sent before
# anything was sent, and the push step -- which reads the batch's un-pushed rows --
# then found nothing to send. All four states block re-entry into the pipeline.
MAX_RENEWALS = 2

BLOCKING_COMPANY_STATUSES = ("pushed", "reserved", "approved")
BLOCKING_CONTACT_STATUSES = ("pushed", "reserved", "approved", "needs_review")


# One connection per THREAD, reused, rather than one per query. Stage 3 and Stage 4 run
# 8-16 workers that each check dedup per company, which was opening hundreds of
# short-lived connections; over the Railway TCP proxy that eventually gets refused with
# "server closed the connection unexpectedly" and killed a 40-minute run mid-flight.
# Keepalives stop an idle worker's connection being reaped during a long copy stage.
_local = threading.local()
# DNS on Railway's public proxy fails for minutes at a time, not seconds: three
# multi-hour runs died on "failed to resolve host ...rlwy.net" after exhausting a
# 30-second backoff, the last of them losing 290 finished contacts. Inside Railway the
# database is on the internal network and none of this applies, so this ladder only
# matters for runs driven from a laptop.
_CURSOR_RETRIES = 12


def connect() -> psycopg.Connection:
    url = getattr(_local, "url", None) or settings.database_url
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    conn = getattr(_local, "conn", None)
    if conn is not None and not conn.closed:
        return conn
    conn = psycopg.connect(
        url, row_factory=dict_row, autocommit=True,
        connect_timeout=20, keepalives=1, keepalives_idle=30,
        keepalives_interval=10, keepalives_count=5,
    )
    _local.conn = conn
    return conn


def _drop_thread_conn() -> None:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    _local.conn = None


@contextlib.contextmanager
def cursor() -> Iterator[psycopg.Cursor]:
    """A cursor on this thread's connection, reconnecting if the server dropped it.

    Retries only the CONNECTION, never a half-applied statement: every write in this
    module is a single autocommit statement or an executemany, so a retry re-runs the
    whole thing rather than resuming a partial one.
    """
    last: Exception | None = None
    for attempt in range(_CURSOR_RETRIES):
        try:
            conn = connect()
            with conn.cursor() as cur:
                yield cur
            return
        except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
            # Covers both failure modes seen against Railway's public TCP proxy from
            # outside Railway: the server dropping a connection under churn, and
            # intermittent DNS ("failed to resolve host ...rlwy.net"). Both surface as
            # OperationalError and both are transient, so back off and rebuild rather
            # than losing an hour of work.
            last = exc
            _drop_thread_conn()
            if attempt < _CURSOR_RETRIES - 1:
                time.sleep(min(2 ** attempt, 45))
    raise last if last else RuntimeError("postgres cursor failed")


@contextlib.contextmanager
def for_client(client_name: str) -> Iterator[None]:
    """Talk to ANOTHER client's database for the duration of this block.

    settings.database_url is computed once, when the process starts. That is right for
    a run, which is one client from start to finish, and wrong for the web service,
    which is one long-lived process serving every client's Slack commands. Setting
    CLIENT in the environment mid-process does nothing, because settings has already
    been built -- so `/ve promote productevo <arm>` wrote the promotion into IDPR's
    database and ProductEVO never changed its copy (found 2026-09-20, while adding
    `/ve addarm`). Any Slack command that writes must wrap the write in this.
    """
    from urllib.parse import urlsplit, urlunsplit

    from . import client as _client
    base = settings.database_url
    want = None
    try:
        want = _client.load(client_name).database
    except Exception:
        pass
    if not base or not want:
        yield
        return
    parts = urlsplit(base)
    if parts.path.lstrip("/") == want:
        yield                                 # already the right database
        return

    prev_conn = getattr(_local, "conn", None)
    prev_url = getattr(_local, "url", None)
    _local.conn = None
    _local.url = urlunsplit(parts._replace(path="/" + want))
    try:
        yield
    finally:
        borrowed = getattr(_local, "conn", None)
        if borrowed is not None and not borrowed.closed:
            try:
                borrowed.close()
            except Exception:
                pass
        _local.conn = prev_conn
        _local.url = prev_url


def init_schema() -> None:
    with cursor() as cur:
        cur.execute(SCHEMA)


def ping() -> str:
    with cursor() as cur:
        cur.execute("SELECT version()")
        return cur.fetchone()["version"]


# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------

def log(run_id: str | None, stage: str, message: str, level: str = "info", **detail: Any) -> None:
    print(f"[{level}] {stage}: {message}" + (f" {detail}" if detail else ""), flush=True)
    try:
        with cursor() as cur:
            cur.execute(
                "INSERT INTO logs (run_id, stage, level, message, detail) VALUES (%s,%s,%s,%s,%s)",
                (run_id, stage, level, message,
                 json.dumps(_json_safe(detail), default=str) if detail else None),
            )
    except Exception:
        # Logging must never take the run down; stdout above still reaches Railway logs.
        pass


# ---------------------------------------------------------------------------
# dedup
# ---------------------------------------------------------------------------

def normalize_domain(value: str) -> str:
    d = (value or "").strip().lower()
    for prefix in ("https://", "http://"):
        if d.startswith(prefix):
            d = d[len(prefix):]
    if d.startswith("www."):
        d = d[4:]
    return d.split("/")[0].strip()


def normalize_linkedin(value: str | None) -> str | None:
    if not value:
        return None
    return value.strip().rstrip("/").lower() or None


def recent_contact_domains(domains: Iterable[str]) -> dict[str, str]:
    """{domain: 'YYYY-MM-DD via <campaign>'} for companies emailed inside the window.

    Sourced from recent_contacts, which scripts/sync_recent.py builds by walking the
    leads of every campaign created in the last settings.recontact_days days. That is
    the ONLY trustworthy answer to "when did we last email this company": the contacts
    table cannot answer it, because the 223,863 backfilled rows carry their ingestion
    date, not a send date, so every one of them looks like it was contacted the day we
    imported it.
    """
    wanted = sorted({normalize_domain(d) for d in domains if d})
    if not wanted:
        return {}
    cutoff = date.today() - timedelta(days=settings.recontact_days)
    with cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (company_domain)
                   company_domain AS domain, campaign_date, campaign_name
              FROM recent_contacts
             WHERE company_domain = ANY(%s) AND campaign_date >= %s
             ORDER BY company_domain, campaign_date DESC
            """,
            (wanted, cutoff),
        )
        return {
            r["domain"]: f"emailed {r['campaign_date']:%Y-%m-%d} in {r['campaign_name']}"
            for r in cur.fetchall()
        }


def blocked_domains(domains: Iterable[str]) -> dict[str, str]:
    """Return {domain: reason} for companies that must not re-enter the pipeline.

    A company is blocked when it was emailed RECENTLY -- not because it was ever
    emailed at all. Troy, 2026-09-11: "rather than throw away any company that
    Interdependence has reached out to, we basically want to check campaigns in email
    bison from the last ninety days to see if those companies are there. The ones that
    aren't there are the ones we go with."

    That is a far larger pool than the old rule allowed. Measured the same day: 21
    campaigns fall inside the 90-day window holding 83,502 lead slots; 59 campaigns fall
    outside it holding 273,806. Blocking on "ever touched" discarded all of the latter.

    The rules are now:
      recent contact       -- blocked, the company saw an email inside recontact_days
      reserved / approved  -- blocked, the company is in flight right now
      killed               -- blocked for killed_dedup_days

    Seat exhaustion is deliberately NOT a rule any more. It was measured against
    backfilled rows whose timestamps are ingestion dates, so it could not distinguish a
    company emailed last week from one emailed last year, and it permanently retired
    companies that are simply due another look.
    """
    wanted = sorted({normalize_domain(d) for d in domains if d})
    if not wanted:
        return {}
    out: dict[str, str] = dict(recent_contact_domains(wanted))
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.killed_dedup_days)
    with cursor() as cur:
        cur.execute(
            """
            SELECT company_domain AS domain,
                   count(*) FILTER (WHERE status IN ('reserved','approved')) AS in_flight,
                   count(*) FILTER (WHERE status = 'killed'
                                      AND killed_at > %s)               AS killed_recent
              FROM contacts
             WHERE company_domain = ANY(%s)
             GROUP BY company_domain
            """,
            (cutoff, wanted),
        )
        for row in cur.fetchall():
            if row["domain"] in out:
                continue
            if row["in_flight"]:
                out[row["domain"]] = "in a batch awaiting approval"
            elif row["killed_recent"]:
                out[row["domain"]] = f"killed, suppressed {settings.killed_dedup_days}d"
    # ProQ / MasterNow only: firm ownership, replied firms and the 60-day floor. Returns
    # {} for every other client. Deliberately not wrapped in try/except: a gated client
    # that cannot read its history must stop, not carry on and email people who replied.
    from . import consultant_gate
    for dom, why in consultant_gate.blocked(wanted).items():
        out.setdefault(dom, why)
    return out


def company_seats_used(domains: Iterable[str]) -> dict[str, int]:
    """How many people at each company have been emailed inside the recontact window.

    Only recent sends consume a seat. Someone emailed eight months ago does not hold a
    slot against a company we are entitled to approach again today.
    """
    wanted = sorted({normalize_domain(d) for d in domains if d})
    if not wanted:
        return {}
    cutoff = date.today() - timedelta(days=settings.recontact_days)
    with cursor() as cur:
        cur.execute(
            "SELECT company_domain AS domain, count(DISTINCT email) AS n "
            "  FROM recent_contacts "
            " WHERE company_domain = ANY(%s) AND campaign_date >= %s "
            " GROUP BY company_domain", (wanted, cutoff))
        seats = {r["domain"]: r["n"] for r in cur.fetchall()}
    with cursor() as cur:
        cur.execute(
            "SELECT company_domain AS domain, count(*) AS n FROM contacts "
            " WHERE company_domain = ANY(%s) AND status IN ('reserved','approved') "
            " GROUP BY company_domain", (wanted,))
        for r in cur.fetchall():
            seats[r["domain"]] = seats.get(r["domain"], 0) + r["n"]
    return seats


def known_domain(domain: str) -> bool:
    """Has IDPR ever held a contact at this company? (Any status, any age.)"""
    d = normalize_domain(domain)
    if not d:
        return False
    with cursor() as cur:
        cur.execute("SELECT 1 FROM contacts WHERE company_domain=%s LIMIT 1", (d,))
        if cur.fetchone():
            return True
        cur.execute("SELECT 1 FROM recent_contacts WHERE company_domain=%s LIMIT 1", (d,))
        return cur.fetchone() is not None


def ever_contacted_domains(domains: Iterable[str]) -> set[str]:
    """Companies IDPR has ever held a contact at, at any age.

    Distinct from blocked_domains, which asks "is this off limits NOW". This asks
    "have we been here before", so sourcing can spend its cap on genuinely new
    companies first. Troy, 2026-09-14: "only when we've exhausted never emailed can we
    add [companies] not within 90 days."
    """
    wanted = sorted({normalize_domain(d) for d in domains if d})
    if not wanted:
        return set()
    with cursor() as cur:
        cur.execute("SELECT DISTINCT company_domain FROM contacts WHERE company_domain = ANY(%s)",
                    (wanted,))
        out = {r["company_domain"] for r in cur.fetchall()}
        cur.execute("SELECT DISTINCT company_domain FROM recent_contacts "
                    " WHERE company_domain = ANY(%s)", (wanted,))
        out |= {r["company_domain"] for r in cur.fetchall()}
    return out


def ever_contacted_emails(emails: Iterable[str]) -> set[str]:
    """People IDPR has ever emailed, at any age. Same purpose as the domain version."""
    wanted = sorted({e.strip().lower() for e in emails if e})
    if not wanted:
        return set()
    with cursor() as cur:
        cur.execute("SELECT DISTINCT email FROM contacts WHERE email = ANY(%s)", (wanted,))
        out = {r["email"].strip().lower() for r in cur.fetchall() if r["email"]}
        cur.execute("SELECT DISTINCT email FROM recent_contacts WHERE email = ANY(%s)", (wanted,))
        out |= {r["email"].strip().lower() for r in cur.fetchall() if r["email"]}
    return out


def blocked_contacts(emails: Iterable[str], linkedin_urls: Iterable[str]) -> set[str]:
    """Return the set of people who must not be contacted right now.

    Recency-based, for the same reason blocked_domains is: "pushed" on a backfilled row
    carries an ingestion date, not a send date, so treating it as a permanent block
    retired 223,863 people forever on the strength of a timestamp that means nothing.

    A person is blocked when any of these hold:
      - they appear in a campaign created inside recontact_days (recent_contacts)
      - they are reserved or approved right now, i.e. in a batch awaiting a decision
      - they were killed inside killed_dedup_days
      - this pipeline pushed them inside recontact_days -- checked on contacts.updated_at
        and restricted to source='pipeline', because our own rows DO carry a real send
        date and recent_contacts only refreshes when the sync script runs

    Either key blocks: email or LinkedIn URL.
    """
    e = sorted({x.strip().lower() for x in emails if x})
    l = sorted({u for u in (normalize_linkedin(x) for x in linkedin_urls) if u})
    if not e and not l:
        return set()
    killed_cutoff = datetime.now(timezone.utc) - timedelta(days=settings.killed_dedup_days)
    push_cutoff = datetime.now(timezone.utc) - timedelta(days=settings.recontact_days)
    seen: set[str] = set()
    with cursor() as cur:
        cur.execute(
            """
            SELECT email, linkedin_url FROM contacts
             WHERE (email = ANY(%s) OR lower(rtrim(linkedin_url, '/')) = ANY(%s))
               AND (status IN ('reserved','approved')
                    OR (status = 'killed' AND killed_at > %s)
                    OR (status = 'pushed' AND source = 'pipeline' AND updated_at > %s))
            """,
            (e or [""], l or [""], killed_cutoff, push_cutoff),
        )
        for row in cur.fetchall():
            if row["email"]:
                seen.add(row["email"].strip().lower())
            norm = normalize_linkedin(row["linkedin_url"])
            if norm:
                seen.add(norm)
    if e:
        with cursor() as cur:
            cur.execute(
                "SELECT DISTINCT email FROM recent_contacts "
                " WHERE email = ANY(%s) AND campaign_date >= %s",
                (e, date.today() - timedelta(days=settings.recontact_days)))
            seen.update(r["email"].strip().lower() for r in cur.fetchall() if r["email"])
    return seen


# ---------------------------------------------------------------------------
# writes
# ---------------------------------------------------------------------------

def start_run(run_id: str) -> None:
    with cursor() as cur:
        cur.execute("INSERT INTO runs (run_id) VALUES (%s) ON CONFLICT DO NOTHING", (run_id,))


def finish_run(run_id: str, status: str, notes: str | None = None, **payload: Any) -> None:
    with cursor() as cur:
        cur.execute(
            """UPDATE runs SET finished_at = now(), status = %s, notes = %s,
                      winners = COALESCE(%s::jsonb, winners),
                      verticals = COALESCE(%s::jsonb, verticals)
                WHERE run_id = %s""",
            (
                status,
                notes,
                json.dumps(_json_safe(payload["winners"]), default=str) if "winners" in payload else None,
                json.dumps(_json_safe(payload["verticals"]), default=str) if "verticals" in payload else None,
                run_id,
            ),
        )


def _yield_ratios(verdicts: dict[str, int], searched: int, stats: dict) -> dict:
    """Keep rate and density from what actually happened, not from a stats dict.

    Three of the first four rows in vertical_yields recorded a 100% keep rate. The
    resumed paths in the runner report companies_kept but never the enforcement drops,
    so kept / (kept + 0) came out as 1.0 -- and yield_medians() then taught the supply
    gate that enforcement rejects nothing. The truth is in staged_companies: every
    verdict the gate gave is a row there. Density is verified contacts over companies
    the finders actually searched (found_at set), which the resumed paths also lack.
    """
    kept = int(verdicts.get("KEEP", 0)) + int(verdicts.get("IDPR", 0))
    dropped = int(verdicts.get("DROP", 0))
    fresh = kept + dropped
    if fresh == 0:                                   # nothing staged: old-style stats
        kept = int(stats.get("companies_kept") or 0)
        fresh = kept + int(stats.get("companies_dropped_enforcement") or 0)
    verified = int(stats.get("contacts_verified") or 0)
    sendable = int(stats.get("contacts_sendable") or stats.get("copy_generated") or 0)
    denom = searched or int(stats.get("companies_kept") or 0)
    return {
        "fresh": fresh, "kept": kept,
        "keep_rate": round(kept / fresh, 4) if fresh else None,
        "verified": verified,
        "density": round(verified / denom, 3) if denom else None,
        "sendable": sendable,
        "copy_pass": round(sendable / verified, 4) if verified else None,
    }


def record_yield(vertical: str, run_id: str, stats: dict) -> None:
    """What a vertical ACTUALLY produced, so the next projection is measured not assumed.

    Troy, 2026-09-11: grade verticals "by tests and results". The stage 2b projection
    assumed 35% enforcement keep and 1.2 contacts per company; medical device measured
    49% and 1.67, DTC Beauty 2% and 2.5. Every run now writes its real ratios here and
    yield_medians() hands them back to the gate. Ratios come from the staged verdicts
    and the finders' markers (see _yield_ratios), so a resumed run records the same
    truth as a fresh one.
    """
    with cursor() as cur:
        cur.execute("SELECT verdict, count(*) AS n FROM staged_companies "
                    " WHERE vertical=%s AND verdict IS NOT NULL GROUP BY verdict", (vertical,))
        verdicts = {r["verdict"]: r["n"] for r in cur.fetchall()}
        cur.execute("SELECT count(*) AS n FROM staged_companies "
                    " WHERE vertical=%s AND found_at IS NOT NULL", (vertical,))
        searched = int(cur.fetchone()["n"] or 0)
        # Verified is the vertical's whole banked pool when the stats do not say.
        if not stats.get("contacts_verified"):
            cur.execute("SELECT count(*) AS n FROM contacts WHERE vertical=%s "
                        " AND status IN ('verified','draft','reserved','approved','pushed')",
                        (vertical,))
            stats = {**stats, "contacts_verified": int(cur.fetchone()["n"] or 0)}
    y = _yield_ratios(verdicts, searched, stats)
    with cursor() as cur:
        cur.execute(
            """INSERT INTO vertical_yields (vertical, run_id, universe, fresh, kept,
                   keep_rate, candidates, verified, density, sendable, copy_pass)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (vertical, run_id) DO UPDATE SET
                   universe=EXCLUDED.universe, fresh=EXCLUDED.fresh, kept=EXCLUDED.kept,
                   keep_rate=EXCLUDED.keep_rate, candidates=EXCLUDED.candidates,
                   verified=EXCLUDED.verified, density=EXCLUDED.density,
                   sendable=EXCLUDED.sendable, copy_pass=EXCLUDED.copy_pass""",
            (vertical, run_id, int(stats.get("available_in_aiark") or 0), y["fresh"],
             y["kept"], y["keep_rate"], int(stats.get("contacts_found") or 0),
             y["verified"], y["density"], y["sendable"], y["copy_pass"]))


def vertical_density(vertical: str, min_searched: int = 200) -> float | None:
    """This vertical's OWN measured contacts-per-searched-company, newest run first.

    Troy, 2026-09-30, on why launches land short of 3,000: stage 3 sized company
    pulls on assumed_contacts_per_company (1.2) while the measured figure this week
    was 0.16-0.2 -- with Apollo and AI Ark out of credit, QuickEnrich alone -- so
    every vertical was fetching a sixth of the companies it actually needed and the
    surplus banked for top-ups was a few hundred, not the thousands the gap called
    for. The vertical's own density is the number to size on; a run that searched
    fewer than min_searched companies is too small a sample to trust.
    """
    with cursor() as cur:
        cur.execute(
            """SELECT y.density FROM vertical_yields y
                WHERE y.vertical = %s AND y.density IS NOT NULL AND y.density > 0
                  AND (SELECT count(*) FROM staged_companies s
                        WHERE s.vertical = y.vertical AND s.found_at IS NOT NULL) >= %s
                ORDER BY y.run_id DESC LIMIT 1""",
            (vertical, min_searched))
        row = cur.fetchone()
    return float(row["density"]) if row and row["density"] else None


def yield_medians(min_rows: int = 2) -> dict[str, float]:
    """Median keep_rate, density and copy_pass across recorded verticals, or {}."""
    with cursor() as cur:
        cur.execute(
            """SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY keep_rate) AS keep_rate,
                      percentile_cont(0.5) WITHIN GROUP (ORDER BY density)   AS density,
                      percentile_cont(0.5) WITHIN GROUP (ORDER BY copy_pass) AS copy_pass,
                      count(*) AS n
                 FROM vertical_yields WHERE kept > 0 AND verified > 0""")
        r = cur.fetchone()
    if not r or (r["n"] or 0) < min_rows:
        return {}
    return {k: float(r[k]) for k in ("keep_rate", "density", "copy_pass") if r[k] is not None}


def record_contact_outcomes(rows: Sequence[dict]) -> int:
    """Write what EmailBison saw happen to each contact we pushed.

    rows: {"email", "replied_at" (datetime|None), "interested" (bool), "bounced_at"}.
    Additive only: a reply once recorded is never un-recorded, and interested is OR-ed,
    because a later sweep sees the same replies again. This is the contact-level memory
    the pipeline had none of -- 20,000 contacts built with no result attached -- so the
    system can start learning which titles, tiers and copy actually get answered.
    """
    payload = [
        ((r.get("email") or "").strip().lower(), r.get("replied_at"),
         bool(r.get("interested")), r.get("bounced_at"))
        for r in rows if r.get("email")
    ]
    if not payload:
        return 0
    with cursor() as cur:
        cur.executemany(
            """UPDATE contacts
                  SET replied_at = COALESCE(contacts.replied_at, %s),
                      interested = COALESCE(contacts.interested, FALSE) OR %s,
                      bounced_at = COALESCE(contacts.bounced_at, %s),
                      outcome_synced_at = now()
                WHERE email = %s""",
            [(replied, interested, bounced, email)
             for email, replied, interested, bounced in payload])
        changed = cur.rowcount if cur.rowcount and cur.rowcount > 0 else len(payload)
        # Append-only event facts make the learning history replayable. Stable keys make
        # the recurring reply sweep idempotent.
        events = []
        for email, replied, interested, bounced in payload:
            for kind, when in (("replied", replied), ("interested", replied if interested else None),
                               ("bounced", bounced)):
                if when is None:
                    continue
                key = hashlib.sha256(f"emailbison:{email}:{kind}:{when}".encode()).hexdigest()
                events.append((key, email, kind, when))
        if events:
            cur.executemany(
                """INSERT INTO outcome_events
                       (event_key, email, campaign_id, event_type, occurred_at, source)
                   SELECT %s,%s,c.eb_campaign_id,%s,%s,'emailbison'
                     FROM contacts c WHERE c.email=%s
                   ON CONFLICT (event_key) DO NOTHING""",
                [(key, email, kind, when, email) for key, email, kind, when in events])
        return changed


def mark_booked(email: str, actor: str | None = None, note: str | None = None,
                occurred_at: datetime | None = None) -> bool:
    """Record a booked meeting without a CRM integration.

    The current contact projection is convenient for reporting; outcome_events remains
    the immutable source of truth. Repeating the same command is idempotent.
    """
    address = (email or "").strip().lower()
    when = occurred_at or datetime.now(timezone.utc)
    with cursor() as cur:
        cur.execute("SELECT eb_campaign_id FROM contacts WHERE email=%s", (address,))
        row = cur.fetchone()
        if not row:
            return False
        cur.execute(
            """UPDATE contacts SET booked_at=COALESCE(booked_at,%s),
                      booked_by=COALESCE(booked_by,%s), booked_note=COALESCE(booked_note,%s),
                      updated_at=now() WHERE email=%s""",
            (when, actor, note, address))
        key = hashlib.sha256(
            f"manual:{address}:{row['eb_campaign_id']}:booked".encode()).hexdigest()
        cur.execute(
            """INSERT INTO outcome_events
                   (event_key,email,campaign_id,event_type,occurred_at,source,actor,note)
               VALUES (%s,%s,%s,'booked',%s,'manual',%s,%s)
               ON CONFLICT (event_key) DO UPDATE
                  SET actor=COALESCE(EXCLUDED.actor,outcome_events.actor),
                      note=COALESCE(EXCLUDED.note,outcome_events.note)""",
            (key, address, row["eb_campaign_id"], when, actor, note))
    return True


def campaign_bookings(campaign_id: int) -> int:
    with cursor() as cur:
        cur.execute("SELECT count(DISTINCT email) AS n FROM outcome_events "
                    "WHERE campaign_id=%s AND event_type='booked'", (campaign_id,))
        return int(cur.fetchone()["n"] or 0)


def experiment_performance(experiment_id: str) -> list[dict]:
    """Outcome table for allocation and reporting; one immutable exposure per send."""
    with cursor() as cur:
        cur.execute(
            """SELECT e.variant_id,
                      count(*) AS exposed,
                      count(*) FILTER (WHERE e.exposed_at < now() - interval '7 days'
                                             AND NOT EXISTS (SELECT 1 FROM outcome_events o
                                                  WHERE o.email=e.email AND o.campaign_id=e.campaign_id
                                                    AND o.event_type='bounced')) AS delivered,
                      count(*) FILTER (WHERE e.exposed_at < now() - interval '7 days'
                                             AND EXISTS (SELECT 1 FROM outcome_events o
                                                  WHERE o.email=e.email AND o.campaign_id=e.campaign_id
                                                    AND o.event_type='replied')) AS replied,
                      count(*) FILTER (WHERE e.exposed_at < now() - interval '7 days'
                                             AND EXISTS (SELECT 1 FROM outcome_events o
                                                  WHERE o.email=e.email AND o.campaign_id=e.campaign_id
                                                    AND o.event_type='interested')) AS interested,
                      count(*) FILTER (WHERE e.exposed_at < now() - interval '7 days'
                                             AND EXISTS (SELECT 1 FROM outcome_events o
                                                  WHERE o.email=e.email AND o.campaign_id=e.campaign_id
                                                    AND o.event_type='booked')) AS booked
                 FROM send_exposures e
                WHERE e.experiment_id=%s
                GROUP BY e.variant_id ORDER BY e.variant_id""",
            (experiment_id,))
        return cur.fetchall()


def learning_segments(min_delivered: int = 50) -> list[dict]:
    """Observed segment outcomes. These explain performance but do not claim causality."""
    with cursor() as cur:
        cur.execute(
            """SELECT COALESCE(e.vertical,'unknown') AS vertical,
                      COALESCE(e.title_segment,'unknown') AS title_segment,
                      count(*) FILTER (WHERE e.exposed_at < now() - interval '7 days'
                                             AND NOT EXISTS (SELECT 1 FROM outcome_events o
                                                  WHERE o.email=e.email AND o.campaign_id=e.campaign_id
                                                    AND o.event_type='bounced')) AS delivered,
                      count(*) FILTER (WHERE e.exposed_at < now() - interval '7 days'
                                             AND EXISTS (SELECT 1 FROM outcome_events o
                                                  WHERE o.email=e.email AND o.campaign_id=e.campaign_id
                                                    AND o.event_type='replied')) AS replied,
                      count(*) FILTER (WHERE e.exposed_at < now() - interval '7 days'
                                             AND EXISTS (SELECT 1 FROM outcome_events o
                                                  WHERE o.email=e.email AND o.campaign_id=e.campaign_id
                                                    AND o.event_type='interested')) AS interested,
                      count(*) FILTER (WHERE e.exposed_at < now() - interval '7 days'
                                             AND EXISTS (SELECT 1 FROM outcome_events o
                                                  WHERE o.email=e.email AND o.campaign_id=e.campaign_id
                                                    AND o.event_type='booked')) AS booked
                 FROM send_exposures e
                GROUP BY 1,2
               HAVING count(*) FILTER (WHERE e.exposed_at < now() - interval '7 days'
                                              AND NOT EXISTS (SELECT 1 FROM outcome_events o
                                                   WHERE o.email=e.email AND o.campaign_id=e.campaign_id
                                                     AND o.event_type='bounced')) >= %s
                ORDER BY booked DESC, interested DESC, delivered DESC""",
            (min_delivered,))
        return cur.fetchall()


def copy_pass_rate(vertical: str, default: float = 0.92) -> float:
    """Share of contacts entering copy that come out sendable, for THIS vertical.

    From vertical_yields when recorded, else the cross-vertical median, else a default.
    Clamped to [0.6, 1.0]: a bad recording must not make copy ask for 5,000 contacts.
    """
    rate = None
    try:
        with cursor() as cur:
            cur.execute("SELECT copy_pass FROM vertical_yields WHERE vertical=%s "
                        " AND copy_pass IS NOT NULL ORDER BY recorded_at DESC LIMIT 1",
                        (vertical,))
            row = cur.fetchone()
        rate = float(row["copy_pass"]) if row else None
        if rate is None:
            rate = yield_medians().get("copy_pass")
    except Exception:
        rate = None
    return max(0.6, min(1.0, float(rate if rate else default)))


# Stages written by the periodic sweeps rather than by a pipeline run. stop_on_reply
# logs against the run that FIRST created the campaign, which may be days old, so its
# lines say nothing about whether that run is still working.
_SWEEP_STAGES = ("stop_on_reply", "stage8", "review")
# A run cannot honestly still be alive after this long. A crashed container never sets
# a final status, so 'running' rows accumulate and would otherwise look alive forever.
RUN_MAX_AGE_HOURS = int(os.getenv("RUN_MAX_AGE_HOURS", "12"))


def run_alive(within_minutes: int = 10, exclude_prefixes: tuple = ()) -> str | None:
    """The run_id of a run that is genuinely still working, else None.

    resume() checks this first, so that a resume kicked at web start-up cannot run
    beside the two-hourly cron and write the same copy twice.

    Liveness needs BOTH a recent log line and a run young enough to be real, and the
    log line must come from pipeline work. Without those two guards this returned a
    run from 14 Sep for three days (2026-09-17): the run never finished, so it stayed
    'running', and the two-hourly stop_on_reply sweep kept logging against its run_id.
    Every resume since 15 Sep exited with "already running" and did nothing -- no
    paused vertical resumed and no held push retried, silently.

    exclude_prefixes: run_ids starting with any of these never count as "alive" here.
    Troy, 2026-10-01: the nightly review's wait-loop used this with no exclusion and
    waited out its full 60 minutes behind a `midweek-*` run sourcing Biotechnology
    Research for 4+ hours, then skipped the whole night's scaling anyway. Midweek
    intake only claims queued, never-launched vertical_backlog rows; the review only
    scales verticals that already have a live campaign -- the two touch disjoint
    verticals, so there is no double-scale risk in letting the review proceed beside
    one. The resume_interrupted_scale_ups / resume_unscaled_winners paths that DO
    share review's verticals log under the plain `resume-*` run_id and are correctly
    still covered.
    """
    with cursor() as cur:
        cur.execute(
            """SELECT r.run_id FROM runs r
                WHERE r.status='running'
                  AND r.started_at > now() - make_interval(hours => %s)
                  AND NOT (r.run_id LIKE ANY(%s))
                  AND EXISTS (SELECT 1 FROM logs l
                               WHERE l.run_id = r.run_id
                                 AND l.ts > now() - make_interval(mins => %s)
                                 AND coalesce(l.stage,'') <> ALL(%s))
                ORDER BY r.started_at DESC LIMIT 1""",
            # An empty list is "exclude nothing": LIKE ANY('{}') is false, NOT false
            # is true. The first cut used a NUL-byte placeholder here and Postgres
            # refuses NUL in text -- every 2-hourly tick on 2026-10-01 died on this
            # line from 08:00 to 16:00 UTC.
            (RUN_MAX_AGE_HOURS, [f"{p}%" for p in exclude_prefixes],
             within_minutes, list(_SWEEP_STAGES)))
        row = cur.fetchone()
    return row["run_id"] if row else None


def run_is_truly_alive(run_id: str | None, within_minutes: int = 45) -> bool:
    """Is this SPECIFIC run_id genuinely still working -- same freshness test as
    run_alive(), scoped to one run rather than "is anything alive".

    Troy, 2026-10-01: used by s8_review._do_act_on_winner to decide whether a
    campaign's scaling_run_id names a tick that is really still scaling it right
    now, versus one that died and left the stamp behind (db.interrupted_scale_ups
    already covers that dead case). Only a genuinely-alive owner is a reason to
    defer that ONE vertical this review; everything else the review scales as usual.
    """
    if not run_id:
        return False
    with cursor() as cur:
        cur.execute(
            """SELECT 1 FROM runs r
                WHERE r.run_id = %s AND r.status='running'
                  AND r.started_at > now() - make_interval(hours => %s)
                  AND EXISTS (SELECT 1 FROM logs l
                               WHERE l.run_id = r.run_id
                                 AND l.ts > now() - make_interval(mins => %s)
                                 AND coalesce(l.stage,'') <> ALL(%s))""",
            (run_id, RUN_MAX_AGE_HOURS, within_minutes, list(_SWEEP_STAGES)))
        return cur.fetchone() is not None


def close_abandoned_runs(older_than_hours: int | None = None,
                         stale_after_minutes: int = 45) -> int:
    """Give up on 'running' rows no worker can still own, so the table stays truthful.

    Two independent reasons to close a row -- either is enough:

    1. It has gone SILENT for stale_after_minutes with no log line at all, and is at
       least that old itself (so a run seconds into starting, before its first log
       line, is never mistaken for dead). A container that vanishes -- killed, OOM,
       Railway redeploying the service it runs on -- never gets to mark itself failed;
       silence is the only signal left. 45 minutes matches the window run_alive() and
       the /health banner already use to judge a run dead, so nothing new is being
       measured here, it just was never ACTED on before.
    2. It is simply too old (older_than_hours, unchanged) -- a fallback for a run that
       is somehow still logging but has clearly outlived any real scale-up.

    Troy, 2026-09-25: Consumer Edtech's scale-up sat orphaned for 9.5 hours after its
    run died at 04:19 -- the run stayed 'running' until it aged past the 12-hour
    default, and interrupted_scale_ups() checks status<>'running', so the vertical it
    had stamped was invisible to the resume mechanism for the whole gap even though the
    two-hourly resume() cron ran six times in that window.
    """
    hours = RUN_MAX_AGE_HOURS if older_than_hours is None else older_than_hours
    with cursor() as cur:
        cur.execute(
            """UPDATE runs SET status='abandoned',
                      finished_at=COALESCE(finished_at, now()),
                      notes=COALESCE(notes, 'no worker; closed by resume')
                WHERE status='running'
                  AND (
                        started_at < now() - make_interval(hours => %s)
                        OR (
                             started_at < now() - make_interval(mins => %s)
                             AND NOT EXISTS (
                                 SELECT 1 FROM logs l
                                  WHERE l.run_id = runs.run_id
                                    AND l.ts > now() - make_interval(mins => %s))
                           )
                      )""", (hours, stale_after_minutes, stale_after_minutes))
        return cur.rowcount


def record_lead_ids(id_by_email: dict[str, int]) -> None:
    """EmailBison lead ids as each upsert chunk returns -- progress a cut-off push keeps."""
    if not id_by_email:
        return
    with cursor() as cur:
        cur.executemany("UPDATE contacts SET eb_lead_id=%s WHERE email=%s",
                        [(lid, e) for e, lid in id_by_email.items()])


def mark_attached(emails: list[str], campaign_id: int) -> None:
    """These contacts are now IN the campaign. A re-push skips them."""
    if not emails:
        return
    with cursor() as cur:
        cur.execute("UPDATE contacts SET eb_campaign_id=%s WHERE email = ANY(%s)",
                    (campaign_id, [e.lower() for e in emails]))
        cur.execute(
            """INSERT INTO send_exposures
                   (exposure_id,email,campaign_id,experiment_id,variant_id,vertical,
                    title_segment,prompt_version)
               SELECT md5(c.email || ':' || %s::text || ':' || c.experiment_id),
                      c.email,%s,c.experiment_id,c.copy_variant,c.vertical,
                      c.title_segment,c.prompt_version
                 FROM contacts c
                WHERE c.email = ANY(%s) AND c.experiment_id IS NOT NULL
               ON CONFLICT (exposure_id) DO NOTHING""",
            (campaign_id, campaign_id, [e.lower() for e in emails]))


def add_arm(client_name: str, name: str, angle: str, bet: str = "",
            by: str = "slack") -> None:
    """Store a copy angle handed over from Slack as a testable arm."""
    with cursor() as cur:
        cur.execute(
            """INSERT INTO client_arms (client, name, angle, bet, created_by)
               VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (client, name) DO UPDATE
                  SET angle=EXCLUDED.angle, bet=EXCLUDED.bet,
                      created_by=EXCLUDED.created_by, retired_at=NULL""",
            (client_name, name, angle.strip(), bet.strip()[:300], by))


def retire_arm(client_name: str, name: str) -> bool:
    with cursor() as cur:
        cur.execute("UPDATE client_arms SET retired_at=now() "
                    " WHERE client=%s AND name=%s AND retired_at IS NULL",
                    (client_name, name))
        return cur.rowcount > 0


def arms(client_name: str) -> dict[str, dict]:
    """Live arms this client has been handed, keyed by name."""
    try:
        with cursor() as cur:
            cur.execute("SELECT name, angle, bet, created_by FROM client_arms "
                        " WHERE client=%s AND retired_at IS NULL ORDER BY created_at",
                        (client_name,))
            return {r["name"]: dict(r) for r in cur.fetchall()}
    except Exception:
        return {}


def verticals_already_run() -> set[str]:
    """Lower-cased names of verticals this client has already worked."""
    try:
        with cursor() as cur:
            cur.execute("SELECT DISTINCT lower(vertical) v FROM contacts "
                        " WHERE vertical IS NOT NULL")
            return {r["v"] for r in cur.fetchall() if r["v"]}
    except Exception:
        return set()


def experiments_seen() -> list[str]:
    """Every experiment this client's exposures actually carry.

    The learning page used to ask for one hardcoded id, so a copy test writing any
    other id was invisible on the one screen built to show it. Ask the data instead.
    """
    with cursor() as cur:
        cur.execute("""SELECT experiment_id, count(*) AS n FROM send_exposures
                        WHERE experiment_id IS NOT NULL
                        GROUP BY experiment_id ORDER BY n DESC""")
        return [r["experiment_id"] for r in cur.fetchall()]


def set_active_variant(client_name: str, variant: str | None, by: str = "system",
                       evidence: str = "") -> None:
    """Record which copy arm won. This is what makes a test conclude rather than end."""
    with cursor() as cur:
        cur.execute(
            """INSERT INTO client_state (client, active_variant, variant_set_at,
                                         variant_set_by, variant_evidence)
               VALUES (%s,%s,now(),%s,%s)
               ON CONFLICT (client) DO UPDATE
                  SET active_variant=EXCLUDED.active_variant,
                      variant_set_at=now(), variant_set_by=EXCLUDED.variant_set_by,
                      variant_evidence=EXCLUDED.variant_evidence, updated_at=now()""",
            (client_name, variant, by, evidence[:500]))


def active_variant(client_name: str) -> str | None:
    """The arm a concluded test promoted, or None while copy is still unsettled."""
    try:
        with cursor() as cur:
            cur.execute("SELECT active_variant FROM client_state WHERE client=%s",
                        (client_name,))
            row = cur.fetchone()
        return row["active_variant"] if row else None
    except Exception:
        return None


def record_campaign_variant(campaign_id: int, variant: str | None,
                            experiment_id: str | None) -> None:
    """Tag a campaign with the arm it carries, so the reviewer can tell an arm from
    an audience and does not record 'copy-test a-sleep-test' as a dead market."""
    if not variant:
        return
    with cursor() as cur:
        cur.execute("UPDATE ve_campaigns SET copy_variant=%s, experiment_id=%s "
                    " WHERE campaign_id=%s", (variant, experiment_id, campaign_id))


def open_copy_test(run_id: str, client_name: str, arms: list[str],
                   audiences: list[str], target: int, sample: int = 0) -> None:
    with cursor() as cur:
        cur.execute(
            """INSERT INTO copy_tests (run_id, client, arms, audiences, target, sample)
               VALUES (%s,%s,%s,%s,%s,%s)
               ON CONFLICT (run_id) DO NOTHING""",
            (run_id, client_name, json.dumps(_json_safe(arms)),
             json.dumps(_json_safe(audiences)), target, sample))


def unfinished_copy_test(client_name: str) -> dict | None:
    """The copy test this client still owes copy for, if any."""
    with cursor() as cur:
        cur.execute("SELECT * FROM copy_tests WHERE client=%s AND status='running' "
                    " ORDER BY started_at DESC LIMIT 1", (client_name,))
        return cur.fetchone()


def finish_copy_test(run_id: str, status: str = "complete") -> None:
    with cursor() as cur:
        cur.execute("UPDATE copy_tests SET status=%s, finished_at=now() WHERE run_id=%s",
                    (status, run_id))


def contacts_without_variant(run_id: str) -> list[dict]:
    """Verified contacts banked by a run that never reached arm assignment."""
    with cursor() as cur:
        cur.execute(
            "SELECT * FROM contacts "
            " WHERE run_id=%s AND status='verified' AND copy_variant IS NULL "
            # email is the primary key here; there is no id column.
            "   AND email IS NOT NULL ORDER BY first_seen_at, email", (run_id,))
        return [dict(r) for r in cur.fetchall()]


def set_contact_variants(rows: Sequence[dict], variant: str,
                         experiment_id: str) -> int:
    """Stamp an arm onto contacts, so a resumed test writes the right copy for them."""
    emails = [(r.get("email") or "").strip().lower() for r in rows if r.get("email")]
    if not emails:
        return 0
    with cursor() as cur:
        cur.execute(
            "UPDATE contacts SET copy_variant=%s, experiment_id=%s "
            " WHERE lower(email) = ANY(%s) AND copy_variant IS NULL",
            (variant, experiment_id, emails))
        return cur.rowcount


def uncopied_by_variant(arms: list[str]) -> dict[str, list[dict]]:
    """Contacts already assigned to an arm whose copy was never written.

    This is what makes a copy test resumable: the arm is persisted on the contact when
    it is banked, so a restart does not reshuffle anyone between arms, and the work
    left is simply the rows with a variant and no copy.
    """
    if not arms:
        return {}
    with cursor() as cur:
        cur.execute(
            """SELECT email, linkedin_url, company_domain, first_name, last_name, title,
                      company_name, vertical, email_verified_at, copy_variant,
                      experiment_id
                 FROM contacts
                WHERE copy_variant = ANY(%s)
                  AND status = 'verified'
                  AND personalization_1 IS NULL
                ORDER BY company_domain""", (list(arms),))
        out: dict[str, list[dict]] = {a: [] for a in arms}
        for r in cur.fetchall():
            out.setdefault(r["copy_variant"], []).append(dict(r))
    return out


def orphan_drafts(vertical: str) -> list[dict]:
    """Finished copy for this vertical that no batch carries yet.

    A run that is restarted after copy has begun leaves its drafts behind as status
    'draft' with no batch. The next run only ever batched what IT wrote, so those
    drafts -- paid for, complete -- were orphaned: 805 for medical device, 1,583 for
    product design on 2026-09-14. They are the first thing a batch should take.
    """
    with cursor() as cur:
        cur.execute(
            """SELECT email, linkedin_url, company_domain, first_name, last_name, title,
                      company_name, vertical, email_verified_at,
                      personalization_1, personalization_2, research_report,
                      prompt_version, research_model, copy_model, experiment_id,
                      copy_variant, title_segment
                 FROM contacts
                WHERE vertical = %s AND status = 'draft'
                  AND personalization_1 IS NOT NULL AND personalization_2 IS NOT NULL
                ORDER BY updated_at""", (vertical,))
        return [dict(r) for r in cur.fetchall()]


def verified_uncopied(vertical: str) -> list[dict]:
    """Contacts verified for this vertical whose copy never got written.

    Stage 4 output used to live only in memory. On 2026-09-11 a run verified 2,500
    contacts for medical device, then the LLM ran out of credit at the first copy call;
    the halt guard saved the run but ~2,954 paid verifications evaporated with the
    process. Stage 4 now persists as status 'verified', and the runner resumes from
    here instead of re-sourcing and re-verifying.

    Re-applies the CURRENT title rules before anything goes into copy. Troy, 2026-09-26:
    "did the medical devices run with all our new changes yesterday, including job
    titles?" Only partly -- the title filter runs when contacts are SOURCED, so contacts
    banked before the 2026-09-25 fix (and the ones detached from batches during that
    day's cleanup) sat here as 'verified' and got paid-for copy written anyway. 350 of
    Medical Device's 2,255 and 111 of Venture Capital's 4,192 new batch contacts were
    off-ICP for exactly that reason, and 914 more (28% of everything banked) were still
    waiting. Off-ICP ones are killed here rather than merely skipped, so the same
    contacts are not re-scored on every tick and cannot be requeued back in.
    """
    with cursor() as cur:
        cur.execute(
            """SELECT email, linkedin_url, company_domain, first_name, last_name, title,
                      company_name, vertical, email_verified_at
                 FROM contacts
                WHERE vertical = %s AND status = 'verified'
                  AND personalization_1 IS NULL""", (vertical,))
        rows = [dict(r) for r in cur.fetchall()]
    # Imported here, not at module top: s4_people imports db, so a top-level import
    # would be circular.
    from .stages.s4_people import score_title
    fit, off_icp = [], []
    for row in rows:
        (fit if score_title(row.get("title"), 200) > 0 else off_icp).append(row)
    if off_icp:
        with cursor() as cur:
            cur.execute(
                """UPDATE contacts SET status='killed', killed_at=now(), updated_at=now(),
                          drop_reason='off-ICP title, caught before copy (2026-09-26)'
                    WHERE email = ANY(%s) AND status='verified'""",
                ([r["email"] for r in off_icp],))
        log(None, "stage5", f"{vertical}: {len(off_icp)} banked contacts fail the current "
            f"title rules; killed before copy instead of paying to write for them")
    return fit


def record_vertical(vertical: str, run_id: str) -> None:
    with cursor() as cur:
        cur.execute(
            "INSERT INTO verticals_run (vertical, run_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
            (vertical, run_id),
        )


def recent_verticals(days: int) -> set[str]:
    with cursor() as cur:
        cur.execute(
            "SELECT DISTINCT vertical FROM verticals_run "
            "WHERE ran_at > now() - make_interval(days => %s)",
            (days,),
        )
        return {r["vertical"] for r in cur.fetchall()}


def pending_batch_for(vertical: str) -> dict | None:
    """The batch this vertical already has awaiting approval, if any."""
    with cursor() as cur:
        cur.execute("SELECT batch_id, contact_count, company_count, expires_at "
                    "  FROM batches WHERE vertical=%s AND approval_status='pending' "
                    " ORDER BY created_at DESC LIMIT 1", (vertical,))
        return cur.fetchone()


def add_to_batch(batch_id: str, run_id: str, contacts: Sequence[dict],
                 companies: Sequence[dict], vertical: str) -> int:
    """Attach more contacts to a batch that is still awaiting approval.

    Troy, 2026-09-14: "can they be resumed and loaded to the existing batch awaiting
    approval?" A batch that closed short -- because Serper ran dry mid-copy and its
    failures were counted as individual contacts -- would otherwise need a SECOND
    batch, and a second batch means a second EmailBison campaign for one vertical,
    which breaks the per-vertical stats the 90-day scoring reads.
    """
    if not contacts:
        return 0
    written = reserve_contacts(contacts, run_id, batch_id)
    reserve_companies(companies, run_id, batch_id, vertical)
    with cursor() as cur:
        cur.execute(
            """UPDATE batches SET
                 contact_count = (SELECT count(*) FROM contacts WHERE batch_id=%s),
                 company_count = (SELECT count(*) FROM companies WHERE batch_id=%s),
                 -- A batch that is still growing is still worth deciding on. Troy,
                 -- 2026-09-30: paid sourcing and copy must not expire at 72h while
                 -- the top-up loop is actively adding to it; the clock restarts on
                 -- every addition, and expiry_warned_at resets so the next warning
                 -- is about the NEW deadline.
                 expires_at = GREATEST(expires_at, now() + make_interval(hours => %s)),
                 expiry_warned_at = NULL
               WHERE batch_id=%s""",
            (batch_id, batch_id, settings.approval_timeout_hours, batch_id))
    return written


def record_batch_addition(batch_id: str, added: int) -> int:
    """Track a top-up against the running total since the last Slack notification.

    Returns the new running total so the caller can decide whether it has crossed
    NOTIFY_ADDED_THRESHOLD and is worth actually posting about.
    """
    with cursor() as cur:
        cur.execute(
            """UPDATE batches SET unnotified_added_count = unnotified_added_count + %s
               WHERE batch_id=%s RETURNING unnotified_added_count""",
            (added, batch_id))
        row = cur.fetchone()
        return row["unnotified_added_count"] if row else added


def reset_batch_notification(batch_id: str) -> None:
    with cursor() as cur:
        cur.execute("UPDATE batches SET unnotified_added_count=0 WHERE batch_id=%s",
                    (batch_id,))


def create_batch(batch_id: str, run_id: str, vertical: str, summary: dict,
                 company_count: int, contact_count: int) -> None:
    expires = datetime.now(timezone.utc) + timedelta(hours=settings.approval_timeout_hours)
    with cursor() as cur:
        cur.execute(
            """INSERT INTO batches (batch_id, run_id, vertical, summary, company_count,
                                    contact_count, expires_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (batch_id, run_id, vertical, json.dumps(_json_safe(summary), default=str),
             company_count, contact_count, expires),
        )


def reserve_companies(rows: Sequence[dict], run_id: str, batch_id: str, vertical: str) -> int:
    """Soft-reserve domains for a pending batch (decision #9)."""
    if not rows:
        return 0
    payload = [
        (normalize_domain(r["domain"]), r.get("name"), vertical, run_id, batch_id)
        for r in rows
        if r.get("domain")
    ]
    payload = [tuple(_json_safe(v) for v in row) for row in payload]
    with cursor() as cur:
        cur.executemany(
            """
            INSERT INTO companies (domain, name, vertical, run_id, batch_id, status)
            VALUES (%s,%s,%s,%s,%s,'reserved')
            ON CONFLICT (domain) DO UPDATE
               SET status='reserved', batch_id=EXCLUDED.batch_id, run_id=EXCLUDED.run_id,
                   vertical=EXCLUDED.vertical, updated_at=now()
             WHERE companies.status <> 'pushed'
            """,
            payload,
        )
    return len(payload)


def reserve_contacts(rows: Sequence[dict], run_id: str, batch_id: str | None,
                     status: str = 'reserved') -> int:
    """Persist a batch's contacts as 'reserved'.

    This function is the last place a finished run can lose everything, so it is
    defensive in three ways. On 2026-09-11 the medical device run generated 615 complete
    emails over two hours and stored NONE of them: two contacts shared a LinkedIn URL,
    and contacts_linkedin_key is a separate partial unique index that ON CONFLICT
    (email) does not cover, so the whole executemany aborted and rolled back. The batch
    row survived, pointing at zero contacts, and the copy existed only in memory.

    So, in order:
      1. de-duplicate by LinkedIn URL WITHIN the payload, keeping the first occurrence
      2. blank the URL on any row whose URL is already held by a DIFFERENT email -- the
         contact is still good, only the profile link is contested, and email is the
         real identity key here
      3. fall back to row-by-row inserts if the bulk insert still fails, so a bad row
         costs one contact instead of the entire run
    """
    if not rows:
        return 0

    # A contact already marked 'pushed' may be re-approached once it falls outside the
    # recontact window -- that is the whole point of the recency rule. Without this the
    # guard below silently drops exactly the people the rule unlocks: measured
    # 2026-09-11, a batch reported 61 contacts and stored 35, and the copy for the other
    # 26 was never written at all, at either the draft or the reserve step.
    #
    # And the guard must mirror blocked_contacts exactly. The 223,863 backfilled rows
    # are 'pushed' with an INGESTION date (8 Sep 2026), not a send date; dedup already
    # ignores that date (it trusts recent_contacts instead), but this guard read it as
    # "pushed inside 90 days" and refused the row. Measured 2026-09-13: 42, 58, then 93
    # verified contacts per slice -- found and paid for -- silently not written.
    push_cutoff = datetime.now(timezone.utc) - timedelta(days=settings.recontact_days)

    seen_li: set[str] = set()
    prepared: list[dict] = []
    for r in rows:
        if not r.get("email"):
            continue
        li = normalize_linkedin(r.get("linkedin_url"))
        if li and li in seen_li:
            li = None                      # (1) duplicate inside this batch
        elif li:
            seen_li.add(li)
        prepared.append({**r, "_li": li})

    # (2) URLs already attached to a different email in the table.
    urls = sorted({r["_li"] for r in prepared if r["_li"]})
    if urls:
        with cursor() as cur:
            cur.execute(
                "SELECT email, linkedin_url FROM contacts "
                " WHERE lower(rtrim(linkedin_url,'/')) = ANY(%s)", (urls,))
            owner = {normalize_linkedin(x["linkedin_url"]): (x["email"] or "").lower()
                     for x in cur.fetchall()}
        for r in prepared:
            li = r["_li"]
            if li and owner.get(li) not in (None, r["email"].strip().lower()):
                r["_li"] = None

    payload = [
        (
            r["email"].strip().lower(), r["_li"],
            normalize_domain(r.get("company_domain", "")),
            r.get("first_name"), r.get("last_name"), r.get("title"),
            run_id, batch_id, status, r.get("email_verified_at"),
            r.get("personalization_1"), r.get("personalization_2"),
            r.get("research_report"), r.get("company_name"), r.get("vertical"),
             r.get("prompt_version"), r.get("research_model"), r.get("copy_model"),
             r.get("experiment_id"), r.get("copy_variant"), r.get("title_segment"),
             push_cutoff,
        )
        for r in prepared
    ]

    # Same NUL rule as the company path: Postgres rejects a NUL in TEXT as hard as in
    # jsonb, and these columns carry vendor names, vendor titles and LLM output -- three
    # sources we do not control. Apparel & Footwear died on one bad character in one
    # vendor record; a NUL in a research_report would end a whole copy run the same way,
    # after every one of those contacts had already been paid for.
    payload = [tuple(_json_safe(v) for v in row) for row in payload]

    sql = """
        INSERT INTO contacts (email, linkedin_url, company_domain, first_name, last_name,
                              title, run_id, batch_id, status, email_verified_at,
                              personalization_1, personalization_2, research_report,
                              company_name, vertical, prompt_version, research_model,
                              copy_model, experiment_id, copy_variant, title_segment)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (email) DO UPDATE
           SET status=EXCLUDED.status,
               batch_id=COALESCE(EXCLUDED.batch_id, contacts.batch_id),
               run_id=EXCLUDED.run_id,
               linkedin_url=COALESCE(EXCLUDED.linkedin_url, contacts.linkedin_url),
               personalization_1=EXCLUDED.personalization_1,
               personalization_2=EXCLUDED.personalization_2,
               research_report=EXCLUDED.research_report,
               company_name=EXCLUDED.company_name, vertical=EXCLUDED.vertical,
               prompt_version=COALESCE(EXCLUDED.prompt_version, contacts.prompt_version),
               research_model=COALESCE(EXCLUDED.research_model, contacts.research_model),
                copy_model=COALESCE(EXCLUDED.copy_model, contacts.copy_model),
                experiment_id=COALESCE(EXCLUDED.experiment_id, contacts.experiment_id),
                copy_variant=COALESCE(EXCLUDED.copy_variant, contacts.copy_variant),
                title_segment=COALESCE(EXCLUDED.title_segment, contacts.title_segment),
               updated_at=now()
         WHERE contacts.status <> 'pushed'
            OR contacts.source <> 'pipeline'
            OR contacts.updated_at < %s
    """
    try:
        with cursor() as cur:
            cur.executemany(sql, payload)
            written = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0                 else len(payload)
        if written < len(payload):
            # Silence here is what hid the 61-vs-35 gap; say it out loud.
            log(run_id, "stage6",
                f"{len(payload) - written} of {len(payload)} contacts were not written "
                f"(existing rows refused the update)", level="warn")
        return written
    except Exception as exc:
        # (3) Never let one row take the batch down. Retry individually, dropping the
        # LinkedIn URL on a row that still collides.
        log(run_id, "stage6",
            f"bulk contact insert failed ({str(exc)[:90]}); retrying row by row",
            level="warn")
        stored = 0
        for row in payload:
            for attempt in (row, (row[0], None) + row[2:]):
                try:
                    with cursor() as cur:
                        cur.execute(sql, attempt)
                    stored += 1
                    break
                except Exception:
                    continue
        log(run_id, "stage6", f"row-by-row insert stored {stored} of {len(payload)} contacts",
            level="warn" if stored < len(payload) else "info")
        return stored


def save_drafts(rows: Sequence[dict], run_id: str) -> int:
    """Persist finished copy the moment it exists, before any batch is opened.

    Troy, 2026-09-11: "I want everything to not be lost when one little thing fails."

    Until now a run held every finished email in memory until stage 6 and wrote them all
    in one transaction at the end, so any failure after the copy stage destroyed the lot.
    That is not hypothetical: the medical device run generated 615 complete emails over
    two hours and stored zero of them when a single duplicate LinkedIn URL aborted the
    final insert. A DNS blip earlier in the week cost 290 the same way.

    Drafts are written with status 'draft', which no dedup rule treats as contacted, so
    a stored draft neither blocks the company nor counts as a send. Stage 6 promotes the
    same rows to 'reserved'.
    """
    return reserve_contacts(rows, run_id, None, status="draft")


def mark_needs_review(email: str, reason: str) -> None:
    with cursor() as cur:
        cur.execute(
            """INSERT INTO contacts (email, status, drop_reason)
               VALUES (%s,'needs_review',%s)
               ON CONFLICT (email) DO UPDATE
                  SET status='needs_review', drop_reason=EXCLUDED.drop_reason, updated_at=now()
                WHERE contacts.status <> 'pushed'""",
            (email.strip().lower(), reason),
        )


# Failure reasons a fresh copy attempt can never fix, so requeue_needs_review() must
# never touch them. "no first name" is a data gap copy cannot invent; an EmailBison
# decline means the contact is already in another sequence, bounced, or unsubscribed --
# external state, not a copy defect.
UNRETRYABLE_DROP_REASONS = ("no first name", "EmailBison declined")


def requeue_needs_review(vertical: str | None = None, max_requeues: int = 2,
                         limit: int = 2000) -> int:
    """Give a needs_review contact another shot at copy, unless nothing about trying
    again could ever change the outcome.

    Troy, 2026-09-25: "imagine how many needs review we've lost, how can we save
    them?" -- 2,889 all-time, and needs_review is a BLOCKING status (see
    BLOCKING_CONTACT_STATUSES): nothing ever retried them on its own. Most failed a
    COPY QA check -- wrong company name in the email, over the length limit, a
    leftover placeholder -- where a fresh generation has a real chance of landing
    differently. This resets exactly what verified_uncopied() looks for (status
    'verified' and no personalization written yet), so the existing copy-writing path
    picks these up with no separate plumbing. max_requeues stops a contact that keeps
    failing for a REAL, unfixable reason from being retried forever.
    """
    patterns = [f"%{p}%" for p in UNRETRYABLE_DROP_REASONS]
    where = ["status = 'needs_review'", "requeue_count < %s",
             "(drop_reason IS NULL OR NOT (drop_reason ILIKE ANY(%s)))"]
    params: list = [max_requeues, patterns]
    if vertical:
        where.append("vertical = %s")
        params.append(vertical)
    params.append(limit)
    with cursor() as cur:
        cur.execute(
            f"""UPDATE contacts SET status='verified', personalization_1=NULL,
                      personalization_2=NULL, research_report=NULL,
                      requeue_count = requeue_count + 1, updated_at = now()
                WHERE email IN (
                    SELECT email FROM contacts WHERE {' AND '.join(where)}
                    ORDER BY updated_at ASC LIMIT %s)""",
            params,
        )
        return cur.rowcount


def batch_contacts(batch_id: str) -> list[dict]:
    with cursor() as cur:
        cur.execute(
            "SELECT * FROM contacts WHERE batch_id=%s "
            "AND status IN ('reserved','approved') ORDER BY email",
            (batch_id,),
        )
        return cur.fetchall()


def get_batch(batch_id: str) -> dict | None:
    with cursor() as cur:
        cur.execute("SELECT * FROM batches WHERE batch_id=%s", (batch_id,))
        return cur.fetchone()


def settle_batch(batch_id: str, decision: str, decided_by: str | None = None,
                 reason: str | None = None) -> dict[str, int]:
    """Move a batch's reserved rows to their terminal status.

    approved             -> approved (gate passed; the push is a separate step)
    killed / auto_killed -> killed with killed_at set (60-day suppression, decision #8)
    """
    # 'expired' is NOT a kill. A batch nobody decided on within the window releases its
    # companies and contacts back to the pool; only an explicit /ve kill suppresses them
    # for killed_dedup_days. Before 2026-09-11 expiry mapped to 'killed', so six pending
    # batches (311 companies) were about to be locked out for 60 days by a timer.
    if decision == "approved":
        status = "approved"
    elif decision in ("expired", "auto_killed"):
        status = "expired"
    else:
        status = "killed"
    killed_at_sql = "now()" if status == "killed" else "NULL"
    # A kill's reason lands on every record it suppresses, so "why was this company
    # locked out for 60 days" is answerable from the row itself.
    drop_reason = f"killed: {reason}" if (status == "killed" and reason) else None
    if status == "expired":
        return _expire_batch(batch_id, decision, decided_by, reason)
    with cursor() as cur:
        cur.execute(
            f"""UPDATE companies SET status=%s, killed_at={killed_at_sql}, updated_at=now(),
                       drop_reason=COALESCE(%s, drop_reason)
                 WHERE batch_id=%s AND status='reserved'""",
            (status, drop_reason, batch_id),
        )
        companies = cur.rowcount
        cur.execute(
            f"""UPDATE contacts SET status=%s, killed_at={killed_at_sql}, updated_at=now(),
                       drop_reason=COALESCE(%s, drop_reason)
                 WHERE batch_id=%s AND status='reserved'""",
            (status, drop_reason, batch_id),
        )
        contacts = cur.rowcount
        cur.execute(
            """UPDATE batches SET approval_status=%s, approved_by=%s, decided_at=now(),
                      decision_reason=COALESCE(%s, decision_reason)
                WHERE batch_id=%s""",
            (decision, decided_by, reason, batch_id),
        )
    return {"companies": companies, "contacts": contacts}


def _expire_batch(batch_id: str, decision: str, decided_by: str | None,
                  reason: str | None) -> dict[str, int]:
    """An unanswered batch is RENEWED, not re-run.

    Troy, 2026-09-15: "what is the whole point of expired being requeued, because this
    mistake is costly?" The point was to keep paid-for copy. Re-running the vertical to
    do it was wrong: it re-sourced and re-wrote two dead test verticals for ~$70. Now
    expiry simply gives the same batch a fresh 72-hour clock and re-posts it, up to
    MAX_RENEWALS times. After that the contacts go back to 'draft' with no batch --
    their copy kept, usable if the vertical is ever run again -- and NOTHING is
    sourced or written. Money is only ever spent by a Monday run or a scale-up.
    """
    with cursor() as cur:
        cur.execute("SELECT vertical, contact_count, COALESCE(renewals, 0) AS renewals "
                    "  FROM batches WHERE batch_id=%s", (batch_id,))
        row = cur.fetchone()
    if not row:
        return {"companies": 0, "contacts": 0}
    if row["renewals"] < MAX_RENEWALS and (row["contact_count"] or 0) >= settings.resume_min_contacts:
        with cursor() as cur:
            cur.execute(
                """UPDATE batches SET expires_at = now() + make_interval(hours => %s),
                          renewals = COALESCE(renewals, 0) + 1, expiry_warned_at = NULL
                    WHERE batch_id=%s""",
                (settings.approval_timeout_hours, batch_id))
        log(None, "stage6", f"{batch_id} renewed for another {settings.approval_timeout_hours}h "
            f"(renewal {row['renewals'] + 1} of {MAX_RENEWALS}); nothing re-run")
        return {"companies": 0, "contacts": 0, "renewed": 1}
    with cursor() as cur:
        cur.execute(
            """UPDATE companies SET status='expired', updated_at=now()
                WHERE batch_id=%s AND status='reserved'""", (batch_id,))
        companies = cur.rowcount
        cur.execute(
            """UPDATE contacts
                  SET status = CASE WHEN personalization_1 IS NOT NULL THEN 'draft'
                                    ELSE 'expired' END,
                      batch_id = NULL, updated_at = now()
                WHERE batch_id=%s AND status='reserved'""", (batch_id,))
        contacts = cur.rowcount
        cur.execute(
            """UPDATE batches SET approval_status=%s, approved_by=%s, decided_at=now(),
                      decision_reason=COALESCE(%s, decision_reason)
                WHERE batch_id=%s""", (decision, decided_by, reason, batch_id))
    return {"companies": companies, "contacts": contacts}


def expire_stale_batches() -> list[str]:
    """Auto-kill batches past their approval window (spec Stage 6: 72h, logged)."""
    with cursor() as cur:
        cur.execute(
            "SELECT batch_id FROM batches WHERE approval_status='pending' AND expires_at < now()"
        )
        stale = [r["batch_id"] for r in cur.fetchall()]
    for batch_id in stale:
        settle_batch(batch_id, "expired", decided_by="system:timeout")
        log(None, "stage6", f"batch {batch_id} auto-killed after "
                            f"{settings.approval_timeout_hours}h with no response", level="warn")
    return stale


def pending_batches() -> list[dict]:
    with cursor() as cur:
        cur.execute(
            "SELECT * FROM batches WHERE approval_status='pending' ORDER BY created_at"
        )
        return cur.fetchall()


def mark_pushed(batch_id: str) -> int:
    """Called only after EmailBison has actually accepted the load (spec Stage 7)."""
    with cursor() as cur:
        cur.execute(
            "UPDATE companies SET status='pushed', updated_at=now() "
            " WHERE batch_id=%s AND status='approved'",
            (batch_id,),
        )
        cur.execute(
            "UPDATE contacts SET status='pushed', updated_at=now() "
            " WHERE batch_id=%s AND status='approved'",
            (batch_id,),
        )
        moved = cur.rowcount
        # The batch row too, so /ve status stops offering a Push button for it.
        cur.execute("UPDATE batches SET approval_status='pushed' WHERE batch_id=%s",
                    (batch_id,))
    return moved


def increment_pushed_lead_count(campaign_id: int, added: int) -> None:
    """The scale gate's own denominator. Troy, 2026-09-27: "does the system check how
    many leads are in a campaign before topping up?" It does -- scale_vertical compares
    pushed_lead_count against scale_target_leads -- but only s7_push's brand-new/pooled
    branch ever wrote to this column; every ordinary TOP-UP push (the common case since
    align_on_topup) never called it at all. Medical Device drifted 4,104 leads behind
    EmailBison's real count, Venture Capital 5,813 -- both understating how close each
    campaign already was to its 10,000 target and risking sourcing well past it.
    """
    if not added:
        return
    with cursor() as cur:
        cur.execute(
            "UPDATE ve_campaigns SET pushed_lead_count = pushed_lead_count + %s "
            " WHERE campaign_id=%s", (added, campaign_id))


# ---------------------------------------------------------------------------
# vertical backlog -- the queue of scored-but-not-yet-run verticals
# ---------------------------------------------------------------------------

def enqueue_verticals(scored: list[dict], configs: dict[str, dict]) -> int:
    """Park gated verticals for a later run. Queued rows keep their place."""
    rows = []
    for item in scored:
        cfg = configs.get(item["vertical"])
        if not cfg or item.get("verdict") != "ok":
            continue
        rows.append((item["vertical"], json.dumps(_json_safe(cfg), default=str),
                     cfg.get("seed_campaign"), int(item.get("projected") or 0),
                     int(item.get("projected_raw") or item.get("projected") or 0),
                     item.get("density"), item.get("fresh_rate")))
    if not rows:
        return 0
    with cursor() as cur:
        cur.executemany(
            "INSERT INTO vertical_backlog "
            " (vertical, config, seed_campaign, projected, projected_raw, density, "
            "  fresh_rate) "
            " VALUES (%s,%s,%s,%s,%s,%s,%s) "
            " ON CONFLICT (vertical) DO UPDATE "
            # A plain overwrite is right HERE, unlike in rescore_backlog: the caller
            # filters to verdict == "ok" before building these rows, so everything
            # reaching this statement is a real measurement, and a real measurement
            # must be allowed to revise a stale figure DOWNWARD. Clamping it upward
            # would pin a mined-out vertical at its best-ever number forever.
            "   SET projected=EXCLUDED.projected, "
            "       projected_raw=EXCLUDED.projected_raw, "
            "       density=EXCLUDED.density, "
            "       fresh_rate=EXCLUDED.fresh_rate, config=EXCLUDED.config "
            " WHERE vertical_backlog.status = 'queued'",
            rows,
        )
    return len(rows)


def rescore_backlog(probe) -> int:
    """Re-project queued verticals with the CURRENT probe. probe(config, density) -> dict."""
    with cursor() as cur:
        cur.execute("SELECT vertical, config, density FROM vertical_backlog "
                    " WHERE status='queued' LIMIT 8")
        rows = cur.fetchall()
    n = 0
    for r in rows:
        cfg = r["config"] if isinstance(r["config"], dict) else json.loads(r["config"])
        try:
            s = probe(cfg, float(r["density"]) if r["density"] else None)
        except Exception:
            continue
        # A FAILED probe must not overwrite a good measurement with zero. s2b returns
        # projected 0 with verdict "no usable search terms" / "probe returned nothing"
        # for a transient miss -- an empty label calibration, an HTTP 402, a vendor
        # blip. This used to be written straight over the stored value, and since the
        # queue claims by projected DESC, one bad probe dropped a vertical to the bottom
        # of the list permanently. Measured 2026-09-22: Applied AI & Machine Learning
        # probed at 50,793 reachable contacts -- the largest unworked audience IDPR has
        # -- and was sitting at 0, last in the queue, because a later probe returned
        # nothing. Keep what we knew until something better is actually measured.
        # Only a FAILED probe is ignored. "universe too small", "already mined" and
        # "only ~N reachable" are real measurements and must be allowed to lower a
        # stale figure -- treating them as failures would pin a mined-out vertical at
        # its old 8,156 forever and keep claiming it at the top of the queue, run after
        # run, to produce nothing.
        from .stages.s2b_freshness import PROBE_FAILURES
        verdict = s.get("verdict") or ""
        if verdict in PROBE_FAILURES:
            log(None, "stage2b",
                f"{r['vertical']}: rescore ignored ({verdict}); "
                f"keeping projected={r.get('projected')}", level="warn")
            continue
        fresh = int(s.get("projected") or 0)
        with cursor() as cur:
            cur.execute(
                "UPDATE vertical_backlog SET projected=%s, projected_raw=%s, "
                "       density=%s, fresh_rate=%s "
                " WHERE vertical=%s AND status='queued'",
                (fresh, int(s.get("projected_raw") or fresh),
                 s.get("density"), s.get("fresh_rate"), r["vertical"]))
        n += 1
    return n


def source_keep_rates(vertical: str | None = None) -> list[dict]:
    """Keep rate by source. The only honest answer to "are they equally good".

    Both sources feed the SAME fit check, so the share each one gets past it is a fair
    comparison. Troy, 2026-09-21: "walk me through how using BuzzLead and AI Ark can
    have the same efficiency rate" -- they can be measured against each other, and until
    the source was recorded they could not be.
    """
    where = " WHERE source IS NOT NULL"
    args: tuple = ()
    if vertical:
        where += " AND vertical=%s"
        args = (vertical,)
    with cursor() as cur:
        cur.execute(
            "SELECT source, count(*) AS staged, count(verdict) AS judged, "
            "       count(*) FILTER (WHERE verdict IN ('KEEP','IDPR')) AS kept "
            "  FROM staged_companies" + where + " GROUP BY 1 ORDER BY 2 DESC", args)
        out = []
        for r in cur.fetchall():
            d = dict(r)
            d["keep_rate"] = (d["kept"] / d["judged"]) if d["judged"] else None
            out.append(d)
        return out


def _json_safe(value):
    """Strip NUL bytes before anything reaches Postgres.

    A company record carrying \u0000 anywhere in it kills the whole INSERT:
    "unsupported Unicode escape sequence -- \u0000 cannot be converted to text".
    Postgres accepts no NUL in text or jsonb, at all, and json.dumps happily emits it.

    Apparel & Footwear, 2026-09-21: the vertical had scanned 5,315 of 7,083 companies
    and kept 2,303 of the 2,595 it needed -- nearly done -- and one bad character in one
    vendor record ended it. It was then filed as "paused for LLM", which it was not.
    """
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, tuple):
        # Keep the tuple a tuple. psycopg renders a list as an ARRAY and a tuple as a
        # composite/IN-list, so collapsing one into the other silently changes the SQL
        # a query generates. Harmless while every element is a scalar; a landmine for
        # whoever first puts a tuple in one of these payloads.
        return tuple(_json_safe(v) for v in value)
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def stage_companies(vertical: str, run_id: str, rows: Sequence[dict],
                    source: str | None = None) -> int:
    """Persist fetched companies BEFORE enforcement judges them.

    Troy, 2026-09-13: "it must run to its limit even when ... concentrate is out of
    credits". Fetching is free and the LLM gate is not; if the gate is unavailable the
    fetch is banked here and resume() judges it later, instead of the next run
    re-fetching and re-judging everything from page 0.
    """
    payload = [(vertical, r["domain"], json.dumps(_json_safe(r), default=str), run_id,
                source or r.get("_source") or "unknown")
               for r in rows if r.get("domain")]
    if not payload:
        return 0
    with cursor() as cur:
        cur.executemany(
            "INSERT INTO staged_companies (vertical, domain, payload, run_id, source) "
            " VALUES (%s,%s,%s,%s,%s) ON CONFLICT (vertical, domain) DO NOTHING", payload)
    return len(payload)


def record_verdicts(vertical: str, verdicts: dict[str, tuple[str, str]]) -> None:
    """Persist enforcement verdicts as they are given, so a halt mid-gate keeps them."""
    if not verdicts:
        return
    with cursor() as cur:
        cur.executemany(
            "UPDATE staged_companies SET verdict=%s, reason=%s WHERE vertical=%s AND domain=%s",
            [(d.upper(), r, vertical, dom) for dom, (d, r) in verdicts.items()])


def staged(vertical: str, judged: bool | None = None) -> list[dict]:
    """Staged companies for a vertical: unjudged (False), judged KEEP (True), or all."""
    # Consumed rows are companies already put into a batch; they are history, not
    # supply. Unconsumed KEEP/IDPR rows are the SURPLUS -- what a vertical kept beyond
    # what its batch needed -- and they are what the scale-up draws from first. Troy,
    # 2026-09-13: "store the rest so if they perform good we can top up."
    cond = " AND consumed_at IS NULL"
    if judged is False:
        cond += " AND verdict IS NULL"
    elif judged is True:
        cond += " AND verdict IN ('KEEP','IDPR')"
    with cursor() as cur:
        cur.execute(f"SELECT domain, payload, verdict FROM staged_companies "
                    f" WHERE vertical=%s{cond}", (vertical,))
        out = []
        for r in cur.fetchall():
            c = r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"])
            c["domain"] = r["domain"]
            if r["verdict"] == "IDPR":
                c["_tier"] = "idpr"
            out.append(c)
        return out


def mark_found(vertical: str, domains: Iterable[str]) -> int:
    """The finders have processed these companies; stage 4 resume skips them."""
    wanted = sorted({normalize_domain(d) for d in domains if d})
    if not wanted:
        return 0
    with cursor() as cur:
        cur.execute("UPDATE staged_companies SET found_at=now() "
                    " WHERE vertical=%s AND domain = ANY(%s) AND found_at IS NULL",
                    (vertical, wanted))
        return cur.rowcount


def unfound_kept(vertical: str) -> list[dict]:
    """Kept companies the finders have NOT processed yet, vertical-fit first.

    Stage 4 resumes from here. On 2026-09-13 a restart found 259 verified contacts
    banked from one finder slice and resumed medical device AT COPY, skipping the
    finders for the other ~2,200 companies -- "any verified contacts" was being read as
    "stage 4 finished". Companies carry a found_at once a finder slice has processed
    them, so the next run continues with the ones it never reached.
    """
    with cursor() as cur:
        cur.execute("SELECT domain, payload, verdict FROM staged_companies "
                    " WHERE vertical=%s AND verdict IN ('KEEP','IDPR') "
                    "   AND consumed_at IS NULL AND found_at IS NULL "
                    " ORDER BY (verdict='IDPR'), staged_at", (vertical,))
        out = []
        for r in cur.fetchall():
            c = r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"])
            c["domain"] = r["domain"]
            c["vertical"] = vertical
            c["_tier"] = "idpr" if r["verdict"] == "IDPR" else "vertical"
            out.append(c)
        return out


def clear_staged(vertical: str) -> None:
    """Drop UNJUDGED staging only. Judged KEEP/IDPR rows stay as surplus; DROPs stay
    so the same company is not re-fetched and re-judged next time."""
    with cursor() as cur:
        cur.execute("DELETE FROM staged_companies WHERE vertical=%s AND verdict IS NULL",
                    (vertical,))


def mark_consumed(vertical: str, domains: Iterable[str]) -> int:
    """The companies a batch actually used. What is left unconsumed is the surplus."""
    wanted = sorted({normalize_domain(d) for d in domains if d})
    if not wanted:
        return 0
    with cursor() as cur:
        cur.execute("UPDATE staged_companies SET consumed_at=now() "
                    " WHERE vertical=%s AND domain = ANY(%s) AND consumed_at IS NULL",
                    (vertical, wanted))
        return cur.rowcount


def surplus_count(vertical: str) -> dict[str, int]:
    with cursor() as cur:
        cur.execute("SELECT verdict, count(*) AS n FROM staged_companies "
                    " WHERE vertical=%s AND consumed_at IS NULL AND verdict IN ('KEEP','IDPR') "
                    " GROUP BY verdict", (vertical,))
        return {r["verdict"]: r["n"] for r in cur.fetchall()}


def paused_verticals(dead_after_hours: int = 1) -> list[dict]:
    """Backlog rows a stopped run left mid-flight, CLOSEST TO DONE first.

    Two kinds. 'paused_llm' rows were parked deliberately when the gateway was out.
    'running' rows whose run is no longer alive were abandoned by a crash -- a DNS
    blip, an out-of-memory kill, a redeploy -- and nothing else will ever pick them up,
    because take_from_backlog only claims 'queued'. Troy, 2026-09-13: "stops of any
    kind just resume". A run counts as dead when its row is not 'running' or it has
    logged nothing for dead_after_hours; those verticals are adopted here so resume()
    finishes them from their banked progress.
    """
    with cursor() as cur:
        cur.execute(
            """
            SELECT b.vertical, b.config, b.projected
              FROM vertical_backlog b
              LEFT JOIN runs r ON r.run_id = b.run_id
             WHERE b.status = 'paused_llm'
                OR (b.status = 'running'
                    AND (r.run_id IS NULL OR r.status <> 'running'
                         OR NOT EXISTS (SELECT 1 FROM logs l
                                         WHERE l.run_id = b.run_id
                                           AND l.ts > now() - make_interval(hours => %s))))
             ORDER BY
                     -- A vertical that already has a batch awaiting approval comes
                     -- first: it is half shipped, and topping it to the cap is what
                     -- makes it approvable. Troy, 2026-09-14: "the 3 pending batches
                     -- should run their copy first so we can quickly reach 3000."
                     (SELECT count(*) FROM batches bb
                       WHERE bb.vertical = b.vertical
                         AND bb.approval_status = 'pending') DESC,
                     -- then DISTANCE TO A COMPLETE BATCH, nearest first: the contacts
                     -- still to write before this vertical has a full one, which is
                     -- capped by what it can actually supply. Supply size decides what
                     -- to START; distance to shippable decides what to FINISH.
                     (SELECT greatest(
                               least(%s, count(*)) - count(*) FILTER (
                                   WHERE c.personalization_1 IS NOT NULL), 0)
                        FROM contacts c
                       WHERE c.vertical = b.vertical
                         AND c.status IN ('verified','draft','reserved','approved')
                     ) ASC, b.projected DESC NULLS LAST
            """, (dead_after_hours, settings.contact_cap_per_vertical))
        return cur.fetchall()


def set_vertical_thread(vertical: str, ts: str | None) -> None:
    if not ts:
        return
    with cursor() as cur:
        cur.execute("UPDATE vertical_backlog SET slack_ts=%s WHERE vertical=%s", (ts, vertical))


def vertical_thread(vertical: str) -> str | None:
    with cursor() as cur:
        cur.execute("SELECT slack_ts FROM vertical_backlog WHERE vertical=%s", (vertical,))
        r = cur.fetchone()
        return r["slack_ts"] if r else None


def batches_expiring(within_hours: int = 36) -> list[dict]:
    """Pending batches that expire soon and have not been warned about."""
    with cursor() as cur:
        cur.execute(
            """SELECT batch_id, vertical, contact_count, expires_at FROM batches
                WHERE approval_status='pending' AND expiry_warned_at IS NULL
                  AND expires_at < now() + make_interval(hours => %s)
                ORDER BY expires_at""", (within_hours,))
        return cur.fetchall()


def mark_expiry_warned(batch_id: str) -> None:
    with cursor() as cur:
        cur.execute("UPDATE batches SET expiry_warned_at=now() WHERE batch_id=%s", (batch_id,))


POOL_VERTICAL = "(pooled)"


def owned_verticals() -> set[str]:
    """Verticals THIS client actually owns, by its own records.

    Troy, 2026-09-23: "scale for idpr should only scale idpr campaigns."

    The contacts table is not proof of ownership. Two ProductEVO copy-test runs wrote
    1,028 contacts across 400 companies into IDPR's database on 2026-09-22 -- the
    clients have separate databases and those runs used the wrong one -- so IDPR's
    contacts table carries Consumer Electronics, Hardware & Building Materials and
    Sporting & Outdoor, none of which IDPR has ever proposed.

    Anything driven off contacts.vertical must be filtered through this, or work that
    finishes banked contacts would write IDPR copy for ProductEVO's prospects and push
    them into IDPR campaigns. Measured the same day: those three verticals appear in NO
    other IDPR table -- no backlog row, no campaign, no outcome -- so the three places
    this client records its own intent are an exact filter.
    """
    with cursor() as cur:
        cur.execute("""SELECT vertical FROM vertical_backlog
                        UNION SELECT vertical FROM ve_campaigns
                        UNION SELECT vertical FROM vertical_outcomes""")
        return {r["vertical"] for r in cur.fetchall() if r["vertical"]}


def banked_awaiting_copy(min_contacts: int = 1) -> list[dict]:
    """Verticals holding verified contacts that have no copy and no batch.

    These are already bought -- sourced, enriched, verified -- and worth only the LLM
    call that finishes them. Measured 2026-09-23: 3,843 of them across 11 verticals,
    left by runs that died mid-flight. They used to be picked up only as a side effect
    of a vertical winning a scale slot, so a PENDING vertical's banked work was never
    finished at all: Biotechnology sat on 255 with no route to a batch.

    Dropped verticals are excluded -- finishing their contacts would resurrect an
    audience someone killed -- and so is anything this client does not own.

    AND a vertical that is MID-JUDGEMENT is held back. Troy, 2026-09-23: "is banked work
    not supposed to run when above our scale rate?" It was not checking the rate at all,
    and the cost is not only spending copy on an audience that may not work. Finishing
    adds leads to the campaign, which drops its completion_percentage, which is the very
    gate it is waiting to clear: measured that day, Medical Device would have fallen from
    65.9% delivered to 46.9%, and Biotechnology -- ALREADY pending at 43.2% for exactly
    this reason -- would have been pushed further from the verdict it is waiting on.

    So: a WINNER is finished, because more of a proven audience is good. One never judged
    is finished, because that is completing the build it was sourced for, not expanding
    it. One sitting on a 'pending' verdict waits until that verdict lands.
    """
    owned = owned_verticals()
    with cursor() as cur:
        # BOTH places record a kill. A vertical judged on a live campaign gets a 'drop'
        # in vertical_outcomes; one abandoned before it ever launched is only marked
        # 'dropped' in the backlog and has no outcome row at all. DTC Beauty was the
        # second kind and slipped straight through an outcomes-only check with 60
        # banked contacts, which would have resurrected an audience someone killed.
        cur.execute("""SELECT c.vertical, count(*) AS n
                         FROM contacts c
                         LEFT JOIN vertical_outcomes o ON o.vertical = c.vertical
                         LEFT JOIN vertical_backlog b  ON b.vertical = c.vertical
                        WHERE c.status = 'verified'
                          AND c.personalization_1 IS NULL
                          AND c.vertical IS NOT NULL
                          AND COALESCE(o.verdict, '') NOT IN ('drop', 'pending')
                          AND COALESCE(b.status, '')  <> 'dropped'
                        GROUP BY c.vertical
                       HAVING count(*) >= %s
                        ORDER BY count(*) DESC""", (min_contacts,))
        return [dict(r) for r in cur.fetchall() if r["vertical"] in owned]


def scaling_overview() -> list[dict]:
    """One row per vertical the pipeline has launched: how far toward target, and why.

    Joined rather than derived per vertical so the page is one query. Pooled campaigns
    are reported under each vertical INSIDE them, not under the '(pooled)' label, which
    is a container and not an audience anyone would recognise.
    """
    with cursor() as cur:
        cur.execute(
            """SELECT v.vertical,
                      v.campaign_id,
                      v.pushed_lead_count                AS leads,
                      v.scale_exhausted_at,
                      v.scale_exhausted_at_leads,
                      COALESCE(o.verdict, '')            AS verdict,
                      COALESCE(o.sends, 0)               AS sends,
                      COALESCE(o.interested, 0)          AS interested,
                      COALESCE(o.rate_of_replies, 0)     AS rate,
                      o.reason,
                      COALESCE(o.pooled, FALSE)          AS pooled
                 FROM ve_campaigns v
                 LEFT JOIN vertical_outcomes o ON o.vertical = v.vertical
                WHERE v.vertical <> %s
                ORDER BY COALESCE(o.rate_of_replies, 0) DESC, v.pushed_lead_count DESC""",
            (POOL_VERTICAL,))
        rows = [dict(r) for r in cur.fetchall()]
        # Verticals living inside a pool have no ve_campaigns row of their own; their
        # outcome row is the only record of them.
        cur.execute(
            """SELECT o.vertical, NULL::bigint AS campaign_id, o.leads,
                      NULL::timestamptz AS scale_exhausted_at,
                      NULL::int AS scale_exhausted_at_leads,
                      o.verdict, o.sends, o.interested,
                      COALESCE(o.rate_of_replies, 0) AS rate, o.reason, o.pooled
                 FROM vertical_outcomes o
                WHERE o.pooled
                  AND NOT EXISTS (SELECT 1 FROM ve_campaigns v
                                   WHERE v.vertical = o.vertical)""")
        rows.extend(dict(r) for r in cur.fetchall())
        return rows


def vertical_config_for(vertical: str) -> dict | None:
    """The stored filter set for a vertical: its campaign's first, then the backlog's.

    The campaign's copy is the audience that was actually judged, which is the one a
    top-up should re-source against; the backlog's is what was proposed.
    """
    import json as _json
    with cursor() as cur:
        cur.execute("SELECT vertical_config FROM ve_campaigns "
                    " WHERE vertical=%s AND vertical_config IS NOT NULL "
                    " ORDER BY created_at DESC LIMIT 1", (vertical,))
        row = cur.fetchone()
        if not row:
            cur.execute("SELECT config AS vertical_config FROM vertical_backlog "
                        " WHERE vertical=%s", (vertical,))
            row = cur.fetchone()
    if not row:
        return None
    cfg = dict(row)["vertical_config"]
    return _json.loads(cfg) if isinstance(cfg, str) else cfg


def live_campaign_for_vertical(vertical: str) -> int | None:
    """This vertical's newest campaign, so banked work tops it up instead of rivalling it."""
    with cursor() as cur:
        cur.execute("SELECT campaign_id FROM ve_campaigns WHERE vertical=%s "
                    " ORDER BY created_at DESC LIMIT 1", (vertical,))
        row = cur.fetchone()
        return int(dict(row)["campaign_id"]) if row else None


def mark_scaling_started(campaign_id: int, run_id: str) -> None:
    with cursor() as cur:
        cur.execute("UPDATE ve_campaigns SET scaling_run_id=%s, scaling_started_at=now() "
                    " WHERE campaign_id=%s", (run_id, campaign_id))


def mark_scaling_finished(campaign_id: int) -> None:
    with cursor() as cur:
        cur.execute("UPDATE ve_campaigns SET scaling_run_id=NULL "
                    " WHERE campaign_id=%s", (campaign_id,))


def record_bison_sync(campaign_id: int, total_leads: int) -> None:
    """Stamp EmailBison's own reported total_leads for this campaign, once a day --
    see bison_total_leads's own comment for why this exists separately from
    pushed_lead_count."""
    with cursor() as cur:
        cur.execute("UPDATE ve_campaigns SET bison_total_leads=%s, bison_synced_at=now() "
                    " WHERE campaign_id=%s", (total_leads, campaign_id))


def record_poor_result(campaign_id: int) -> int:
    """One more consecutive night a vertical's own labels came back a poor match.
    Returns the new streak so the caller can compare it to POOR_RESULT_STREAK_LIMIT."""
    with cursor() as cur:
        cur.execute(
            """UPDATE ve_campaigns SET poor_result_streak = poor_result_streak + 1
               WHERE campaign_id=%s RETURNING poor_result_streak""", (campaign_id,))
        row = cur.fetchone()
        return row["poor_result_streak"] if row else 0


def reset_poor_streak(campaign_id: int) -> None:
    with cursor() as cur:
        cur.execute("UPDATE ve_campaigns SET poor_result_streak=0 WHERE campaign_id=%s",
                    (campaign_id,))


def mark_needs_human(campaign_id: int) -> None:
    with cursor() as cur:
        cur.execute("UPDATE ve_campaigns SET scale_needs_human_at=now() "
                    " WHERE campaign_id=%s", (campaign_id,))


def record_market_scan(campaign_id: int, scanned: int, available: int | None) -> None:
    """Cumulative, all-time record of how much of a vertical's real market has ever
    been looked at, for the live dashboard's "% explored" figure. `available` is kept
    as the LARGEST value ever reported for this campaign: the vendor's own count for a
    label can vary slightly call to call, and the biggest one seen is the best estimate
    of the true market size, never a truncated in-between read.
    """
    if not scanned and not available:
        return
    with cursor() as cur:
        cur.execute(
            """UPDATE ve_campaigns SET
                 market_scanned_total = market_scanned_total + %s,
                 market_available = GREATEST(COALESCE(market_available, 0), COALESCE(%s, 0))
               WHERE campaign_id=%s""",
            (scanned or 0, available, campaign_id))


def market_exploration() -> dict[str, dict]:
    """Every campaign's cumulative scan progress, keyed by vertical, for the live page."""
    with cursor() as cur:
        cur.execute(
            """SELECT vertical, market_scanned_total, market_available
                 FROM ve_campaigns WHERE market_available IS NOT NULL""")
        return {r["vertical"]: dict(r) for r in cur.fetchall()}


def interrupted_scale_ups() -> list[dict]:
    """VE campaigns whose scale-up started and never cleanly finished.

    A row here means scaling_run_id was stamped and the run it names is not 'running'
    any more -- it finished some other way without going through the finally that
    clears the stamp, which only happens when the container was killed. Troy,
    2026-09-24: "runs should also resume if they were failed right?"
    """
    with cursor() as cur:
        cur.execute(
            """SELECT v.* FROM ve_campaigns v
                 LEFT JOIN runs r ON r.run_id = v.scaling_run_id
                WHERE v.scaling_run_id IS NOT NULL
                  AND (r.run_id IS NULL OR r.status <> 'running')
                ORDER BY v.scaling_started_at""")
        return [dict(r) for r in cur.fetchall()]


def unscaled_winners(within_hours: int = 30) -> list[dict]:
    """VE campaigns whose vertical WON a review but never even started scaling --
    distinct from interrupted_scale_ups(), which finds work that started and died.
    scale_vertical only ever stamps scaling_run_id once it actually begins, so a
    winner that lost the queue -- the review's own 3-lanes-at-a-time limit, plus the
    process dying before a freed lane could be handed to the next one waiting -- never
    gets a stamp at all and is invisible to that check.

    Troy, 2026-09-25: Product Design won the 2026-09-24 review (5th of 5 winners) but
    the process crashed right as a lane should have opened for it, and it sat untouched
    for a full day waiting for the NEXT review to re-decide the same winner. within_hours
    bounds this to a recent verdict on purpose: an old 'scale' row a fresh review has
    since re-judged should be left to that review, not resurrected here.
    """
    with cursor() as cur:
        cur.execute(
            """SELECT v.* FROM ve_campaigns v
                 JOIN vertical_outcomes o ON o.vertical = v.vertical
                WHERE o.verdict = 'scale'
                  AND o.decided_at > now() - make_interval(hours => %s)
                  AND v.scaling_run_id IS NULL
                  AND v.scaling_started_at IS NULL
                  AND v.scale_exhausted_at IS NULL
                ORDER BY o.decided_at""", (within_hours,))
        return [dict(r) for r in cur.fetchall()]


def get_ve_campaign(campaign_id: int) -> dict | None:
    with cursor() as cur:
        cur.execute("SELECT * FROM ve_campaigns WHERE campaign_id = %s", (campaign_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def ve_campaign_for_vertical(vertical: str) -> dict | None:
    """The launched campaign for this vertical's name, if one exists.

    Troy, 2026-09-29, asking whether a manual --topup would still run on an ALREADY
    launched, active campaign: it would, and it should for a live winner topping up
    -- but it must not silently re-source one this app already closed for real
    (scale_exhausted_at). vertical_backlog keeps a row forever (finish_backlog only
    ever updates its status, never deletes it), so a vertical stays --topup-able by
    name long after it launches; this is what lets the caller check the OTHER table
    launched campaigns actually live in before deciding whether that is safe.
    """
    with cursor() as cur:
        cur.execute("SELECT * FROM ve_campaigns WHERE vertical = %s", (vertical,))
        row = cur.fetchone()
        return dict(row) if row else None


def pool_campaign_with_room(max_leads: int, need: int = 0) -> dict | None:
    """An open pooled campaign this batch can join, or None to start one.

    Troy, 2026-09-22: "it should be wise to loop campaigns where we cannot find up to
    3000 together." A small vertical cannot earn a verdict on its own -- scoring needs
    scale_min_sends (2,000) and at ~2.7 sends per lead that is ~740 leads, while
    Conversational AI shipped 237 and Funded Proptech 927. Left alone those campaigns
    hold a slot, their own sender accounts and a 600/day launch gate forever, and are
    never judged. Pooled, they share one campaign's senders and are scored per vertical
    from our own reply data instead of EmailBison's blended number.

    Only campaigns this pipeline marked as pools are offered, never a real vertical's
    campaign: mixing into a single vertical's campaign would corrupt the one number the
    scale/drop loop reads for it.
    """
    with cursor() as cur:
        cur.execute(
            # `+ need` leaves room for the batch about to join, so a pool cannot be
            # accepted at 2,999 and end up at 3,900.
            #
            # No filter on 'drop' outcomes here. The obvious version excluded any pool
            # whose campaign_id appears in a drop row -- but a pooled verdict names the
            # VERTICAL, several share the campaign, and a pooled drop is deliberately
            # never acted on. So the first weak vertical inside a pool would make the
            # whole still-live, still-sending, half-empty pool refuse new joins while
            # never being archived either, and the next batch would open a second pool:
            # burning exactly the sender slot pooling exists to save.
            """SELECT v.* FROM ve_campaigns v
                WHERE v.vertical = %s
                  AND v.pushed_lead_count + %s <= %s
                ORDER BY v.pushed_lead_count DESC
                LIMIT 1""",
            (POOL_VERTICAL, need, max_leads),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def verticals_in_campaign(campaign_id: int) -> list[dict]:
    """Per-vertical outcome inside ONE campaign, from our own data.

    EmailBison reports `interested` per CAMPAIGN only, so a pooled campaign's number is
    blended and useless for deciding which vertical inside it works. But stop-on-reply
    already records every replying company and whether it was interested, and every
    contact carries its vertical -- so the numerator is ours. Verified 2026-09-22: this
    join reproduces the per-campaign figures on the unpooled campaigns.

    Sends per vertical do not exist at any level EmailBison exposes, so the caller
    apportions the campaign's sends by each vertical's share of its leads. Every lead in
    one campaign shares one sequence and one schedule, which is what makes that fair.
    """
    with cursor() as cur:
        cur.execute(
            """SELECT ct.vertical,
                      count(DISTINCT ct.email)                                AS leads,
                      count(DISTINCT rc.company_domain)                       AS replied,
                      count(DISTINCT rc.company_domain)
                          FILTER (WHERE rc.interested)                        AS interested
                 FROM contacts ct
                 LEFT JOIN replied_companies rc
                        ON rc.company_domain = ct.company_domain
                       AND rc.campaign_id    = ct.eb_campaign_id
                WHERE ct.eb_campaign_id = %s
                  AND ct.vertical IS NOT NULL
                GROUP BY ct.vertical
                ORDER BY count(DISTINCT ct.email) DESC""",
            (campaign_id,),
        )
        return [dict(r) for r in cur.fetchall()]


def live_ve_campaign_count() -> int:
    """VE campaigns actually holding sender capacity right now.

    Counted from EmailBison, not from `ve_campaigns`. The first version counted rows
    and excluded only verticals with a 'drop' outcome, which is monotonically
    increasing: rows are never removed when a campaign is archived or finishes, a
    pooled row's vertical '(pooled)' never appears in vertical_outcomes at all, and
    copy-test arms count even though the review refuses to judge them. Once five
    campaigns had ever existed the step-up to 3 verticals could never fire again --
    which is the whole feature it gates (runner._intake_size).

    EmailBison's own status is the truth, and it is self-correcting: archive a campaign
    and the count drops on its own. senders.ACTIVE_STATUSES is the same set, because
    holding a sender slot is exactly what is being counted.
    """
    from .clients import emailbison
    from . import senders
    try:
        rows = emailbison.list_campaigns()
    except Exception:
        # Never let a vendor blip silently raise intake; the caller falls back to base.
        raise
    return sum(1 for c in rows
               if str(c.get("name") or "").startswith("VE-")
               and str(c.get("status") or "").lower() in senders.ACTIVE_STATUSES)


def take_from_backlog(limit: int, run_id: str) -> list[dict]:
    """Claim the highest-projected queued verticals for this run.

    Winners are scaled through Stage 8. A dropped audience can be retested only after
    the lookback window, when Stage 2 supplies a materially new hypothesis.
    """
    with cursor() as cur:
        cur.execute(
            "UPDATE vertical_backlog SET status='running', started_at=now(), run_id=%s "
            " WHERE vertical IN ("
            "   SELECT b.vertical FROM vertical_backlog b "
            # `AND o.pooled IS NOT TRUE`: a pooled verdict is an apportioned estimate,
            # never acted on and never confirmed by a human, so it must not bar the
            # vertical from being tested properly on its own campaign. Without this a
            # vertical that merely shared a pool is locked out for lookback_days on the
            # strength of a number nobody measured.
            "     LEFT JOIN vertical_outcomes o ON o.vertical = b.vertical "
            "                                  AND o.pooled IS NOT TRUE "
            "    WHERE b.status='queued' AND (o.vertical IS NULL OR "
            "          (o.verdict='drop' AND o.updated_at < now() - make_interval(days => %s))) "
            # Rank on the larger of the gate and the measurement, then break ties on
            # real supply. `projected` is floored to MIN_PROJECTED for BuzzLead-backed
            # verticals, so on its own it ranked five verticals identically at 2,000 and
            # the pick fell to whatever order the update happened to touch rows in.
            # density x fresh_rate is the honest tiebreak: contacts per company times
            # the share of that universe nobody has emailed yet.
            # VIABLE FOR A STANDALONE LAUNCH FIRST. Troy, 2026-09-30: a vertical the
            # probe measured at well under the 3,000 target (Applied AI & ML, ~140
            # reachable) took a Monday lane ahead of ones that could carry a campaign.
            # projected_raw is the honest measurement (projected is floored for the
            # gate); anything measured under the target ranks behind everything over
            # it, and only gets a lane when nothing viable is queued.
            "    ORDER BY (COALESCE(b.projected_raw, 0) >= %s) DESC, "
            "             GREATEST(b.projected, COALESCE(b.projected_raw, 0)) DESC, "
            "             (COALESCE(b.density,0) * COALESCE(b.fresh_rate,0)) DESC, "
            "             b.queued_at ASC "
            "    LIMIT %s) "
            # projected_raw comes back too: the runner re-sorts the claimed rows to
            # decide WORK order, and sorting on the floored `projected` there undoes
            # this ORDER BY by tying five verticals at 2,000 again. Work order is
            # what matters when a run dies mid-flight, which is often.
            " RETURNING vertical, config, projected, projected_raw",
            (run_id, settings.lookback_days, settings.target_contacts, limit),
        )
        return cur.fetchall()


def finish_backlog(vertical: str, status: str = "done") -> None:
    with cursor() as cur:
        cur.execute("UPDATE vertical_backlog SET status=%s WHERE vertical=%s",
                    (status, vertical))


def backlog_config(vertical: str) -> dict | None:
    """A vertical's own stored definition, regardless of its backlog status.

    Troy, 2026-09-29: a vertical whose batch already finished (status 'done') is
    otherwise unreachable by name -- the weekly walk only ever looks at 'queued'
    rows, moving forward to new verticals rather than revisiting one that came up
    short. This is what lets a manual top-up rebuild the same Vertical object the
    original run used, so _work_vertical's existing pending-batch top-up path
    (pending_batch_for/add_to_batch) can be re-entered for a NAMED vertical on
    demand instead of only through the normal backlog order.
    """
    with cursor() as cur:
        cur.execute("SELECT config FROM vertical_backlog WHERE vertical=%s", (vertical,))
        row = cur.fetchone()
    if not row:
        return None
    cfg = row["config"]
    return cfg if isinstance(cfg, dict) else json.loads(cfg)


def week_start_utc():
    """Monday 00:00 WAT of the current week, as a UTC datetime (WAT = UTC+1)."""
    from datetime import datetime, timedelta, timezone
    now_wat = datetime.now(timezone.utc) + timedelta(hours=1)
    monday_wat = (now_wat - timedelta(days=now_wat.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return monday_wat - timedelta(hours=1)


def occupied_test_seats() -> int:
    """Verticals launched but not yet judged -- holding a test seat right now.

    vertical_outcomes is upserted ON CONFLICT (vertical), one row per vertical, so a
    verdict of 'pending' means launched and under evaluation; 'scale' or 'drop' means
    it already left the seat (promoted to the winners pool, or retired) the moment it
    was first judged, even if it keeps getting re-judged daily afterward. A vertical
    still queued in the backlog with no campaign yet has no row at all and correctly
    does not count.

    pooled IS NOT TRUE, same exclusion take_from_backlog() already makes: a pooled
    verdict is an apportioned ESTIMATE of a shared pool campaign's performance, not a
    confirmed judgment on a dedicated campaign, and the vertical never held a reserved
    seat allocation to begin with -- it rides the pool's existing capacity. Troy,
    2026-10-06: 6 of 11 "pending" rows measured this way were pooled verticals that
    never actually spent a launch seat, overcounting occupied seats by more than half.
    """
    with cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM vertical_outcomes "
                    " WHERE verdict = 'pending' AND pooled IS NOT TRUE")
        return int(cur.fetchone()["n"])


def weekly_progress() -> dict:
    """Leads produced so far this week against the weekly quota.

    Troy, 2026-09-30: the pipeline is built around 15,000 leads a week (five 3,000
    launches plus scale-ups), and nothing measured it -- a short Monday only became
    visible the following Monday. A lead counts when it first reaches a live status
    (verified onward) this week; killed and needs_review do not count.
    """
    from datetime import datetime, timezone
    start = week_start_utc()
    with cursor() as cur:
        cur.execute(
            """SELECT count(*) AS n FROM contacts
                WHERE first_seen_at >= %s
                  AND status IN ('verified','draft','reserved','approved','pushed')""",
            (start,))
        n = int(cur.fetchone()["n"] or 0)
    elapsed_days = max((datetime.now(timezone.utc) - start).total_seconds() / 86400, 0.01)
    return {"leads": n, "quota": settings.weekly_lead_quota, "week_start": start,
            "elapsed_days": elapsed_days,
            "on_pace_for": int(n / min(elapsed_days, 7) * 7),
            "pace_quota": int(settings.weekly_lead_quota * min(elapsed_days, 7) / 7)}


def paused_for_human() -> list[dict]:
    """Every vertical currently parked waiting on a human decision, whichever of the
    three relief valves caught it: a scale-up's poor-match streak (scale_needs_human_at),
    a top-up's five-empty-attempts streak (topup_needs_human_at), or a scale-up declared
    permanently exhausted (scale_exhausted_at). Used by `/ve brief` to name the single
    most actionable thing a human can unblock right now.

    scale_exhausted_at was silent beyond its one Slack post on the day it triggered --
    Medical Device sat re-judged a SCALE winner every day for 8+ days, refused by
    scale_vertical() every single time with no log line and no repeat alert, while its
    real pushed_lead_count kept climbing past the leads figure the exhaustion verdict
    was stamped with -- proof the verdict was already stale and nobody was told twice.
    """
    with cursor() as cur:
        cur.execute(
            """SELECT vertical, 'scale' AS kind, scale_needs_human_at AS since
                 FROM ve_campaigns WHERE scale_needs_human_at IS NOT NULL
               UNION ALL
               SELECT vertical, 'topup' AS kind, topup_needs_human_at AS since
                 FROM vertical_backlog WHERE topup_needs_human_at IS NOT NULL
               UNION ALL
               SELECT vertical, 'exhausted' AS kind, scale_exhausted_at AS since
                 FROM ve_campaigns WHERE scale_exhausted_at IS NOT NULL
               ORDER BY since""")
        return [dict(r) for r in cur.fetchall()]


def short_pending_batches(cap: int) -> list[dict]:
    """Pending batches under the per-vertical cap, not already paused for a human.

    PRE-LAUNCH half of the automatic top-up loop. Troy, 2026-09-29: the exact
    Monday-vendor-outage gap that landed 3 batches short of 3,000 -- nothing
    previously retried a vertical whose batch already exists; the weekly walk only
    ever moves forward to new ones.
    """
    with cursor() as cur:
        cur.execute(
            """SELECT b.batch_id, b.vertical, b.contact_count,
                      coalesce(v.topup_streak, 0) AS topup_streak
                 FROM batches b
                 LEFT JOIN vertical_backlog v ON v.vertical = b.vertical
                WHERE b.approval_status = 'pending'
                  AND b.contact_count < %s
                  AND (v.topup_needs_human_at IS NULL)
                ORDER BY b.contact_count ASC""",
            (cap,))
        return [dict(r) for r in cur.fetchall()]


def short_launched_campaigns(floor: int) -> list[dict]:
    """Launched campaigns under the floor, not yet a recognized scale winner, not
    already paused for a human.

    POST-LAUNCH half. A campaign this thin can never clear scale_min_sends on its
    own and nothing tops it up once it has launched -- scale_vertical() only starts
    AFTER a 'scale' verdict, and a verdict needs sends this campaign does not have
    yet. Excludes anything already recognized as a scale winner (scaling_run_id set,
    or scale_exhausted_at set -- it was one and finished, win or lose) so this never
    fights the real scale-up path over the same campaign.
    """
    with cursor() as cur:
        cur.execute(
            """SELECT c.campaign_id, c.vertical, c.pushed_lead_count,
                      coalesce(v.topup_streak, 0) AS topup_streak
                 FROM ve_campaigns c
                 LEFT JOIN vertical_backlog v ON v.vertical = c.vertical
                 LEFT JOIN vertical_outcomes o ON o.vertical = c.vertical
                WHERE c.vertical <> %s
                  AND c.pushed_lead_count > 0
                  AND c.pushed_lead_count < %s
                  AND c.scaling_run_id IS NULL
                  AND c.scale_exhausted_at IS NULL
                  AND c.scale_needs_human_at IS NULL
                  AND (v.topup_needs_human_at IS NULL)
                  -- a DROPPED campaign is done; topping it up is paid work for
                  -- nothing (2026-09-30: Funded Proptech, dropped, got a batch)
                  AND (o.verdict IS NULL OR o.verdict <> 'drop')
                ORDER BY c.pushed_lead_count ASC""",
            (POOL_VERTICAL, floor))
        own = [dict(r) for r in cur.fetchall()]

        # POOLED VERTICALS. A pool is ONE ve_campaigns row for the whole campaign,
        # tagged POOL_VERTICAL, so the verticals inside it have no row of their own and
        # were invisible here -- a vertical pushed into a pool at 499 leads sat at 499,
        # ~1,350 sends, under the 2,000 it takes to ever be judged, and nothing topped
        # it up. Troy, 2026-09-30: "would our pooled verticals still top up when
        # launched." Their pushed count comes from the contacts themselves.
        cur.execute(
            """SELECT ct.vertical,
                      ct.eb_campaign_id AS campaign_id,
                      count(*) AS pushed_lead_count,
                      coalesce(max(v.topup_streak), 0) AS topup_streak
                 FROM contacts ct
                 JOIN ve_campaigns p ON p.campaign_id = ct.eb_campaign_id
                                    AND p.vertical = %s
                 LEFT JOIN vertical_backlog v ON v.vertical = ct.vertical
                 LEFT JOIN vertical_outcomes o ON o.vertical = ct.vertical
                WHERE ct.status = 'pushed' AND ct.vertical IS NOT NULL
                  AND (v.topup_needs_human_at IS NULL)
                  AND (o.verdict IS NULL OR o.verdict <> 'drop')
                GROUP BY ct.vertical, ct.eb_campaign_id
               HAVING count(*) < %s
                ORDER BY count(*) ASC""",
            (POOL_VERTICAL, floor))
        pooled = [dict(r, pooled=True) for r in cur.fetchall()]
    return own + pooled


def pool_for_vertical(vertical: str) -> int | None:
    """The pool campaign this vertical's pushed contacts already live in, if any.

    A top-up batch for a pooled vertical must rejoin the SAME pool: s7's stickiness
    only reads the batch's own contacts' eb_campaign_id, and a fresh top-up batch has
    none, so it would join whichever pool happened to be fullest-with-room and split
    one vertical's leads across two campaigns.
    """
    with cursor() as cur:
        cur.execute(
            """SELECT ct.eb_campaign_id AS campaign_id, count(*) AS n
                 FROM contacts ct
                 JOIN ve_campaigns p ON p.campaign_id = ct.eb_campaign_id
                                    AND p.vertical = %s
                WHERE ct.vertical = %s AND ct.status = 'pushed'
                GROUP BY ct.eb_campaign_id ORDER BY n DESC LIMIT 1""",
            (POOL_VERTICAL, vertical))
        row = cur.fetchone()
    return int(row["campaign_id"]) if row else None


def record_topup_streak(vertical: str, added: int) -> int:
    """One more automatic top-up attempt for this vertical. Any real growth resets
    the streak to 0; a genuine zero (not just 'vendor still down' -- the caller only
    calls this after an attempt actually completed) increments it. Returns the new
    streak so the caller can compare it to AUTO_TOPUP_STREAK_LIMIT."""
    with cursor() as cur:
        cur.execute(
            """UPDATE vertical_backlog
                  SET topup_streak = CASE WHEN %s > 0 THEN 0
                                          ELSE topup_streak + 1 END
                WHERE vertical = %s
               RETURNING topup_streak""",
            (added, vertical))
        row = cur.fetchone()
        return row["topup_streak"] if row else 0


def pause_topup_for_human(vertical: str) -> None:
    with cursor() as cur:
        cur.execute(
            "UPDATE vertical_backlog SET topup_needs_human_at = now() "
            " WHERE vertical = %s AND topup_needs_human_at IS NULL",
            (vertical,))


def mark_found_with_misses(vertical: str, found: dict[str, tuple[list[str], int]]) -> int:
    """found_at plus what the finders could NOT ask this company, per domain.

    found: {domain: (missed_vendors, seats_filled)}. A company with a missed vendor
    and empty seats is what reopen_after_recovery() looks for once that vendor is
    back; one with every seat filled is done regardless.
    """
    n = 0
    with cursor() as cur:
        for domain, (missed, filled) in found.items():
            cur.execute(
                """UPDATE staged_companies
                      SET found_at = now(), missed_vendors = %s, seats_filled = %s
                    WHERE vertical = %s AND domain = %s AND found_at IS NULL""",
                (missed or None, filled, vertical, normalize_domain(domain)))
            n += cur.rowcount
    return n


def reopen_after_recovery(vendor: str, seats: int, limit: int = 5000) -> int:
    """Put companies that missed `vendor` back in front of the finders, now that it
    is up -- only those with a seat still empty, only this vendor recorded as missed,
    and only ones not yet consumed into a batch. Clearing found_at is what makes
    unfound_kept() (the top-up's own surplus pool) pick them up again; missed_vendors
    is trimmed so a recovered vendor is not re-opened for twice."""
    with cursor() as cur:
        cur.execute(
            """UPDATE staged_companies
                  SET found_at = NULL,
                      missed_vendors = array_remove(missed_vendors, %s)
                WHERE (vertical, domain) IN (
                      SELECT vertical, domain FROM staged_companies
                       WHERE %s = ANY(missed_vendors)
                         AND found_at IS NOT NULL AND consumed_at IS NULL
                         AND verdict IN ('KEEP','IDPR')
                         AND coalesce(seats_filled, 0) < %s
                       LIMIT %s)""",
            (vendor, vendor, seats, limit))
        return cur.rowcount


def batches_expiring_soon(hours: int = 24) -> list[dict]:
    """Pending batches inside `hours` of expiry that have not been warned about."""
    with cursor() as cur:
        cur.execute(
            """SELECT batch_id, vertical, contact_count, expires_at FROM batches
                WHERE approval_status = 'pending' AND expiry_warned_at IS NULL
                  AND expires_at < now() + make_interval(hours => %s)""", (hours,))
        return [dict(r) for r in cur.fetchall()]


def mark_expiry_warned(batch_id: str) -> None:
    with cursor() as cur:
        cur.execute("UPDATE batches SET expiry_warned_at = now() WHERE batch_id = %s",
                    (batch_id,))


def vertical_is_launched(vertical: str) -> int | None:
    """The campaign this vertical already sends from -- its own, or the pool it lives
    in -- or None if it has never been pushed. This is the test for "is a new batch a
    top-up of already-approved copy, or a first launch that needs a human"."""
    camp = ve_campaign_for_vertical(vertical)
    if camp and int(camp.get("pushed_lead_count") or 0) > 0:
        return int(camp["campaign_id"])
    return pool_for_vertical(vertical)


def unpushed_supply(vertical: str) -> int:
    """Everything this vertical holds that has not been sent yet: contacts in its
    pending batch (reserved/approved), verified ones awaiting copy, and finished
    drafts no batch has picked up. This -- not the size of one batch -- is what says
    whether a vertical can stand alone.

    Troy, 2026-10-01, on Biotechnology Research landing in a pool: its first batch
    was a 123-contact snapshot taken while sourcing was still running and 2,117 more
    sat banked; s7 read "123 < 1,000" and pooled it, and the sticky rule then sent the
    1,819-contact batch after it into the same pool.
    """
    with cursor() as cur:
        cur.execute(
            """SELECT count(*) AS n FROM contacts
                WHERE vertical = %s
                  AND status IN ('verified','draft','reserved','approved')""",
            (vertical,))
        return int(cur.fetchone()["n"] or 0)


def mark_standalone(vertical: str) -> None:
    """Stamp the backlog config so s7 never pools this vertical again."""
    with cursor() as cur:
        cur.execute(
            """UPDATE vertical_backlog
                  SET config = COALESCE(config, '{}'::jsonb) || '{"standalone": true}'::jsonb
                WHERE vertical = %s""", (vertical,))


def vendor_refusals_since(hours: int = 2) -> dict[str, int]:
    with cursor() as cur:
        cur.execute(
            """SELECT vendor, count(*) AS n FROM vendor_usage
                WHERE status IN ('401','402') AND ts >= now() - make_interval(hours => %s)
                GROUP BY vendor""", (hours,))
        return {r["vendor"]: int(r["n"]) for r in cur.fetchall()}


def recently_logged(stage: str, needle: str, hours: int = 24) -> bool:
    with cursor() as cur:
        cur.execute(
            """SELECT 1 FROM logs WHERE stage = %s AND message ILIKE %s
                  AND ts >= now() - make_interval(hours => %s) LIMIT 1""",
            (stage, f"%{needle}%", hours))
        return cur.fetchone() is not None


def vertical_outcome(vertical: str) -> dict | None:
    """This vertical's own recorded verdict (scale | drop | pending), or None if it
    has never been judged. Used to refuse pushing more leads into a DROPPED
    vertical's archived campaign (Troy, 2026-09-30, Funded Proptech)."""
    with cursor() as cur:
        cur.execute("SELECT * FROM vertical_outcomes WHERE vertical = %s", (vertical,))
        row = cur.fetchone()
    return dict(row) if row else None


def requeue_pooled_winner(vertical: str) -> bool:
    """A vertical that WON inside a pool gets its own campaign next: back to the
    backlog as queued, ranked to be taken first, with `standalone` set so s7 never
    pools it again. Troy, 2026-09-30, choosing this over "needs a human"."""
    with cursor() as cur:
        cur.execute(
            """UPDATE vertical_backlog
                  SET status = 'queued', run_id = NULL, started_at = NULL,
                      projected = GREATEST(projected, 100000),
                      projected_raw = GREATEST(coalesce(projected_raw, 0), 100000),
                      config = config || '{"standalone": true}'::jsonb
                WHERE vertical = %s""", (vertical,))
        return cur.rowcount > 0


def record_vendor_usage(vendor: str, kind: str, units: int = 1, status: str | None = "ok",
                       run_id: str | None = None, vertical: str | None = None) -> None:
    """Never lets a ledger write take a run down."""
    try:
        with cursor() as cur:
            cur.execute(
                "INSERT INTO vendor_usage (vendor, kind, units, status, run_id, vertical) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (vendor, kind, int(units), status, run_id, vertical))
    except Exception:
        pass


def vendor_usage_since(since) -> dict[str, dict[str, int]]:
    """{vendor: {kind: units}} since a timestamp, plus a '_402' error count per vendor."""
    with cursor() as cur:
        cur.execute(
            """SELECT vendor, kind, sum(units) AS units,
                      count(*) FILTER (WHERE status LIKE '4%%') AS errors
                 FROM vendor_usage WHERE ts >= %s GROUP BY vendor, kind""",
            (since,))
        out: dict[str, dict[str, int]] = {}
        for r in cur.fetchall():
            out.setdefault(r["vendor"], {})[r["kind"]] = int(r["units"] or 0)
            out[r["vendor"]]["_errors"] = out[r["vendor"]].get("_errors", 0) + int(r["errors"] or 0)
    return out


def backlog_depth() -> int:
    with cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM vertical_backlog b "
            "  LEFT JOIN vertical_outcomes o ON o.vertical = b.vertical "
            " WHERE b.status='queued' AND (o.vertical IS NULL OR "
            "       (o.verdict='drop' AND o.updated_at < now() - make_interval(days => %s)))",
            (settings.lookback_days,))
        return cur.fetchone()["n"]
