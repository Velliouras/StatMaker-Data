import copy,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from betting_v2.raw_contract_recovery import numeric_entries,recover_snapshot,card_readiness
from betting_v2.audit_raw_contract_archives import join,packets,complete_pairs

class RawQuoteTests(unittest.TestCase):
    def data(self,name='Total Fouls',book='Unibet'):
        market=dict(name=name,updatedAt='2026-10-03T11:45:00Z',odds=[dict(hdp=25.5,over='1.95',under='1.85')]);source=dict(sha='a'*40,sha256='b'*64,committedAtUTC='2026-10-03T12:00:00Z')
        match=dict(id='123',kickoff='2026-10-03T14:00:00Z',teamMappingStatus='matched',homeTeam='Alpha',awayTeam='Beta',providerMarkets=[dict(bookmaker=book,exactProviderPayload=True,market=market)])
        return dict(generatedAt='2026-10-03T11:59:00Z',leagues=[dict(leagueCode='E0',matches=[match])]),source,market,match
    def test_numeric_sides_and_casing_preserved_without_missing_side_imputation(self):
        self.assertEqual({a for a,_,_ in numeric_entries(dict(Over='1.9',UNDER='2.0'))},{'OVER','UNDER'})
        self.assertEqual(numeric_entries(dict(over='1.9',under=None)),[('OVER',1.9,'over')]);self.assertFalse(numeric_entries(dict(odds=2.,name='')))
    def test_fouls_under_survives_exact_mapping_and_clock(self):
        d,s,_,_=self.data();rows,_,_,_=recover_snapshot(d,s);self.assertEqual({r['spec'][2] for r in rows},{'OVER','UNDER'});self.assertTrue(all(r['spec'][0]=='FOULS' for r in rows));self.assertFalse(rows[0]['executionEvidence'])
    def test_card_unit_target_is_distinct_and_exact_book_and_market_required(self):
        d,s,m,match=self.data('Number of Cards In Match','Bet365');r=recover_snapshot(d,s)[0];self.assertTrue(all(x['spec'][0]=='BET365_CARD_UNITS' for x in r))
        for name,book in [('Number of Cards In Match','Unibet'),('Player Cards','Bet365'),('Bookings Spread','Bet365')]:
            m['name']=name;match['providerMarkets'][0]['bookmaker']=book;self.assertFalse(recover_snapshot(d,s)[0])
    def test_stale_future_and_postkickoff_rows_cannot_be_recovered(self):
        for update in [None,'2026-10-03T12:01:00Z','2026-10-02T11:59:59Z']:
            d,s,m,_=self.data();m['updatedAt']=update;self.assertFalse(recover_snapshot(d,s)[0])
        d,s,m,match=self.data();match['kickoff']=s['committedAtUTC'];self.assertFalse(recover_snapshot(d,s)[0])
    def test_join_requires_previous_numeric_provider_and_exact_kickoff(self):
        d,s,_,_=self.data();rows=recover_snapshot(d,s)[0];key='123',int(rows[0]['kickoff']);good={key:{('f','h','a')}}
        self.assertEqual(len(join(rows,good)[0]),2);self.assertFalse(join(rows,{('124',key[1]):good[key]})[0]);self.assertFalse(join(rows,{key:{('f','h','a'),('g','h','a')}})[0])
    def test_metric_packet_never_mixes_receipts_and_complete_pairs_need_both_sides(self):
        rows=[dict(fixture='f',sourceReceipt=x,observed=i,bookmaker='b',spec=['FOULS','TOTAL',side,'',25.5]) for x,i,side in [('a',1.,'OVER'),('a',1.,'UNDER'),('b',2.,'OVER')]]
        self.assertEqual(len(packets(rows,False)),2);self.assertEqual(len(packets(rows,True)),1);self.assertEqual(complete_pairs(packets(rows,True))['partitions'],0)

class CardReadinessTests(unittest.TestCase):
    def body(self):
        return dict(fixture=dict(id=1,status=dict(short='FT'),referee='r'),teams={s:dict(id=i) for s,i in [('home',1),('away',2)]},lineups=[dict(team=dict(id=i),startXI=[dict(player=dict(id=i*100+j)) for j in range(11)],substitutes=[dict(player=dict(id=i*100+20))]) for i in (1,2)],events=[],statistics=[dict(team=dict(id=i),statistics=[dict(type='Yellow Cards',value=0),dict(type='Red Cards',value=0)]) for i in (1,2)])
    def card(self,b,detail='Yellow Card',pid=100,minute=25,tid=1):
        b['events'].append(dict(type='Card',detail=detail,player=dict(id=pid),team=dict(id=tid),time=dict(elapsed=minute,extra=None)))
        typ='Red Cards' if detail=='Red Card' else 'Yellow Cards';s=next(s for s in b['statistics'] if s['team']['id']==tid);next(v for v in s['statistics'] if v['type']==typ)['value']+=1
    def test_clean_yellow_and_direct_red_have_correct_book_units(self):
        b=self.body();self.card(b);self.card(b,'Red Card',101,40);r=card_readiness(b);self.assertTrue(r['ready']);self.assertEqual(r['provisionalUnits'],[3,0])
    def test_late_cards_cannot_be_assumed_before_final_whistle(self):
        for t in [90,91,120,None]:
            b=self.body();self.card(b,minute=t);r=card_readiness(b);self.assertFalse(r['ready']);self.assertIn('unverifiedCardPeriodOrFinalWhistle',r['flags'])
    def test_missing_or_bad_lineups_and_aet_are_blocked(self):
        for change in ['lineups','events']:
            b=self.body();b.pop(change);self.assertFalse(card_readiness(b)['ready'])
        b=self.body();b['fixture']['status']['short']='AET';self.assertFalse(card_readiness(b)['ready'])
    def test_unused_bench_card_excluded_but_later_appearance_counts(self):
        b=self.body();self.card(b,pid=120,minute=20);r=card_readiness(b);self.assertTrue(r['ready']);self.assertEqual(r['provisionalUnits'],[0,0])
        b['events'].append(dict(type='subst',team=dict(id=1),player=dict(id=100),assist=dict(id=120),time=dict(elapsed=60)));r=card_readiness(b);self.assertTrue(r['ready']);self.assertEqual(r['provisionalUnits'],[1,0])
    def test_subbed_out_and_same_minute_cards_are_handled_without_order_guess(self):
        b=self.body();b['events'].append(dict(type='subst',team=dict(id=1),player=dict(id=100),assist=dict(id=120),time=dict(elapsed=60)));self.card(b,pid=100,minute=65);r=card_readiness(b);self.assertTrue(r['ready']);self.assertEqual(r['provisionalUnits'],[0,0]);b['events'][-1]['time']['elapsed']=60;self.assertFalse(card_readiness(b)['ready'])
    def test_second_yellow_is_provisional_and_never_silently_reused_aggregate(self):
        b=self.body();self.card(b);self.card(b,'Second Yellow card',100,60);r=card_readiness(b);self.assertFalse(r['ready']);self.assertEqual(r['provisionalUnits'],[3,0])
    def test_incomplete_events_unknown_player_and_duplicate_card_block(self):
        b=self.body();b['statistics'][0]['statistics'][0]['value']=1;self.assertFalse(card_readiness(b)['ready'])
        b=self.body();self.card(b,pid=None);self.assertFalse(card_readiness(b)['ready'])
        b=self.body();self.card(b);b['events'].append(copy.deepcopy(b['events'][0]));self.assertFalse(card_readiness(b)['ready'])

if __name__=='__main__':unittest.main()
