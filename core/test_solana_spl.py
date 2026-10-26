import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "coni_backend.settings")

import django
from django.test import SimpleTestCase

django.setup()

from .deposit_discovery import _solana_instruction_matches


class SolanaSplDiscoveryTests(SimpleTestCase):
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
