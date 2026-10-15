# Copy-test template (reference build: Lumion, proven 2026-10-09 to 2026-10-15)

Use this to stand up the ProductEVO-style copy test for a new client. Reference implementation is this repo. Scaffold command:

    python scripts/new_client.py <name> --workspace <id> --base https://personal.buzzlead.io --key-env <ENV_NAME> --model-campaign <id>

Never edit `idpr-vertical-expander/` (IDPR and ProductEVO live there). Never print a key. Estimate and get a go-ahead before anything that spends.

## 0. Inputs to collect first
EmailBison workspace id and host (a key scopes the workspace; test it on both hosts), key variable name in the root `.env`, model campaign id (steps 1 and 2 carry
`PERSONALIZATION 1` and `PERSONALIZATION 2`, check step 3's merge fields), the angles (one per campaign), ICP and misfit rules, titles (declared, best first), pitch and proof
points (approved and forbidden), copy length windows, the client's hard rules, Slack channel.

## 1. Build
1. Scaffold (above). Fill `clients/<name>/TODO.md` top to bottom. `copy_test.toml [fill] target = 15` for the proof run.
2. Prompts: nine files, `scripts/gen_lumion_prompts.py` style. Model lines: research `openai/gpt-oss-120b`, emails `anthropic/claude-haiku-4.5`.
   `copy_rules.toml` bans and required patterns are enforced in code, not just prompts. One angle per campaign.
3. Railway: own project, `railway add -d postgres`, one `<name>-web` service, `DATABASE_URL=${{Postgres.DATABASE_URL}}`, variables through `scripts/set_railway_vars.py`
   (check no value has a trailing space). Safe defaults: `PIPELINE_ENABLED=false`, `AUTO_START_CAMPAIGNS=false`, `SKIP_SENDER_ATTACH=true`, `POOL_REFILL_ENABLED=false`.
   Container filesystem resets on every deploy: `mkdir -p /app/logs` before writing logs. Redeploy kills a run in flight.
4. Slack app from a manifest with a unique slash command, bot invited to the channel, signed test POST.
5. History: `collect_*_history.py` (send log, newest first, paginate campaigns) then `build_*_history.py`, then `funnel_dedupe.py`, `history_backfill2.py`,
   `add_history_to_funnel.py`, `tier_counts.py`. This fills `history_leads`, `history_replies`, `recent_contacts` and the tier table `company_funnel`.
6. Campaigns: duplicate the model campaign once per angle, register in `copy_test_campaigns`, keep as DRAFT, no senders. Proof run 15 leads each, read the sample with
   the client, then raise the target and switch on.

## 2. How the fill picks people (app/wave1_fill.py)
Order: saved copy that still passes, then never-contacted bank contacts (including schools with no tier row), then reuse people on THEIR OWN 60 day clock
(`recent_contacts` campaign date). No fit check and no title drop on reuse; titles only rank the 5 seats per company. Kept on purpose: company-level reply suppression,
bounced people, "Company team" placeholder first names, people already in `contacts`, personal-email domains. No re-verifier (`REVERIFY_EMAILS=true` restores it; keep it
on for any client whose rules need it). EmailBison refuses leads already in another active sequence: they go to needs_review, which is correct.

## 3. LLM waterfall (app/clients/llm.py), ProductEVO commit 8eb7c65
copy: buzzlead, concentrate, openrouter. research: buzzlead, openrouter, concentrate (Concentrate swaps gpt-oss for Haiku). precopy and fit: anthropic first.
`BUZZLEAD_API_KEY` must be on the service. `_llm_ready()` sends one real-size call through the chain before each batch and pauses only if nobody answers.
Boot releases a dead fill lock so a redeploy never stalls for 90 minutes. Resume runs at every boot and every 2 hours.

## 4. Traps (each cost time)
Python heredoc `\n` and `\b` corrupt code: use Write/Edit. A `#` comment mid-line swallows the rest of that line. A tiny test call passes on an empty balance: test real size.
Staged contacts with no tier row were invisible to the fill (fixed with a LEFT JOIN). The company clock hid people whose coworker was emailed recently (person clock now).
Contact finding on new schools yields about 9 percent per company with QuickEnrich and Lead Ocean alone: sourcing companies is not the constraint, contacts are.
Hunter and TryKitt credits gate email resolution for the web-research people finder (`app/clients/people_agent.py`, 62 percent names, emails blocked).
No detach endpoint in EmailBison: a wrong lead is permanent, so fit-check new companies. Sender attachment is a separate decision.

## 5. Per-client hard rules go in copy_rules.toml and preflight
Example (TFP): fit-check every company first, never push Mimecast or Barracuda (MX based), COMPANY at most 3 words, MATERIALS_HAULED at most 5 words, forbidden proof point
INEOS Composites. These override any shortcut above.
