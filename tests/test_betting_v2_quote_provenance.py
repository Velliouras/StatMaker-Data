from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/betting_v2"))
from quote_provenance import verified_offer_reason


def quote():
    return {
        "quoteObservedAt": "2026-10-10T20:00:00Z",
        "quoteCutoff": "2026-10-10T21:00:00Z",
        "kickoffUTC": "2026-10-10T22:30:00Z",
        "priceObservationTimestampVerified": True,
        "independentUnfilteredBookmakerUniverseVerified": True,
        "priceUniverse": "INDEPENDENT_VERIFIED_BOOKMAKER_OFFERS",
        "bookmaker": "SyntheticBookmaker",
        "bookmakerMarketSelectionId": "synthetic-pick-a",
        "sourceGenerationId": "synthetic-generation-a",
        "odd": 2.05,
    }


class QuoteIntegrityTests(unittest.TestCase):
    def test_individually_verified_observed_quote(self):
        self.assertIsNone(verified_offer_reason(quote()))

    def test_prepared_selections_do_not_prove_quote_timestamp(self):
        q = quote()
        q.update(priceObservationTimestampVerified=False,
                 independentUnfilteredBookmakerUniverseVerified=False,
                 priceUniverse="LEGACY_PREPARED_SELECTIONS")
        self.assertEqual(verified_offer_reason(q),
                         "UNVERIFIED_INDIVIDUAL_BOOKMAKER_QUOTE_OR_PRICE_UNIVERSE")

    def test_reject_fake_cutoff_as_quote_time(self):
        q = quote()
        q["quoteObservedAt"] = "2026-10-10T22:30:00Z"
        self.assertEqual(verified_offer_reason(q),
                         "QUOTE_OBSERVED_AFTER_CUTOFF_OR_KICKOFF")

    def test_missing_bookmaker_and_generation(self):
        for key in ("bookmaker", "bookmakerMarketSelectionId", "sourceGenerationId"):
            q = quote()
            q[key] = ""
            self.assertEqual(verified_offer_reason(q),
                             "MISSING_BOOKMAKER_SELECTION_OR_SOURCE_GENERATION")

    def test_reject_naive_timestamp_and_old_quote(self):
        q = quote()
        q["quoteObservedAt"] = "2026-10-10T20:00:00"
        self.assertEqual(verified_offer_reason(q),
                         "MISSING_ABSOLUTE_QUOTE_OBSERVATION_OR_KICKOFF")
        q["quoteObservedAt"] = "2026-10-08T20:00:00Z"
        self.assertEqual(verified_offer_reason(q),
                         "STALE_OR_FUTURE_QUOTE_OBSERVATION")

    def test_infinite_or_below_minimum_price_never_accepted(self):
        for odd in (.99, 1.8, 3, 4, float("nan"), float("inf")):
            q = quote()
            q["odd"] = odd
            self.assertEqual(verified_offer_reason(q),
                             "INVALID_EXACT_BOOKMAKER_ODD")


if __name__ == "__main__":
    unittest.main()
