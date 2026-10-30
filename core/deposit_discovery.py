"""Discover native and configured token deposits without trusting the browser.

Discovery only creates immutable observations. The verifier still checks the
transaction, finality, exact amount, recipient, and network before ledger credit.
"""

import hashlib

from django.conf import settings
from django.utils import timezone

from .assets import native_asset, recipient_for, token_registry
from .audit import record_event
from .chain_verifier import ProviderError, VerificationError, _expected_chain_ids, _hex_int, _rpc_urls
from .models import ChainCursor, DepositObservation, FundingIntent
from .rpc import result as rpc_result


TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a8df523b3ef"


def _provider_rpc(chain, method, params):
    """Use the allowlisted chain RPC and preserve worker retry semantics."""
    try:
        return rpc_result(chain, method, params)
    except Exception as exc:
        raise ProviderError("chain provider request failed") from exc


def _address_key(chain, address):
    return address if chain == "solana" else address.lower()


def _targets(chain):
    targets = {}
    native = native_asset(chain)
    recipient = recipient_for(native, chain) if native else ""
    if recipient:
        targets[native] = {recipient if chain == "solana" else recipient.lower()}
    for asset, config in token_registry().items():
        if not isinstance(config, dict):
            continue
        chain_config = config.get(chain)
        address = chain_config.get("address") if isinstance(chain_config, dict) else chain_config
        recipient = recipient_for(asset, chain)
        if address and recipient:
            targets.setdefault(asset, set()).add(recipient if chain == "solana" else recipient.lower())
    # Active per-user bot wallets are valid deposit recipients. They are still
    # credited only when matched to an exact, unexpired funding intent.
    from .models import BotWallet
    for wallet in BotWallet.objects.filter(chain=chain, status="active", signer_key_ref__gt="").exclude(address=""):
        for asset in token_registry().keys():
            targets.setdefault(asset, set()).add(wallet.address if chain == "solana" else wallet.address.lower())
        native = native_asset(chain)
        if native:
            targets.setdefault(native, set()).add(wallet.address if chain == "solana" else wallet.address.lower())
    return targets


def _identity_and_intent(chain, asset, sender, destination, amount):
    intent_filters = {
        "chain": chain,
        "asset": asset,
        "amount_base_units": amount,
        "status__in": ["awaiting-wallet-approval", "pending-verification"],
    }
    intent_filters["destination" if chain == "solana" else "destination__iexact"] = destination
    candidates = FundingIntent.objects.filter(**intent_filters).select_related("wallet_identity")
    for intent in candidates:
        if intent.wallet_identity and (intent.wallet_identity.address == sender if chain == "solana" else intent.wallet_identity.address.lower() == sender.lower()):
            return intent.wallet_identity, intent
    # A transfer into an active per-user bot wallet is discoverable even when
    # the user did not create an intent first. Keep it explicitly unclaimed;
    # the owner must claim it and the verifier must still prove finality.
    from .models import BotWallet
    wallet_filters = {"chain": chain, "status": "active", "signer_key_ref__gt": ""}
    wallet_filters["address" if chain == "solana" else "address__iexact"] = destination
    wallet = BotWallet.objects.filter(**wallet_filters).select_related("owner").first()
    if wallet:
        return wallet.owner, None
    return None, None


def _record_candidate(*, chain, asset, sender, destination, tx_hash, event_index, amount, block_number, block_hash, raw_event):
    identity, intent = _identity_and_intent(chain, asset, sender, destination, amount)
    event_key = f"{chain}:{tx_hash.lower() if chain != 'solana' else tx_hash}:{event_index}"
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
        "status": "pending-verification" if intent else "unclaimed",
        "rejection_code": None if intent else "unclaimed",
        "raw_event": raw_event,
    }
    observation, created = DepositObservation.objects.get_or_create(event_key=event_key, defaults=defaults)
    if not created and observation.status == "unclaimed" and identity:
        observation.wallet_identity = identity
        if intent:
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
    if not expected or str(_provider_rpc(chain, "eth_chainId", [])).lower() != str(expected).lower():
        raise ProviderError(f"RPC chain identity is not configured for {chain}")
    cursor = _cursor(chain)
    if cursor.cursor_number is not None and cursor.cursor_hash:
        canonical = _provider_rpc(chain, "eth_getBlockByNumber", [hex(cursor.cursor_number), False]) or {}
        if canonical.get("hash") != cursor.cursor_hash:
            cursor.cursor_number = max(0, cursor.cursor_number - settings.CONI_DEPOSIT_CONFIRMATIONS)
            cursor.cursor_hash = None
            cursor.status = "reorg-rewind"
            cursor.save(update_fields=["cursor_number", "cursor_hash", "status", "updated_at"])
    latest = _hex_int(_provider_rpc(chain, "eth_blockNumber", []))
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
            block = _provider_rpc(chain, "eth_getBlockByNumber", [hex(number), True]) or {}
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
                logs = _provider_rpc(chain, "eth_getLogs", [{"fromBlock": hex(start), "toBlock": hex(end), "address": token_address, "topics": [TRANSFER_TOPIC, None, topic_to]}]) or []
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
        cursor.cursor_hash = _provider_rpc(chain, "eth_getBlockByNumber", [hex(end), False]).get("hash")
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


def _solana_native_recipients():
    """Return the treasury and every active per-user SOL bot-wallet address."""
    chain = "solana"
    recipients = set()
    treasury = recipient_for("SOL", chain)
    if treasury:
        recipients.add(treasury)
    from .models import BotWallet
    recipients.update(
        wallet.address
        for wallet in BotWallet.objects.filter(chain=chain, status="active", signer_key_ref__gt="").exclude(address="")
        if wallet.address
    )
    return sorted(recipients, key=str.lower)


def _solana_native_cursor(recipient):
    digest = hashlib.sha256(recipient.encode("utf-8")).hexdigest()[:20]
    return ChainCursor.objects.get_or_create(key=f"solana:native:{digest}", defaults={"chain": "solana"})[0]


def discover_solana():
    chain = "solana"
    if not settings.CONI_SOLANA_RPC_URL:
        return {"chain": chain, "status": "unconfigured", "created": 0}
    recipients = _solana_native_recipients()
    if not recipients:
        return {"chain": chain, "status": "unconfigured", "created": 0}
    created = 0
    scanned_recipients = 0
    for recipient in recipients:
        cursor = _solana_native_cursor(recipient)
        params = [recipient, {"limit": min(settings.CONI_DEPOSIT_SCAN_BATCH, 1000), "commitment": "finalized"}]
        if cursor.cursor_hash:
            params[1]["before"] = cursor.cursor_hash
        signatures = _provider_rpc(chain, "getSignaturesForAddress", params) or []
        for item in reversed(signatures):
            signature = item.get("signature")
            if not signature or item.get("err"):
                continue
            transaction_data = _provider_rpc(chain, "getTransaction", [signature, {"encoding": "jsonParsed", "commitment": "finalized", "maxSupportedTransactionVersion": 0}])
            if not transaction_data or transaction_data.get("meta", {}).get("err") is not None:
                continue
            instructions = list(((transaction_data.get("transaction") or {}).get("message") or {}).get("instructions") or [])
            for group in transaction_data.get("meta", {}).get("innerInstructions") or []:
                instructions.extend(group.get("instructions") or [])
            for event_index, instruction in enumerate(instructions):
                parsed = instruction.get("parsed") or {}
                info = parsed.get("info") or {}
                if str(parsed.get("program") or instruction.get("program") or "").lower() != "system" or parsed.get("type") != "transfer" or info.get("destination") != recipient:
                    continue
                amount = int(info.get("lamports", 0))
                if amount <= 0:
                    continue
                _, was_created = _record_candidate(chain=chain, asset="SOL", sender=info.get("source", ""), destination=recipient, tx_hash=signature, event_index=event_index, amount=amount, block_number=transaction_data.get("slot"), block_hash=None, raw_event={"type": "solana-system-transfer", "slot": transaction_data.get("slot")})
                created += int(was_created)
        if signatures:
            cursor.cursor_hash = signatures[-1].get("signature")
        cursor.status = "healthy"
        cursor.last_scan_at = timezone.now()
        cursor.last_success_at = timezone.now()
        cursor.last_error = None
        cursor.save(update_fields=["cursor_hash", "status", "last_scan_at", "last_success_at", "last_error", "updated_at"])
        scanned_recipients += 1
    spl = discover_solana_spl()
    return {"chain": chain, "status": "healthy", "created": created + spl.get("created", 0), "nativeRecipients": scanned_recipients, "spl": spl}


def _spl_cursor(asset, token_account):
    digest = hashlib.sha256(token_account.encode("utf-8")).hexdigest()[:20]
    return ChainCursor.objects.get_or_create(key=f"solspl:{asset}:{digest}", defaults={"chain": "solana"})[0]


def _spl_account_lookup_params(mint):
    """Return Solana's positional filter and config for token-account lookup."""
    return [
        {"mint": mint},
        {"encoding": "jsonParsed", "commitment": "finalized"},
    ]


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
        # If the registry pins a token program, an omitted program id is not
        # sufficient evidence.  Do not discover a transfer through a provider
        # response that could belong to a different SPL program.
        if program_id and destination_balance.get("programId") != program_id:
            continue
        amount_data = info.get("tokenAmount") or {}
        amount = amount_data.get("amount") or info.get("amount")
        if int(amount or 0) <= 0:
            continue
        yield index, info, amount, destination_balance


def discover_solana_spl():
    """Discover SPL transfers to treasury and active user bot-wallet owners.

    A discovered transfer is still only an observation. It is not credited
    unless an exact funding intent, sender, asset, destination, amount, and
    finalized transaction all pass the verifier.
    """
    chain, rpc = "solana", settings.CONI_SOLANA_RPC_URL
    if not rpc:
        return {"chain": chain, "status": "unconfigured", "created": 0}
    created = 0
    configured = 0
    from .models import BotWallet

    for asset, config in token_registry().items():
        chain_config = config.get(chain) if isinstance(config, dict) else None
        mint = chain_config.get("address") if isinstance(chain_config, dict) else chain_config
        program_id = chain_config.get("programId") if isinstance(chain_config, dict) else None
        recipients = set()
        treasury_recipient = recipient_for(asset, chain)
        if treasury_recipient:
            recipients.add(treasury_recipient)
        recipients.update(
            wallet.address
            for wallet in BotWallet.objects.filter(chain=chain, status="active", signer_key_ref__gt="").exclude(address="")
            if wallet.address
        )
        if not mint or not recipients:
            continue
        for recipient in sorted(recipients):
            configured += 1
            account_data = _provider_rpc(chain, "getTokenAccountsByOwner", [recipient, *_spl_account_lookup_params(mint)]) or {}
            accounts = (account_data.get("value") if isinstance(account_data, dict) else None) or []
            for account in accounts:
                token_account = account.get("pubkey") if isinstance(account, dict) else None
                if not token_account:
                    continue
                cursor = _spl_cursor(asset, token_account)
                params = [token_account, {"limit": min(settings.CONI_DEPOSIT_SCAN_BATCH, 1000), "commitment": "finalized"}]
                if cursor.cursor_hash:
                    params[1]["before"] = cursor.cursor_hash
                signatures = _provider_rpc(chain, "getSignaturesForAddress", params) or []
                for signature_data in reversed(signatures):
                    signature = signature_data.get("signature")
                    if not signature or signature_data.get("err"):
                        continue
                    transaction_data = _provider_rpc(chain, "getTransaction", [signature, {"encoding": "jsonParsed", "commitment": "finalized", "maxSupportedTransactionVersion": 0}])
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
