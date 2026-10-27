"""Lower-friction CTAs for the quick-run copy (Emmanuel, 2026-10-27): options A to F, 3 per spin. Edits the spec file in place, by copy ID.
Changes: BR E1 V1 V2 V3, BM E1 V2 V3 V4 V5, and E2 V2 in all four lists (the 3-photo sketch ask is gone). BM E1 V1 is untouched (waiting on Emmanuel's wording).
Usage: python scripts/tfp/quickrun_cta_edit.py
"""
import os
import re

SPEC = os.path.join(os.path.dirname(__file__), "..", "..", "clients", "tfp", "spec", "TFP_Quick_Run_Campaign_Shell.md")
CTA_E1 = "{Open to a short video of it in action?|Want me to send a 30-second video of it?|Mind if I send over a quick video?}"
CTA_E2 = "{Should I send the short clip?|Want to see a 1-minute video of it on a flatbed?|Reply \"video\" and I'll send it over.}"
CTA_BM5 = "{Want me to send a short video of how crews stay tied off?|Open to a short video of it in action?|Mind if I send over a quick video?}"
NOUN = {"BM": "yards", "HG": "plants", "CN": "branches", "BR": "sites"}
BR_V3_P2 = "If loading-area fall protection isn't in {{company_name}}'s 2027 budget yet, it helps to see what it looks like before the numbers lock."


def e2v2(code):
    return ("{{first_name}}, {circling back on fall protection for|following up on fall protection at|a quick follow-up on fall protection for} "
            "{{company_name}}'s " + NOUN[code] + ".\n\n" + CTA_E2)


def edit(cid, body):
    parts = body.split("\n\n")
    if cid == "BR-E1-V1":
        parts[2] = CTA_E1
    elif cid == "BR-E1-V2":
        parts[3] = CTA_E1
    elif cid == "BR-E1-V3":
        parts[2], parts[3] = BR_V3_P2, CTA_E1
    elif cid == "BM-E1-V2":
        parts[2] = CTA_E1
    elif cid in ("BM-E1-V3", "BM-E1-V4"):
        parts.append(CTA_E1)
    elif cid == "BM-E1-V5":
        parts.append(CTA_BM5)
    elif cid.endswith("-E2-V2"):
        return e2v2(cid[:2])
    return "\n\n".join(parts)


TARGETS = {"BR-E1-V1", "BR-E1-V2", "BR-E1-V3", "BM-E1-V2", "BM-E1-V3", "BM-E1-V4", "BM-E1-V5", "BM-E2-V2", "HG-E2-V2", "CN-E2-V2", "BR-E2-V2"}


def main():
    text = open(SPEC, encoding="utf-8").read()
    done = []

    def sub(m):
        cid = m.group(1)
        if cid not in TARGETS:
            return m.group(0)
        done.append(cid)
        return m.group(0).replace(m.group(3), edit(cid, m.group(3).strip("\n")) + "\n")

    text = re.sub(r"\*\*([A-Z]{2}-E\d-V\d) \(([^)]*)\)\*\*\s*```\n(.*?)```", sub, text, flags=re.S)
    assert set(done) == TARGETS, set(TARGETS) - set(done)
    open(SPEC, "w", encoding="utf-8").write(text)
    print("edited", len(done), "copy blocks:", ", ".join(sorted(done)))


if __name__ == "__main__":
    main()
