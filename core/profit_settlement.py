"""Realized-profit settlement in verified USDG on Robinhood Chain."""

from decimal import Decimal, InvalidOperation, ROUND_DOWN

from django.conf import settings

from .models import Execution, ProfitSettlement, WalletIdentity


def _decimal(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def prepare_usdg_settlement(*, execution_id, user_identity_id, realized_profit_usd, usdg_price_usd, recipient):
    """Create a payout instruction only after a settled execution is reconciled.

    The USDG token address, decimals, and price are explicit inputs/configuration;
    this function never guesses them and never broadcasts a transaction.
    """
    execution = Execution.objects.get(id=execution_id)
    identity = WalletIdentity.objects.get(id=user_identity_id)
    realized = _decimal(realized_profit_usd)
    price = _decimal(usdg_price_usd)
    if execution.state != "settled":
        raise ValueError("profit must be settled before USDG payout preparation")
    if not settings.CONI_PROFIT_SETTLEMENT_ENABLED:
        raise ValueError("USDG settlement is disabled")
    if not settings.CONI_PROFIT_SETTLEMENT_TOKEN_ADDRESS or settings.CONI_PROFIT_SETTLEMENT_DECIMALS is None:
        raise ValueError("verified USDG token address and decimals are required")
    if settings.CONI_PROFIT_SETTLEMENT_CHAIN != "robinhood" or identity.chain != "robinhood":
        raise ValueError("USDG settlement requires the user's Robinhood Chain wallet identity")
    if realized <= 0 or price <= 0 or not recipient or recipient.lower() != identity.address.lower():
        raise ValueError("invalid realized profit, USDG price, or recipient")
    amount = (realized / price * (Decimal("10") ** settings.CONI_PROFIT_SETTLEMENT_DECIMALS)).to_integral_value(rounding=ROUND_DOWN)
    if amount <= 0:
        raise ValueError("realized profit is below one USDG base unit")
    settlement, _ = ProfitSettlement.objects.update_or_create(
        execution=execution,
        defaults={
            "user_identity": identity,
            "realized_profit_usd": realized,
            "settlement_asset": settings.CONI_PROFIT_SETTLEMENT_ASSET,
            "settlement_chain": settings.CONI_PROFIT_SETTLEMENT_CHAIN,
            "settlement_token": settings.CONI_PROFIT_SETTLEMENT_TOKEN_ADDRESS,
            "settlement_price_usd": price,
            "amount_base_units": amount,
            "recipient": identity.address,
            "state": "pending-approval",
        },
    )
    return settlement
