"""Push the current spec copy into the four quick-run campaigns' existing steps (ids kept), changing only the steps whose text differs.
Mapping relies on how quickrun_campaigns.create() built them: orders 1 to 3 are the Email 1, 2, 3 main steps, orders 4 and up are the variants in plan order.
Usage: python scripts/tfp/quickrun_update_steps.py check|apply   (needs EMAILBISON_API_KEY_TFP)
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(__file__)
spec = importlib.util.spec_from_file_location("qc", os.path.join(HERE, "quickrun_campaigns.py"))
qc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qc)
spec2 = importlib.util.spec_from_file_location("qe", os.path.join(HERE, "quickrun_cta_edit.py"))
qe = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(qe)
TARGETS = qe.TARGETS
CAMPAIGNS = {"BM": 2210, "HG": 2211, "CN": 2212, "BR": 2213}


def main(mode):
    copy = qc.load_copy()
    for code, cid in CAMPAIGNS.items():
        rows = qc.plan(code, copy)
        ordered = [r for r in rows if r["main"]] + [r for r in rows if not r["main"]]
        cur = qc.api("GET", f"/api/campaigns/v1.1/{cid}/sequence-steps")["data"]
        steps = sorted(cur["sequence_steps"], key=lambda s: s["order"])
        assert len(steps) == len(ordered), (code, len(steps), len(ordered))
        changed = []
        for step, row in zip(steps, ordered):
            # Only the copy the CTA edit changed: other steps may have been edited by hand in EmailBison (BM-E1-V1 was, 2026-10-27 12:15) and must not be overwritten.
            if row["copy_id"] in TARGETS and step["email_body"].strip() != row["email_body"].strip():
                changed.append(dict(id=step["id"], order=step["order"], active=step["active"], variant=step["variant"], variant_from_step_id=step.get("variant_from_step_id"),
                                    wait_in_days=step["wait_in_days"], thread_reply=step["thread_reply"], email_subject=step["email_subject"], email_body=row["email_body"]))
                print(f"{code} {cid}: {row['copy_id']} (step {step['id']}) changes")
        if mode == "apply" and changed:
            qc.api("PUT", f"/api/campaigns/v1.1/sequence-steps/{cur['sequence_id']}", {"title": "TFP Cold Email Sequence", "sequence_steps": changed})
            print(f"{code} {cid}: updated {len(changed)} steps")
        elif not changed:
            print(f"{code} {cid}: nothing to change")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "check")
