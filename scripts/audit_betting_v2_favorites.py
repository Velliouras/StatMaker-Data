#!/usr/bin/env python3
"""Replay an installed UAT feed using repository caches only; zero provider calls."""
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import zipfile

import materialize_betting_v2_direct as direct

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = {'74173144','74173166','74173164','74173178','74173208','74173148','74173156','74172952','68823386','67817908','72723266','72037390'}


def main():
    manifest = json.loads((ROOT / 'data/statmaker/app_ready/update_manifest.json').read_text())
    artifact = next(v for v in manifest['artifacts'] if v['id'] == 'app_ready_betting_bundle')
    output = ROOT / 'reports/betting_v2/favorite_audit'
    output.mkdir(parents=True, exist_ok=True)
    cutoff = direct.kickoff(artifact['generatedAt'])
    history = direct.load_actual_results(cutoff)
    model = direct.MatchupLeague(history['GLOBAL'],cutoff)
    models = {code:model for code in ('UECL','D1')}
    audit = dict(bundle=artifact, replayCutoff=cutoff, externalProviderCalls=0, fixtures=[])
    audit['increasingCoefficients'] = {
        metric:{key:float(model.models[metric].coef_[i]/model.feature_scales[metric][i])
                for key,i in model.columns[metric].items()
                if key in ('elo_gap','recent_for','recent_opponent_against','xg_for','opponent_xg_against','goal_strength_gap')}
        for metric in ('GOALS_FT','CORNERS','SOT')
    }
    print('AUDIT_INCREASING_COEFFICIENTS',json.dumps(audit['increasingCoefficients']))

    with tempfile.TemporaryDirectory() as td:
        bundle = Path(td) / 'bundle'
        with zipfile.ZipFile(ROOT / artifact['path']) as z:
            z.extractall(bundle)
        db = bundle / 'databases/statmaker_prepared_betting.db'
        with sqlite3.connect(db) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute('SELECT * FROM prepared_fixture_matches').fetchall()
            for fixture in rows:
                if str(fixture['id']) not in FIXTURES:
                    continue
                f = dict(fixture)
                model = next(iter(models.values()))
                aliases = direct.UEFA_ALIASES.get(direct.league_code(f['league_code']), {})
                def resolved(side):
                    keys = ('canonical_'+side+'_team', side+'_team', 'provider_'+side+'_team')
                    choices = [aliases.get(direct.name(f[k]), direct.name(f[k])) for k in keys]
                    return next((v for v in choices if v in model.history), choices[0])
                home, away = resolved('home'), resolved('away')
                item = dict(fixture=f, homeIdentity=home, awayIdentity=away,
                            homeElo=model.elo[home], awayElo=model.elo[away], forecasts=[], metrics={})
                for metric in ('GOALS_FT', 'CORNERS', 'SOT'):
                    item['metrics'][metric] = dict(prediction=model.predict(home, away, metric),
                        context=model.context(home,away,metric))
                item['homeRecent'] = [v for v in history['GLOBAL']
                    if v['_date'] < cutoff and home in (v['_home'], v['_away'])][-12:]
                item['awayRecent'] = [v for v in history['GLOBAL']
                    if v['_date'] < cutoff and away in (v['_home'], v['_away'])][-12:]
                item['periodQuotes'] = [dict(v) for v in con.execute('SELECT * FROM prepared_betting_v2_quotes WHERE competition_id=? AND snapshot_version=? AND match_key=?',(f['competition_id'],f['snapshot_version'],f['match_key']))]
                for v in con.execute('SELECT * FROM prepared_betting_v2_forecasts '
                    'WHERE competition_id=? AND snapshot_version=? AND match_key=? '
                    'AND odd>1.8 AND odd<3 AND win_probability>=0.6',
                    (f['competition_id'], f['snapshot_version'], f['match_key'])):
                    item['forecasts'].append(dict(v))
                feed_path = bundle / ('files/app_ready_odds/'+f['competition_id']+'.json')
                if feed_path.exists():
                    feed = json.loads(feed_path.read_text())
                    item['sourceMetadata'] = {k:v for k,v in feed.items() if k not in ('matches','leagues','debug')}
                    matches = list(feed.get('matches', [])) + [v for g in feed.get('leagues', []) for v in g.get('matches', [])]
                    item['sourceQuotes'] = [m for m in matches if str(m.get('id')) == str(f['id'])]
                audit['fixtures'].append(item)
        case_selections = {
            '74173144':('TEAM_SHOTS_ON_TARGET','Ajax Amsterdam Under 5.5'),
            '74173166':('TEAM_TOTAL_GOALS','AS Monaco Under 1.5'),
            '74173164':('TEAM_TOTAL_GOALS','FK Borac Banja Luka Over 0.5'),
            '74173178':('TEAM_TOTAL_GOALS','Atalanta BC Under 1.5'),
            '74173208':('DOUBLE_CHANCE','X2'),
        }
        audit['caseSelections'] = []
        with sqlite3.connect(db) as con:
            con.row_factory = sqlite3.Row
            for fid,(market,selection) in case_selections.items():
                rows = [dict(v) for v in con.execute(
                    'SELECT f.id,v.market,v.selection,v.odd,v.win_probability,v.push_probability,'
                    'v.prediction_home_mean,v.prediction_away_mean FROM prepared_fixture_matches f '
                    'JOIN prepared_betting_v2_forecasts v USING(competition_id,snapshot_version,match_key) '
                    'WHERE f.id=? AND v.market=? AND v.selection=?',(fid,market,selection))]
                audit['caseSelections'].extend(rows)
            audit['atalantaX2'] = [dict(v) for v in con.execute(
                "SELECT v.market,v.selection,v.odd,v.win_probability FROM prepared_fixture_matches f "
                "JOIN prepared_betting_v2_forecasts v USING(competition_id,snapshot_version,match_key) "
                "WHERE f.id='74173178' AND v.market='DOUBLE_CHANCE' AND v.selection='X2'")]
        m = models['D1']
        aliases = direct.UEFA_ALIASES.get('D1',{})
        bayern = aliases.get('bayernmunchen')
        gladbach = next((v for k,v in aliases.items() if 'gladbach' in k),None)
        dortmund = aliases.get('borussiadortmund')
        audit['bayernGladbach'] = m.predict(bayern,gladbach,'GOALS_FT') if bayern and gladbach else None
        audit['bayernDortmund'] = m.predict(bayern,dortmund,'GOALS_FT') if bayern and dortmund else None
        print('AUDIT_CASE_SELECTIONS',json.dumps(audit['caseSelections']))
        print('AUDIT_ATALANTA_X2',json.dumps(audit['atalantaX2']))
        print('AUDIT_BAYERN_MATCHUPS',audit['bayernGladbach'],audit['bayernDortmund'])
        for code in ('D1',):
            m = models[code]
            names = sorted(v for v in m.history if any(x in v for x in ('bayern', 'gladbach', 'dortmund')))
            audit[code] = {v:dict(elo=m.elo[v], goals=m.history[v]['GOALS_FT'][-12:]) for v in names}
        (output / 'audit.json').write_text(json.dumps(audit, ensure_ascii=False, indent=2)+'\n')
        # A small SQLite copy retains exact quotes/forecasts and snapshot bindings
        # while omitting unrelated legacy simulation matrices.
        replay_db = output / 'statmaker_prepared_betting.db'
        with sqlite3.connect(db) as source, sqlite3.connect(replay_db) as target:
            for table in ('prepared_fixture_matches','prepared_fixture_markets','prepared_betting_v2_quotes','prepared_betting_v2_feed_meta','prepared_betting_v2_forecasts'):
                ddl = source.execute('SELECT sql FROM sqlite_master WHERE type=\'table\' AND name=?', (table,)).fetchone()[0]
                target.execute(ddl)
                records = source.execute('SELECT * FROM '+table).fetchall()
                if records:
                    target.executemany('INSERT INTO '+table+' VALUES ('+','.join('?' for _ in records[0])+')', records)
            target.commit()
        with zipfile.ZipFile(output / 'replay_inputs.zip', 'w', zipfile.ZIP_DEFLATED) as z:
            z.write(replay_db, 'databases/statmaker_prepared_betting.db')
            for path in sorted((bundle / 'files/app_ready_odds').rglob('*.json')):
                z.write(path, path.relative_to(bundle).as_posix())
            paths = {ROOT / 'data/statmaker/domestic_enriched/index.json'}
            index = json.loads(next(iter(paths)).read_text())
            paths.update(ROOT / v['output_path'] for v in index.get('leagues', []) if v.get('output_path'))
            paths.update(ROOT / ('data/statmaker/'+s) for s in ('uefa_current_stats.json', 'uefa_support_history.json', 'uefa_team_support_history.json'))
            paths.update(ROOT / ('mappings/'+s) for s in ('domestic_team_aliases.json', 'uefa_betting_team_aliases.json'))
            paths.update(ROOT / ('config/'+s) for s in ('uefa_club_competitions.json', 'uefa_simulation_2026_27.json'))
            paths.update((ROOT / 'scripts/betting_v2').rglob('*.py'))
            paths.add(ROOT / 'scripts/materialize_betting_v2_direct.py')
            paths.add(ROOT / 'scripts/odds_api_io_market_audit.py')
            paths.update((ROOT / 'data/api_football/league_tables').glob('*_fixtures.json'))
            for path in sorted(paths):
                if path.is_file():
                    z.write(path, path.relative_to(ROOT).as_posix())
        replay_db.unlink()
    print('FAVORITE_AUDIT', json.dumps(dict(fixtures=len(audit['fixtures']), externalProviderCalls=0,
        inputBytes=(output / 'replay_inputs.zip').stat().st_size)))
    for item in audit['fixtures']:
        f = item['fixture']
        print('AUDIT_FIXTURE', f['id'], f['home_team'], f['away_team'],
              'ELO', item['homeElo'], item['awayElo'],
              'GOALS', item['metrics']['GOALS_FT']['prediction'])


if __name__ == '__main__':
    main()
