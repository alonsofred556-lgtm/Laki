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
    destination: str
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
                "amountBaseUnits", "destination", "signerKeyRef", "attempt", "leaseUntil",
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
                "destination", "nonce", "signerKeyRef", "attempt", "leaseUntil",
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
