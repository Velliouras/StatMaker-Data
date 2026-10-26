import json
from pathlib import Path
import tempfile
import unittest
from scripts.shots_uat import primary_quotes
from scripts.shots_uat.feed import raw_quotes,bind_quote,favourite
from scripts.shots_uat.repricing import _raw_price


class PrimarySnapshotTest(unittest.TestCase):
    def test_new_source_binds_exact_price_favourite_and_repricing(self):
        now=1793016000.0
        observed='2026-10-26T12:00:00Z';kickoff='2026-10-27T12:00:00Z'
        common=dict(bookmaker='Bet365',exactBookmakerOdds=True,oddsProvider='api-football',apiFootballFixtureId='10',oddsObservedAt=observed,oddsUpdatedAt=observed)
        match=dict(id='70000001',homeTeam='A',awayTeam='B',providerHomeTeam='A FC',providerAwayTeam='B FC',kickoff=kickoff,apiFootballFixtureId='10',markets=[
            dict(common,market='MATCH_SHOTS_ON_TARGET',providerMarketName='Total ShotOnGoal',selection='Shots on Target Over 8.5',line=8.5,odds=1.91),
            dict(common,market='MATCH_SHOTS_ON_TARGET',providerMarketName='Total ShotOnGoal',selection='Shots on Target Under 8.5',line=8.5,odds=1.8),
            *[dict(common,market='1X2',providerMarketName='Match Winner',selection=s,odds=o) for s,o in [('Home',1.8),('Draw',3.7),('Away',4.5)]]] )
        doc=dict(oddsProviderPolicy=primary_quotes.CONTRACT,leagues=[dict(matches=[match])])
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'feed.json';path.write_text(json.dumps(doc))
            raw=primary_quotes.merge({('70000001','Bet365','Old Market'):[{}]},path,now)
            self.assertNotIn(('70000001','Bet365','Old Market'),raw)
            quotes=raw_quotes(match,raw)
            self.assertEqual(len(quotes),2)
            quote=next(q for q in quotes if q['odds']==1.91)
            self.assertEqual(bind_quote(match,quote,raw),('OVER',now))
            self.assertEqual(favourite(match,'Bet365',raw,now),'HOME')
            row=dict(match=match,market='MATCH_SHOTS_ON_TARGET',scope='TOTAL',bookmaker='Bet365',selection='Over 8.5',line=8.5)
            self.assertEqual(_raw_price(row,raw),(1.91,now))
            self.assertEqual(quote['quoteObservationEvidence'],'API_FOOTBALL_NORMALIZED_SNAPSHOT')
            match['apiFootballFixtureId']='11';path.write_text(json.dumps(doc))
            self.assertEqual(primary_quotes.packet(path,now)[0],{})


if __name__=='__main__':unittest.main()
