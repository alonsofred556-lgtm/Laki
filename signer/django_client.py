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


class DjangoSignerClient:
    """HMAC-authenticated client for the private Django signer boundary."""

    _chains = {"ethereum", "base", "robinhood", "bsc", "solana"}
    _evm_address = re.compile(r"^0x[a-fA-F0-9]{40}$")
    _solana_address = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
    _hash = re.compile(r"^(0x)?[a-fA-F0-9]{64}$")
    _key_ref = re.compile(r"^[A-Za-z0-9._:-]{8,256}$")

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
        if not decoded.get("ok", True) and decoded.get("error"):
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
            if chain not in self._chains or not row.get("provisionRequestId") or not row.get("botWalletId") or not row.get("ownerId"):
                raise DjangoSignerClientError("provision response contains invalid identifiers")
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
