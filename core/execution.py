"""Durable execution lifecycle for quote-bound Pangea arbitrage plans.

This module records state transitions only. It never signs or broadcasts a
transaction. A separately audited signer may submit idempotent events after it
has independently validated and executed the exact transaction intent.
"""

import hashlib
import hmac
import json
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.utils import timezone

from .audit import record_event
from .models import ArbitrageOpportunity, BotWallet, Execution, ExecutionEvent, ExecutionLeg, WalletIdentity


EXECUTION_EVENTS = {
    "signer-requested",
    "buy-submitted",
    "buy-confirmed",
    "bridge-source-submitted",
    "bridge-source-confirmed",
    "bridge-in-flight",
    "bridge-destination-confirmed",
    "sell-submitted",
    "sell-confirmed",
    "settled",
    "failed",
    "refunded",
}

_TRANSITIONS = {
    "planned": {"signer-requested": "awaiting-signer", "failed": "failed"},
    "awaiting-signer": {"buy-submitted": "buy-submitted", "failed": "failed"},
    "buy-submitted": {"buy-confirmed": "buy-confirmed", "failed": "failed"},
    "buy-confirmed": {"bridge-source-submitted": "bridge-source-submitted", "failed": "failed"},
    "bridge-source-submitted": {"bridge-source-confirmed": "bridge-source-confirmed", "failed": "failed"},
    "bridge-source-confirmed": {"bridge-in-flight": "bridge-in-flight", "failed": "failed", "refunded": "refunded"},
    "bridge-in-flight": {"bridge-destination-confirmed": "bridge-destination-confirmed", "failed": "failed", "refunded": "refunded"},
    "bridge-destination-confirmed": {"sell-submitted": "sell-submitted", "failed": "failed"},
    "sell-submitted": {"sell-confirmed": "sell-confirmed", "failed": "failed"},
    "sell-confirmed": {"settled": "settled", "failed": "failed"},
}


def next_execution_state(current, event):
    """Return the next state or reject an out-of-order signer callback."""
    if event not in EXECUTION_EVENTS:
        raise ValueError("unsupported execution event")
    if event in {"failed", "refunded"}:
        if current in {"settled", "failed", "refunded"}:
            raise ValueError(f"invalid execution transition {current}->{event}")
        if event == "refunded" and current not in {"bridge-source-confirmed", "bridge-in-flight"}:
            raise ValueError(f"invalid execution transition {current}->{event}")
        return event
    try:
        return _TRANSITIONS[current][event]
    except KeyError as exc:
        raise ValueError(f"invalid execution transition {current}->{event}") from exc


def _money(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("invalid realized profit")


def _quote_hash(artifact):
    """Use the same canonical representation as the arbitrage quote builder."""
    return hashlib.sha256(
        json.dumps(artifact, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def execution_payload(execution):
    execution = Execution.objects.select_related("profit_settlement").prefetch_related("legs", "events").get(id=execution.id)
    settlement = getattr(execution, "profit_settlement", None)
    return {
        "id": str(execution.id),
        "slug": execution.slug,
        "state": execution.state,
        "botWalletId": str(execution.bot_wallet_id) if execution.bot_wallet_id else None,
        "sellBotWalletId": str(execution.sell_bot_wallet_id) if execution.sell_bot_wallet_id else None,
        "opportunityId": str(execution.opportunity_id) if execution.opportunity_id else None,
        "expectedNetBps": execution.expected_net_bps,
        "expectedProfitUsd": str(execution.expected_profit_usd) if execution.expected_profit_usd is not None else None,
        "realizedProfitUsd": str(execution.realized_profit_usd) if execution.realized_profit_usd is not None else None,
        "quoteHash": execution.quote_hash,
        "quoteArtifactPresent": bool(execution.quote_artifact),
        "errorCode": execution.error_code,
        "createdAt": execution.created_at.isoformat(),
        "updatedAt": execution.updated_at.isoformat(),
        "realizedAt": execution.realized_at.isoformat() if execution.realized_at else None,
        "profitSettlement": {
            "id": str(settlement.id),
            "state": settlement.state,
            "asset": settlement.settlement_asset,
            "chain": settlement.settlement_chain,
            "amountBaseUnits": str(settlement.amount_base_units),
            "recipient": settlement.recipient,
            "txHash": settlement.tx_hash,
            "settledAt": settlement.settled_at.isoformat() if settlement.settled_at else None,
        } if settlement else None,
        "legs": [
            {
                "id": str(leg.id),
                "kind": leg.kind,
                "chain": leg.chain,
                "state": leg.state,
                "quoteHash": leg.quote_hash or None,
                "txHash": leg.tx_hash or None,
                "bridgeGuid": leg.bridge_guid or None,
                "amountInBaseUnits": str(leg.amount_in_base_units),
                "amountOutBaseUnits": str(leg.amount_out_base_units),
                "confirmedAt": leg.confirmed_at.isoformat() if leg.confirmed_at else None,
            }
            for leg in execution.legs.all()
        ],
        "events": [
            {
                "id": str(event.id),
                "event": event.event,
                "legId": str(event.leg_id) if event.leg_id else None,
                "txHash": event.tx_hash or None,
                "bridgeGuid": event.bridge_guid or None,
                "realizedProfitUsd": str(event.realized_profit_usd) if event.realized_profit_usd is not None else None,
                "metadata": event.metadata,
                "createdAt": event.created_at.isoformat(),
            }
            for event in execution.events.all()
        ],
    }


def signer_wallets_ready(execution):
    """Require both route wallets to remain attested and active at handoff time."""
    wallets = (getattr(execution, "bot_wallet", None), getattr(execution, "sell_bot_wallet", None))
    return all(
        wallet
        and wallet.status == "active"
        and bool(wallet.address)
        and bool(wallet.signer_key_ref)
        for wallet in wallets
    )


def signer_intent_payload(execution):
    """Return the private signer handoff for one hash-bound execution plan."""
    execution = Execution.objects.select_related("bot_wallet", "sell_bot_wallet", "opportunity").prefetch_related("legs").get(id=execution.id)
    if execution.state not in {"planned", "awaiting-signer"}:
        raise ValueError("execution is not awaiting signer handoff")
    if not execution.opportunity or not execution.opportunity.expires_at or execution.opportunity.expires_at <= timezone.now():
        raise ValueError("execution opportunity quote has expired")
    if not signer_wallets_ready(execution):
        raise ValueError("execution bot wallets are not currently attested and active")
    if not execution.quote_hash or not isinstance(execution.quote_artifact, dict) or not execution.quote_artifact.get("trade") or not execution.quote_artifact.get("bridge"):
        raise ValueError("execution quote artifact is incomplete")
    artifact_hash = _quote_hash({"trade": execution.quote_artifact["trade"], "bridge": execution.quote_artifact["bridge"]})
    if not hmac.compare_digest(str(execution.quote_hash), artifact_hash):
        raise ValueError("execution quote artifact hash mismatch")
    return {
        "executionId": str(execution.id),
        "slug": execution.slug,
        "quoteHash": execution.quote_hash,
        "quoteArtifact": execution.quote_artifact,
        "expectedProfitUsd": str(execution.expected_profit_usd) if execution.expected_profit_usd is not None else None,
        "botWallet": {
            "id": str(execution.bot_wallet_id) if execution.bot_wallet_id else None,
            "chain": execution.bot_wallet.chain if execution.bot_wallet else None,
            "address": execution.bot_wallet.address if execution.bot_wallet else None,
            "signerKeyRef": execution.bot_wallet.signer_key_ref if execution.bot_wallet else None,
        },
        "sellBotWallet": {
            "id": str(execution.sell_bot_wallet_id) if execution.sell_bot_wallet_id else None,
            "chain": execution.sell_bot_wallet.chain if execution.sell_bot_wallet else None,
            "address": execution.sell_bot_wallet.address if execution.sell_bot_wallet else None,
            "signerKeyRef": execution.sell_bot_wallet.signer_key_ref if execution.sell_bot_wallet else None,
        },
        "legs": [{
            "kind": leg.kind,
            "chain": leg.chain,
            "quoteHash": leg.quote_hash,
            "amountInBaseUnits": str(leg.amount_in_base_units),
            "amountOutBaseUnits": str(leg.amount_out_base_units),
        } for leg in execution.legs.all()],
        "expiresAt": execution.opportunity.expires_at.isoformat() if execution.opportunity and execution.opportunity.expires_at else None,
    }


@transaction.atomic
def create_execution_from_opportunity(*, identity: WalletIdentity, opportunity_id, bot_wallet: BotWallet, sell_bot_wallet: BotWallet | None = None, decision=None, quote_hash=None):
    """Create one execution plan from one fresh, quote-bound opportunity."""
    opportunity = ArbitrageOpportunity.objects.select_for_update().get(id=opportunity_id)
    decision = decision if isinstance(decision, dict) else opportunity.decision
    quote_hash = quote_hash or opportunity.quote_hash
    if opportunity.state not in {"approved-paper", "approved"}:
        raise ValueError("opportunity is not approved for execution")
    if opportunity.expires_at and opportunity.expires_at <= timezone.now():
        raise ValueError("opportunity quote has expired")
    if not quote_hash or not decision.get("executable"):
        raise ValueError("opportunity does not contain an executable quote decision")
    artifact = decision.get("quoteArtifact")
    if not isinstance(artifact, dict) or not isinstance(artifact.get("trade"), dict) or not isinstance(artifact.get("bridge"), dict):
        raise ValueError("execution requires a complete quote artifact")
    if not hmac.compare_digest(str(quote_hash), _quote_hash({"trade": artifact["trade"], "bridge": artifact["bridge"]})):
        raise ValueError("quote artifact does not match quote hash")
    if bot_wallet.owner_id != identity.id or bot_wallet.chain != opportunity.buy_chain or bot_wallet.status != "active" or not bot_wallet.address:
        raise ValueError("source bot wallet is not active for this opportunity")
    if (
        not sell_bot_wallet
        or sell_bot_wallet.owner_id != identity.id
        or sell_bot_wallet.chain != opportunity.sell_chain
        or sell_bot_wallet.status != "active"
        or not sell_bot_wallet.address
    ):
        raise ValueError("destination bot wallet is not active for this opportunity")

    execution, created = Execution.objects.get_or_create(
        opportunity=opportunity,
        user_identity=identity,
        defaults={
            "bot_wallet": bot_wallet,
            "sell_bot_wallet": sell_bot_wallet,
            "slug": opportunity.slug,
            "state": "planned",
            "expected_net_bps": int(decision.get("netBps", opportunity.net_bps)),
            "expected_profit_usd": _money(decision.get("netProfitUsd", opportunity.net_profit_usd)),
            "quote_hash": quote_hash,
            "quote_artifact": artifact,
        },
    )
    if not created:
        return execution

    bridge = decision.get("bridgeRoute") or {}
    for kind, chain, amount_in, amount_out, quote_hash in (
        ("buy", opportunity.buy_chain, decision.get("buyAmountBaseUnits", "0"), decision.get("tokenOutBaseUnits", "0"), quote_hash),
        ("bridge", f"{opportunity.buy_chain}->{opportunity.sell_chain}", decision.get("tokenOutBaseUnits", "0"), bridge.get("amountOut", "0"), bridge.get("callHash") or quote_hash),
        ("sell", opportunity.sell_chain, decision.get("sellAmountBaseUnits") or bridge.get("amountOut", "0"), "0", quote_hash),
    ):
        ExecutionLeg.objects.create(
            execution=execution,
            kind=kind,
            chain=chain,
            quote_hash=quote_hash or "",
            amount_in_base_units=str(amount_in or 0),
            amount_out_base_units=str(amount_out or 0),
        )
    ExecutionEvent.objects.create(
        idempotency_key=f"execution:{execution.id}:planned",
        execution=execution,
        event="planned",
        metadata={"opportunityId": str(opportunity.id), "quoteHash": quote_hash, "freshDecision": True},
    )
    record_event("execution.planned", actor_type="wallet", actor_id=identity.id, subject_type="execution", subject_id=execution.id, metadata={"opportunityId": str(opportunity.id), "quoteHash": quote_hash})
    return execution


def _leg_for_event(execution, event):
    if event.startswith("buy-"):
        return execution.legs.get(kind="buy")
    if event.startswith("bridge-"):
        return execution.legs.get(kind="bridge")
    if event.startswith("sell-"):
        return execution.legs.get(kind="sell")
    return None


def _leg_state(event):
    return {
        "buy-submitted": "submitted",
        "buy-confirmed": "confirmed",
        "bridge-source-submitted": "source-submitted",
        "bridge-source-confirmed": "source-confirmed",
        "bridge-in-flight": "in-flight",
        "bridge-destination-confirmed": "destination-confirmed",
        "sell-submitted": "submitted",
        "sell-confirmed": "confirmed",
    }.get(event)


@transaction.atomic
def record_execution_event(*, execution_id, event, idempotency_key, tx_hash="", bridge_guid="", realized_profit_usd=None, metadata=None):
    """Apply one signer/reconciliation event exactly once."""
    if event not in EXECUTION_EVENTS or event == "signer-requested" and realized_profit_usd is not None:
        raise ValueError("invalid execution event")
    execution = Execution.objects.select_for_update().get(id=execution_id)
    existing = ExecutionEvent.objects.filter(idempotency_key=idempotency_key).first()
    if existing:
        if existing.execution_id != execution.id:
            raise ValueError("idempotency key belongs to another execution")
        return execution
    next_state = next_execution_state(execution.state, event)
    profit = _money(realized_profit_usd) if realized_profit_usd is not None else None
    if event == "settled" and profit is None:
        raise ValueError("settled event requires realized profit")
    leg = _leg_for_event(execution, event)
    if event not in {"failed", "refunded", "signer-requested", "settled"} and not leg:
        raise ValueError("execution leg is missing")
    try:
        event_row = ExecutionEvent.objects.create(
            idempotency_key=idempotency_key,
            execution=execution,
            leg=leg,
            event=event,
            tx_hash=tx_hash or "",
            bridge_guid=bridge_guid or "",
            realized_profit_usd=profit,
            metadata=metadata if isinstance(metadata, dict) else {},
        )
    except IntegrityError:
        return Execution.objects.get(id=execution_id)
    execution.state = next_state
    if event in {"failed", "refunded"}:
        execution.error_code = str((metadata or {}).get("code") or event)
    if event == "settled":
        execution.realized_profit_usd = profit
        execution.realized_at = timezone.now()
    execution.save(update_fields=["state", "error_code", "realized_profit_usd", "realized_at", "updated_at"])
    if leg:
        leg_state = _leg_state(event)
        if leg_state:
            leg.state = leg_state
            if tx_hash:
                leg.tx_hash = tx_hash
            if bridge_guid:
                leg.bridge_guid = bridge_guid
            if leg_state in {"confirmed", "destination-confirmed"}:
                leg.confirmed_at = timezone.now()
            leg.save(update_fields=["state", "tx_hash", "bridge_guid", "confirmed_at", "updated_at"])
    record_event("execution.event", actor_type="signer", subject_type="execution", subject_id=execution.id, metadata={"event": event, "idempotencyKey": idempotency_key, "txHash": tx_hash or None, "bridgeGuid": bridge_guid or None})
    return execution
