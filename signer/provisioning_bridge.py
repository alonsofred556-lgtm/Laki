"""Fail-closed bot-wallet provisioning boundary.

This module deliberately does not generate keys, derive addresses, or attest a
wallet.  A real MPC/TSS provider must implement ``ProvisioningProvider`` and
return a public address plus an opaque key reference and attestation that is
bound to the exact request.  Private keys and key shares are rejected at this
boundary.

The bridge is intentionally dependency-free so it can be used by a signer
worker, a management command, or an adapter without coupling the custody
policy to Django models.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence
import re


class ProvisioningError(RuntimeError):
    """Raised when provisioning cannot be completed safely."""


@dataclass(frozen=True)
class ProvisionRequest:
    request_id: str
    owner_id: str
    chain: str
    mode: str
    idempotency_key: str
    request_nonce: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ProvisionRequest":
        required = (
            "request_id",
            "owner_id",
            "chain",
            "mode",
            "idempotency_key",
            "request_nonce",
        )
        missing = [name for name in required if not str(value.get(name, "")).strip()]
        if missing:
            raise ProvisioningError("provisioning request is missing required fields")
        return cls(*(str(value[name]).strip() for name in required))


@dataclass(frozen=True)
class ProvisionedWallet:
    request_id: str
    chain: str
    address: str
    signer_key_ref: str
    attestation: str
    request_nonce: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ProvisionedWallet":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise ProvisioningError("provider response must be an object")
        forbidden = {
            "private_key",
            "privateKey",
            "secret",
            "key_share",
            "keyShare",
            "share",
            "seed",
        }
        if any(name in value and value[name] for name in forbidden):
            raise ProvisioningError("provider returned private key material")
        required = (
            "request_id",
            "chain",
            "address",
            "signer_key_ref",
            "attestation",
            "request_nonce",
        )
        missing = [name for name in required if not str(value.get(name, "")).strip()]
        if missing:
            raise ProvisioningError("provider response is incomplete")
        result = cls(*(str(value[name]).strip() for name in required))
        if len(result.signer_key_ref) > 256 or len(result.attestation) > 512:
            raise ProvisioningError("provider response field is too large")
        return result


class ProvisioningProvider(Protocol):
    def provision(self, request: ProvisionRequest) -> ProvisionedWallet:
        """Run DKG/derivation in the provider and return public attestation data."""


class DisabledProvisioningProvider:
    """Default provider used until a real audited MPC provider is configured."""

    def provision(self, request: ProvisionRequest) -> ProvisionedWallet:
        raise ProvisioningError("MPC wallet provisioning is disabled")


class ProvisioningBridge:
    """Validate provider output before calling the Django attestation endpoint.

    ``pull`` must return pending request mappings.  ``attest`` must be the
    authenticated signer-client callback.  A failed provider call never calls
    ``attest``.  Duplicate requests are left to the server-side idempotency
    constraint; this bridge does not maintain a mutable local ledger.
    """

    def __init__(
        self,
        provider: ProvisioningProvider,
        pull,
        attest,
    ) -> None:
        self.provider = provider
        self.pull = pull
        self.attest = attest

    def run_once(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for raw in self._requests():
            request = ProvisionRequest.from_mapping(raw)
            wallet = ProvisionedWallet.from_mapping(
                self.provider.provision(request)
            )
            self._validate_binding(request, wallet)
            response = self.attest(
                request_id=request.request_id,
                chain=wallet.chain,
                address=wallet.address,
                signer_key_ref=wallet.signer_key_ref,
                attestation=wallet.attestation,
                request_nonce=wallet.request_nonce,
            )
            results.append({"request_id": request.request_id, "response": response})
        return results

    def _requests(self) -> Sequence[Mapping[str, Any]]:
        value = self.pull()
        if value is None:
            return ()
        if isinstance(value, (str, bytes, Mapping)):
            raise ProvisioningError("provisioning queue returned an invalid shape")
        return value

    @staticmethod
    def _validate_binding(
        request: ProvisionRequest, wallet: ProvisionedWallet
    ) -> None:
        if wallet.request_id != request.request_id:
            raise ProvisioningError("provider response is bound to another request")
        if wallet.request_nonce != request.request_nonce:
            raise ProvisioningError("provider response nonce mismatch")
        if wallet.chain.lower() != request.chain.lower():
            raise ProvisioningError("provider response chain mismatch")
        if not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,256}", wallet.signer_key_ref):
            raise ProvisioningError("invalid opaque signer key reference")
        if not re.fullmatch(r"[A-Za-z0-9_.:+/=~-]{16,512}", wallet.attestation):
            raise ProvisioningError("invalid provider attestation")
        chain = request.chain.lower()
        if chain in {"ethereum", "base", "bsc", "robinhood"}:
            if not re.fullmatch(r"0x[0-9a-fA-F]{40}", wallet.address):
                raise ProvisioningError("invalid EVM wallet address")
        elif chain == "solana":
            if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", wallet.address):
                raise ProvisioningError("invalid Solana wallet address")
        else:
            raise ProvisioningError("unsupported wallet chain")
