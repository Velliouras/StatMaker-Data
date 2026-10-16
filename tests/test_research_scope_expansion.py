import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from betting_v2.research_scope_expansion import expanded_spec,app_label
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
if __name__=='__main__':unittest.main()
