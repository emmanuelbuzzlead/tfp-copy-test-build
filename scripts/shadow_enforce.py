"""Compare the Haiku enforcement gate against a Jev judgment. Promote nothing.

The point is to answer one question with data instead of estimates: where do the two
judges disagree, and which one is right when they do?

FREE ON THE HAIKU SIDE. staged_companies already stores the verdict Haiku gave and the
AI Ark record it saw, so this re-judges nothing: it reads decisions already paid for and
asks Jev the same question about the same record. Only the Jev calls cost anything, and
at $0.042/MTok input with free output a 300-company run is a rounding error.

WHAT IT DOES NOT DO. It does not write to staged_companies, does not change any verdict,
and does not spend an enrichment credit. Stage 3 is untouched until the CSV says
something.

    railway run --service Postgres python scripts/shadow_enforce.py --list
    railway run --service Postgres python scripts/shadow_enforce.py \n        --vertical "Medical Device & Diagnostics Manufacturers" --limit 300

READ THE OUTPUT THIS WAY. Agreement rate is the least interesting number in the file.
The rows that matter are the disagreements, and they split into two piles:

  haiku=DROP, jev=KEEP   -> companies the current gate is throwing away. This is the
                            expensive failure. ENFORCE_SYSTEM itself says a full-service
                            firm with a real practice in the vertical is IN, yet the
                            medical-malpractice run dropped kelleykronenberg.com for
                            being "full-service multi-practice ... not genuinely
                            specialized". Go read 20 of these by hand.
  haiku=KEEP, jev=DROP   -> companies that would have cost enrichment credits and three
                            copy calls before anyone noticed. Cheaper to be wrong here,
                            but still worth reading.

The REVIEW bucket has no Haiku counterpart by design: it is the cases where Jev's
confidence was too low to spend money on. If that bucket is large, lower REVIEW_BELOW in
clients/typesafe.py rather than concluding the model is weak.
"""

from __future__ import annotations

import argparse
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import socket
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# TYPESAFE_API_KEY lives in the local .env; the database vars come from `railway run`.
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass


def _reach_the_database() -> None:
    """Point DATABASE_URL at the public proxy when the internal host cannot be resolved.

    Railway sets DATABASE_URL to postgres.railway.internal, which exists only inside
    their private network. `railway run` from a laptop injects that value faithfully and
    psycopg then dies on `[Errno 11001] getaddrinfo failed`, which reads like a database
    outage rather than a DNS fact. DEPLOY.md already says not to copy that value.

    The user, password and database name are identical on both routes, so only host and
    port change. Nothing is hardcoded and no credential is written anywhere: the proxy
    host and port arrive as RAILWAY_TCP_PROXY_DOMAIN / _PORT, which means this must run
    under a service that carries them:

        railway run --service Postgres python scripts/shadow_enforce.py --list

    Set DATABASE_PUBLIC_URL to override all of this.
    """
    override = os.getenv("DATABASE_PUBLIC_URL")
    if override:
        os.environ["DATABASE_URL"] = override
        return
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        return
    parts = urlsplit(dsn)
    if not parts.hostname:
        return
    try:
        socket.gethostbyname(parts.hostname)
        return                      # resolves: either inside Railway, or already public
    except OSError:
        pass
    domain = os.getenv("RAILWAY_TCP_PROXY_DOMAIN")
    port = os.getenv("RAILWAY_TCP_PROXY_PORT")
    if not (domain and port):
        sys.exit(
            f"DATABASE_URL points at {parts.hostname!r}, which does not resolve from "
            f"this machine, and no public route was supplied.\n\n"
            f"Run it under the database service so the proxy variables come with it:\n"
            f"  railway run --service Postgres python scripts/shadow_enforce.py --list\n\n"
            f"Or set DATABASE_PUBLIC_URL from the Postgres service's Connect tab."
        )
    netloc = f"{parts.username}:{parts.password}@{domain}:{port}"
    os.environ["DATABASE_URL"] = urlunsplit(
        (parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    print(f"database: {parts.hostname} is private, using {domain}:{port}\n")


_reach_the_database()                                 # must precede the app imports:
                                                      # config.Settings reads env once.
from app import db                                    # noqa: E402
from app.clients import typesafe                      # noqa: E402
from app.stages.s2_expansion import Vertical          # noqa: E402

FIELDS = ["domain", "name", "haiku", "haiku_reason", "jev", "jev_reason",
          "p_keep", "p_idpr", "p_drop", "confidence", "is_operator", "agree"]


def available() -> list[tuple[str, int]]:
    """Verticals that actually have Haiku verdicts to compare against, biggest first.

    Exact name matching against a free-text column is the likeliest way this script
    fails, so every exit path prints this rather than leaving you to guess.
    """
    with db.cursor() as cur:
        cur.execute("SELECT vertical, count(*) n FROM staged_companies "
                    " WHERE verdict IS NOT NULL GROUP BY vertical ORDER BY n DESC")
        return [(r["vertical"], r["n"]) for r in cur.fetchall()]


def _die_with_menu(msg: str) -> None:
    rows = available()
    if not rows:
        sys.exit(f"{msg}\n\nNo vertical anywhere has enforced companies yet. "
                 "Stage 3 has to run before there is anything to compare.")
    listing = "\n".join(f"  {n:>6}  {v}" for v, n in rows[:20])
    sys.exit(f"{msg}\n\nVerticals with Haiku verdicts to compare against:\n{listing}\n\n"
             f"Copy one exactly, quotes included.")


def load_vertical(name: str) -> Vertical:
    """The Vertical is stored as JSON in vertical_backlog.config, not as columns."""
    with db.cursor() as cur:
        cur.execute("SELECT config FROM vertical_backlog WHERE vertical = %s", (name,))
        row = cur.fetchone()
    if not row:
        _die_with_menu(f"no vertical named {name!r} in vertical_backlog")
    cfg = row["config"] if isinstance(row["config"], dict) else json.loads(row["config"])
    return Vertical.from_json(cfg)


def load_judged(vertical: str, limit: int) -> list[tuple[dict, str, str]]:
    """Companies Haiku has ALREADY ruled on: (record, verdict, reason)."""
    with db.cursor() as cur:
        cur.execute(
            # md5(domain), not random(): a bare LIMIT returns whatever the planner likes,
            # usually one ingestion batch, and random() makes two runs incomparable. Hash
            # order is a fair sample that is the SAME sample every time, so a threshold
            # change can be re-scored against identical companies.
            "SELECT payload, verdict, reason FROM staged_companies "
            "  WHERE vertical = %s AND verdict IS NOT NULL "
            "  ORDER BY md5(domain) LIMIT %s", (vertical, limit))
        rows = cur.fetchall()
    out = []
    for r in rows:
        p = r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"])
        out.append((p, r["verdict"], r["reason"] or ""))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vertical", help="exact name; omit with --list to see the options")
    ap.add_argument("--list", action="store_true",
                    help="print the verticals that have Haiku verdicts, then exit")
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--out", default="shadow_enforce.csv")
    ap.add_argument("--workers", type=int, default=8, help="concurrent Jev calls")
    args = ap.parse_args()

    if args.list:
        rows = available()
        if not rows:
            sys.exit("nothing enforced yet -- stage 3 has to run first")
        print(f"{'companies':>10}  vertical")
        for v, n in rows:
            print(f"{n:>10}  {v}")
        return
    if not args.vertical:
        _die_with_menu("--vertical is required (or pass --list)")

    if not typesafe.configured():
        sys.exit("TYPESAFE_API_KEY is not set")

    # Every phase announces itself. The first version printed nothing for the six minutes
    # 300 sequential calls take, which is indistinguishable from a hang.
    print("1/3 loading the vertical ...", flush=True)
    vertical = load_vertical(args.vertical)
    print(f"2/3 sampling up to {args.limit} companies Haiku already ruled on ...", flush=True)
    judged = load_judged(args.vertical, args.limit)
    if not judged:
        _die_with_menu(f"no enforced companies staged for {args.vertical!r}")
    print(f"3/3 asking Jev about {len(judged)} companies, {args.workers} at a time",
          flush=True)
    print(flush=True)

    def one(item: tuple[dict, str, str]) -> dict:
        record, h_dec, h_reason = item
        base = {"domain": record.get("domain"), "name": record.get("name"),
                "haiku": h_dec, "haiku_reason": h_reason[:80]}
        try:
            j = typesafe.judge(vertical, record)
            t_dec, t_reason = typesafe.decide(j)
        except Exception as exc:                       # noqa: BLE001
            return {**base, "jev": "ERROR", "jev_reason": str(exc)[:120],
                    "p_keep": "", "p_idpr": "", "p_drop": "",
                    "confidence": "", "is_operator": "", "agree": ""}
        p = j["probabilities"]
        return {**base, "jev": t_dec, "jev_reason": t_reason,
                "p_keep": round(float(p.get("KEEP", 0)), 3),
                "p_idpr": round(float(p.get("IDPR", 0)), 3),
                "p_drop": round(float(p.get("DROP", 0)), 3),
                "confidence": j["confidence"], "is_operator": j["is_operator"],
                "agree": h_dec == t_dec}

    t0 = time.time()
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(one, item) for item in judged]
        for n, fut in enumerate(as_completed(futures), 1):
            rows.append(fut.result())
            if n % 25 == 0 or n == len(judged):
                bad = sum(1 for r in rows if r["jev"] == "ERROR")
                print(f"  {n:>4}/{len(judged)}  {time.time() - t0:5.0f}s  errors={bad}",
                      flush=True)
    rows.sort(key=lambda r: r["domain"] or "")
    secs = time.time() - t0
    errors = sum(1 for r in rows if r["jev"] == "ERROR")
    agree = sum(1 for r in rows if r["agree"] is True)
    if errors and errors == len(rows):
        print()
        print(f"EVERY call failed. First error: {rows[0]['jev_reason']}")

    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    ok = len(rows) - errors
    print()
    print(f"jev: {ok} judgments in {secs:.0f}s ({secs / max(ok, 1) * 1000:.0f}ms each wall-clock, "
          f"{errors} errors)\n")
    mix = {}
    for r in rows:
        mix[r["haiku"]] = mix.get(r["haiku"], 0) + 1
    print("haiku verdict mix:    " + ", ".join(f"{k}={v}" for k, v in sorted(mix.items())))
    if ok:
        print(f"agreement:            {agree}/{ok} ({agree / ok:.0%})")
    print(f"haiku DROP, jev KEEP: {sum(1 for r in rows if r['haiku'] == 'DROP' and r['jev'] == 'KEEP')}  <- read these")
    print(f"haiku KEEP, jev DROP: {sum(1 for r in rows if r['haiku'] == 'KEEP' and r['jev'] == 'DROP')}")
    print(f"jev REVIEW:           {sum(1 for r in rows if r['jev'] == 'REVIEW')}")
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
