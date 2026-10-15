# Lumion copy-test expander

A standalone build for one client: **Lumion** (EmailBison workspace 37 on personal.buzzlead.io). It was forked
from the ProQ and MasterNow build, which was itself forked from the IDPR vertical expander. It is **not connected**
to IDPR, ProductEVO, ProQ or MasterNow: separate code, separate Railway project (`lumion-vertical-expander`, to be
created), separate Postgres database `lumion`, separate EmailBison key, separate Slack app.

## Separation rules
- No other client's folders, prompts, suppression lists or keys are in this tree.
- `CLIENT` must be set to `lumion`. There is no default.
- Never edit the IDPR/ProductEVO working folder or the ProQ/MasterNow build from here, and never relink them to this project.

## The test
Three angles, one EmailBison campaign each, every vertical in every campaign, contacts dealt to the angles at random
and evenly inside each segment (`clients/lumion/copy_test.toml`). The engine tests the ANGLE, not the vertical.

| Arm folder | Angle |
|---|---|
| pdf-application | The school's application is a PDF students print and bring in |
| after-5pm | The office closes at 5, so after-hours callers hit voicemail |
| next-start-date | A published start date approaches while older inquiries sit unworked |

Spec: `clients/lumion/spec/LUMION-SPEC.md`. Titles: `clients/lumion/titles.txt` (Emmanuel's 22, in order).

## What is different from the original app
- `app/company_fit.py` reads `clients/<name>/fit_prompt.txt` when present, so Lumion's gate judges schools, not product makers.
- `app/stages/s4_people.py` supports `include_wins`: a title that contains a buyer title is kept even if it carries an excluded
  word ("Owner / Lead Instructor"), and "coordinator" is allowed when the client declared a coordinator title.
- `app/consultant_gate.py` is used only as the history gate (replied, unsubscribed, in flight, 90-day floor, re-entry ladder
  150 / 120 / 90). The partition split is off.
- `clients/lumion/copy_rules.toml`: the spec's client bans on top of the shared validator.

## Status
The three copy prompts are the only thing missing: `clients/lumion/prompts/<arm>/prompt_research.txt`, `prompt_email1.txt`,
`prompt_email2.txt`. Until they exist the engine reports the profile incomplete and refuses to send. `PIPELINE_ENABLED=false`
on Railway until the sample is approved. Tests: `CLIENT=lumion pytest tests`; 33 inherited tests are skipped and listed in
`tests/inherited_client_specific.txt`.
