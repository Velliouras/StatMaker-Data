import pathlib
import unittest

ROOT=pathlib.Path(__file__).resolve().parents[1]
SCRIPT=(ROOT/'scripts/materialize_canonical_recommendation_ledger.py').read_text()
SETTLEMENT=(ROOT/'.github/workflows/live-settlement-publisher.yml').read_text()
PUBLISHER=(ROOT/'.github/workflows/canonical-prod-value-first-publisher.yml').read_text()

class ProdValueFirstPromotionTest(unittest.TestCase):
    def test_cohort_cutover_is_explicit(self):
        self.assertIn("PROD_VALUE_FIRST_START='2026-10-09'",SCRIPT)
        self.assertIn("legacy-pre-2026-10-09",SCRIPT)
        self.assertIn("policyCohort='value-first-v15'",SCRIPT)
        self.assertIn("--prod-value-first",PUBLISHER)
    def test_settlement_does_not_overwrite_prod_model_ledger(self):
        self.assertNotIn('python scripts/materialize_canonical_recommendation_ledger.py --backfill-dates 30',SETTLEMENT)
        self.assertNotIn('            data/statmaker/canonical_recommendation_ledger.json',SETTLEMENT)
    def test_prod_publisher_owns_only_prod_ledger(self):
        self.assertIn('--prod-value-first --backfill-dates 0',PUBLISHER)
        self.assertIn('PROD_VALUE_FIRST_LEDGER_EMPTY',PUBLISHER)
        self.assertNotIn('canonical_recommendation_ledger_uat_hybrid.json',PUBLISHER)

if __name__=='__main__':
    unittest.main()
