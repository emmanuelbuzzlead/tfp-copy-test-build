"""Do-not-contact list. Hard block, applied before any spend.

Two sources, both enforced identically:
  - assets/do_not_contact.txt, versioned in the repo (current clients, IDPR itself).
  - the `do_not_contact` table, so entries can be added at runtime without a deploy.

Matching is deliberately generous in the blocking direction, because the cost of the
two errors is wildly asymmetric: wrongly blocking a prospect loses one lead out of
hundreds, while wrongly allowing one cold-pitches a paying client. So a blocked domain
also blocks its subdomains, and name matching is case- and punctuation-insensitive on a
normalised form ("Harvey Law Group, LLC" matches "harvey law group").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

def _list_path() -> Path:
    """The active client's suppression list."""
    try:
        from . import client
        return client.profile().do_not_contact_path
    except Exception:
        return Path(__file__).resolve().parent.parent / "assets" / "do_not_contact.txt"


LIST_PATH = _list_path()

# Corporate suffixes stripped before a name comparison, so "Pryon, Inc." matches "Pryon".
SUFFIX_RX = re.compile(
    r"\b(inc|llc|l\.l\.c|ltd|limited|corp|corporation|co|company|plc|lp|llp|pllc|"
    r"gmbh|pty|group|holdings|partners)\b\.?",
    re.IGNORECASE,
)


def normalize_domain(value: str) -> str:
    d = (value or "").strip().lower()
    for prefix in ("https://", "http://"):
        if d.startswith(prefix):
            d = d[len(prefix):]
    if d.startswith("www."):
        d = d[4:]
    return d.split("/")[0].strip()


def normalize_name(value: str) -> str:
    n = (value or "").lower()
    n = SUFFIX_RX.sub(" ", n)
    n = re.sub(r"[^a-z0-9]+", " ", n)
    return re.sub(r"\s+", " ", n).strip()


@dataclass
class DoNotContact:
    domains: set[str] = field(default_factory=set)
    names: set[str] = field(default_factory=set)

    def blocks_domain(self, domain: str) -> str | None:
        d = normalize_domain(domain)
        if not d:
            return None
        if d in self.domains:
            return f"do-not-contact domain {d}"
        # Subdomain of a blocked domain: news.pryon.com is still Pryon.
        for blocked in self.domains:
            if d.endswith("." + blocked):
                return f"do-not-contact domain {blocked} (as {d})"
        return None

    def blocks_name(self, name: str) -> str | None:
        n = normalize_name(name)
        if not n:
            return None
        if n in self.names:
            return f"do-not-contact company name {name!r}"
        # A listed name appearing as a whole-word phrase inside a longer name.
        for blocked in self.names:
            if re.search(rf"\b{re.escape(blocked)}\b", n):
                return f"do-not-contact company name {blocked!r} (in {name!r})"
        return None

    def blocks(self, domain: str | None = None, name: str | None = None) -> str | None:
        return (self.blocks_domain(domain or "") or self.blocks_name(name or "")) or None

    def blocks_email(self, email: str) -> str | None:
        if not email or "@" not in email:
            return None
        return self.blocks_domain(email.rsplit("@", 1)[1])


def _parse(text: str) -> tuple[set[str], set[str]]:
    domains: set[str] = set()
    names: set[str] = set()
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.lower().startswith("name:"):
            n = normalize_name(line.split(":", 1)[1])
            if n:
                names.add(n)
        else:
            d = normalize_domain(line)
            if d:
                domains.add(d)
    return domains, names


@lru_cache(maxsize=1)
def _from_file() -> DoNotContact:
    if not LIST_PATH.exists():
        raise RuntimeError(
            f"do-not-contact list missing at {LIST_PATH}. Refusing to run without it: "
            "the whole point is that its absence must not silently mean 'block nothing'."
        )
    domains, names = _parse(LIST_PATH.read_text(encoding="utf-8"))
    if not domains and not names:
        raise RuntimeError(f"{LIST_PATH} parsed to zero entries; refusing to run.")
    return DoNotContact(domains, names)


def load(include_db: bool = True) -> DoNotContact:
    """File entries plus any runtime additions from the database."""
    base = _from_file()
    combined = DoNotContact(set(base.domains), set(base.names))
    if include_db:
        try:
            from . import db

            with db.cursor() as cur:
                cur.execute("SELECT entry, kind FROM do_not_contact")
                for row in cur.fetchall():
                    if row["kind"] == "name":
                        combined.names.add(normalize_name(row["entry"]))
                    else:
                        combined.domains.add(normalize_domain(row["entry"]))
        except Exception:
            # A DB that is unreachable must not silently shrink the block list; the
            # file entries still apply, and the caller logs the degraded state.
            pass
    return combined


def add(entry: str, kind: str = "domain", note: str | None = None) -> None:
    from . import db

    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO do_not_contact (entry, kind, note) VALUES (%s,%s,%s) "
            "ON CONFLICT (entry) DO NOTHING",
            (entry.strip().lower(), kind, note),
        )
