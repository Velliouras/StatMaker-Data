import copy
import unittest
from betting_v2.research_csv_prices import parse_csv, row_quotes, parse_date, number, join_rows, validate_input, replay, recover_schedule_aliases
from betting_v2.opponent_matchup import timestamp


def fixture():
    row = dict(metric='GOALS_FT',regime='ELO_NO_XG',means=[1.8,.7],samples=[20,20])
    return dict(fixture='f',league='E0',utcDate='2026-08-21',date=timestamp('2026-08-21T19:00:00Z'),
                featureCutoff=timestamp('2026-08-21'),home='england:arsenal',away='england:chelsea',
                actual=[2,0],predictions={'full':row,'separate_ft_selected_period':copy.deepcopy(row)})


def document():
    return dict(schema=1,testLabelsUsedForSelection=False,untouchedHoldout=False,testFrom='2026-08-01',testBefore='2026-09-01',
        aliases={'E0':{'arsenal':'england:arsenal','chelsea':'england:chelsea'}},folds=[dict(
        fitBefore='2026-07-01',profileBefore='2026-08-01',testFrom='2026-08-01',testBefore='2026-09-01',
        fixtures=[fixture()],profiles={'full':{},'separate_ft_selected_period':{}})])


def csv_item(**kwargs):
    row = dict(Div='E0',Date='21/08/2026',HomeTeam='Arsenal',AwayTeam='Chelsea',FTHG='2',FTAG='0',FTR='H',
        B365H='1.5',B365D='4.0',B365A='6.5',B365CH='1.45',B365CD='4.5',B365CA='7.0',
        **{'B365C>2.5':'2.1','B365C<2.5':'1.8'})
    row.update(kwargs)
    return dict(data=row,source={'code':'E0','layout':'main','url':'https://www.football-data.co.uk/mmz4281/2627/E0.csv'},row=2,sha256='abc')


class CsvPricesTest(unittest.TestCase):
    def test_prices_keep_packets_and_real_bookmakers_separate(self):
        row = csv_item()['data']
        row.update(MaxCH='10',AvgCH='9',BFECH='8',PSCH='1.4',PCH='1.4')
        quotes=row_quotes(row)
        self.assertEqual(len(quotes),9)
        self.assertEqual({q['bookmaker'] for q in quotes},{'Bet365','Pinnacle'})
        self.assertTrue(all(q['observedAtUTC'] is None for q in quotes))
        self.assertEqual(next(q['odd'] for q in quotes if q['sourceColumns']==['B365H']),1.5)
        self.assertEqual(next(q['odd'] for q in quotes if q['sourceColumns']==['B365CH']),1.45)

    def test_conflicting_pinnacle_alias_columns_not_arbitraged(self):
        self.assertFalse(row_quotes(dict(PSCH='2.2',PCH='2.4')))

    def test_bad_prices_counts_and_test_divisions_are_rejected(self):
        for v in ['nan','inf','-2','0','1','garbage']:
            self.assertIsNone(number(v))
        for v in ['nan','-1','1.5','']:
            self.assertIsNone(number(v,True))
        self.assertEqual(number('0',True),0)
        joined,cov=join_rows([csv_item(Div='E0T')],document())
        self.assertEqual(joined,[])
        self.assertEqual(cov['counters']['DIVISION_MISMATCH'],1)

    def test_dates_are_day_first_and_csv_quotes_can_contain_commas(self):
        self.assertEqual(parse_date('08/09/26'),'2026-09-08')
        self.assertIsNone(parse_date('09/30/26'))
        raw=b'\xef\xbb\xbfDate,HomeTeam,AwayTeam\r\n21/08/2026,"Arsenal, FC",Chelsea\r\n'
        self.assertEqual(parse_csv(raw,{} )[0]['data']['HomeTeam'],'Arsenal, FC')
        for raw in [b'<html>error</html>',b'Date,HomeTeam,HomeTeam\n1,A,B',b'Date,HomeTeam,AwayTeam\n1,A,B,C']:
            with self.assertRaises(ValueError):parse_csv(raw,{})

    def test_join_requires_exact_explicit_alias_date_and_score(self):
        joined,_=join_rows([csv_item()],document())
        self.assertEqual(len(joined),1)
        self.assertIn('UTC date',joined[0]['joinMethod'])
        for changes,reason in [({'Date':'20/08/2026'},'NO_UNIQUE_FROZEN_PREDICTION'),
                               ({'HomeTeam':'Arsenal FC'},'UNKNOWN_EXPLICIT_ALIAS'),
                               ({'FTHG':'3'},'FINAL_SCORE_CONFLICT'),({'FTHG':''},'MISSING_FINAL_SCORE'),
                               ({'FTR':'D'},'FINAL_SCORE_CONFLICT')]:
            joined,cov=join_rows([csv_item(**changes)],document())
            self.assertEqual(joined,[])
            self.assertEqual(cov['counters'][reason],1)

    def test_score_is_not_used_to_pick_an_ambiguous_fixture(self):
        doc=document();other=fixture();other['fixture']='other';other['actual']=[0,1]
        doc['folds'][0]['fixtures'].append(other)
        joined,cov=join_rows([csv_item()],doc)
        self.assertEqual(joined,[])
        self.assertEqual(cov['counters']['NO_UNIQUE_FROZEN_PREDICTION'],1)

    def test_conflicting_duplicate_prices_are_excluded(self):
        joined,cov=join_rows([csv_item(),csv_item(B365CH='1.6')],document())
        self.assertEqual(joined,[])
        self.assertEqual(cov['counters']['duplicateConflictingFixturesExcluded'],1)
        joined,_=join_rows([csv_item(),csv_item()],document())
        self.assertEqual(len(joined),1)

    def test_prediction_window_guards(self):
        validate_input(document())
        for field,value in [('featureCutoff',timestamp('2026-09-01')),('date',timestamp('2026-07-31'))]:
            doc=document();doc['folds'][0]['fixtures'][0][field]=value
            with self.assertRaises(ValueError):validate_input(doc)
        doc=document();doc['folds'][0]['profileBefore']='2026-09-01'
        with self.assertRaises(ValueError):validate_input(doc)

    def test_pre_closing_prices_never_enter_replay(self):
        doc=document();joined,_=join_rows([csv_item()],doc)
        for q in joined[0]['quotes']:
            q['priceType']='pre_closing'
        reports=replay(doc,joined)
        self.assertTrue(all(r['overall']['bets']==0 and r['pricedFixtures']==0 for r in reports.values()))

    def test_nonempty_replay_uses_frozen_probabilities_and_correct_settlement(self):
        doc=document()
        for row in doc['folds'][0]['fixtures'][0]['predictions'].values():
            row['means']=[3.5,.3]
        item=csv_item(B365CH='2.1',B365CD='4.5',B365CA='7.0')
        joined,_=join_rows([item],doc)
        reports=replay(doc,joined)
        first=reports['Bet365::full::current_preference']
        self.assertGreater(first['overall']['bets'],0)
        self.assertTrue(any(p['spec'][1:3]==('RESULT','HOME') and p['y']==1 for p in first['picks']))
        self.assertTrue(all(p['priceType']=='closing' and p['observedAtUTC'] is None for p in first['picks']))
        self.assertAlmostEqual(first['overall']['profitUnits'],sum(p['y']*p['odd']+p['actualPush']-1 for p in first['picks']))
        self.assertEqual(first['picks'],reports['Bet365::separate_ft_selected_period::current_preference']['picks'])

    def test_schedule_recovery_requires_independent_anchors_and_ignores_labels_prices(self):
        doc=document(); f=fixture(); f.update(fixture='g',utcDate='2026-08-28',home='england:arsenal',away='england:everton')
        doc['folds'][0]['fixtures'].append(f); doc['aliases']['E0']['everton']='england:everton'
        rows=[csv_item(HomeTeam='Arsenal FC'),csv_item(Date='28/08/2026',HomeTeam='Arsenal FC',AwayTeam='Everton')]
        one,audit=recover_schedule_aliases(rows[:1],doc)
        self.assertNotIn('arsenalfc',one['aliases']['E0'])
        learned,audit=recover_schedule_aliases(rows,doc)
        self.assertEqual(learned['aliases']['E0']['arsenalfc'],'england:arsenal')
        self.assertEqual(len(audit['accepted']),1)
        for r in rows:
            r['data'].update(FTHG='999',FTAG='999',B365CH='999',FTR='D')
        for fold in doc['folds']:
            for f in fold['fixtures']:
                f['actual']=[999,999]; f['predictions']={}
        learned2,audit2=recover_schedule_aliases(rows,doc)
        self.assertEqual(audit,audit2)
        self.assertEqual(learned['aliases'],learned2['aliases'])

    def test_conflicting_schedule_constraints_do_not_merge_teams(self):
        doc=document(); f=fixture(); f.update(fixture='g',utcDate='2026-08-28',home='england:fulham',away='england:everton')
        doc['folds'][0]['fixtures'].append(f);doc['aliases']['E0']['everton']='england:everton'
        rows=[csv_item(HomeTeam='Arsenal FC'),csv_item(Date='28/08/2026',HomeTeam='Arsenal FC',AwayTeam='Everton')]
        learned,audit=recover_schedule_aliases(rows,doc)
        self.assertNotIn('arsenalfc',learned['aliases']['E0'])
        self.assertEqual(audit['rejected'][0]['reason'],'CONFLICTING_OR_AMBIGUOUS_SCHEDULE')


if __name__ == '__main__':
    unittest.main()
