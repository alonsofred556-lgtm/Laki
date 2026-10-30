import base64
import json
import os
import tempfile
import unittest

from nacl.signing import SigningKey

from .custody import readiness


class CustodyReadinessTests(unittest.TestCase):
    def test_railway_private_django_http_is_allowed_but_public_http_is_not(self):
        railway = readiness({"SIGNER_DJANGO_API_URL": "http://coni-web.railway.internal:8000"})
        public = readiness({"SIGNER_DJANGO_API_URL": "http://coni.example.com"})
        self.assertTrue(railway.checks["privateDjangoApi"])
        self.assertFalse(public.checks["privateDjangoApi"])

    def test_disabled_service_is_not_live_ready(self):
        result = readiness({"CONI_EXECUTION_MODE": "disabled"})
        self.assertFalse(result.ready)
        self.assertIn("liveMode", result.missing)

    def test_live_service_requires_every_custody_control(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = {}
            for name in ("ca", "cert", "key"):
                path = os.path.join(directory, name)
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write("test")
                paths[name] = path
            env = {
                "CONI_EXECUTION_MODE": "live",
                "CONI_SIGNER_AUDIT_APPROVED": "true",
                "SIGNER_ADAPTER_URL": "https://hsm.internal",
                "SIGNER_ADAPTER_CA_FILE": paths["ca"],
                "SIGNER_ADAPTER_CLIENT_CERT": paths["cert"],
                "SIGNER_ADAPTER_CLIENT_KEY": paths["key"],
                "SIGNER_ADAPTER_SHARED_SECRET": "a" * 32,
                "SIGNER_DATABASE_URL": "postgresql://signer/db",
                "SIGNER_AUDIT_DATABASE_URL": "postgresql://audit/db",
                "SIGNER_AUDIT_HMAC_KEY": "a" * 32,
                "SIGNER_SHARED_SECRET": "x" * 32,
                "SIGNER_ALLOWED_RECIPIENTS": '{"base":["0x1111111111111111111111111111111111111111"]}',
                "SIGNER_MAX_VALUE_BASE_UNITS": "100",
                "SIGNER_MAX_TOTAL_VALUE_BASE_UNITS": "100",
                "SIGNER_REQUIRED_APPROVALS": "2",
                "SIGNER_APPROVER_KEYS": '{"a":"bad","b":"bad"}',
                "SIGNER_AUDIT_SINK_ID": "sink-2026",
                "SIGNER_CUSTODY_POLICY_ID": "policy-2026",
                "SIGNER_KEY_CEREMONY_ID": "ceremony-2026",
                "SIGNER_RECOVERY_DRILL_ID": "recovery-2026",
                "SIGNER_SECURITY_REVIEW_ID": "review-2026",
                "SIGNER_APPROVAL_POLICY_ID": "approval-2026",
            }
            result = readiness(env)
            self.assertFalse(result.ready)
            self.assertIn("twoPersonApproval", result.missing)

    def test_complete_live_policy_is_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = {}
            for name in ("ca", "cert", "key"):
                path = os.path.join(directory, name)
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write("test")
                paths[name] = path
            keys = [SigningKey.generate().verify_key for _ in range(2)]
            encoded = {name: base64.b64encode(key.encode()).decode("ascii") for name, key in zip(("ops-a", "ops-b"), keys)}
            env = {
                "CONI_EXECUTION_MODE": "live",
                "CONI_SIGNER_AUDIT_APPROVED": "true",
                "SIGNER_ADAPTER_URL": "https://hsm.internal",
                "SIGNER_ADAPTER_CA_FILE": paths["ca"],
                "SIGNER_ADAPTER_CLIENT_CERT": paths["cert"],
                "SIGNER_ADAPTER_CLIENT_KEY": paths["key"],
                "SIGNER_ADAPTER_SHARED_SECRET": "a" * 32,
                "SIGNER_DATABASE_URL": "postgresql://signer/db",
                "SIGNER_AUDIT_DATABASE_URL": "postgresql://audit/db",
                "SIGNER_AUDIT_HMAC_KEY": "a" * 32,
                "SIGNER_SHARED_SECRET": "x" * 32,
                "CONI_SIGNER_PROVISION_SECRET": "p" * 32,
                "CONI_SIGNER_EVENT_SECRET": "e" * 32,
                "SIGNER_DJANGO_API_URL": "https://coni.internal",
                "SIGNER_ALLOWED_RECIPIENTS": '{"base":["0x1111111111111111111111111111111111111111"]}',
                "SIGNER_MAX_VALUE_BASE_UNITS": "100",
                "SIGNER_MAX_TOTAL_VALUE_BASE_UNITS": "100",
                "SIGNER_REQUIRED_APPROVALS": "2",
                "SIGNER_APPROVER_KEYS": json.dumps(encoded),
                "SIGNER_AUDIT_SINK_ID": "sink-2026",
                "SIGNER_CUSTODY_POLICY_ID": "policy-2026",
                "SIGNER_KEY_CEREMONY_ID": "ceremony-2026",
                "SIGNER_RECOVERY_DRILL_ID": "recovery-2026",
                "SIGNER_SECURITY_REVIEW_ID": "review-2026",
                "SIGNER_APPROVAL_POLICY_ID": "approval-2026",
            }
            result = readiness(env)
            self.assertTrue(result.ready, result.missing)
            self.assertEqual(result.missing, ())


if __name__ == "__main__":
    unittest.main()
