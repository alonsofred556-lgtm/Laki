"""Private bot-wallet provisioning worker.

This process is a transport/orchestration layer only.  It never generates a
wallet, stores a key share, or signs a transaction.  A real custody provider
must implement ``POST /v1/provision`` behind the configured mTLS adapter and
return an attested public address bound to the request and session nonce.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import logging
import os
import time
from typing import Any

from .adapter import AdapterError, configured_adapter
from .django_client import DjangoSignerClient, DjangoSignerClientError


LOGGER = logging.getLogger("coni.signer.provisioning")


def _enabled() -> bool:
    return os.environ.get("SIGNER_PROVISIONING_ENABLED", "false").strip().lower() == "true"


def _client() -> DjangoSignerClient:
    return DjangoSignerClient(
        os.environ.get("SIGNER_DJANGO_API_URL", ""),
        os.environ.get("CONI_SIGNER_PROVISION_SECRET", ""),
        timeout=float(os.environ.get("SIGNER_DJANGO_TIMEOUT_SECONDS", "10")),
    )


def _session_nonce(provision_request_id: str) -> str:
    # Derive one stable, request-scoped protocol nonce. Stable retry semantics
    # let the provider return the same idempotent DKG result after a worker
    # crash; the request ID still prevents cross-wallet reuse.
    key = os.environ.get("CONI_SIGNER_PROVISION_SESSION_KEY", "")
    if len(key) < 32:
        raise RuntimeError("CONI_SIGNER_PROVISION_SESSION_KEY is not configured")
    digest = hmac.new(key.encode("utf-8"), provision_request_id.encode("ascii"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _request_payload(request: Any, nonce: str) -> dict[str, Any]:
    return {
        "provisionRequestId": request.provision_request_id,
        "botWalletId": request.bot_wallet_id,
        "ownerId": request.owner_id,
        "chain": request.chain,
        "mode": request.mode,
        "sourceAddress": request.source_address,
        "attempt": request.attempt,
        "sessionNonce": nonce,
    }


def run_once(*, client=None, adapter=None, limit: int = 20) -> list[dict[str, str]]:
    """Process one bounded lease batch and return non-sensitive statuses."""
    if not _enabled():
        raise RuntimeError("signer provisioning is disabled")
    client = client or _client()
    adapter = adapter or configured_adapter()
    results: list[dict[str, str]] = []
    for request in client.pull_provision_requests(limit=limit):
        nonce = _session_nonce(request.provision_request_id)
        try:
            result = adapter.provision(_request_payload(request, nonce), nonce)
            client.attest_bot_wallet(
                provision_request_id=request.provision_request_id,
                bot_wallet_id=request.bot_wallet_id,
                chain=result["chain"],
                address=result["address"],
                signer_key_ref=result["signerKeyRef"],
                attestation_hash=result["attestationHash"],
            )
            results.append({"provisionRequestId": request.provision_request_id, "state": "attested"})
        except (AdapterError, DjangoSignerClientError, KeyError, ValueError) as exc:
            # Do not log provider responses, addresses, nonces, or key refs.
            LOGGER.error("bot-wallet provisioning failed request=%s error=%s", request.provision_request_id, type(exc).__name__)
            results.append({"provisionRequestId": request.provision_request_id, "state": "failed"})
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Coni's private bot-wallet provisioning worker")
    parser.add_argument("--once", action="store_true", help="process one bounded request batch")
    args = parser.parse_args()
    if not _enabled():
        raise SystemExit("SIGNER_PROVISIONING_ENABLED is not true; refusing to start")
    interval = max(2.0, float(os.environ.get("SIGNER_PROVISIONING_POLL_SECONDS", "15")))
    if args.once:
        run_once()
        return 0
    while True:
        run_once()
        time.sleep(interval)


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    raise SystemExit(main())
