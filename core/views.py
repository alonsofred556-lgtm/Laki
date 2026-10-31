import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation

import base58
from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, connection, transaction
from django.db.models import Count, Sum
from django.http import FileResponse, HttpRequest, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from eth_account import Account
from eth_account.messages import encode_defunct
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

from .layerzero import route_summary
from .assets import asset_decimals, browser_funding_supported, native_asset, recipient_for, supported_asset, token_config, token_registry
from .audit import record_event
from .funding import InsufficientBalance, claim_withdrawal_requests, record_withdrawal_event, reserve_withdrawal
from .models import AuditEvent, ArbitrageOpportunity, BotControl, BotWallet, BotWalletProvisionRequest, BotWalletTransfer, ChainCursor, DepositObservation, Execution, FeeEvent, FundingIntent, ProfitAllocation, ProfitSettlement, ReconciliationRun, WalletChallenge, WalletIdentity, WithdrawalRequest
from .bot_wallets import accessible_bot_chains, addresses_match, bot_wallet_payload, bot_wallet_summary, chain_accessible, claim_bot_transfer_requests, claim_provision_requests, create_bot_transfer, ensure_bot_wallet, ensure_bot_wallets, record_bot_transfer_event
from .bot_controls import BOT_MODES, effective_user_policy, ensure_user_bot_control, global_bot_control
from .execution import create_execution_from_opportunity, execution_payload, record_execution_event, signer_intent_payload
from .arbitrage import fresh_user_quotes
from .fees import accrue_fee, claim_fee_events, fee_amount_base_units as calculate_fee_amount, fee_intent_payload, fee_recipient_for, record_fee_event
from .pangea import PangeaError, request_json, scan_opportunities, token_registry_snapshot
from .motion import MotionError, discoveries as motion_discoveries
from .profit_compounding import compound_intent_payload, record_compound_event
from .profit_settlement import prepare_usdg_settlement, record_usdg_settlement_event
from .rate_limit import RateLimitUnavailable, allow as redis_allow
from .chain_verifier import observation_is_stale
from .rpc import RpcError, forward, configured_health

auth_logger = logging.getLogger("coni.auth")

EVM_CHAINS = {"ethereum", "base", "robinhood", "bsc"}
CHAINS = EVM_CHAINS | {"solana"}
ASSET_DECIMALS = {"ETH": 18, "BNB": 18, "SOL": 9}
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,80}$", re.I)
EVM_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
SOL_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
EVM_TX_RE = re.compile(r"^0x[a-fA-F0-9]{64}$")
SOL_TX_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{40,100}$")
IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
SIGNER_REF_RE = re.compile(r"^[A-Za-z0-9._:/-]{8,128}$")
HASH_RE = re.compile(r"^(0x)?[a-fA-F0-9]{64}$")
MAX_BASE_UNIT_DIGITS = 78


def api_response(data, status=200, request=None, extra=None):
    response = JsonResponse(data, status=status)
    origin = request.headers.get("Origin") if request else None
    if origin in settings.CONI_ALLOWED_ORIGINS or origin in getattr(settings, "CONI_EXTENSION_ORIGINS", set()):
        response["Access-Control-Allow-Origin"] = origin
        response["Access-Control-Allow-Credentials"] = "true"
    response["Cache-Control"] = "no-store"
    if extra:
        for key, value in extra.items():
            response[key] = value
    return response


def error(message, status=400, request=None):
    return api_response({"error": message}, status, request)


def body(request):
    if len(request.body) > 16 * 1024:
        raise ValueError("request body too large")
    if not request.body:
        return {}
    value = json.loads(request.body.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("invalid JSON object")
    return value


def post_allowed(request):
    origin = request.headers.get("Origin")
    return not origin or origin in settings.CONI_ALLOWED_ORIGINS or origin in settings.CONI_EXTENSION_ORIGINS


def ops_allowed(request):
    configured = settings.CONI_OPS_TOKEN
    return bool(configured and secrets.compare_digest(request.headers.get("X-Coni-Ops-Token", ""), configured))


def signer_provision_allowed(request):
    return signer_callback_allowed(request, settings.CONI_SIGNER_PROVISION_SECRET, "X-Coni-Signer-Provision-Secret")


def signer_event_allowed(request):
    return signer_callback_allowed(request, settings.CONI_SIGNER_EVENT_SECRET, "X-Coni-Signer-Event-Secret")


def signer_callback_allowed(request, configured, legacy_header):
    """Authenticate a private signer callback and prevent delayed replays."""
    if not configured:
        return False
    timestamp = request.headers.get("X-Coni-Signer-Timestamp", "")
    signature = request.headers.get("X-Coni-Signer-Signature", "")
    try:
        timestamp_int = int(timestamp)
    except (TypeError, ValueError):
        timestamp_int = 0
    now = int(timezone.now().timestamp())
    if timestamp_int and abs(now - timestamp_int) <= settings.CONI_SIGNER_CALLBACK_MAX_SKEW_SECONDS and re.fullmatch(r"[a-fA-F0-9]{64}", signature or ""):
        message = f"{timestamp_int}.".encode() + request.body
        expected = hmac.new(configured.encode(), message, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature.lower(), expected.lower())
    if settings.CONI_SIGNER_CALLBACK_REQUIRE_HMAC:
        return False
    supplied = request.headers.get(legacy_header, "") or request.headers.get("Authorization", "").removeprefix("Bearer ")
    return bool(supplied and secrets.compare_digest(supplied, configured))


def request_id(request):
    value = request.headers.get("X-Request-ID", "")
    return value[:128] if re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value) else None


def rate_limited(request, bucket, limit=10, window=60):
    key = request.META.get("REMOTE_ADDR", "unknown")
    try:
        allowed, remaining = redis_allow(bucket, key, limit, window)
    except RateLimitUnavailable:
        return error("security rate limiting is temporarily unavailable", 503, request)
    if not allowed:
        return error("too many requests", 429, request, {"Retry-After": str(window), "X-RateLimit-Remaining": "0"})
    return None


def wallet_address_valid(chain, address):
    if not isinstance(address, str):
        return False
    if chain in EVM_CHAINS:
        return bool(EVM_RE.fullmatch(address)) and address.lower() != "0x" + "0" * 40
    return bool(SOL_RE.fullmatch(address)) and address != "1" * 32


def canonical_wallet_address(chain, address):
    """Return the storage form for a validated wallet address.

    EVM addresses are byte-identities and are compared case-insensitively by
    the protocol.  Persisting checksum casing would allow the same wallet to
    create multiple WalletIdentity and bot-wallet trees.  Solana addresses
    remain case-sensitive and are returned unchanged.
    """
    if not wallet_address_valid(chain, address):
        return address
    return address.lower() if chain in EVM_CHAINS else address


def wallet_address_matches(chain, supplied, expected):
    """EVM addresses are case-insensitive; Solana base58 addresses are not."""
    if not isinstance(supplied, str) or not isinstance(expected, str):
        return False
    return supplied == expected if chain == "solana" else supplied.lower() == expected.lower()


def valid_amount(value):
    return isinstance(value, str) and re.fullmatch(r"\d{1,78}", value or "") and int(value) > 0


def decimal_units(value, decimals):
    if not isinstance(value, str) or not re.fullmatch(r"\d+(\.\d+)?", value):
        return None
    whole, _, fraction = value.partition(".")
    if len(fraction) > decimals:
        return None
    if len(whole) + len(fraction) > MAX_BASE_UNIT_DIGITS:
        return None
    try:
        result = int(f"{whole}{fraction.ljust(decimals, '0')}")
    except ValueError:
        return None
    return str(result) if result > 0 else None


def _session_identity_ids(request):
    """Return authenticated wallet identities recorded in this browser session.

    Older sessions stored one scalar ``wallet_identity_id``.  Keep that value
    as a migration-free fallback while allowing an EVM and Solana identity to
    remain authenticated at the same time.
    """
    values = request.session.get("wallet_identity_ids", [])
    if not isinstance(values, list):
        values = []
    legacy = request.session.get("wallet_identity_id")
    if legacy and legacy not in values:
        values.append(legacy)
    return [value for value in values if isinstance(value, str) and len(value) <= 64]


def session_identities(request):
    ids = _session_identity_ids(request)
    if not ids:
        return []
    found = {str(item.id): item for item in WalletIdentity.objects.filter(id__in=ids)}
    # Preserve the session's most-recent-first ordering.  It is used as the
    # active identity when an endpoint has no chain-specific selector.
    return [found[value] for value in reversed(ids) if value in found]


def session_identity(request, chain=None, identity_id=None):
    identities = session_identities(request)
    if identity_id is not None:
        return next((item for item in identities if str(item.id) == str(identity_id)), None)
    if chain:
        exact = next((item for item in identities if item.chain == chain), None)
        if exact:
            return exact
        if chain in EVM_CHAINS:
            return next((item for item in identities if item.chain in EVM_CHAINS), None)
        return None
    return identities[0] if identities else None


def require_session(request, chain=None, identity_id=None):
    identity = session_identity(request, chain=chain, identity_id=identity_id)
    if not identity:
        raise PermissionError("authenticated wallet session required")
    return identity


def require_bot_wallet_session(request, wallet_id):
    """Resolve a bot wallet only among identities authenticated in this session."""
    if not wallet_id:
        return require_session(request)
    try:
        wallet_id = uuid.UUID(str(wallet_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise PermissionError("authenticated wallet session required") from exc
    identity = next(
        (item for item in session_identities(request) if BotWallet.objects.filter(id=wallet_id, owner=item).exists()),
        None,
    )
    if not identity:
        raise PermissionError("authenticated wallet session required")
    return identity


def parse_json_setting(value):
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def execution_ready():
    return (
        settings.CONI_EXECUTION_MODE == "live"
        and bool(settings.CONI_SIGNER_URL)
        and settings.CONI_LIVE_TRADING_CONFIRMATION == "I_UNDERSTAND_LIVE_RISK"
        and settings.CONI_SIGNER_AUDIT_APPROVED
    )


def bot_status():
    control = global_bot_control()
    recipients = parse_json_setting(settings.CONI_FEE_RECIPIENTS)
    withdrawal_limits_configured = bool(parse_json_setting(getattr(settings, "CONI_BOT_WITHDRAWAL_LIMITS", "{}")))
    try:
        native_reserve_configured = bool(parse_json_setting(settings.CONI_ARBITRAGE_NATIVE_RESERVE_BASE_UNITS))
    except (TypeError, ValueError, json.JSONDecodeError):
        native_reserve_configured = False
    return {"mode": settings.CONI_EXECUTION_MODE, "executionReady": execution_ready(), "database": True, "enabled": control.enabled, "paused": control.paused, "minNetBps": control.min_net_bps, "maxNotionalUsd": float(control.max_notional_usd), "compoundProfitBps": control.compound_profit_bps, "confidencePolicyBps": settings.CONI_ARBITRAGE_MIN_CONFIDENCE_BPS, "maxQuoteAgeSeconds": settings.CONI_ARBITRAGE_MAX_QUOTE_AGE_SECONDS, "maxPriceMoveBps": settings.CONI_ARBITRAGE_MAX_PRICE_MOVE_BPS, "maxMarketCapMoveBps": settings.CONI_ARBITRAGE_MAX_MARKET_CAP_MOVE_BPS, "requireMarketCapData": settings.CONI_ARBITRAGE_REQUIRE_MARKET_CAP_DATA, "scanIntervalSeconds": settings.CONI_ARBITRAGE_SCAN_INTERVAL_SECONDS, "quoteIntervalSeconds": settings.CONI_ARBITRAGE_QUOTE_INTERVAL_SECONDS, "planIntervalSeconds": settings.CONI_ARBITRAGE_PLAN_INTERVAL_SECONDS, "minDepthMultiple": str(settings.CONI_ARBITRAGE_MIN_DEPTH_MULTIPLE), "maxPriceImpactBps": settings.CONI_ARBITRAGE_MAX_PRICE_IMPACT_BPS, "minBridgeRetentionBps": settings.CONI_ARBITRAGE_MIN_BRIDGE_RETENTION_BPS, "signerConfigured": bool(settings.CONI_SIGNER_URL), "signerAuditApproved": settings.CONI_SIGNER_AUDIT_APPROVED, "feeConfigConfigured": bool(recipients), "withdrawalLimitsRequired": getattr(settings, "CONI_BOT_WITHDRAWAL_LIMITS_REQUIRED", not settings.DEBUG), "withdrawalLimitsConfigured": withdrawal_limits_configured, "nativeReserveConfigured": native_reserve_configured, "quoteRequired": settings.CONI_ARBITRAGE_REQUIRE_QUOTES, "riskBufferBps": settings.CONI_ARBITRAGE_RISK_BUFFER_BPS, "profitSettlement": {"enabled": settings.CONI_PROFIT_SETTLEMENT_ENABLED, "asset": settings.CONI_PROFIT_SETTLEMENT_ASSET, "chain": settings.CONI_PROFIT_SETTLEMENT_CHAIN, "tokenConfigured": bool(settings.CONI_PROFIT_SETTLEMENT_TOKEN_ADDRESS), "decimalsConfigured": settings.CONI_PROFIT_SETTLEMENT_DECIMALS is not None, "priceConfigured": bool(getattr(settings, "CONI_PROFIT_SETTLEMENT_PRICE_USD", ""))}}


def user_bot_status(identity):
    control = global_bot_control()
    return effective_user_policy(identity, control)


def health(request):
    database = False
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            database = cursor.fetchone()[0] == 1
    except Exception:
        pass
    redis_ok = False
    redis_required = settings.CONI_REDIS_REQUIRED
    try:
        if settings.CONI_REDIS_URL:
            from .rate_limit import _client
            redis_ok = bool(_client().ping())
        else:
            redis_ok = not redis_required
    except Exception:
        redis_ok = False
    rpc_health = configured_health()
    return api_response({"ok": database and (redis_ok or not redis_required), "service": "coni-django", "database": database, "redis": redis_ok, "redisRequired": redis_required, "rpc": rpc_health, "time": timezone.now().isoformat()}, 200, request)


@csrf_exempt
def rpc_proxy(request, chain):
    """Session-authenticated, allowlisted JSON-RPC relay for Coni wallets.

    The relay never receives a private key. Write methods accept only already
    signed raw transactions, so it cannot authorize a transfer on a user's
    behalf. Node URLs remain server-side in Railway secrets.
    """
    if request.method != "POST" or not post_allowed(request):
        return error("method or origin not allowed", 405 if request.method != "POST" else 403, request)
    try:
        require_session(request, chain=chain)
    except PermissionError as exc:
        return error(str(exc), 401, request)
    limited = rate_limited(request, f"rpc-{chain}", 90, 60)
    if limited:
        return limited
    if chain not in CHAINS:
        return error("unsupported RPC chain", 404, request)
    if len(request.body) > 256 * 1024:
        return error("RPC request too large", 413, request)
    try:
        payload = json.loads(request.body.decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0" or "method" not in payload:
            return error("invalid JSON-RPC request", 400, request)
        params = payload.get("params", [])
        response = forward(chain, payload["method"], params, payload.get("id", 1))
        return api_response(response, 200, request)
    except (ValueError, json.JSONDecodeError, RpcError) as exc:
        return error(str(exc), 400 if isinstance(exc, RpcError) else 400, request)


def operations_health(request):
    if not ops_allowed(request):
        return error("operations authorization required", 403, request)
    cursors = list(ChainCursor.objects.values("key", "chain", "cursor_number", "status", "last_success_at", "last_error"))
    latest = ReconciliationRun.objects.order_by("-started_at").first()
    counts = {}
    for row in DepositObservation.objects.values("status").annotate(total=Count("id")):
        counts[row["status"]] = row["total"]
    return api_response({
        "service": "coni-django",
        "time": timezone.now().isoformat(),
        "bot": bot_status(),
        "deposits": counts,
        "cursors": cursors,
        "lastReconciliation": {"id": str(latest.id), "status": latest.status, "checked": latest.checked_count, "anomalies": latest.anomaly_count, "finishedAt": latest.finished_at.isoformat() if latest.finished_at else None} if latest else None,
        "lastAuditAt": AuditEvent.objects.order_by("-created_at").values_list("created_at", flat=True).first(),
    }, 200, request)


@csrf_exempt
def auth_challenge(request):
    if request.method != "POST" or not post_allowed(request):
        return error("method or origin not allowed", 405 if request.method != "POST" else 403, request)
    limited = rate_limited(request, "auth-challenge", 10, 60)
    if limited:
        return limited
    try:
        data = body(request)
        chain, address = data.get("chain"), data.get("address")
        address = canonical_wallet_address(chain, address)
        if set(data) - {"chain", "address"} or chain not in CHAINS or not wallet_address_valid(chain, address):
            return error("invalid wallet challenge", 400, request)
        now = timezone.now()
        expires = now + timedelta(minutes=5)
        nonce = secrets.token_hex(24)
        network_name = "Ethereum" if chain in EVM_CHAINS else "Solana"
        message = f"coni wants you to sign in with your {network_name} account:\n{address}\n\nSign in to Coni. This signature is free and cannot move funds.\n\nURI: {settings.CONI_AUTH_URI}\nChain: {chain}\nNonce: {nonce}\nIssued At: {now.isoformat()}\nExpiration Time: {expires.isoformat()}"
        challenge = WalletChallenge.objects.create(chain=chain, address=address, message=message, nonce=nonce, expires_at=expires)
        return api_response({"challengeId": str(challenge.id), "chain": chain, "address": address, "message": message, "expiresAt": expires.isoformat()}, 201, request)
    except (ValueError, json.JSONDecodeError) as exc:
        return error(str(exc), 400, request)


def verify_signature(challenge, signature):
    if challenge.chain in EVM_CHAINS:
        if not isinstance(signature, str):
            return False
        value = signature.strip()
        # EIP-1193 wallets normally return a 65-byte 0x-prefixed signature.
        # Some providers return the same bytes without the prefix; normalize
        # both forms before eth-account recovery. EIP-2098 compact signatures
        # are expanded to r||s||v for older eth-account versions.
        if re.fullmatch(r"0x[a-fA-F0-9]{128}", value):
            compact = bytes.fromhex(value[2:])
            r = compact[:32]
            vs = int.from_bytes(compact[32:], "big")
            parity = (vs >> 255) & 1
            s = (vs & ((1 << 255) - 1)).to_bytes(32, "big")
            value = "0x" + (r + s + bytes([parity + 27])).hex()
        elif re.fullmatch(r"(?:0x)?[a-fA-F0-9]{130}", value):
            raw = bytes.fromhex(value[2:] if value.startswith("0x") else value)
            if raw[-1] in (0, 1):
                raw = raw[:64] + bytes([raw[-1] + 27])
            value = "0x" + raw.hex()
        elif re.fullmatch(r"[a-fA-F0-9]{128}", value):
            value = "0x" + value
        if not re.fullmatch(r"0x[a-fA-F0-9]{130}", value):
            return False
        # EIP-1193 wallets are inconsistent about ``personal_sign``: most
        # providers hex-decode the first parameter before signing, while a
        # few injected providers sign the literal string they received. The
        # frontend sends the hex form, but accepting both canonical forms here
        # keeps authentication interoperable without weakening ownership
        # verification: both candidates are derived solely from the stored
        # server challenge and must recover the requested address.
        messages = [challenge.message]
        encoded = "0x" + challenge.message.encode("utf-8").hex()
        if encoded != challenge.message:
            messages.append(encoded)
        for message in messages:
            try:
                recovered = Account.recover_message(encode_defunct(text=message), signature=value)
            except (TypeError, ValueError, IndexError):
                continue
            if recovered.lower() == challenge.address.lower():
                return True
        return False
    try:
        public_key = base58.b58decode(challenge.address)
        if not isinstance(signature, str):
            return False
        candidates = []
        encoded_signature = signature[2:] if signature.startswith("0x") else signature
        try:
            candidates.append(base64.b64decode(encoded_signature, validate=True))
        except (ValueError, base64.binascii.Error):
            pass
        try:
            candidates.append(base58.b58decode(encoded_signature))
        except ValueError:
            pass
        if re.fullmatch(r"[a-fA-F0-9]{128}", encoded_signature):
            candidates.append(bytes.fromhex(encoded_signature))
        message = challenge.message.encode("utf-8")
        for signed in candidates:
            if len(signed) != 64:
                continue
            try:
                VerifyKey(public_key).verify(message, signed)
                return True
            except BadSignatureError:
                continue
        return False
    except (ValueError, BadSignatureError):
        return False


@csrf_exempt
def auth_verify(request):
    if request.method != "POST" or not post_allowed(request):
        return error("method or origin not allowed", 405 if request.method != "POST" else 403, request)
    limited = rate_limited(request, "auth-verify", 10, 60)
    if limited:
        return limited
    try:
        data = body(request)
        requested_mode = data.get("walletMode")
        if set(data) - {"challengeId", "address", "signature", "walletMode"} or (requested_mode is not None and requested_mode not in BOT_MODES):
            return error("invalid wallet mode", 400, request)
        supplied_address = data.get("address")
        challenge = WalletChallenge.objects.filter(id=data.get("challengeId"), expires_at__gt=timezone.now()).first()
        if not challenge or not wallet_address_matches(challenge.chain, supplied_address, challenge.address) or not isinstance(data.get("signature"), str):
            auth_logger.warning("wallet auth rejected request_id=%s reason=challenge-or-address", request_id(request))
            return error("challenge expired or invalid", 401, request)
        if not verify_signature(challenge, data["signature"]):
            auth_logger.warning("wallet auth rejected request_id=%s reason=signature chain=%s", request_id(request), challenge.chain)
            return error("wallet signature verification failed", 401, request)
        with transaction.atomic():
            locked_challenge = WalletChallenge.objects.select_for_update().filter(id=challenge.id, consumed_at__isnull=True, expires_at__gt=timezone.now()).first()
            if not locked_challenge:
                return error("challenge expired or already consumed", 401, request)
            locked_challenge.consumed_at = timezone.now()
            locked_challenge.save(update_fields=["consumed_at"])
            identity, _ = WalletIdentity.objects.get_or_create(chain=locked_challenge.chain, address=locked_challenge.address)
            identity.last_authenticated_at = timezone.now()
            identity.save(update_fields=["last_authenticated_at"])
            active_mode = requested_mode or ensure_user_bot_control(identity).wallet_mode
            control = ensure_user_bot_control(identity)
            if control.wallet_mode != active_mode:
                control.wallet_mode = active_mode
                control.save(update_fields=["wallet_mode", "updated_at"])
            bot_wallets = ensure_bot_wallets(identity, mode=active_mode)
            visible_bot_wallets = [item for item in bot_wallets if item.chain in accessible_bot_chains(identity)]
            bot_wallet = next(item for item in bot_wallets if item.chain == identity.chain)
        request.session.cycle_key()
        identity_ids = _session_identity_ids(request)
        identity_id = str(identity.id)
        identity_ids = [value for value in identity_ids if value != identity_id]
        identity_ids.append(identity_id)
        request.session["wallet_identity_ids"] = identity_ids[-8:]
        request.session["wallet_identity_id"] = str(identity.id)
        request.session["wallet_chain"] = identity.chain
        request.session["wallet_address"] = identity.address
        record_event("wallet.authenticate", actor_type="wallet", actor_id=identity.id, subject_type="wallet", subject_id=identity.id, request_id=request_id(request), metadata={"chain": identity.chain})
        return api_response({"authenticated": True, "wallet": {"id": str(identity.id), "chain": identity.chain, "address": identity.address}, "botWallet": bot_wallet_payload(bot_wallet), "botWallets": [bot_wallet_payload(item) for item in visible_bot_wallets]}, 200, request)
    except Exception as exc:
        auth_logger.exception("wallet auth exception request_id=%s error_type=%s", request_id(request), type(exc).__name__)
        return error("wallet signature verification failed", 401, request) if not settings.DEBUG else error(str(exc), 400, request)


@csrf_exempt
def auth_logout(request):
    if request.method != "POST":
        return error("method not allowed", 405, request)
    request.session.flush()
    return api_response({"authenticated": False}, 200, request)


@csrf_exempt
def auth_logout_identity(request):
    """Remove one chain identity without logging out other wallet ecosystems."""
    if request.method != "POST" or not post_allowed(request):
        return error("method or origin not allowed", 405 if request.method != "POST" else 403, request)
    limited = rate_limited(request, "auth-logout-identity", 10, 60)
    if limited:
        return limited
    try:
        data = body(request)
        if set(data) != {"chain", "address"}:
            return error("invalid wallet identity logout", 400, request)
        chain = str(data.get("chain") or "")
        address = canonical_wallet_address(chain, data.get("address"))
        if chain not in CHAINS or not wallet_address_valid(chain, address):
            return error("invalid wallet identity logout", 400, request)
        identity = next(
            (
                item for item in session_identities(request)
                if item.chain == chain and wallet_address_matches(chain, item.address, address)
            ),
            None,
        )
        if not identity:
            return error("wallet identity is not authenticated in this session", 404, request)
        identity_ids = [value for value in _session_identity_ids(request) if value != str(identity.id)]
        if identity_ids:
            request.session["wallet_identity_ids"] = identity_ids[-8:]
            remaining = session_identities(request)
            if remaining:
                active = remaining[0]
                request.session["wallet_identity_id"] = str(active.id)
                request.session["wallet_chain"] = active.chain
                request.session["wallet_address"] = active.address
        else:
            for key in ("wallet_identity_ids", "wallet_identity_id", "wallet_chain", "wallet_address"):
                request.session.pop(key, None)
        request.session.modified = True
        record_event("wallet.logout.identity", actor_type="wallet", actor_id=identity.id, subject_type="wallet", subject_id=identity.id, request_id=request_id(request), metadata={"chain": chain})
        return api_response({"authenticated": bool(identity_ids), "removed": {"chain": chain, "address": identity.address}}, 200, request)
    except (ValueError, json.JSONDecodeError):
        return error("invalid wallet identity logout", 400, request)


def auth_session(request):
    identities = session_identities(request)
    identity = identities[0] if identities else None
    wallet_rows = []
    visible_wallets = []
    for item in identities:
        active_mode = ensure_user_bot_control(item).wallet_mode
        wallets = ensure_bot_wallets(item, mode=active_mode)
        wallet_rows.append({"id": str(item.id), "chain": item.chain, "address": item.address})
        visible_wallets.extend(
            {**bot_wallet_payload(wallet), "walletIdentityId": str(item.id)}
            for wallet in wallets
            if wallet.chain in accessible_bot_chains(item)
        )
    current = next((item for item in visible_wallets if identity and item["chain"] == identity.chain and item["walletIdentityId"] == str(identity.id)), None)
    return api_response({
        "authenticated": bool(identities),
        "wallet": {"id": str(identity.id), "chain": identity.chain, "address": identity.address} if identity else None,
        "wallets": wallet_rows,
        "botWallet": current,
        "botWallets": visible_wallets,
    }, 200, request)


def bot_wallet_view(request):
    try:
        wallet_id = request.GET.get("walletId")
        identity = require_bot_wallet_session(request, wallet_id) if wallet_id else require_session(request, chain=request.GET.get("chain") or None)
    except PermissionError as exc:
        return error(str(exc), 401, request)
    summary = bot_wallet_summary(identity, wallet_id)
    if not wallet_id:
        # Present both signed ecosystems in one browser session. Selecting a
        # wallet from another identity causes the next request to resolve that
        # identity explicitly, so no owner boundary is weakened.
        combined = []
        for session_owner in session_identities(request):
            owner_summary = summary if session_owner.id == identity.id else bot_wallet_summary(session_owner)
            combined.extend({**wallet, "walletIdentityId": str(session_owner.id)} for wallet in owner_summary["wallets"])
        summary["wallets"] = combined
        summary["sessionWallets"] = [
            {"id": str(item.id), "chain": item.chain, "address": item.address}
            for item in session_identities(request)
        ]
    return api_response(summary, 200, request)


@csrf_exempt
def bot_wallet_provision(request):
    if request.method != "POST" or not post_allowed(request):
        return error("method or origin not allowed", 405 if request.method != "POST" else 403, request)
    limited = rate_limited(request, "bot-wallet-provision", 10, 60)
    if limited:
        return limited
    try:
        identity = require_session(request)
        data = body(request)
        mode = str(data.get("mode", "external-bot"))
        if mode not in BOT_MODES or set(data) - {"mode"}:
            return error("invalid bot wallet mode", 400, request)
        wallets = ensure_bot_wallets(identity, mode=mode)
        user_control = ensure_user_bot_control(identity)
        user_control.wallet_mode = mode
        user_control.save(update_fields=["wallet_mode", "updated_at"])
        current = next(item for item in wallets if item.chain == identity.chain)
        visible_wallets = [item for item in wallets if item.chain in accessible_bot_chains(identity)]
        return api_response({"wallet": bot_wallet_payload(current), "wallets": [bot_wallet_payload(item) for item in visible_wallets], "botControl": user_bot_status(identity), "message": "The bot wallet records exist, but real addresses require the audited signer/MPC provisioner. No private key is generated or stored by Coni."}, 200, request)
    except PermissionError as exc:
        return error(str(exc), 401, request)
    except (ValueError, json.JSONDecodeError) as exc:
        return error(str(exc), 400, request)


@csrf_exempt
def bot_wallet_transfer(request):
    if request.method != "POST" or not post_allowed(request):
        return error("method or origin not allowed", 405 if request.method != "POST" else 403, request)
    limited = rate_limited(request, "bot-wallet-transfer", 10, 60)
    if limited:
        return limited
    try:
        data = body(request)
        identity = require_bot_wallet_session(request, data.get("botWalletId"))
        amount = str(data.get("amountBaseUnits", ""))
        if not amount.isdigit() or len(amount) > MAX_BASE_UNIT_DIGITS or int(amount) <= 0 or not IDEMPOTENCY_RE.fullmatch(str(data.get("idempotencyKey", ""))):
            return error("invalid bot wallet transfer", 400, request)
        transfer = create_bot_transfer(identity=identity, wallet_id=data.get("botWalletId"), direction=str(data.get("direction", "")), asset=str(data.get("asset", "")).upper(), amount_base_units=amount, destination=str(data.get("destination", "")), idempotency_key=data["idempotencyKey"])
        return api_response({"transferId": str(transfer.id), "state": transfer.state, "direction": transfer.direction, "asset": transfer.asset, "chain": transfer.chain, "amountBaseUnits": str(transfer.amount_base_units), "feeAmountBaseUnits": str(transfer.fee_amount_base_units), "totalReservedBaseUnits": str(transfer.total_reserved_base_units or transfer.amount_base_units), "destination": transfer.destination, "message": "Transfer is awaiting signer approval; the configured fee is queued separately for the fee wallet. No transaction has been signed or broadcast."}, 201, request)
    except PermissionError as exc:
        return error(str(exc), 401, request)
    except ValueError as exc:
        return error(str(exc), 409, request)


@csrf_exempt
def signer_bot_wallet_attest(request):
    """Private callback used by the audited HSM/MPC provisioner.

    The callback binds a public address to a durable bot-wallet record. It
    deliberately accepts no private key, seed, calldata, or user-supplied
    custody material. Private networking/mTLS and an independent signer audit
    remain deployment requirements.
    """
    if request.method != "POST" or not signer_provision_allowed(request):
        return error("signer provisioning authorization required", 403, request)
    try:
        data = body(request)
        if set(data) - {"provisionRequestId", "botWalletId", "chain", "address", "signerKeyRef", "attestationHash"} or not data.get("provisionRequestId"):
            return error("unexpected signer provisioning fields", 400, request)
        if not data.get("botWalletId") or not isinstance(data.get("chain"), str) or data["chain"] not in CHAINS:
            return error("invalid bot wallet provisioning request", 400, request)
        address = canonical_wallet_address(data.get("chain"), str(data.get("address", "")))
        key_ref = str(data.get("signerKeyRef", ""))
        attestation = str(data.get("attestationHash", ""))
        if not wallet_address_valid(data["chain"], address) or not SIGNER_REF_RE.fullmatch(key_ref) or not HASH_RE.fullmatch(attestation):
            return error("invalid signer attestation", 400, request)
        with transaction.atomic():
            wallet = BotWallet.objects.select_for_update().filter(id=data["botWalletId"], chain=data["chain"]).first()
            if not wallet:
                return error("bot wallet not found", 404, request)
            provision = BotWalletProvisionRequest.objects.select_for_update().filter(id=data["provisionRequestId"], bot_wallet=wallet).first()
            if not provision or provision.status not in {"pending", "leased", "completed"}:
                return error("invalid bot wallet provision request", 409, request)
            if provision.status == "leased" and provision.lease_until and provision.lease_until < timezone.now():
                return error("bot wallet provision request lease expired", 409, request)
            if wallet.address and not addresses_match(data["chain"], wallet.address, address):
                return error("bot wallet address rotation requires a new audited record", 409, request)
            wallet.address = address
            wallet.signer_key_ref = key_ref
            wallet.custody = "audited-signer"
            wallet.status = "active"
            wallet.save(update_fields=["address", "signer_key_ref", "custody", "status", "updated_at"])
            provision.status = "completed"
            provision.lease_until = None
            provision.completed_at = timezone.now()
            provision.save(update_fields=["status", "lease_until", "completed_at", "updated_at"])
        record_event("bot_wallet.attested", actor_type="signer", actor_id=key_ref, subject_type="bot_wallet", subject_id=wallet.id, metadata={"chain": wallet.chain, "attestationHash": attestation})
        return api_response({"wallet": bot_wallet_payload(wallet), "state": "active"}, 200, request)
    except IntegrityError:
        return error("signer address is already bound to another bot wallet", 409, request)
    except (ValueError, json.JSONDecodeError) as exc:
        return error(str(exc), 400, request)


@csrf_exempt
def signer_bot_wallet_provision_requests(request):
    """Private pull endpoint for the audited signer/MPC provisioning adapter."""
    if request.method != "GET" or not signer_provision_allowed(request):
        return error("signer provisioning authorization required", 403, request)
    try:
        limit = min(max(int(request.GET.get("limit", "20")), 1), 100)
    except (TypeError, ValueError):
        return error("invalid provisioning request limit", 400, request)
    requests = claim_provision_requests(limit=limit)
    return api_response({
        "requests": [{
            "provisionRequestId": str(item.id),
            "botWalletId": str(item.bot_wallet_id),
            "ownerId": str(item.bot_wallet.owner_id),
            "chain": item.bot_wallet.chain,
            "mode": item.bot_wallet.mode,
            "sourceAddress": item.bot_wallet.source_address or None,
            "attempt": item.attempt_count,
            "leaseUntil": item.lease_until.isoformat() if item.lease_until else None,
        } for item in requests]
    }, 200, request)


@csrf_exempt
def signer_bot_transfer_event(request):
    """Private, idempotent callback for an already-approved bot transfer."""
    if request.method != "POST" or not signer_event_allowed(request):
        return error("signer event authorization required", 403, request)
    try:
        data = body(request)
        allowed = {"transferId", "event", "idempotencyKey", "txHash", "metadata"}
        if set(data) - allowed or not data.get("transferId") or data.get("event") not in {"submitted", "confirmed", "failed"} or not IDEMPOTENCY_RE.fullmatch(str(data.get("idempotencyKey", ""))):
            return error("invalid bot transfer event", 400, request)
        transfer = BotWalletTransfer.objects.select_related("bot_wallet").filter(id=data["transferId"]).first()
        if not transfer:
            return error("bot transfer not found", 404, request)
        tx_hash = str(data.get("txHash", ""))
        if tx_hash:
            if transfer.chain == "solana" and not SOL_TX_RE.fullmatch(tx_hash):
                return error("invalid Solana transaction signature", 400, request)
            if transfer.chain in EVM_CHAINS and not EVM_TX_RE.fullmatch(tx_hash):
                return error("invalid EVM transaction hash", 400, request)
        transfer = record_bot_transfer_event(transfer_id=transfer.id, event=data["event"], idempotency_key=data["idempotencyKey"], tx_hash=tx_hash, metadata=data.get("metadata"))
        return api_response({"transferId": str(transfer.id), "state": transfer.state, "txHash": transfer.tx_hash or None, "failureCode": transfer.failure_code or None}, 200, request)
    except BotWalletTransfer.DoesNotExist:
        return error("bot transfer not found", 404, request)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return error(str(exc), 409, request)


@csrf_exempt
def signer_bot_transfer_intents(request):
    """Private leased queue for the custody adapter's bot withdrawals."""
    if request.method != "GET" or not signer_event_allowed(request):
        return error("signer event authorization required", 403, request)
    try:
        limit = min(max(int(request.GET.get("limit", "20")), 1), 100)
    except (TypeError, ValueError):
        return error("invalid transfer intent limit", 400, request)
    transfers = claim_bot_transfer_requests(limit=limit)
    return api_response({
        "transfers": [{
            "transferId": str(item.id),
            "botWalletId": str(item.bot_wallet_id),
            "ownerId": str(item.owner_id),
            "chain": item.chain,
            "asset": item.asset,
            "direction": item.direction,
            "amountBaseUnits": str(item.amount_base_units),
            "feeAmountBaseUnits": str(item.fee_amount_base_units),
            "totalReservedBaseUnits": str(item.total_reserved_base_units or item.amount_base_units),
            "destination": item.destination,
            "signerKeyRef": item.bot_wallet.signer_key_ref or None,
            "attempt": item.attempt_count,
            "leaseUntil": item.lease_until.isoformat() if item.lease_until else None,
        } for item in transfers]
    }, 200, request)


@csrf_exempt
def signer_fee_intents(request):
    """Private leased queue for fee-wallet transfers."""
    if request.method != "GET" or not signer_event_allowed(request):
        return error("signer event authorization required", 403, request)
    try:
        limit = min(max(int(request.GET.get("limit", "20")), 1), 100)
    except (TypeError, ValueError):
        return error("invalid fee intent limit", 400, request)
    fees = claim_fee_events(limit=limit)
    return api_response({"fees": [fee_intent_payload(item) for item in fees]}, 200, request)


@csrf_exempt
def signer_fee_event(request):
    """Private proof callback for a fee-wallet transfer."""
    if request.method != "POST" or not signer_event_allowed(request):
        return error("signer event authorization required", 403, request)
    try:
        data = body(request)
        allowed = {"feeId", "event", "idempotencyKey", "txHash", "metadata"}
        if set(data) - allowed or not data.get("feeId") or data.get("event") not in {"submitted", "confirmed", "failed"} or not IDEMPOTENCY_RE.fullmatch(str(data.get("idempotencyKey", ""))):
            return error("invalid fee event", 400, request)
        fee = FeeEvent.objects.filter(id=data["feeId"]).first()
        if not fee:
            return error("fee event not found", 404, request)
        tx_hash = str(data.get("txHash", ""))
        if tx_hash:
            if fee.chain == "solana" and not SOL_TX_RE.fullmatch(tx_hash):
                return error("invalid Solana fee transaction signature", 400, request)
            if fee.chain in EVM_CHAINS and not EVM_TX_RE.fullmatch(tx_hash):
                return error("invalid EVM fee transaction hash", 400, request)
        fee = record_fee_event(fee_id=fee.id, event=data["event"], idempotency_key=data["idempotencyKey"], tx_hash=tx_hash, metadata=data.get("metadata"))
        return api_response({"feeId": str(fee.id), "state": fee.status, "txHash": fee.tx_hash or None}, 200, request)
    except FeeEvent.DoesNotExist:
        return error("fee event not found", 404, request)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return error(str(exc), 409, request)


@csrf_exempt
def signer_profit_settlement_event(request):
    if request.method != "POST" or not signer_event_allowed(request):
        return error("signer event authorization required", 403, request)
    try:
        data = body(request)
        allowed = {"settlementId", "event", "idempotencyKey", "txHash", "metadata"}
        if set(data) - allowed or not data.get("settlementId") or data.get("event") not in {"submitted", "confirmed", "failed"} or not IDEMPOTENCY_RE.fullmatch(str(data.get("idempotencyKey", ""))):
            return error("invalid profit settlement event", 400, request)
        settlement = ProfitSettlement.objects.filter(id=data["settlementId"]).first()
        if not settlement:
            return error("profit settlement not found", 404, request)
        tx_hash = str(data.get("txHash", ""))
        if tx_hash and not EVM_TX_RE.fullmatch(tx_hash):
            return error("invalid settlement transaction hash", 400, request)
        settlement = record_usdg_settlement_event(settlement_id=settlement.id, event=data["event"], idempotency_key=data["idempotencyKey"], tx_hash=tx_hash, metadata=data.get("metadata"))
        return api_response({"settlementId": str(settlement.id), "state": settlement.state, "txHash": settlement.tx_hash or None, "settledAt": settlement.settled_at.isoformat() if settlement.settled_at else None}, 200, request)
    except ProfitSettlement.DoesNotExist:
        return error("profit settlement not found", 404, request)
    except (ValueError, TypeError, InvalidOperation, json.JSONDecodeError) as exc:
        return error(str(exc), 409, request)


@csrf_exempt
def signer_profit_compound_event(request):
    """Private callback proving that retained profit reached the bot wallet."""
    if request.method != "POST" or not signer_event_allowed(request):
        return error("signer event authorization required", 403, request)
    try:
        data = body(request)
        allowed = {"allocationId", "event", "idempotencyKey", "txHash", "metadata"}
        if (
            set(data) - allowed
            or not data.get("allocationId")
            or data.get("event") not in {"submitted", "confirmed", "failed"}
            or not IDEMPOTENCY_RE.fullmatch(str(data.get("idempotencyKey", "")))
        ):
            return error("invalid profit compound event", 400, request)
        allocation = ProfitAllocation.objects.select_related("execution__sell_bot_wallet").filter(id=data["allocationId"]).first()
        if not allocation:
            return error("profit allocation not found", 404, request)
        chain = allocation.execution.sell_bot_wallet.chain if allocation.execution.sell_bot_wallet else ""
        tx_hash = str(data.get("txHash", ""))
        if tx_hash:
            if chain == "solana" and not SOL_TX_RE.fullmatch(tx_hash):
                return error("invalid Solana compound transaction signature", 400, request)
            if chain in EVM_CHAINS and not EVM_TX_RE.fullmatch(tx_hash):
                return error("invalid EVM compound transaction hash", 400, request)
        allocation = record_compound_event(
            allocation_id=allocation.id,
            event=data["event"],
            idempotency_key=data["idempotencyKey"],
            tx_hash=tx_hash,
            metadata=data.get("metadata"),
        )
        return api_response({"allocationId": str(allocation.id), "state": allocation.compound_state, "txHash": allocation.compound_tx_hash or None, "confirmedAt": allocation.confirmed_at.isoformat() if allocation.confirmed_at else None}, 200, request)
    except ProfitAllocation.DoesNotExist:
        return error("profit allocation not found", 404, request)
    except (ValueError, TypeError, InvalidOperation, json.JSONDecodeError) as exc:
        return error(str(exc), 409, request)


@csrf_exempt
def signer_profit_compound_intent(request):
    """Private pull endpoint for a bounded retained-profit transfer intent."""
    if request.method != "POST" or not signer_event_allowed(request):
        return error("signer event authorization required", 403, request)
    try:
        data = body(request)
        allowed = {"allocationId", "idempotencyKey"}
        if set(data) - allowed or not data.get("allocationId") or not IDEMPOTENCY_RE.fullmatch(str(data.get("idempotencyKey", ""))):
            return error("invalid profit compound intent request", 400, request)
        allocation = ProfitAllocation.objects.filter(id=data["allocationId"]).first()
        if not allocation:
            return error("profit allocation not found", 404, request)
        return api_response({"intent": compound_intent_payload(allocation)}, 200, request)
    except ProfitAllocation.DoesNotExist:
        return error("profit allocation not found", 404, request)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return error(str(exc), 409, request)


def pangea_proxy(request, path, query=None, method="GET"):
    limited = rate_limited(request, f"pangea:{method}:{path}", 60 if method == "GET" else 20, 60)
    if limited:
        return limited
    try:
        return api_response(request_json(path + (("?" + query) if query else ""), method=method, payload=body(request) if method == "POST" else None), 200, request)
    except (PangeaError, ValueError, json.JSONDecodeError) as exc:
        return error(str(exc), 502, request)


def market_pools(request):
    return pangea_proxy(request, "/pools")


def market_tokens(request):
    limited = rate_limited(request, "pangea:market-tokens", 60, 60)
    if limited:
        return limited
    try:
        return api_response({"readAt": timezone.now().isoformat(), "tokens": token_registry_snapshot()}, 200, request)
    except PangeaError as exc:
        return error(str(exc), 502, request)


def market_motion(request):
    """Read-only optional Motion discovery; never an execution authority."""
    try:
        chain = request.GET.get("chain") or None
        limit = int(request.GET.get("limit", "20"))
        if chain and chain not in {"robinhood", "bsc", "solana"}:
            return error("invalid Motion chain", 400, request)
        return api_response(motion_discoveries(chain=chain, limit=limit), 200, request)
    except (MotionError, ValueError, json.JSONDecodeError) as exc:
        return error(str(exc), 502 if isinstance(exc, MotionError) else 400, request)


def market_meta(request):
    limited = rate_limited(request, "pangea:token-meta", 60, 60)
    if limited:
        return limited
    launch = request.GET.get("launch", "")
    if not SLUG_RE.fullmatch(launch):
        return error("invalid launch", 400, request)
    try:
        payload = request_json(f"/token/meta?launch={launch}")
        if isinstance(payload, dict) and payload.get("image"):
            cache.set(f"coni:pangea:token-image:{launch}", payload["image"], 3600)
        return api_response(payload, 200, request)
    except (PangeaError, ValueError, json.JSONDecodeError) as exc:
        return error(str(exc), 502, request)


def market_history(request):
    launch = request.GET.get("launch", "")
    try:
        hours = int(request.GET.get("hours", "24"))
    except ValueError:
        return error("invalid history query", 400, request)
    if (launch and not SLUG_RE.fullmatch(launch)) or not 1 <= hours <= 168:
        return error("invalid history query", 400, request)
    query = f"hours={hours}" + (f"&launch={launch}" if launch else "")
    return pangea_proxy(request, "/history", query)


def bridge_activity(request):
    token = request.GET.get("token", "")
    try:
        hours, limit = int(request.GET.get("hours", "24")), int(request.GET.get("limit", "25"))
    except ValueError:
        return error("invalid bridge activity query", 400, request)
    if (token and not SLUG_RE.fullmatch(token)) or not 1 <= hours <= 168 or not 1 <= limit <= 100:
        return error("invalid bridge activity query", 400, request)
    query = f"hours={hours}&limit={limit}" + (f"&token={token}" if token else "")
    return pangea_proxy(request, "/bridge/activity", query)


def market_launches(request):
    try:
        limit = int(request.GET.get("limit", "50"))
    except ValueError:
        return error("invalid launches query", 400, request)
    return pangea_proxy(request, "/v1/launches", f"limit={limit}") if 1 <= limit <= 100 else error("invalid launches query", 400, request)


@csrf_exempt
def quote_proxy(request, kind):
    allowed = {"trade": {"slug", "side", "chain", "amount", "fromChain", "user", "solanaUser", "recipient", "slippageBps"}, "bridge": {"slug", "from", "to", "amount", "user", "solanaUser", "recipient", "slippageBps"}}[kind]
    try:
        payload = body(request)
        if set(payload) - allowed or not SLUG_RE.fullmatch(payload.get("slug", "")) or not valid_amount(str(payload.get("amount", ""))):
            return error(f"invalid {kind} quote", 400, request)
        if kind == "trade" and (payload.get("side") not in {"buy", "sell"} or payload.get("chain") not in CHAINS | {"auto"}):
            return error("invalid trade quote", 400, request)
        if kind == "bridge" and (payload.get("from") not in CHAINS or payload.get("to") not in CHAINS or payload["from"] == payload["to"]):
            return error("invalid bridge quote", 400, request)
        if kind == "bridge":
            route_summary(payload["from"], payload["to"])
        return pangea_proxy(request, "/trade/quote" if kind == "trade" else "/bridge/quote", method="POST")
    except (ValueError, json.JSONDecodeError):
        return error("invalid JSON", 400, request)


def _manual_wallet(request, chain):
    try:
        return require_session(request, chain=chain)
    except PermissionError:
        raise


def _manual_payload(request, allowed):
    payload = body(request)
    if set(payload) - allowed:
        raise ValueError("unexpected manual trade fields")
    return payload


@csrf_exempt
def manual_arbitrage_prepare(request):
    """Return fresh, read-only buy/bridge/sell quotes for a user-selected route."""
    if request.method != "POST" or not post_allowed(request):
        return error("method or origin not allowed", 405 if request.method != "POST" else 403, request)
    try:
        data = _manual_payload(request, {"slug", "buyChain", "sellChain", "amount", "buyWallet", "sellWallet", "slippageBps"})
        slug, buy_chain, sell_chain = str(data.get("slug", "")), str(data.get("buyChain", "")), str(data.get("sellChain", ""))
        if not SLUG_RE.fullmatch(slug) or buy_chain not in CHAINS or sell_chain not in CHAINS or buy_chain == sell_chain or not valid_amount(str(data.get("amount", ""))):
            return error("invalid manual arbitrage route", 400, request)
        wallet = str(data.get("buyWallet", ""))
        _manual_wallet(request, buy_chain)
        if not wallet_address_valid(buy_chain, wallet):
            return error("invalid source wallet", 400, request)
        sell_wallet = str(data.get("sellWallet", "")) or wallet
        if not wallet_address_valid(sell_chain, sell_wallet):
            return error("invalid destination wallet", 400, request)
        slippage = int(data.get("slippageBps", 100))
        if slippage < 1 or slippage > 1000:
            return error("invalid slippage", 400, request)
        amount = str(data["amount"])
        buy = request_json("/trade/quote", "POST", {"slug": slug, "side": "buy", "chain": buy_chain, "amount": amount, "user": wallet, "slippageBps": slippage})
        bridge = request_json("/bridge/quote", "POST", {"slug": slug, "from": buy_chain, "to": sell_chain, "amount": amount, "user": wallet, "recipient": sell_wallet, "slippageBps": slippage})
        sell = request_json("/trade/quote", "POST", {"slug": slug, "side": "sell", "chain": sell_chain, "amount": amount, "user": sell_wallet, "slippageBps": slippage})
        return api_response({"mode": "manual", "slug": slug, "buyChain": buy_chain, "sellChain": sell_chain, "wallet": wallet, "recipient": sell_wallet, "quotes": {"buy": buy, "bridge": bridge, "sell": sell}, "note": "Quotes are indicative until the wallet-specific unsigned transaction is built."}, 200, request)
    except PermissionError:
        return error("authenticated wallet session required", 401, request)
    except (ValueError, json.JSONDecodeError):
        return error("invalid manual arbitrage request", 400, request)
    except PangeaError as exc:
        return error(str(exc), 502, request)


@csrf_exempt
def manual_solana_swap(request):
    if request.method != "POST" or not post_allowed(request):
        return error("method or origin not allowed", 405 if request.method != "POST" else 403, request)
    try:
        data = _manual_payload(request, {"instructions", "addressLookupTableAddresses", "payer"})
        payer = str(data.get("payer", ""))
        _manual_wallet(request, "solana")
        if not wallet_address_valid("solana", payer) or not isinstance(data.get("instructions"), list):
            return error("invalid Solana swap transaction request", 400, request)
        if len(data["instructions"]) < 1 or len(data["instructions"]) > 24:
            return error("invalid Solana instruction count", 400, request)
        return api_response(request_json("/trade/solana-tx", "POST", data), 200, request)
    except PermissionError:
        return error("authenticated Solana wallet session required", 401, request)
    except (ValueError, json.JSONDecodeError):
        return error("invalid Solana swap transaction request", 400, request)
    except PangeaError as exc:
        return error(str(exc), 502, request)


@csrf_exempt
def manual_solana_bridge(request):
    if request.method != "POST" or not post_allowed(request):
        return error("method or origin not allowed", 405 if request.method != "POST" else 403, request)
    try:
        data = _manual_payload(request, {"slug", "to", "amount", "sender", "recipient"})
        sender, recipient, to = str(data.get("sender", "")), str(data.get("recipient", "")), str(data.get("to", ""))
        _manual_wallet(request, "solana")
        if not SLUG_RE.fullmatch(str(data.get("slug", ""))) or to not in EVM_CHAINS or not wallet_address_valid("solana", sender) or not wallet_address_valid(to, recipient) or not valid_amount(str(data.get("amount", ""))):
            return error("invalid Solana bridge transaction request", 400, request)
        return api_response(request_json("/bridge/solana-tx", "POST", data), 200, request)
    except PermissionError:
        return error("authenticated Solana wallet session required", 401, request)
    except (ValueError, json.JSONDecodeError):
        return error("invalid Solana bridge transaction request", 400, request)
    except PangeaError as exc:
        return error(str(exc), 502, request)


@csrf_exempt
def manual_evm_swap(request):
    """Return a Pangea EVM quote; the wallet signs the returned transaction client-side."""
    if request.method != "POST" or not post_allowed(request):
        return error("method or origin not allowed", 405 if request.method != "POST" else 403, request)
    try:
        data = _manual_payload(request, {"slug", "side", "chain", "amount", "user", "recipient", "slippageBps"})
        chain = str(data.get("chain", ""))
        _manual_wallet(request, chain)
        if chain not in EVM_CHAINS or data.get("side") not in {"buy", "sell"} or not SLUG_RE.fullmatch(str(data.get("slug", ""))) or not wallet_address_valid(chain, str(data.get("user", ""))) or not valid_amount(str(data.get("amount", ""))):
            return error("invalid EVM swap transaction request", 400, request)
        return api_response(request_json("/trade/quote", "POST", data), 200, request)
    except PermissionError:
        return error("authenticated EVM wallet session required", 401, request)
    except (ValueError, json.JSONDecodeError):
        return error("invalid EVM swap transaction request", 400, request)
    except PangeaError as exc:
        return error(str(exc), 502, request)


@csrf_exempt
def manual_arbitrage_status(request):
    if request.method != "POST" or not post_allowed(request):
        return error("method or origin not allowed", 405 if request.method != "POST" else 403, request)
    try:
        data = _manual_payload(request, {"txHash", "chain", "bridgeTxHash"})
        chain, tx_hash = str(data.get("chain", "")), str(data.get("txHash", ""))
        _manual_wallet(request, chain)
        if chain not in CHAINS or not tx_hash or len(tx_hash) > 200:
            return error("invalid transaction status request", 400, request)
        result = {"transaction": request_json("/chain/tx", "POST", {"chain": chain, "hash": tx_hash})}
        if data.get("bridgeTxHash"):
            result["bridge"] = request_json("/bridge/arrival", "POST", {"txHash": str(data["bridgeTxHash"])})
        return api_response(result, 200, request)
    except PermissionError:
        return error("authenticated wallet session required", 401, request)
    except (ValueError, json.JSONDecodeError):
        return error("invalid transaction status request", 400, request)
    except PangeaError as exc:
        return error(str(exc), 502, request)


@csrf_exempt
def funding_intent(request):
    if request.method != "POST" or not post_allowed(request):
        return error("method or origin not allowed", 405 if request.method != "POST" else 403, request)
    limited = rate_limited(request, "funding-intent", 10, 60)
    if limited:
        return limited
    try:
        data = body(request)
        asset, amount, wallet = str(data.get("asset", "")).upper(), str(data.get("amount", "")), data.get("wallet")
        requested_chain = str(data.get("chain") or "")
        identity = require_bot_wallet_session(request, data.get("botWalletId")) if data.get("botWalletId") else require_session(request, chain=requested_chain or None)
        chain = requested_chain or identity.chain
        if not chain_accessible(identity, chain):
            return error("authenticated wallet cannot fund this chain", 403, request)
        bot_wallet = None
        if data.get("botWalletId"):
            bot_wallet = BotWallet.objects.filter(id=data.get("botWalletId"), owner=identity, chain=chain, status="active", signer_key_ref__gt="").first()
            if not bot_wallet or not bot_wallet.address:
                return error("the selected bot wallet is not provisioned by the audited signer", 409, request)
            if bot_wallet.mode != ensure_user_bot_control(identity).wallet_mode:
                return error("selected bot wallet does not match the active custody mode", 403, request)
        decimals = asset_decimals(asset, chain)
        if not supported_asset(asset, chain) or not browser_funding_supported(asset, chain) or decimals is None or not wallet_address_valid(chain, wallet) or not addresses_match(chain, wallet, identity.address):
            return error("invalid funding request", 400, request)
        if ((asset in ASSET_DECIMALS and asset != native_asset(chain)) or (asset not in ASSET_DECIMALS and not supported_asset(asset, chain))):
            return error("funding asset does not match connected chain", 400, request)
        amount_base = decimal_units(amount, decimals)
        destination = bot_wallet.address if bot_wallet else recipient_for(asset, chain)
        if not amount_base or not wallet_address_valid(chain, destination):
            return error("funding destination or amount is not configured", 503, request)
        fee_amount, fee_bps = calculate_fee_amount(amount_base, "funding")
        if fee_amount > 0 and not fee_recipient_for(asset, chain):
            return error("funding fee recipient is not configured for this asset and chain", 503, request)
        net_amount = Decimal(str(amount_base)) - fee_amount
        if net_amount <= 0:
            return error("funding amount is smaller than the configured fee", 400, request)
        expires = timezone.now() + timedelta(minutes=5)
        intent = FundingIntent.objects.create(wallet_identity=identity, bot_wallet=bot_wallet, asset=asset, chain=chain, amount_base_units=amount_base, fee_amount_base_units=fee_amount, net_amount_base_units=net_amount, destination=destination, status="awaiting-wallet-approval", expires_at=expires)
        record_event("funding.intent.created", actor_type="wallet", actor_id=identity.id, subject_type="funding_intent", subject_id=intent.id, request_id=request_id(request), metadata={"asset": asset, "chain": chain, "amountBaseUnits": amount_base, "feeAmountBaseUnits": str(fee_amount), "netAmountBaseUnits": str(net_amount), "feeBps": fee_bps})
        configured_token = token_config(asset, chain) or {}
        return api_response({"intentId": str(intent.id), "state": intent.status, "asset": asset, "chain": chain, "amount": amount, "amountBaseUnits": amount_base, "feeAmountBaseUnits": str(fee_amount), "netAmountBaseUnits": str(net_amount), "feeBps": fee_bps, "decimals": decimals, "tokenAddress": configured_token.get("address") if asset != native_asset(chain) else None, "tokenProgram": configured_token.get("programId") if asset != native_asset(chain) else None, "destination": destination, "botWalletId": str(bot_wallet.id) if bot_wallet else None, "expiresAt": expires.isoformat()}, 201, request)
    except PermissionError as exc:
        return error(str(exc), 401, request)
    except (ValueError, json.JSONDecodeError) as exc:
        return error(str(exc), 400, request)


def funding_assets(request):
    """Return only server-configured assets that the verifier can validate."""
    try:
        requested_chain = str(request.GET.get("chain") or "")
        identity = require_session(request, chain=requested_chain or None)
        chain = requested_chain or identity.chain
        if chain not in CHAINS:
            return error("unsupported funding chain", 400, request)
        if not chain_accessible(identity, chain):
            return error("authenticated wallet cannot fund this chain", 403, request)
        assets = []
        native = native_asset(chain)
        if native:
            assets.append({"asset": native, "decimals": asset_decimals(native, chain), "native": True, "tokenAddress": None, "tokenProgram": None})
        for asset in token_registry().keys():
            configured = token_config(asset, chain)
            if not configured or not configured.get("address"):
                continue
            if not browser_funding_supported(asset, chain):
                continue
            decimals = asset_decimals(asset, chain)
            if decimals is None:
                continue
            assets.append({"asset": str(asset).upper(), "decimals": decimals, "native": False, "tokenAddress": configured["address"], "tokenProgram": configured.get("programId")})
        return api_response({"chain": chain, "assets": assets}, 200, request)
    except PermissionError as exc:
        return error(str(exc), 401, request)


@csrf_exempt
def funding_claim(request):
    if request.method != "POST" or not post_allowed(request):
        return error("method or origin not allowed", 405 if request.method != "POST" else 403, request)
    limited = rate_limited(request, "funding-claim", 20, 60)
    if limited:
        return limited
    try:
        data = body(request)
        identities = session_identities(request)
        if not identities:
            raise PermissionError("authenticated wallet session required")
        intent = FundingIntent.objects.select_related("wallet_identity").filter(id=data.get("intentId"), wallet_identity__in=identities).first()
        identity = intent.wallet_identity if intent else None
        if not identity:
            return error("funding intent not found", 404, request)
        tx_hash = str(data.get("txHash", ""))
        try:
            event_index = int(data.get("eventIndex", 0))
        except (TypeError, ValueError):
            event_index = -1
        if not intent or intent.expires_at <= timezone.now() or intent.status in {"credited", "cancelled"}:
            return error("funding intent is unavailable", 409, request)
        if event_index < 0 or event_index > 10_000:
            return error("invalid event index", 400, request)
        if intent.chain in EVM_CHAINS and not EVM_TX_RE.fullmatch(tx_hash):
            return error("invalid EVM transaction hash", 400, request)
        if intent.chain == "solana" and not SOL_TX_RE.fullmatch(tx_hash):
            return error("invalid Solana transaction signature", 400, request)
        event_hash_key = tx_hash.lower() if intent.chain in EVM_CHAINS else tx_hash
        event_key = f"{intent.chain}:{event_hash_key}:{event_index}"
        observation, created = DepositObservation.objects.get_or_create(
            event_key=event_key,
            defaults={
                "funding_intent": intent,
                "wallet_identity": identity,
                "asset": intent.asset,
                "chain": intent.chain,
                "sender": identity.address,
                "destination": intent.destination,
                "tx_hash": tx_hash,
                "event_index": event_index,
                "amount_base_units": intent.amount_base_units,
                "status": "pending-verification",
            },
        )
        if observation.wallet_identity_id != identity.id:
            if observation.wallet_identity_id is not None or observation.asset != intent.asset or observation.chain != intent.chain or not addresses_match(observation.chain, observation.destination, intent.destination) or not addresses_match(observation.chain, observation.sender, identity.address) or int(observation.amount_base_units) != int(intent.amount_base_units):
                return error("transaction is already claimed by another wallet or does not match the intent", 409, request)
            observation.wallet_identity = identity
            observation.funding_intent = intent
            observation.status = "pending-verification"
            observation.rejection_code = None
            observation.save(update_fields=["wallet_identity", "funding_intent", "status", "rejection_code"])
        if created and intent.status == "awaiting-wallet-approval":
            intent.status = "pending-verification"
            intent.save(update_fields=["status"])
        record_event("funding.claim.submitted", actor_type="wallet", actor_id=identity.id, subject_type="deposit_observation", subject_id=observation.id, request_id=request_id(request), metadata={"chain": observation.chain, "asset": observation.asset, "txHash": observation.tx_hash, "eventIndex": observation.event_index})
        return api_response({
            "observationId": str(observation.id),
            "intentId": str(intent.id),
            "state": observation.status,
            "txHash": observation.tx_hash,
            "message": "The verifier must confirm the on-chain transaction before the ledger is credited.",
        }, 202 if created else 200, request)
    except PermissionError as exc:
        return error(str(exc), 401, request)
    except (ValueError, json.JSONDecodeError) as exc:
        return error(str(exc), 400, request)


def funding_status(request, intent_id):
    identities = session_identities(request)
    if not identities:
        return error("authenticated wallet session required", 401, request)
    intent = FundingIntent.objects.filter(id=intent_id, wallet_identity__in=identities).first()
    if not intent:
        return error("funding intent not found", 404, request)
    observations = DepositObservation.objects.filter(funding_intent=intent).order_by("-observed_at")
    return api_response({
        "intentId": str(intent.id),
        "state": intent.status,
        "asset": intent.asset,
        "chain": intent.chain,
        "amountBaseUnits": str(intent.amount_base_units),
        "expiresAt": intent.expires_at.isoformat(),
        "observations": [{
            "id": str(item.id),
            "state": item.status,
            "txHash": item.tx_hash,
            "confirmations": item.confirmations,
            "rejectionCode": item.rejection_code,
            "observedAt": item.observed_at.isoformat(),
            "confirmedAt": item.confirmed_at.isoformat() if item.confirmed_at else None,
            "creditedAt": item.credited_at.isoformat() if item.credited_at else None,
        } for item in observations],
    }, 200, request)


def unclaimed_deposits(request):
    identities = session_identities(request)
    if not identities:
        return error("authenticated wallet session required", 401, request)
    active_wallets = []
    for identity in identities:
        control = ensure_user_bot_control(identity)
        active_wallets.extend(BotWallet.objects.filter(
            owner=identity,
            chain__in=accessible_bot_chains(identity),
            mode=control.wallet_mode,
            status="active",
            signer_key_ref__gt="",
        ).exclude(address=""))
    from django.db.models import Q
    destination_filter = Q()
    for wallet in active_wallets:
        destination_filter |= Q(
            chain=wallet.chain,
            **({"destination": wallet.address} if wallet.chain == "solana" else {"destination__iexact": wallet.address}),
        )
    rows = DepositObservation.objects.filter(
        wallet_identity__in=identities,
            status="unclaimed",
            funding_intent__isnull=True,
        ).filter(destination_filter).order_by("-observed_at")[:50] if destination_filter else DepositObservation.objects.none()
    return api_response({"deposits": [{
        "id": str(item.id),
        "asset": item.asset,
        "chain": item.chain,
        "sender": item.sender,
        "destination": item.destination,
        "txHash": item.tx_hash,
        "eventIndex": item.event_index,
        "amountBaseUnits": str(item.amount_base_units),
        "status": item.status,
        "observedAt": item.observed_at.isoformat(),
    } for item in rows]}, 200, request)


@csrf_exempt
def claim_unclaimed_deposit(request):
    if request.method != "POST" or not post_allowed(request):
        return error("method or origin not allowed", 405 if request.method != "POST" else 403, request)
    try:
        identities = session_identities(request)
        if not identities:
            raise PermissionError("authenticated wallet session required")
        data = body(request)
        with transaction.atomic():
            observation = DepositObservation.objects.select_for_update().filter(
                id=data.get("observationId"),
                wallet_identity__in=identities,
                status="unclaimed",
                funding_intent__isnull=True,
            ).select_related("wallet_identity").first()
            if not observation:
                return error("unclaimed deposit not found", 404, request)
            identity = observation.wallet_identity
            if observation_is_stale(observation):
                observation.status = "rejected"
                observation.rejection_code = "deposit-too-old"
                observation.save(update_fields=["status", "rejection_code"])
                return error("unclaimed deposit is outside the configured verification age window", 409, request)
            bot_wallet_filters = {
                "owner": identity,
                "chain": observation.chain,
                "status": "active",
            }
            bot_wallet_filters["signer_key_ref__gt"] = ""
            bot_wallet_filters["address" if observation.chain == "solana" else "address__iexact"] = observation.destination
            bot_wallet = BotWallet.objects.filter(**bot_wallet_filters).first()
            if not bot_wallet or bot_wallet.mode != ensure_user_bot_control(identity).wallet_mode or not supported_asset(observation.asset, observation.chain) or asset_decimals(observation.asset, observation.chain) is None:
                return error("deposit destination is not an active supported bot wallet", 409, request)
            fee_amount, fee_bps = calculate_fee_amount(observation.amount_base_units, "funding")
            if fee_amount > 0 and not fee_recipient_for(observation.asset, observation.chain):
                return error("funding fee recipient is not configured for this asset and chain", 503, request)
            net_amount = Decimal(str(observation.amount_base_units)) - fee_amount
            if net_amount <= 0:
                return error("funding amount is smaller than the configured fee", 409, request)
            intent = FundingIntent.objects.create(
                wallet_identity=identity,
                bot_wallet=bot_wallet,
                asset=observation.asset,
                chain=observation.chain,
                amount_base_units=observation.amount_base_units,
                fee_amount_base_units=fee_amount,
                net_amount_base_units=net_amount,
                destination=observation.destination,
                status="pending-verification",
                expires_at=timezone.now() + timedelta(minutes=30),
            )
            observation.funding_intent = intent
            observation.status = "pending-verification"
            observation.rejection_code = None
            observation.save(update_fields=["funding_intent", "status", "rejection_code"])
        record_event("funding.unclaimed.claimed", actor_type="wallet", actor_id=identity.id, subject_type="deposit_observation", subject_id=observation.id, request_id=request_id(request), metadata={"intentId": str(intent.id), "asset": observation.asset, "chain": observation.chain, "amountBaseUnits": str(observation.amount_base_units), "feeAmountBaseUnits": str(intent.fee_amount_base_units), "netAmountBaseUnits": str(intent.net_amount_base_units), "feeBps": fee_bps})
        return api_response({"observationId": str(observation.id), "intentId": str(intent.id), "state": observation.status, "feeAmountBaseUnits": str(intent.fee_amount_base_units), "netAmountBaseUnits": str(intent.net_amount_base_units), "feeBps": fee_bps, "message": "The deposit is queued for exact transaction and finality verification."}, 202, request)
    except PermissionError as exc:
        return error(str(exc), 401, request)
    except (ValueError, json.JSONDecodeError) as exc:
        return error(str(exc), 400, request)


@csrf_exempt
def withdrawal_request(request):
    if request.method != "POST" or not post_allowed(request):
        return error("method or origin not allowed", 405 if request.method != "POST" else 403, request)
    limited = rate_limited(request, "withdrawal-request", 10, 60)
    if limited:
        return limited
    try:
        data = body(request)
        requested_chain = str(data.get("chain") or "")
        identity = require_session(request, chain=requested_chain or None)
        chain = requested_chain or identity.chain
        asset = str(data.get("asset", "")).upper()
        amount = str(data.get("amount", ""))
        destination = data.get("destination")
        idempotency_key = str(data.get("idempotencyKey", ""))
        decimals = asset_decimals(asset, chain)
        if not supported_asset(asset, chain) or decimals is None or not wallet_address_valid(chain, destination) or not IDEMPOTENCY_RE.fullmatch(idempotency_key):
            return error("invalid withdrawal request", 400, request)
        if ((asset in ASSET_DECIMALS and asset != native_asset(chain)) or (asset not in ASSET_DECIMALS and not supported_asset(asset, chain))):
            return error("withdrawal asset does not match connected chain", 400, request)
        amount_base = decimal_units(amount, decimals)
        if not amount_base:
            return error("invalid withdrawal amount", 400, request)
        fee_amount, fee_bps = calculate_fee_amount(amount_base, "withdrawal")
        if fee_amount > 0 and not fee_recipient_for(asset, chain):
            return error("withdrawal fee recipient is not configured for this asset and chain", 503, request)
        total_reserved = Decimal(str(amount_base)) + fee_amount
        existing = WithdrawalRequest.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            if existing.wallet_identity_id != identity.id:
                return error("idempotency key is already in use", 409, request)
            return api_response({"withdrawalId": str(existing.id), "state": existing.status, "nonce": existing.nonce, "amountBaseUnits": str(existing.amount_base_units), "feeAmountBaseUnits": str(existing.fee_amount_base_units), "totalReservedBaseUnits": str(existing.total_reserved_base_units or existing.amount_base_units)}, 200, request)
        with transaction.atomic():
            withdrawal = WithdrawalRequest.objects.create(
                idempotency_key=idempotency_key,
                wallet_identity=identity,
                asset=asset,
                chain=chain,
                amount_base_units=amount_base,
                fee_amount_base_units=fee_amount,
                total_reserved_base_units=total_reserved,
                destination=destination,
                status="pending-approval",
                nonce=secrets.token_hex(24),
                expires_at=timezone.now() + timedelta(minutes=15),
            )
            reserve_withdrawal(withdrawal.id)
            accrue_fee(kind="withdrawal", source_id=str(withdrawal.id), asset=asset, chain=chain, gross_amount_base_units=amount_base, user_identity=identity)
        record_event("withdrawal.requested", actor_type="wallet", actor_id=identity.id, subject_type="withdrawal", subject_id=withdrawal.id, request_id=request_id(request), metadata={"asset": asset, "chain": chain, "amountBaseUnits": amount_base, "feeAmountBaseUnits": str(fee_amount), "totalReservedBaseUnits": str(total_reserved), "feeBps": fee_bps, "destination": destination})
        return api_response({
            "withdrawalId": str(withdrawal.id),
            "state": withdrawal.status,
            "asset": withdrawal.asset,
            "chain": withdrawal.chain,
            "amountBaseUnits": str(withdrawal.amount_base_units),
            "feeAmountBaseUnits": str(withdrawal.fee_amount_base_units),
            "totalReservedBaseUnits": str(withdrawal.total_reserved_base_units),
            "feeBps": fee_bps,
            "destination": withdrawal.destination,
            "nonce": withdrawal.nonce,
            "message": "Withdrawal is reserved for review. The requested amount is sent to your wallet and the configured fee is queued separately for the fee wallet. No transaction has been signed or broadcast.",
        }, 201, request)
    except PermissionError as exc:
        return error(str(exc), 401, request)
    except InsufficientBalance as exc:
        return error(str(exc), 409, request)
    except IntegrityError:
        return error("idempotency key is already in use", 409, request)
    except (ValueError, json.JSONDecodeError) as exc:
        return error(str(exc), 400, request)


def withdrawal_status(request, withdrawal_id):
    identities = session_identities(request)
    if not identities:
        return error("authenticated wallet session required", 401, request)
    withdrawal = WithdrawalRequest.objects.filter(id=withdrawal_id, wallet_identity__in=identities).first()
    if not withdrawal:
        return error("withdrawal not found", 404, request)
    return api_response({
        "withdrawalId": str(withdrawal.id),
        "state": withdrawal.status,
        "asset": withdrawal.asset,
        "chain": withdrawal.chain,
        "amountBaseUnits": str(withdrawal.amount_base_units),
        "feeAmountBaseUnits": str(withdrawal.fee_amount_base_units),
        "totalReservedBaseUnits": str(withdrawal.total_reserved_base_units or withdrawal.amount_base_units),
        "destination": withdrawal.destination,
        "nonce": withdrawal.nonce,
        "txHash": withdrawal.tx_hash,
        "failureCode": withdrawal.failure_code,
        "requestedAt": withdrawal.requested_at.isoformat(),
        "expiresAt": withdrawal.expires_at.isoformat(),
        "attempt": withdrawal.attempt_count,
    }, 200, request)


@csrf_exempt
def approve_withdrawal(request, withdrawal_id):
    """Manual operator approval; never auto-approve user withdrawals."""
    if request.method != "POST" or not post_allowed(request):
        return error("method or origin not allowed", 405 if request.method != "POST" else 403, request)
    if not settings.CONI_WITHDRAWAL_APPROVALS_ENABLED or not ops_allowed(request):
        return error("withdrawal approval is disabled or unauthorized", 403, request)
    approver = request.headers.get("X-Coni-Approval-Actor", "")
    if not re.fullmatch(r"[A-Za-z0-9._:/-]{3,128}", approver):
        return error("a named approval actor is required", 400, request)
    try:
        with transaction.atomic():
            withdrawal = WithdrawalRequest.objects.select_for_update().get(id=withdrawal_id)
            if withdrawal.status != "pending-approval":
                return error("withdrawal is not awaiting approval", 409, request)
            if withdrawal.expires_at <= timezone.now():
                return error("withdrawal approval window has expired", 409, request)
            withdrawal.status = "approved"
            withdrawal.approved_at = timezone.now()
            withdrawal.save(update_fields=["status", "approved_at"])
        record_event(
            "withdrawal.approved",
            actor_type="operator",
            actor_id=approver,
            subject_type="withdrawal",
            subject_id=withdrawal.id,
            request_id=request_id(request),
            metadata={"approvalPolicy": "manual-operator-approval"},
        )
        return api_response({"withdrawalId": str(withdrawal.id), "state": withdrawal.status}, 200, request)
    except WithdrawalRequest.DoesNotExist:
        return error("withdrawal not found", 404, request)


def _signer_withdrawal_key_refs():
    try:
        configured = json.loads(getattr(settings, "CONI_SIGNER_WITHDRAWAL_KEY_REFS", "{}") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(configured, dict):
        return {}
    return {
        str(chain): str(ref)
        for chain, ref in configured.items()
        if str(chain) in CHAINS and isinstance(ref, str) and SIGNER_REF_RE.fullmatch(ref)
    }


@csrf_exempt
def signer_withdrawal_intents(request):
    """Private leased queue for manually approved user withdrawals."""
    if request.method != "GET" or not signer_event_allowed(request):
        return error("signer event authorization required", 403, request)
    try:
        limit = min(max(int(request.GET.get("limit", "20")), 1), 100)
    except (TypeError, ValueError):
        return error("invalid withdrawal intent limit", 400, request)
    key_refs = _signer_withdrawal_key_refs()
    withdrawals = claim_withdrawal_requests(limit=limit, chains=tuple(key_refs)) if key_refs else []
    return api_response({
        "withdrawals": [{
            "withdrawalId": str(item.id),
            "ownerId": str(item.wallet_identity_id),
            "chain": item.chain,
            "asset": item.asset,
            "amountBaseUnits": str(item.amount_base_units),
            "feeAmountBaseUnits": str(item.fee_amount_base_units),
            "totalReservedBaseUnits": str(item.total_reserved_base_units or item.amount_base_units),
            "destination": item.destination,
            "nonce": item.nonce,
            "signerKeyRef": key_refs[item.chain],
            "attempt": item.attempt_count,
            "leaseUntil": item.lease_until.isoformat() if item.lease_until else None,
        } for item in withdrawals]
    }, 200, request)


@csrf_exempt
def signer_withdrawal_event(request):
    """Private proof-bound callback for normal custody withdrawals."""
    if request.method != "POST" or not signer_event_allowed(request):
        return error("signer event authorization required", 403, request)
    try:
        data = body(request)
        allowed = {"withdrawalId", "event", "idempotencyKey", "txHash", "metadata"}
        if set(data) - allowed or not data.get("withdrawalId") or data.get("event") not in {"submitted", "confirmed", "failed"} or not IDEMPOTENCY_RE.fullmatch(str(data.get("idempotencyKey", ""))):
            return error("invalid withdrawal event", 400, request)
        withdrawal = WithdrawalRequest.objects.filter(id=data["withdrawalId"]).first()
        if not withdrawal:
            return error("withdrawal not found", 404, request)
        tx_hash = str(data.get("txHash", ""))
        if tx_hash:
            pattern = SOL_TX_RE if withdrawal.chain == "solana" else EVM_TX_RE
            if not pattern.fullmatch(tx_hash):
                return error("invalid withdrawal transaction proof", 400, request)
        withdrawal = record_withdrawal_event(
            withdrawal_id=withdrawal.id,
            event=data["event"],
            idempotency_key=data["idempotencyKey"],
            tx_hash=tx_hash,
            metadata=data.get("metadata"),
        )
        return api_response({"withdrawalId": str(withdrawal.id), "state": withdrawal.status, "txHash": withdrawal.tx_hash or None, "failureCode": withdrawal.failure_code}, 200, request)
    except WithdrawalRequest.DoesNotExist:
        return error("withdrawal not found", 404, request)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return error(str(exc), 409, request)


def bot_status_view(request):
    return api_response(bot_status(), 200, request)


@csrf_exempt
def user_bot_config(request):
    if request.method not in {"GET", "POST"} or (request.method == "POST" and not post_allowed(request)):
        return error("method or origin not allowed", 405 if request.method not in {"GET", "POST"} else 403, request)
    if request.method == "POST":
        limited = rate_limited(request, "user-bot-control", 20, 60)
        if limited:
            return limited
    try:
        identity = require_session(request)
        control = ensure_user_bot_control(identity)
        if request.method == "POST":
            data = body(request)
            allowed = {"enabled", "paused", "walletMode", "minNetBps", "maxNotionalUsd", "compoundProfitBps"}
            if set(data) - allowed:
                return error("unexpected bot-control fields", 400, request)
            global_control = global_bot_control()
            if "enabled" in data and not isinstance(data["enabled"], bool):
                return error("enabled must be boolean", 400, request)
            if "paused" in data and not isinstance(data["paused"], bool):
                return error("paused must be boolean", 400, request)
            if "walletMode" in data and data["walletMode"] not in BOT_MODES:
                return error("invalid wallet mode", 400, request)
            if "minNetBps" in data:
                value = int(data["minNetBps"])
                if value < global_control.min_net_bps or value > 10_000:
                    return error("minimum edge can only be as strict as the global policy", 400, request)
                control.min_net_bps = value
            if "maxNotionalUsd" in data:
                value = Decimal(str(data["maxNotionalUsd"]))
                if value <= 0 or value > global_control.max_notional_usd:
                    return error("maximum notional can only be as strict as the global policy", 400, request)
                control.max_notional_usd = value
            if "compoundProfitBps" in data:
                value = int(data["compoundProfitBps"])
                if value < 0 or value > 10_000 or value > global_control.compound_profit_bps:
                    return error("compound percentage must be between 0 and the global policy", 400, request)
                control.compound_profit_bps = value
            if "enabled" in data:
                control.enabled = data["enabled"]
            if "paused" in data:
                control.paused = data["paused"]
            if "walletMode" in data:
                control.wallet_mode = data["walletMode"]
            control.save()
            # Keep custody-mode changes self-contained.  A user can switch
            # from the connected-wallet bot to the Coni-managed bot from the
            # Settings page; create the durable per-chain records immediately
            # so the worker and wallet page see the same mode without relying
            # on a later read request to self-heal the session.
            if "walletMode" in data:
                ensure_bot_wallets(identity, mode=control.wallet_mode)
        return api_response(user_bot_status(identity), 200, request)
    except PermissionError as exc:
        return error(str(exc), 401, request)
    except (ValueError, InvalidOperation, json.JSONDecodeError):
        return error("invalid bot-control policy", 400, request)


@csrf_exempt
def bot_config(request):
    if request.method != "POST" or not ops_allowed(request):
        return error("operations authorization required", 403, request)
    data = body(request)
    try:
        min_bps, max_usd = int(data["minNetBps"]), Decimal(str(data["maxNotionalUsd"]))
        compound_bps = int(data.get("compoundProfitBps", 7000))
        if not isinstance(data.get("enabled"), bool) or min_bps < 1 or min_bps > 10_000 or max_usd <= 0 or max_usd > 1_000_000 or compound_bps < 0 or compound_bps > 10_000:
            raise ValueError
        control = global_bot_control()
        control.enabled, control.paused, control.min_net_bps, control.max_notional_usd, control.compound_profit_bps = data["enabled"], data.get("paused", True), min_bps, max_usd, compound_bps
        control.save()
        return api_response(bot_status(), 200, request)
    except (KeyError, InvalidOperation, ValueError):
        return error("invalid bot configuration", 400, request)


@csrf_exempt
def bot_pause(request):
    if request.method != "POST" or not ops_allowed(request):
        return error("operations authorization required", 403, request)
    data = body(request)
    if not isinstance(data.get("paused"), bool):
        return error("paused must be boolean", 400, request)
    control = global_bot_control()
    control.paused = data["paused"]
    control.save(update_fields=["paused", "updated_at"])
    return api_response(bot_status(), 200, request)


@csrf_exempt
def bot_scan(request):
    if not ops_allowed(request):
        return error("operations authorization required", 403, request)
    try:
        return api_response({"asOf": timezone.now().isoformat(), "status": bot_status(), "opportunities": scan_opportunities(), "note": "Discovery only. No transaction was signed or broadcast."}, 200, request)
    except PangeaError as exc:
        return error(str(exc), 502, request)


def opportunities(request):
    try:
        limit = int(request.GET.get("limit", "100"))
    except ValueError:
        return error("invalid opportunities query", 400, request)
    if not 1 <= limit <= 100:
        return error("invalid opportunities query", 400, request)
    slug = request.GET.get("slug", "")
    buy_chain = request.GET.get("buyChain", "")
    sell_chain = request.GET.get("sellChain", "")
    if slug and not SLUG_RE.fullmatch(slug):
        return error("invalid opportunities query", 400, request)
    if buy_chain and buy_chain not in CHAINS or sell_chain and sell_chain not in CHAINS:
        return error("invalid opportunities query", 400, request)
    filters = {}
    if slug:
        filters["slug"] = slug
    if buy_chain:
        filters["buy_chain"] = buy_chain
    if sell_chain:
        filters["sell_chain"] = sell_chain
    rows = ArbitrageOpportunity.objects.filter(**filters).order_by("-detected_at")[:limit]

    def public_decision(decision):
        """Expose discovery facts without returning signer-bound quote data."""
        if not isinstance(decision, dict):
            return {}
        allowed = {
            "executable", "quoteState", "reasons", "netBps", "netProfitUsd",
            "buyAmountBaseUnits", "sellAmountBaseUnits", "tokenOutBaseUnits",
            "bridgeRoute",
        }
        safe = {key: decision[key] for key in allowed if key in decision}
        route = safe.get("bridgeRoute")
        if isinstance(route, dict):
            safe["bridgeRoute"] = {
                key: route[key]
                for key in ("id", "amountOut", "nativeFee", "tokenFee", "setupFee", "seconds", "state")
                if key in route
            }
        return safe

    return api_response({
        "opportunities": [{
            "id": str(item.id),
            "slug": item.slug,
            "symbol": item.symbol,
            "buyChain": item.buy_chain,
            "sellChain": item.sell_chain,
            "buyToken": item.buy_token,
            "sellToken": item.sell_token,
            "notionalUsd": str(item.notional_usd),
            "netProfitUsd": str(item.net_profit_usd),
            "netBps": item.net_bps,
            "state": item.state,
            "quoteHash": item.quote_hash,
            "decisionSummary": public_decision(item.decision),
            "detectedAt": item.detected_at.isoformat(),
            "expiresAt": item.expires_at.isoformat() if item.expires_at else None,
        } for item in rows]}, 200, request)


def fees_config(request):
    if not ops_allowed(request):
        return error("operations authorization required", 403, request)
    recipients = parse_json_setting(settings.CONI_FEE_RECIPIENTS)
    return api_response({"tradingFeeBps": settings.CONI_TRADING_FEE_BPS, "fundingFeeBps": settings.CONI_BOT_FUNDING_FEE_BPS, "withdrawalFeeBps": settings.CONI_WITHDRAWAL_FEE_BPS, "botWithdrawalFeeBps": settings.CONI_BOT_WITHDRAWAL_FEE_BPS, "recipientAssets": list(recipients), "note": "10 bps equals 0.1%; fees are queued as signer-gated transfers to the configured chain-specific recipient."}, 200, request)


@csrf_exempt
def fees_accrue(request):
    if request.method != "POST" or not ops_allowed(request):
        return error("operations authorization required", 403, request)
    try:
        data = body(request)
        asset, chain = str(data.get("asset", "")).upper(), str(data.get("chain") or "")
        kind = str(data.get("kind") or "trade")
        source_id = str(data.get("sourceId") or data.get("executionId") or f"manual-{uuid.uuid4()}")
        gross = str(data.get("grossAmountBaseUnits") or data.get("amountBaseUnits") or "")
        if chain not in CHAINS or not valid_amount(gross):
            return error("invalid fee accrual", 400, request)
        event = accrue_fee(kind=kind, source_id=source_id, asset=asset, chain=chain, gross_amount_base_units=gross, execution_id=data.get("executionId"))
        if not event:
            return api_response({"status": "not-charged", "asset": asset, "chain": chain, "grossAmountBaseUnits": gross, "feeBps": calculate_fee_amount(gross, kind)[1]}, 200, request)
        return api_response({"id": str(event.id), "status": event.status, "asset": event.asset, "chain": event.chain, "grossAmountBaseUnits": str(event.gross_amount_base_units), "amountBaseUnits": str(event.amount_base_units), "recipient": event.recipient, "feeBps": event.fee_bps}, 201, request)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return error(str(exc), 400, request)


def profit_summary(request):
    if not ops_allowed(request):
        return error("operations authorization required", 403, request)
    settled = Execution.objects.filter(state="settled")
    expected = sum((item.expected_profit_usd or Decimal("0") for item in settled), Decimal("0"))
    realized = sum((item.realized_profit_usd or Decimal("0") for item in settled), Decimal("0"))
    settlements = ProfitSettlement.objects.filter(execution__in=settled)
    allocations = ProfitAllocation.objects.filter(execution__in=settled)
    compound = sum((item.compound_profit_usd for item in allocations), Decimal("0"))
    payout = sum((item.payout_profit_usd for item in allocations), Decimal("0"))
    return api_response({"executions": settled.count(), "expectedProfitUsd": str(expected), "realizedProfitUsd": str(realized), "compounding": {"allocatedProfitUsd": str(compound), "payoutProfitUsd": str(payout), "pendingReinvestment": allocations.filter(compound_state="pending-approval").count(), "confirmedReinvestment": allocations.filter(compound_state="confirmed").count()}, "settlements": {"total": settlements.count(), "pending": settlements.exclude(state__in={"confirmed", "failed"}).count(), "confirmed": settlements.filter(state="confirmed").count()}, "settlement": {"asset": settings.CONI_PROFIT_SETTLEMENT_ASSET, "chain": settings.CONI_PROFIT_SETTLEMENT_CHAIN, "enabled": settings.CONI_PROFIT_SETTLEMENT_ENABLED}, "note": "Confirmed profit is split by policy: the compound portion remains signer-gated working capital and the payout portion is eligible for USDG settlement."}, 200, request)


@csrf_exempt
def prepare_profit_settlement(request):
    if request.method != "POST" or not ops_allowed(request):
        return error("operations authorization required", 403, request)
    data = body(request)
    try:
        settlement = prepare_usdg_settlement(
            execution_id=data["executionId"],
            user_identity_id=data["userIdentityId"],
            realized_profit_usd=data["realizedProfitUsd"],
            usdg_price_usd=data["usdgPriceUsd"],
            recipient=data["recipient"],
        )
        return api_response({"id": str(settlement.id), "state": settlement.state, "asset": settlement.settlement_asset, "chain": settlement.settlement_chain, "amountBaseUnits": str(settlement.amount_base_units), "recipient": settlement.recipient}, 201, request)
    except (KeyError, ValueError, InvalidOperation) as exc:
        return error(str(exc), 409, request)


@csrf_exempt
def execution_plan(request):
    if request.method != "POST" or not post_allowed(request):
        return error("method or origin not allowed", 405 if request.method != "POST" else 403, request)
    try:
        data = body(request)
        opportunity_id = data.get("opportunityId")
        if not opportunity_id:
            return error("invalid execution plan", 400, request)
        try:
            opportunity = ArbitrageOpportunity.objects.get(id=opportunity_id)
        except (ArbitrageOpportunity.DoesNotExist, ValueError, TypeError):
            return error("opportunity not found", 404, request)
        identity = require_session(request, chain=opportunity.buy_chain)
        status = bot_status()
        if not status["executionReady"] or status["paused"] or not status["enabled"]:
            return api_response({"error": "execution is gated", "status": status, "next": "Configure Postgres, the signer, fee recipients, risk policy, and explicit live approval."}, 409, request)
        user_policy = user_bot_status(identity)
        if not user_policy["enabled"] or user_policy["paused"]:
            return api_response({"error": "this wallet's bot is paused", "status": {**status, "userBot": user_policy}}, 409, request)
        bot_wallet = BotWallet.objects.filter(owner=identity, chain=opportunity.buy_chain, mode=user_policy["walletMode"], status="active", signer_key_ref__gt="").first()
        sell_wallet = BotWallet.objects.filter(owner=identity, chain=opportunity.sell_chain, mode=user_policy["walletMode"], status="active", signer_key_ref__gt="").first()
        if not bot_wallet or not bot_wallet.address or not sell_wallet or not sell_wallet.address:
            return api_response({"error": "user bot wallets are not provisioned on both route chains", "status": status, "botWallet": bot_wallet_payload(bot_wallet) if bot_wallet else None, "sellBotWallet": bot_wallet_payload(sell_wallet) if sell_wallet else None, "next": "Provision active audited signer/MPC bot wallets on the buy and sell chains before execution."}, 409, request)
        try:
            fresh = fresh_user_quotes(opportunity, buy_wallet=bot_wallet.address, sell_wallet=sell_wallet.address)
        except PangeaError as exc:
            return api_response({"error": "fresh opportunity quote rejected", "reason": str(exc), "status": status}, 409, request)
        if fresh.get("netBps", 0) < user_policy["minNetBps"] or Decimal(str(fresh.get("notionalUsd", "0"))) > Decimal(str(user_policy["maxNotionalUsd"])):
            return api_response({"error": "route does not meet this wallet's policy", "status": {**status, "userBot": user_policy}}, 409, request)
        execution = create_execution_from_opportunity(identity=identity, opportunity_id=opportunity.id, bot_wallet=bot_wallet, sell_bot_wallet=sell_wallet, decision=fresh, quote_hash=fresh["quoteHash"])
        return api_response({"execution": execution_payload(execution), "next": "The signer adapter must independently validate and sign the exact Pangea transaction steps."}, 202, request)
    except PermissionError as exc:
        return error(str(exc), 401, request)
    except (ValueError, json.JSONDecodeError) as exc:
        return error(str(exc), 400, request)


def execution_history(request):
    identities = session_identities(request)
    if not identities:
        return error("authenticated wallet session required", 401, request)
    executions = Execution.objects.filter(user_identity__in=identities).order_by("-created_at")[:50]
    return api_response({"executions": [execution_payload(item) for item in executions]}, 200, request)


def user_activity(request):
    """Return one authenticated user's complete bot activity stream.

    This endpoint deliberately joins only identities already authenticated in
    the current wallet session.  It is the read model for the Activity page;
    operations-only profit reporting remains separate in ``/api/bot/profit``.
    """
    identities = session_identities(request)
    if not identities:
        return error("authenticated wallet session required", 401, request)

    identity_ids = [item.id for item in identities]
    execution_queryset = Execution.objects.filter(user_identity_id__in=identity_ids)
    executions = list(execution_queryset.order_by("-created_at")[:100])
    transfers = list(
        BotWalletTransfer.objects.filter(owner_id__in=identity_ids)
        .order_by("-created_at")[:100]
    )
    fees = list(
        FeeEvent.objects.filter(user_identity_id__in=identity_ids)
        .order_by("-created_at")[:100]
    )
    allocations = ProfitAllocation.objects.filter(execution__user_identity_id__in=identity_ids)
    settlements = ProfitSettlement.objects.filter(execution__user_identity_id__in=identity_ids)
    settled_count = execution_queryset.filter(state="settled").count()
    expected = execution_queryset.aggregate(total=Sum("expected_profit_usd"))["total"] or Decimal("0")
    realized = execution_queryset.filter(state="settled").aggregate(total=Sum("realized_profit_usd"))["total"] or Decimal("0")

    recent = [
        {
            "kind": "trade",
            "id": str(item.id),
            "slug": item.slug,
            "state": item.state,
            "expectedProfitUsd": str(item.expected_profit_usd or 0),
            "realizedProfitUsd": str(item.realized_profit_usd or 0),
            "createdAt": item.created_at.isoformat(),
        }
        for item in executions
    ] + [
        {
            "kind": item.direction,
            "id": str(item.id),
            "asset": item.asset,
            "chain": item.chain,
            "state": item.state,
            "amountBaseUnits": str(item.amount_base_units),
            "feeAmountBaseUnits": str(item.fee_amount_base_units),
            "totalReservedBaseUnits": str(item.total_reserved_base_units or item.amount_base_units),
            "txHash": item.tx_hash or None,
            "createdAt": item.created_at.isoformat(),
        }
        for item in transfers
    ]
    recent += [
        {
            "kind": "fee",
            "id": str(item.id),
            "feeKind": item.kind,
            "asset": item.asset,
            "chain": item.chain,
            "amountBaseUnits": str(item.amount_base_units),
            "grossAmountBaseUnits": str(item.gross_amount_base_units),
            "feeBps": item.fee_bps,
            "recipient": item.recipient,
            "state": item.status,
            "txHash": item.tx_hash or None,
            "createdAt": item.created_at.isoformat(),
        }
        for item in fees
    ]
    recent.sort(key=lambda item: item["createdAt"], reverse=True)

    return api_response({
        "identities": [
            {"id": str(item.id), "chain": item.chain, "address": item.address}
            for item in identities
        ],
        "wallets": [
            {
                **bot_wallet_payload(item),
                "walletIdentityId": str(item.owner_id),
            }
            for item in BotWallet.objects.filter(owner_id__in=identity_ids).order_by("chain")
        ],
        "executions": [execution_payload(item) for item in executions],
        "recent": recent[:100],
        "fees": [{
            "id": str(item.id),
            "kind": item.kind,
            "asset": item.asset,
            "chain": item.chain,
            "amountBaseUnits": str(item.amount_base_units),
            "grossAmountBaseUnits": str(item.gross_amount_base_units),
            "feeBps": item.fee_bps,
            "recipient": item.recipient,
            "state": item.status,
            "txHash": item.tx_hash or None,
            "createdAt": item.created_at.isoformat(),
        } for item in fees],
        "pnl": {
            "expectedUsd": str(expected),
            "realizedUsd": str(realized),
            "settledTrades": settled_count,
        },
        "compounding": {
            "allocatedProfitUsd": str(sum((item.compound_profit_usd for item in allocations), Decimal("0"))),
            "payoutProfitUsd": str(sum((item.payout_profit_usd for item in allocations), Decimal("0"))),
            "pendingReinvestment": allocations.filter(compound_state="pending-approval").count(),
            "confirmedReinvestment": allocations.filter(compound_state="confirmed").count(),
        },
        "settlements": {
            "total": settlements.count(),
            "pending": settlements.exclude(state__in={"confirmed", "failed"}).count(),
            "confirmed": settlements.filter(state="confirmed").count(),
        },
    }, 200, request)


def execution_detail(request, execution_id):
    identities = session_identities(request)
    if not identities:
        return error("authenticated wallet session required", 401, request)
    execution = Execution.objects.filter(id=execution_id, user_identity__in=identities).first()
    if not execution:
        return error("execution not found", 404, request)
    return api_response({"execution": execution_payload(execution)}, 200, request)


@csrf_exempt
def signer_execution_event(request):
    if request.method != "POST" or not signer_event_allowed(request):
        return error("signer event authorization required", 403, request)
    try:
        data = body(request)
        allowed = {"executionId", "event", "idempotencyKey", "txHash", "bridgeGuid", "realizedProfitUsd", "metadata"}
        if set(data) - allowed or not data.get("executionId") or not isinstance(data.get("event"), str) or not IDEMPOTENCY_RE.fullmatch(str(data.get("idempotencyKey", ""))):
            return error("invalid signer execution event", 400, request)
        tx_hash, bridge_guid = str(data.get("txHash", "")), str(data.get("bridgeGuid", ""))
        if tx_hash and len(tx_hash) > 160 or bridge_guid and len(bridge_guid) > 128:
            return error("invalid signer execution event reference", 400, request)
        execution = record_execution_event(execution_id=data["executionId"], event=data["event"], idempotency_key=data["idempotencyKey"], tx_hash=tx_hash, bridge_guid=bridge_guid, realized_profit_usd=data.get("realizedProfitUsd"), metadata=data.get("metadata"))
        return api_response({"execution": execution_payload(execution)}, 200, request)
    except Execution.DoesNotExist:
        return error("execution not found", 404, request)
    except (ValueError, TypeError, InvalidOperation, json.JSONDecodeError) as exc:
        return error(str(exc), 409, request)


@csrf_exempt
def signer_execution_intent(request):
    """Private signer pull endpoint for one exact, quote-bound execution."""
    if request.method != "POST" or not signer_event_allowed(request):
        return error("signer event authorization required", 403, request)
    try:
        data = body(request)
        allowed = {"executionId", "idempotencyKey"}
        if set(data) - allowed or not data.get("executionId") or not IDEMPOTENCY_RE.fullmatch(str(data.get("idempotencyKey", ""))):
            return error("invalid signer intent request", 400, request)
        execution = Execution.objects.select_related("bot_wallet", "sell_bot_wallet", "opportunity").filter(id=data["executionId"]).first()
        if not execution:
            return error("execution not found", 404, request)
        # Validate the quote and both custody wallets before recording the
        # signer-requested transition. A stale or revoked plan must remain
        # untouched for reconciliation rather than becoming handoff-ready.
        signer_intent_payload(execution)
        if execution.state == "planned":
            execution = record_execution_event(
                execution_id=execution.id,
                event="signer-requested",
                idempotency_key=data["idempotencyKey"],
                metadata={"source": "signer-intent-pull"},
            )
        return api_response({"intent": signer_intent_payload(execution)}, 200, request)
    except Execution.DoesNotExist:
        return error("execution not found", 404, request)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return error(str(exc), 409, request)


def frontend(request, filename="index.html"):
    allowed = {"index.html", "wallet.html", "activity.html", "settings.html", "app.js", "wallet.js", "config.js", "styles.css", "theme.css", "ui.js"}
    if filename.startswith("assets/"):
        file_path = settings.BASE_DIR / filename
    elif filename in allowed:
        file_path = settings.BASE_DIR / filename
    else:
        return error("not found", 404, request)
    if not file_path.is_file() or not str(file_path.resolve()).startswith(str(settings.BASE_DIR.resolve())):
        return error("not found", 404, request)
    return FileResponse(open(file_path, "rb"))
