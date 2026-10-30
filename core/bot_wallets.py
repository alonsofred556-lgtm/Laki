"""Per-user bot-wallet lifecycle and read-only portfolio summaries."""

import json
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from .audit import record_event
from .assets import asset_decimals, native_asset, supported_asset
from .bot_controls import ensure_user_bot_control, global_bot_control
from .funding import account_for, ledger_balance
from .fees import accrue_fee, fee_amount_base_units as calculate_fee_amount, fee_recipient_for
from .models import BotWallet, BotWalletProvisionRequest, BotWalletTransfer, BotWalletTransferEvent, Execution, FeeEvent, LedgerAccount, LedgerEntry, ProfitAllocation, WalletIdentity
from .planner_funding import native_reserve_base_units


BOT_WALLET_CHAINS = ("ethereum", "base", "robinhood", "bsc", "solana")
EVM_BOT_CHAINS = {"ethereum", "base", "robinhood", "bsc"}
BOT_TRANSFER_EVENTS = {"submitted", "confirmed", "failed"}
BOT_TRANSFER_PROOF_EVENTS = {"submitted", "confirmed"}
BOT_TRANSFER_DIRECTIONS = {"withdrawal"}
PROVISION_LEASE_SECONDS = 120
_BOT_TRANSFER_TRANSITIONS = {
    "pending-approval": {"submitted": "submitted", "failed": "failed"},
    "leased": {"submitted": "submitted", "failed": "failed"},
    "submitted": {"confirmed": "confirmed", "failed": "failed"},
}


def addresses_match(chain, supplied, expected):
    """Compare EVM addresses case-insensitively and Solana addresses exactly."""
    if not isinstance(supplied, str) or not isinstance(expected, str):
        return False
    return supplied == expected if chain == "solana" else supplied.lower() == expected.lower()


def chain_accessible(identity, chain):
    """An authenticated wallet may control its chain and the EVM sibling chains.

    EVM addresses are chain-independent, but a Solana identity must never be
    treated as an EVM signer (or vice versa). The actual transaction still
    requires the provider/signer to use the selected target chain.
    """
    chain = str(chain or "")
    identity_chain = getattr(identity, "chain", "")
    return chain == identity_chain or (chain in EVM_BOT_CHAINS and identity_chain in EVM_BOT_CHAINS)


def accessible_bot_chains(identity):
    """Return bot-wallet chains this authenticated identity may operate."""
    return ("solana",) if getattr(identity, "chain", "") == "solana" else tuple(sorted(EVM_BOT_CHAINS))


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


def claim_bot_transfer_requests(*, limit=20):
    """Lease pending bot withdrawals for the private signer adapter."""
    now = timezone.now()
    lease_until = now + timedelta(seconds=PROVISION_LEASE_SECONDS)
    claimed = []
    with transaction.atomic():
        candidates = BotWalletTransfer.objects.select_for_update().select_related(
            "owner", "bot_wallet"
        ).filter(
            direction="withdrawal",
            bot_wallet__status="active",
            bot_wallet__address__gt="",
        ).filter(
            Q(state="pending-approval") | Q(state="leased", lease_until__lt=now)
        ).filter(bot_wallet__signer_key_ref__gt="").order_by("created_at")[:limit]
        for transfer in candidates:
            transfer.state = "leased"
            transfer.attempt_count += 1
            transfer.lease_until = lease_until
            transfer.save(update_fields=["state", "attempt_count", "lease_until", "updated_at"])
            claimed.append(transfer)
    return claimed


def next_bot_transfer_state(current, event):
    if event not in BOT_TRANSFER_EVENTS:
        raise ValueError("unsupported bot transfer event")
    try:
        return _BOT_TRANSFER_TRANSITIONS[current][event]
    except KeyError as exc:
        raise ValueError(f"invalid bot transfer transition {current}->{event}") from exc


def bot_transfer_event_requires_tx_hash(event):
    """Return whether a signer callback must include an on-chain tx proof."""
    return event in BOT_TRANSFER_PROOF_EVENTS


def bot_transfer_direction_allowed(direction):
    """Bot-transfer records are signer withdrawals only.

    Bot top-ups use FundingIntent plus the chain verifier. Keeping them out of
    this signer-transfer table prevents an alternate callback path from
    creating an accounting-only bot balance.
    """
    return direction in BOT_TRANSFER_DIRECTIONS


def validate_bot_transfer_proof(transfer, event, metadata):
    """Bind signer callback facts to the exact transfer intent.

    The signer adapter must verify the transaction against the chain before
    calling this API. A callback is not accepted merely because it contains a
    syntactically valid transaction hash.
    """
    if event not in BOT_TRANSFER_PROOF_EVENTS:
        return
    if not isinstance(metadata, dict):
        raise ValueError("bot transfer requires structured chain proof")
    if metadata.get("chain") != transfer.chain:
        raise ValueError("bot transfer proof chain does not match the intent")
    if str(metadata.get("asset", "")).upper() != transfer.asset:
        raise ValueError("bot transfer proof asset does not match the intent")
    if str(metadata.get("amountBaseUnits", "")) != str(transfer.amount_base_units):
        raise ValueError("bot transfer proof amount does not match the intent")
    if not addresses_match(transfer.chain, metadata.get("destination"), transfer.destination):
        raise ValueError("bot transfer proof recipient does not match the intent")
    if metadata.get("verified") is not True:
        raise ValueError("bot transfer proof must be chain-verified by the signer")
    if event == "confirmed" and metadata.get("finality") not in {"confirmed", "finalized"}:
        raise ValueError("confirmed bot transfer requires chain finality")


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


def withdrawal_limits(asset, chain):
    """Read explicit base-unit withdrawal caps for one bot-wallet route."""
    try:
        configured = json.loads(getattr(settings, "CONI_BOT_WITHDRAWAL_LIMITS", "{}") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(configured, dict):
        return None
    row = configured.get(f"{chain}:{str(asset).upper()}")
    if not isinstance(row, dict):
        return None
    try:
        per_transfer = int(row.get("perTransferBaseUnits", "0"))
        daily = int(row.get("dailyBaseUnits", "0"))
    except (TypeError, ValueError):
        return None
    if per_transfer <= 0 or daily <= 0 or per_transfer > daily:
        return None
    return {"perTransferBaseUnits": per_transfer, "dailyBaseUnits": daily}


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
        # The model's historical default preserves migrations, but a wallet
        # is not actually attested until the signer binds both a public
        # address and an opaque key reference.
        "custody": "audited-signer" if wallet.status == "active" and wallet.signer_key_ref else "pending-attestation",
        "status": wallet.status,
        "sourceAddress": wallet.source_address,
        "createdAt": wallet.created_at.isoformat(),
        "updatedAt": wallet.updated_at.isoformat(),
    }


def bot_wallet_automation_status(wallet, *, user_control, global_control, native_balance):
    """Describe the exact gate that controls automatic planner eligibility.

    This is status only: it never authorizes a transaction.  The planner still
    obtains a fresh route quote and the private signer still enforces the
    final policy.  Exposing the intermediate state keeps a funded user from
    seeing an opaque ``active`` label while a reserve or signer gate is still
    missing.
    """
    try:
        balance = Decimal(str(native_balance or 0))
    except (InvalidOperation, TypeError, ValueError):
        balance = Decimal("0")
    reserve = native_reserve_base_units(wallet.chain)
    if wallet.status != "active" or not wallet.address or not wallet.signer_key_ref:
        state = "awaiting-signer"
    elif not getattr(global_control, "enabled", False) or getattr(global_control, "paused", True):
        state = "global-paused"
    elif not getattr(user_control, "enabled", False) or getattr(user_control, "paused", True):
        state = "user-paused"
    elif reserve is None:
        state = "awaiting-reserve-policy"
    elif balance < Decimal(reserve):
        state = "awaiting-funding"
    else:
        state = "ready-to-scan"
    return {
        "state": state,
        "autoStartAfterFunding": state == "awaiting-funding",
        "plannerEligible": state == "ready-to-scan",
        "nativeAsset": native_asset(wallet.chain),
        "nativeBalanceBaseUnits": str(balance),
        "requiredNativeReserveBaseUnits": str(reserve) if reserve is not None else None,
    }


def select_bot_wallet(wallets, *, chain, mode="external-bot", selected_wallet_id=None):
    """Choose the requested per-user bot wallet without crossing custody modes."""
    if selected_wallet_id:
        selected = next((wallet for wallet in wallets if str(wallet.id) == str(selected_wallet_id)), None)
        if selected is not None and selected.chain == chain and selected.mode == mode:
            return selected
    preferred = next((wallet for wallet in wallets if wallet.chain == chain and wallet.mode == mode), None)
    return preferred


def _bot_balance(wallet, asset=None):
    filters = {"owner": wallet.owner, "bot_wallet": wallet, "chain": wallet.chain, "kind": "bot-available"}
    if asset:
        filters["asset"] = asset
    rows = []
    from .models import LedgerAccount
    for account in LedgerAccount.objects.filter(**filters):
        decimals = asset_decimals(account.asset, account.chain)
        rows.append({"asset": account.asset, "chain": account.chain, "amountBaseUnits": str(ledger_balance(account)), "decimals": decimals})
    return rows


def bot_wallet_summary(identity, selected_wallet_id=None):
    # Keep the read endpoint self-healing for sessions created before the
    # five-chain provisioning behavior was introduced.
    user_control = ensure_user_bot_control(identity)
    global_control = global_bot_control()
    ensure_bot_wallets(identity, mode=user_control.wallet_mode)
    wallets = BotWallet.objects.filter(owner=identity, chain__in=accessible_bot_chains(identity), mode=user_control.wallet_mode).order_by("chain")
    executions = Execution.objects.select_related("opportunity").filter(user_identity=identity).order_by("-created_at")
    realized = sum((item.realized_profit_usd or Decimal("0") for item in executions if item.state == "settled"), Decimal("0"))
    expected = sum((item.expected_profit_usd or Decimal("0") for item in executions), Decimal("0"))
    selected = select_bot_wallet(wallets, chain=identity.chain, mode=user_control.wallet_mode, selected_wallet_id=selected_wallet_id)
    # A cross-chain execution touches both the buy/source bot wallet and the
    # sell/destination bot wallet. Keep the user total unchanged, but expose
    # the route in either selected wallet's history so a user never sees an
    # apparently idle destination wallet after a completed bridge leg.
    selected_executions = executions.filter(Q(bot_wallet=selected) | Q(sell_bot_wallet=selected)).distinct() if selected else executions.none()
    selected_realized = sum((item.realized_profit_usd or Decimal("0") for item in selected_executions if item.state == "settled"), Decimal("0"))
    selected_expected = sum((item.expected_profit_usd or Decimal("0") for item in selected_executions), Decimal("0"))
    allocations = ProfitAllocation.objects.filter(execution__in=executions)
    selected_allocations = allocations.filter(execution__in=selected_executions)
    token_totals = {}
    for execution in selected_executions:
        totals = token_totals.setdefault(execution.slug, {"trades": 0, "expected": Decimal("0"), "realized": Decimal("0")})
        totals["trades"] += 1
        totals["expected"] += execution.expected_profit_usd or Decimal("0")
        if execution.state == "settled":
            totals["realized"] += execution.realized_profit_usd or Decimal("0")
    transfer_query = BotWalletTransfer.objects.filter(owner=identity)
    transfers = transfer_query.order_by("-created_at")[:50]
    selected_transfers = transfer_query.filter(bot_wallet=selected).order_by("-created_at")[:50] if selected else transfer_query.none()
    selected_fee_events = {
        str(item.execution_id): item
        for item in FeeEvent.objects.filter(kind="trade", execution_id__in=[str(execution.id) for execution in selected_executions])
    }
    selected_history = [
        {"id": str(item.id), "kind": "trade", "slug": item.slug, "state": item.state, "buyChain": item.opportunity.buy_chain if item.opportunity else None, "sellChain": item.opportunity.sell_chain if item.opportunity else None, "expectedNetBps": item.expected_net_bps, "expectedProfitUsd": str(item.expected_profit_usd or 0), "realizedProfitUsd": str(item.realized_profit_usd or 0), "fee": ({"asset": selected_fee_events[str(item.id)].asset, "chain": selected_fee_events[str(item.id)].chain, "amountBaseUnits": str(selected_fee_events[str(item.id)].amount_base_units), "grossAmountBaseUnits": str(selected_fee_events[str(item.id)].gross_amount_base_units), "feeBps": selected_fee_events[str(item.id)].fee_bps, "state": selected_fee_events[str(item.id)].status, "txHash": selected_fee_events[str(item.id)].tx_hash or None} if str(item.id) in selected_fee_events else None), "createdAt": item.created_at.isoformat()}
        for item in selected_executions[:50]
    ] + [
        {"id": str(item.id), "kind": item.direction, "asset": item.asset, "chain": item.chain, "amountBaseUnits": str(item.amount_base_units), "feeAmountBaseUnits": str(item.fee_amount_base_units), "totalReservedBaseUnits": str(item.total_reserved_base_units or item.amount_base_units), "state": item.state, "txHash": item.tx_hash or None, "createdAt": item.created_at.isoformat()}
        for item in selected_transfers
    ]
    # Trades and transfers are queried separately; merge them into one
    # chronological activity stream so a recent withdrawal cannot appear
    # below an older block of trades simply because of query order.
    selected_history.sort(key=lambda item: item["createdAt"], reverse=True)
    payload_wallets = []
    for wallet in wallets:
        balances = _bot_balance(wallet)
        native = native_asset(wallet.chain)
        native_balance = next((item["amountBaseUnits"] for item in balances if item["asset"] == native), "0")
        payload_wallets.append({
            **bot_wallet_payload(wallet),
            "balances": balances,
            "automation": bot_wallet_automation_status(
                wallet,
                user_control=user_control,
                global_control=global_control,
                native_balance=native_balance,
            ),
        })
    current = next((item for item in payload_wallets if selected and item["id"] == str(selected.id)), None)
    return {
        "currentChain": identity.chain,
        "botControl": {
            "walletMode": user_control.wallet_mode,
            "enabled": user_control.enabled,
            "paused": user_control.paused,
            "minNetBps": user_control.min_net_bps,
            "maxNotionalUsd": str(user_control.max_notional_usd),
            "compoundProfitBps": user_control.compound_profit_bps,
        },
        "currentWallet": current,
        "automation": current["automation"] if current else None,
        "selectedWalletId": str(selected.id) if selected else None,
        "wallets": payload_wallets,
        "pnl": {"realizedUsd": str(selected_realized), "expectedUsd": str(selected_expected), "settledTrades": selected_executions.filter(state="settled").count()},
        "compounding": {"allocatedProfitUsd": str(sum((item.compound_profit_usd for item in selected_allocations), Decimal("0"))), "payoutProfitUsd": str(sum((item.payout_profit_usd for item in selected_allocations), Decimal("0"))), "pendingReinvestment": selected_allocations.filter(compound_state="pending-approval").count(), "confirmedReinvestment": selected_allocations.filter(compound_state="confirmed").count()},
        "overallPnl": {"realizedUsd": str(realized), "expectedUsd": str(expected), "settledTrades": executions.filter(state="settled").count()},
        "tradedTokens": [
            {
                "slug": slug,
                "trades": totals["trades"],
                "expectedProfitUsd": str(totals["expected"]),
                "realizedProfitUsd": str(totals["realized"]),
            }
            for slug, totals in token_totals.items()
        ],
        "history": selected_history,
    }


@transaction.atomic
def create_bot_transfer(*, identity, wallet_id, direction, asset, amount_base_units, destination, idempotency_key):
    wallet = BotWallet.objects.select_for_update().filter(id=wallet_id, owner=identity).first()
    if not wallet:
        raise ValueError("bot wallet not found")
    if not chain_accessible(identity, wallet.chain):
        raise ValueError("authenticated wallet cannot control this bot wallet chain")
    user_control = ensure_user_bot_control(identity)
    if wallet.mode != user_control.wallet_mode:
        raise ValueError("selected bot wallet does not match the active custody mode")
    if wallet.status != "active" or not wallet.address or not wallet.signer_key_ref:
        raise ValueError("bot wallet is awaiting audited signer provisioning")
    if not bot_transfer_direction_allowed(direction):
        raise ValueError("bot top-ups must use a verified funding intent")
    try:
        amount_base_units = Decimal(str(amount_base_units))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("invalid bot transfer amount") from exc
    if (
        not amount_base_units.is_finite()
        or amount_base_units <= 0
        or amount_base_units != amount_base_units.to_integral_value()
        or amount_base_units.adjusted() >= 78
        or not destination
    ):
        raise ValueError("invalid bot transfer")
    if not supported_asset(asset, wallet.chain) or asset_decimals(asset, wallet.chain) is None:
        raise ValueError("unsupported bot wallet asset")
    if direction == "withdrawal" and not addresses_match(wallet.chain, destination, identity.address):
        raise ValueError("bot withdrawal destination must be the authenticated wallet")
    fee_amount, fee_bps = calculate_fee_amount(amount_base_units, "bot-withdrawal")
    if fee_amount > 0 and not fee_recipient_for(asset, wallet.chain):
        raise ValueError("bot withdrawal fee recipient is not configured for this asset and chain")
    total_reserved = amount_base_units + fee_amount
    if direction == "withdrawal":
        limits = withdrawal_limits(asset, wallet.chain)
        if getattr(settings, "CONI_BOT_WITHDRAWAL_LIMITS_REQUIRED", not settings.DEBUG) and not limits:
            raise ValueError("bot withdrawal limits are not configured")
        if limits:
            amount_int = int(total_reserved)
            if amount_int > limits["perTransferBaseUnits"]:
                raise ValueError("bot withdrawal exceeds the per-transfer limit")
            day_start = timezone.now() - timedelta(hours=24)
            daily_rows = BotWalletTransfer.objects.filter(
                owner=identity,
                bot_wallet=wallet,
                direction="withdrawal",
                asset=asset,
                chain=wallet.chain,
                created_at__gte=day_start,
                state__in=("pending-approval", "submitted", "confirmed"),
            ).values("amount_base_units", "fee_amount_base_units", "total_reserved_base_units")
            daily_total = sum(
                int(row.get("total_reserved_base_units") or (row["amount_base_units"] + row.get("fee_amount_base_units") or 0))
                for row in daily_rows
            )
            if daily_total + amount_int > limits["dailyBaseUnits"]:
                raise ValueError("bot withdrawal exceeds the rolling daily limit")
    transfer, created = BotWalletTransfer.objects.get_or_create(
        idempotency_key=idempotency_key,
        defaults={
            "bot_wallet": wallet,
            "owner": identity,
            "direction": direction,
            "asset": asset,
            "chain": bot_transfer_chain(wallet),
            "amount_base_units": amount_base_units,
            "fee_amount_base_units": fee_amount,
            "total_reserved_base_units": total_reserved,
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
            or int(transfer.fee_amount_base_units) != int(fee_amount)
            or not addresses_match(transfer.chain, transfer.destination, destination)
        ):
            raise ValueError("idempotency key does not match the original transfer")
        return transfer
    if direction == "withdrawal":
        available = account_for(identity, asset, wallet.chain, "bot-available", bot_wallet=wallet)
        reserved = account_for(identity, asset, wallet.chain, "bot-withdrawal-reserved", bot_wallet=wallet)
        available = LedgerAccount.objects.select_for_update().get(id=available.id)
        reserved = LedgerAccount.objects.select_for_update().get(id=reserved.id)
        if ledger_balance(available) < total_reserved:
            raise ValueError("insufficient bot wallet balance")
        LedgerEntry.objects.get_or_create(
            reference_type="bot-withdrawal-reservation",
            reference_id=str(transfer.id),
            debit_account=available,
            credit_account=reserved,
            defaults={"asset": asset, "amount_base_units": total_reserved},
        )
        accrue_fee(
            kind="bot-withdrawal",
            source_id=str(transfer.id),
            asset=asset,
            chain=wallet.chain,
            gross_amount_base_units=amount_base_units,
            user_identity=identity,
        )
    record_event("bot_wallet.transfer.requested", actor_type="wallet", actor_id=identity.id, subject_type="bot_transfer", subject_id=transfer.id, metadata={"direction": direction, "asset": asset, "chain": wallet.chain, "amountBaseUnits": amount_base_units, "feeAmountBaseUnits": str(fee_amount), "totalReservedBaseUnits": str(total_reserved), "feeBps": fee_bps})
    return transfer


@transaction.atomic
def record_bot_transfer_event(*, transfer_id, event, idempotency_key, tx_hash="", metadata=None):
    """Apply one private signer callback and settle/release bot ledger funds."""
    if event not in BOT_TRANSFER_EVENTS:
        raise ValueError("unsupported bot transfer event")
    if bot_transfer_event_requires_tx_hash(event) and not str(tx_hash or "").strip():
        raise ValueError("bot transfer proof requires tx hash")
    transfer = BotWalletTransfer.objects.select_for_update().select_related("owner", "bot_wallet").get(id=transfer_id)
    validate_bot_transfer_proof(transfer, event, metadata)
    existing = BotWalletTransferEvent.objects.filter(idempotency_key=idempotency_key).first()
    if existing:
        if existing.transfer_id != transfer.id:
            raise ValueError("idempotency key belongs to another bot transfer")
        if existing.event != event or existing.tx_hash != (tx_hash or "") or existing.metadata != (metadata if isinstance(metadata, dict) else {}):
            raise ValueError("idempotency key does not match the original bot transfer event")
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
        fee_event = FeeEvent.objects.filter(kind="bot-withdrawal", source_id=str(transfer.id)).first()
        if fee_event and fee_event.amount_base_units > 0:
            fee_account = account_for(None, transfer.asset, transfer.chain, "fee-payable")
            LedgerEntry.objects.get_or_create(
                reference_type="bot-withdrawal-fee-accrual",
                reference_id=str(fee_event.id),
                debit_account=reserved,
                credit_account=fee_account,
                defaults={"asset": transfer.asset, "amount_base_units": fee_event.amount_base_units},
            )
    elif event == "failed" and transfer.direction == "withdrawal":
        available = account_for(transfer.owner, transfer.asset, transfer.chain, "bot-available", bot_wallet=transfer.bot_wallet)
        reserved = account_for(transfer.owner, transfer.asset, transfer.chain, "bot-withdrawal-reserved", bot_wallet=transfer.bot_wallet)
        LedgerEntry.objects.get_or_create(
            reference_type="bot-withdrawal-release",
            reference_id=str(transfer.id),
            debit_account=reserved,
            credit_account=available,
            defaults={"asset": transfer.asset, "amount_base_units": transfer.total_reserved_base_units or transfer.amount_base_units},
        )
    transfer.state = next_state
    if tx_hash:
        transfer.tx_hash = tx_hash
    if event == "failed":
        transfer.failure_code = str((metadata or {}).get("code") or "signer-failed")[:64]
    transfer.lease_until = None
    transfer.save(update_fields=["state", "tx_hash", "failure_code", "lease_until", "updated_at"])
    record_event("bot_wallet.transfer.event", actor_type="signer", subject_type="bot_transfer", subject_id=transfer.id, metadata={"event": event, "idempotencyKey": idempotency_key, "txHash": tx_hash or None})
    return transfer
