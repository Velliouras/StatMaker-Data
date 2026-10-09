"""Assert the long-running settlement publisher cannot replace the UAT Daily ledger."""
from pathlib import Path
import unittest
ROOT=Path(__file__).resolve().parents[1]
LIVE=(ROOT/".github/workflows/live-settlement-publisher.yml").read_text()
UAT=(ROOT/".github/workflows/canonical-uat-daily-multimarket-hotfix.yml").read_text()

class UatLedgerWriterOwnershipTest(unittest.TestCase):
    def test_settlement_workflow_does_not_publish_uat_ledger(self):
        self.assertNotIn("data/statmaker/canonical_recommendation_ledger_uat_hybrid.json",LIVE)
        self.assertNotIn("materialize_canonical_recommendation_ledger.py --uat-hybrid",LIVE)
        self.assertNotIn("reconcile_canonical_ledger_identity.py --uat-hybrid",LIVE)
    def test_uat_has_own_fast_publication_workflow(self):
        self.assertIn("canonical_recommendation_ledger_uat_hybrid.json",UAT)
        self.assertIn("UAT_VALUE_FIRST_LEDGER_EMPTY_REFUSING_PUBLISH",UAT)
    def test_uat_refresh_is_scheduled_without_heavy_publisher(self):
        self.assertIn("cron:",UAT)
        self.assertIn("--uat-hybrid --backfill-dates 0",UAT)

if __name__=='__main__':unittest.main()
