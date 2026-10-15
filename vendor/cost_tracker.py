"""
cost_tracker.py — Track and aggregate costs across GTM pipeline steps.

Logs cost reports to stderr as JSON for Node.js worker to capture:
    {"type": "cost_report", "data": {"total_cost": 0.50, "by_service": {...}, "entries": [...]}}

Usage:
    from cost_tracker import CostTracker
    tracker = CostTracker()
    tracker.discolike_query(records_returned=50, query_count=1, details={...})
    tracker.aiark_contact(num_contacts=10, details={...})
    tracker.log_to_stderr()
"""

import json
import sys
from typing import Any, Dict, List, Optional


class BudgetExceededError(Exception):
    """Raised when budget limit is exceeded."""
    pass


class CostReport:
    """Read-only snapshot returned by CostTracker.get_report()."""
    def __init__(self, total_cost: float, by_service: Dict[str, float], entries: List["CostEntry"]):
        self.total_cost = total_cost
        self.by_service = by_service
        self.entries = entries


AIARK_PRICING = {
    "starter": {"per_query": 0.18, "per_1k_records": 3.50},
    "pro": {"per_query": 0.10, "per_1k_records": 2.00},
    "team": {"per_query": 0.05, "per_1k_records": 1.00},
    "enterprise": {"per_query": 0.00, "per_1k_records": 0.00},
}


class CostEntry:
    """Single cost entry."""
    def __init__(
        self,
        service: str,
        operation: str,
        quantity: int,
        unit_cost: float,
        total_cost: float,
        model: Optional[str] = None,
        details: Optional[str] = None,
    ):
        self.service = service
        self.operation = operation
        self.quantity = quantity
        self.unit_cost = unit_cost
        self.total_cost = total_cost
        self.model = model
        self.details = details

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "service": self.service,
            "operation": self.operation,
            "quantity": self.quantity,
            "unit_cost": self.unit_cost,
            "total_cost": self.total_cost,
        }
        if self.model:
            d["model"] = self.model
        if self.details:
            d["details"] = self.details
        return d


class CostTracker:
    """Track costs across GTM pipeline steps with plan-aware pricing and budget enforcement."""

    def __init__(self, plan: str = "starter", budget_limit: Optional[float] = None):
        self.entries: List[CostEntry] = []
        self.by_service: Dict[str, float] = {}
        self.plan = plan.lower()
        self.budget_limit = budget_limit
        self.session_total = 0.0

    def _add_entry(
        self,
        service: str,
        operation: str,
        quantity: int,
        unit_cost: float,
        model: Optional[str] = None,
        details: Optional[str] = None,
    ) -> None:
        total = quantity * unit_cost
        entry = CostEntry(
            service=service,
            operation=operation,
            quantity=quantity,
            unit_cost=unit_cost,
            total_cost=total,
            model=model,
            details=details,
        )
        self.entries.append(entry)
        self.by_service[service] = self.by_service.get(service, 0.0) + total

    def discolike_query(
        self,
        records_returned: int,
        query_count: int = 1,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        DiscoLike pricing: $0.18 per query + $3.50 per 1,000 records returned.
        """
        query_cost = 0.18 * query_count
        record_cost = (records_returned / 1000) * 3.50

        # Query fee
        self._add_entry(
            service="discolike",
            operation="query",
            quantity=query_count,
            unit_cost=0.18,
            details=json.dumps(details) if details else None,
        )

        # Record fee
        if records_returned > 0:
            self._add_entry(
                service="discolike",
                operation="records",
                quantity=records_returned,
                unit_cost=3.50 / 1000,
                details=json.dumps(details) if details else None,
            )

    def aiark_contact(
        self,
        num_contacts: int,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        AI Ark contact search: ~$0.50 per contact (conservative estimate for search + email finder).
        """
        unit_cost = 0.50
        self._add_entry(
            service="aiark",
            operation="contact_search",
            quantity=num_contacts,
            unit_cost=unit_cost,
            details=json.dumps(details) if details else None,
        )

    def email_verification(
        self,
        provider: str,
        emails_verified: int,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Email verification providers: varies by provider.
        - MillionVerifier: $0.003 per email
        - TryKitt: $0.005 per email
        - BounceBan: $0.005 per email
        - LeadMagic: $0.001 per email
        - IcyPeas: $0.002 per email
        - EmailGuard: $0.002 per email
        """
        unit_costs = {
            "millionverifier": 0.003,
            "trykitt": 0.005,
            "bounceban": 0.005,
            "leadmagic": 0.001,
            "icypeas": 0.002,
            "emailguard": 0.002,
        }
        unit_cost = unit_costs.get(provider.lower(), 0.003)

        self._add_entry(
            service="email_verification",
            operation=f"{provider}_verify",
            quantity=emails_verified,
            unit_cost=unit_cost,
            details=json.dumps(details) if details else None,
        )

    def anthropic_call(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Anthropic pricing (Claude 3.5 Sonnet):
        - Input: $0.80 per 1M tokens
        - Output: $2.40 per 1M tokens
        """
        input_cost = (input_tokens / 1_000_000) * 0.80
        output_cost = (output_tokens / 1_000_000) * 2.40

        # Input tokens
        if input_tokens > 0:
            self._add_entry(
                service="anthropic",
                operation="input_tokens",
                quantity=input_tokens,
                unit_cost=0.80 / 1_000_000,
                model=model,
                details=json.dumps(details) if details else None,
            )

        # Output tokens
        if output_tokens > 0:
            self._add_entry(
                service="anthropic",
                operation="output_tokens",
                quantity=output_tokens,
                unit_cost=2.40 / 1_000_000,
                model=model,
                details=json.dumps(details) if details else None,
            )

    def signalbase_signal(
        self,
        signal_type: str,
        companies: int,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        SignalBase intent signals: ~$1.00 per company (varies by signal type).
        """
        unit_cost = 1.00
        self._add_entry(
            service="signalbase",
            operation=f"{signal_type}_signal",
            quantity=companies,
            unit_cost=unit_cost,
            details=json.dumps(details) if details else None,
        )

    def serper_search(
        self,
        queries: int,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Serper search: $0.01 per query for news/web search.
        """
        unit_cost = 0.01
        self._add_entry(
            service="serper",
            operation="search",
            quantity=queries,
            unit_cost=unit_cost,
            details=json.dumps(details) if details else None,
        )

    def harvest_api_profile(
        self,
        profiles: int,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Harvest API profile enrichment: ~$0.02 per profile (conservative for Pro plan).
        """
        unit_cost = 0.02
        self._add_entry(
            service="harvest",
            operation="profile_lookup",
            quantity=profiles,
            unit_cost=unit_cost,
            details=json.dumps(details) if details else None,
        )

    def aiark_query(
        self,
        query_count: int = 1,
        records_returned: int = 0,
        plan: Optional[str] = None,
    ) -> float:
        """
        Track AI Ark search costs with plan-aware pricing.
        Query fee + per-record fee based on plan tier.
        Returns total cost for this query.
        Raises BudgetExceededError if budget limit exceeded.
        """
        use_plan = (plan or self.plan).lower()
        pricing = AIARK_PRICING.get(use_plan, AIARK_PRICING["starter"])
        query_fee = pricing["per_query"] * query_count
        record_fee = (records_returned / 1000.0) * pricing["per_1k_records"]
        total_cost = query_fee + record_fee

        # Query fee entry
        self._add_entry(
            service="aiark",
            operation="query",
            quantity=query_count,
            unit_cost=pricing["per_query"],
        )

        # Record fee entry (if any records)
        if records_returned > 0:
            self._add_entry(
                service="aiark",
                operation="records",
                quantity=records_returned,
                unit_cost=pricing["per_1k_records"] / 1000.0,
            )

        self.session_total += total_cost
        self._check_budget()
        return total_cost

    def estimate_aiark_query(
        self,
        query_count: int = 1,
        records_returned: int = 0,
        plan: Optional[str] = None,
    ) -> float:
        """
        Estimate AI Ark search cost WITHOUT accumulating to session total.
        Used for dry-run cost estimation.
        Returns estimated cost.
        """
        use_plan = (plan or self.plan).lower()
        pricing = AIARK_PRICING.get(use_plan, AIARK_PRICING["starter"])
        query_fee = pricing["per_query"] * query_count
        record_fee = (records_returned / 1000.0) * pricing["per_1k_records"]
        return query_fee + record_fee

    def _check_budget(self) -> None:
        """Check budget and warn at thresholds or raise if exceeded."""
        if not self.budget_limit:
            return

        percent_used = (self.session_total / self.budget_limit) * 100
        if percent_used >= 100:
            raise BudgetExceededError(
                f"Budget limit exceeded: ${self.session_total:.2f} / ${self.budget_limit:.2f}"
            )
        elif percent_used >= 95:
            print(
                f"⚠️  Budget warning (95%): ${self.session_total:.2f} / ${self.budget_limit:.2f}",
                file=sys.stderr,
            )
        elif percent_used >= 80:
            print(
                f"⚠️  Budget warning (80%): ${self.session_total:.2f} / ${self.budget_limit:.2f}",
                file=sys.stderr,
            )

    def total_cost(self) -> float:
        """Return total cost across all entries."""
        return sum(entry.total_cost for entry in self.entries)

    def get_report(self) -> "CostReport":
        """Snapshot of current state. Recomputes by_service from entries so
        callers that append entries directly (e.g. column_pipeline aggregator)
        still get an accurate per-service breakdown."""
        by_svc: Dict[str, float] = {}
        for e in self.entries:
            by_svc[e.service] = by_svc.get(e.service, 0.0) + e.total_cost
        return CostReport(
            total_cost=sum(e.total_cost for e in self.entries),
            by_service=by_svc,
            entries=list(self.entries),
        )

    def log_to_stderr(self) -> None:
        """Log cost report to stderr as JSON for Node.js to capture."""
        report = {
            "type": "cost_report",
            "data": {
                "total_cost": round(self.total_cost(), 4),
                "by_service": {k: round(v, 4) for k, v in self.by_service.items()},
                "entries": [e.to_dict() for e in self.entries],
            },
        }
        print(json.dumps(report), file=sys.stderr)
