"""mTLS adapter bridge for the real HSM/MPC custody system.

The bridge is intentionally provider-neutral. The private service never sees a
private key; it sends a hash-bound intent to an HSM/MPC endpoint that performs
the key operation inside custody and returns a signature or signed payload.
"""

from __future__ import annotations

import json
import os
import ssl
from dataclasses import asdict, is_dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class AdapterError(RuntimeError):
    pass


def _jsonable(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


class HsmMpcHttpAdapter:
    """Call an independently operated HSM/MPC signing adapter over mTLS."""

    name = "hsm-mpc-mtls"

    def __init__(self, *, url, ca_file="", client_cert="", client_key="", timeout=5.0, opener=None):
        parsed = urlparse(str(url or "").strip())
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("HSM/MPC adapter URL must be HTTPS")
        if timeout <= 0 or timeout > 30:
            raise ValueError("adapter timeout must be between 0 and 30 seconds")
        self.url = parsed.geturl().rstrip("/") + "/v1/sign"
        self.timeout = timeout
        self.opener = opener or urlopen
        self.context = ssl.create_default_context(cafile=ca_file or None)
        if client_cert or client_key:
            if not client_cert or not client_key:
                raise ValueError("mTLS client certificate and key must be configured together")
            self.context.load_cert_chain(client_cert, client_key)

    def sign(self, intent):
        body = json.dumps(_jsonable(intent), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        request = Request(
            self.url,
            data=body,
            headers={"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "coni-signer/1"},
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
        return {"intentDigest": intent["digest"], "signature": signature, "adapter": self.name}


class UnconfiguredAdapter:
    name = "unconfigured"

    def sign(self, intent):
        raise AdapterError("no HSM/MPC adapter is configured")


def configured_adapter():
    url = os.environ.get("SIGNER_ADAPTER_URL", "").strip()
    if not url:
        return UnconfiguredAdapter()
    return HsmMpcHttpAdapter(
        url=url,
        ca_file=os.environ.get("SIGNER_ADAPTER_CA_FILE", "").strip(),
        client_cert=os.environ.get("SIGNER_ADAPTER_CLIENT_CERT", "").strip(),
        client_key=os.environ.get("SIGNER_ADAPTER_CLIENT_KEY", "").strip(),
        timeout=float(os.environ.get("SIGNER_ADAPTER_TIMEOUT_SECONDS", "5")),
    )
