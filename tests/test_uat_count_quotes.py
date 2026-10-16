import gzip,json,sqlite3,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from betting_v2.uat_count_quotes import stage
from betting_v2.opponent_matchup import timestamp


class UatCountQuoteTests(unittest.TestCase):
    def test_latest_complete_packet_exact_kickoff_and_no_legacy_mutation(self):
        now=timestamp('2026-10-16T12:00:00Z')
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);receipts=root/'reports/betting_v2/quote_receipts';receipts.mkdir(parents=True)
            for name in ('old','new'): (receipts/(name+'.gz')).write_bytes(gzip.compress(json.dumps(dict(tag=name)).encode()))
            con=sqlite3.connect(':memory:');con.row_factory=sqlite3.Row
            con.execute('CREATE TABLE prepared_fixture_matches (competition_id,snapshot_version,match_key,id,kickoff,date,usable_for_stats,home_team,away_team,provider_home_team,provider_away_team)')
            con.execute('INSERT INTO prepared_fixture_matches VALUES (?,?,?,?,?,?,?,?,?,?,?)',('domestic','s','f','42','2026-10-17T12:00:00Z','2026-10-17',1,'Home','Away','Home','Away'))
            con.execute('CREATE TABLE prepared_betting_v2_quotes (competition_id,snapshot_version,match_key,ordinal,market,selection,team,line,odd,bookmaker,provider_market_name,provider_market_family)')
            con.execute('INSERT INTO prepared_betting_v2_quotes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',('domestic','s','f',0,'1X2','1',None,None,2.,'Bet365','ML','1X2'))
            before=[tuple(r) for r in con.execute('SELECT * FROM prepared_betting_v2_quotes')]
            def flatten(doc):
                obs='2026-10-16T11:00:00Z' if doc['tag']=='new' else '2026-10-16T10:00:00Z'
                q=dict(providerEventId='42',kickoffUTC='2026-10-17T12:00:00Z',clientReceivedAtUTC=obs,
                    providerMarketName='Total Fouls',selection='OVER',line=23.5,odd=2.1 if doc['tag']=='new' else 2.8,bookmaker='Bet365')
                return [q,dict(q,selection='UNDER',odd=1.9),dict(q,providerMarketName='Player Fouls'),
                    dict(q,kickoffUTC='2026-10-17T12:00:00.500Z'),dict(q,providerEventId='99')],{}
            with patch('betting_v2.uat_count_quotes.extracted_offers',side_effect=flatten):result=stage(con,root,now)
            self.assertEqual(result['addedFoulQuotes'],2)
            self.assertEqual(before,[tuple(r) for r in con.execute('SELECT * FROM prepared_betting_v2_quotes')])
            rows=con.execute("SELECT ordinal,selection,odd FROM prepared_betting_v4_quotes WHERE market='MATCH_FOULS' ORDER BY ordinal").fetchall()
            self.assertEqual([tuple(r) for r in rows],[(1,'Over',2.1),(2,'Under',1.9)])

if __name__=='__main__':unittest.main()
