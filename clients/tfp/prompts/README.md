# Lumion prompt arms

Three arms, one folder each: pdf-application, after-5pm, next-start-date.
Each folder needs three files: prompt_research.txt, prompt_email1.txt, prompt_email2.txt.
Until all three arms hold their files the engine reports the profile as incomplete and refuses to send. That is deliberate.

Format of each file (see LUMION-SPEC.md section 6.5 and the ProQ prompts for a working example):
header lines SOURCE, VARIANT, model, temperature, max_tokens, then `SYSTEM PROMPT (verbatim):` fenced by two `---`,
then `USER MESSAGE TEMPLATE:` with slots.
Research slots: company_profile_data, person_profile_data, website_text, serper_news_results, serper_competitor_results, today, cutoff.
Email slots: research_report, client_overview, company_name, job_title, email_1_output.

Every Email prompt must embed the spec guardrail block and the client bans, and the lengths 380 to 460 (email 1) and 600 to 699 (email 2).
Because contacts are dealt to arms at random, every research prompt must output the angle's signal as found on the school's own site
(or NONE FOUND) and every email prompt must write a signal-free opener when the signal is NONE FOUND. Never assert a fact the research did not find.
