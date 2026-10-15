#!/usr/bin/env python3
"""Push a CSV to the running service's upload endpoint.

  python scripts/upload_csv.py performance export.csv --url https://<service>.up.railway.app
  python scripts/upload_csv.py history all-time-sends.csv --url ...

Reads UPLOAD_TOKEN from the environment; never takes it on the command line, where it
would land in shell history.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=["performance", "history"])
    parser.add_argument("csv_path")
    parser.add_argument("--url", default=os.getenv("SERVICE_URL", "http://localhost:8000"))
    parser.add_argument("--as-of", default=None, help="YYYY-MM-DD for a performance export")
    args = parser.parse_args()

    token = os.getenv("UPLOAD_TOKEN")
    if not token:
        print("UPLOAD_TOKEN is not set in the environment", file=sys.stderr)
        return 1

    path = Path(args.csv_path)
    if not path.exists():
        print(f"no such file: {path}", file=sys.stderr)
        return 1

    data = {"as_of": args.as_of} if args.as_of and args.kind == "performance" else None
    with httpx.Client(timeout=120) as client:
        resp = client.post(
            f"{args.url.rstrip('/')}/upload/{args.kind}",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": (path.name, path.read_bytes(), "text/csv")},
            data=data,
        )
    print(resp.status_code)
    try:
        print(json.dumps(resp.json(), indent=2))
    except ValueError:
        print(resp.text)
    return 0 if resp.status_code < 400 else 1


if __name__ == "__main__":
    sys.exit(main())
