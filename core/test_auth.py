import os
import json
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "coni_backend.settings")

import django
from django.conf import settings
from django.test import SimpleTestCase, TestCase
from eth_account import Account
from eth_account.messages import encode_defunct

django.setup()

from .bot_controls import BOT_MODES
from .models import WalletIdentity
from .views import canonical_wallet_address, session_identity, session_identities, verify_signature, wallet_address_matches


class WalletAuthenticationTests(SimpleTestCase):
    def test_evm_signature_recovery_accepts_standard_and_unprefixed_hex(self):
        account = Account.create()
        message = "coni login challenge"
        challenge = SimpleNamespace(chain="base", address=account.address.lower(), message=message)
        signed = Account.sign_message(encode_defunct(text=message), account.key).signature.hex()
        self.assertTrue(verify_signature(challenge, "0x" + signed))
        self.assertTrue(verify_signature(challenge, signed))

    def test_evm_signature_recovery_rejects_invalid_shape(self):
        challenge = SimpleNamespace(chain="base", address="0x" + "1" * 40, message="coni login challenge")
        self.assertFalse(verify_signature(challenge, "0x1234"))

    def test_evm_addresses_are_canonicalized_before_persistence(self):
        address = "0xAbCd000000000000000000000000000000000001"
        self.assertEqual(canonical_wallet_address("base", address), address.lower())

    def test_solana_address_casing_is_preserved(self):
        address = "Abcdefghijkmnpqrstuvwxyz123456789"
        self.assertEqual(canonical_wallet_address("solana", address), address)

    def test_invalid_address_is_not_transformed(self):
        address = "not-a-wallet"
        self.assertEqual(canonical_wallet_address("base", address), address)

    def test_evm_address_matching_is_case_insensitive(self):
        address = "0xAbCd000000000000000000000000000000000001"
        self.assertTrue(wallet_address_matches("base", address.lower(), address))

    def test_solana_address_matching_is_case_sensitive(self):
        address = "Abcdefghijkmnpqrstuvwxyz123456789"
        self.assertTrue(wallet_address_matches("solana", address, address))
        self.assertFalse(wallet_address_matches("solana", address.swapcase(), address))

    def test_non_string_addresses_never_match(self):
        self.assertFalse(wallet_address_matches("base", None, "0x1111111111111111111111111111111111111111"))

    def test_wallet_modes_are_explicit_and_limited_to_supported_custody_modes(self):
        self.assertEqual(BOT_MODES, {"external-bot", "internal-bot"})


class MultiIdentitySessionTests(SimpleTestCase):
    def test_session_keeps_evm_and_solana_identities_and_selects_by_chain(self):
        evm = SimpleNamespace(id="evm-id", chain="base", address="0x" + "1" * 40)
        solana = SimpleNamespace(id="sol-id", chain="solana", address="Abcdefghijkmnpqrstuvwxyz123456789")
        request = SimpleNamespace(session={"wallet_identity_ids": ["evm-id", "sol-id"], "wallet_identity_id": "sol-id"})
        with patch("core.views.WalletIdentity.objects.filter", return_value=[evm, solana]):
            self.assertEqual([item.id for item in session_identities(request)], ["sol-id", "evm-id"])
            self.assertEqual(session_identity(request, chain="base").id, "evm-id")
            self.assertEqual(session_identity(request, chain="solana").id, "sol-id")


class ChainScopedLogoutTests(TestCase):
    def setUp(self):
        self.evm = WalletIdentity.objects.create(chain="base", address="0x" + "1" * 40)
        self.solana = WalletIdentity.objects.create(chain="solana", address="Abcdefghijkmnpqrstuvwxyz123456789")
        session = self.client.session
        session["wallet_identity_ids"] = [str(self.evm.id), str(self.solana.id)]
        session["wallet_identity_id"] = str(self.solana.id)
        session["wallet_chain"] = self.solana.chain
        session["wallet_address"] = self.solana.address
        session.save()
        self.client.cookies[settings.SESSION_COOKIE_NAME] = session.session_key

    def test_evm_logout_removes_only_evm_identity(self):
        response = self.client.post(
            "/api/auth/logout-identity",
            data=json.dumps({"chain": "base", "address": self.evm.address[:2] + self.evm.address[2:].upper()}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        session = self.client.session
        self.assertEqual(session["wallet_identity_ids"], [str(self.solana.id)])
        self.assertEqual(session["wallet_identity_id"], str(self.solana.id))
        self.assertEqual(self.client.get("/api/auth/session").json()["wallet"]["chain"], "solana")

    def test_logout_rejects_identity_not_in_session(self):
        other = WalletIdentity.objects.create(chain="bsc", address="0x" + "2" * 40)
        response = self.client.post(
            "/api/auth/logout-identity",
            data=json.dumps({"chain": other.chain, "address": other.address}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 404)
