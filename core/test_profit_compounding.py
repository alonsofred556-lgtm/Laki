import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "coni_backend.settings")

import django
from django.test import SimpleTestCase

django.setup()

from decimal import Decimal

from .profit_compounding import _positive_integer, next_compound_state, split_realized_profit


class ProfitCompoundingTests(SimpleTestCase):
    def test_compound_lifecycle_requires_submission_before_confirmation(self):
        self.assertEqual(next_compound_state("pending-approval", "submitted"), "submitted")
        self.assertEqual(next_compound_state("submitted", "confirmed"), "confirmed")
        self.assertEqual(next_compound_state("submitted", "failed"), "failed")
        with self.assertRaises(ValueError):
            next_compound_state("pending-approval", "confirmed")

    def test_compound_amount_must_be_a_positive_integer_string(self):
        self.assertEqual(_positive_integer("1000"), 1000)
        self.assertEqual(_positive_integer("0001000"), 1000)
        self.assertEqual(_positive_integer("1.5"), 0)
        self.assertEqual(_positive_integer("0"), 0)

    def test_non_positive_realized_pnl_is_recorded_without_a_payout_or_reinvestment(self):
        for realized in ("0", "-0.25"):
            split = split_realized_profit(realized, 7000)
            self.assertEqual(split["realized"], Decimal(realized))
            self.assertEqual(split["compound"], Decimal("0"))
            self.assertEqual(split["payout"], Decimal("0"))
            self.assertEqual(split["compoundState"], "not-applicable")

    def test_positive_realized_pnl_is_split_by_compound_policy(self):
        split = split_realized_profit("1.00", 7000)
        self.assertEqual(split["compound"], Decimal("0.70000000"))
        self.assertEqual(split["payout"], Decimal("0.30000000"))
        self.assertEqual(split["compoundState"], "pending-approval")
