"""Provider coverage must be scoped to verified league + season and budget."""
from datetime import datetime,timezone
from pathlib import Path
import sys
import unittest
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import api_football_stats_coverage_gate as coverage

LEAGUE={"api_football_league_id":183,"season":"2026"}

def official(value, league_id=183, season=2026):
    return {"errors":[],"response":[{"league":{"id":league_id},
        "seasons":[{"year":season,"coverage":{"fixtures":{
            "statistics_fixtures":value}}}]}]}

class VerifiedCoverageTests(unittest.TestCase):
    def test_known_unsupported_prevents_stats_requests_and_is_cached(self):
        state={"count":0}
        with mock.patch.object(coverage.api,"api_get",return_value=official(False)) as get:
            ok,saved,reason=coverage.read_statistics_coverage(LEAGUE,None,"key",state,5)
        self.assertFalse(ok)
        self.assertEqual(reason,"OFFICIAL_STATISTICS_NOT_COVERED")
        self.assertEqual(saved["leagueId"],183)
        self.assertEqual(saved["season"],2026)
        self.assertEqual(get.call_args.args[1],"leagues")
        with mock.patch.object(coverage.api,"api_get",side_effect=AssertionError("must be cached")):
            check,entry,msg=coverage.read_statistics_coverage(LEAGUE,saved,"key",state,5)
        self.assertFalse(check)
        self.assertEqual(msg,"OFFICIAL_CACHED_COVERAGE")
    def test_true_coverage_does_not_fake_metric_values(self):
        with mock.patch.object(coverage.api,"api_get",return_value=official(True)):
            yes,entry,reason=coverage.read_statistics_coverage(LEAGUE,None,"key",{"count":0},5)
        self.assertTrue(yes)
        self.assertNotIn("HC",entry)
        self.assertEqual(reason,"OFFICIAL_STATISTICS_AVAILABLE")
    def test_mismatched_provider_identity_fails_open_and_not_cached(self):
        with mock.patch.object(coverage.api,"api_get",return_value=official(False,league_id=99)):
            result,entry,reason=coverage.read_statistics_coverage(LEAGUE,None,"key",{"count":0},5)
        self.assertIsNone(result)
        self.assertEqual(reason,"UNVERIFIED_PROVIDER_LEAGUE_ID")
        self.assertIsNone(entry)
    def test_same_league_other_season_does_not_reuse_coverage(self):
        now=datetime(2026,10,13,12,tzinfo=timezone.utc)
        old={"leagueId":183,"season":2025,"statisticsCovered":False,
             "checkedAtUtc":"2026-10-12T12:00:00Z"}
        self.assertIsNone(coverage.cached_coverage_status(LEAGUE,old,now))
    def test_stale_negative_coverage_is_refreshed(self):
        now=datetime(2026,10,13,12,tzinfo=timezone.utc)
        old={"leagueId":183,"season":2026,"statisticsCovered":False,
             "checkedAtUtc":"2026-10-01T12:00:00Z"}
        self.assertIsNone(coverage.cached_coverage_status(LEAGUE,old,now))
    def test_empty_provider_response_does_not_mean_no_coverage(self):
        with mock.patch.object(coverage.api,"api_get",return_value={"errors":[],"response":[]}):
            result,entry,reason=coverage.read_statistics_coverage(LEAGUE,None,"key",{"count":0},5)
        self.assertIsNone(result)
        self.assertEqual(reason,"UNVERIFIED_PROVIDER_LEAGUE_ID")
    def test_request_limit_no_extra_api_calls(self):
        with mock.patch.object(coverage.api,"api_get",side_effect=AssertionError("No budget")):
            result,entry,reason=coverage.read_statistics_coverage(LEAGUE,None,"key",{"count":5},5)
        self.assertIsNone(result)
        self.assertEqual(reason,"REQUEST_BUDGET_EXHAUSTED_BEFORE_COVERAGE")


if __name__=="__main__":
    unittest.main()
