# TFP Cold Email Prompt Spec (for Claude Code)

Client: Truck Fall Prevention (TFP), Taylor, MI
Built with: BuzzLead `/copywriting` skill (4T flow, SPARK / HPPSC / Poke The Bear / CBC frameworks, 13 rules, hard constraints) + `spam-word-checker-en-ish` guardrails
Angles in scope: A4 Safety Page vs Loading Yard, A5 Load Type Fit, A7 Carrier Liability, A10 Peer Proof (with A1 Multi-Site folded in)
Data inputs: Jina website scrape + Serper news

---

## 0. What Claude Code is building

A per-lead pipeline that turns a lead row into a validated Email 1 + Email 2, ready for EmailBison upload.

```
lead row
  -> Step 1  fetch      (Jina pages + Serper news)          [code]
  -> Step 2  research   (PROMPT R: extract facts as JSON)   [LLM]
  -> Step 3  route      (pick angle, pick proof)            [code]
  -> Step 4  email 1    (PROMPT A4 | A5 | A7 | A10)          [LLM]
  -> Step 5  validate   (hard rules, retry loop)             [code]
  -> Step 6  email 2    (PROMPT F, same angle, new proof)    [LLM]
  -> Step 7  validate + export CSV                           [code]
```

The prompts generate copy. They do not reproduce example emails. Every prompt says so, and the validator checks for leakage of example phrasing (Section 7).

Model: keep in one config constant (`MODEL`). Temperature 0 for PROMPT R, 0.7 for email prompts.

---

## 1. Input contract

CSV columns (required unless marked):

| column | notes |
|---|---|
| `first_name` | required |
| `last_name` | optional |
| `title` | required, used for persona bucket |
| `company` | required, display name (strip Inc, LLC, Corp) |
| `domain` | required |
| `city`, `state` | optional, from list source |
| `employee_count` | optional |
| `industry_raw` | optional, from list source |

---

## 2. Step 1: Fetch (code)

**Jina.** Fetch `https://r.jina.ai/https://{domain}`. From the returned markdown, collect internal links whose path or anchor text matches any of: `location|facilit|plant|branch|mill|about|safety|ehs|sustainab|esg|career|product|capabilit|service|logistic|shipping|fleet|transport`. Fetch up to 5 of those pages. Concatenate as:

```
=== PAGE: {url} ===
{markdown}
```

Strip nav/footer boilerplate (repeated lines across pages). Truncate total to ~25k chars, keeping the safety, locations, and products pages first.

**Serper.** `POST https://google.serper.dev/news` with `q: "{company}"`, `tbs: qdr:y`, `num: 10`. Keep title, snippet, date, link. Expect empty results for many mid-market targets. That is fine: no angle in this spec depends on news.

Cache both raw payloads per domain (`cache/{domain}.json`) so re-runs do not re-bill.

---

## 3. Step 2: PROMPT R (research extraction)

Output must be JSON only. Code parses it; on parse failure retry once with "Return valid JSON only."

```
You are a research analyst preparing facts for a cold email about truck and trailer fall prevention. You extract. You never invent.

The seller: Truck Fall Prevention (TFP) makes engineered systems that stop people falling off flatbeds, conestogas, dry vans, and lift-gate trucks during loading, unloading, tarping, and securement.

The prospect:
- Name: {{first_name}}, Title: {{title}}
- Company: {{company}} ({{domain}})
- List data: city={{city}}, state={{state}}, employees={{employee_count}}, industry={{industry_raw}}

WEBSITE CONTENT (Jina scrape):
<website>
{{jina_markdown}}
</website>

RECENT NEWS (Serper, may be empty):
<news>
{{serper_news_json}}
</news>

Extract the fields below. Rules:
- Use only what the website or news states. If a field is not supported, use null or [] and confidence "none".
- Quotes must be copied exactly from the website, 12 words max.
- "inferred" values must be marked with confidence "low" and an evidence note.
- Do not guess location counts. Count only facilities the site names or numbers it states.

Return JSON only:
{
  "company_short": "name as a person would say it in conversation",
  "sector": one of ["steel_producer","metals_service_center","bar_wire_drawn","fabrication_manufacturing","building_materials","construction","logistics_trucking","warehousing_distribution","chemicals_industrial","other"],
  "sector_evidence": "short note",
  "location_count": integer or null,
  "location_count_evidence": "exact phrase or list of sites",
  "named_locations": ["City, ST", ...] (max 5),
  "load_types": ["coils","plate","sheet","bar","pipe_tube","rebar","structural","lumber","building_materials","pallets","machinery","other: ..."],
  "load_types_evidence": "exact product words from the site",
  "trailer_types": ["flatbed","conestoga","dry_van","lift_gate","step_deck","unknown"],
  "trailer_types_confidence": "stated" | "inferred" | "none",
  "fleet_model": "private_fleet" | "common_carrier" | "customer_pickup" | "mixed" | "unknown",
  "fleet_model_evidence": "exact phrase or null",
  "fleet_model_confidence": "stated" | "low" | "none",
  "safety_quote": "exact safety value phrase from the site, e.g. a Zero Harm or Target Zero line" or null,
  "safety_quote_page": "url" or null,
  "safety_program_name": "named program if any" or null,
  "site_constraints": ["low_clearance","tight_yard","outdoor_yard","high_volume","multi_shift","unknown"],
  "site_constraints_evidence": "phrase or null",
  "news_context": {
     "expansion": "one-line summary of a new site, line, or acquisition in the last 12 months" or null,
     "safety_recognition": "award or milestone" or null,
     "incident_or_osha": "one-line summary" or null
  },
  "persona_bucket": "ehs" | "warehouse" | "logistics" | "plant_ops" | "other",
  "usable_hook": "the single most specific, non-generic fact you found that a peer would recognize as true about this company (max 20 words)",
  "data_quality": "high" | "medium" | "low"
}

Persona rules for persona_bucket: safety, EHS, HSE, risk, loss prevention -> ehs. Warehouse, shipping, receiving, yard, DC -> warehouse. Logistics, transportation, fleet, supply chain, traffic -> logistics. Plant manager, operations, GM, VP ops -> plant_ops.
```

---

## 4. Step 3: Router (code, no LLM)

### 4.1 Hard exclusions

- `news_context.incident_or_osha` not null -> do not send any angle in this spec. Write lead to `trigger_queue.csv` for the separate OSHA/incident campaign. Never reference an incident at the prospect's company in these angles.
- `sector == "other"` and `data_quality == "low"` -> `needs_review.csv`.

### 4.2 Eligibility

| angle | eligible when |
|---|---|
| A4 Safety Page vs Yard | `safety_quote` not null, or `safety_program_name` not null, or `news_context.safety_recognition` not null |
| A5 Load Type Fit | `load_types` has at least one value |
| A7 Carrier Liability | always eligible. If `fleet_model_confidence` is "none" or "low", the prompt uses hedged framing |
| A10 Peer Proof (+A1) | always eligible. Multi-site mode when `location_count >= 3` or `news_context.expansion` not null |

### 4.3 Assignment

Config flag `MODE`:

- `test` (default for launch): round-robin across eligible angles so each angle gets roughly equal volume per persona. Log `angle` on every row so reply rates can be split by angle and persona.
- `best_fit`: first eligible angle from the persona priority list.

| persona_bucket | priority |
|---|---|
| ehs | A4, A7, A10, A5 |
| logistics | A5, A7, A10, A4 |
| warehouse | A5, A10, A7, A4 |
| plant_ops | A10, A5, A7, A4 |
| other | A10, A5, A7, A4 |

### 4.4 Proof selection

Pick `proof_primary` for Email 1 and a different `proof_secondary` for Email 2 from the Proof Library (Section 5).

Peer match for A10 and as default elsewhere:

| sector | primary peer | secondary |
|---|---|---|
| steel_producer | P1 SDI | P2 Eaton/ADICA |
| metals_service_center | P4 Eaton Steel | P1 SDI |
| bar_wire_drawn | P5 Hercules Drawn Steel | P4 Eaton Steel |
| fabrication_manufacturing | P6 United Alloy | P5 Hercules |
| logistics_trucking | P3 ADICA | P2 Eaton/ADICA |
| warehousing_distribution | P4 Eaton Steel | P3 ADICA |
| chemicals_industrial | P7 INEOS Composites | P1 SDI |
| building_materials, construction | P1 SDI | P7 INEOS (custom fit) |

A10 multi-site mode always uses P1 SDI as primary.
A7 always uses P2 as primary.
A5 uses the sector peer as primary, P7 when `site_constraints` suggests a custom fit.

Gap: TFP has no named construction or building materials customer. Ask David for one (Section 9).

---

## 5. Proof Library (the only claims the model may make)

Pass the selected entries into the prompt as `{{proof_primary}}` / `{{proof_secondary}}`. The model may restate them in its own words but may not add numbers, outcomes, or timeframes that are not here.

```yaml
P1:
  name: Steel Dynamics (SDI) flat rolled divisions
  sector: steel_producer
  claims:
    - Facilities spread across the US differed in age, layout, height clearance, and truck flow, so one off-the-shelf setup could not cover them.
    - TFP supplied a mix of mobile platforms (10, 20, 30, 40 ft) plus on-truck and custom-built systems sized to each site.
    - SDI named TFP a preferred vendor for safety solutions at multiple locations and is rolling the systems out to more facilities.
  quote_person: Simon Schlatter, Safety Coordinator, Steel Dynamics
  source: https://www.truckfallprevention.com/blog/case-study-steel-dynamics-flat-rolled-divisions/

P2:
  name: Eaton Steel / ADICA origin story
  config_flag: ALLOW_NAMED_INCIDENT   # if false, say "a Michigan steel group" instead of Eaton
  claims:
    - A driver for a contracted carrier fell off a flatbed at one of Eaton Steel's plants.
    - The fall cost the parent company of ADICA Trucking a 1.4 million dollar settlement.
    - The team looked for something lightweight and portable, found nothing, and built what became NoFalls.
  source: https://www.ccjdigital.com/workforce/safety/article/15064764/flatbed-carrier-invents-safety-feature-for-drivers

P3:
  name: ADICA Trucking (Michigan)
  claims:
    - ADICA's Director of Safety says NoFalls has prevented many falls from the decks of their flatbeds.
  source: https://www.truckfallprevention.com/testimonials/

P4:
  name: Eaton Steel Corp.
  claims:
    - Eaton Steel uses NoFalls on all trucks in its warehouse (per its plant manager).
  source: https://www.truckfallprevention.com/testimonials/

P5:
  name: Hercules Drawn Steel (Michigan)
  claims:
    - Hercules bought NoFalls and got rid of the roll-up platforms it had been using (per its plant manager).
  source: https://www.truckfallprevention.com/testimonials/

P6:
  name: United Alloy (Wisconsin)
  claims:
    - United Alloy's manufacturing engineering team rated the build quality highly and expects to use it in future facilities too.
  source: https://www.truckfallprevention.com/

P7:
  name: INEOS Composites (Arkansas)
  claims:
    - When INEOS had an unusual use case, TFP had a solution at their door the next week (per their SHE manager).
  source: https://www.truckfallprevention.com/

PRODUCT_FACTS:   # usable in any angle, max one per email
  - The Side Rolling Panel System sets up in about 40 seconds (client-supplied, onboarding form).
  - Systems are built to support OSHA 1910.29 guardrail criteria. Never say "certified" or "guaranteed compliant".
  - Mobile systems roll up beside the trailer; on-truck systems ride with the trailer.
  - Made in the USA (brochure).
  source: onboarding form; https://www.truckfallprevention.com/blog/case-study-steel-dynamics-flat-rolled-divisions/

INDUSTRY_INSIGHT:   # A4 only
  - In MSCI's 2025 webinar on trailer fall prevention, Russel Metals' safety team said they had no industry standard to follow and had to build their own program. Say "MSCI" or "a metals industry safety webinar"; do not name Russel Metals to a Russel competitor without ALLOW_RUSSEL_MENTION=true.
  source: https://www.msci.org/?p=94416
```

### Load type to system map (A5)

Pass the matched rows as `{{system_matches}}`. Product names are TFP's; do not invent features beyond the descriptor.

| trigger in research JSON | TFP system | descriptor the model may use |
|---|---|---|
| flatbed + coils/plate/sheet/bar/pipe/structural/rebar | Side Rolling Panel System | rolls up beside the deck, about 40 second setup |
| conestoga | Conestoga Offset System | built for conestoga trailers |
| lift_gate or box truck deliveries | Rolling Panel System for Lift-Gate Trucks | for lift-gate trucks |
| tarping or strapping heavy loads | Mobile Panel Securement System | covers securement work |
| low_clearance | Custom: trailers and truck beds with height requirements | custom-sized for clearance |
| tight_yard | Custom: trucks with space restrictions, or on-truck Aluminum Bar System | rides on the trailer, no yard footprint |
| trailers without stake pockets | Custom: trailers without pockets | custom fit |
| mixed fleet of trailer types | mix of mobile + on-truck (P1 pattern) | one approach across trailer types |

---

## 6. Email prompts

### 6.0 SHARED_RULES (inject into every email prompt verbatim)

```
WRITING RULES. Breaking any of these fails the email.

Structure (4T):
1. Trigger: the first line is about THEM, built from a specific fact in the research. Never a generic compliment.
2. Think: one line that makes them picture the risk or friction in their own yard.
3. Third-party credibility: one proof point, restated from the PROOF block only.
4. Talk: one low-friction question. Never ask for a meeting, call, or time on a calendar.

Format:
- Body 300 to 500 characters total. Subject 6 words or fewer.
- Start the body with the first name and a comma. No Hi, Hello, Hey, Dear.
- Paragraphs of 1 to 2 sentences, blank line between them.
- No colon in the first line. No em dashes or en dashes. At most one exclamation point (prefer none). No emojis. No ALL CAPS.
- Use "we", "our", or "us" no more than once.
- No signature. No P.S. No links.
- Subject: lowercase except names, reads like an internal note, no selling, never "Re:" or "Fwd:".

Truth:
- Use only facts from RESEARCH, PROOF, and PRODUCT_FACTS. Do not invent numbers, timeframes, outcomes, or customer names.
- If a research fact has low confidence, soften it ("guessing", "tentatively assuming", "if I've read this right").
- Never claim to have visited their site or seen their yard.
- Never reference an injury, fall, or OSHA event at THEIR company.

Voice:
- Plain, peer-to-peer, like a safety guy talking to a safety guy. Short words. Clarity beats grammar.
- Never use: hope this finds you well, I wanted to reach out, I noticed, I saw, just (as filler), quick (in the CTA), leverage, synergy, optimize, streamline, scale, pain points, best-in-class, industry-leading, innovative, cutting-edge, robust, seamless, game-changing, trusted by, leading companies, circle back, touch base.

Spam-safe (deliverability). Do not use these words in any form:
get, bank, credit, access, open, compare, problem, now, billing, deal, finance, financial, claims, insurance, mortgage, soon, new, performance, freedom, home, sales, medical, urgent, life, marketing, investment, diagnostics, friend, cash, invoice, extra, purchase, free, quote, today, promise, guarantee, guaranteed, earn, click, discount, bonus, thousands, immediately, amazing, fantastic, the best.
Rewrite around them (e.g. "new site" -> "the Gary expansion", "get on the trailer" -> "climb onto the deck").

Examples in this prompt are tone references only. Do not reuse their sentences, openers, or CTAs.

Output JSON only:
{"subject": "...", "body": "...", "first_line_fact_used": "...", "proof_used": "P#", "char_count": n}
```

### 6.1 PROMPT A4: Safety Page vs Loading Yard

Framework: HPPSC (Hook, Pivot, Proof, Suggest, CTA). Best persona: EHS.

Idea: Use their own published safety language as the hook, then point to the spot those programs often under-cover: the top of a trailer. Respectful, never "gotcha". Make them the hero who closes the gap.

```
You write cold emails for Truck Fall Prevention (TFP). This is Email 1, angle "Safety Page vs Loading Yard".

THE ANGLE
{{company_short}} publicly commits to safety. Most plant safety programs are strong on the floor and thinner on the trailer deck, where drivers and loaders work 4 to 5 feet up while tarping and strapping. You respect their program and point at the one spot it may not reach. The tone is "you clearly care about this, here's a corner worth checking", never "you're failing".

PROSPECT
{{first_name}}, {{title}} at {{company_short}}

RESEARCH (from their site and news)
{{research_json}}

Hook material, use the strongest one available:
- safety_quote: {{safety_quote}}
- safety_program_name: {{safety_program_name}}
- safety_recognition: {{news_context.safety_recognition}}

INDUSTRY_INSIGHT (optional, use as the "think" line if it reads naturally)
{{industry_insight}}

PROOF (use one)
{{proof_primary}}

PRODUCT_FACTS (optional, max one)
{{product_facts}}

How to write it
1. First line: reference their safety commitment by echoing their own words or program name (paraphrase or a short quote of 6 words max). Make it clear you read it.
2. Second line: pivot to the trailer deck as the place these programs often stop. Frame it as a question or an observation, not an accusation.
3. Third line: the proof, restated in plain words.
4. CTA: offer something useful and small. Pick ONE: a one-page trailer deck checklist, the peer case study, or photos of the setup at a plant like theirs.

Tone reference only (do not reuse):
"Ana, putting 'zero incidents, every shift' on your careers page sets a high bar.
Trailer decks tend to be where that bar is hardest to hold, since drivers often aren't your employees and the work is 5 feet up.
Steel Dynamics hit that across its flat rolled plants and ended up standardizing it site by site.
Want the one-page checklist they started from?"

{{SHARED_RULES}}
```

### 6.2 PROMPT A5: Load Type Fit

Framework: CBC (Context, Benefit, CTA) with SPARK specificity. Best persona: logistics, warehouse.

Idea: What they ship decides their fall exposure. Name their actual product (coils, plate, rebar, lumber) and trailer type, then name the TFP system built for that exact setup. Feels custom because it is.

```
You write cold emails for Truck Fall Prevention (TFP). This is Email 1, angle "Load Type Fit".

THE ANGLE
What a company ships decides where its people end up standing on a trailer. Coils and plate on flatbeds mean tarping and strapping up top. Conestogas, lift-gates, low-clearance docks, and tight yards each change what a fall prevention setup has to look like. You show you understand THEIR loads and point to the system built for that setup. No generic "we make fall protection".

PROSPECT
{{first_name}}, {{title}} at {{company_short}}

RESEARCH
{{research_json}}

Their loads: {{load_types}} (evidence: {{load_types_evidence}})
Trailer types: {{trailer_types}} (confidence: {{trailer_types_confidence}})
Site constraints: {{site_constraints}}

SYSTEM MATCHES (only name systems from this list)
{{system_matches}}

PROOF (use one)
{{proof_primary}}

PRODUCT_FACTS (optional, max one)
{{product_facts}}

How to write it
1. First line: name their specific load in their words (e.g. "hot-rolled coil", "rebar bundles", "engineered lumber") and the work it creates on the trailer (tarping, strapping, edge protection). If trailer type is inferred, hedge it.
2. Second line: one sentence on why generic platforms or harness setups struggle with that load or yard. Specific, not dramatic.
3. Third line: name ONE matched TFP system and the one descriptor allowed for it, plus the proof if it fits in length. If space is tight, drop the proof before dropping the system match.
4. CTA: offer the spec sheet or a photo of that system on the same load type. One question.

Tone reference only (do not reuse):
"Marcus, tarping coil on flatbeds all day puts your drivers up on the deck more than most shippers.
Roll-up stairs help getting up there but don't do much once someone is walking the edge.
Our Side Rolling Panel System rolls in beside the trailer and is up in about 40 seconds.
Want a photo of it on a coil load?"

{{SHARED_RULES}}
```

### 6.3 PROMPT A7: Not Your Driver, Still Your Liability

Framework: Pain Amplification with a narrative proof (Narrative Transportation rule). Best persona: EHS, logistics.

Idea: Third-party carrier drivers and customer pickups fall on YOUR property, and the liability lands on you. The Eaton fall was a contract carrier's driver and cost $1.4M. Most safety directors assume the carrier handles it.

```
You write cold emails for Truck Fall Prevention (TFP). This is Email 1, angle "Not Your Driver, Still Your Liability".

THE ANGLE
A lot of loading happens with people who don't work for the shipper: common carrier drivers, customer pickups, contract haulers. Many safety teams assume the carrier owns that risk. In practice, when a driver falls off a flatbed at your dock, the claim follows the site. The story that proves it is the fall that started TFP.

PROSPECT
{{first_name}}, {{title}} at {{company_short}}

RESEARCH
{{research_json}}

Fleet model: {{fleet_model}} (confidence: {{fleet_model_confidence}}, evidence: {{fleet_model_evidence}})

Framing by confidence:
- "stated" and common_carrier/customer_pickup/mixed: state it plainly ("with carriers hauling out of your {{named_location}} plant").
- "stated" and private_fleet: shift to "even with your own fleet, outside carriers and customer pickups still load at your dock".
- "low" or "none": hedge ("tentatively assuming outside carriers load at some of your sites").

PROOF (use this one)
{{proof_primary}}
ALLOW_NAMED_INCIDENT = {{allow_named_incident}}  (if false, say "a Michigan steel group", not Eaton)

How to write it
1. First line: their shipping setup, specific to them (location, product, or fleet detail).
2. Second line: the assumption most teams make, that the carrier's driver is the carrier's problem. Pose it as a question or plain observation.
3. Third line: the story in one or two sentences. A contract driver fell off a flatbed at a steel plant, the settlement was 1.4 million dollars, and that fall is why NoFalls exists. Write the number as $1.4M.
4. CTA: soft and curious. Offer to send how that plant handles carrier drivers now, or ask whether carrier drivers are covered by their current setup.

Do not dramatize the injury. No words like tragic, horrific, devastating.

Tone reference only (do not reuse):
"Dana, with outside carriers pulling coil out of Decatur most days, a lot of your loading is done by people who aren't on your payroll.
Most teams figure that's the carrier's risk.
A Michigan steel plant thought so too, until a contract driver fell off a flatbed and it settled for $1.4M. That fall is why NoFalls exists.
Are carrier drivers covered by your current setup?"

{{SHARED_RULES}}
```

### 6.4 PROMPT A10: Peer Proof (with Multi-Site folded in)

Framework: Authority Opener (single site) / SPARK (multi-site). Best persona: plant_ops, all.

Idea: Someone who looks like them already solved this. Single-site prospects get their closest peer by sector and region. Multi-site prospects (3+ locations or an expansion in the news) get the SDI story: one standard that flexes per site.

```
You write cold emails for Truck Fall Prevention (TFP). This is Email 1, angle "Peer Proof".

THE ANGLE
Mode: {{a10_mode}}   ("single_site" or "multi_site")

single_site: A company that looks like {{company_short}} (same kind of product, similar region) already put fall prevention on its trailers. Lead with the peer, connect to their operation, offer the details.

multi_site: Running {{location_count}} sites means {{location_count}} different loading yards with different clearances, truck flow, and trailer mixes. Getting one fall prevention standard to work across all of them is where safety teams stall. Steel Dynamics solved it across its flat rolled divisions with one approach sized per site, and named TFP a preferred vendor. If news_context.expansion exists, use the expansion as the trigger ("with the {{city}} expansion adding another yard...").

PROSPECT
{{first_name}}, {{title}} at {{company_short}}

RESEARCH
{{research_json}}

Locations: {{location_count}} ({{location_count_evidence}}), named: {{named_locations}}
Expansion news: {{news_context.expansion}}

PROOF (primary, lead with this)
{{proof_primary}}

How to write it
single_site:
1. First line: a specific fact about their operation (usable_hook, product, or location).
2. Second line: the peer and what they did, restated from PROOF. Name the peer company.
3. Third line: one sentence connecting the peer's situation to theirs (same product, same trailer type, same region).
4. CTA: offer the peer story or setup photos.

multi_site:
1. First line: their footprint, using the real count or named sites. If count came only from news or is inferred, hedge.
2. Second line: why one standard across different yards is hard.
3. Third line: SDI proof in plain words.
4. CTA: offer the SDI case study or "how they rolled it out site by site".

Tone references only (do not reuse):
single_site: "Kevin, cold-drawn bar out of Detroit means a lot of strapping on flatbeds. Hercules Drawn Steel, a few miles from you, swapped its roll-up platforms for our panels. Same load, same trailers. Want to see their setup?"
multi_site: "Lisa, four service centers means four yards with four different dock layouts. That's usually where a single fall standard falls apart. Steel Dynamics ran into it across its flat rolled plants and ended up with one approach sized to each site. Worth sending how they rolled it out?"

{{SHARED_RULES}}
```

### 6.5 PROMPT F: Email 2 (shared follow-up, all angles)

Rule from `/copywriting`: Different, Disarming, Direct. New element, 150 to 300 characters.

```
You write the second email in a cold sequence for Truck Fall Prevention (TFP). It sends 3 business days after Email 1, in the same thread, so no subject line.

EMAIL 1 (already sent)
{{email1_body}}

ANGLE: {{angle}}
PROSPECT: {{first_name}}, {{title}} at {{company_short}}
RESEARCH: {{research_json}}

NEW PROOF (must be different from Email 1)
{{proof_secondary}}

PRODUCT_FACTS (optional)
{{product_facts}}

Pick the follow-up move that fits best:
- Value extension: one product fact Email 1 didn't use (e.g. 40 second setup, mobile vs on-truck, built to OSHA 1910.29 guardrail criteria).
- Social proof hook: the new peer, one line.
- Colleague redirect: ask if this sits better with whoever runs shipping or the yard at {{named_location}}.
- One-liner question: a sharp question about their yard that Email 1 raised but didn't ask.

Rules
- 150 to 300 characters. 2 or 3 short paragraphs.
- Start with first name and a comma.
- Never say: just following up, bumping, checking in, circling back, following up, in case you missed, any construction that refers to the previous email being unanswered.
- Never promise to stop emailing if they stay silent. If you include an out, it must require them to act ("a one-word 'pass' works if this isn't yours").
- One question at the end.

{{SHARED_RULES minus the Structure and length lines}}

Output JSON only:
{"body": "...", "move_used": "...", "proof_used": "P#", "char_count": n}
```

---

## 7. Step 5 and 7: Validator (code)

Run on every generated email. Collect all violations, then retry the prompt up to 2 times, appending:

```
Your last draft broke these rules: {{violations}}. Rewrite it fixing only those. Same angle, same facts.
```

After 2 failed retries, write to `needs_review.csv` with the violations.

Checks:

| check | rule |
|---|---|
| JSON | parses, required keys present |
| length | Email 1 body 300 to 500 chars; Email 2 body 150 to 300 chars (count after trimming) |
| subject | 1 to 6 words, no `re:` / `fwd:`, no `?!` |
| opener | body starts with `{first_name},` ; no `^(hi|hello|hey|dear)\b` |
| dashes | no `—` or `–`; no ` - ` used as a dash |
| colon | no `:` in first line |
| exclamation | count of `!` <= 1 |
| we-count | `\b(we|our|us)\b` count <= 1 in Email 1 |
| banned words | word-boundary regex on the full banned list in SHARED_RULES, case-insensitive, also catch hyphenated variants (`get-`, `-new`) |
| banned phrases | phrases list in SHARED_RULES + `spam-word-checker` phrase lists |
| placeholders | no `{{` or `}}` left |
| numbers | every digit sequence in the body must appear in the allowed set: research JSON values, `1.4`, `40`, `10`, `20`, `30`, `1910.29`. Anything else = invented number |
| names | any capitalized company name must be the prospect or a Proof Library name |
| example leakage | no 6-word shingle shared with any tone reference in the prompt |
| incident guard | if body mentions OSHA, citation, injury, or fall in the same sentence as the prospect's company name -> fail |

---

## 8. Step 7: Output

`output/emails.csv`, one row per lead:

```
first_name, last_name, email, company, title, persona_bucket, angle, a10_mode,
proof_primary, proof_secondary, subject, email1_body, email2_body,
first_line_fact_used, data_quality, validator_retries, status
```

Also write `output/research/{domain}.json` for QA and `output/trigger_queue.csv`, `output/needs_review.csv`.

Run order for QA before full volume: process 20 leads (5 per angle), print a side-by-side review table to the terminal, stop for human approval. Then run the rest.

---

## 9. Open items to confirm with David (TFP)

1. `ALLOW_NAMED_INCIDENT`: OK to name Eaton Steel and the $1.4M settlement in cold copy? It is public (CCJ), but it is his group's incident. Default false until confirmed.
2. A number for SDI: how many SDI sites run TFP, and how long the rollout took. Adds a real "result + timeframe" to P1, which `/copywriting` wants.
3. A construction or building materials customer. Those sectors are in the ICP but have no named peer, so A10 falls back to SDI.
4. Assets the CTAs promise: one-page trailer deck checklist (A4), spec sheets per system (A5), setup photos by load type (A5, A10). If any don't exist, remove that CTA option from the prompt.
5. ADICA_Eaton Customers.csv from the onboarding form: run it as a separate re-engagement list, and suppress those domains from this campaign.

---

## Sources

- TFP onboarding form (Airtable, submitted 8/5/2026): ICP, trigger events, competitors, 40 second Side Rolling Panel setup, buyer quotes.
- SDI case study: https://www.truckfallprevention.com/blog/case-study-steel-dynamics-flat-rolled-divisions/
- TFP testimonials (ADICA, Eaton Steel, Hercules): https://www.truckfallprevention.com/testimonials/
- TFP homepage testimonials (United Alloy, INEOS Composites, SDI): https://www.truckfallprevention.com/
- Eaton Steel fall and $1.4M settlement: https://www.ccjdigital.com/workforce/safety/article/15064764/flatbed-carrier-invents-safety-feature-for-drivers
- MSCI trailer fall prevention webinar (Russel Metals, no industry standard): https://www.msci.org/?p=94416
- BuzzLead skills: `/copywriting` (frameworks.md, rules-and-components.md, personalization.md), `spam-word-checker-en-ish`
