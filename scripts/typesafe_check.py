"""Validate the TypeSafe key and the API contract. No database, no pipeline, one call.

Run this before shadow_enforce.py. It proves three things in about half a second:
the key works, the request shape in clients/typesafe.py is right, and the response
carries the probabilities the threshold logic depends on.

    python scripts/typesafe_check.py

THE TEST CASE IS NOT ARBITRARY. kelleykronenberg.com is a real company from the real
medical-malpractice run, and the Haiku gate DROPPED it with the reason "Full-service
multi-practice firm with 40+ practice areas; not genuinely specialized in medical
malpractice/health law". ENFORCE_SYSTEM in stages/s3_companies.py says, in as many
words, that a substantial practice counts, that exclusive specialisation is never a
reason to drop, and that "a full-service law firm with a medical-malpractice practice
is in". The gate contradicted its own instruction on a 400-person firm that lists
medical malpractice defence as a named practice area.

So there is a right answer here, and it is KEEP. If Jev says DROP too, that is a real
result and worth knowing before anyone builds on this.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Load .env the way the app does, so this works from a bare shell.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

from app.clients import typesafe                      # noqa: E402
from app.stages.s2_expansion import Vertical          # noqa: E402

VERTICAL = Vertical(
    name="Medical Malpractice Law",
    rationale=("US law firms with a real medical-malpractice or health-law practice, "
               "who would want press coverage."),
    employee_min=10, employee_max=500,
    disqualifiers=["staffing firms", "legal software vendors", "expert-witness brokers"],
)

COMPANY = {
    "domain": "kelleykronenberg.com",
    "name": "Kelley Kronenberg",
    "description": ("Full-service business law firm with over 400 employees across 13 "
                    "offices. Practice areas include medical malpractice defense, "
                    "healthcare law, insurance defense, labor and employment, real "
                    "estate, and commercial litigation."),
    "employee_count": 400,
    "location": "Fort Lauderdale, FL, United States",
    "industry": "Law Practice",
}

HAIKU_SAID = ("DROP", "Full-service multi-practice firm with 40+ practice areas; "
                      "not genuinely specialized in medical malpractice/health law")


def main() -> None:
    if not typesafe.configured():
        sys.exit("TYPESAFE_API_KEY is not set. Put it in .env or export it.")

    print(f"company: {COMPANY['name']} ({COMPANY['domain']})")
    print(f"haiku:   {HAIKU_SAID[0]} -- {HAIKU_SAID[1]}\n")

    try:
        judgment = typesafe.judge(VERTICAL, COMPANY)
    except Exception as exc:                           # noqa: BLE001
        sys.exit(f"CALL FAILED: {type(exc).__name__}: {exc}")

    decision, reason = typesafe.decide(judgment)
    print("jev raw:", json.dumps(judgment, indent=2))
    print(f"\njev:     {decision} -- {reason}")
    print(f"agree:   {decision == HAIKU_SAID[0]}")

    if decision == "KEEP":
        print("\nJev kept a company the current gate threw away, which is the failure "
              "mode worth measuring at scale. Next: scripts/shadow_enforce.py")
    elif decision == HAIKU_SAID[0]:
        print("\nBoth judges agree on DROP. The key and the contract work, but this "
              "particular case is not the win it looked like. Still run the shadow "
              "test -- one company proves nothing either way.")


if __name__ == "__main__":
    main()
