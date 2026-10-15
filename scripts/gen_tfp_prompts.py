#!/usr/bin/env python3
"""Turn clients/tfp/spec/TFP-COPY-SPEC.md into the engine's prompt files, copy rules, copy test and client settings.

The spec is a per-lead pipeline (code router + prompts). The engine deals contacts to angle campaigns at random and each arm handles missing facts itself,
so the router's eligibility and proof choice move INTO the prompts (sector to peer table, fallbacks). Deviations from the spec, on purpose:
  1. The model campaign (2103) prints "Hi {FIRST_NAME} - " before PERSONALIZATION 1 and 2, so bodies do NOT start with the first name. No subject is generated
     (the sequence has its own spintax subject).
  2. P7 INEOS Composites is dropped from the proof library: standing TFP rule, forbidden proof point (memory tfp-vertical-playbook). Confirm with Emmanuel.
  3. ALLOW_NAMED_INCIDENT stays false (spec default): the Eaton fall is told as "a Michigan steel group", enforced in copy_rules.toml.
  4. Output is the email body only (the engine takes plain text), not JSON.
Run from the repo root:  python scripts/gen_tfp_prompts.py
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = (ROOT / "clients/tfp/spec/TFP-COPY-SPEC.md").read_text(encoding="utf-8")
OUT = ROOT / "clients/tfp"

RESEARCH_MODEL = "openai/gpt-oss-120b"
EMAIL_MODEL = "anthropic/claude-haiku-4.5"

ANGLES = {   # folder -> (engine key, title, angle marker in the spec block)
    "a4-safety-page": ("A4", "Safety Page vs Loading Yard", 'angle "Safety Page vs Loading Yard"'),
    "a5-load-type": ("A5", "Load Type Fit", 'angle "Load Type Fit"'),
    "a7-carrier-liability": ("A7", "Not Your Driver, Still Your Liability", 'angle "Not Your Driver, Still Your Liability"'),
    "a10-peer-proof": ("A10", "Peer Proof", 'angle "Peer Proof"'),
}

blocks = re.findall(r"```(\w*)\n(.*?)```", SPEC, re.S)


def block(start: str) -> str:
    for _lang, body in blocks:
        if body.lstrip().startswith(start) or start in body[:400]:
            return body.strip("\n")
    raise SystemExit("spec block not found: " + start)


R = block("You are a research analyst")
SHARED = block("WRITING RULES")
PROOFS = block("P1:")
F = block("You write the second email")
ANGLE_BLOCK = {k: block(v[2]) for k, v in ANGLES.items()}


def table_after(heading: str) -> str:
    i = SPEC.index(heading)
    seg = SPEC[i:].split("\n---", 1)[0]
    return "\n".join(l for l in seg.splitlines() if l.startswith("|"))


SYSTEM_MAP = table_after("### Load type to system map (A5)")

# P7 (INEOS Composites) removed, see the module docstring.
PROOFS = re.sub(r"\nP7:\n(?:  .*\n?)+", "\n", PROOFS)
PROOFS = PROOFS.replace("  config_flag: ALLOW_NAMED_INCIDENT   # if false, say \"a Michigan steel group\" instead of Eaton",
                        "  rule: ALLOW_NAMED_INCIDENT is FALSE. Tell this story as \"a Michigan steel group\" or \"a Michigan steel plant\". Never write Eaton or ADICA next to the fall or the settlement.")

ROUTING = """PROOF CHOICE (you pick it from the research JSON field "sector"; the proofs below are the ONLY claims you may make).
Email 1 uses the PRIMARY peer, Email 2 uses a DIFFERENT one.
| sector | primary peer | secondary |
|---|---|---|
| steel_producer | P1 SDI | P2 told as a Michigan steel group |
| metals_service_center | P4 Eaton Steel | P1 SDI |
| bar_wire_drawn | P5 Hercules Drawn Steel | P4 Eaton Steel |
| fabrication_manufacturing | P6 United Alloy | P5 Hercules |
| logistics_trucking | P3 ADICA | P2 told as a Michigan steel group |
| warehousing_distribution | P4 Eaton Steel | P3 ADICA |
| chemicals_industrial | P1 SDI | P4 Eaton Steel |
| building_materials, construction, other | P1 SDI | P4 Eaton Steel |
There is no customer in construction or building materials: use P1 and say only what P1 says. A10 in multi_site mode always uses P1 as primary. A7 always uses P2 (as a Michigan steel group) as primary."""

SKIP = """SKIP RULE. If the research JSON has news_context.incident_or_osha that is not null, or sector is "other" with data_quality "low", output exactly the one word SKIP and nothing else. Never mention an incident, fall, injury or OSHA event at the prospect's own company."""

DATA_NOTE = """DATA YOU RECEIVE. The user message gives the prospect's company, their job title and a RESEARCH block that is JSON with these fields: company_short, sector, location_count, named_locations, load_types, trailer_types (and trailer_types_confidence), fleet_model (and fleet_model_confidence), safety_quote, safety_program_name, site_constraints, news_context (expansion, safety_recognition, incident_or_osha), persona_bucket, usable_hook, data_quality. Use company_short as the company's name. Fields that are null or empty were not found on the site: never invent them, write around them with a hedge or a plainer first line that is still about them."""

FALLBACKS = {
    "a4-safety-page": "FALLBACK. If safety_quote, safety_program_name and news_context.safety_recognition are all null, open on the most specific true fact you have (usable_hook, a named location or their product) and keep the trailer deck pivot hedged (\"guessing\", \"if I've read this right\").",
    "a5-load-type": "FALLBACK. If load_types is empty, name what a company like this ships from the sector, hedged, and keep the matched TFP system.",
    "a7-carrier-liability": "FALLBACK. If fleet_model_confidence is none or low, use the hedged framing.",
    "a10-peer-proof": "MODE. Use multi_site when location_count is 3 or more or news_context.expansion is not null; otherwise single_site.",
}

# SHARED_RULES adapted for the engine (greeting, subject, output format).
SH = SHARED
SH = SH.replace("- Body 300 to 500 characters total. Subject 6 words or fewer.", "- Body 300 to 500 characters total. There is no subject line.")
SH = SH.replace("- Start the body with the first name and a comma. No Hi, Hello, Hey, Dear.",
                "- Do NOT start with a greeting or the first name. The sequence already prints \"Hi {first name} -\" in front of your text, so begin straight with the first line.")
SH = re.sub(r"- Subject: .*\n", "", SH)
SH = SH.split("Output JSON only:")[0].rstrip() + "\n\nOutput the email body only: plain text, no subject, no JSON, no quotes, no labels, no character count."


def strip_leading_name(text: str) -> str:
    """Tone references start with a first name; the sequence supplies the greeting, so drop it from the examples."""
    def fix(m):
        return m.group(1) + m.group(3).upper()
    return re.sub(r'(")[A-Z][a-z]+, (.)', lambda m: m.group(1) + m.group(2).upper(), text)


def angle_system(folder: str) -> str:
    b = ANGLE_BLOCK[folder]
    head = b.split("\nPROSPECT\n", 1)[0]
    how = "How to write it" + b.split("How to write it", 1)[1].split("\n{{SHARED_RULES}}")[0]
    head = head.replace("{{company_short}}", "the prospect").replace("{{location_count}}", "N")
    head = re.sub(r'Mode: \{\{a10_mode\}\}.*\n', "", head)
    how = how.replace("{{city}}", "the city").replace("{{company_short}}", "the prospect")
    how = strip_leading_name(how)
    how = re.sub(r"\{\{[^}]*\}\}", "the matching research field", how)
    extra = ""
    if folder == "a5-load-type":
        extra = "SYSTEM MATCHES (only name systems from this table; pick the row that fits the research JSON)\n" + SYSTEM_MAP + "\n\n"
    if folder == "a4-safety-page":
        extra = ("INDUSTRY_INSIGHT (optional, A4 only): In MSCI's 2025 webinar on trailer fall prevention, a metals safety team said they had no industry "
                 "standard to follow and built their own program. Say \"MSCI\" or \"a metals industry safety webinar\". Never name Russel Metals.\n\n")
    pf = "PRODUCT_FACTS and PROOF (the only claims you may make)\n" + PROOFS
    text = "\n\n".join([head.strip(), DATA_NOTE, FALLBACKS[folder], SKIP, how.strip(), extra.strip(), ROUTING, pf, SH]).replace("\n\n\n", "\n\n")
    return text.replace("\nthe prospect", "\nThe prospect")


def email2_system(folder: str, title: str) -> str:
    moves = "Pick the follow-up move that fits best:" + F.split("Pick the follow-up move that fits best:", 1)[1].split("\n{{SHARED_RULES")[0]
    moves = moves.replace("{{named_location}}", "their named location").replace("{{angle}}", title)
    moves = re.sub(r"\{\{[^}]*\}\}", "the matching research field", moves)
    moves = moves.replace("- Start with first name and a comma.\n", "- Do not start with a greeting or the first name; the sequence prints \"Hi {first name} -\" first.\n")
    head = ("You write the second email in a cold sequence for Truck Fall Prevention (TFP). It sends 3 business days after Email 1, in the same thread, so no subject line.\n"
            f"This sequence's angle is \"{title}\": stay in that angle, with a new element.")
    shared2 = re.sub(r"Structure \(4T\):.*?\n\nFormat:", "Format:", SH, flags=re.S)
    shared2 = shared2.replace("- Body 300 to 500 characters total. There is no subject line.\n", "")
    return "\n\n".join([head, DATA_NOTE, SKIP.replace("Email 1", "this email"), moves.strip(), ROUTING, "PROOF LIBRARY (the only claims you may make; use a DIFFERENT peer than Email 1 used, read it from the Email 1 text)\n" + PROOFS, shared2]).replace("\n\n\n", "\n\n")


def research_system() -> str:
    i = R.index("The prospect:")
    j = R.index("Extract the fields below.")
    return (R[:i].rstrip() + "\n\n" + R[j:]).replace("Return JSON only:", "Return JSON only, no prose, no code fence:")


def prompt_file(name, variant, model, temp, tokens, source, system, user) -> str:
    return (f"SOURCE: {source}\nVARIANT: {variant}\nmodel: {model}\ntemperature: {temp}\nmax_tokens: {tokens}\nSYSTEM PROMPT (verbatim):\n---\n{system}\n---\n"
            f"USER MESSAGE TEMPLATE:\n{user}\n")


RES_USER = """PROSPECT DATA:
{person_profile_data}
{company_profile_data}

WEBSITE CONTENT (Jina scrape):
<website>
{website_text}
</website>

RECENT NEWS (Serper, may be empty):
<news>
{serper_news_results}
</news>

Return the JSON now."""
E1_USER = """PROSPECT DATA:
Company: {company_name}
Title: {job_title}
RESEARCH (JSON):
{research_report}
Write Email 1 now. Body only."""
E2_USER = """PROSPECT DATA:
Company: {company_name}
Title: {job_title}
RESEARCH (JSON):
{research_report}
EMAIL 1 (already sent):
{email_1_output}
Write Email 2 now. Body only."""

src = "TFP-COPY-SPEC.md (2026-10-15), adapted for the engine by scripts/gen_tfp_prompts.py"
for folder, (key, title, _m) in ANGLES.items():
    d = OUT / "prompts" / folder
    d.mkdir(parents=True, exist_ok=True)
    (d / "prompt_research.txt").write_text(prompt_file("research", folder, RESEARCH_MODEL, 0, 2000, src + ", PROMPT R", research_system(), RES_USER), encoding="utf-8")
    (d / "prompt_email1.txt").write_text(prompt_file("email1", folder, EMAIL_MODEL, 0.7, 700, src + f", PROMPT {key}, Email 1 300 to 500 chars", angle_system(folder), E1_USER), encoding="utf-8")
    (d / "prompt_email2.txt").write_text(prompt_file("email2", folder, EMAIL_MODEL, 0.7, 500, src + ", PROMPT F, Email 2 150 to 300 chars", email2_system(folder, title), E2_USER), encoding="utf-8")

# ---- copy rules (added to the shared floor in app/validator.py) ----
SPAM = ("get bank credit access open compare problem now billing deal finance financial claims insurance mortgage soon new performance freedom home sales "
        "medical urgent life marketing investment diagnostics friend cash invoice extra purchase free quote today promise guarantee guaranteed earn click "
        "discount bonus thousands immediately amazing fantastic").split() + ["the best"]
PHRASES = ["hope this finds you well", "i wanted to reach out", "i noticed", "i saw", "leverage", "synergy", "optimi[sz]e", "streamline", "scale", "pain points",
           "best-in-class", "industry-leading", "innovative", "cutting-edge", "robust", "seamless(ly)?", "game-changing", "trusted by", "leading companies",
           "circle back", "touch base", "just following up", "bumping", "checking in", "circling back", "following up", "in case you missed", "quick (call|question|chat)",
           "tragic", "horrific", "devastating", "guaranteed compliant", "certified"]
rules = [
    ("spam-trigger words (deliverability list in the spec)", r"(?i)(?<![A-Za-z])(" + "|".join(SPAM) + r")(?![A-Za-z])"),
    ("banned phrases and filler", r"(?i)(" + "|".join(PHRASES) + r")"),
    ("no dash characters: spaced hyphen or double hyphen", r"\s-\s|--"),
    ("no colon in the opening line", r"\A[^\n]{0,200}:"),
    ("at most one exclamation point", r"(?s)!.*!"),
    ("we, our or us at most once", r"(?is)\b(we|our|us)\b.*\b(we|our|us)\b"),
    ("never ask for a meeting, call, demo or calendar time", r"(?i)\b(a call|meeting|calendar|demo|schedule a|book a)\b"),
    ("no dollar figures other than the settlement story", r"\$\s?(?!1\.4M)\d"),
    ("no percentages", r"\d\s?%"),
    ("FORBIDDEN proof point: INEOS", r"(?i)\bINEOS\b"),
    ("never name Eaton or ADICA next to the fall or the settlement (ALLOW_NAMED_INCIDENT is false)",
     r"(?is)(Eaton|ADICA)[^.\n]*(1\.4|settle|fell|fall)|(1\.4M?|settle)[^.\n]*(Eaton|ADICA)"),
    ("never name Russel Metals", r"(?i)\bRussel"),
    ("no ALL CAPS shouting", r"(?-i:\b[A-Z]{5,}\b)"),
]
body = ["# TFP copy bans from TFP-COPY-SPEC.md (section 6.0 and 7). They are ADDED to the shared floor in app/validator.py (dashes, greetings, sign-offs,",
        "# markdown, preambles). Generated by scripts/gen_tfp_prompts.py. Literal strings avoid backslash escapes.", ""]
for name, pat in rules:
    body += ["[[banned]]", f'name = "{name}"', f"pattern = '{pat}'", ""]
(OUT / "copy_rules.toml").write_text("\n".join(body), encoding="utf-8")

# ---- copy test ----
test = '''# TFP copy test (TFP-COPY-SPEC.md angles A4, A5, A7, A10). One campaign per angle, every vertical in every campaign, contacts dealt at random.
# Campaign 2103 is the model campaign and the first campaign to load into; the other angle campaigns are duplicates of it (see campaigns below).

[angles]
A4  = "Safety Page vs Loading Yard"
A5  = "Load Type Fit"
A7  = "Not Your Driver, Still Your Liability"
A10 = "Peer Proof"

[segments]
Manufacturing = ["Steel, Metals and Metal Fabrication", "Machinery and Equipment Manufacturing", "General Manufacturing and Industrial Products", "Automotive and Transportation Equipment", "Defense and Aerospace Manufacturing", "Consumer and Medical Products Manufacturing"]
Materials     = ["Building Materials and Products", "Construction and Infrastructure", "Mining, Oil, Gas and Utilities"]
Logistics     = ["Trucking and Freight Carriers", "Logistics, Warehousing and 3PL", "Maritime and Rail", "Wholesale and Distribution"]
Resources     = ["Food and Beverage Production", "Agriculture and Forestry", "Equipment Rental, Recycling and Waste"]
Other         = ["Other Industrial Shippers"]
Tier2         = ["Tier 2 - previously emailed"]

[segment_caps]
Manufacturing = 250
Materials     = 250
Logistics     = 250
Resources     = 250
Other         = 250
Tier2         = 250

[rules]
per_segment_per_campaign = 250
freeze_days = 7
decision_day = 10
positive_events = ["interested", "booked"]
random_deal_seed = "tfp-angles-1"
booked_weight = 3

[angle_prompts]
A4  = "a4-safety-page"
A5  = "a5-load-type"
A7  = "a7-carrier-liability"
A10 = "a10-peer-proof"

[scaling]
champion_topup = 1000
expand_verticals = 3
leads_per_new_vertical = 300

[fill]
# Proof run size per angle. Raise only after the sample is approved.
target = 15

[fit]
b2b_product_makers = ""

[angle_pitch]
A4  = "Safety Page vs Loading Yard: \\"your safety page is strong on the floor, the trailer deck is the corner worth checking\\""
A5  = "Load Type Fit: \\"what you ship decides where your people stand, here is the system built for that load\\""
A7  = "Carrier Liability: \\"it is not your driver, but the claim follows your site\\""
A10 = "Peer Proof: \\"a plant like yours already put this on its trailers\\""
'''
(OUT / "copy_test.toml").write_text(test, encoding="utf-8")

# ---- client.toml: lengths, arms ----
ct = (OUT / "client.toml").read_text(encoding="utf-8")


def setkey(src: str, key: str, value: str) -> str:
    pat = re.compile(rf"^{re.escape(key)}\s*=.*$", re.M)
    return pat.sub(lambda _m: f"{key} = {value}", src, count=1) if pat.search(src) else src + f"\n{key} = {value}\n"


for k, v in (("email1_min_chars", "300"), ("email1_max_chars", "500"), ("email2_min_chars", "150"), ("email2_max_chars", "300"), ("base_variant", '"a10-peer-proof"')):
    ct = setkey(ct, k, v)
ct = re.sub(r"(?s)# Angles are pending.*?(?=\[\[vertical_defs\]\])", "", ct)
ct = re.sub(r"\[\[copy_variants\]\]\nname = .*\nbet = .*\n\n?", "", ct)   # idempotent: drop any arms from an earlier run
arms = ""
for folder, (key, title, _m) in ANGLES.items():
    arms += f'[[copy_variants]]\nname = "{folder}"\nbet = "{title}"\n\n'
ct = ct.replace("[[vertical_defs]]", arms + "[[vertical_defs]]", 1)
(OUT / "client.toml").write_text(ct, encoding="utf-8")
print("wrote prompts for", list(ANGLES), "| copy_rules", len(rules), "rules | copy_test.toml | client.toml updated")
