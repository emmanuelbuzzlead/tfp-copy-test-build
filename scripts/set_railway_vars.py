#!/usr/bin/env python3
"""Load the Lumion web service's variables onto Railway from the local root .env, never printing a value.

Reads C:/Claude_Code_GTM_Training/.env the way python-dotenv does (last duplicate wins, quotes and surrounding
whitespace stripped, because a trailing space in a key once made every email verification read as empty), then pipes
each value to `railway variable set --stdin`. Output lists variable NAMES and ok or failed, nothing else.

Usage (from the railway-lumion folder, linked to the lumion project):
    python scripts/set_railway_vars.py            set everything
    python scripts/set_railway_vars.py --slack    also set the Slack bot token and signing secret (LUMION_SLACK_*)
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ENV = Path("C:/Claude_Code_GTM_Training/.env")
SERVICE = "tfp-web"
if "--service" in __import__("sys").argv:
    SERVICE = __import__("sys").argv[__import__("sys").argv.index("--service") + 1]
PUBLIC_URL = "https://tfp-web-production-3a35.up.railway.app"

# Copied from the root .env, by name.
FROM_ENV = [
    "EMAILBISON_API_KEY_TFP",
    "QUICKENRICH_API_KEY", "LEADOCEAN_API_KEY", "JINA_API_KEY", "SERPER_API_KEY",
    "MILLIONVERIFIER_API_KEY", "BOUNCEBAN_API_KEY", "TRYKITT_API_KEY",
    "ANTHROPIC_API_KEY", "CONCENTRATE_AI_API_KEY", "OPENROUTER_API_KEY", "BUZZLEAD_API_KEY",
    "AIARK_API_KEY", "APOLLO_API_KEY", "HUNTER_API_KEY", "ICYPEAS_API_KEY", "LEADMAGIC_API_KEY",
]
SLACK_MAP = {"LUMION_SLACK_BOT_TOKEN": "SLACK_BOT_TOKEN", "LUMION_SLACK_SIGNING_SECRET": "SLACK_SIGNING_SECRET"}

# Fixed values. Safe defaults: nothing runs, nothing auto starts, senders are attached by hand.
FIXED = {
    "CLIENT": "tfp",
    "DATABASE_URL": "${{Postgres.DATABASE_URL}}",
    "PUBLIC_URL": PUBLIC_URL,
    "PIPELINE_ENABLED": "false",     # the kill switch; the guards below keep the proof phase small and unable to send
    "BUZZLEAD_ENABLED": "false",
    "AUTO_START_CAMPAIGNS": "false",
    "SKIP_SENDER_ATTACH": "true",
    "POOL_REFILL_ENABLED": "false",   # new-company sourcing deferred until asked for
}


def read_env() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in ENV.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'").strip()
    return out


def setvar(name: str, value: str) -> bool:
    r = subprocess.run(f"railway variable set -s {SERVICE} --skip-deploys --stdin {name}", input=value, text=True,
                       capture_output=True, shell=True)
    return r.returncode == 0


def main() -> int:
    env = read_env()
    todo: list[tuple[str, str]] = list(FIXED.items())
    missing = []
    for name in FROM_ENV:
        if env.get(name):
            todo.append((name, env[name]))
        else:
            missing.append(name)
    if "--slack" in sys.argv:
        for src, dst in SLACK_MAP.items():
            if env.get(src):
                todo.append((dst, env[src]))
            else:
                missing.append(src)
    failed = []
    for name, value in todo:
        ok = setvar(name, value)
        print(f"{'ok    ' if ok else 'FAILED'} {name}")
        if not ok:
            failed.append(name)
    for name in missing:
        print(f"missing in .env: {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
