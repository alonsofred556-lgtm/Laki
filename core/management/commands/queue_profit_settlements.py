"""Prepare signer-gated USDG payout instructions for settled user trades.

This worker creates durable instructions only. It never signs, broadcasts, or
marks a payout complete. It stays inert until the operator supplies an
explicitly verified USDG address, decimals, and current USD price.
"""

from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.management.base import BaseCommand

from core.audit import record_event
from core.models import Execution, ProfitSettlement
from core.profit_settlement import prepare_usdg_settlement


class Command(BaseCommand):
    help = "Queue signer-gated USDG payout instructions for settled executions."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run one queue pass and exit.")
        parser.add_argument("--loop-seconds", type=int, default=30, help="Delay between queue passes.")
        parser.add_argument("--limit", type=int, default=100, help="Maximum settled executions per pass.")

    @staticmethod
    def _configured_price():
        raw = str(getattr(settings, "CONI_PROFIT_SETTLEMENT_PRICE_USD", "") or "")
        try:
            price = Decimal(raw)
        except (InvalidOperation, TypeError, ValueError):
            return None
        return price if price > 0 else None

    def _run_once(self, limit):
        price = self._configured_price()
        if not settings.CONI_PROFIT_SETTLEMENT_ENABLED:
            return {"prepared": 0, "skipped": 0, "failed": 0, "reason": "settlement-disabled"}
        if not settings.CONI_PROFIT_SETTLEMENT_TOKEN_ADDRESS or settings.CONI_PROFIT_SETTLEMENT_DECIMALS is None or price is None:
            return {"prepared": 0, "skipped": 0, "failed": 0, "reason": "verified-usdg-config-or-price-missing"}

        rows = list(Execution.objects.select_related("user_identity").filter(
            state="settled",
            realized_profit_usd__gt=0,
            user_identity__isnull=False,
        ).exclude(
            profit_settlement__isnull=False,
        ).order_by("realized_at")[: max(1, min(limit, 1000))])
        prepared = skipped = failed = 0
        for execution in rows:
            if ProfitSettlement.objects.filter(execution_id=execution.id).exists():
                skipped += 1
                continue
            identity = execution.user_identity
            try:
                settlement = prepare_usdg_settlement(
                    execution_id=execution.id,
                    user_identity_id=identity.id,
                    realized_profit_usd=execution.realized_profit_usd,
                    usdg_price_usd=price,
                    recipient=identity.address,
                )
                record_event(
                    "profit_settlement.queued",
                    actor_type="system",
                    subject_type="profit_settlement",
                    subject_id=settlement.id,
                    metadata={"executionId": str(execution.id), "recipient": identity.address},
                )
                prepared += 1
            except (ValueError, TypeError, InvalidOperation):
                failed += 1
        return {"prepared": prepared, "skipped": skipped, "failed": failed, "candidates": len(rows)}

    def handle(self, *args, **options):
        limit = max(1, min(int(options["limit"]), 1000))
        while True:
            result = self._run_once(limit)
            self.stdout.write(self.style.SUCCESS(str(result)))
            if options["once"]:
                return
            time.sleep(max(5, int(options["loop_seconds"])))
