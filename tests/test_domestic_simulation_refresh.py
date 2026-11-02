from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import materialize_prepared_league_simulations as sim
import refresh_domestic_simulations as refresh
from simulation_priority_inputs import overlay, verified_points

NOW = datetime(2026,11,1,21,tzinfo=timezone.utc)


def entry(code='G1'):
    return {'league_code':code,'api_football_season':'2026','api_football_league_id':197,
            'app_season':'2026-2027','country':'Greece','league':'Super League',
            'target_season_start':'2026-08-01','target_season_end':'2027-07-01'}


def priority(root, fixtures, standings=None):
    path=root/'data/statmaker/priority_refresh/G1.json';path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps({'contract':'statmaker-league-tables-v1','leagueCode':'G1','season':2026,
        'apiFootballLeagueId':197,'fixtures':fixtures,'singleStandingsGroup':True,'standings':standings or []}))
    return path


def final(fid=1, status='FT', goals=(2,0)):
    return {'fixture_id':fid,'date_utc':(NOW-timedelta(days=2-fid)).isoformat(),
        'round':'Regular Season - '+str(fid),'status':status,
        'home_team':'Home','away_team':'Away','home_team_id':10,'away_team_id':11,
        'home_goals':goals[0],'away_goals':goals[1]}


class DomesticSimulationRefreshTest(unittest.TestCase):
    def test_new_final_is_added_without_inventing_advanced_statistics(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);priority(root,[final()])
            original={'fixtures':[]}
            merged=overlay(root,entry(),original)
            self.assertEqual([],original['fixtures'])
            self.assertEqual(1,len(merged['fixtures']))
            self.assertEqual({},merged['fixtures'][0]['normalized_stats'])
            self.assertEqual((2,0),sim.fixture_goals(merged['fixtures'][0]))

    def test_cached_statistics_and_names_survive_score_update_without_duplicates(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);priority(root,[final(),final(2)])
            original={'fixtures':[{'fixture_id':1,'home_team':'Canonical Home','away_team':'Canonical Away',
                'date':NOW.isoformat(),'status':'FT','home_goals':1,'away_goals':0,
                'normalized_stats':{'HS':15},'raw_statistics':[{'team':{'id':10,'name':'Canonical Home'}}]}]}
            merged=overlay(root,entry(),original)
            self.assertEqual(2,len(merged['fixtures']))
            self.assertEqual(['Canonical Home']*2,[r['home_team'] for r in merged['fixtures']])
            self.assertEqual({'HS':15},merged['fixtures'][0]['normalized_stats'])
            self.assertEqual(merged,overlay(root,entry(),merged))

    def test_live_match_and_wrong_season_are_not_added(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);path=priority(root,[final(status='2H')])
            self.assertEqual([],overlay(root,entry(),{'fixtures':[]})['fixtures'])
            doc=json.loads(path.read_text());doc['season']=2025;path.write_text(json.dumps(doc))
            with self.assertRaises(ValueError):overlay(root,entry(),{'fixtures':[]})

    def test_invalid_final_score_stops_projection(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);priority(root,[final(goals=(None,0))])
            with self.assertRaises(ValueError):overlay(root,entry(),{'fixtures':[]})

    def test_verified_points_preserve_deductions_and_reject_delayed_table(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            rows=[{'team':'Home','played':1,'goalsFor':2,'goalsAgainst':0,'points':3},
                  {'team':'Away','played':1,'goalsFor':0,'goalsAgainst':2,'points':-3}]
            path=priority(root,[final()],rows)
            fixtures=overlay(root,entry(),{'fixtures':[]})['fixtures']
            self.assertEqual({'Home':3,'Away':-3},verified_points(root,entry(),fixtures))
            doc=json.loads(path.read_text());doc['standings'][0]['played']=0;path.write_text(json.dumps(doc))
            self.assertEqual({},verified_points(root,entry(),fixtures))

    def test_fingerprints_ignore_refresh_timestamps_but_notice_results_and_statistics(self):
        fixtures=[{'fixture_id':1,'date':NOW.isoformat(),'status':'FT','home_team':'Home','away_team':'Away',
                   'home_goals':2,'away_goals':0,'normalized_stats':{'HS':5}}]
        payload={'fixtures':fixtures,'generated_at':'old'}
        scope=sim.EloScope('team-elo-v1',400,65,{'home':sim.EloTeam(1500,1)})
        with patch.object(sim,'load_fixture_payload',return_value=payload),patch.object(sim,'load_elo_scope',return_value=scope),\
             patch.object(refresh,'verified_points',return_value={}),patch.object(refresh,'sha256',return_value='code'):
            first=refresh.fingerprint(entry(),None)
            core=refresh.fingerprint(entry(),None,True)
            payload['generated_at']='new'
            self.assertEqual(first,refresh.fingerprint(entry(),None))
            fixtures[0]['normalized_stats']['HS']=6
            self.assertNotEqual(first,refresh.fingerprint(entry(),None))
            self.assertEqual(core,refresh.fingerprint(entry(),None,True))
            fixtures[0]['home_goals']=3
            self.assertNotEqual(core,refresh.fingerprint(entry(),None,True))

    def test_protected_digest_includes_uefa_and_matches_but_excludes_domestic_seasons(self):
        with sqlite3.connect(':memory:') as con:
            con.executescript('CREATE TABLE protected(id INTEGER PRIMARY KEY,value TEXT) WITHOUT ROWID;'
                "INSERT INTO protected VALUES(1,'original');"
                'CREATE TABLE prepared_league_simulation_meta(value INTEGER);'
                'INSERT INTO prepared_league_simulation_meta VALUES(1);')
            before=refresh.protected_digest(con)
            con.execute('UPDATE prepared_league_simulation_meta SET value=2')
            self.assertEqual(before,refresh.protected_digest(con))
            con.execute("UPDATE protected SET value='changed'")
            self.assertNotEqual(before,refresh.protected_digest(con))

    def test_partial_materialization_keeps_other_leagues_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);db=root/'prepared.db';sqlite3.connect(db).close()
            index=root/'index.json';index.write_text('{}')
            fixtures=[]
            for i in range(16):
                fixtures.append({'fixture_id':i+1,'date':'2026-09-'+str(i+1).zfill(2),'status':'FT',
                    'home_team':'Team '+str(i%8),'away_team':'Team '+str((i+1)%8),
                    'home_goals':1,'away_goals':0,'normalized_stats':{},
                    'source_league':{'round':'Regular Season - '+str(i+1)}})
            entries=[entry(code) for code in ('E0','D1')]
            scope=sim.EloScope('team-elo-v1',400,65,{sim.norm('Team '+str(i)):sim.EloTeam(1500,4) for i in range(8)})
            with patch.object(sim,'ROOT',root),patch.object(sim,'INDEX_PATH',index),\
                 patch.object(sim,'current_entries',return_value=entries),patch.object(sim,'load_fixture_payload',return_value={'fixtures':fixtures}),\
                 patch.object(sim,'load_elo_scope',return_value=scope),patch.object(sim,'load_official_standings_cache',return_value={}):
                with patch.object(sys,'argv',['sim',str(db),'--runs','1000']):self.assertEqual(0,sim.main())
                with sqlite3.connect(db) as con:before=con.execute("SELECT * FROM prepared_league_team_simulations WHERE league_code='E0' ORDER BY team_key").fetchall()
                fixtures[-1]['home_goals']=4
                with patch.object(sys,'argv',['sim',str(db),'--runs','1000','--league-code','D1']):self.assertEqual(0,sim.main())
                with sqlite3.connect(db) as con:
                    self.assertEqual(before,con.execute("SELECT * FROM prepared_league_team_simulations WHERE league_code='E0' ORDER BY team_key").fetchall())
                    self.assertEqual(2,con.execute('SELECT COUNT(*) FROM prepared_league_simulation_meta').fetchone()[0])

    def test_new_workflow_is_cached_and_does_not_depend_on_retired_betting_jobs(self):
        path=ROOT/'.github/workflows/domestic-simulation-refresh.yml'
        if not path.is_file():
            # The runtime sparse checkout contains only the code and offline tests.
            return
        text=path.read_text()
        self.assertNotIn('API_FOOTBALL_KEY',text)
        self.assertNotIn('upload-artifact',text)
        self.assertIn('Priority Results and Official Standings',text)
        self.assertIn('App-Ready Stats Hot Publish',text)
        self.assertIn('statmaker-app-ready-targeted-writers',text)
        self.assertNotIn('Domestic League Simulation Refresh\']',text)


if __name__=='__main__':unittest.main()
