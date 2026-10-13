"""No shortcut around true point-in-time receipts, attested odds or lineups."""
from collections import Counter
from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.betting_v2.phase2_prod_readiness_gate import source_truth


class ProducerReadinessGateTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root=Path(t.name)
        p=self.root/"reports/betting_v2"
        p.mkdir(parents=True,exist_ok=True)
        (p/"quote_receipt_index.json").write_text(json.dumps({
            "contract":"betting-v2-client-quote-index-v1","verifiedSnapshotReceipts":712,
            "completeSnapshotReceipts":400,"preKickoffPriceRows":581049,
            "hasPricedProfitCertificate":False}),encoding="utf-8")
        (p/"REAL_HOLDOUT_EVIDENCE_AUDIT_2026-10-11.json").write_text(json.dumps({
            "contract":"betting-v2-real-holdout-evidence-audit-v1",
            "bookmakerPriceAudit":{
                "individualPriceObservationTimestampVerified":False,
                "independentlyCompletePriceUniverseVerified":False,
                "pricedROIProven":False}}),encoding="utf-8")
    def test_many_quote_snapshots_do_not_fake_approved_profitability(self):
        with patch("scripts.betting_v2.phase2_prod_readiness_gate.cached_history",
                   return_value=([],[],Counter({"MISSING_POINT_IN_TIME_STATISTICS_RECEIPT":1000}))):
            result=source_truth(self.root)
        self.assertEqual(result["decision"],"BLOCKED")
        self.assertEqual(result["facts"]["verifiedQuoteSnapshotReceipts"],712)
        self.assertIn("NO_POINT_IN_TIME_PROVIDER_STATISTICS_TRAINING_ROWS",
                      result["blockingRequirements"])
        self.assertIn("NO_INDEPENDENT_PRICED_ROI_CERTIFICATE",
                      result["blockingRequirements"])
        self.assertEqual(result["strongPublished"],0)
    def test_real_statistics_rows_alone_do_not_override_missing_priced_holdout(self):
        with patch("scripts.betting_v2.phase2_prod_readiness_gate.cached_history",
                   return_value=([{"fixture_id":"1"}],[],Counter())):
            result=source_truth(self.root)
        self.assertEqual(result["decision"],"BLOCKED")
        self.assertNotIn("NO_POINT_IN_TIME_PROVIDER_STATISTICS_TRAINING_ROWS",
                         result["blockingRequirements"])
        self.assertIn("NO_PROVEN_INDEPENDENT_PROFITABILITY_BOUND",
                      result["blockingRequirements"])
    def test_mismatched_audit_contract_fails_closed(self):
        p=self.root/"reports/betting_v2/quote_receipt_index.json"
        d=json.loads(p.read_text(encoding="utf-8"))
        d["contract"]="unknown"
        p.write_text(json.dumps(d),encoding="utf-8")
        with self.assertRaisesRegex(ValueError,"Unknown quote"):
            source_truth(self.root)

if __name__=="__main__":
    unittest.main()
