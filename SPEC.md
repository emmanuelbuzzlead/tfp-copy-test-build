# SPEC: Vertical Expander Pipeline
Railway-hosted, scheduled, self-running with one human approval gate.
Owner: Troy (BuzzLead). Client: IDPR. Hand this file directly to Claude Code.

---

## 1. OBJECTIVE

Every run: pull last 90 days of EmailBison campaign performance, identify winning
campaigns, expand their verticals into adjacent verticals, build company + people
lists, generate personalized 2-email copy using the ported Clay workflow, and stage
a batch into EmailBison. Nothing sends without Slack approval.

## 2. HARD RULES (do not soften these)

- Metric: **positive reply rate** only. Not raw reply rate.
- Sample gate: campaign must have **>= 1,000 leads** to qualify as a winner. No exceptions.
- Lookback: rolling 90 days.
- Dedup: no company or contact that exists in the history store may re-enter. Check BEFORE enrichment (saves credits).
- Approval gate: no EmailBison push without explicit Slack approval.
- Kill switch: env var `PIPELINE_ENABLED=false` halts everything at next tick.
- ICP lock: US-based, funded (Series A+), 11-500 employees. Titles: CEO/Founder if <50 employees, CMO/VP Marketing if >=50.
- Hard exclusions: MSPs, VARs, IT consulting/services, government contractors, nonprofits, staffing firms, non-US, pre-seed, MLM/direct-sales structures.
- Law firms are IN scope. Do not exclude.

## 3. ARCHITECTURE (Railway)

```
[Cron trigger (weekly)]
        |
  [Stage 1: Performance Pull]  -- EmailBison API, workspace 10/ws34
        |
  [Stage 2: Vertical Expansion] -- OpenRouter LLM
        |
  [Stage 3: Company Finder]     -- AI Ark + LLM enforcement pass
        |
  [Stage 4: People + Emails]    -- Quick Enrich -> Hunter
        |
  [Stage 5: Research + Copy]    -- Serper + ported Clay prompts
        |
  [Stage 6: Slack Approval]     -- approve / kill per batch
        |
  [Stage 7: EmailBison Push]
```

Services:
- **worker** (Python): runs stages sequentially per run.
- **Postgres** (Railway plugin): state store, dedup history, run logs.
- **Slack app**: incoming webhook for batch summary + interactive approve/kill buttons (or slash-command fallback).

Env vars: `EMAILBISON_API_KEY`, `AIARK_API_KEY`, `QUICKENRICH_API_KEY`, `HUNTER_API_KEY`, `SERPER_API_KEY`, `OPENROUTER_API_KEY`, `SLACK_WEBHOOK_URL`, `PIPELINE_ENABLED`, `WORKSPACE_ID`.

## 4. STAGE DETAIL

### Stage 1 — Performance Pull
- Pull all campaigns, workspace 10/ws34, active in last 90 days.
- Per campaign: total leads, sends, replies, positive/interested replies.
- Filter: leads >= 1000.
- Rank by positive reply rate. Top N (default 3) = winners.
- **GATE (unresolved):** confirm EmailBison API exposes reply sentiment/interested
  category per campaign. If not, fallback = dashboard CSV export ingestion
  (known reliable workaround for campaign-scoped pulls; `list_leads` does NOT
  support campaign_id filtering).
- **GATE (unresolved):** live stats via MCP/API may lag. Add a `stats_verified`
  flag; if numbers look off vs. thresholds, pause and flag in Slack instead of
  proceeding.

### Stage 2 — Vertical Expansion (LLM, OpenRouter)
- Input: winner campaigns + their vertical labels + full ICP rules + exclusion list
  + known vertical performance history (healthcare top; SaaS + Funded Brands strong;
  CPG weak).
- Task: propose 1-3 adjacent verticals. Adjacency = similar BUYER BEHAVIOR
  (funded, PR-hungry, founder-led, credibility-dependent), not similar industry names.
- Output per vertical: name, rationale, AI Ark industry array, AI Ark keyword array,
  employeeSize, funding floor, disqualifier list for the enforcement pass.
- Skip any proposed vertical already run in the last 90 days.

### Stage 3 — Company Finder
- AI Ark Company Search (`POST /v1/companies`) with Stage 2 filters.
  Base: `location.country=["United States"]`, employeeSize + funding from vertical config.
- AI Ark industry/keyword filters are OR-logic. Therefore:
- **Enforcement pass (replaces Claygent, since Clay is out of the loop):** LLM call
  per company (batched) applying AND logic: actually in vertical? US? funded?
  not on exclusion list? not MLM/direct-sales? Output KEEP/DROP + one-line reason.
  Log drops.
- Dedup against history store. Drop known domains.
- Cap: default 500 companies per vertical per run (env-tunable).

### Stage 4 — People + Emails
- Quick Enrich: find people at kept companies using the IDPR title inclusion list:

  Founder, Co-Founder, CEO, President, Vice President, Managing Director, Owner,
  CMO, Chief Marketing Officer, VP of Marketing, VP Marketing, Vice President of
  Marketing, Head of Marketing, Director of Marketing, Senior Marketing Manager,
  Brand Director, Brand Marketing Manager, Product Marketing Director,
  Chief Communications Officer, VP of Communications, Head of Communications,
  Director of Communications, Corporate Communications Manager,
  Public Relations Manager, Media Relations Manager,
  Strategic Communications Director, Head of Brand Communications,
  Brand & Communications Director, Marketing Communications Manager,
  VP of Public Relations, Public Relations Director, Head of Public Relations,
  VP, Director of Digital, VP of Digital, Head of Digital

- Contact selection priority when multiple people match at one company:
  <50 employees: CEO/Founder/Owner first. >=50: CMO/VP Marketing/Head of
  Marketing first, then comms/PR titles, then generic exec titles.
- Bare "VP" / "President" / "Vice President" will match non-marketing departments
  (Sales, Engineering, Finance). Keep them in the search, but the selection step
  must prefer marketing/comms/exec matches and only fall back to bare titles when
  nothing better exists at the company. Never select VP of Sales/Engineering/
  Finance-type titles even as fallback.
- **GATE: Quick Enrich client wiring. API key exists in Claude Code env. Claude
  Code resolves the integration from any ONE of: (a) existing QE calls already in
  the repo, (b) public/fetchable QE docs, (c) an example request from Troy. If
  none of the three exist, ask Troy for a sample curl before proceeding.**
- Hunter: find + verify email per person. Drop anything below Hunter's
  "deliverable" confidence. Never push risky/unverified.
- Dedup contacts against history store (email + LinkedIn URL).

### Stage 5 — Research + Copy (ported Clay workflow)
Per contact:
1. **Serper call 1:** Google News, company name, last 10 results.
2. **Serper call 2:** Google Search, "{company name} competitors", top 10.
3. **Website:** fetch homepage + /about + /press or /news, strip to text.
4. **Profile data:** company + person profile fields from AI Ark / Quick Enrich
   substitute for the Clay LinkedIn scrapes (no direct LinkedIn scraping off-Clay).
5. **Research prompt:** `openai/gpt-oss-120b`, temp 0.3, system prompt = Clay
   research prompt verbatim (Prospect Research Report format). Inputs 1-4 mapped
   into its user-message slots.
6. **Email 1 prompt:** `anthropic/claude-haiku-4.5`, temp 0.5, system prompt =
   Clay Email 1 prompt verbatim. max_tokens raised 150 -> 300 (150 risks
   mid-sentence truncation at the 75-word target; validator still enforces length).
7. **Email 2 prompt:** `anthropic/claude-haiku-4.5`, temp 0.4, system prompt =
   Clay Email 2 (follow-up) prompt verbatim. Inputs: Email 1 output + research
   report + IDPR overview.
8. **IDPR overview:** static content block baked into the repo
   (`assets/idpr_overview.md`), sourced from existing project docs. Not fetched live.
9. **Output contract:** strict JSON
   `{"PERSONALIZATION 1": "<email 1 body>", "PERSONALIZATION 2": "<email 2 body>"}`
   mapping to EmailBison merge variables.
10. **Validator (post-generation, auto-regenerate on violation, max 2 retries):**
    - Email 1 <= 500 chars; Email 2 <= 800 chars
    - No em dashes, no greetings/names/sign-offs, no markdown/rich text
    - No hype words (amazing, game-changing, revolutionary, innovative, excited,
      leverage, synergy, strategic, solutions)
    - No "congrats on the raise" openers
    - Spam-word check (reuse spam/spintax QA chain rules)
    - Research report empty or all-blank -> DROP contact, do not send generic copy
11. Failed twice -> contact goes to `needs_review` bucket, excluded from batch.

### Stage 6 — Slack Approval
- Post per-batch summary: vertical name + rationale, company count, contact count,
  drop stats from enforcement pass, 3 sample email pairs, dedup stats.
- Buttons: **Approve** / **Kill**. No response in 72h = auto-kill, logged.
- Killed batches keep their dedup entries OUT of history (so those companies can
  be re-found later if targeting changes).

### Stage 7 — EmailBison Push
- On approve: create/load into designated campaign, workspace 10/ws34, with
  PERSONALIZATION 1/2 as merge variables.
- **GATE (unresolved): bulk-load endpoint not confirmed (existing known blocker).**
  Fallback: generate upload-ready CSV, deliver via Slack for manual load.
- Write all pushed companies + contacts to history store with run_id, vertical,
  campaign_id, timestamp.
- **GATE (unresolved): workspace ID verification (523 vs 655 question) before any
  live send.**

## 5. STATE STORE (Postgres)

- `runs` (run_id, started, status, winners json, verticals json)
- `companies` (domain PK, name, vertical, run_id, status: pushed/dropped/killed)
- `contacts` (email PK, linkedin_url, company_domain, title, run_id, status)
- `batches` (batch_id, run_id, vertical, slack_ts, approval_status, approved_by, ts)
- `logs` (run_id, stage, level, message, ts)
- Backfill task: import historical sends (CSV loads) into `companies`/`contacts`
  so dedup covers pre-pipeline history. (Existing known gap; this closes it.)

## 6. LAUNCH GATES (all must clear before first live run)

1. Quick Enrich client wired and verified with one successful test call.
2. EmailBison positive-reply metric confirmed via API, or CSV fallback wired.
3. EmailBison bulk-load endpoint confirmed, or CSV handoff accepted.
4. Workspace ID verified.
5. History backfill loaded (dedup has teeth).
6. Dry run end-to-end with push disabled; Troy reviews full output.

## 7. COST CONTROLS

- Dedup before enrichment (Quick Enrich/Hunter credits only spent on new contacts).
- Company cap per vertical per run.
- AI Ark export credits only after enforcement pass KEEPs.
- Log per-run credit usage estimates to Slack summary.
