"""Per-user bot-wallet lifecycle and read-only portfolio summaries."""

from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from .audit import record_event
from .assets import asset_decimals, supported_asset
from .bot_controls import ensure_user_bot_control
from .funding import account_for, ledger_balance
from .models import BotWallet, BotWalletProvisionRequest, BotWalletTransfer, BotWalletTransferEvent, Execution, LedgerAccount, LedgerEntry, WalletIdentity


BOT_WALLET_CHAINS = ("ethereum", "base", "robinhood", "bsc", "solana")
EVM_BOT_CHAINS = {"ethereum", "base", "robinhood", "bsc"}
BOT_TRANSFER_EVENTS = {"submitted", "confirmed", "failed"}
PROVISION_LEASE_SECONDS = 120
_BOT_TRANSFER_TRANSITIONS = {
    "pending-approval": {"submitted": "submitted", "failed": "failed"},
    "submitted": {"confirmed": "confirmed", "failed": "failed"},
}


def chain_accessible(identity, chain):
    """An authenticated wallet may control its chain and the EVM sibling chains.

    EVM addresses are chain-independent, but a Solana identity must never be
    treated as an EVM signer (or vice versa). The actual transaction still
    requires the provider/signer to use the selected target chain.
    """
    chain = str(chain or "")
    identity_chain = getattr(identity, "chain", "")
    return chain == identity_chain or (chain in EVM_BOT_CHAINS and identity_chain in EVM_BOT_CHAINS)


def ensure_provision_request(wallet):
    """Ensure every unprovisioned bot wallet has one durable signer request."""
    request, _ = BotWalletProvisionRequest.objects.get_or_create(bot_wallet=wallet)
    return request


def claim_provision_requests(*, limit=20):
    """Lease pending wallet requests so a signer can safely retry after a crash."""
    # Backfill records created before the provisioning queue existed. The
    # unique one-to-one relation makes this safe when multiple signer polls
    # arrive at the same time.
    legacy_wallet_ids = BotWallet.objects.filter(
        status="awaiting-signer-provision",
        address="",
        provision_request__isnull=True,
    ).values_list("id", flat=True)[:limit]
    for wallet_id in legacy_wallet_ids:
        ensure_provision_request(BotWallet.objects.get(id=wallet_id))

    now = timezone.now()
    lease_until = now + timedelta(seconds=PROVISION_LEASE_SECONDS)
    claimed = []
    with transaction.atomic():
        candidates = BotWalletProvisionRequest.objects.select_for_update().select_related("bot_wallet", "bot_wallet__owner").filter(
            Q(status="pending") | Q(status="leased", lease_until__lt=now),
            bot_wallet__status="awaiting-signer-provision",
            bot_wallet__address="",
        ).order_by("created_at")[:limit]
        for request in candidates:
            request.status = "leased"
            request.attempt_count += 1
            request.lease_until = lease_until
            request.save(update_fields=["status", "attempt_count", "lease_until", "updated_at"])
            claimed.append(request)
    return claimed


def next_bot_transfer_state(current, event):
    if event not in BOT_TRANSFER_EVENTS:
        raise ValueError("unsupported bot transfer event")
    try:
        return _BOT_TRANSFER_TRANSITIONS[current][event]
    except KeyError as exc:
        raise ValueError(f"invalid bot transfer transition {current}->{event}") from exc


def bot_transfer_chain(wallet):
    """Return the chain that owns a bot transfer.

    The selected bot wallet, not the chain used to authenticate the external
    wallet, is authoritative. This matters when one EVM identity controls a
    bot wallet on a different EVM network.
    """
    chain = str(getattr(wallet, "chain", ""))
    if not chain:
        raise ValueError("bot wallet chain is required")
    return chain


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
    if wallet.status == "awaiting-signer-provision" and not wallet.address:
        ensure_provision_request(wallet)
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
        if wallet.status == "awaiting-signer-provision" and not wallet.address:
            ensure_provision_request(wallet)
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


def bot_wallet_summary(identity, selected_wallet_id=None):
    # Keep the read endpoint self-healing for sessions created before the
    # five-chain provisioning behavior was introduced.
    ensure_bot_wallets(identity, mode="external-bot")
    user_control = ensure_user_bot_control(identity)
    wallets = BotWallet.objects.filter(owner=identity).order_by("chain", "mode")
    executions = Execution.objects.filter(user_identity=identity).order_by("-created_at")
    realized = sum((item.realized_profit_usd or Decimal("0") for item in executions if item.state == "settled"), Decimal("0"))
    expected = sum((item.expected_profit_usd or Decimal("0") for item in executions), Decimal("0"))
    selected = next((wallet for wallet in wallets if selected_wallet_id and str(wallet.id) == str(selected_wallet_id)), None)
    if selected is None:
        selected = next((wallet for wallet in wallets if wallet.chain == identity.chain and wallet.mode == "external-bot"), None)
    selected_executions = executions.filter(bot_wallet=selected) if selected else executions.none()
    selected_realized = sum((item.realized_profit_usd or Decimal("0") for item in selected_executions if item.state == "settled"), Decimal("0"))
    selected_expected = sum((item.expected_profit_usd or Decimal("0") for item in selected_executions), Decimal("0"))
    selected_tokens = list(selected_executions.values("slug").annotate(trades=Sum("expected_profit_usd")))
    transfer_query = BotWalletTransfer.objects.filter(owner=identity)
    transfers = transfer_query.order_by("-created_at")[:50]
    selected_transfers = transfer_query.filter(bot_wallet=selected).order_by("-created_at")[:50] if selected else transfer_query.none()
    selected_history = [
        {"id": str(item.id), "kind": "trade", "slug": item.slug, "state": item.state, "expectedProfitUsd": str(item.expected_profit_usd or 0), "realizedProfitUsd": str(item.realized_profit_usd or 0), "createdAt": item.created_at.isoformat()}
        for item in selected_executions[:50]
    ] + [
        {"id": str(item.id), "kind": item.direction, "asset": item.asset, "chain": item.chain, "amountBaseUnits": str(item.amount_base_units), "state": item.state, "txHash": item.tx_hash or None, "createdAt": item.created_at.isoformat()}
        for item in selected_transfers
    ]
    payload_wallets = [
            {**bot_wallet_payload(wallet), "balances": _bot_balance(wallet)}
            for wallet in wallets
        ]
    current = next((item for item in payload_wallets if item["chain"] == identity.chain and item["mode"] == "external-bot"), None)
    return {
        "currentChain": identity.chain,
        "botControl": {
            "walletMode": user_control.wallet_mode,
            "enabled": user_control.enabled,
            "paused": user_control.paused,
            "minNetBps": user_control.min_net_bps,
            "maxNotionalUsd": str(user_control.max_notional_usd),
        },
        "currentWallet": current,
        "selectedWalletId": str(selected.id) if selected else None,
        "wallets": payload_wallets,
        "pnl": {"realizedUsd": str(selected_realized), "expectedUsd": str(selected_expected), "settledTrades": selected_executions.filter(state="settled").count()},
        "overallPnl": {"realizedUsd": str(realized), "expectedUsd": str(expected), "settledTrades": executions.filter(state="settled").count()},
        "tradedTokens": [{"slug": item["slug"], "expectedProfitUsd": str(item["trades"] or 0)} for item in selected_tokens],
        "history": selected_history,
    }


@transaction.atomic
def create_bot_transfer(*, identity, wallet_id, direction, asset, amount_base_units, destination, idempotency_key):
    wallet = BotWallet.objects.select_for_update().filter(id=wallet_id, owner=identity).first()
    if not wallet:
        raise ValueError("bot wallet not found")
    if not chain_accessible(identity, wallet.chain):
        raise ValueError("authenticated wallet cannot control this bot wallet chain")
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
            "chain": bot_transfer_chain(wallet),
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


@transaction.atomic
def record_bot_transfer_event(*, transfer_id, event, idempotency_key, tx_hash="", metadata=None):
    """Apply one private signer callback and settle/release bot ledger funds."""
    if event not in BOT_TRANSFER_EVENTS:
        raise ValueError("unsupported bot transfer event")
    transfer = BotWalletTransfer.objects.select_for_update().select_related("owner", "bot_wallet").get(id=transfer_id)
    existing = BotWalletTransferEvent.objects.filter(idempotency_key=idempotency_key).first()
    if existing:
        if existing.transfer_id != transfer.id:
            raise ValueError("idempotency key belongs to another bot transfer")
        return transfer
    next_state = next_bot_transfer_state(transfer.state, event)
    try:
        with transaction.atomic():
            BotWalletTransferEvent.objects.create(
                idempotency_key=idempotency_key,
                transfer=transfer,
                event=event,
                tx_hash=tx_hash or "",
                metadata=metadata if isinstance(metadata, dict) else {},
            )
    except IntegrityError:
        existing = BotWalletTransferEvent.objects.filter(idempotency_key=idempotency_key).first()
        if existing and existing.transfer_id == transfer.id:
            return transfer
        raise ValueError("idempotency key belongs to another bot transfer")
    if event == "confirmed" and transfer.direction == "withdrawal":
        reserved = account_for(transfer.owner, transfer.asset, transfer.chain, "bot-withdrawal-reserved", bot_wallet=transfer.bot_wallet)
        treasury = account_for(None, transfer.asset, transfer.chain, "onchain-outflow")
        LedgerEntry.objects.get_or_create(
            reference_type="bot-withdrawal",
            reference_id=str(transfer.id),
            debit_account=reserved,
            credit_account=treasury,
            defaults={"asset": transfer.asset, "amount_base_units": transfer.amount_base_units},
        )
    elif event == "failed" and transfer.direction == "withdrawal":
        available = account_for(transfer.owner, transfer.asset, transfer.chain, "bot-available", bot_wallet=transfer.bot_wallet)
        reserved = account_for(transfer.owner, transfer.asset, transfer.chain, "bot-withdrawal-reserved", bot_wallet=transfer.bot_wallet)
        LedgerEntry.objects.get_or_create(
            reference_type="bot-withdrawal-release",
            reference_id=str(transfer.id),
            debit_account=reserved,
            credit_account=available,
            defaults={"asset": transfer.asset, "amount_base_units": transfer.amount_base_units},
        )
    transfer.state = next_state
    if tx_hash:
        transfer.tx_hash = tx_hash
    if event == "failed":
        transfer.failure_code = str((metadata or {}).get("code") or "signer-failed")[:64]
    transfer.save(update_fields=["state", "tx_hash", "failure_code", "updated_at"])
    record_event("bot_wallet.transfer.event", actor_type="signer", subject_type="bot_transfer", subject_id=transfer.id, metadata={"event": event, "idempotencyKey": idempotency_key, "txHash": tx_hash or None})
    return transfer
