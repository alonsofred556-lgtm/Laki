"""mTLS adapter bridge for the real HSM/MPC custody system.

The bridge is intentionally provider-neutral. The private service never sees a
private key; it sends a hash-bound intent to an HSM/MPC endpoint that performs
the key operation inside custody and returns a signature or signed payload.
"""

from __future__ import annotations

import json
import hashlib
import hmac
import os
import re
import ssl
import time
from dataclasses import asdict, is_dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class AdapterError(RuntimeError):
    pass


_KEY_REF = re.compile(r"^[A-Za-z0-9._:/-]{8,128}$")
_ATTESTATION = re.compile(r"^(0x)?[a-fA-F0-9]{64}$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")
_EVM_ADDRESS = re.compile(r"^0x[a-fA-F0-9]{40}$")
_SOLANA_ADDRESS = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_PROVISION_CHAINS = {"ethereum", "base", "robinhood", "bsc", "solana"}
_PROVISION_MODES = {"external-bot", "internal-bot"}
_FORBIDDEN_FIELDS = {"privatekey", "seed", "mnemonic", "secret", "keyshare", "share"}


def _jsonable(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _reject_secret_fields(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).replace("-", "").replace("_", "").lower() in _FORBIDDEN_FIELDS:
                raise AdapterError("adapter payload contains forbidden key material")
            _reject_secret_fields(item)
    elif isinstance(value, list):
        for item in value:
            _reject_secret_fields(item)


class HsmMpcHttpAdapter:
    """Call an independently operated HSM/MPC signing adapter over mTLS."""

    name = "hsm-mpc-mtls"

    def __init__(self, *, url, ca_file="", client_cert="", client_key="", shared_secret="", timeout=5.0, clock=time.time, opener=None):
        parsed = urlparse(str(url or "").strip())
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("HSM/MPC adapter URL must be HTTPS")
        if timeout <= 0 or timeout > 30:
            raise ValueError("adapter timeout must be between 0 and 30 seconds")
        if shared_secret and len(shared_secret) < 32:
            raise ValueError("adapter shared secret must be at least 32 characters")
        self.base_url = parsed.geturl().rstrip("/")
        self.url = self.base_url + "/v1/sign"
        self.provision_url = self.base_url + "/v1/provision"
        self.timeout = timeout
        self.shared_secret = shared_secret
        self.clock = clock
        self.opener = opener or urlopen
        self.context = ssl.create_default_context(cafile=ca_file or None)
        if client_cert or client_key:
            if not client_cert or not client_key:
                raise ValueError("mTLS client certificate and key must be configured together")
            self.context.load_cert_chain(client_cert, client_key)

    def sign(self, intent):
        body = json.dumps(_jsonable(intent), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        headers = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "coni-signer/1"}
        if self.shared_secret:
            timestamp = str(int(self.clock()))
            signature = hmac.new(
                self.shared_secret.encode("utf-8"),
                f"{timestamp}.".encode("ascii") + body,
                hashlib.sha256,
            ).hexdigest()
            headers.update({
                "X-Coni-Adapter-Timestamp": timestamp,
                "X-Coni-Adapter-Signature": signature,
            })
        request = Request(
            self.url,
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with self.opener(request, timeout=self.timeout, context=self.context) as response:
                raw = response.read()
        except (HTTPError, URLError, OSError) as exc:
            raise AdapterError("HSM/MPC adapter unavailable") from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AdapterError("HSM/MPC adapter returned invalid JSON") from exc
        if not isinstance(payload, dict) or payload.get("intentDigest") != intent.get("digest"):
            raise AdapterError("HSM/MPC adapter returned an unbound response")
        signature = payload.get("signature") or payload.get("signedPayload")
        if not isinstance(signature, str) or not signature or len(signature) > 400000:
            raise AdapterError("HSM/MPC adapter returned no bounded signature")
        signer_key_ref = payload.get("signerKeyRef")
        attestation = payload.get("attestation")
        if not isinstance(signer_key_ref, str) or not _KEY_REF.fullmatch(signer_key_ref):
            raise AdapterError("HSM/MPC adapter returned no opaque signer key reference")
        if not isinstance(attestation, str) or not _ATTESTATION.fullmatch(attestation):
            raise AdapterError("HSM/MPC adapter returned no bounded attestation")
        return {
            "intentDigest": intent["digest"],
            "signature": signature,
            "signerKeyRef": signer_key_ref,
            "attestation": attestation,
            "adapter": self.name,
        }

    def provision(self, request, session_nonce):
        """Request public wallet metadata from the custody provider.

        The provider must perform DKG/derivation inside custody. This method
        transports only the leased request and a one-time protocol nonce; it
        rejects any response containing private material before it reaches the
        Django callback.
        """
        payload = _jsonable(request)
        if not isinstance(payload, dict):
            raise AdapterError("provision request must be an object")
        allowed = {
            "provisionRequestId", "botWalletId", "ownerId", "chain", "mode",
            "sourceAddress", "attempt", "sessionNonce",
        }
        if set(payload) - allowed or payload.get("sessionNonce") != session_nonce:
            raise AdapterError("provision request envelope is invalid")
        if not _UUID.fullmatch(str(payload.get("provisionRequestId", ""))) or not _UUID.fullmatch(str(payload.get("botWalletId", ""))) or not _UUID.fullmatch(str(payload.get("ownerId", ""))):
            raise AdapterError("provision request identifiers are invalid")
        chain = str(payload.get("chain", ""))
        if chain not in _PROVISION_CHAINS or str(payload.get("mode", "")) not in _PROVISION_MODES:
            raise AdapterError("provision request chain or mode is invalid")
        if not isinstance(session_nonce, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", session_nonce):
            raise AdapterError("provision session nonce is invalid")
        attempt = payload.get("attempt")
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            raise AdapterError("provision request attempt is invalid")
        _reject_secret_fields(payload)
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        headers = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "coni-signer-provisioner/1"}
        if self.shared_secret:
            timestamp = str(int(self.clock()))
            signature = hmac.new(self.shared_secret.encode("utf-8"), f"{timestamp}.".encode("ascii") + body, hashlib.sha256).hexdigest()
            headers.update({"X-Coni-Adapter-Timestamp": timestamp, "X-Coni-Adapter-Signature": signature})
        request_obj = Request(self.provision_url, data=body, headers=headers, method="POST")
        try:
            with self.opener(request_obj, timeout=self.timeout, context=self.context) as response:
                raw = response.read()
        except (HTTPError, URLError, OSError) as exc:
            raise AdapterError("HSM/MPC provisioning adapter unavailable") from exc
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AdapterError("HSM/MPC provisioning adapter returned invalid JSON") from exc
        _reject_secret_fields(result)
        if not isinstance(result, dict):
            raise AdapterError("HSM/MPC provisioning adapter returned an invalid object")
        allowed_result = {"provisionRequestId", "botWalletId", "chain", "address", "signerKeyRef", "attestationHash", "sessionNonce"}
        if set(result) - allowed_result or result.get("provisionRequestId") != payload["provisionRequestId"] or result.get("botWalletId") != payload["botWalletId"] or result.get("chain") != chain or result.get("sessionNonce") != session_nonce:
            raise AdapterError("HSM/MPC provisioning response is not bound to the request")
        address = str(result.get("address", ""))
        pattern = _SOLANA_ADDRESS if chain == "solana" else _EVM_ADDRESS
        if not pattern.fullmatch(address) or not _KEY_REF.fullmatch(str(result.get("signerKeyRef", ""))) or not _ATTESTATION.fullmatch(str(result.get("attestationHash", ""))):
            raise AdapterError("HSM/MPC provisioning response is invalid")
        return result


class UnconfiguredAdapter:
    name = "unconfigured"

    def sign(self, intent):
        raise AdapterError("no HSM/MPC adapter is configured")

    def provision(self, request, session_nonce):
        raise AdapterError("no HSM/MPC provisioning adapter is configured")


def configured_adapter():
    url = os.environ.get("SIGNER_ADAPTER_URL", "").strip()
    if not url:
        return UnconfiguredAdapter()
    return HsmMpcHttpAdapter(
        url=url,
        ca_file=os.environ.get("SIGNER_ADAPTER_CA_FILE", "").strip(),
        client_cert=os.environ.get("SIGNER_ADAPTER_CLIENT_CERT", "").strip(),
        client_key=os.environ.get("SIGNER_ADAPTER_CLIENT_KEY", "").strip(),
        shared_secret=os.environ.get("SIGNER_ADAPTER_SHARED_SECRET", "").strip(),
        timeout=float(os.environ.get("SIGNER_ADAPTER_TIMEOUT_SECONDS", "5")),
    )
