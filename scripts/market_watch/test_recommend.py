import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from . import recommend, router, build
from .policy import decision
from .results import exact_settlement
from .quotes import fresh_uefa_quotes


class FavouritePointPolicyTest(unittest.TestCase):
    def test_favourite_over_wins_the_bayern_gladbach_direction(self):
        self.assertEqual(0, recommend.preference(['GOALS_FT','TEAM','OVER','HOME',4.5], 'HOME'))
        self.assertIsNone(recommend.preference(['GOALS_FT','TEAM','OVER','AWAY',.5], 'HOME'))
        self.assertIsNone(recommend.preference(['SHOTS','TEAM','UNDER','HOME',15.5], 'HOME'))
        self.assertEqual(0, recommend.preference(['CORNERS','TEAM','UNDER','AWAY',4.5], 'HOME'))

    def test_match_goals_btts_and_fouls_do_not_require_favourite_victory(self):
        for spec in (['GOALS_FT','TOTAL','OVER','',2.5], ['GOALS_FT','BTTS','YES','',None],
                     ['CORNERS','TOTAL','UNDER','',10.5], ['FOULS','TEAM','OVER','AWAY',11.5]):
            self.assertEqual(1, recommend.preference(spec, 'HOME'))

    def test_result_markets_cannot_push_the_opposing_underdog(self):
        for spec in (['GOALS_FT','RESULT','AWAY','',None], ['GOALS_FT','DC','AWAY_OR_DRAW','',None],
                     ['GOALS_FT','DNB','AWAY','',None], ['GOALS_FT','RESULT_TOTAL','AWAY_OVER','',2.5]):
            self.assertIsNone(recommend.preference(spec, 'HOME'))
        self.assertEqual(0, recommend.preference(['GOALS_FT','DC','HOME_OR_DRAW','',None], 'HOME'))

    def test_missing_favourite_and_cards_never_invent_evidence(self):
        self.assertIsNone(recommend.preference(['GOALS_FT','TEAM','OVER','HOME',1.5], None))
        self.assertEqual(1, recommend.preference(['GOALS_FT','BTTS','YES','',None], None))
        self.assertIsNone(recommend.preference(['CARDS','TOTAL','OVER','',4.5], 'HOME'))

    def test_connected_uat_pilot_starts_without_fifty_results_or_fourteen_days(self):
        cfg=build.config()
        result=decision([],dict(status='SHADOW'),cfg,'MATCH_CORNERS',1e9)
        self.assertEqual('ACTIVE',result['status'])
        self.assertEqual(0,result['total']['settled'])
        self.assertEqual('BLOCKED_CONTRACT',decision([],None,cfg,'MATCH_CARDS',1e9)['status'])

    def test_pilot_still_pauses_after_a_losing_run(self):
        cfg=build.config();rows=[dict(id=str(i),fixture=str(i),kickoff=1e8+i*86400,published=1e8-1,
            outcome='LOST',p=.65,push=0.,odd=1.91,returned=0.) for i in range(20)]
        self.assertEqual('PAUSED',decision(rows,dict(status='ACTIVE'),cfg,'MATCH_CORNERS',1e9)['status'])

    def test_receipt_favourite_cannot_mix_bookmakers_or_receipts(self):
        rows=[dict(providerEventId='e',bookmaker='B',sourceReceipt='r',providerMarketName='ML',
                   selection=s,odd=o) for s,o in [('HOME',1.5),('DRAW',4.),('AWAY',6.)]]
        self.assertEqual('HOME',recommend.receipt_favourites({'g':dict(rows=rows)})['e','B','r'])
        rows[2]['sourceReceipt']='other'
        self.assertEqual({},recommend.receipt_favourites({'g':dict(rows=rows)}))

    def test_point_model_is_used_without_conditioning_btts_on_winner(self):
        now=1792692000.;ko=now+3600
        match=dict(id='e',kickoff=ko,providerHomeTeam='H',providerAwayTeam='A')
        league=dict(matches=[match],leagueCode='E0')
        quote=dict(providerEventId='e',kickoff=ko,observed=now-60,bookmaker='B',sourceReceipt='r',
            providerMarketName='Both Teams To Score',selection='YES',line=None,odd=1.91,
            providerSelectionId='s',_providerHome='H',_providerAway='A',sourceSha256='a'*64)
        cfg=build.config()
        class Identity:
            def __init__(self,*args):pass
            def bind(self,l,m):return (m,'H','A',42),'exact'
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'config').mkdir()
            (root/'config/market_watch_all_source.json').write_text(json.dumps(dict(files={})))
            with patch.object(recommend,'ROOT',root),patch.object(recommend,'OUT',root/'out'), \
                 patch.object(recommend,'config',return_value=cfg), \
                 patch.object(recommend,'current_quotes',return_value={'q':dict(rows=[quote])}), \
                 patch('scripts.shots_uat.sources.Identities',Identity), \
                 patch('betting_v2.live_revised.load_revised',return_value=(object(),{},{})), \
                 patch('scripts.market_watch.all_paper.bank',return_value=[({}, {}, object())]), \
                 patch('betting_v2.exact_forecast_adapter.forecast',return_value=dict(
                     winProbability=.65,pushProbability=0.,scenarios={'HOME':dict(winProbability=.1)})):
                first=recommend.candidates([],now,[league],set())
                self.assertEqual(1,len(first));self.assertEqual(.65,first[0]['_monitorP'])
                quote['odd']=1.95
                second=recommend.candidates([],now,[league],set())
                self.assertEqual(1.95,second[0]['odd'])
                self.assertEqual([],recommend.candidates([],now,[league],{('42','GOALS')}))

    def test_routing_keeps_one_issue_per_family_and_preserves_existing_rows(self):
        from datetime import datetime,timezone
        now=1792692000.;ko=now+3600;iso=lambda t:datetime.fromtimestamp(t,timezone.utc).isoformat()
        match=dict(id='e',kickoff=iso(ko),homeTeam='H',awayTeam='A',providerHomeTeam='H',providerAwayTeam='A')
        league=dict(matches=[match],leagueCode='E0',country='England',competition='Premier',season='2026')
        base=dict(providerEventId='e',kickoff=ko,observed=now-60,featureCutoff=now-86400,
            bookmaker='B',odd=1.91,_home='H',_away='A',_favourite='HOME',_monitorP=.65,
            _monitorPush=0.,_monitorModel='ALL_MARKETS_JOINT_V1',_monitorPolicy=recommend.POLICY,
            _monitorId='id',sourceMarket='BTTS',preference=1,ev=.2415)
        options=[dict(base,spec=['GOALS_FT','TOTAL','OVER','',2.5],appMarket='MATCH_GOALS'),
                 dict(base,spec=['GOALS_FT','BTTS','YES','',None],appMarket='BTTS'),
                 dict(base,spec=['GOALS_FT','DC','HOME_OR_DRAW','',None],appMarket='DOUBLE_CHANCE')]
        class Identity:
            def __init__(self,*args):pass
            def bind(self,l,m):return (m,'H','A',42),'exact'
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);out=root/'out';out.mkdir()
            with patch.object(router,'ROOT',root),patch.object(router,'OUT',out), \
                 patch.object(router,'allowed',return_value=True), \
                 patch.object(router,'current_quotes',return_value={}),patch.object(router,'bind',return_value=now-30), \
                 patch('scripts.shots_uat.sources.Identities',Identity), \
                 patch.object(recommend,'candidates',return_value=options):
                router.stage([],now,[league],{})
                before=json.loads((out/'market_ledger.json').read_text())
                self.assertEqual(2,len(before['rows']))
                router.stage([],now+1,[league],{})
                self.assertEqual(before,json.loads((out/'market_ledger.json').read_text()))


class ExactRoutedSettlementTest(unittest.TestCase):
    def test_uefa_quotes_include_goals_btts_and_corners_only_from_fresh_exact_polls(self):
        from .policy import time
        now=time('2026-10-21T19:05:00Z')
        doc=dict(generatedAt='2026-10-21T14:00:00Z',debug=dict(oddsRefreshSkippedByRateLimit=False,
            preservedPreviousExactMarketRows=0,apiCalls=[dict(status=200,path='/odds',params=dict(eventIds='e'))]),
            matches=[dict(id='e',kickoff='2026-10-22T19:00:00Z',providerHomeTeam='H',providerAwayTeam='A',markets=[
                dict(market='BTTS',selection='Yes',odds=1.91,bookmaker='B',providerMarketName='Both Teams To Score',exactBookmakerOdds=True),
                dict(market='TEAM_TOTAL_GOALS',selection='H Over 1.5',line=1.5,odds=1.91,bookmaker='B',providerMarketName='Total Goals Home',exactBookmakerOdds=True),
                dict(market='MATCH_CORNERS',selection='Under 10.5',line=10.5,odds=1.91,bookmaker='B',providerMarketName='Corners Totals',exactBookmakerOdds=True)])])
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'europa_league_odds.json';path.write_text(json.dumps(doc))
            rows=fresh_uefa_quotes(path,now)
            self.assertEqual(['YES','OVER','UNDER'],[q['selection'] for q in rows])
            self.assertTrue(all(q['verifiedClientReceipt'] is False for q in rows))
            doc['debug']['preservedPreviousExactMarketRows']=1;path.write_text(json.dumps(doc))
            self.assertEqual([],fresh_uefa_quotes(path,now))
            doc['debug']['preservedPreviousExactMarketRows']=0;doc['debug']['apiCalls'][0]['status']=429;path.write_text(json.dumps(doc))
            self.assertEqual([],fresh_uefa_quotes(path,now))

    def test_goals_and_corners_read_exact_cache_without_history_fixture_id(self):
        now=1792692000.;kickoff='2026-10-21T01:00:00Z'
        issue=dict(apiFixtureId=42,kickoff=kickoff)
        data=dict(generatedAt='2026-10-21T06:00:00Z',fixtures=[dict(fixtureId=42,kickoff=kickoff,status='FT',
            homeGoals=2,awayGoals=1,normalizedStats=dict(HC=4,AC=3))])
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'data/statmaker/live_settlements.json';path.parent.mkdir(parents=True)
            path.write_text(json.dumps(data))
            self.assertEqual((1.,0.),exact_settlement(issue,['GOALS_FT','TOTAL','OVER','',2.5],[],root,now)[0])
            self.assertEqual((1.,0.),exact_settlement(issue,['CORNERS','TOTAL','OVER','',6.5],[],root,now)[0])
            self.assertIsNone(exact_settlement(issue,['GOALS_1H','TOTAL','OVER','',.5],[],root,now))
            self.assertIsNone(exact_settlement(dict(issue,apiFixtureId=43),['GOALS_FT','TOTAL','OVER','',2.5],[],root,now))
            data['fixtures'][0]['status']='AET';path.write_text(json.dumps(data))
            self.assertIsNone(exact_settlement(issue,['GOALS_FT','TOTAL','OVER','',2.5],[],root,now))


if __name__=='__main__':unittest.main()
