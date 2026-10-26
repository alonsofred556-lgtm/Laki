"""Per-user bot-wallet lifecycle and read-only portfolio summaries."""

from decimal import Decimal

from django.db import transaction
from django.db.models import Sum

from .audit import record_event
from .assets import asset_decimals, supported_asset
from .funding import account_for, ledger_balance
from .models import BotWallet, BotWalletTransfer, Execution, LedgerAccount, LedgerEntry, WalletIdentity


BOT_WALLET_CHAINS = ("ethereum", "base", "robinhood", "bsc", "solana")
EVM_BOT_CHAINS = {"ethereum", "base", "robinhood", "bsc"}


def ensure_bot_wallet(identity, *, mode="external-bot"):
    """Create the durable per-user bot-wallet record without inventing an address.

    A real address must be provisioned by the audited signer/MPC service. The
    web process never generates, receives, or stores a bot private key.
    """
    if mode not in {"external-bot", "internal-bot"}:
        raise ValueError("invalid bot wallet mode")
    wallet, created = BotWallet.objects.get_or_create(
        owner=identity,
        chain=identity.chain,
        mode=mode,
        defaults={"source_address": identity.address, "status": "awaiting-signer-provision"},
    )
    if created:
        record_event("bot_wallet.created", actor_type="wallet", actor_id=identity.id, subject_type="bot_wallet", subject_id=wallet.id, metadata={"chain": identity.chain, "mode": mode, "status": wallet.status})
    return wallet


def ensure_bot_wallets(identity, *, mode="external-bot"):
    """Create one durable placeholder per supported chain for this identity.

    The records are not addresses. They become usable only after the private
    signer/MPC service provisions and attests a chain-specific address.
    """
    wallets = []
    for chain in BOT_WALLET_CHAINS:
        source = identity.address if chain == identity.chain or (chain in EVM_BOT_CHAINS and identity.chain in EVM_BOT_CHAINS) else ""
        wallet, created = BotWallet.objects.get_or_create(
            owner=identity,
            chain=chain,
            mode=mode,
            defaults={"source_address": source, "status": "awaiting-signer-provision"},
        )
        if source and not wallet.source_address:
            wallet.source_address = source
            wallet.save(update_fields=["source_address", "updated_at"])
        if created:
            record_event("bot_wallet.created", actor_type="wallet", actor_id=identity.id, subject_type="bot_wallet", subject_id=wallet.id, metadata={"chain": chain, "mode": mode, "status": wallet.status})
        wallets.append(wallet)
    return wallets


def bot_wallet_payload(wallet):
    return {
        "id": str(wallet.id),
        "chain": wallet.chain,
        "address": wallet.address or None,
        "mode": wallet.mode,
        "custody": wallet.custody,
        "status": wallet.status,
        "sourceAddress": wallet.source_address,
        "createdAt": wallet.created_at.isoformat(),
        "updatedAt": wallet.updated_at.isoformat(),
    }


def _bot_balance(wallet, asset=None):
    filters = {"owner": wallet.owner, "bot_wallet": wallet, "chain": wallet.chain, "kind": "bot-available"}
    if asset:
        filters["asset"] = asset
    rows = []
    from .models import LedgerAccount
    for account in LedgerAccount.objects.filter(**filters):
        rows.append({"asset": account.asset, "chain": account.chain, "amountBaseUnits": str(ledger_balance(account))})
    return rows


def bot_wallet_summary(identity):
    wallets = BotWallet.objects.filter(owner=identity).order_by("chain", "mode")
    executions = Execution.objects.filter(user_identity=identity).order_by("-created_at")
    realized = sum((item.realized_profit_usd or Decimal("0") for item in executions if item.state == "settled"), Decimal("0"))
    expected = sum((item.expected_profit_usd or Decimal("0") for item in executions), Decimal("0"))
    tokens = list(executions.values("slug").annotate(trades=Sum("expected_profit_usd")))
    transfers = BotWalletTransfer.objects.filter(owner=identity).order_by("-created_at")[:50]
    payload_wallets = [
            {**bot_wallet_payload(wallet), "balances": _bot_balance(wallet)}
            for wallet in wallets
        ]
    current = next((item for item in payload_wallets if item["chain"] == identity.chain and item["mode"] == "external-bot"), None)
    return {
        "currentChain": identity.chain,
        "currentWallet": current,
        "wallets": payload_wallets,
        "pnl": {"realizedUsd": str(realized), "expectedUsd": str(expected), "settledTrades": executions.filter(state="settled").count()},
        "tradedTokens": [{"slug": item["slug"], "expectedProfitUsd": str(item["trades"] or 0)} for item in tokens],
        "history": [
            {"id": str(item.id), "kind": "trade", "slug": item.slug, "state": item.state, "expectedProfitUsd": str(item.expected_profit_usd or 0), "realizedProfitUsd": str(item.realized_profit_usd or 0), "createdAt": item.created_at.isoformat()}
            for item in executions[:50]
        ] + [
            {"id": str(item.id), "kind": item.direction, "asset": item.asset, "chain": item.chain, "amountBaseUnits": str(item.amount_base_units), "state": item.state, "txHash": item.tx_hash or None, "createdAt": item.created_at.isoformat()}
            for item in transfers
        ],
    }


@transaction.atomic
def create_bot_transfer(*, identity, wallet_id, direction, asset, amount_base_units, destination, idempotency_key):
    wallet = BotWallet.objects.select_for_update().filter(id=wallet_id, owner=identity, chain=identity.chain).first()
    if not wallet:
        raise ValueError("bot wallet not found")
    if wallet.status != "active" or not wallet.address:
        raise ValueError("bot wallet is awaiting audited signer provisioning")
    if direction not in {"topup", "withdrawal"}:
        raise ValueError("invalid bot transfer direction")
    if int(amount_base_units) <= 0 or not destination:
        raise ValueError("invalid bot transfer")
    if not supported_asset(asset, wallet.chain) or asset_decimals(asset, wallet.chain) is None:
        raise ValueError("unsupported bot wallet asset")
    if direction == "topup" and destination.lower() != wallet.address.lower():
        raise ValueError("top-up destination must be the user's bot wallet")
    if direction == "withdrawal" and destination.lower() != identity.address.lower():
        raise ValueError("bot withdrawal destination must be the authenticated wallet")
    transfer, created = BotWalletTransfer.objects.get_or_create(
        idempotency_key=idempotency_key,
        defaults={
            "bot_wallet": wallet,
            "owner": identity,
            "direction": direction,
            "asset": asset,
            "chain": identity.chain,
            "amount_base_units": amount_base_units,
            "destination": destination,
            "state": "pending-approval",
        },
    )
    if not created and transfer.owner_id != identity.id:
        raise ValueError("idempotency key is already in use")
    if not created:
        if (
            transfer.bot_wallet_id != wallet.id
            or transfer.direction != direction
            or transfer.asset != asset
            or int(transfer.amount_base_units) != int(amount_base_units)
            or transfer.destination.lower() != destination.lower()
        ):
            raise ValueError("idempotency key does not match the original transfer")
        return transfer
    if direction == "withdrawal":
        available = account_for(identity, asset, wallet.chain, "bot-available", bot_wallet=wallet)
        reserved = account_for(identity, asset, wallet.chain, "bot-withdrawal-reserved", bot_wallet=wallet)
        available = LedgerAccount.objects.select_for_update().get(id=available.id)
        reserved = LedgerAccount.objects.select_for_update().get(id=reserved.id)
        if ledger_balance(available) < amount_base_units:
            raise ValueError("insufficient bot wallet balance")
        LedgerEntry.objects.get_or_create(
            reference_type="bot-withdrawal-reservation",
            reference_id=str(transfer.id),
            debit_account=available,
            credit_account=reserved,
            defaults={"asset": asset, "amount_base_units": amount_base_units},
        )
    record_event("bot_wallet.transfer.requested", actor_type="wallet", actor_id=identity.id, subject_type="bot_transfer", subject_id=transfer.id, metadata={"direction": direction, "asset": asset, "chain": identity.chain, "amountBaseUnits": amount_base_units})
    return transfer
