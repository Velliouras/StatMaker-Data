from __future__ import annotations
import json, sqlite3, struct, tempfile, unittest
from pathlib import Path
from scripts.statistics_archive_catalog import statistics_catalog
from scripts.hotpatch_app_ready_stats import create_stats_db, archived_normalized_values, NORMALIZED_FIELDS, write_utf
from scripts.restore_archived_stats import restore, merge_archived_normalized

class HistoricalStatisticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.archive=self.root/'data/statmaker/domestic_enriched';self.archive.mkdir(parents=True)
        self.current=self.artifact('E0','2026-2027',2026,'current_2026.json',1)
        self.old=self.artifact('E0','2025-2026',2025,'previous_2025.json',2)
        self.calendar=self.artifact('FIN','2025',2025,'finland_2025.json',3)
        self.index={'leagues':[dict(self.current['competition'],output_path='data/statmaker/domestic_enriched/current_2026.json',completed_fixtures=1)]}
        (self.archive/'index.json').write_text(json.dumps(self.index))
    def artifact(self,code,season,year,name,fid):
        payload={'competition':{'league_code':code,'app_season':season,'api_football_season':str(year)},
          'source':{'provider':'api-football'},'readiness':{'completed_fixtures':1},
          'matches':[{'fixture_id':fid,'date_utc':f'{year}-09-01T18:00:00Z','app_season':season,'league_code':code,
             'home_team':'Club','away_team':'Other','status':'FT','home_goals':2,'away_goals':1,
             'normalized_stats':{'HxG':1.8,'AxG':0.7},'api_football_league_id':39}]}
        (self.archive/name).write_text(json.dumps(payload));return payload
    def test_restores_split_and_calendar_seasons_without_changing_live_catalogue(self):
        self.assertEqual(3,len(statistics_catalog(self.root,self.index)['leagues']))
        self.assertEqual(1,len(self.index['leagues']))
        db=self.root/'stats.db';count,scopes=create_stats_db(db,self.root)
        self.assertEqual(3,count)
        self.assertEqual({('2026-2027','E0'):1,('2526','E0'):1,('2025','FIN'):1},scopes)
        with sqlite3.connect(db) as c:
            self.assertEqual(2,c.execute("SELECT fthg FROM matches WHERE season='2526'").fetchone()[0])
            self.assertEqual('ok',c.execute('PRAGMA quick_check').fetchone()[0])
        normalized=archived_normalized_values(self.root)
        self.assertTrue(any('2025_09_01' in key for key in normalized))
        self.assertTrue(all(row[NORMALIZED_FIELDS.index('HxG')]==1.8 for row in normalized.values()))
    def test_restore_preserves_every_current_row_and_is_repeatable(self):
        db=self.root/'stats.db';create_stats_db(db,self.root)
        with sqlite3.connect(db) as c:
            c.execute("DELETE FROM matches WHERE season!='2026-2027'")
            before=c.execute("SELECT * FROM matches WHERE season='2026-2027'").fetchall()
        self.assertEqual(2,restore(db,root=self.root));self.assertEqual(0,restore(db,root=self.root))
        with sqlite3.connect(db) as c:
            self.assertEqual(before,c.execute("SELECT * FROM matches WHERE season='2026-2027'").fetchall())
            self.assertEqual(3,c.execute('SELECT COUNT(*) FROM matches').fetchone()[0])
    def test_wrong_provider_or_incomplete_score_is_rejected(self):
        p=self.archive/'previous_2025.json';self.old['source']['provider']='unknown';p.write_text(json.dumps(self.old))
        with self.assertRaisesRegex(ValueError,'provider'):statistics_catalog(self.root,self.index)
        self.old['source']['provider']='api-football';self.old['matches'][0]['home_goals']=None;p.write_text(json.dumps(self.old))
        with self.assertRaisesRegex(ValueError,'score'):statistics_catalog(self.root,self.index)
    def test_live_game_cannot_enter_archived_statistics(self):
        self.old['matches'][0]['status']='2H';(self.archive/'previous_2025.json').write_text(json.dumps(self.old))
        with self.assertRaisesRegex(ValueError,'Unfinished'):statistics_catalog(self.root,self.index)
    def test_active_source_wins_and_duplicate_archive_is_rejected(self):
        self.index['leagues'].append(dict(self.old['competition'],output_path='data/statmaker/domestic_enriched/previous_2025.json',completed_fixtures=1))
        self.assertEqual(3,len(statistics_catalog(self.root,self.index)['leagues']))
        self.index['leagues'].pop();(self.archive/'duplicate_2025.json').write_text(json.dumps(self.old))
        with self.assertRaisesRegex(ValueError,'matching source artifacts=2'):statistics_catalog(self.root,self.index)
    def test_normalized_merge_keeps_current_values(self):
        p=self.root/'normalized.bin';current=tuple(1.0 for _ in NORMALIZED_FIELDS)
        with p.open('wb') as out:
            out.write(struct.pack('>iii',0x534D4453,2,1));write_utf(out,'current')
            for v in current:out.write(b'\1'+struct.pack('>d',v))
        merge_archived_normalized(p,{'historical':tuple(2.0 for _ in NORMALIZED_FIELDS)})
        with p.open('rb') as src:
            self.assertEqual((0x534D4453,2,2),struct.unpack('>iii',src.read(12)))
            n=struct.unpack('>H',src.read(2))[0];self.assertEqual('current',src.read(n).decode())
            for v in current:
                self.assertEqual(b'\1',src.read(1));self.assertEqual(v,struct.unpack('>d',src.read(8))[0])
if __name__=='__main__':unittest.main()
