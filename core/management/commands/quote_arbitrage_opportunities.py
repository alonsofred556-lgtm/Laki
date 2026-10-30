"""Drain the durable Pangea discovery-quote queue.

This worker expands discovery coverage beyond the synchronous scan budget. It
never signs or broadcasts a transaction, and its scanner-wallet quotes are
not valid user execution authorization; the execution planner must obtain a
fresh quote for each user's active bot wallets.
"""

import time
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.arbitrage import quote_discovery_opportunity
from core.models import ArbitrageOpportunity
from core.pangea import PangeaError, request_json


class Command(BaseCommand):
    help = "Drain queued Pangea route quotes without signing or broadcasting."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Process one batch and exit.")
        parser.add_argument("--loop-seconds", type=int, default=int(getattr(settings, "CONI_ARBITRAGE_QUOTE_INTERVAL_SECONDS", 5)), help="Delay between batches.")
        parser.add_argument(
            "--batch-size",
            type=int,
            default=None,
            help="Maximum routes to quote per batch; defaults to CONI_ARBITRAGE_MAX_QUOTE_REQUESTS.",
        )

    def _claim(self, limit):
        now = timezone.now()
        stale_cutoff = now - timedelta(seconds=300)
        with transaction.atomic():
            rows = list(
                ArbitrageOpportunity.objects.select_for_update(skip_locked=True)
                .filter(quote_hash__isnull=True, detected_at__gte=stale_cutoff)
                .filter(Q(quote_available_at__isnull=True) | Q(quote_available_at__lte=now))
                .filter(Q(quote_lease_until__isnull=True) | Q(quote_lease_until__lte=now))
                .order_by("detected_at")[:limit]
            )
            lease_until = now + timedelta(seconds=90)
            for opportunity in rows:
                opportunity.quote_attempts += 1
                opportunity.quote_lease_until = lease_until
                opportunity.quote_available_at = None
                opportunity.save(update_fields=["quote_attempts", "quote_lease_until", "quote_available_at"])
            return rows

    @staticmethod
    def _apply_quote(opportunity_id, row):
        now = timezone.now()
        with transaction.atomic():
            opportunity = ArbitrageOpportunity.objects.select_for_update().get(id=opportunity_id)
            quote_hash = row.get("quoteHash")
            opportunity.quote_hash = quote_hash or None
            opportunity.decision = row
            opportunity.gross_profit_usd = row.get("executableGrossProfitUsd", "0")
            opportunity.total_cost_usd = row.get("totalCostUsd", "0")
            opportunity.net_profit_usd = row.get("netProfitUsd", "0")
            opportunity.net_bps = int(row.get("netBps", 0))
            opportunity.state = "approved-paper" if row.get("executable") else "rejected"
            opportunity.expires_at = now + timedelta(seconds=60)
            opportunity.quote_lease_until = None
            opportunity.quote_available_at = None
            opportunity.quote_last_error = ""
            opportunity.save(update_fields=[
                "quote_hash", "decision", "gross_profit_usd", "total_cost_usd",
                "net_profit_usd", "net_bps", "state", "expires_at",
                "quote_lease_until", "quote_available_at", "quote_last_error",
            ])

    @staticmethod
    def _release(opportunity_id, error):
        now = timezone.now()
        with transaction.atomic():
            opportunity = ArbitrageOpportunity.objects.select_for_update().get(id=opportunity_id)
            # Exponential backoff prevents a malformed launch or upstream
            # outage from consuming the entire Pangea request budget.
            delay = min(300, 2 ** min(int(opportunity.quote_attempts), 8))
            opportunity.quote_lease_until = None
            opportunity.quote_available_at = now + timedelta(seconds=delay)
            opportunity.quote_last_error = str(error)[:160]
            opportunity.state = "rejected"
            opportunity.save(update_fields=["quote_lease_until", "quote_available_at", "quote_last_error", "state"])

    def _run_batch(self, limit):
        if not getattr(settings, "CONI_ARBITRAGE_REQUIRE_QUOTES", True):
            return {"quoted": 0, "failed": 0, "reason": "quotes-disabled"}
        rows = self._claim(max(1, limit))
        if not rows:
            return {"quoted": 0, "failed": 0, "queued": 0}
        try:
            data = request_json("/pools")
        except PangeaError as exc:
            for opportunity in rows:
                self._release(opportunity.id, exc)
            return {"quoted": 0, "failed": len(rows), "queued": len(rows), "error": str(exc)}

        quoted = failed = 0
        for opportunity in rows:
            try:
                row = quote_discovery_opportunity(opportunity, data=data)
                self._apply_quote(opportunity.id, row)
                quoted += 1
            except (PangeaError, ValueError, TypeError) as exc:
                self._release(opportunity.id, exc)
                failed += 1
        return {"quoted": quoted, "failed": failed, "queued": len(rows)}

    def handle(self, *args, **options):
        limit = options["batch_size"] or int(getattr(settings, "CONI_ARBITRAGE_MAX_QUOTE_REQUESTS", 20))
        while True:
            result = self._run_batch(limit)
            self.stdout.write(self.style.SUCCESS(str(result)))
            if options["once"]:
                return
            time.sleep(max(3, options["loop_seconds"]))
