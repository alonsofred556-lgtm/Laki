import json
import os
from unittest.mock import patch

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "coni_backend.settings")

import django
from django.test import TestCase, override_settings

django.setup()

from .deposit_discovery import discover_evm_chain, discover_solana_spl
from .models import BotWallet, DepositObservation, WalletIdentity


class DynamicBotWalletDepositDiscoveryTests(TestCase):
    @override_settings(
        CONI_RPC_URLS=json.dumps({"base": ["https://base.example"]}),
        CONI_EXPECTED_CHAIN_IDS=json.dumps({"base": "0x2105"}),
        CONI_DEPOSIT_RECIPIENTS="{}",
        CONI_TOKEN_REGISTRY="{}",
        CONI_DEPOSIT_CONFIRMATIONS=1,
        CONI_DEPOSIT_SCAN_BATCH=50,
        CONI_DEPOSIT_SCAN_LOOKBACK_BLOCKS=10,
    )
    def test_evm_native_transfer_to_attested_bot_wallet_is_discovered_as_unclaimed(self):
        identity = WalletIdentity.objects.create(
            chain="base",
            address="0x" + "1" * 40,
        )
        bot_address = "0x" + "2" * 40
        BotWallet.objects.create(
            owner=identity,
            chain="base",
            address=bot_address,
            mode="external-bot",
            custody="audited-signer",
            status="active",
            signer_key_ref="mpc/base/user-1",
        )

        def rpc(chain, method, params):
            self.assertEqual(chain, "base")
            if method == "eth_chainId":
                return "0x2105"
            if method == "eth_blockNumber":
                return "0x2"
            if method == "eth_getBlockByNumber":
                if params[1] is False:
                    return {"hash": "0x" + "a" * 64}
                if params[0] == "0x0":
                    return {
                        "hash": "0x" + "a" * 64,
                        "transactions": [{
                            "from": "0x" + "3" * 40,
                            "to": bot_address,
                            "value": "0x7b",
                            "hash": "0x" + "b" * 64,
                            "transactionIndex": "0x0",
                        }],
                    }
                return {"hash": "0x" + "a" * 64, "transactions": []}
            raise AssertionError(f"unexpected RPC method: {method}")

        with patch("core.deposit_discovery.rpc_result", side_effect=rpc):
            result = discover_evm_chain("base")

        self.assertEqual(result["created"], 1)
        observation = DepositObservation.objects.get()
        self.assertEqual(observation.status, "unclaimed")
        self.assertEqual(observation.wallet_identity_id, identity.id)
        self.assertEqual(observation.destination, bot_address)
        self.assertEqual(observation.amount_base_units, 123)

    @override_settings(
        CONI_SOLANA_RPC_URL="https://solana.example",
        CONI_DEPOSIT_RECIPIENTS="{}",
        CONI_TOKEN_REGISTRY=json.dumps({
            "ZGAP": {
                "solana": {
                    "address": "Mint111111111111111111111111111111111111111",
                    "decimals": 6,
                    "programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                }
            }
        }),
        CONI_DEPOSIT_SCAN_BATCH=50,
    )
    def test_solana_spl_transfer_to_attested_bot_wallet_is_discovered_as_unclaimed(self):
        identity = WalletIdentity.objects.create(
            chain="solana",
            address="Owner1111111111111111111111111111111111111",
        )
        bot_address = "Bot111111111111111111111111111111111111111"
        token_account = "TokenAccount111111111111111111111111111111"
        mint = "Mint111111111111111111111111111111111111111"
        program_id = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
        BotWallet.objects.create(
            owner=identity,
            chain="solana",
            address=bot_address,
            mode="external-bot",
            custody="audited-signer",
            status="active",
            signer_key_ref="mpc/solana/user-1",
        )

        def rpc(chain, method, params):
            self.assertEqual(chain, "solana")
            if method == "getTokenAccountsByOwner":
                self.assertEqual(params[0], bot_address)
                return {"value": [{"pubkey": token_account}]}
            if method == "getSignaturesForAddress":
                self.assertEqual(params[0], token_account)
                return [{"signature": "solana-signature-1"}]
            if method == "getTransaction":
                return {
                    "slot": 42,
                    "meta": {
                        "err": None,
                        "postTokenBalances": [{
                            "accountIndex": 0,
                            "owner": bot_address,
                            "mint": mint,
                            "programId": program_id,
                        }],
                    },
                    "transaction": {
                        "message": {
                            "accountKeys": [token_account],
                            "instructions": [{
                                "program": "spl-token",
                                "parsed": {
                                    "type": "transferChecked",
                                    "program": "spl-token",
                                    "info": {
                                        "authority": "Sender1111111111111111111111111111111111111",
                                        "destination": token_account,
                                        "tokenAmount": {"amount": "500"},
                                    },
                                },
                            }],
                        }
                    },
                }
            raise AssertionError(f"unexpected RPC method: {method}")

        with patch("core.deposit_discovery.rpc_result", side_effect=rpc):
            result = discover_solana_spl()

        self.assertEqual(result["created"], 1)
        observation = DepositObservation.objects.get()
        self.assertEqual(observation.status, "unclaimed")
        self.assertEqual(observation.wallet_identity_id, identity.id)
        self.assertEqual(observation.asset, "ZGAP")
        self.assertEqual(observation.destination, bot_address)
        self.assertEqual(observation.amount_base_units, 500)
        self.assertEqual(observation.raw_event["tokenAccount"], token_account)

    @override_settings(
        CONI_SOLANA_RPC_URL="https://solana.example",
        CONI_DEPOSIT_RECIPIENTS="{}",
        CONI_TOKEN_REGISTRY=json.dumps({
            "ZGAP": {
                "solana": {
                    "address": "Mint111111111111111111111111111111111111111",
                    "decimals": 6,
                    "programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                }
            }
        }),
        CONI_DEPOSIT_SCAN_BATCH=50,
    )
    def test_solana_spl_discovery_rejects_missing_or_mismatched_token_program(self):
        identity = WalletIdentity.objects.create(
            chain="solana",
            address="Owner1111111111111111111111111111111111111",
        )
        bot_address = "Bot111111111111111111111111111111111111111"
        BotWallet.objects.create(
            owner=identity,
            chain="solana",
            address=bot_address,
            mode="external-bot",
            custody="audited-signer",
            status="active",
            signer_key_ref="mpc/solana/user-2",
        )
        token_account = "TokenAccount111111111111111111111111111111"
        mint = "Mint111111111111111111111111111111111111111"

        def rpc(chain, method, params):
            if method == "getTokenAccountsByOwner":
                return {"value": [{"pubkey": token_account}]}
            if method == "getSignaturesForAddress":
                return [{"signature": "solana-signature-2"}]
            if method == "getTransaction":
                return {
                    "slot": 43,
                    "meta": {
                        "err": None,
                        "postTokenBalances": [{
                            "accountIndex": 0,
                            "owner": bot_address,
                            "mint": mint,
                            # Omitted on purpose: the registry requires an
                            # exact program id before discovery can proceed.
                        }],
                    },
                    "transaction": {
                        "message": {
                            "accountKeys": [token_account],
                            "instructions": [{
                                "program": "spl-token",
                                "parsed": {
                                    "type": "transferChecked",
                                    "program": "spl-token",
                                    "info": {
                                        "authority": "Sender1111111111111111111111111111111111111",
                                        "destination": token_account,
                                        "tokenAmount": {"amount": "500"},
                                    },
                                },
                            }],
                        }
                    },
                }
            raise AssertionError(f"unexpected RPC method: {method}")

        with patch("core.deposit_discovery.rpc_result", side_effect=rpc):
            result = discover_solana_spl()

        self.assertEqual(result["created"], 0)
        self.assertFalse(DepositObservation.objects.exists())
