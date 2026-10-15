# Deploying the Lumion expander

Standalone build: its own Railway project, its own Postgres, its own Slack app. It shares nothing with the IDPR,
ProductEVO, ProQ or MasterNow deployments. Do not link this folder to any other project.

| Thing | Lumion |
|---|---|
| Railway project | lumion-vertical-expander (9f364913-12dd-4c59-a128-332b6c00fc1b), environment production |
| Web service | lumion-web, https://lumion-web-production.up.railway.app (live 2026-10-09, health 200, schema ok) |
| Database | lumion (24 tables; the default database holds none) |
| Slack app | Vertical Expander (Lumion), command /lumion, manifest in slack/lumion-app-manifest.yaml |
| Channel | #lumion-expander (to confirm) |
| EmailBison | workspace 37, https://personal.buzzlead.io, key variable LUMION_EMAIL_BISON_API_KEY |

## Created 2026-10-09
Project, Postgres, the lumion database, lumion-web and its domain exist, and 23 variables are set with no stray whitespace.
`python scripts/set_railway_vars.py` re-sets them from the root .env without printing values; add `--slack` once the Slack
secrets are in .env. Safe defaults are set: PIPELINE_ENABLED=false, AUTO_START_CAMPAIGNS=false, SKIP_SENDER_ATTACH=true.

## Variables (names only)
CLIENT=lumion, DATABASE_URL (reference to Postgres), PUBLIC_URL, PIPELINE_ENABLED=false, BUZZLEAD_ENABLED=false,
LUMION_EMAIL_BISON_API_KEY, SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET, and the vendor keys: QUICKENRICH, LEADOCEAN, JINA,
SERPER, MILLIONVERIFIER, BOUNCEBAN, TRYKITT, ANTHROPIC, CONCENTRATE_AI (plus AIARK, APOLLO, HUNTER, ICYPEAS, LEADMAGIC
if their credit is restored). For a test also TARGET_CONTACTS and CONTACT_CAP. Leave AUTO_INTAKE unset.
Decide SKIP_SENDER_ATTACH: unset means the push attaches free sender accounts itself, true means senders are attached by hand.

## Deploy a change
    railway up --service lumion-web --detach
A deploy kills a run in flight. Edit locally, deploy between runs.

## Slack, done 2026-10-09
App "Vertical Expander (Lumion)", command /lumion, bot in private channel #lumion-expander. Signed tests: valid signature 200, wrong
secret 401, garbage 401, stale timestamp 401. A test message posted to the channel. SLACK_BOT_TOKEN and SLACK_SIGNING_SECRET are set on lumion-web.

## Not set up yet
UPLOAD_TOKEN (only for the CSV upload endpoints), scheduled services (weekly run, reply stopping, daily review), PIPELINE_ENABLED=true, the first run.
