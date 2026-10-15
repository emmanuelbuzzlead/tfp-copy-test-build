"""HARD BLOCK: never load contacts behind Mimecast or Barracuda into a campaign (TFP rule, Troy 2026-09-16, memory tfp-mx-blocklist-rule).

Both are email security gateways that filter cold outbound and run spam traps, so one push costs sender reputation on every campaign that shares
the sending domain. Detection is by MX record, NOT by the visible address: kevin@bldr.com shows nothing, but Builders FirstSource sits behind Mimecast.
Results are cached per domain (memory and the mx_cache table) because an MX lookup is slow and the answer rarely changes. Public resolvers are pinned
because a local router resolver times out on MX queries.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from . import db

BLOCKED_MX = ("mimecast", "barracuda", "barracudanetworks", "bnet.cudasvc", "cudamail")
BLOCKED_DOMAINS = ("mimecast.com", "barracuda.com", "barracudanetworks.com")
PUBLIC_NAMESERVERS = ["1.1.1.1", "8.8.8.8", "9.9.9.9", "1.0.0.1"]

_lock = threading.Lock()
_mem: dict[str, bool] = {}


def _ensure() -> None:
    with db.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS mx_cache (domain TEXT PRIMARY KEY, blocked BOOLEAN NOT NULL, hosts TEXT, checked_at TIMESTAMPTZ DEFAULT now())")


def _mx_hosts(domain: str) -> list[str] | None:
    """MX hosts, [] when the domain has no MX (a real answer), None when every resolver failed (unknown)."""
    import dns.resolver
    for ns in PUBLIC_NAMESERVERS:
        try:
            r = dns.resolver.Resolver(configure=False)
            r.nameservers = [ns]
            r.lifetime, r.timeout = 8.0, 4.0
            return [str(x.exchange).lower().rstrip(".") for x in r.resolve(domain, "MX")]
        except Exception as exc:
            if type(exc).__name__ in ("NXDOMAIN", "NoAnswer", "NoNameservers"):
                return []
    return None


def check(domain: str) -> bool | None:
    """True when blocked, False when clear, None when it could not be determined (callers treat None as NOT blocked but do not cache it)."""
    d = (domain or "").strip().lower()
    if not d:
        return False
    if d in BLOCKED_DOMAINS:
        return True
    with _lock:
        if d in _mem:
            return _mem[d]
    _ensure()
    with db.cursor() as cur:
        cur.execute("SELECT blocked FROM mx_cache WHERE domain=%s", (d,))
        row = cur.fetchone()
    if row:
        with _lock:
            _mem[d] = bool(row["blocked"])
        return bool(row["blocked"])
    hosts = _mx_hosts(d)
    if hosts is None:
        return None
    blocked = any(b in h for h in hosts for b in BLOCKED_MX)
    with db.cursor() as cur:
        cur.execute("INSERT INTO mx_cache (domain, blocked, hosts) VALUES (%s,%s,%s) ON CONFLICT (domain) DO NOTHING", (d, blocked, ",".join(hosts)[:400]))
    with _lock:
        _mem[d] = blocked
    return blocked


def split(contacts: list[dict]) -> tuple[list[dict], list[dict]]:
    """(allowed, blocked) by the MX of each contact's EMAIL domain and company domain."""
    def domains(c: dict) -> set[str]:
        em = (c.get("email") or "").split("@")[-1].lower()
        return {x for x in (em, (c.get("company_domain") or "").lower()) if x}

    all_domains = sorted({d for c in contacts for d in domains(c)})
    with ThreadPoolExecutor(max_workers=8) as pool:
        verdict = dict(zip(all_domains, pool.map(check, all_domains)))
    allowed, blocked = [], []
    for c in contacts:
        (blocked if any(verdict.get(d) for d in domains(c)) else allowed).append(c)
    return allowed, blocked
