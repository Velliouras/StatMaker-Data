from datetime import datetime, timezone, timedelta
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import refresh_primary_odds as p

NOW=datetime(2026,10,26,12,tzinfo=timezone.utc)
FIXTURE=dict(id=10,leagueId=39,season=2026,kickoff='2026-10-27T12:00:00Z',home='Alpha',away='Beta',homeId=1,awayId=2)
MATCH=dict(id='70000001',homeTeam='Alpha',awayTeam='Beta',kickoff=FIXTURE['kickoff'],
           canonicalHomeTeam='Alpha',canonicalAwayTeam='Beta',teamMappingStatus='matched',usableForStats=True)


def quotes(book='Bet365',family='MATCH_SHOTS',price='1.91',provider='api-football'):
    return [dict(market=family,selection='Shots '+side+' 20.5',line=20.5,odds=float(price),bookmaker=book,exactBookmakerOdds=True,oddsProvider=provider) for side in ['Over','Under']]


class PrimaryOddsTest(unittest.TestCase):
    def test_exact_prices_identity_and_semantics_are_retained(self):
        bet=dict(id=211,name='Total Shots',values=[dict(value='Over 20.5',odd='1.91'),dict(value='Under 20.5',odd='1.80')])
        out=p.normalize_bet(bet,'Bet365',MATCH,FIXTURE,NOW.isoformat(),NOW.isoformat())
        self.assertEqual([x['odds'] for x in out],[1.91,1.8])
        self.assertEqual({x['apiFootballFixtureId'] for x in out},{'10'})
        self.assertEqual({x['oddsProvider'] for x in out},{'api-football'})
        self.assertTrue(all(x['exactBookmakerOdds'] for x in out))

    def test_player_props_name_mismatch_asian_and_integer_goals_are_excluded(self):
        for ident,name,value in [(276,'Away Player Shots Total','Over 8.5'),(211,'Player Total Shots','Over 8.5'),(5,'Goals Over/Under','Over 2.25'),(5,'Goals Over/Under','Over 2')]:
            self.assertEqual(p.normalize_bet(dict(id=ident,name=name,values=[dict(value=value,odd='1.9')]),'Bet365',MATCH,FIXTURE,'x','x'),[])

    def test_half_period_and_team_side_do_not_become_full_match(self):
        q=p.normalize_bet(dict(id=105,name='Home Team Total Goals(1st Half)',values=[dict(value='Over 0.5',odd='2.1')]),'Bet365',MATCH,FIXTURE,'x','x')[0]
        self.assertEqual((q['market'],q['team']),('TEAM_FIRST_HALF_GOALS','Alpha'))
        self.assertIn('Alpha Goals Over',q['selection'])

    def test_primary_complete_family_beats_more_expensive_unibet(self):
        out=p.choose(quotes(),quotes('Unibet',price='2.2',provider='odds-api-io'))
        self.assertEqual({x['bookmaker'] for x in out},{'Bet365'})
        self.assertEqual({x['odds'] for x in out},{1.91})

    def test_incomplete_primary_falls_back_to_whole_unibet_family(self):
        out=p.choose(quotes()[:1],quotes('Unibet',provider='odds-api-io'))
        self.assertEqual({x['bookmaker'] for x in out},{'Unibet'})
        self.assertEqual(len(out),2)

    def test_legacy_bet365_and_stale_primary_are_not_relabelled_as_fresh(self):
        self.assertEqual(p.choose([],quotes(provider='odds-api-io')),[])
        q={**quotes()[0], 'apiFootballFixtureId':'10','oddsObservedAt':(NOW-timedelta(hours=25)).isoformat()}
        self.assertEqual(p.cached_primary({**MATCH,'markets':[q]},None,FIXTURE,NOW),[])
        q['oddsObservedAt']=NOW.isoformat()
        self.assertEqual(p.cached_primary({**MATCH,'markets':[q]},None,{**FIXTURE,'id':11},NOW),[])

    def test_both_numeric_team_ids_and_kickoff_are_checked(self):
        index=p.FixtureIndex.__new__(p.FixtureIndex);index.by_code={'E0':{'10':FIXTURE}}
        match={**MATCH,'homeTeamLogo':'https://media.api-sports.io/football/teams/1.png','awayTeamLogo':'https://media.api-sports.io/football/teams/2.png'}
        self.assertEqual(index.resolve('E0',match)['id'],10)
        self.assertIsNone(index.resolve('E0',{**match,'awayTeamLogo':'https://media.api-sports.io/football/teams/3.png'}))
        self.assertIsNone(index.resolve('E0',{**match,'kickoff':'2026-10-28T12:00:00Z'}))
        self.assertIsNone(index.resolve('E1',match))

    def test_successful_empty_response_removes_withdrawn_primary(self):
        index=p.FixtureIndex.__new__(p.FixtureIndex);index.by_code={'E0':{'10':FIXTURE}}
        index.policy={'E0':dict(leagueId=39,season=2026,tier='primary')}
        old=[{**q,'apiFootballFixtureId':'10','oddsObservedAt':NOW.isoformat()} for q in quotes()]
        feed=dict(leagues=[dict(leagueCode='E0',matches=[{**MATCH,'markets':old}])])
        class Response(io.BytesIO):headers={}
        response=lambda *a,**k:Response(json.dumps(dict(response=[],paging=dict(total=1))).encode())
        with patch.object(p.time,'sleep'):
            r=p.refresh([feed],[{}],index,{},'key',NOW,opener=response)
        self.assertEqual(r['apiCalls'],1)
        self.assertEqual(feed['leagues'][0]['matches'][0]['markets'],[])

    def test_cap_and_shared_budget_stop_preserve_existing_verified_quote(self):
        index=p.FixtureIndex.__new__(p.FixtureIndex);index.by_code={'E0':{'10':FIXTURE}}
        index.policy={'E0':dict(leagueId=39,season=2026,tier='primary')}
        old=[{**q,'apiFootballFixtureId':'10','oddsObservedAt':NOW.isoformat()} for q in quotes()]
        for cap in (0,1):
            feed=dict(leagues=[dict(leagueCode='E0',matches=[{**MATCH,'markets':old}])])
            BudgetStop=type('BudgetStop',(SystemExit,),{})
            with patch.object(p.time,'sleep'),patch.object(p,'urlopen') as unused:
                def blocked(*a,**k):raise BudgetStop()
                r=p.refresh([feed],[{}],index,{},'key',NOW,max_requests=cap,opener=blocked)
            self.assertEqual(len(feed['leagues'][0]['matches'][0]['markets']),2)
            self.assertEqual(r['apiCalls'],cap)


if __name__=='__main__':unittest.main()
