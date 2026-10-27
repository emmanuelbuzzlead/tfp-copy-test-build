"""Create the four TFP quick-run campaigns (BM, HG, CN, BR) as drafts, copy loaded by ID from the spec.

One campaign per list: Email 1 variants run as A/B variants of step 1, Email 2 has three variants, Email 3 two.
Copy is loaded word for word from clients/tfp/spec/TFP_Quick_Run_Campaign_Shell.md; only the merge variables are converted to EmailBison's
single-brace form ({{first_name}} -> {FIRST_NAME}), and the sender signature and the TFP opt-out line are added, as on the winning campaigns.
Usage: python scripts/tfp/quickrun_campaigns.py preview|create [BM HG CN BR]
"""
import os, random, re, sys
import requests

SPEC = os.path.join(os.path.dirname(__file__), "..", "..", "clients", "tfp", "spec", "TFP_Quick_Run_Campaign_Shell.md")
BASE = "https://personal.buzzlead.io"
MODEL = 2107                      # the winning A10 campaign: duplicating keeps its tuned settings; the copy starts as a draft with no senders and no leads
SIG = "{SENDER_EMAIL_SIGNATURE}"
OPT = 'Just say "{recall|fall off}" if you {don\'t want to hear from me again|want me to leave you alone|would rather not hear from me|want me to stop contacting you}.'
HOLD = {"HG-E1-V5"}               # names Steel Dynamics: held until David approves (spec part 7, item 1)
NAMES = {"BM": "Building Materials", "HG": "Heavy Goods Manufacturers", "CN": "Construction", "BR": "Broad Angle Test (warehousing)"}
VARS = {"first_name": "FIRST_NAME", "company_name": "COMPANY", "city": "CITY", "product_mix": "PRODUCT_MIX", "shipped_product": "SHIPPED_PRODUCT", "plant_count": "PLANT_COUNT"}
SAMPLE = {"FIRST_NAME": "Dana", "COMPANY": "Acme Steel", "CITY": "Tulsa", "PRODUCT_MIX": "lumber and trusses", "SHIPPED_PRODUCT": "steel joists", "PLANT_COUNT": "4", "SENDER_EMAIL_SIGNATURE": "Sam"}


def load_copy() -> dict[str, tuple[str, list[str]]]:
    text = open(SPEC, encoding="utf-8").read()
    out = {}
    for m in re.finditer(r"\*\*([A-Z]{2}-E\d-V\d) \([^)]*\)\*\*\s*```\n(.*?)```", text, re.S):
        body = m.group(2).strip("\n")
        subject = ""
        if body.startswith("Subject:"):
            first, _, body = body.partition("\n")
            subject = first[len("Subject:"):].strip()
        for src, dst in VARS.items():
            body = body.replace("{{" + src + "}}", "{" + dst + "}")
        out[m.group(1)] = (subject, [p.strip() for p in body.strip().split("\n\n") if p.strip()])
    return out


def html(paras: list[str]) -> str:
    return "<p><br></p>".join(f"<p>{p}</p>" for p in paras + [SIG, OPT])


def render(s: str, rng: random.Random) -> str:
    while True:
        t = re.sub(r"\{([^{}|]*\|[^{}]*)\}", lambda m: rng.choice(m.group(1).split("|")), s)
        if t == s:
            break
        s = t
    for k, v in SAMPLE.items():
        s = s.replace("{" + k + "}", v)
    return s


def plan(code: str, copy: dict) -> list[dict]:
    e1 = sorted(k for k in copy if k.startswith(f"{code}-E1-") and k not in HOLD)
    e2 = sorted(k for k in copy if k.startswith(f"{code}-E2-"))
    e3 = sorted(k for k in copy if k.startswith(f"{code}-E3-"))
    rows = []
    for stage, ids, wait, thread in ((1, e1, 1, False), (2, e2, 3, True), (3, e3, 3, True)):
        for n, cid in enumerate(ids):
            subj, paras = copy[cid]
            rows.append(dict(stage=stage, main=(n == 0), copy_id=cid, wait_in_days=wait, thread_reply=thread, email_subject=subj or e1_subject(copy, e1), email_body=html(paras)))
    return rows


def e1_subject(copy, e1):
    return copy[e1[0]][0]


def check(rows):
    problems = []
    rng = random.Random(7)
    for r in rows:
        for _ in range(6):
            t = render(r["email_body"] + " " + r["email_subject"], rng)
            if re.search(r"[{}|]|\{\{", t):
                problems.append((r["copy_id"], t[:120])); break
    return problems


def api(method, path, body=None):
    h = {"Authorization": f"Bearer {os.environ['EMAILBISON_API_KEY_TFP']}", "Accept": "application/json", "Content-Type": "application/json"}
    r = requests.request(method, BASE + path, headers=h, json=body, timeout=90)
    if r.status_code >= 300:
        raise RuntimeError(f"{method} {path} -> {r.status_code} {r.text[:300]}")
    return r.json() if r.text else {}


def create(code, rows):
    name = f"TFP | {code} {NAMES[code]} | quick run 27 Oct"
    for c in api("GET", "/api/campaigns").get("data", []):
        if c.get("name") == name:
            raise RuntimeError(f"{name} already exists as {c['id']}")
    dup = api("POST", f"/api/campaigns/{MODEL}/duplicate")
    cid = (dup.get("data") or dup)["id"]
    api("PATCH", f"/api/campaigns/{cid}/update", {"name": name})
    cur = api("GET", f"/api/campaigns/v1.1/{cid}/sequence-steps")["data"]
    seq_id, steps = cur["sequence_id"], sorted(cur["sequence_steps"], key=lambda s: s["order"])
    mains = [r for r in rows if r["main"]]
    assert len(steps) >= 3 and len(mains) == 3, (len(steps), len(mains))
    put = [dict(id=steps[i]["id"], order=i + 1, active=True, variant=False, variant_from_step_id=None, **{k: m[k] for k in ("wait_in_days", "thread_reply", "email_subject", "email_body")}) for i, m in enumerate(mains)]
    for extra in steps[3:]:
        api("DELETE", f"/api/campaigns/sequence-steps/{extra['id']}")
    api("PUT", f"/api/campaigns/v1.1/sequence-steps/{seq_id}", {"title": "TFP Cold Email Sequence", "sequence_steps": put})
    parent = {m["stage"]: put[i]["id"] for i, m in enumerate(mains)}
    variants = [dict(order=4 + i, active=True, variant=True, variant_from_step_id=parent[r["stage"]], **{k: r[k] for k in ("wait_in_days", "thread_reply", "email_subject", "email_body")})
                for i, r in enumerate(x for x in rows if not x["main"])]
    if variants:
        api("POST", f"/api/campaigns/v1.1/{cid}/sequence-steps", {"title": "TFP Cold Email Sequence", "sequence_steps": variants})
    return cid, name


if __name__ == "__main__":
    mode = sys.argv[1]
    codes = sys.argv[2:] or ["BM", "HG", "CN", "BR"]
    copy = load_copy()
    for code in codes:
        rows = plan(code, copy)
        bad = check(rows)
        counts = {s: sum(1 for r in rows if r["stage"] == s) for s in (1, 2, 3)}
        print(f"{code}: E1 {counts[1]} versions, E2 {counts[2]}, E3 {counts[3]}; problems: {bad or 'none'}")
        if mode == "preview":
            r = rows[0]; print("  sample:", render(r["email_subject"], random.Random(1)), "|", re.sub(r"</p><p><br></p><p>", " // ", re.sub(r"^<p>|</p>$", "", render(r["email_body"], random.Random(1))))[:420])
        elif mode == "create" and not bad:
            print("  created", create(code, rows))
