"""Minimal private-client for the Django signer callbacks.

This module is intentionally provider-neutral. It transports only public
provisioning metadata and an attestation produced by an external HSM/MPC
adapter. It never accepts, serializes, logs, or persists a private key, seed
phrase, or mnemonic.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen


class DjangoSignerClientError(RuntimeError):
    """Raised when Django rejects or returns an invalid signer payload."""


@dataclass(frozen=True)
class ProvisionRequest:
    provision_request_id: str
    bot_wallet_id: str
    owner_id: str
    chain: str
    mode: str
    source_address: str | None
    attempt: int
    lease_until: str | None


@dataclass(frozen=True)
class BotTransferIntent:
    transfer_id: str
    bot_wallet_id: str
    owner_id: str
    chain: str
    asset: str
    direction: str
    amount_base_units: str
    fee_amount_base_units: str
    total_reserved_base_units: str
    destination: str
    signer_key_ref: str
    attempt: int
    lease_until: str | None


@dataclass(frozen=True)
class FeeIntent:
    fee_id: str
    idempotency_key: str
    kind: str
    source_id: str
    execution_id: str | None
    user_identity_id: str | None
    chain: str
    asset: str
    gross_amount_base_units: str
    amount_base_units: str
    fee_bps: int
    recipient: str
    signer_key_ref: str
    attempt: int
    lease_until: str | None


@dataclass(frozen=True)
class WithdrawalIntent:
    withdrawal_id: str
    owner_id: str
    chain: str
    asset: str
    amount_base_units: str
    fee_amount_base_units: str
    total_reserved_base_units: str
    destination: str
    nonce: str
    signer_key_ref: str
    attempt: int
    lease_until: str | None


class DjangoSignerClient:
    """HMAC-authenticated client for the private Django signer boundary."""

    _chains = {"ethereum", "base", "robinhood", "bsc", "solana"}
    _evm_address = re.compile(r"^0x[a-fA-F0-9]{40}$")
    _solana_address = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
    _hash = re.compile(r"^(0x)?[a-fA-F0-9]{64}$")
    _solana_tx = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{43,88}$")
    _idempotency = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
    _key_ref = re.compile(r"^[A-Za-z0-9._:/-]{8,128}$")
    _uuid = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")
    _tx_hash = re.compile(r"^(?:0x[a-fA-F0-9]{64}|[1-9A-HJ-NP-Za-km-z]{43,100})$")
    _bridge_guid = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")
    _amount = re.compile(r"^\d{1,78}$")
    _money = re.compile(r"^-?(?:0|[1-9]\d{0,21})(?:\.\d{1,8})?$")
    _forbidden_keys = {"privatekey", "seed", "mnemonic", "secret"}

    def __init__(
        self,
        base_url: str,
        callback_secret: str,
        *,
        timeout: float = 10.0,
        clock: Callable[[], float] = time.time,
        opener: Callable[..., Any] | None = None,
    ):
        base_url = str(base_url or "").strip()
        if not re.fullmatch(r"https?://[^\s/]+(?:/[^\s]*)?", base_url, re.IGNORECASE):
            raise ValueError("signer Django API URL must be an absolute HTTP(S) URL")
        if not callback_secret or len(callback_secret) < 32:
            raise ValueError("signer callback secret must be at least 32 characters")
        if timeout <= 0 or timeout > 60:
            raise ValueError("signer client timeout must be between 0 and 60 seconds")
        self.base_url = base_url.rstrip("/") + "/"
        self.callback_secret = callback_secret
        self.timeout = timeout
        self.clock = clock
        self.opener = opener or urlopen

    def _signed_request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not path.startswith("/"):
            raise ValueError("signer callback path must be absolute")
        body = b"" if payload is None else json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        timestamp = int(self.clock())
        signature = hmac.new(
            self.callback_secret.encode("utf-8"),
            f"{timestamp}.".encode("ascii") + body,
            hashlib.sha256,
        ).hexdigest()
        url = urljoin(self.base_url, path.lstrip("/"))
        if query:
            url = f"{url}?{urlencode(query)}"
        headers = {
            "Accept": "application/json",
            "X-Coni-Signer-Timestamp": str(timestamp),
            "X-Coni-Signer-Signature": signature,
            "User-Agent": "coni-private-signer/1",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = Request(url, data=body if payload is not None else None, headers=headers, method=method)
        try:
            with self.opener(request, timeout=self.timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            raise DjangoSignerClientError(f"Django signer callback returned HTTP {exc.code}") from exc
        except URLError as exc:
            raise DjangoSignerClientError("Django signer callback is unavailable") from exc
        except OSError as exc:
            raise DjangoSignerClientError("Django signer callback failed") from exc
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DjangoSignerClientError("Django signer callback returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise DjangoSignerClientError("Django signer callback returned an invalid object")
        if decoded.get("error") or decoded.get("ok") is False:
            raise DjangoSignerClientError("Django signer callback rejected the request")
        return decoded

    @classmethod
    def _address_valid(cls, chain: str, address: str | None) -> bool:
        if not address:
            return True
        pattern = cls._solana_address if chain == "solana" else cls._evm_address
        return bool(pattern.fullmatch(address))

    @classmethod
    def _reject_secret_fields(cls, value: Any) -> None:
        if isinstance(value, dict):
            if any(str(key).replace("-", "").replace("_", "").lower() in cls._forbidden_keys for key in value):
                raise DjangoSignerClientError("signer response contains forbidden secret material")
            for item in value.values():
                cls._reject_secret_fields(item)
        elif isinstance(value, list):
            for item in value:
                cls._reject_secret_fields(item)

    @classmethod
    def _validate_execution_intent(cls, intent: Any) -> dict[str, Any]:
        if not isinstance(intent, dict):
            raise DjangoSignerClientError("execution response is missing intent")
        cls._reject_secret_fields(intent)
        allowed = {
            "executionId", "slug", "quoteHash", "quoteArtifact", "expectedProfitUsd",
            "botWallet", "sellBotWallet", "legs", "expiresAt",
        }
        if set(intent) - allowed or not cls._uuid.fullmatch(str(intent.get("executionId", ""))):
            raise DjangoSignerClientError("execution response contains unexpected fields")
        if not isinstance(intent.get("slug"), str) or not intent["slug"] or len(intent["slug"]) > 80:
            raise DjangoSignerClientError("execution response contains an invalid slug")
        if not cls._hash.fullmatch(str(intent.get("quoteHash", ""))):
            raise DjangoSignerClientError("execution response contains an invalid quote hash")
        artifact = intent.get("quoteArtifact")
        if not isinstance(artifact, dict) or not isinstance(artifact.get("trade"), dict) or not isinstance(artifact.get("bridge"), dict):
            raise DjangoSignerClientError("execution response contains an incomplete quote artifact")
        for wallet_key in ("botWallet", "sellBotWallet"):
            wallet = intent.get(wallet_key)
            if not isinstance(wallet, dict) or set(wallet) - {"id", "chain", "address", "signerKeyRef"}:
                raise DjangoSignerClientError("execution response contains an invalid bot wallet")
            if not cls._uuid.fullmatch(str(wallet.get("id", ""))) or wallet.get("chain") not in cls._chains:
                raise DjangoSignerClientError("execution response contains an invalid bot wallet identity")
            if not cls._address_valid(str(wallet["chain"]), wallet.get("address")) or not cls._key_ref.fullmatch(str(wallet.get("signerKeyRef", ""))):
                raise DjangoSignerClientError("execution response contains an invalid bot wallet binding")
        legs = intent.get("legs")
        if not isinstance(legs, list) or not 1 <= len(legs) <= 3:
            raise DjangoSignerClientError("execution response contains invalid route legs")
        for leg in legs:
            if not isinstance(leg, dict) or set(leg) - {"kind", "chain", "quoteHash", "amountInBaseUnits", "amountOutBaseUnits"}:
                raise DjangoSignerClientError("execution response contains an invalid route leg")
            chain = str(leg.get("chain", ""))
            chain_valid = chain in cls._chains or (
                bool(re.fullmatch(r"([a-z]+)->([a-z]+)", chain))
                and all(part in cls._chains for part in chain.split("->"))
            )
            if not chain_valid or not cls._hash.fullmatch(str(leg.get("quoteHash", ""))):
                raise DjangoSignerClientError("execution response contains an invalid route leg binding")
            if not cls._amount.fullmatch(str(leg.get("amountInBaseUnits", ""))) or not cls._amount.fullmatch(str(leg.get("amountOutBaseUnits", ""))):
                raise DjangoSignerClientError("execution response contains an invalid route amount")
        return intent

    def pull_execution_intent(self, *, execution_id: str, idempotency_key: str) -> dict[str, Any]:
        """Pull one exact, quote-bound execution for independent custody processing."""
        if not self._uuid.fullmatch(str(execution_id)) or not self._idempotency.fullmatch(str(idempotency_key)):
            raise ValueError("invalid execution intent identifier")
        response = self._signed_request(
            "POST",
            "/api/internal/signer/execution-intent",
            payload={"executionId": str(execution_id), "idempotencyKey": str(idempotency_key)},
        )
        return self._validate_execution_intent(response.get("intent"))

    def record_execution_event(
        self,
        *,
        execution_id: str,
        event: str,
        idempotency_key: str,
        tx_hash: str = "",
        bridge_guid: str = "",
        realized_profit_usd: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Post proof-bound execution lifecycle evidence to Django."""
        events = {
            "signer-requested", "buy-submitted", "buy-confirmed", "bridge-source-submitted",
            "bridge-source-confirmed", "bridge-in-flight", "bridge-destination-confirmed",
            "sell-submitted", "sell-confirmed", "settled", "failed", "refunded",
        }
        tx_events = {
            "buy-submitted", "buy-confirmed", "bridge-source-submitted", "bridge-source-confirmed",
            "bridge-destination-confirmed", "sell-submitted", "sell-confirmed",
        }
        if not self._uuid.fullmatch(str(execution_id)) or event not in events or not self._idempotency.fullmatch(str(idempotency_key)):
            raise ValueError("invalid execution event")
        if event in tx_events and not self._tx_hash.fullmatch(tx_hash):
            raise ValueError("execution event requires a valid transaction hash")
        if event == "bridge-in-flight" and not self._bridge_guid.fullmatch(bridge_guid):
            raise ValueError("bridge-in-flight requires a valid bridge GUID")
        if tx_hash and not self._tx_hash.fullmatch(tx_hash):
            raise ValueError("invalid execution transaction hash")
        if bridge_guid and not self._bridge_guid.fullmatch(bridge_guid):
            raise ValueError("invalid bridge GUID")
        if realized_profit_usd is not None:
            if not self._money.fullmatch(str(realized_profit_usd).strip()):
                raise ValueError("invalid realized profit")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError("execution metadata must be an object")
        return self._signed_request(
            "POST",
            "/api/internal/signer/execution-event",
            payload={
                "executionId": str(execution_id),
                "event": event,
                "idempotencyKey": str(idempotency_key),
                "txHash": tx_hash,
                "bridgeGuid": bridge_guid,
                "realizedProfitUsd": realized_profit_usd,
                "metadata": metadata or {},
            },
        )

    def pull_provision_requests(self, *, limit: int = 20) -> list[ProvisionRequest]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("provision request limit must be between 1 and 100")
        response = self._signed_request(
            "GET",
            "/api/internal/signer/bot-wallet-provision-requests",
            query={"limit": str(limit)},
        )
        rows = response.get("requests")
        if not isinstance(rows, list):
            raise DjangoSignerClientError("provision response is missing requests")
        parsed: list[ProvisionRequest] = []
        for row in rows:
            allowed = {
                "provisionRequestId", "botWalletId", "ownerId", "chain", "mode",
                "sourceAddress", "attempt", "leaseUntil",
            }
            if not isinstance(row, dict) or set(row) - allowed:
                raise DjangoSignerClientError("provision response contains unexpected fields")
            chain = str(row.get("chain", ""))
            if chain not in self._chains or not self._uuid.fullmatch(str(row.get("provisionRequestId", ""))) or not self._uuid.fullmatch(str(row.get("botWalletId", ""))) or not self._uuid.fullmatch(str(row.get("ownerId", ""))):
                raise DjangoSignerClientError("provision response contains invalid identifiers")
            if str(row.get("mode", "")) not in {"external-bot", "internal-bot"}:
                raise DjangoSignerClientError("provision response contains an invalid wallet mode")
            source = row.get("sourceAddress")
            if source is not None and (not isinstance(source, str) or not self._address_valid(chain, source)):
                raise DjangoSignerClientError("provision response contains an invalid source address")
            attempt = row.get("attempt")
            if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
                raise DjangoSignerClientError("provision response contains an invalid attempt")
            parsed.append(ProvisionRequest(
                provision_request_id=str(row["provisionRequestId"]),
                bot_wallet_id=str(row["botWalletId"]),
                owner_id=str(row["ownerId"]),
                chain=chain,
                mode=str(row.get("mode", "")),
                source_address=source,
                attempt=attempt,
                lease_until=str(row["leaseUntil"]) if row.get("leaseUntil") else None,
            ))
        return parsed

    def attest_bot_wallet(
        self,
        *,
        provision_request_id: str,
        bot_wallet_id: str,
        chain: str,
        address: str,
        signer_key_ref: str,
        attestation_hash: str,
    ) -> dict[str, Any]:
        if not self._uuid.fullmatch(str(provision_request_id)) or not self._uuid.fullmatch(str(bot_wallet_id)):
            raise ValueError("invalid signer provisioning identifiers")
        if chain not in self._chains:
            raise ValueError("unsupported signer chain")
        if not self._address_valid(chain, address):
            raise ValueError("invalid public bot wallet address")
        if not self._key_ref.fullmatch(signer_key_ref):
            raise ValueError("invalid opaque signer key reference")
        if not self._hash.fullmatch(attestation_hash):
            raise ValueError("invalid signer attestation hash")
        payload = {
            "provisionRequestId": str(provision_request_id),
            "botWalletId": str(bot_wallet_id),
            "chain": chain,
            "address": address,
            "signerKeyRef": signer_key_ref,
            "attestationHash": attestation_hash,
        }
        return self._signed_request("POST", "/api/internal/signer/bot-wallet-attest", payload=payload)

    def pull_bot_transfer_intents(self, *, limit: int = 20) -> list[BotTransferIntent]:
        """Lease withdrawal intents for the custody adapter."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("bot transfer intent limit must be between 1 and 100")
        response = self._signed_request(
            "GET",
            "/api/internal/signer/bot-transfer-intents",
            query={"limit": str(limit)},
        )
        rows = response.get("transfers")
        if not isinstance(rows, list):
            raise DjangoSignerClientError("transfer response is missing transfers")
        parsed: list[BotTransferIntent] = []
        for row in rows:
            allowed = {
                "transferId", "botWalletId", "ownerId", "chain", "asset", "direction",
                "amountBaseUnits", "feeAmountBaseUnits", "totalReservedBaseUnits", "destination", "signerKeyRef", "attempt", "leaseUntil",
            }
            if not isinstance(row, dict) or set(row) - allowed:
                raise DjangoSignerClientError("transfer response contains unexpected fields")
            chain = str(row.get("chain", ""))
            if (
                chain not in self._chains
                or row.get("direction") != "withdrawal"
                or not self._uuid.fullmatch(str(row.get("transferId", "")))
                or not self._uuid.fullmatch(str(row.get("botWalletId", "")))
                or not self._uuid.fullmatch(str(row.get("ownerId", "")))
                or not re.fullmatch(r"[A-Za-z0-9]{1,16}", str(row.get("asset", "")))
                or not re.fullmatch(r"\d{1,78}", str(row.get("amountBaseUnits", "")))
                or not re.fullmatch(r"\d{1,78}", str(row.get("feeAmountBaseUnits", "0")))
                or not re.fullmatch(r"\d{1,78}", str(row.get("totalReservedBaseUnits", row.get("amountBaseUnits", "0"))))
                or not self._address_valid(chain, row.get("destination"))
                or not self._key_ref.fullmatch(str(row.get("signerKeyRef", "")))
            ):
                raise DjangoSignerClientError("transfer response contains invalid intent fields")
            attempt = row.get("attempt")
            if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
                raise DjangoSignerClientError("transfer response contains an invalid attempt")
            parsed.append(BotTransferIntent(
                transfer_id=str(row["transferId"]),
                bot_wallet_id=str(row["botWalletId"]),
                owner_id=str(row["ownerId"]),
                chain=chain,
                asset=str(row["asset"]).upper(),
                direction="withdrawal",
                amount_base_units=str(row["amountBaseUnits"]),
                fee_amount_base_units=str(row.get("feeAmountBaseUnits", "0")),
                total_reserved_base_units=str(row.get("totalReservedBaseUnits", row["amountBaseUnits"])),
                destination=str(row["destination"]),
                signer_key_ref=str(row["signerKeyRef"]),
                attempt=attempt,
                lease_until=str(row["leaseUntil"]) if row.get("leaseUntil") else None,
            ))
        return parsed

    def record_bot_transfer_event(
        self,
        *,
        transfer_id: str,
        chain: str,
        event: str,
        idempotency_key: str,
        tx_hash: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Post a proof-bound bot withdrawal lifecycle event."""
        if not self._uuid.fullmatch(str(transfer_id)) or chain not in self._chains:
            raise ValueError("invalid bot transfer identifier or chain")
        if event not in {"submitted", "confirmed", "failed"}:
            raise ValueError("invalid bot transfer event")
        if not self._idempotency.fullmatch(str(idempotency_key)):
            raise ValueError("invalid bot transfer idempotency key")
        if event != "failed":
            pattern = self._solana_tx if chain == "solana" else self._hash
            if not pattern.fullmatch(tx_hash):
                raise ValueError("invalid bot transfer transaction proof")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError("bot transfer metadata must be an object")
        return self._signed_request(
            "POST",
            "/api/internal/signer/bot-transfer-event",
            payload={
                "transferId": str(transfer_id),
                "event": event,
                "idempotencyKey": str(idempotency_key),
                "txHash": tx_hash,
                "metadata": metadata or {},
            },
        )

    def pull_fee_intents(self, *, limit: int = 20) -> list[FeeIntent]:
        """Lease fee-wallet transfers for the custody adapter."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("fee intent limit must be between 1 and 100")
        response = self._signed_request(
            "GET",
            "/api/internal/signer/fee-intents",
            query={"limit": str(limit)},
        )
        rows = response.get("fees")
        if not isinstance(rows, list):
            raise DjangoSignerClientError("fee response is missing fees")
        parsed: list[FeeIntent] = []
        for row in rows:
            allowed = {
                "feeId", "idempotencyKey", "kind", "sourceId", "executionId", "userIdentityId",
                "chain", "asset", "grossAmountBaseUnits", "amountBaseUnits", "feeBps", "recipient",
                "signerKeyRef", "attempt", "leaseUntil",
            }
            if not isinstance(row, dict) or set(row) - allowed:
                raise DjangoSignerClientError("fee response contains unexpected fields")
            chain = str(row.get("chain", ""))
            if (
                chain not in self._chains
                or not self._uuid.fullmatch(str(row.get("feeId", "")))
                or not self._idempotency.fullmatch(str(row.get("idempotencyKey", "")))
                or row.get("kind") not in {"trade", "funding", "withdrawal", "bot-withdrawal"}
                or not isinstance(row.get("sourceId"), str) or not row["sourceId"]
                or not re.fullmatch(r"[A-Za-z0-9]{1,16}", str(row.get("asset", "")))
                or not re.fullmatch(r"\d{1,78}", str(row.get("grossAmountBaseUnits", "")))
                or not re.fullmatch(r"\d{1,78}", str(row.get("amountBaseUnits", "")))
                or not isinstance(row.get("feeBps"), int) or not 0 <= row["feeBps"] <= 10_000
                or not self._address_valid(chain, row.get("recipient"))
                or not self._key_ref.fullmatch(str(row.get("signerKeyRef", "")))
            ):
                raise DjangoSignerClientError("fee response contains invalid intent fields")
            attempt = row.get("attempt")
            if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
                raise DjangoSignerClientError("fee response contains an invalid attempt")
            parsed.append(FeeIntent(
                fee_id=str(row["feeId"]),
                idempotency_key=str(row["idempotencyKey"]),
                kind=str(row["kind"]),
                source_id=str(row["sourceId"]),
                execution_id=str(row["executionId"]) if row.get("executionId") else None,
                user_identity_id=str(row["userIdentityId"]) if row.get("userIdentityId") else None,
                chain=chain,
                asset=str(row["asset"]).upper(),
                gross_amount_base_units=str(row["grossAmountBaseUnits"]),
                amount_base_units=str(row["amountBaseUnits"]),
                fee_bps=int(row["feeBps"]),
                recipient=str(row["recipient"]),
                signer_key_ref=str(row["signerKeyRef"]),
                attempt=attempt,
                lease_until=str(row["leaseUntil"]) if row.get("leaseUntil") else None,
            ))
        return parsed

    def record_fee_event(
        self,
        *,
        fee_id: str,
        chain: str,
        event: str,
        idempotency_key: str,
        tx_hash: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self._uuid.fullmatch(str(fee_id)) or chain not in self._chains:
            raise ValueError("invalid fee identifier or chain")
        if event not in {"submitted", "confirmed", "failed"} or not self._idempotency.fullmatch(str(idempotency_key)):
            raise ValueError("invalid fee event")
        if event != "failed":
            pattern = self._solana_tx if chain == "solana" else self._hash
            if not pattern.fullmatch(tx_hash):
                raise ValueError("fee event requires a valid transaction proof")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError("fee metadata must be an object")
        return self._signed_request(
            "POST",
            "/api/internal/signer/fee-event",
            payload={
                "feeId": str(fee_id),
                "event": event,
                "idempotencyKey": str(idempotency_key),
                "txHash": tx_hash,
                "metadata": metadata or {},
            },
        )

    def pull_withdrawal_intents(self, *, limit: int = 20) -> list[WithdrawalIntent]:
        """Lease manually approved normal-custody withdrawal intents."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("withdrawal intent limit must be between 1 and 100")
        response = self._signed_request(
            "GET",
            "/api/internal/signer/withdrawal-intents",
            query={"limit": str(limit)},
        )
        rows = response.get("withdrawals")
        if not isinstance(rows, list):
            raise DjangoSignerClientError("withdrawal response is missing withdrawals")
        parsed: list[WithdrawalIntent] = []
        for row in rows:
            allowed = {
                "withdrawalId", "ownerId", "chain", "asset", "amountBaseUnits",
                "feeAmountBaseUnits", "totalReservedBaseUnits", "destination", "nonce", "signerKeyRef", "attempt", "leaseUntil",
            }
            if not isinstance(row, dict) or set(row) - allowed:
                raise DjangoSignerClientError("withdrawal response contains unexpected fields")
            chain = str(row.get("chain", ""))
            if (
                chain not in self._chains
                or not self._uuid.fullmatch(str(row.get("withdrawalId", "")))
                or not self._uuid.fullmatch(str(row.get("ownerId", "")))
                or not re.fullmatch(r"[A-Za-z0-9]{1,16}", str(row.get("asset", "")))
                or not re.fullmatch(r"\d{1,78}", str(row.get("amountBaseUnits", "")))
                or not re.fullmatch(r"\d{1,78}", str(row.get("feeAmountBaseUnits", "0")))
                or not re.fullmatch(r"\d{1,78}", str(row.get("totalReservedBaseUnits", row.get("amountBaseUnits", "0"))))
                or not self._address_valid(chain, row.get("destination"))
                or not self._key_ref.fullmatch(str(row.get("signerKeyRef", "")))
                or not str(row.get("nonce", ""))
            ):
                raise DjangoSignerClientError("withdrawal response contains invalid intent fields")
            attempt = row.get("attempt")
            if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
                raise DjangoSignerClientError("withdrawal response contains an invalid attempt")
            parsed.append(WithdrawalIntent(
                withdrawal_id=str(row["withdrawalId"]),
                owner_id=str(row["ownerId"]),
                chain=chain,
                asset=str(row["asset"]).upper(),
                amount_base_units=str(row["amountBaseUnits"]),
                fee_amount_base_units=str(row.get("feeAmountBaseUnits", "0")),
                total_reserved_base_units=str(row.get("totalReservedBaseUnits", row["amountBaseUnits"])),
                destination=str(row["destination"]),
                nonce=str(row["nonce"]),
                signer_key_ref=str(row["signerKeyRef"]),
                attempt=attempt,
                lease_until=str(row["leaseUntil"]) if row.get("leaseUntil") else None,
            ))
        return parsed

    def record_withdrawal_event(
        self,
        *,
        withdrawal_id: str,
        chain: str,
        event: str,
        idempotency_key: str,
        tx_hash: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Post proof-bound evidence for a normal custody withdrawal."""
        if not self._uuid.fullmatch(str(withdrawal_id)) or chain not in self._chains:
            raise ValueError("invalid withdrawal identifier or chain")
        if event not in {"submitted", "confirmed", "failed"}:
            raise ValueError("invalid withdrawal event")
        if not self._idempotency.fullmatch(str(idempotency_key)):
            raise ValueError("invalid withdrawal idempotency key")
        if event != "failed":
            pattern = self._solana_tx if chain == "solana" else self._hash
            if not pattern.fullmatch(tx_hash):
                raise ValueError("invalid withdrawal transaction proof")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError("withdrawal metadata must be an object")
        return self._signed_request(
            "POST",
            "/api/internal/signer/withdrawal-event",
            payload={
                "withdrawalId": str(withdrawal_id),
                "event": event,
                "idempotencyKey": str(idempotency_key),
                "txHash": tx_hash,
                "metadata": metadata or {},
            },
        )

    def record_profit_compound_event(
        self,
        *,
        allocation_id: str,
        event: str,
        idempotency_key: str,
        tx_hash: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Post a proof-bound retained-profit lifecycle event to Django."""
        if not self._uuid.fullmatch(str(allocation_id)):
            raise ValueError("invalid profit allocation identifier")
        if event not in {"submitted", "confirmed", "failed"}:
            raise ValueError("invalid compound event")
        if not self._idempotency.fullmatch(str(idempotency_key)):
            raise ValueError("invalid compound idempotency key")
        if tx_hash and not self._hash.fullmatch(tx_hash):
            raise ValueError("invalid compound transaction hash")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError("compound metadata must be an object")
        return self._signed_request(
            "POST",
            "/api/internal/signer/profit-compound-event",
            payload={
                "allocationId": str(allocation_id),
                "event": event,
                "idempotencyKey": str(idempotency_key),
                "txHash": tx_hash,
                "metadata": metadata or {},
            },
        )

    def pull_profit_compound_intent(self, *, allocation_id: str, idempotency_key: str) -> dict[str, Any]:
        """Pull one bounded retained-profit allocation for custody processing."""
        if not self._uuid.fullmatch(str(allocation_id)):
            raise ValueError("invalid profit allocation identifier")
        if not self._idempotency.fullmatch(str(idempotency_key)):
            raise ValueError("invalid compound idempotency key")
        response = self._signed_request(
            "POST",
            "/api/internal/signer/profit-compound-intent",
            payload={"allocationId": str(allocation_id), "idempotencyKey": str(idempotency_key)},
        )
        if not isinstance(response.get("intent"), dict):
            raise DjangoSignerClientError("compound intent response is missing intent")
        return response["intent"]
