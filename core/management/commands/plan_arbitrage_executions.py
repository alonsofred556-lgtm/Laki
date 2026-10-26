"""Create quote-bound execution plans for funded user bot wallets.

This worker is intentionally not a trading signer. It only creates durable
Execution records after a fresh Pangea buy/bridge/sell quote still clears the
configured policy. A private, audited signer must consume those plans and
submit idempotent lifecycle events.
"""

import time

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.arbitrage import fresh_user_quotes
from core.assets import native_asset
from core.execution import create_execution_from_opportunity
from core.funding import account_for, ledger_balance
from core.models import ArbitrageOpportunity, BotWallet, WalletIdentity
from core.pangea import PangeaError
from core.views import bot_status


class Command(BaseCommand):
    help = "Re-quote approved opportunities and create signer-gated execution plans."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run one planning pass and exit.")
        parser.add_argument("--loop-seconds", type=int, default=30, help="Delay between planning passes.")
        parser.add_argument("--max-plans", type=int, default=10, help="Maximum new plans per pass.")

    def _funded(self, wallet, identity):
        asset = native_asset(wallet.chain)
        if not asset:
            return False
        account = account_for(identity, asset, wallet.chain, "bot-available", bot_wallet=wallet)
        return ledger_balance(account) > 0

    def _plan_once(self, max_plans):
        status = bot_status()
        if not status["executionReady"] or status["paused"] or not status["enabled"]:
            return {"planned": 0, "skipped": 0, "reason": "execution-gated", "status": status}

        now = timezone.now()
        opportunities = ArbitrageOpportunity.objects.filter(
            state__in=("approved-paper", "approved"),
            expires_at__gt=now,
        ).order_by("-net_bps", "-detected_at")[: max(1, max_plans * 4)]
        planned = 0
        skipped = 0
        for opportunity in opportunities:
            if planned >= max_plans:
                break
            identities = WalletIdentity.objects.filter(
                bot_wallets__chain=opportunity.buy_chain,
                bot_wallets__mode="external-bot",
                bot_wallets__status="active",
            ).distinct()
            for identity in identities:
                if planned >= max_plans:
                    break
                buy_wallet = BotWallet.objects.filter(
                    owner=identity, chain=opportunity.buy_chain,
                    mode="external-bot", status="active",
                ).first()
                sell_wallet = BotWallet.objects.filter(
                    owner=identity, chain=opportunity.sell_chain,
                    mode="external-bot", status="active",
                ).first()
                if not buy_wallet or not sell_wallet or not buy_wallet.address or not sell_wallet.address:
                    skipped += 1
                    continue
                if not self._funded(buy_wallet, identity) or not self._funded(sell_wallet, identity):
                    skipped += 1
                    continue
                try:
                    decision = fresh_user_quotes(
                        opportunity,
                        buy_wallet=buy_wallet.address,
                        sell_wallet=sell_wallet.address,
                    )
                    create_execution_from_opportunity(
                        identity=identity,
                        opportunity_id=opportunity.id,
                        bot_wallet=buy_wallet,
                        sell_bot_wallet=sell_wallet,
                        decision=decision,
                        quote_hash=decision["quoteHash"],
                    )
                    planned += 1
                except (PangeaError, ValueError):
                    skipped += 1
        return {"planned": planned, "skipped": skipped, "candidates": len(opportunities)}

    def handle(self, *args, **options):
        while True:
            result = self._plan_once(max(1, options["max_plans"]))
            self.stdout.write(self.style.SUCCESS(str(result)))
            if options["once"]:
                return
            time.sleep(max(10, options["loop_seconds"]))
