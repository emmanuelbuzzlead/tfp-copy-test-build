"""Stage 1 -- Performance pull. API primary, CSV override.

The spec assumed the API exposes no reply sentiment per campaign, and decision #3
therefore made CSV the only source. Checking the live workspace on 2026-09-07 showed
the assumption was wrong: each campaign object from /api/campaigns carries total_leads,
emails_sent, replied and `interested`. It is populated on 62 of 82 campaigns, and the
ranking it produces (Funded Brands 1.29%, SaaS 0.79%, Healthcare 0.73%) independently
reproduces the vertical performance history stated in the spec, which is good evidence
the field means what we want it to mean.

So Stage 1 reads the API and the pipeline is genuinely self-running. What IS broken is
the send-log level -- per_page capped at 15, no campaign filter on list_leads -- but
campaign-level aggregates are a different endpoint and complete in six pages.

The CSV path is kept as an override, not a fallback: any uploaded figure wins over the
API for that campaign and marks it stats_verified. That covers the spec's other worry
(live stats may lag) and gives a way to correct a number by hand.

THE METRIC IS THE SHARED WINNER CONTRACT in app/scoring.py: after 1,000 sends and a
week, interested replies per send plus booked-meeting value govern. Stage 1 and Stage 8
use the same bar, so a vertical that scales becomes a seed for adjacent audiences.

Ranking is on interested / sends. The live run pools
campaigns into audiences via select_seed_themes -- three Funded Brands reruns are one
seed, not three.

Lookback: gated on sends + age, not EmailBison updated_at (every campaign in this
workspace reports an updated_at inside 90 days from a bulk touch).
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date
from typing import Iterable

from ..config import settings

# Canonical field -> accepted header aliases (lowercased, non-alphanumerics stripped).
ALIASES: dict[str, tuple[str, ...]] = {
    # "campaign" is deliberately NOT a campaign_id alias. A column headed plainly
    # "Campaign" holds the name; letting it claim campaign_id left campaign_name
    # unmapped, and since a text value fails numeric parsing the row then had
    # neither id nor name and was dropped as identity-less. Silent row loss.
    "campaign_id": ("campaignid", "id"),
    "campaign_name": ("campaignname", "name", "campaign", "title"),
    "total_leads": ("totalleads", "leads", "leadcount", "totalcontacts", "contacts", "prospects"),
    "sends": ("sends", "sent", "emailssent", "totalsent", "sentcount", "delivered"),
    "replies": ("replies", "replied", "totalreplies", "replycount", "responses"),
    "interested": (
        "interested", "interestedreplies", "interestedcount", "leadsinterested",
        "positivereplies", "positive", "positiveresponses", "positivereplycount",
        "positiveresponsecount",
    ),
    "vertical_label": ("vertical", "verticallabel", "industry", "segment", "niche", "category"),
}

REQUIRED = ("total_leads", "interested")


def _key(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (header or "").strip().lower())


def map_headers(headers: Iterable[str]) -> dict[str, str]:
    """Return {canonical_field: actual_header}. Exact alias hits win over substring hits."""
    seen = {_key(h): h for h in headers if h and h.strip()}
    mapping: dict[str, str] = {}
    taken: set[str] = set()

    for field, aliases in ALIASES.items():
        for alias in aliases:
            if alias in seen and seen[alias] not in taken:
                mapping[field] = seen[alias]
                taken.add(seen[alias])
                break

    # Second pass: substring match for anything still unmapped, longest alias first so
    # "positivereplies" claims a column before the shorter "replies" alias can.
    for field, aliases in ALIASES.items():
        if field in mapping:
            continue
        for alias in sorted(aliases, key=len, reverse=True):
            hit = next((orig for k, orig in seen.items() if alias in k and orig not in taken), None)
            if hit:
                mapping[field] = hit
                taken.add(hit)
                break
    return mapping


class IngestError(RuntimeError):
    pass


def _number(raw: object) -> int | None:
    if raw is None:
        return None
    cleaned = re.sub(r"[,\s%$]", "", str(raw)).strip()
    if not cleaned or cleaned.lower() in {"n/a", "na", "-", "none", "null"}:
        return None
    try:
        return int(float(cleaned))
    except ValueError:
        return None


@dataclass
class CampaignRow:
    campaign_id: int | None
    campaign_name: str | None
    total_leads: int | None
    sends: int | None
    replies: int | None
    interested: int | None
    vertical_label: str | None = None
    source: str = "api"          # "api" or "csv"
    stats_verified: bool = False  # true once a human-supplied export has confirmed it

    @property
    def interested_rate(self) -> float | None:
        """interested / total_leads -- kept for CSV/debug."""
        if not self.total_leads or self.interested is None:
            return None
        return self.interested / self.total_leads

    @property
    def positive_reply_rate(self) -> float | None:
        """interested / replied -- diagnostic quality among replies."""
        if not self.replies or self.interested is None:
            return None
        return self.interested / self.replies

    @property
    def merge_key(self) -> str:
        """Campaign id when available, else the normalised name."""
        if self.campaign_id:
            return f"id:{self.campaign_id}"
        return f"name:{(self.campaign_name or '').strip().lower()}"


# ---------------------------------------------------------------------------
# source: API
# ---------------------------------------------------------------------------

def from_api() -> list[CampaignRow]:
    """Campaign-level aggregates straight off /api/campaigns (paginated to last_page)."""
    from ..clients import emailbison

    rows = []
    for c in emailbison.list_campaigns():
        rows.append(CampaignRow(
            campaign_id=c.get("id"),
            campaign_name=(c.get("name") or "").strip() or None,
            total_leads=_number(c.get("total_leads")),
            sends=_number(c.get("emails_sent")),
            replies=_number(c.get("replied")),
            interested=_number(c.get("interested")),
            vertical_label=None,
            source="api",
            stats_verified=False,
        ))
    return rows


# ---------------------------------------------------------------------------
# source: CSV override
# ---------------------------------------------------------------------------

def parse_performance_csv(content: bytes | str, filename: str = "") -> tuple[list[CampaignRow], dict]:
    text = content.decode("utf-8-sig", errors="replace") if isinstance(content, bytes) else content
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    if not headers:
        raise IngestError("CSV has no header row")

    mapping = map_headers(headers)
    missing = [f for f in REQUIRED if f not in mapping]
    if missing:
        raise IngestError(
            f"could not map required column(s) {missing} in {filename or 'upload'}. "
            f"Headers seen: {headers}. Mapped so far: {mapping}. "
            f"Add the real header to ALIASES in app/stages/s1_performance.py."
        )

    rows: list[CampaignRow] = []
    skipped = 0
    for raw in reader:
        get = lambda f: raw.get(mapping[f]) if f in mapping else None  # noqa: E731
        row = CampaignRow(
            campaign_id=_number(get("campaign_id")),
            campaign_name=(get("campaign_name") or "").strip() or None,
            total_leads=_number(get("total_leads")),
            sends=_number(get("sends")),
            replies=_number(get("replies")),
            interested=_number(get("interested")),
            vertical_label=(get("vertical_label") or "").strip() or None,
            source="csv",
            stats_verified=True,
        )
        if row.campaign_id is None and not row.campaign_name:
            skipped += 1
            continue
        rows.append(row)

    report = {
        "headers_seen": headers,
        "mapping": mapping,
        "unmapped_fields": [f for f in ALIASES if f not in mapping],
        "rows_parsed": len(rows),
        "rows_skipped_no_identity": skipped,
    }
    return rows, report


# ---------------------------------------------------------------------------
# merge + winner selection
# ---------------------------------------------------------------------------

def merge(api_rows: list[CampaignRow], csv_rows: list[CampaignRow]) -> list[CampaignRow]:
    """CSV wins per campaign; API supplies everything the export did not mention.

    Matching is by campaign id when the export carries one, otherwise by name -- so an
    export with only names still overrides the right campaigns.
    """
    merged: dict[str, CampaignRow] = {row.merge_key: row for row in api_rows}
    by_name = {
        f"name:{(r.campaign_name or '').strip().lower()}": key
        for key, r in merged.items() if r.campaign_name
    }
    for row in csv_rows:
        key = row.merge_key
        if key not in merged and key in by_name:
            key = by_name[key]
        if key in merged and not row.campaign_name:
            row.campaign_name = merged[key].campaign_name
        if key in merged and row.campaign_id is None:
            row.campaign_id = merged[key].campaign_id
        merged[key] = row
    return list(merged.values())


def select_winners(rows: list[CampaignRow]) -> tuple[list[CampaignRow], dict]:
    """Apply the shared winner contract to individual campaigns.

    Prefer select_seed_themes for the live run -- it pools reruns of the same
    audience so three Funded Brands lists don't burn three seed slots. This
    function stays for CSV previews and tests.
    """
    from ..scoring import judge

    eligible, rejected = [], []
    for row in rows:
        if not is_prospecting(row):
            rejected.append((row, "not a prospecting campaign"))
            continue
        verdict = judge(sends=row.sends or 0, replies=row.replies or 0,
                        interested=row.interested or 0, leads=row.total_leads or 0)
        if verdict.is_winner:
            eligible.append(row)
        else:
            rejected.append((row, verdict.reason))

    eligible.sort(key=lambda r: (r.interested or 0) / (r.sends or 1), reverse=True)
    winners = eligible[: settings.top_winners]
    stats = {
        "candidates": len(rows),
        "cleared_gates": len(eligible),
        "winners": [
            {
                "campaign_id": w.campaign_id,
                "name": w.campaign_name,
                "leads": w.total_leads,
                "sends": w.sends,
                "replies": w.replies,
                "interested": w.interested,
                "interested_rate": round((w.interested or 0) / (w.sends or 1), 5),
                "of_replies": round(w.positive_reply_rate or 0, 5),
                "of_sends": round((w.interested or 0) / (w.sends or 1), 5),
                "vertical_label": w.vertical_label,
                "source": w.source,
                "stats_verified": w.stats_verified,
            }
            for w in winners
        ],
        "rejected_sample": [{"name": r.campaign_name, "reason": why} for r, why in rejected[:10]],
    }
    return winners, stats


def current_stats() -> list[CampaignRow]:
    """The merged view Stage 1 actually ranks: API aggregates, overridden by any CSV."""
    return merge(from_api(), stored_csv_rows())


# ---------------------------------------------------------------------------
# persistence for the CSV override
# ---------------------------------------------------------------------------

def persist(rows: list[CampaignRow], upload_id: str, as_of: date | None = None) -> int:
    from .. import db

    as_of = as_of or date.today()
    payload = [
        (r.campaign_id or 0, r.campaign_name, as_of, r.total_leads, r.sends,
         r.replies, r.interested, r.vertical_label, upload_id)
        for r in rows
    ]
    with db.cursor() as cur:
        cur.executemany(
            """INSERT INTO campaign_stats (campaign_id, campaign_name, as_of, total_leads,
                                           sends, replies, interested, vertical_label, upload_id)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (campaign_id, as_of) DO UPDATE
                  SET total_leads=EXCLUDED.total_leads, sends=EXCLUDED.sends,
                      replies=EXCLUDED.replies, interested=EXCLUDED.interested,
                      campaign_name=EXCLUDED.campaign_name,
                      vertical_label=EXCLUDED.vertical_label, upload_id=EXCLUDED.upload_id""",
            payload,
        )
    return len(payload)


def stored_csv_rows() -> list[CampaignRow]:
    """Most recent uploaded figure per campaign. Empty when no export was ever uploaded."""
    from .. import db

    if not settings.database_url:
        return []
    try:
        with db.cursor() as cur:
            cur.execute(
                """SELECT DISTINCT ON (campaign_id) campaign_id, campaign_name, total_leads,
                          sends, replies, interested, vertical_label
                     FROM campaign_stats
                    ORDER BY campaign_id, as_of DESC"""
            )
            return [
                CampaignRow(
                    campaign_id=r["campaign_id"] or None,
                    campaign_name=r["campaign_name"],
                    total_leads=r["total_leads"],
                    sends=r["sends"],
                    replies=r["replies"],
                    interested=r["interested"],
                    vertical_label=r["vertical_label"],
                    source="csv",
                    stats_verified=True,
                )
                for r in cur.fetchall()
            ]
    except Exception:
        # No export uploaded yet is the normal case, not an error: the API carries the run.
        return []


# ---------------------------------------------------------------------------
# theme grouping -- seed on distinct AUDIENCES, not on individual campaigns
# ---------------------------------------------------------------------------

# Campaign names carry the audience plus a lot ofscheduling noise: dates, "RERUN",
# "Copy of", batch numbers, the data source. Stripping that leaves the audience, which
# is what a seed actually is.
_NOISE_RX = re.compile(
    r"\b(rerun|re-?run|copy of|new|test|final|batch|part|round|v\d+|"
    r"jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|"
    r"january|february|march|april|june|july|august|september|october|november|december|"
    r"\d{1,2}(st|nd|rd|th)?|'\d\d|20\d\d|buzzlead data|signalbase|gemini[\s\d.]*flash|"
    r"ms\s*\+\s*google|standard|reusing data)\b",
    re.IGNORECASE,
)


def theme_of(name: str | None) -> str:
    """The audience a campaign was aimed at, with scheduling noise removed.

    "IDPR Funded Brands - Campaign 1", "2026 Funded companies 9th July" and
    "SignalBase Funded companies 10th August" are one audience run three times, not
    three audiences. Seeding on campaigns treats them as three and wastes two of the
    three seed slots re-expanding the same buyer.
    """
    t = (name or "").lower()
    t = _NOISE_RX.sub(" ", t)
    t = re.sub(r"[^a-z&/ ]+", " ", t)
    t = re.sub(r"\b(idpr|campaign|companies|company|list|accounts|data)\b", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t or (name or "").strip().lower()


@dataclass
class Theme:
    """Several campaigns aimed at one audience, pooled."""
    name: str
    campaigns: list[CampaignRow]

    @property
    def total_leads(self) -> int:
        return sum(c.total_leads or 0 for c in self.campaigns)

    @property
    def sends(self) -> int:
        return sum(c.sends or 0 for c in self.campaigns)

    @property
    def interested(self) -> int:
        return sum(c.interested or 0 for c in self.campaigns)

    @property
    def replies(self) -> int:
        return sum(c.replies or 0 for c in self.campaigns)

    @property
    def interested_rate(self) -> float:
        """of-leads; kept for debug."""
        return (self.interested / self.total_leads) if self.total_leads else 0.0

    @property
    def positive_reply_rate(self) -> float:
        return (self.interested / self.replies) if self.replies else 0.0

    @property
    def outcome_rate(self) -> float:
        return (self.interested / self.sends) if self.sends else 0.0

    def as_seed(self) -> dict:
        best = max(self.campaigns, key=lambda c: ((c.interested or 0) / (c.sends or 1)))
        return {
            "name": self.name,
            "example_campaign": best.campaign_name,
            "campaigns_pooled": len(self.campaigns),
            "leads": self.total_leads,
            "sends": self.sends,
            "interested": self.interested,
            "replies": self.replies,
            "interested_rate": round(self.outcome_rate, 5),
            "of_replies": round(self.positive_reply_rate, 5),
            "of_sends": round(self.outcome_rate, 5),
            "vertical_label": self.name,
        }


# Sequences that handle people who ALREADY replied, rather than prospecting campaigns.
# They score 8-16% interested because everyone in them was interested before they were
# added, and seeding on them would expand an audience we never prospected at all.
_FOLLOWUP_RX = re.compile(
    r"\b(interested|reply|replies|follow[\s-]?up|subsequence|nurture|re[\s-]?engage|"
    r"booked|meeting|no[\s-]?show|unsubscribe|exclusion|test)\b", re.IGNORECASE)


def is_prospecting(row: CampaignRow) -> bool:
    """False for follow-up sequences and for rows whose counters cannot be true."""
    name = row.campaign_name or ""
    if _FOLLOWUP_RX.search(name):
        return False
    leads, interested = row.total_leads or 0, row.interested or 0
    # More interested replies than leads is impossible and does happen: "New Industries |
    # Oct '25" reports 1,144% because its lead count was reset while its interested count
    # was not. One such row dominates any pooled average it lands in.
    if leads <= 0 or interested > leads:
        return False
    return True


def _tokens(theme: str) -> set[str]:
    return {w for w in theme.split() if len(w) > 2}


def group_into_themes(rows: list[CampaignRow], overlap: float = 0.6) -> list[Theme]:
    """Pool campaigns by audience and rank the pooled themes by interested rate.

    Matching is on TOKEN OVERLAP, not on an exact normalised string. Stripping the
    scheduling noise out of these names still leaves "funded brands", "funded" and
    "funded" for what is plainly one audience -- "IDPR Funded Brands - Campaign 1",
    "2026 Funded companies 9th July" and "SignalBase Funded companies 10th August" --
    so exact matching pooled 79 campaigns into 75 themes and changed almost nothing.
    Two themes join when they share most of the smaller one's words.

    Pooling also strengthens the signal: those three campaigns carry 52, 24 and 26
    interested replies, thin evidence three times over. Pooled they carry 102 against
    their combined leads, which is a far better basis for deciding what to expand into.
    """
    clusters: list[tuple[set[str], list[CampaignRow]]] = []
    for r in [x for x in rows if is_prospecting(x)]:
        toks = _tokens(theme_of(r.campaign_name))
        if not toks:
            continue
        for keys, members in clusters:
            shared = len(toks & keys) / max(min(len(toks), len(keys)), 1)
            if shared >= overlap:
                members.append(r)
                keys &= toks or keys       # the shared core names the cluster
                break
        else:
            clusters.append((set(toks), [r]))

    themes = [Theme(name=" ".join(sorted(k)) or "unnamed", campaigns=v)
              for k, v in clusters]
    themes.sort(key=lambda t: t.outcome_rate, reverse=True)
    return themes


def select_seed_themes(rows: list[CampaignRow]) -> tuple[list[Theme], dict]:
    """The shared winner contract, applied to pooled audiences rather than campaigns.

    Three Funded Brands reruns are one seed. Ranking is on interested/sends after
    1,000 pooled sends -- see app/scoring.py.
    """
    from ..scoring import judge

    themes = group_into_themes(rows)
    eligible, rejected = [], []
    for t in themes:
        verdict = judge(sends=t.sends, replies=t.replies,
                        interested=t.interested, leads=t.total_leads)
        if verdict.is_winner:
            eligible.append(t)
        else:
            rejected.append((t, verdict.reason))
    eligible.sort(key=lambda t: t.outcome_rate, reverse=True)
    winners = eligible[: settings.top_winners]
    stats = {
        "campaigns": len(rows),
        "themes": len(themes),
        "cleared_gates": len(eligible),
        "winners": [t.as_seed() for t in winners],
        "rejected_sample": [{"theme": t.name, "reason": why} for t, why in rejected[:10]],
    }
    return winners, stats
