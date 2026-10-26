"""Discover native and configured token deposits without trusting the browser.

Discovery only creates immutable observations. The verifier still checks the
transaction, finality, exact amount, recipient, and network before ledger credit.
"""

import hashlib

from django.conf import settings
from django.utils import timezone

from .assets import native_asset, recipient_for, token_registry
from .audit import record_event
from .chain_verifier import ProviderError, VerificationError, _expected_chain_ids, _hex_int, _rpc, _rpc_urls
from .models import ChainCursor, DepositObservation, FundingIntent


TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a8df523b3ef"


def _targets(chain):
    targets = {}
    native = native_asset(chain)
    recipient = recipient_for(native, chain) if native else ""
    if recipient:
        targets[native] = {recipient.lower()}
    for asset, config in token_registry().items():
        if not isinstance(config, dict):
            continue
        chain_config = config.get(chain)
        address = chain_config.get("address") if isinstance(chain_config, dict) else chain_config
        recipient = recipient_for(asset, chain)
        if address and recipient:
            targets.setdefault(asset, set()).add(recipient.lower())
    # Active per-user bot wallets are valid deposit recipients. They are still
    # credited only when matched to an exact, unexpired funding intent.
    from .models import BotWallet
    for wallet in BotWallet.objects.filter(chain=chain, status="active").exclude(address=""):
        for asset in token_registry().keys():
            targets.setdefault(asset, set()).add(wallet.address.lower())
        native = native_asset(chain)
        if native:
            targets.setdefault(native, set()).add(wallet.address.lower())
    return targets


def _identity_and_intent(chain, asset, sender, destination, amount):
    candidates = FundingIntent.objects.filter(
        chain=chain,
        asset=asset,
        destination__iexact=destination,
        amount_base_units=amount,
        status__in=["awaiting-wallet-approval", "pending-verification"],
    ).select_related("wallet_identity")
    for intent in candidates:
        if intent.wallet_identity and intent.wallet_identity.address.lower() == sender.lower():
            return intent.wallet_identity, intent
    return None, None


def _record_candidate(*, chain, asset, sender, destination, tx_hash, event_index, amount, block_number, block_hash, raw_event):
    identity, intent = _identity_and_intent(chain, asset, sender, destination, amount)
    event_key = f"{chain}:{tx_hash.lower()}:{event_index}"
    defaults = {
        "funding_intent": intent,
        "wallet_identity": identity,
        "asset": asset,
        "chain": chain,
        "sender": sender,
        "destination": destination,
        "tx_hash": tx_hash,
        "event_index": event_index,
        "block_number": block_number,
        "block_hash": block_hash,
        "amount_base_units": amount,
        "status": "pending-verification" if identity else "unclaimed",
        "rejection_code": None if identity else "unclaimed",
        "raw_event": raw_event,
    }
    observation, created = DepositObservation.objects.get_or_create(event_key=event_key, defaults=defaults)
    if not created and observation.status == "unclaimed" and identity:
        observation.wallet_identity = identity
        observation.funding_intent = intent
        observation.status = "pending-verification"
        observation.rejection_code = None
        observation.save(update_fields=["wallet_identity", "funding_intent", "status", "rejection_code"])
    if intent and intent.status == "awaiting-wallet-approval":
        intent.status = "pending-verification"
        intent.save(update_fields=["status"])
    return observation, created


def _cursor(chain):
    return ChainCursor.objects.get_or_create(key=f"evm:{chain}", defaults={"chain": chain})[0]


def _block_range(chain, latest):
    cursor = _cursor(chain)
    confirmation_tip = latest - int(settings.CONI_DEPOSIT_CONFIRMATIONS) + 1
    if confirmation_tip < 0:
        return cursor, None, None
    start = cursor.cursor_number + 1 if cursor.cursor_number is not None else max(0, confirmation_tip - settings.CONI_DEPOSIT_SCAN_LOOKBACK_BLOCKS + 1)
    if start > confirmation_tip:
        return cursor, None, None
    end = min(confirmation_tip, start + settings.CONI_DEPOSIT_SCAN_BATCH - 1)
    return cursor, start, end


def discover_evm_chain(chain):
    url = _rpc_urls().get(chain)
    if not url:
        return {"chain": chain, "status": "provider-unavailable", "created": 0}
    expected = _expected_chain_ids().get(chain)
    if not expected or str(_rpc(url, "eth_chainId", [])).lower() != str(expected).lower():
        raise ProviderError(f"RPC chain identity is not configured for {chain}")
    cursor = _cursor(chain)
    if cursor.cursor_number is not None and cursor.cursor_hash:
        canonical = _rpc(url, "eth_getBlockByNumber", [hex(cursor.cursor_number), False]) or {}
        if canonical.get("hash") != cursor.cursor_hash:
            cursor.cursor_number = max(0, cursor.cursor_number - settings.CONI_DEPOSIT_CONFIRMATIONS)
            cursor.cursor_hash = None
            cursor.status = "reorg-rewind"
            cursor.save(update_fields=["cursor_number", "cursor_hash", "status", "updated_at"])
    latest = _hex_int(_rpc(url, "eth_blockNumber", []))
    cursor, start, end = _block_range(chain, latest)
    if start is None:
        cursor.status = "idle"
        cursor.last_scan_at = timezone.now()
        cursor.save(update_fields=["status", "last_scan_at", "updated_at"])
        return {"chain": chain, "status": "idle", "created": 0}
    targets = _targets(chain)
    if not targets:
        return {"chain": chain, "status": "unconfigured", "created": 0}
    created = 0
    try:
        native = native_asset(chain)
        native_targets = targets.get(native, set())
        for number in range(start, end + 1):
            block = _rpc(url, "eth_getBlockByNumber", [hex(number), True]) or {}
            for index, tx_data in enumerate(block.get("transactions") or []):
                destination = str(tx_data.get("to") or "")
                value = _hex_int(tx_data.get("value", "0x0"))
                if native and destination.lower() in native_targets and value > 0:
                    _, was_created = _record_candidate(chain=chain, asset=native, sender=tx_data.get("from", ""), destination=destination, tx_hash=tx_data.get("hash", ""), event_index=_hex_int(tx_data.get("transactionIndex", hex(index))), amount=value, block_number=number, block_hash=block.get("hash"), raw_event={"type": "native", "block": number, "transactionIndex": index})
                    created += int(was_created)
        for asset, asset_targets in targets.items():
            if asset == native:
                continue
            config = token_registry().get(asset, {}).get(chain, {})
            token_address = config.get("address") if isinstance(config, dict) else config
            if not token_address:
                continue
            for destination in asset_targets:
                topic_to = "0x" + destination[2:].lower().rjust(64, "0")
                logs = _rpc(url, "eth_getLogs", [{"fromBlock": hex(start), "toBlock": hex(end), "address": token_address, "topics": [TRANSFER_TOPIC, None, topic_to]}]) or []
                for log in logs:
                    topics = log.get("topics") or []
                    if len(topics) < 3:
                        continue
                    sender = "0x" + topics[1][-40:]
                    recipient = "0x" + topics[2][-40:]
                    amount = int(log.get("data", "0x0"), 16)
                    _, was_created = _record_candidate(chain=chain, asset=asset, sender=sender, destination=recipient, tx_hash=log.get("transactionHash", ""), event_index=_hex_int(log.get("logIndex", "0x0")), amount=amount, block_number=_hex_int(log.get("blockNumber", hex(start))), block_hash=log.get("blockHash"), raw_event={"type": "erc20-transfer", "token": token_address, "logIndex": log.get("logIndex")})
                    created += int(was_created)
        cursor.cursor_number = end
        cursor.cursor_hash = _rpc(url, "eth_getBlockByNumber", [hex(end), False]).get("hash")
        cursor.status = "healthy"
        cursor.last_scan_at = timezone.now()
        cursor.last_success_at = timezone.now()
        cursor.last_error = None
        cursor.save(update_fields=["cursor_number", "cursor_hash", "status", "last_scan_at", "last_success_at", "last_error", "updated_at"])
        return {"chain": chain, "status": "healthy", "created": created, "from": start, "to": end}
    except Exception as exc:
        cursor.status = "error"
        cursor.last_scan_at = timezone.now()
        cursor.last_error = str(exc)[:255]
        cursor.save(update_fields=["status", "last_scan_at", "last_error", "updated_at"])
        raise


def discover_solana():
    chain = "solana"
    recipient = recipient_for("SOL", chain)
    if not settings.CONI_SOLANA_RPC_URL or not recipient:
        return {"chain": chain, "status": "unconfigured", "created": 0}
    cursor, _ = ChainCursor.objects.get_or_create(key="solana:address", defaults={"chain": chain})
    params = [recipient, {"limit": min(settings.CONI_DEPOSIT_SCAN_BATCH, 1000), "commitment": "finalized"}]
    if cursor.cursor_hash:
        params[1]["before"] = cursor.cursor_hash
    signatures = _rpc(settings.CONI_SOLANA_RPC_URL, "getSignaturesForAddress", params) or []
    created = 0
    for item in reversed(signatures):
        signature = item.get("signature")
        if not signature or item.get("err"):
            continue
        transaction_data = _rpc(settings.CONI_SOLANA_RPC_URL, "getTransaction", [signature, {"encoding": "jsonParsed", "commitment": "finalized", "maxSupportedTransactionVersion": 0}])
        if not transaction_data or transaction_data.get("meta", {}).get("err") is not None:
            continue
        instructions = list(((transaction_data.get("transaction") or {}).get("message") or {}).get("instructions") or [])
        for group in transaction_data.get("meta", {}).get("innerInstructions") or []:
            instructions.extend(group.get("instructions") or [])
        event_index = 0
        for instruction in instructions:
            parsed = instruction.get("parsed") or {}
            info = parsed.get("info") or {}
            if parsed.get("program") != "system" or parsed.get("type") != "transfer" or info.get("destination") != recipient:
                continue
            amount = int(info.get("lamports", 0))
            if amount <= 0:
                continue
            _, was_created = _record_candidate(chain=chain, asset="SOL", sender=info.get("source", ""), destination=recipient, tx_hash=signature, event_index=event_index, amount=amount, block_number=transaction_data.get("slot"), block_hash=None, raw_event={"type": "solana-system-transfer", "slot": transaction_data.get("slot")})
            created += int(was_created)
            event_index += 1
    if signatures:
        cursor.cursor_hash = signatures[-1].get("signature")
    cursor.status = "healthy"
    cursor.last_scan_at = timezone.now()
    cursor.last_success_at = timezone.now()
    cursor.last_error = None
    cursor.save(update_fields=["cursor_hash", "status", "last_scan_at", "last_success_at", "last_error", "updated_at"])
    spl = discover_solana_spl()
    return {"chain": chain, "status": "healthy", "created": created + spl.get("created", 0), "spl": spl}


def _spl_cursor(asset, token_account):
    digest = hashlib.sha256(token_account.encode("utf-8")).hexdigest()[:20]
    return ChainCursor.objects.get_or_create(key=f"solspl:{asset}:{digest}", defaults={"chain": "solana"})[0]


def _solana_instruction_matches(transaction_data, token_account, mint, program_id):
    """Yield parsed SPL transfers into one treasury token account."""
    instructions = []
    message = ((transaction_data.get("transaction") or {}).get("message") or {})
    instructions.extend(message.get("instructions") or [])
    for group in transaction_data.get("meta", {}).get("innerInstructions") or []:
        instructions.extend(group.get("instructions") or [])
    keys = []
    for key in message.get("accountKeys") or []:
        keys.append(key.get("pubkey") if isinstance(key, dict) else key)
    balances = {}
    for item in (transaction_data.get("meta", {}).get("postTokenBalances") or []):
        index = item.get("accountIndex")
        if isinstance(index, int) and index < len(keys):
            balances[keys[index]] = item
    for index, instruction in enumerate(instructions):
        parsed = instruction.get("parsed") or {}
        info = parsed.get("info") or {}
        program = str(parsed.get("program") or instruction.get("program") or "").lower()
        if program not in {"spl-token", "spl-token-2022", "spltoken"} or parsed.get("type") not in {"transfer", "transferChecked"}:
            continue
        if info.get("destination") != token_account:
            continue
        destination_balance = balances.get(token_account) or {}
        if destination_balance.get("mint") != mint:
            continue
        if program_id and destination_balance.get("programId") and destination_balance.get("programId") != program_id:
            continue
        amount_data = info.get("tokenAmount") or {}
        amount = amount_data.get("amount") or info.get("amount")
        if int(amount or 0) <= 0:
            continue
        yield index, info, amount, destination_balance


def discover_solana_spl():
    """Discover unclaimed SPL transfers to every configured treasury token account."""
    chain, rpc = "solana", settings.CONI_SOLANA_RPC_URL
    if not rpc:
        return {"chain": chain, "status": "unconfigured", "created": 0}
    created = 0
    configured = 0
    for asset, config in token_registry().items():
        chain_config = config.get(chain) if isinstance(config, dict) else None
        mint = chain_config.get("address") if isinstance(chain_config, dict) else chain_config
        program_id = chain_config.get("programId") if isinstance(chain_config, dict) else None
        recipient = recipient_for(asset, chain)
        if not mint or not recipient:
            continue
        configured += 1
        account_params = {"mint": mint, "encoding": "jsonParsed", "commitment": "finalized"}
        account_data = _rpc(rpc, "getTokenAccountsByOwner", [recipient, account_params]) or {}
        accounts = (account_data.get("value") if isinstance(account_data, dict) else None) or []
        for account in accounts:
            token_account = account.get("pubkey") if isinstance(account, dict) else None
            if not token_account:
                continue
            cursor = _spl_cursor(asset, token_account)
            params = [token_account, {"limit": min(settings.CONI_DEPOSIT_SCAN_BATCH, 1000), "commitment": "finalized"}]
            if cursor.cursor_hash:
                params[1]["before"] = cursor.cursor_hash
            signatures = _rpc(rpc, "getSignaturesForAddress", params) or []
            for signature_data in reversed(signatures):
                signature = signature_data.get("signature")
                if not signature or signature_data.get("err"):
                    continue
                transaction_data = _rpc(rpc, "getTransaction", [signature, {"encoding": "jsonParsed", "commitment": "finalized", "maxSupportedTransactionVersion": 0}])
                if not transaction_data or transaction_data.get("meta", {}).get("err") is not None:
                    continue
                for instruction_index, info, amount, destination_balance in _solana_instruction_matches(transaction_data, token_account, mint, program_id):
                    authority = info.get("authority") or info.get("owner") or ""
                    _, was_created = _record_candidate(
                        chain=chain, asset=str(asset).upper(), sender=authority, destination=recipient,
                        tx_hash=signature, event_index=instruction_index, amount=int(amount), block_number=transaction_data.get("slot"), block_hash=None,
                        raw_event={"type": "solana-spl-transfer", "mint": mint, "tokenAccount": token_account, "tokenProgram": destination_balance.get("programId") or program_id, "sourceAccount": info.get("source"), "authority": authority, "slot": transaction_data.get("slot")},
                    )
                    created += int(was_created)
            if signatures:
                cursor.cursor_hash = signatures[-1].get("signature")
            cursor.status = "healthy"
            cursor.last_scan_at = timezone.now()
            cursor.last_success_at = timezone.now()
            cursor.last_error = None
            cursor.save(update_fields=["cursor_hash", "status", "last_scan_at", "last_success_at", "last_error", "updated_at"])
    return {"chain": chain, "status": "healthy" if configured else "unconfigured", "created": created, "accounts": configured}


def discover_deposits():
    results = []
    for chain in ("ethereum", "base", "robinhood", "bsc"):
        try:
            results.append(discover_evm_chain(chain))
        except (ProviderError, VerificationError) as exc:
            results.append({"chain": chain, "status": "provider-unavailable", "error": str(exc)[:120], "created": 0})
    try:
        results.append(discover_solana())
    except (ProviderError, VerificationError) as exc:
        results.append({"chain": "solana", "status": "provider-unavailable", "error": str(exc)[:120], "created": 0})
    record_event("deposit.discovery", result="healthy" if all(item.get("status") not in {"error", "provider-unavailable"} for item in results) else "degraded", metadata={"chains": results})
    return results
