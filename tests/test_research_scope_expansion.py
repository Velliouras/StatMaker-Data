import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from betting_v2.research_scope_expansion import expanded_spec,app_label,feed_bindings,select_scope,write_gz,collect
from betting_v2.research_quotes import one_fixture_receipt,feature_cutoff

class ExpandedScopeTest(unittest.TestCase):
    def offer(self,name,line=22.5):return dict(providerMarketName=name,selection='Over',line=line)
    def test_adds_only_full_time_foul_totals_with_known_team_side(self):
        event=dict(home='a',away='b')
        self.assertEqual(expanded_spec(self.offer('Total Fouls'),event),('FOULS','TOTAL','OVER','',22.5))
        self.assertEqual(expanded_spec(self.offer('Home Team Fouls'),event),('FOULS','TEAM','OVER','HOME',22.5))
        for name in ['Player Fouls','Fouls 1H','Fouls HT','HT Fouls','First Half Fouls','Fouls Handicap']:
            self.assertIsNone(expanded_spec(self.offer(name),event))
    def test_cards_and_ambiguous_prices_do_not_become_scored_totals(self):
        event=dict(home='a',away='b')
        self.assertIsNone(expanded_spec(self.offer('Bookings Totals'),event))
        self.assertIsNone(expanded_spec(self.offer('Total Fouls','junk'),event))
        self.assertIsNone(expanded_spec(self.offer('Total Fouls',22.25),event))
    def test_packet_choices_never_mix_fixture_receipts(self):
        rows=[dict(fixture='f',observed=t,sourceReceipt=str(t)) for t in [2,1,3]]
        self.assertEqual([q['observed'] for q in one_fixture_receipt(rows)],[1])
        self.assertEqual([q['observed'] for q in one_fixture_receipt(rows,latest=True)],[3])
        self.assertLessEqual(feature_cutoff(3*86400+1),3*86400+1-86400)
    def test_all_app_catalogue_families_can_be_inventoried_even_if_not_scored(self):
        self.assertEqual(app_label(self.offer('Correct Score'),None),'Correct Score')
        self.assertEqual(app_label(self.offer('Most Shots on Target'),None),'Most Shots on Target')
        self.assertEqual(app_label(self.offer('Total Fouls'),('FOULS','TOTAL','OVER','',22.5)),'Fouls')
    def test_aliases_require_existing_explicit_approved_canonical_bindings(self):
        import tempfile,json
        from unittest.mock import patch
        import materialize_betting_v2_direct as direct
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);p=root/'odds/odds_api_io';p.mkdir(parents=True)
            match=dict(id='1',kickoff='2026-10-13T12:00:00Z',teamMappingStatus='matched',usableForStats=True,
                canonicalHomeTeam='Everton',canonicalAwayTeam='Chelsea',providerHomeTeam='Everton FC',providerAwayTeam='Chelsea FC')
            (p/'domestic_odds.json').write_text(json.dumps(dict(leagues=[dict(leagueCode='E0',matches=[match])])))
            with patch.dict(direct.UEFA_ALIASES,{'E0':{'everton':'england:everton','chelsea':'england:chelsea'}},clear=True):
                _,aliases=feed_bindings(root,lambda _:set())
            self.assertEqual(aliases['evertonfc'],{'england:everton'})
            match['usableForStats']=False;(p/'domestic_odds.json').write_text(json.dumps(dict(matches=[match],leagueCode='E0')))
            _,aliases=feed_bindings(root,lambda _:set());self.assertEqual(dict(aliases),{})
    def test_combined_result_total_cannot_duplicate_either_broad_family(self):
        def row(scope,confidence):return dict(fixture='f',spec=('GOALS_FT',scope,'HOME_OVER' if scope=='RESULT_TOTAL' else 'OVER','',2.5),
            preference=1,confidence=confidence,p=confidence,ev=.2,bookmaker='b')
        rows=[row('RESULT_TOTAL',.8),row('TOTAL',.75),row('RESULT',.7)]
        self.assertEqual(len(select_scope(rows)),1)
    def test_gzip_checkpoint_closes_and_roundtrips_before_replacement(self):
        import tempfile,gzip,json
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'q.json.gz';value=[dict(a='b',spec=('GOALS_FT','RANGE','IN','','2:3'))]*100
            write_gz(path,value);self.assertEqual(json.loads(gzip.decompress(path.read_bytes())),json.loads(json.dumps(value)))
    def test_full_receipt_collection_keeps_match_prices_and_traces_prior_player_picks(self):
        import tempfile,json,gzip
        from unittest.mock import patch
        import materialize_betting_v2_direct as direct
        from betting_v2.opponent_matchup import timestamp
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);p=root/'reports/betting_v2/quote_receipts';p.mkdir(parents=True);out=root/'out';out.mkdir()
            (root/'odds/odds_api_io').mkdir(parents=True)
            event=dict(id='1',home='Everton',away='Chelsea')
            (p/'1.json.gz').write_bytes(gzip.compress(json.dumps(dict(boundedMarketSnapshot=[dict(event=event)])).encode()))
            offer=dict(providerEventId='1',kickoffUTC='2026-10-14T12:00:00Z',clientReceivedAtUTC='2026-10-13T12:00:00Z',bookmaker='b',odd=2.1)
            offers=[dict(offer,providerMarketName='ML',selection='HOME',line=None),dict(offer,providerMarketName='To Score 2+ Goals',selection='OVER',line=1.5)]
            old=root/'reports/betting_v2/oof_research_20261016';old.mkdir()
            pick=dict(sourceReceipt='1.json.gz',providerEventId='1',bookmaker='b',spec=['GOALS_FT','TOTAL','OVER','',1.5],odd=2.1,fixture='fixture')
            (old/'goal_factorized_priced_replay.json').write_text(json.dumps(dict(reports={'old':dict(picks=[pick])})))
            history=[dict(_date=timestamp(offer['kickoffUTC']),_home='england:everton',_away='england:chelsea')]
            with patch.dict(direct.UEFA_ALIASES,{'E0':{'everton':'england:everton','chelsea':'england:chelsea'}},clear=True),patch('betting_v2.research_scope_expansion.extracted_offers',return_value=(offers,{})):
                coverage=collect(root,out,history,timestamp('2026-10-16'))
            self.assertEqual(coverage['counters']['excludedPlayerScorerOffers'],1)
            self.assertEqual(coverage['rawMarkets']['To Score 2+ Goals']['mappedOffers'],0)
            self.assertEqual(len(json.loads(gzip.decompress((out/'quotes_latest.json.gz').read_bytes()))),1)
            self.assertEqual(json.loads((out/'prior_selected_player_prop_audit.json').read_text())['affectedUniqueFixtures'],1)
    def test_shared_classifier_does_not_confuse_players_fouls_or_half_time_result(self):
        from odds_api_io_market_audit import classify_provider_market
        cases={'To Score 2+ Goals':'PLAYER_PROPS','To Score 3+ Goals':'PLAYER_PROPS',
            'Total Fouls':'MATCH_FOULS','Total Fouls Home':'TEAM_FOULS','Player Fouls':'PLAYER_PROPS',
            'Fouls HT':'UNSUPPORTED','ML HT':'HALF_TIME_RESULT'}
        for raw,expected in cases.items():self.assertEqual(classify_provider_market(raw)['family'],expected,raw)
if __name__=='__main__':unittest.main()
