"""Recover contacts EmailBison already holds, for companies due another approach.

Troy, 2026-09-11: a company IDPR emailed more than 90 days ago is not spent. Where the
finders come back empty for such a company, EmailBison itself is a contact source --
the people are already in the workspace, with names and often titles. Those get their
addresses re-verified through the normal waterfall and brand-new copy; nothing is ever
re-sent as written.

This is a FALLBACK, not the primary route, and the reason is measured. Sampling seven
older campaigns on 2026-09-11, the share of leads carrying a title ranged from 6% to
100% ("New Industries | Jan '26", 30,546 leads, has titles on 6% of them). A lead with
no title cannot be scored against the ICP ladder, so it cannot be shown to fit. And of
the 97,400 companies in the history, 57% have exactly one person emailed -- reuse
returns that one person, the one who already ignored two emails, where the finders
return the three colleagues who have never heard from us. Fresh sourcing is also not
the expensive step: QuickEnrich is an unlimited-credit plan.

Endpoint, verified live 2026-09-11:

    GET /api/leads?search=<domain>

The `search` parameter is FUZZY, not a domain filter. Searching "quidel.com" returned 8
leads of which 8 were other companies, and "stripe.com" returned 1,381 matches, none of
them at stripe.com. Every row is therefore re-checked against the exact email domain
here; trusting the endpoint's own filtering would import strangers under a known logo.

Each row carries lead_campaign_data -- campaign_id, emails_sent, replies, interested --
which is what lets this skip anyone already in a conversation.
"""

from __future__ import annotations

import httpx

from . import db
from .clients.emailbison import _headers
from .clients.quickenrich import Contact
from .config import settings

# One page. The search is fuzzy, so a company with no leads of its own still returns
# pages of strangers -- and paging through six of them cost 32 seconds per empty
# company (measured 2026-09-13). A company that IS in EmailBison surfaces on page 1:
# the exact-domain rows rank first because the domain is in every one of their emails.
MAX_PAGES = 1


def _rows(domain: str) -> list[dict]:
    """Every EmailBison lead whose email is actually at this domain."""
    out: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        try:
            r = httpx.get(f"{settings.emailbison_base}/api/leads", headers=_headers(),
                          params={"search": domain, "page": page}, timeout=12)
            if r.status_code != 200:
                break
            payload = r.json() or {}
        except Exception:
            break
        rows = payload.get("data") or []
        if not rows:
            break
        for row in rows:
            email = (row.get("email") or "").strip().lower()
            if "@" not in email:
                continue
            # The fuzzy search is why this check exists -- see the module docstring.
            if db.normalize_domain(email.rsplit("@", 1)[1]) == db.normalize_domain(domain):
                out.append(row)
        meta = payload.get("meta") or {}
        if page >= int(meta.get("last_page") or 1):
            break
    return out


def _engaged(row: dict) -> bool:
    """True when this person is already in a conversation and must be left alone."""
    stats = row.get("overall_stats") or {}
    if (stats.get("replies") or 0) > 0 or (stats.get("unique_replies") or 0) > 0:
        return True
    for c in row.get("lead_campaign_data") or []:
        if c.get("interested") or (c.get("replies") or 0) > 0:
            return True
    return False


def find(domain: str, company_name: str | None = None) -> list[Contact]:
    """Contacts EmailBison already holds at this company, as finder-shaped rows.

    Only people with a title survive: the ICP ladder scores on title, and a lead with a
    null title cannot be shown to belong in the campaign. Anyone who has replied or been
    marked interested is excluded outright.

    Recency is NOT checked here. The company-level gate in db.blocked_domains has already
    established that this company saw no campaign inside recontact_days, and
    db.blocked_contacts re-checks each individual before verification spends anything.
    """
    # Ask EmailBison only about companies it can plausibly know. Its search is fuzzy
    # and a domain with no leads makes it scan the whole table: measured 2026-09-13,
    # 31 seconds per unknown company against under 4 for a known one. The 223,863-row
    # contact history is a near-perfect local index of who EmailBison has ever held,
    # so a domain absent from it is skipped without a call.
    if not db.known_domain(domain):
        return []
    contacts: list[Contact] = []
    seen: set[str] = set()
    for row in _rows(domain):
        email = (row.get("email") or "").strip().lower()
        title = (row.get("title") or "").strip()
        if not title or email in seen or _engaged(row):
            continue
        seen.add(email)
        contacts.append(Contact(
            first_name=(row.get("first_name") or "").strip(),
            last_name=(row.get("last_name") or "").strip(),
            email=email,
            title=title,
            linkedin_url=None,          # EmailBison does not store one
            company=(row.get("company") or company_name or "").strip() or None,
            company_domain=db.normalize_domain(domain),
            # Deliberately left unset: an address EmailBison accepted months ago is not
            # evidence it still resolves, and re-verification is the point of this path.
            email_verified_at=None,
        ))
    return contacts
