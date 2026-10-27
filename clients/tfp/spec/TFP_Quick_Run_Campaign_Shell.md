# TFP Quick Run: Campaign Shell for Claude Code

Client: Truck Fall Prevention (truckfallprevention.com)
Scope: 4 campaigns, 150 contacts each, spintaxed copy ready for EmailBison.
This is a quick directional run. Do not add steps that are not in this file.

---

## PART 1: HOW TO READ THIS FILE (READ FIRST)

1. Every piece of copy has a unique ID, for example `BM-E1-V3`.
   - `BM` = campaign code
   - `E1` = email step (E1, E2, E3)
   - `V3` = variant number
2. Copy is FINAL. Load it from this file by ID. Do not retype, rewrite, re-spin, shorten, or "improve" it.
3. Spintax uses single braces `{a|b|c}`. Merge variables use double braces `{{first_name}}`.
   - Variables are NEVER inside a spintax group. Do not move them inside one.
   - Before upload, check EmailBison's variable syntax. If it differs from `{{variable}}`, convert ONLY the variables, never the spintax.
4. E2 and E3 are sent in the same thread. They have NO subject line.
5. Campaign codes:

| Code | Campaign |
|---|---|
| BM | Building Materials |
| HG | Heavy Goods Manufacturers |
| CN | Construction (equipment rental and dealers) |
| BR | Broad Angle Test (warehousing) |

---

## PART 2: BUILD STEPS (RUN IN THIS EXACT ORDER, ONE CAMPAIGN AT A TIME)

### Step 1: Pull companies
- Use the NAICS codes for the campaign (Part 3).
- Geography: United States and Canada.
- Size: 500+ employees. If fewer than 60 companies pass Step 2, add 200-499 employees and tag them `size_tier = 2`.
- Pull roughly 100 companies so that 50-60 survive the gate.

### Step 2: Qualify (gate)
- Run the campaign's gate checks from Part 3 against each company website.
- Drop any company that fails a required gate. Do not "fix" a failed gate by guessing.

### Step 3: Enrich
- Fill the enrichment fields listed for the campaign in Part 3, using the exact formatting rules.
- If a required field cannot be filled with confidence, leave it blank. Step 5 handles blanks.

### Step 4: Find contacts
- Use the title list for the campaign in Part 3.
- Max 3 contacts per company. Aim for 1 safety title and 1 operations title per company.
- Stop at 150 contacts per campaign.

### Step 5: Assign Email 1 variant (MOST IMPORTANT STEP)
- Assign the E1 variant at the COMPANY level. Every contact at the same company gets the same E1 variant.
- Only assign a variant if the company meets that variant's eligibility rule (Part 3 eligibility table) AND every merge variable used in that variant's copy is filled.
- Balance by contact count. Targets:
  - BM, HG, CN: 5 variants x 30 contacts
  - BR: 3 variants x 50 contacts
- Algorithm:
  1. For each company, list its eligible variants.
  2. Sort companies by number of eligible variants (fewest first).
  3. Assign each company to whichever of its eligible variants currently has the fewest contacts.
  4. If a company has zero eligible variants, drop it and replace it with another qualified company.
- Write the result to column `e1_variant` (example value: `BM-E1-V3`).

### Step 6: Validate (all checks must pass before Step 7)
- [ ] Every contact has exactly one `e1_variant`.
- [ ] All contacts at the same domain share the same `e1_variant`.
- [ ] Every assigned variant passes its eligibility rule for that company.
- [ ] No merge variable used by the contact's E1, E2 or E3 copy is blank.
- [ ] Variant counts are within 3 of the target.
- [ ] Enrichment values follow the formatting rules (Part 3).
- [ ] Render test: for 3 random contacts per variant, resolve spintax randomly and fill variables. Read the result. Confirm no leftover `{`, `}`, `|`, or `{{`, no blank spots, and the sentences read naturally.
- Print a summary table: variant ID, contact count, company count.

### Step 7: Create EmailBison campaigns
- Create ONE EmailBison campaign per E1 variant. Name it exactly as in the naming matrix (Part 5).
- Inside each campaign:
  - Step 1 = the single E1 variant for that campaign (subject + body).
  - Step 2 = all 3 E2 variants for that vertical, loaded as A/B variants within the step. Delay 3 days.
  - Step 3 = both E3 variants for that vertical, loaded as A/B variants within the step. Delay 3 days.
- E2 and E3 reply in the same thread (no subject).
- Upload only the contacts whose `e1_variant` matches that campaign.
- Double check: the E1 body pasted into each campaign matches the copy ID in the campaign name.

### Step 8: Output
- One CSV per campaign with the columns in Part 6.
- One summary file listing every EmailBison campaign created, its copy IDs, and its contact count.

---

## PART 3: VERTICALS

### BM: Building Materials (distribution yards)

**NAICS:**
| Code | Description |
|---|---|
| 423310 | Lumber, plywood, millwork, wood panel wholesalers |
| 423320 | Brick, stone, related construction material wholesalers |
| 423330 | Roofing, siding, insulation wholesalers |
| 423390 | Other construction material wholesalers |
| 423510 | Metal service centers (rebar, structural, sheet) |
| 444180 (2022) / 444190 (2017) | Other building material dealers (pro lumber yards) |

**Gate (required):**
- `operates_yards` = yes (has physical yards/branches that load trucks)
- `branch_count` >= 2

**Enrichment:**
| Field | Rule |
|---|---|
| `city` | City of their largest or most prominent US/CA yard. City name only, Title Case, no state. Fallback: HQ city. |
| `product_mix` | 2 main stocked product groups, lowercase, joined with "and". Example: `lumber and trusses`, `roofing and siding`, `rebar and wire mesh`. |
| `branch_count` | Integer. |
| `has_own_delivery_fleet` | yes / no / unclear (website mentions delivery trucks, boom trucks, or flatbed delivery). |

**Titles:** Safety Director, Director of Safety, Safety Manager, EHS Manager, EHS Director, Health and Safety Manager, Regional Safety Manager, VP of Operations, Director of Operations, Regional Operations Manager, Director of Branch Operations, Branch Operations Manager, Branch Manager, Yard Manager, Delivery Manager, Fleet Manager, Transportation Manager, Logistics Manager.

**E1 eligibility:**
| Variant | Angle | Eligible if |
|---|---|---|
| BM-E1-V1 | Your drivers | `has_own_delivery_fleet` = yes |
| BM-E1-V2 | Slow season | `city` filled |
| BM-E1-V3 | Top of the bundles | `product_mix` and `city` filled |
| BM-E1-V4 | Branch or corporate | `branch_count` >= 2 |
| BM-E1-V5 | Green yard crews | always |

---

### HG: Heavy Goods Manufacturers

**NAICS:**
| Code | Description |
|---|---|
| 331110 | Iron and steel mills |
| 331210 | Steel pipe and tube from purchased steel |
| 331221 | Rolled steel shapes |
| 331222 | Steel wire drawing |
| 331315 | Aluminum sheet, plate, foil |
| 331318 | Other aluminum rolling, drawing, extruding |
| 332311 | Prefabricated metal buildings and components |
| 332312 | Fabricated structural metal |
| 332313 | Plate work |
| 332420 | Heavy gauge metal tanks |
| 333111 | Farm machinery |
| 333120 | Construction machinery |
| 333131 | Mining machinery |
| 333924 | Industrial truck, tractor, trailer machinery |
| 336212 | Truck trailers |
| 327310 | Cement |
| 327331 | Concrete block and brick |
| 327332 | Concrete pipe |
| 327390 | Other concrete products (precast) |
| 327420 | Gypsum products |
| 321113 | Sawmills |
| 321212 | Softwood veneer and plywood |
| 321213 | Engineered wood members |
| 321214 | Truss manufacturing |
| 335929 | Other communication/energy wire (cable on reels) |

**Gate (required):**
- `ships_on_flatbed` = yes or likely (heavy, long, or oversized physical product)

**Enrichment:**
| Field | Rule |
|---|---|
| `city` | City of their largest US/CA plant. City name only, Title Case, no state. Fallback: HQ city. |
| `shipped_product` | Main finished product, lowercase, PLURAL, 1-3 words. Example: `steel joists`, `precast vaults`, `truck trailers`, `steel coils`. Must read correctly in "Your steel joists go out on flatbeds". |
| `plant_count` | Integer, digits only. |
| `is_coil_producer` | yes / no (main product is steel or aluminum coil). |

**Titles:** Plant Manager, Director of Operations, VP of Operations, Director of Manufacturing, Shipping Manager, Shipping and Receiving Manager, Traffic Manager, Logistics Manager, Director of Logistics, Safety Manager, Safety Director, EHS Manager, EHS Director, Corporate EHS Director, Plant Safety Manager, Health and Safety Manager.

**E1 eligibility:**
| Variant | Angle | Eligible if |
|---|---|---|
| HG-E1-V1 | Odd shapes | `shipped_product` and `city` filled AND `is_coil_producer` = no |
| HG-E1-V2 | Carrier drivers | `city` filled |
| HG-E1-V3 | Every plant | `plant_count` >= 2 |
| HG-E1-V4 | Trucks waiting | `city` filled |
| HG-E1-V5 | Steel proof | `shipped_product` and `city` filled |

---

### CN: Construction (equipment rental and dealers)

**NAICS:**
| Code | Description |
|---|---|
| 532412 | Construction, mining, forestry machinery rental and leasing |
| 532490 | Other commercial/industrial machinery rental |
| 423810 | Construction and mining machinery wholesalers (equipment dealers) |

**Gate (required):**
- `has_equipment_yard` = yes (branches or yards where machines are loaded and unloaded)
- `rents_earthmoving` = yes (excavators, loaders, dozers, skid steers). Drop aerial-only and tool-only rental.

**Enrichment:**
| Field | Rule |
|---|---|
| `city` | City of a main branch. City name only, Title Case, no state. Fallback: HQ city. |
| `offers_customer_pickup` | yes / likely / no (customers can haul rentals on their own trailers). |
| `branch_count` | Integer. |

**Titles:** Safety Director, Director of Safety, Safety Manager, Regional Safety Manager, EHS Manager, VP of Operations, Director of Operations, Regional Operations Manager, Branch Manager, Branch Operations Manager, Yard Manager, Fleet Manager, Transportation Manager, Director of Equipment, Equipment Manager.

**E1 eligibility:**
| Variant | Angle | Eligible if |
|---|---|---|
| CN-E1-V1 | 6am rush | `city` filled |
| CN-E1-V2 | Customer self-haul | `city` filled AND `offers_customer_pickup` = yes or likely |
| CN-E1-V3 | Twice per rental | always |
| CN-E1-V4 | Walking the tracks | `city` filled |
| CN-E1-V5 | Mud on the deck | `city` filled |

---

### BR: Broad Angle Test (warehousing)

Quick run uses warehousing only (the OSHA warehouse program covers these codes, so all 3 angles are valid for every contact).

**NAICS:**
| Code | Description |
|---|---|
| 493110 | General warehousing and storage |
| 493120 | Refrigerated warehousing and storage |
| 493130 | Farm product warehousing and storage |
| 493190 | Other warehousing and storage |

**Gate (required):**
- `has_physical_facility` = yes (operates warehouses or yards, not a pure broker)
- `flatbed_ops` = yes or likely (handles flatbed/open-deck freight, yard operations, or heavy/oversized goods). PENDING David: if TFP has a dry van/dock product, this gate can be removed.

**Enrichment:**
| Field | Rule |
|---|---|
| `city` | City of their main warehouse or yard. City name only, Title Case, no state. Fallback: HQ city. |

**Titles:** Site Manager, Site Director, Site Leader, General Manager, Director of Warehouse Operations, Warehouse Operations Manager, Warehouse Manager, Distribution Center Manager, Director of Operations, Transportation Manager, Director of Transportation, Safety Manager, EHS Manager, Safety Director, Health and Safety Manager.

**E1 eligibility:**
| Variant | Angle | Eligible if |
|---|---|---|
| BR-E1-V1 | OSHA warehouse program | `city` filled |
| BR-E1-V2 | Winter on the deck | always |
| BR-E1-V3 | 2027 budget | always |

---

## PART 4: COPY LIBRARY (FINAL, LOAD BY ID)

Variables used: `{{first_name}}`, `{{company_name}}`, `{{city}}`, `{{product_mix}}`, `{{shipped_product}}`, `{{plant_count}}`.
Signature: use the sender signature configured in EmailBison.

### BM: Building Materials

**BM-E1-V1 (Your drivers)**
```
Subject: {Drivers|Your drivers|Your delivery drivers}

{{first_name}}, {at a steel mill|at most mills|at a mill} the {guy|person|driver} tarping {is usually|tends to be|is often} a carrier's driver. At {{company_name}} {it's your own driver|it's one of your drivers|it's your own crew} strapping {bundles|the load|bundles down} before {a delivery|a run|the delivery}.

{If he falls, it's on your workers' comp.|If he slips, that's on your workers' comp.|A fall there lands on your workers' comp.} {What keeps him tied off on the deck?|What keeps him tied off up there?|Is anything keeping him tied off on that deck?}
```

**BM-E1-V2 (Slow season)**
```
Subject: {Winter|Slow season|Before spring rush}

{{first_name}}, {winter is|the winter months are|this time of year is} {the slow stretch|the quiet stretch|the slower season} for {most yards|a lot of yards|most branch yards}. {It's also the easiest time|It's also the best window|That also makes it the easiest time} to put fall protection in {before spring loads stack up.|before the spring rush hits.|before spring volume picks back up.}

{Worth sizing it|Want to size it|Would it make sense to size it} for {{company_name}}'s {{city}} yard {while it's quiet?|before things pick up?|during the slow weeks?}
```

**BM-E1-V3 (Top of the bundles)**
```
Subject: {Bundles|Stacked loads|Top of the bundles}

{{first_name}}, {strapping|tying down|securing} a full load of {{product_mix}} puts {your guys|your yard crew|your drivers} on top of the bundles, {not just the deck.|not just on the deck.|well above the deck.}

{That's higher than most people think.|That's a lot higher than it looks from the ground.|It's higher up than most folks realize.} {What catches them|What's there to catch them|What stops the fall} if they slip at your {{city}} yard?
```

**BM-E1-V4 (Branch or corporate)**
```
Subject: {Branch|Branch or corporate|Who decides this}

{{first_name}}, at {{company_name}}, {is fall protection for the yard decided|is the call on yard fall protection made|does yard fall protection come down} at each branch or at corporate?

{Asking because|Reason I ask is|I ask because} we keep drivers tied off while they strap loads, and {I'd rather talk to the right person first.|I'd like to reach the right person first.|I want to make sure I'm talking to the right person.}
```

**BM-E1-V5 (Green yard crews)**
```
Subject: {Yard crew|First month|Green yard guys}

{{first_name}}, {yard crews turn over fast|yard crews change a lot|turnover on yard crews is high}, and {the guy climbing a load in his first month|the guy in his first month on the yard|whoever is in his first month climbing loads} is {the one most likely to slip.|usually the one who slips.|the most likely to go over the side.}

{What keeps him tied off|Does anything keep him tied off|Is anything keeping him tied off} at {{company_name}}'s yards besides experience he doesn't have yet?
```

**BM-E2-V1 (The fine is the cheap part)**
```
{{first_name}}, {OSHA kept its penalty levels flat this year|OSHA didn't raise its penalties this year|OSHA held its fines flat this year}, so a serious citation {still tops out at|still caps at|maxes out at} $16,550.

{Most of our customers called us after a fall, not a fine.|Our customers usually call after a fall, not after a fine.|Most people call us after a fall, not after a fine.} {That's the part that costs real money.|The fall is what really costs money.|That's where the real cost is.}
```

**BM-E2-V2 (3-photo layout sketch)**
```
{{first_name}}, {one easy way to see if this fits|an easy way to check if this fits|a simple way to see if this makes sense for} {{company_name}}'s yards: {your team sends|someone sends us|you send} 3 photos of a loading area.

{We send back a sketch of the setup that fits your trailers.|We mark up which setup fits your trailers and send it back.|We send back a layout showing what would work for your trailers.} {Want me to send the details?|Should I send over how it works?|Want the details?}
```

**BM-E2-V3 (Routing)**
```
{{first_name}}, {is yard safety yours|do you own yard safety|is loading safety on your plate} at {{company_name}}, or {should I be talking to someone else?|is there someone better to talk to?|should I reach out to someone else?}

{Either way, a name helps.|A name would help either way.|Even just a name would help.}
```

**BM-E3-V1 (Redirect)**
```
{{first_name}}, {if fall protection at your yards sits with someone else|if yard fall protection is someone else's call|if someone else handles fall protection at your yards}, {pointing me to them would help a lot.|a name would help a lot.|I'd be grateful for a name.}

{If it's not a priority this year, a one-word reply is fine and I'll stop reaching out.|If the timing's off this year, just say so and I'll stop reaching out.|If it's not on the list this year, a quick no works and I'll step out of your inbox.}
```

**BM-E3-V2 (Try later)**
```
{{first_name}}, {sounds like the timing might be off|maybe this isn't the right season|guessing the timing isn't right}, {which is fair.|and that's fair.|no worries.}

{Want me to try back in a couple of months?|Should I check back in a couple of months?|Worth reaching back out in a couple of months?} {If it's a no, just say the word and I'll stop.|If it's a hard no, one word works and I'll stop.|If it's not for you, tell me and I'll stop.}
```

---

### HG: Heavy Goods Manufacturers

**HG-E1-V1 (Odd shapes)**
```
Subject: {Odd loads|Oddball loads|Tarping odd shapes}

{{first_name}}, tarping {{shipped_product}} {isn't like tarping steel coils.|is nothing like tarping coils.|is a lot harder than tarping coils.} {Odd shapes mean more climbing and more time up top.|Odd shapes mean more climbing and longer on the deck.|Irregular loads keep drivers up top longer.}

{We build fall protection setups for loads like yours that other suppliers don't offer.|We build setups for loads like yours that you won't find elsewhere.|Our setups are built for loads like yours, not just flat coils.} {Worth a look|Worth seeing|Worth a closer look} for your {{city}} plant?
```

**HG-E1-V2 (Carrier drivers)**
```
Subject: {Carriers|Carrier drivers|Drivers on your dock}

{{first_name}}, {most loads leaving your|most shipments out of your|most of the loads out of your} {{city}} plant {are tarped by a carrier's driver, not your own people.|are tarped by carrier drivers, not your crew.|are strapped and tarped by a carrier's driver.}

{If he falls on your dock, it's still your plant.|If that driver falls, it still happened at your plant.|A fall on your dock still lands on your plant.} {What keeps him tied off up there?|What keeps him tied off on the deck?|Is anything keeping him tied off up there?}
```

**HG-E1-V3 (Every plant)**
```
Subject: {Plants|Every plant|Plant by plant}

{{first_name}}, with {{plant_count}} plants shipping on flatbeds, {I'd bet each one|my guess is each one|odds are each one} handles tarping safety {a little differently.|its own way.|a bit differently.}

{Is that something corporate wants standardized, or does each plant own it?|Does corporate want one standard, or does each plant decide?|Is one standard across plants a goal, or is it plant by plant?}
```

**HG-E1-V4 (Trucks waiting)**
```
Subject: {Detention|Trucks waiting|Trucks stuck tarping}

{{first_name}}, every minute {a driver spends|a driver is up|drivers spend} tarping at your {{city}} plant {is a truck not leaving,|keeps a truck from leaving,|is a truck sitting still,} and {carriers charge for that wait.|carriers charge detention for it.|that wait shows up in the carrier's charges.}

{Our setup goes up in about 40 seconds.|Our setup takes about 40 seconds to put up.|Setup takes about 40 seconds.} {Would faster tarping help your shipping schedule?|Would that help keep trucks moving?|Would that help your shipping schedule?}
```

**HG-E1-V5 (Steel proof)**
```
Subject: {Steel|Steel Dynamics|Like Steel Dynamics}

{{first_name}}, {we put fall protection|we installed fall protection|we set up fall protection} where drivers tarp coils at Steel Dynamics' flat roll mills.

Your {{shipped_product}} {go out on flatbeds the same way.|ship the same way, on flatbeds.|leave on flatbeds the same way.} {Would something similar make sense|Would a similar setup work|Could something like that work} at your {{city}} plant?
```

**HG-E2-V1 (The fine is the cheap part)**
```
{{first_name}}, {OSHA kept its penalty levels flat this year|OSHA didn't raise its penalties this year|OSHA held its fines flat this year}, so a serious citation {still tops out at|still caps at|maxes out at} $16,550.

{Most of our customers called us after a fall, not a fine.|Our customers usually call after a fall, not after a fine.|Most people call us after a fall, not after a fine.} {That's the part that costs real money.|The fall is what really costs money.|That's where the real cost is.}
```

**HG-E2-V2 (3-photo layout sketch)**
```
{{first_name}}, {one easy way to see if this fits|an easy way to check if this fits|a simple way to see if this makes sense for} {{company_name}}'s plants: {your team sends|someone sends us|you send} 3 photos of a shipping area.

{We send back a sketch of the setup that fits your trailers.|We mark up which setup fits your trailers and send it back.|We send back a layout showing what would work for your trailers.} {Want me to send the details?|Should I send over how it works?|Want the details?}
```

**HG-E2-V3 (Routing)**
```
{{first_name}}, {is shipping safety yours|do you own shipping safety|is loading safety on your plate} at {{company_name}}, or {should I be talking to someone else?|is there someone better to talk to?|should I reach out to someone else?}

{Either way, a name helps.|A name would help either way.|Even just a name would help.}
```

**HG-E3-V1 (Redirect)**
```
{{first_name}}, {if fall protection at your plants sits with someone else|if shipping-area fall protection is someone else's call|if someone else handles fall protection at your plants}, {pointing me to them would help a lot.|a name would help a lot.|I'd be grateful for a name.}

{If it's not a priority this year, a one-word reply is fine and I'll stop reaching out.|If the timing's off this year, just say so and I'll stop reaching out.|If it's not on the list this year, a quick no works and I'll step out of your inbox.}
```

**HG-E3-V2 (Try later)**
```
{{first_name}}, {sounds like the timing might be off|maybe this isn't the right season|guessing the timing isn't right}, {which is fair.|and that's fair.|no worries.}

{Want me to try back in a couple of months?|Should I check back in a couple of months?|Worth reaching back out in a couple of months?} {If it's a no, just say the word and I'll stop.|If it's a hard no, one word works and I'll stop.|If it's not for you, tell me and I'll stop.}
```

---

### CN: Construction (equipment rental and dealers)

**CN-E1-V1 (6am rush)**
```
Subject: {Mornings|6am rush|Machines going out}

{{first_name}}, at 6am {when every machine is going out|when the yard is loading out|when everything's headed out}, {nobody's waiting on fall protection.|nobody has time for fall protection.|fall protection is the first thing skipped.}

{Ours sets up in about 40 seconds.|Our setup goes up in about 40 seconds.|Ours takes about 40 seconds to set up.} {Would that fly|Would that work|Could that work} at your {{city}} branch?
```

**CN-E1-V2 (Customer self-haul)**
```
Subject: {Pickups|Customer pickups|Contractors loading up}

{{first_name}}, when a contractor hauls a rental out on his own trailer, {he's chaining it down|he's the one chaining it down|he chains it down} in your {{city}} yard.

{If he falls off that deck, it's still your yard.|If he goes off that deck, it still happened in your yard.|A fall off that deck still happens on your property.} {Is anything there to tie him off?|Is there anything to tie him off to?|What's there to tie him off?}
```

**CN-E1-V3 (Twice per rental)**
```
Subject: {Twice|Twice per rental|Out and back}

{{first_name}}, every machine that goes out {comes back,|also comes back,|eventually comes back,} so your guys {chain and unchain on the deck twice per rental.|are up on the deck twice for every rental.|climb the deck twice per rental.}

{That's a lot of trips up there across your branches.|Across all your branches, that adds up fast.|Multiply that by every branch and it adds up.} {What keeps them tied off?|What keeps them tied off up there?|Is anything keeping them tied off?}
```

**CN-E1-V4 (Walking the tracks)**
```
Subject: {Tracks|Chaining excavators|Walking the tracks}

{{first_name}}, chaining an excavator {means walking the deck around the tracks|means working the deck around the tracks|has your guys walking the deck around the tracks} {with nothing to grab.|with nothing to hold onto.|with nothing to catch them.}

{What keeps your guys tied off|What keeps your crew tied off|Is anything keeping your crew tied off} at the {{city}} branch {while they do it?|while they chain down?|during that?}
```

**CN-E1-V5 (Mud on the deck)**
```
Subject: {Mud|Muddy decks|Returns caked in mud}

{{first_name}}, machines {come back to|roll back into|return to} your {{city}} branch caked in mud, and {that mud ends up on the trailer deck.|the trailer deck ends up slick with it.|most of it ends up on the trailer deck.}

{Somebody still has to walk it to unchain.|Someone still has to walk that deck to unchain.|Your guys still have to walk it to unchain.} {What keeps them from going over the side?|What stops them from going over the side?|What catches them if they slip?}
```

**CN-E2-V1 (The fine is the cheap part)**
```
{{first_name}}, {OSHA kept its penalty levels flat this year|OSHA didn't raise its penalties this year|OSHA held its fines flat this year}, so a serious citation {still tops out at|still caps at|maxes out at} $16,550.

{Most of our customers called us after a fall, not a fine.|Our customers usually call after a fall, not after a fine.|Most people call us after a fall, not after a fine.} {That's the part that costs real money.|The fall is what really costs money.|That's where the real cost is.}
```

**CN-E2-V2 (3-photo layout sketch)**
```
{{first_name}}, {one easy way to see if this fits|an easy way to check if this fits|a simple way to see if this makes sense for} {{company_name}}'s branches: {your team sends|someone sends us|you send} 3 photos of a branch yard.

{We send back a sketch of the setup that fits your trailers.|We mark up which setup fits your trailers and send it back.|We send back a layout showing what would work for your trailers.} {Want me to send the details?|Should I send over how it works?|Want the details?}
```

**CN-E2-V3 (Routing)**
```
{{first_name}}, {is yard safety yours|do you own yard safety|is branch yard safety on your plate} at {{company_name}}, or {should I be talking to someone else?|is there someone better to talk to?|should I reach out to someone else?}

{Either way, a name helps.|A name would help either way.|Even just a name would help.}
```

**CN-E3-V1 (Redirect)**
```
{{first_name}}, {if fall protection at your branches sits with someone else|if yard fall protection is someone else's call|if someone else handles fall protection at your branches}, {pointing me to them would help a lot.|a name would help a lot.|I'd be grateful for a name.}

{If it's not a priority this year, a one-word reply is fine and I'll stop reaching out.|If the timing's off this year, just say so and I'll stop reaching out.|If it's not on the list this year, a quick no works and I'll step out of your inbox.}
```

**CN-E3-V2 (Try later)**
```
{{first_name}}, {sounds like the timing might be off|maybe this isn't the right season|guessing the timing isn't right}, {which is fair.|and that's fair.|no worries.}

{Want me to try back in a couple of months?|Should I check back in a couple of months?|Worth reaching back out in a couple of months?} {If it's a no, just say the word and I'll stop.|If it's a hard no, one word works and I'll stop.|If it's not for you, tell me and I'll stop.}
```

---

### BR: Broad Angle Test (warehousing)

**BR-E1-V1 (OSHA warehouse program)**
```
Subject: {Docks|Warehouse program|OSHA warehouse program}

{{first_name}}, OSHA {extended|re-upped|reissued} its warehouse inspection program in August {and stretched it to 2031.|and it runs through 2031.|for five more years, through 2031.} {Falls are on the inspection checklist.|Falls are one of the hazards on the checklist.|Slips, trips and falls are on the checklist.}

{What keeps someone tied off|How is someone kept tied off|What protects someone} when they're up on a trailer at {{company_name}}'s {{city}} site?
```

**BR-E1-V2 (Winter on the deck)**
```
Subject: {Ice|Icy decks|Tarping in January}

{{first_name}}, {tarping in January means|January tarping means|winter tarping means} ice on the deck and wind pulling at the tarp, and someone at {{company_name}} {still has to climb up.|still has to go up there.|still has to be up top.}

{We build fall protection for that spot.|We build fall protection for exactly that spot.|That's the spot our fall protection is built for.} {Our side rolling panel sets up in about 40 seconds, so crews actually use it in the cold.|Our side rolling panel goes up in about 40 seconds, so it's used even in the cold.|Our side rolling panel takes about 40 seconds, so it doesn't sit idle in the cold.}

{Worth a layout sketch from 3 photos of your loading area?|Want a layout sketch from 3 photos of your loading area?|Should we sketch a layout from 3 photos of your loading area?}
```

**BR-E1-V3 (2027 budget)**
```
Subject: {Budget|2027 budget|2027 safety budget}

{{first_name}}, {plenty of safety teams|a lot of safety teams|many safety teams} lock next year's capital requests before the holidays.

{If loading-area fall protection isn't in|If fall protection for your loading areas isn't in|If loading-area fall protection hasn't made it into} {{company_name}}'s 2027 budget yet, a layout sketch {would give you a real number.|gives you a real number to put in.|would put a real number on it.} {Your team sends 3 photos, we send back the setup and a price range.|Send us 3 photos and we'll send back the setup and a price range.|Your team shares 3 photos and we return the setup with a price range.}

{Worth doing before the budget closes?|Want to do that before budgets close?|Worth it before the budget locks?}
```

**BR-E2-V1 (The fine is the cheap part)**
```
{{first_name}}, {OSHA kept its penalty levels flat this year|OSHA didn't raise its penalties this year|OSHA held its fines flat this year}, so a serious citation {still tops out at|still caps at|maxes out at} $16,550.

{Most of our customers called us after a fall, not a fine.|Our customers usually call after a fall, not after a fine.|Most people call us after a fall, not after a fine.} {That's the part that costs real money.|The fall is what really costs money.|That's where the real cost is.}
```

**BR-E2-V2 (3-photo layout sketch)**
```
{{first_name}}, {one easy way to see if this fits|an easy way to check if this fits|a simple way to see if this makes sense for} {{company_name}}'s sites: {your team sends|someone sends us|you send} 3 photos of a loading area.

{We send back a sketch of the setup that fits your trailers.|We mark up which setup fits your trailers and send it back.|We send back a layout showing what would work for your trailers.} {Want me to send the details?|Should I send over how it works?|Want the details?}
```

**BR-E2-V3 (Routing)**
```
{{first_name}}, {is loading-area safety yours|do you own loading-area safety|is loading safety on your plate} at {{company_name}}, or {should I be talking to someone else?|is there someone better to talk to?|should I reach out to someone else?}

{Either way, a name helps.|A name would help either way.|Even just a name would help.}
```

**BR-E3-V1 (Redirect)**
```
{{first_name}}, {if fall protection at your sites sits with someone else|if loading-area fall protection is someone else's call|if someone else handles fall protection at your sites}, {pointing me to them would help a lot.|a name would help a lot.|I'd be grateful for a name.}

{If it's not a priority this year, a one-word reply is fine and I'll stop reaching out.|If the timing's off this year, just say so and I'll stop reaching out.|If it's not on the list this year, a quick no works and I'll step out of your inbox.}
```

**BR-E3-V2 (Try later)**
```
{{first_name}}, {sounds like the timing might be off|maybe this isn't the right season|guessing the timing isn't right}, {which is fair.|and that's fair.|no worries.}

{Want me to try back in a couple of months?|Should I check back in a couple of months?|Worth reaching back out in a couple of months?} {If it's a no, just say the word and I'll stop.|If it's a hard no, one word works and I'll stop.|If it's not for you, tell me and I'll stop.}
```

---

## PART 5: EMAILBISON CAMPAIGN NAMING MATRIX (18 CAMPAIGNS)

| EmailBison campaign name | Step 1 (E1) | Step 2 (A/B) | Step 3 (A/B) | Contacts |
|---|---|---|---|---|
| TFP_BM_E1V1_YourDrivers | BM-E1-V1 | BM-E2-V1, V2, V3 | BM-E3-V1, V2 | 30 |
| TFP_BM_E1V2_SlowSeason | BM-E1-V2 | BM-E2-V1, V2, V3 | BM-E3-V1, V2 | 30 |
| TFP_BM_E1V3_Bundles | BM-E1-V3 | BM-E2-V1, V2, V3 | BM-E3-V1, V2 | 30 |
| TFP_BM_E1V4_BranchOrCorp | BM-E1-V4 | BM-E2-V1, V2, V3 | BM-E3-V1, V2 | 30 |
| TFP_BM_E1V5_GreenCrews | BM-E1-V5 | BM-E2-V1, V2, V3 | BM-E3-V1, V2 | 30 |
| TFP_HG_E1V1_OddShapes | HG-E1-V1 | HG-E2-V1, V2, V3 | HG-E3-V1, V2 | 30 |
| TFP_HG_E1V2_CarrierDrivers | HG-E1-V2 | HG-E2-V1, V2, V3 | HG-E3-V1, V2 | 30 |
| TFP_HG_E1V3_EveryPlant | HG-E1-V3 | HG-E2-V1, V2, V3 | HG-E3-V1, V2 | 30 |
| TFP_HG_E1V4_TrucksWaiting | HG-E1-V4 | HG-E2-V1, V2, V3 | HG-E3-V1, V2 | 30 |
| TFP_HG_E1V5_SteelProof | HG-E1-V5 | HG-E2-V1, V2, V3 | HG-E3-V1, V2 | 30 |
| TFP_CN_E1V1_6amRush | CN-E1-V1 | CN-E2-V1, V2, V3 | CN-E3-V1, V2 | 30 |
| TFP_CN_E1V2_SelfHaul | CN-E1-V2 | CN-E2-V1, V2, V3 | CN-E3-V1, V2 | 30 |
| TFP_CN_E1V3_TwicePerRental | CN-E1-V3 | CN-E2-V1, V2, V3 | CN-E3-V1, V2 | 30 |
| TFP_CN_E1V4_Tracks | CN-E1-V4 | CN-E2-V1, V2, V3 | CN-E3-V1, V2 | 30 |
| TFP_CN_E1V5_Mud | CN-E1-V5 | CN-E2-V1, V2, V3 | CN-E3-V1, V2 | 30 |
| TFP_BR_E1V1_OSHAWarehouse | BR-E1-V1 | BR-E2-V1, V2, V3 | BR-E3-V1, V2 | 50 |
| TFP_BR_E1V2_Winter | BR-E1-V2 | BR-E2-V1, V2, V3 | BR-E3-V1, V2 | 50 |
| TFP_BR_E1V3_Budget | BR-E1-V3 | BR-E2-V1, V2, V3 | BR-E3-V1, V2 | 50 |

Rule: a campaign's Step 2 and Step 3 copy must come from the SAME vertical code as its name. Never load BM E2 copy into an HG campaign, and so on.

---

## PART 6: OUTPUT CSV COLUMNS (ONE FILE PER VERTICAL)

```
campaign_code, e1_variant, emailbison_campaign_name, size_tier,
company_name, domain, naics, employee_count,
city, product_mix, shipped_product, plant_count, branch_count,
has_own_delivery_fleet, is_coil_producer, offers_customer_pickup,
first_name, last_name, title, email, linkedin_url
```

Leave columns that do not apply to a vertical blank.

---

## PART 7: OPEN ITEMS (DO NOT BLOCK THE QUICK RUN UNLESS NOTED)

1. HG-E1-V5 names Steel Dynamics. HOLD the `TFP_HG_E1V5_SteelProof` campaign until David approves. If not approved, reassign its contacts across HG V1-V4.
2. HG-E1-V1 says "setups other suppliers don't offer." Confirm with David before sending.
3. BR gate `flatbed_ops` depends on whether TFP has a dry van/dock product.
4. BR-E1-V2 and BR-E1-V3 offer a 3-photo layout sketch and a price range. Confirm TFP will deliver both.
