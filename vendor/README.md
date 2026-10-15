# vendor/

`aiark_company_search.py` is copied verbatim from the BuzzLead training repo
(Claude_Code_GTM_Training/aiark_company_search.py) as of 2026-09-07, per the build
decision to reuse it as-is rather than rewrite the AI Ark client.

Do not edit it here. If it needs a fix, fix it upstream and re-copy, so the two
copies do not drift.

`email_waterfall.py` and `aiark_search.py` are copied verbatim from the training repo
(2026-09-09), same rule as above: fix upstream and re-copy, never edit here.

- `email_waterfall.py` supplies the VERIFICATION half of the agreed chain:
  MillionVerifier v3 triage -> TryKit validator -> BounceBan fallback. Contact FINDING
  is ours (QuickEnrich -> AI.ARK -> Apollo), so the waterfall is handed an email and
  asked to verify it rather than to source it.
- `aiark_search.py` supplies `search_people()` for the AI.ARK finder step, in the
  filter shape the BuzzLead API docs describe (job_titles / seniority / industries /
  department, with a tight -> recovery -> broad fallback).

`cost_tracker.py`, `config_storage.py`, `file_utils.py`, `output_formatter.py` are
leaf dependencies of `aiark_search.py`, copied for the same reason. None of them
import anything else local, so the vendored set is closed.
