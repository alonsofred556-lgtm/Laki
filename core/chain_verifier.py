import json
import time
from datetime import datetime, timezone as datetime_timezone

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .funding import credit_confirmed_deposit
from .assets import native_asset, token_config
from .models import DepositObservation


class ProviderError(Exception):
    pass


class VerificationError(Exception):
    pass


TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a8df523b3ef"


def _rpc(url, method, params):
    try:
        response = requests.post(
            url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise ProviderError("chain provider request failed") from exc
    if payload.get("error"):
        raise ProviderError("chain provider returned an error")
    return payload.get("result")


def _rpc_urls():
    try:
        value = json.loads(settings.CONI_RPC_URLS or "{}")
    except json.JSONDecodeError as exc:
        raise ProviderError("CONI_RPC_URLS is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ProviderError("CONI_RPC_URLS must be a JSON object")
    return {str(chain): str(url) for chain, url in value.items() if str(url).startswith(("https://", "http://"))}


def _expected_chain_ids():
    try:
        value = json.loads(settings.CONI_EXPECTED_CHAIN_IDS or "{}")
    except json.JSONDecodeError as exc:
        raise ProviderError("CONI_EXPECTED_CHAIN_IDS is not valid JSON") from exc
    return value if isinstance(value, dict) else {}


def _hex_int(value):
    if not isinstance(value, str) or not value.startswith("0x"):
        raise VerificationError("provider returned an invalid quantity")
    return int(value, 16)


def _reject(observation, code):
    observation.status = "rejected"
    observation.rejection_code = code
    observation.save(update_fields=["status", "rejection_code"])
    if observation.funding_intent and observation.funding_intent.status == "pending-verification":
        observation.funding_intent.status = "rejected"
        observation.funding_intent.save(update_fields=["status"])


def _pending(observation, confirmations, raw_event=None):
    observation.confirmations = max(0, confirmations)
    if raw_event is not None:
        observation.raw_event = raw_event
    observation.save(update_fields=["confirmations", "raw_event"] if raw_event is not None else ["confirmations"])


def _confirm_and_credit(observation, confirmations, block_number=None, block_hash=None, raw_event=None):
    with transaction.atomic():
        current = DepositObservation.objects.select_for_update().select_related("funding_intent", "wallet_identity").get(id=observation.id)
        if current.status == "credited":
            return current
        current.confirmations = confirmations
        current.block_number = block_number
        current.block_hash = block_hash
        current.raw_event = raw_event or {}
        current.status = "confirmed"
        current.confirmed_at = timezone.now()
        current.rejection_code = None
        current.save(update_fields=["confirmations", "block_number", "block_hash", "raw_event", "status", "confirmed_at", "rejection_code"])
        return credit_confirmed_deposit(current.id)


def _verify_evm(observation):
    url = _rpc_urls().get(observation.chain)
    if not url:
        raise ProviderError(f"no RPC configured for {observation.chain}")
    expected_chain_id = _expected_chain_ids().get(observation.chain)
    actual_chain_id = _rpc(url, "eth_chainId", [])
    if not expected_chain_id or str(actual_chain_id).lower() != str(expected_chain_id).lower():
        raise ProviderError(f"RPC chain identity is not configured for {observation.chain}")
    if getattr(observation, "asset", native_asset(observation.chain)) != native_asset(observation.chain):
        return _verify_evm_token(observation, url)
    transaction_data = _rpc(url, "eth_getTransactionByHash", [observation.tx_hash])
    if not transaction_data:
        return "pending"
    sender = str(transaction_data.get("from", ""))
    destination = str(transaction_data.get("to", ""))
    value = _hex_int(transaction_data.get("value"))
    expected = int(observation.amount_base_units)
    if sender.lower() != observation.sender.lower() or destination.lower() != observation.destination.lower():
        raise VerificationError("sender or destination mismatch")
    if value != expected:
        raise VerificationError("amount mismatch")
    receipt = _rpc(url, "eth_getTransactionReceipt", [observation.tx_hash])
    if not receipt:
        return "pending"
    if receipt.get("status") != "0x1":
        raise VerificationError("transaction reverted")
    block_number = _hex_int(receipt.get("blockNumber"))
    latest = _hex_int(_rpc(url, "eth_blockNumber", []))
    confirmations = max(0, latest - block_number + 1)
    raw = {"from": sender, "to": destination, "value": transaction_data.get("value"), "receiptStatus": receipt.get("status")}
    if confirmations < settings.CONI_DEPOSIT_CONFIRMATIONS:
        _pending(observation, confirmations, raw)
        return "pending"
    _confirm_and_credit(observation, confirmations, block_number, receipt.get("blockHash"), raw)
    return "credited"


def _verify_evm_token(observation, url):
    config = token_config(observation.asset, observation.chain) or {}
    token_address = str(config.get("address", ""))
    if not token_address:
        raise VerificationError("token is not configured for this chain")
    receipt = _rpc(url, "eth_getTransactionReceipt", [observation.tx_hash])
    if not receipt:
        return "pending"
    if receipt.get("status") != "0x1":
        raise VerificationError("transaction reverted")
    matching = None
    for log in receipt.get("logs") or []:
        topics = log.get("topics") or []
        if len(topics) < 3 or str(log.get("address", "")).lower() != token_address.lower() or topics[0].lower() != TRANSFER_TOPIC:
            continue
        sender = "0x" + topics[1][-40:]
        destination = "0x" + topics[2][-40:]
        index = _hex_int(log.get("logIndex", "0x0"))
        amount = int(log.get("data", "0x0"), 16)
        if index == observation.event_index and sender.lower() == observation.sender.lower() and destination.lower() == observation.destination.lower() and amount == int(observation.amount_base_units):
            matching = log
            break
    if matching is None:
        raise VerificationError("token transfer does not match deposit observation")
    block_number = _hex_int(receipt.get("blockNumber"))
    latest = _hex_int(_rpc(url, "eth_blockNumber", []))
    confirmations = max(0, latest - block_number + 1)
    raw = {"token": token_address, "from": observation.sender, "to": observation.destination, "value": str(observation.amount_base_units), "logIndex": observation.event_index, "receiptStatus": receipt.get("status")}
    if confirmations < settings.CONI_DEPOSIT_CONFIRMATIONS:
        _pending(observation, confirmations, raw)
        return "pending"
    _confirm_and_credit(observation, confirmations, block_number, receipt.get("blockHash"), raw)
    return "credited"


def _solana_instructions(transaction_data):
    message = ((transaction_data.get("transaction") or {}).get("message") or {})
    instructions = list(message.get("instructions") or [])
    for group in transaction_data.get("meta", {}).get("innerInstructions") or []:
        instructions.extend(group.get("instructions") or [])
    return instructions


def _solana_native_transfer_match(transaction_data, event_index, sender, destination, amount):
    """Match the exact instruction index recorded by native SOL discovery."""
    instructions = _solana_instructions(transaction_data)
    if not isinstance(event_index, int) or event_index < 0 or event_index >= len(instructions):
        return None
    instruction = instructions[event_index]
    parsed = instruction.get("parsed") or {}
    info = parsed.get("info") or {}
    if str(parsed.get("program") or instruction.get("program") or "").lower() != "system" or parsed.get("type") != "transfer":
        return None
    if info.get("source") != sender or info.get("destination") != destination:
        return None
    if int(info.get("lamports", 0)) != int(amount):
        return None
    return info


def _verify_solana(observation):
    if not settings.CONI_SOLANA_RPC_URL:
        raise ProviderError("no Solana RPC configured")
    transaction_data = _rpc(
        settings.CONI_SOLANA_RPC_URL,
        "getTransaction",
        [observation.tx_hash, {"encoding": "jsonParsed", "commitment": "finalized", "maxSupportedTransactionVersion": 0}],
    )
    if not transaction_data:
        return "pending"
    if transaction_data.get("meta", {}).get("err") is not None:
        raise VerificationError("Solana transaction failed")
    matches = []
    raw_event = observation.raw_event or {}
    expected_spl = raw_event.get("type") == "solana-spl-transfer"
    instructions = _solana_instructions(transaction_data)
    message = ((transaction_data.get("transaction") or {}).get("message") or {})
    account_keys = []
    for key in message.get("accountKeys") or []:
        account_keys.append(key.get("pubkey") if isinstance(key, dict) else key)
    post_balances = {}
    for item in transaction_data.get("meta", {}).get("postTokenBalances") or []:
        index = item.get("accountIndex")
        if isinstance(index, int) and index < len(account_keys):
            post_balances[account_keys[index]] = item
    for index, instruction in enumerate(instructions):
        parsed = instruction.get("parsed") or {}
        info = parsed.get("info") or {}
        if not expected_spl and index == observation.event_index:
            native_match = _solana_native_transfer_match(transaction_data, observation.event_index, observation.sender, observation.destination, observation.amount_base_units)
            if native_match:
                matches.append(native_match)
        if expected_spl and index == observation.event_index and parsed.get("type") in {"transfer", "transferChecked"} and str(parsed.get("program") or instruction.get("program") or "").lower() in {"spl-token", "spl-token-2022", "spltoken"}:
            destination = info.get("destination")
            balance = post_balances.get(destination) or {}
            amount_info = info.get("tokenAmount") or {}
            amount = amount_info.get("amount") or info.get("amount")
            if destination == raw_event.get("tokenAccount") and balance.get("owner") == observation.destination and balance.get("mint") == raw_event.get("mint") and (not raw_event.get("tokenProgram") or balance.get("programId") == raw_event.get("tokenProgram")) and (info.get("authority") or info.get("owner")) == observation.sender and int(amount or 0) == int(observation.amount_base_units):
                matches.append(info)
    if not matches:
        raise VerificationError("Solana transfer does not match funding intent")
    block_time = transaction_data.get("blockTime")
    raw = {**raw_event, "slot": transaction_data.get("slot"), "amount": str((matches[0].get("lamports") if not expected_spl else (matches[0].get("tokenAmount") or {}).get("amount") or matches[0].get("amount")))}
    confirmed_at = datetime.fromtimestamp(block_time, tz=datetime_timezone.utc) if isinstance(block_time, int) else timezone.now()
    with transaction.atomic():
        current = DepositObservation.objects.select_for_update().get(id=observation.id)
        current.confirmations = 1
        current.block_number = transaction_data.get("slot")
        current.raw_event = raw
        current.status = "confirmed"
        current.confirmed_at = confirmed_at
        current.rejection_code = None
        current.save(update_fields=["confirmations", "block_number", "raw_event", "status", "confirmed_at", "rejection_code"])
        credit_confirmed_deposit(current.id)
    return "credited"


def verify_observation(observation):
    if observation.status not in {"pending-verification", "verifying"}:
        return observation.status
    observation.status = "verifying"
    observation.save(update_fields=["status"])
    try:
        return _verify_solana(observation) if observation.chain == "solana" else _verify_evm(observation)
    except VerificationError as exc:
        _reject(observation, str(exc)[:64])
        return "rejected"
    except ProviderError:
        observation.status = "pending-verification"
        observation.rejection_code = "provider-unavailable"
        observation.save(update_fields=["status", "rejection_code"])
        return "provider-unavailable"


def verify_pending_deposits(limit=None):
    limit = limit or settings.CONI_DEPOSIT_SCAN_BATCH
    observations = DepositObservation.objects.filter(status__in=["pending-verification", "verifying"]).order_by("observed_at")[:limit]
    counts = {"credited": 0, "pending": 0, "rejected": 0, "provider-unavailable": 0}
    for observation in observations:
        result = verify_observation(observation)
        counts[result] = counts.get(result, 0) + 1
    return counts
