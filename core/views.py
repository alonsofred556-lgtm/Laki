import base64
import json
import re
import secrets
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation

import base58
from django.conf import settings
from django.db import connection, transaction
from django.http import FileResponse, HttpRequest, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from eth_account import Account
from eth_account.messages import encode_defunct
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

from .layerzero import route_summary
from .models import BotControl, Execution, FeeEvent, FundingIntent, WalletChallenge, WalletIdentity
from .pangea import PangeaError, request_json, scan_opportunities

EVM_CHAINS = {"ethereum", "base", "robinhood", "bsc"}
CHAINS = EVM_CHAINS | {"solana"}
ASSET_DECIMALS = {"ETH": 18, "BNB": 18, "SOL": 9}
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,80}$", re.I)
EVM_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
SOL_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def api_response(data, status=200, request=None, extra=None):
    response = JsonResponse(data, status=status)
    origin = request.headers.get("Origin") if request else None
    if origin in settings.CONI_ALLOWED_ORIGINS:
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
    return not origin or origin in settings.CONI_ALLOWED_ORIGINS


def ops_allowed(request):
    configured = settings.CONI_OPS_TOKEN
    return bool(configured and secrets.compare_digest(request.headers.get("X-Coni-Ops-Token", ""), configured))


def wallet_address_valid(chain, address):
    if not isinstance(address, str):
        return False
    if chain in EVM_CHAINS:
        return bool(EVM_RE.fullmatch(address)) and address.lower() != "0x" + "0" * 40
    return bool(SOL_RE.fullmatch(address)) and address != "1" * 32


def valid_amount(value):
    return isinstance(value, str) and re.fullmatch(r"\d{1,78}", value or "") and int(value) > 0


def decimal_units(value, decimals):
    if not isinstance(value, str) or not re.fullmatch(r"\d+(\.\d+)?", value):
        return None
    whole, _, fraction = value.partition(".")
    if len(fraction) > decimals:
        return None
    try:
        result = int(f"{whole}{fraction.ljust(decimals, '0')}")
    except ValueError:
        return None
    return str(result) if result > 0 else None


def session_identity(request):
    identity_id = request.session.get("wallet_identity_id")
    if not identity_id:
        return None
    return WalletIdentity.objects.filter(id=identity_id).first()


def require_session(request):
    identity = session_identity(request)
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
    return settings.CONI_EXECUTION_MODE == "live" and bool(settings.CONI_SIGNER_URL) and settings.CONI_LIVE_TRADING_CONFIRMATION == "I_UNDERSTAND_LIVE_RISK"


def bot_status():
    control, _ = BotControl.objects.get_or_create(scope="global")
    recipients = parse_json_setting(settings.CONI_FEE_RECIPIENTS)
    return {"mode": settings.CONI_EXECUTION_MODE, "executionReady": execution_ready(), "database": True, "enabled": control.enabled, "paused": control.paused, "minNetBps": control.min_net_bps, "maxNotionalUsd": float(control.max_notional_usd), "signerConfigured": bool(settings.CONI_SIGNER_URL), "feeConfigConfigured": bool(recipients)}


def health(request):
    database = False
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            database = cursor.fetchone()[0] == 1
    except Exception:
        pass
    return api_response({"ok": True, "service": "coni-django", "database": database, "time": timezone.now().isoformat()}, 200, request)


@csrf_exempt
def auth_challenge(request):
    if request.method != "POST" or not post_allowed(request):
        return error("method or origin not allowed", 405 if request.method != "POST" else 403, request)
    try:
        data = body(request)
        chain, address = data.get("chain"), data.get("address")
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
        recovered = Account.recover_message(encode_defunct(text=challenge.message), signature=signature)
        return recovered.lower() == challenge.address.lower()
    try:
        public_key = base58.b58decode(challenge.address)
        signed = base64.b64decode(signature, validate=True)
        VerifyKey(public_key).verify(challenge.message.encode("utf-8"), signed)
        return True
    except (ValueError, BadSignatureError):
        return False


@csrf_exempt
def auth_verify(request):
    if request.method != "POST" or not post_allowed(request):
        return error("method or origin not allowed", 405 if request.method != "POST" else 403, request)
    try:
        data = body(request)
        challenge = WalletChallenge.objects.filter(id=data.get("challengeId"), consumed_at__isnull=True, expires_at__gt=timezone.now()).first()
        if not challenge or data.get("address", "").lower() != challenge.address.lower() or not isinstance(data.get("signature"), str):
            return error("challenge expired or invalid", 401, request)
        if not verify_signature(challenge, data["signature"]):
            return error("wallet signature verification failed", 401, request)
        with transaction.atomic():
            challenge.consumed_at = timezone.now()
            challenge.save(update_fields=["consumed_at"])
            identity, _ = WalletIdentity.objects.get_or_create(chain=challenge.chain, address=challenge.address)
            identity.last_authenticated_at = timezone.now()
            identity.save(update_fields=["last_authenticated_at"])
        request.session.cycle_key()
        request.session["wallet_identity_id"] = str(identity.id)
        request.session["wallet_chain"] = identity.chain
        request.session["wallet_address"] = identity.address
        return api_response({"authenticated": True, "wallet": {"id": str(identity.id), "chain": identity.chain, "address": identity.address}}, 200, request)
    except Exception as exc:
        return error("wallet signature verification failed", 401, request) if not settings.DEBUG else error(str(exc), 400, request)


@csrf_exempt
def auth_logout(request):
    if request.method != "POST":
        return error("method not allowed", 405, request)
    request.session.flush()
    return api_response({"authenticated": False}, 200, request)


def auth_session(request):
    identity = session_identity(request)
    return api_response({"authenticated": bool(identity), "wallet": {"id": str(identity.id), "chain": identity.chain, "address": identity.address} if identity else None}, 200, request)


def pangea_proxy(request, path, query=None, method="GET"):
    try:
        return api_response(request_json(path + (("?" + query) if query else ""), method=method, payload=body(request) if method == "POST" else None), 200, request)
    except (PangeaError, ValueError, json.JSONDecodeError) as exc:
        return error(str(exc), 502, request)


def market_pools(request):
    return pangea_proxy(request, "/pools")


def market_meta(request):
    launch = request.GET.get("launch", "")
    return pangea_proxy(request, "/token/meta", f"launch={launch}") if SLUG_RE.fullmatch(launch) else error("invalid launch", 400, request)


def market_history(request):
    launch = request.GET.get("launch", "")
    try:
        hours = int(request.GET.get("hours", "24"))
    except ValueError:
        return error("invalid history query", 400, request)
    if not SLUG_RE.fullmatch(launch) or not 1 <= hours <= 168:
        return error("invalid history query", 400, request)
    return pangea_proxy(request, "/history", f"launch={launch}&hours={hours}")


def bridge_activity(request):
    token = request.GET.get("token", "")
    try:
        hours, limit = int(request.GET.get("hours", "24")), int(request.GET.get("limit", "25"))
    except ValueError:
        return error("invalid bridge activity query", 400, request)
    if not SLUG_RE.fullmatch(token) or not 1 <= hours <= 168 or not 1 <= limit <= 100:
        return error("invalid bridge activity query", 400, request)
    return pangea_proxy(request, "/bridge/activity", f"token={token}&hours={hours}&limit={limit}")


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


@csrf_exempt
def funding_intent(request):
    if request.method != "POST" or not post_allowed(request):
        return error("method or origin not allowed", 405 if request.method != "POST" else 403, request)
    try:
        identity = require_session(request)
        data = body(request)
        asset, amount, wallet = str(data.get("asset", "")).upper(), str(data.get("amount", "")), data.get("wallet")
        if asset not in ASSET_DECIMALS or not wallet_address_valid(identity.chain, wallet) or wallet.lower() != identity.address.lower():
            return error("invalid funding request", 400, request)
        if asset == "SOL" and identity.chain != "solana" or asset == "BNB" and identity.chain != "bsc" or asset == "ETH" and identity.chain == "bsc":
            return error("funding asset does not match connected chain", 400, request)
        amount_base = decimal_units(amount, ASSET_DECIMALS[asset])
        recipients = parse_json_setting(settings.CONI_FUNDING_RECIPIENTS)
        destination = recipients.get(asset)
        if not amount_base or not wallet_address_valid(identity.chain, destination):
            return error("funding destination or amount is not configured", 503, request)
        expires = timezone.now() + timedelta(minutes=5)
        intent = FundingIntent.objects.create(wallet_identity=identity, asset=asset, chain=identity.chain, amount_base_units=amount_base, destination=destination, status="awaiting-wallet-approval", expires_at=expires)
        return api_response({"intentId": str(intent.id), "state": intent.status, "asset": asset, "amount": amount, "destination": destination, "expiresAt": expires.isoformat()}, 201, request)
    except PermissionError as exc:
        return error(str(exc), 401, request)
    except (ValueError, json.JSONDecodeError) as exc:
        return error(str(exc), 400, request)


def bot_status_view(request):
    return api_response(bot_status(), 200, request)


@csrf_exempt
def bot_config(request):
    if request.method != "POST" or not ops_allowed(request):
        return error("operations authorization required", 403, request)
    data = body(request)
    try:
        min_bps, max_usd = int(data["minNetBps"]), Decimal(str(data["maxNotionalUsd"]))
        if not isinstance(data.get("enabled"), bool) or min_bps < 1 or min_bps > 10_000 or max_usd <= 0 or max_usd > 1_000_000:
            raise ValueError
        control, _ = BotControl.objects.get_or_create(scope="global")
        control.enabled, control.paused, control.min_net_bps, control.max_notional_usd = data["enabled"], data.get("paused", True), min_bps, max_usd
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
    control, _ = BotControl.objects.get_or_create(scope="global")
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


def fees_config(request):
    if not ops_allowed(request):
        return error("operations authorization required", 403, request)
    recipients = parse_json_setting(settings.CONI_FEE_RECIPIENTS)
    return api_response({"tradingFeeBps": settings.CONI_TRADING_FEE_BPS, "fundingFeeBps": settings.CONI_BOT_FUNDING_FEE_BPS, "recipientAssets": list(recipients)}, 200, request)


@csrf_exempt
def fees_accrue(request):
    if request.method != "POST" or not ops_allowed(request):
        return error("operations authorization required", 403, request)
    data = body(request)
    asset, chain, amount = str(data.get("asset", "")).upper(), data.get("chain"), str(data.get("amountBaseUnits", ""))
    recipient = parse_json_setting(settings.CONI_FEE_RECIPIENTS).get(asset)
    if chain not in CHAINS or not valid_amount(amount) or not wallet_address_valid(chain, recipient):
        return error("invalid fee accrual", 400, request)
    event = FeeEvent.objects.create(execution_id=data.get("executionId"), asset=asset, chain=chain, amount_base_units=amount, recipient=recipient)
    return api_response({"id": str(event.id), "status": event.status, "asset": asset, "chain": chain, "amountBaseUnits": amount, "recipient": recipient}, 201, request)


def profit_summary(request):
    if not ops_allowed(request):
        return error("operations authorization required", 403, request)
    settled = Execution.objects.filter(state="settled")
    total = sum((item.expected_profit_usd or Decimal("0") for item in settled), Decimal("0"))
    return api_response({"executions": settled.count(), "expectedProfitUsd": str(total), "realizedProfitUsd": None, "note": "Realized profit is reported only after confirmed fills and ledger settlement."}, 200, request)


@csrf_exempt
def execution_plan(request):
    if request.method != "POST" or not post_allowed(request):
        return error("method or origin not allowed", 405 if request.method != "POST" else 403, request)
    try:
        identity = require_session(request)
        data = body(request)
        if not SLUG_RE.fullmatch(data.get("slug", "")) or not valid_amount(str(data.get("amount", ""))):
            return error("invalid execution plan", 400, request)
        status = bot_status()
        if not status["executionReady"] or status["paused"] or not status["enabled"]:
            return api_response({"error": "execution is gated", "status": status, "next": "Configure Postgres, the signer, fee recipients, risk policy, and explicit live approval."}, 409, request)
        execution = Execution.objects.create(user_identity=identity, slug=data["slug"], state="quoted")
        return api_response({"executionId": str(execution.id), "state": execution.state, "next": "The signer adapter must validate and sign the exact Pangea transaction steps."}, 202, request)
    except PermissionError as exc:
        return error(str(exc), 401, request)
    except (ValueError, json.JSONDecodeError) as exc:
        return error(str(exc), 400, request)


def frontend(request, filename="index.html"):
    allowed = {"index.html", "wallet.html", "activity.html", "settings.html", "app.js", "wallet.js", "config.js", "styles.css"}
    if filename.startswith("assets/"):
        file_path = settings.BASE_DIR / filename
    elif filename in allowed:
        file_path = settings.BASE_DIR / filename
    else:
        return error("not found", 404, request)
    if not file_path.is_file() or not str(file_path.resolve()).startswith(str(settings.BASE_DIR.resolve())):
        return error("not found", 404, request)
    return FileResponse(open(file_path, "rb"))
