"""Which mail gateway fronts a company's inbox, from its MX records.

Troy, 2026-09-15: contacts behind Barracuda and Mimecast do not go to copy; Proofpoint
stays. Measured on 400 sourced companies that day: Mimecast fronts ~10% of contacts,
Barracuda ~3%, Proofpoint ~3%, Microsoft 39%, Google 26%.

The check runs at company entry in stage 4, BEFORE the finders and the paid
verification -- skipping a company there costs one DNS lookup; skipping its contacts
at copy would have cost their verification and research as well. Results are cached
in Postgres for 30 days: MX records rarely change and the same domains recur across
verticals.
"""

from __future__ import annotations

import re
import subprocess
from datetime import datetime, timedelta, timezone

from . import db
from .config import settings

CACHE_DAYS = 30

# Substring of the MX hostname -> gateway label.
SIGNATURES = (
    ("barracuda", "barracuda"),
    ("mimecast", "mimecast"),
    ("pphosted", "proofpoint"), ("proofpoint", "proofpoint"),
    ("outlook", "microsoft"), ("microsoft", "microsoft"),
    ("google", "google"), ("googlemail", "google"),
    ("iphmx", "cisco"), ("sophos", "sophos"), ("messagelabs", "broadcom"),
    ("hornetsecurity", "hornetsecurity"), ("fortimail", "fortinet"),
    ("trendmicro", "trendmicro"), ("zoho", "zoho"),
)

def _mx_hosts(domain: str) -> list[str] | None:
    """MX hostnames, or None when the lookup itself failed (never treat as 'no MX')."""
    try:
        import dns.resolver  # type: ignore
        try:
            answers = dns.resolver.resolve(domain, "MX", lifetime=6)
            return sorted(str(r.exchange).rstrip(".").lower() for r in answers)
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            return []
        except Exception:
            return None
    except ImportError:
        pass
    try:
        out = subprocess.run(["nslookup", "-type=mx", domain], capture_output=True,
                             text=True, timeout=8).stdout.lower()
    except Exception:
        return None
    return sorted(set(re.findall(r"mail exchanger = (\S+)", out)))


def classify(hosts: list[str] | None) -> str | None:
    """Gateway label for a set of MX hosts; 'other' when unrecognised; None when unknown."""
    if hosts is None:
        return None
    if not hosts:
        return "none"
    joined = " ".join(hosts)
    for needle, label in SIGNATURES:
        if needle in joined:
            return label
    return "other"


def gateway(domain: str) -> str | None:
    """Cached gateway label for a domain. None when DNS could not answer."""
    domain = db.normalize_domain(domain)
    if not domain:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(days=CACHE_DAYS)
    try:
        with db.cursor() as cur:
            cur.execute("SELECT gateway, checked_at FROM mx_cache WHERE domain=%s", (domain,))
            row = cur.fetchone()
        if row and row["checked_at"] > cutoff and row["gateway"]:
            return row["gateway"]
    except Exception:
        pass
    hosts = _mx_hosts(domain)
    label = classify(hosts)
    if label is not None:
        try:
            with db.cursor() as cur:
                cur.execute(
                    """INSERT INTO mx_cache (domain, gateway, hosts, checked_at)
                       VALUES (%s,%s,%s,now())
                       ON CONFLICT (domain) DO UPDATE SET gateway=EXCLUDED.gateway,
                           hosts=EXCLUDED.hosts, checked_at=now()""",
                    (domain, label, " ".join(hosts or [])))
        except Exception:
            pass
    return label


def blocked(domain: str) -> str | None:
    """The reason to skip this company, or None. Unknown DNS never blocks."""
    label = gateway(domain)
    if label and label in settings.blocked_mail_gateways:
        return f"mail gateway: {label}"
    return None
