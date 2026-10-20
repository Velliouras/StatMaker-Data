import copy,json,tempfile,unittest,hashlib,gzip
from pathlib import Path
from unittest.mock import patch
from . import build
from .policy import decision,statistics


def records(n,wins=None,first=1000000.):
    wins=n if wins is None else wins
    return [dict(id=str(i),fixture=str(i),kickoff=first+i*86400,published=first+i*86400-100,
                 outcome="WON" if i<wins else "LOST",p=.65,push=0.,odd=1.91,returned=1.91 if i<wins else 0.) for i in range(n)]


class MarketPolicyTest(unittest.TestCase):
    def setUp(self):self.cfg=build.config()

    def test_one_lucky_win_does_not_activate_a_market(self):
        self.assertEqual(decision(records(1),None,self.cfg,"MATCH_CORNERS",1e9)["status"],"SHADOW")

    def test_many_lines_of_one_fixture_do_not_supply_fifty_fixtures(self):
        rows=records(100)
        for r in rows:r["fixture"]="same"
        self.assertEqual(decision(rows,None,self.cfg,"MATCH_CORNERS",1e9)["status"],"SHADOW")

    def test_profitable_paper_cohort_can_activate_supported_market(self):
        result=decision(records(50),None,self.cfg,"MATCH_CORNERS",1e9)
        self.assertEqual(result["status"],"ACTIVE");self.assertTrue(result["transition"])

    def test_positive_development_history_cannot_replace_a_live_model(self):
        self.assertEqual(decision(records(100),None,self.cfg,"MATCH_CARDS",1e9)["status"],"BLOCKED_CONTRACT")

    def test_pending_is_excluded_from_roi(self):
        row=records(1)[0];row.update(outcome="PENDING",returned=None)
        self.assertIsNone(statistics([row])["roi"])

    def test_new_losing_results_pause_active_market_and_preserve_data(self):
        rows=records(100,80);before=copy.deepcopy(rows)
        d=decision(rows,dict(status="ACTIVE"),self.cfg,"MATCH_SHOTS",1e9)
        self.assertEqual(d["status"],"PAUSED");self.assertEqual(rows,before)

    def test_paused_market_needs_new_forecasts_not_reusing_old_wins(self):
        prev=dict(status="PAUSED",pausedAt=1e9,settledAtPause=50)
        self.assertEqual(decision(records(80),prev,self.cfg,"MATCH_CORNERS",1e9+8*86400)["status"],"PAUSED")

    def test_recovery_in_paper_can_resume_without_erasing_losses(self):
        pause=1e8;rows=records(50,40,first=pause-100*86400)
        new=records(20,first=pause+86400)
        for r in new:r["id"]="new"+r["id"];r["fixture"]="new"+r["fixture"]
        d=decision(rows+new,dict(status="PAUSED",pausedAt=pause,settledAtPause=50),self.cfg,"MATCH_CORNERS",pause+30*86400)
        self.assertEqual(d["status"],"ACTIVE");self.assertEqual(d["total"]["losses"],10)

    def test_late_publication_never_enters_performance(self):
        r=records(1)[0];r.update(observed=r["published"]-1)
        self.assertTrue(build.valid_contract(r,self.cfg));r["published"]=r["kickoff"]
        self.assertFalse(build.valid_contract(r,self.cfg))

    def test_stale_state_stops_routing(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);(p/"state.json").write_text(json.dumps(dict(generatedAt="2026-10-01T00:00:00Z",markets=dict(SHOTS=dict(status="ACTIVE")))))
            with patch.object(build,"OUT",p):self.assertFalse(build.allowed("MATCH_SHOTS",1792454400))

    def test_bankroll_curve_includes_first_selection(self):
        s=statistics(records(2));self.assertAlmostEqual(s["profitUnits"],1.82)
        self.assertAlmostEqual(s["curve"][0]["profit"],.91)

    def test_pause_and_resume_update_live_feed_without_rewriting_issued_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);base=root/"data/statmaker/uat/shots_v1/p55";base.mkdir(parents=True)
            issue=dict(selectionId="s",metric="shots",market="MATCH_SHOTS",winProbability=.65,odd=1.91,pushProbability=0.,
                       kickoff="2026-10-25T15:00:00Z",latestQuoteObservedAt="2026-10-20T07:00:00Z")
            payload=dict(issued=[issue],candidates=[issue],candidateCount=1,generationId="g")
            raw=json.dumps(payload).encode();(base/"initial.json").write_bytes(raw)
            (base/"latest.json").write_text(json.dumps(dict(artifact="initial.json",sha256=hashlib.sha256(raw).hexdigest())))
            now=1792483200. # 20 October, within the quote observation day
            with patch.object(build,"ROOT",root),patch.object(build,"allowed",return_value=False):build.refresh_current_feed(now)
            ix=json.loads((base/"latest.json").read_text());paused=json.loads((base/ix["artifact"]).read_text())
            self.assertEqual(paused["candidates"],[]);self.assertEqual(paused["issued"],[issue])
            with patch.object(build,"ROOT",root),patch.object(build,"allowed",return_value=True):build.refresh_current_feed(now)
            ix=json.loads((base/"latest.json").read_text());resumed=json.loads((base/ix["artifact"]).read_text())
            self.assertEqual(resumed["candidates"],[issue]);self.assertEqual(resumed["issued"],[issue])

    def test_fresh_observed_exact_price_can_have_an_old_last_change(self):
        from .paper import observed_receipt_times
        received="2026-10-20T07:00:00Z"
        doc=dict(boundedMarketSnapshot=[dict(event=dict(id=1),bookmakers=[dict(bookmaker="B",markets=[dict(name="Corners Totals",updatedAt="2026-10-01T00:00:00Z",outcomes=[dict(hdp=9.5,over=1.91)])])])])
        raw=gzip.compress(json.dumps(doc).encode())
        from .policy import time
        q=dict(sourceReceipt="r.gz",sourceSha256=hashlib.sha256(raw).hexdigest(),observed=time(received),
               providerEventId="1",bookmaker="B",providerMarketName="Corners Totals",selection="OVER",providerSelectionId="",line=9.5,odd=1.91)
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);(folder/'r.gz').write_bytes(raw)
            with patch('betting_v2.quote_receipt_capture.verify_receipt',return_value=dict(clientReceivedAtUTC=received)):
                rows,audit=observed_receipt_times([q],folder)
                self.assertEqual(len(rows),1);self.assertTrue(rows[0]['verifiedClientReceipt'])
                self.assertEqual(observed_receipt_times([dict(q,odd=2.0)],folder)[0],[])
                with self.assertRaises(ValueError):observed_receipt_times([dict(q,sourceSha256='bad')],folder)


if __name__=="__main__":unittest.main()
