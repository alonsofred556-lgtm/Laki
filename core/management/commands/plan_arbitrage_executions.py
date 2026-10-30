"""Create quote-bound execution plans for funded user bot wallets.

This worker is intentionally not a trading signer. It only creates durable
Execution records after a fresh Pangea buy/bridge/sell quote still clears the
configured policy. A private, audited signer must consume those plans and
submit idempotent lifecycle events.
"""

import time
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from django.db.models import Q

from core.arbitrage import fresh_user_quotes
from core.assets import native_asset
from core.bot_controls import effective_user_policy, global_bot_control
from core.execution import create_execution_from_opportunity
from core.funding import account_for, ledger_balance
from core.models import ArbitrageOpportunity, BotControl, BotWallet, Execution, WalletIdentity
from core.pangea import PangeaError
from core.planner_funding import required_native_units
from core.views import bot_status


ACTIVE_EXECUTION_STATES = (
    "planned", "awaiting-signer", "buy-submitted", "buy-confirmed",
    "bridge-source-submitted", "bridge-source-confirmed", "bridge-in-flight",
    "bridge-destination-confirmed", "sell-submitted", "sell-confirmed",
)


class Command(BaseCommand):
    help = "Re-quote approved opportunities and create signer-gated execution plans."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run one planning pass and exit.")
        parser.add_argument("--loop-seconds", type=int, default=int(getattr(settings, "CONI_ARBITRAGE_PLAN_INTERVAL_SECONDS", 5)), help="Delay between planning passes.")
        parser.add_argument("--max-plans", type=int, default=10, help="Maximum new plans per pass.")

    def _has_active_route(self, wallet):
        return Execution.objects.filter(
            Q(bot_wallet_id=wallet.id) | Q(sell_bot_wallet_id=wallet.id),
            state__in=ACTIVE_EXECUTION_STATES,
        ).exists()

    def _funded(self, wallet, identity, decision=None, *, role="sell"):
        asset = native_asset(wallet.chain)
        if not asset:
            return False
        if self._has_active_route(wallet):
            return False
        try:
            required = required_native_units(decision or {}, wallet.chain, role=role)
        except ValueError:
            return False
        if required is None:
            return False
        account = account_for(identity, asset, wallet.chain, "bot-available", bot_wallet=wallet)
        return ledger_balance(account) >= required

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
        global_control = global_bot_control()
        for opportunity in opportunities:
            if planned >= max_plans:
                break
            identities = WalletIdentity.objects.filter(
                bot_wallets__chain=opportunity.buy_chain,
                bot_wallets__mode__in=("external-bot", "internal-bot"),
                bot_wallets__status="active",
                bot_wallets__signer_key_ref__gt="",
            ).distinct()
            for identity in identities:
                if planned >= max_plans:
                    break
                user_policy = effective_user_policy(identity, global_control)
                if not user_policy["enabled"] or user_policy["paused"]:
                    skipped += 1
                    continue
                buy_wallet = BotWallet.objects.filter(
                    owner=identity, chain=opportunity.buy_chain,
                    mode=user_policy["walletMode"], status="active", signer_key_ref__gt="",
                ).first()
                sell_wallet = BotWallet.objects.filter(
                    owner=identity, chain=opportunity.sell_chain,
                    mode=user_policy["walletMode"], status="active", signer_key_ref__gt="",
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
                    if decision.get("netBps", 0) < user_policy["minNetBps"] or Decimal(str(decision.get("notionalUsd", "0"))) > Decimal(str(user_policy["maxNotionalUsd"])):
                        skipped += 1
                        continue
                    # Re-check funding after the network quote and hold both
                    # wallet rows while creating the plan. This keeps two
                    # planner processes from consuming the same native balance.
                    with transaction.atomic():
                        wallet_ids = sorted({str(buy_wallet.id), str(sell_wallet.id)})
                        locked = {
                            str(item.id): item
                            for item in BotWallet.objects.select_for_update().filter(id__in=wallet_ids).order_by("id")
                        }
                        locked_buy = locked.get(str(buy_wallet.id))
                        locked_sell = locked.get(str(sell_wallet.id))
                        if (
                            not locked_buy or not locked_sell
                            or not self._funded(locked_buy, identity, decision, role="buy")
                            or not self._funded(locked_sell, identity, decision, role="sell")
                        ):
                            skipped += 1
                            continue
                        create_execution_from_opportunity(
                            identity=identity,
                            opportunity_id=opportunity.id,
                            bot_wallet=locked_buy,
                            sell_bot_wallet=locked_sell,
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
            time.sleep(max(3, options["loop_seconds"]))
