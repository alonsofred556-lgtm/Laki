import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "coni_backend.settings")

import django
from django.test import SimpleTestCase

django.setup()

from .chain_verifier import _solana_native_transfer_match
from .deposit_discovery import _solana_instruction_matches, _spl_account_lookup_params


class SolanaSplDiscoveryTests(SimpleTestCase):
    def test_token_account_lookup_uses_filter_and_config_parameters(self):
        self.assertEqual(
            _spl_account_lookup_params("mint-address"),
            [
                {"mint": "mint-address"},
                {"encoding": "jsonParsed", "commitment": "finalized"},
            ],
        )

    def test_parses_transfer_checked_into_treasury_token_account(self):
        transaction = {
            "transaction": {"message": {"accountKeys": [{"pubkey": "source-account"}, {"pubkey": "treasury-token-account"}]}},
            "meta": {
                "err": None,
                "postTokenBalances": [{"accountIndex": 1, "mint": "mint-address", "owner": "treasury-owner", "programId": "TokenzQdY..."}],
                "innerInstructions": [],
            },
        }
        transaction["transaction"]["message"]["instructions"] = [{
            "program": "spl-token-2022",
            "parsed": {"type": "transferChecked", "info": {"source": "source-account", "destination": "treasury-token-account", "authority": "sender-wallet", "tokenAmount": {"amount": "123", "decimals": 9}}},
        }]
        matches = list(_solana_instruction_matches(transaction, "treasury-token-account", "mint-address", "TokenzQdY..."))
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0][2], "123")
        self.assertEqual(matches[0][1]["authority"], "sender-wallet")

    def test_native_transfer_matches_its_original_instruction_index(self):
        transaction = {
            "transaction": {"message": {"instructions": [
                {"program": "system", "parsed": {"type": "transfer", "info": {"source": "other", "destination": "other-destination", "lamports": 7}}},
                {"program": "system", "parsed": {"type": "transfer", "info": {"source": "sender-wallet", "destination": "bot-wallet", "lamports": 123}}},
            ]}},
            "meta": {"innerInstructions": []},
        }
        match = _solana_native_transfer_match(transaction, 1, "sender-wallet", "bot-wallet", 123)
        self.assertEqual(match["lamports"], 123)

    def test_native_transfer_wrong_instruction_index_is_rejected(self):
        transaction = {
            "transaction": {"message": {"instructions": [
                {"program": "system", "parsed": {"type": "transfer", "info": {"source": "sender-wallet", "destination": "bot-wallet", "lamports": 123}}},
                {"program": "system", "parsed": {"type": "transfer", "info": {"source": "other", "destination": "other-destination", "lamports": 7}}},
            ]}},
            "meta": {"innerInstructions": []},
        }
        self.assertIsNone(_solana_native_transfer_match(transaction, 1, "sender-wallet", "bot-wallet", 123))
