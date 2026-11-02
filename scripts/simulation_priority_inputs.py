"""Overlay verified current-season final scores onto cached statistical inputs."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import json


def read_model(root, entry):
    season = entry.get('api_football_season') or entry.get('target_api_football_season')
    league_id = entry.get('api_football_league_id') or entry.get('target_api_football_league_id')
    if not season or not league_id:
        return None
    path = root/'data/statmaker/priority_refresh'/(str(entry.get('league_code'))+'.json')
    if not path.is_file():
        return None
    doc = json.loads(path.read_text())
    if (doc.get('contract') != 'statmaker-league-tables-v1'
            or doc.get('leagueCode') != entry.get('league_code')
            or str(doc.get('season')) != str(season)
            or str(doc.get('apiFootballLeagueId')) != str(league_id)):
        raise ValueError('Priority simulation input identity mismatch: '+str(entry.get('league_code')))
    return doc


def overlay(root, entry, payload):
    doc = read_model(root, entry)
    if doc is None:
        return payload
    result = deepcopy(payload)
    rows = {int(r['fixture_id']): r for r in result.get('fixtures', [])}
    aliases = {}
    for f in doc.get('fixtures', []):
        old = rows.get(f.get('fixture_id'))
        if old:
            for side in ('home','away'):
                if f.get(side+'_team_id') and old.get(side+'_team'):
                    aliases[f[side+'_team_id']] = old[side+'_team']
    now = datetime.now(timezone.utc)
    for f in doc.get('fixtures', []):
        if f.get('status') != 'FT':
            continue
        if (type(f.get('fixture_id')) is not int or not f.get('home_team') or not f.get('away_team')
                or any(type(f.get(k)) is not int or f[k] < 0 for k in ('home_goals','away_goals'))):
            raise ValueError('Invalid verified final in priority simulation input')
        date = datetime.fromisoformat(str(f['date_utc']).replace('Z','+00:00'))
        if date.utcoffset() is None:
            raise ValueError('Invalid final date in priority simulation input')
        if date > now+timedelta(minutes=1):
            print('SIMULATION_FUTURE_FINAL_IGNORED code='+str(doc['leagueCode'])+' fixture='+str(f['fixture_id']),flush=True)
            continue
        old = rows.get(f['fixture_id'], {})
        hg, ag = f['home_goals'], f['away_goals']
        rows[f['fixture_id']] = {**old, 'fixture_id': f['fixture_id'], 'date': f['date_utc'],
            'home_team': old.get('home_team') or aliases.get(f.get('home_team_id')) or f['home_team'],
            'away_team': old.get('away_team') or aliases.get(f.get('away_team_id')) or f['away_team'],
            'status': 'FT', 'home_goals':hg, 'away_goals':ag,
            'home_score':hg, 'away_score':ag, 'fthg':hg, 'ftag':ag,
            'goals':{'home':hg,'away':ag},
            'source_league':{'id':doc['apiFootballLeagueId'],'season':doc['season'],'round':f['round']},
            'normalized_stats':old.get('normalized_stats') or {}}
    result['fixtures'] = sorted(rows.values(), key=lambda r:(str(r.get('date') or ''),r['fixture_id']))
    return result


def verified_points(root, entry, fixtures):
    """Apply official point adjustments only when all current totals agree."""
    doc = read_model(root, entry)
    if not doc or not doc.get('singleStandingsGroup') or not doc.get('standings'):
        return {}
    if any(not str((f.get('source_league') or {}).get('round','')).startswith('Regular Season') for f in fixtures):
        return {}
    stats = {}
    for f in fixtures:
        for side, opposite in (('home','away'),('away','home')):
            name = f[side+'_team']
            row = stats.setdefault(name, [0,0,0])
            scored = f.get(side+'_goals', (f.get('goals') or {}).get(side))
            conceded = f.get(opposite+'_goals', (f.get('goals') or {}).get(opposite))
            if type(scored) is not int or type(conceded) is not int:
                return {}
            row[0] += 1
            row[1] += scored
            row[2] += conceded
    expected = {r['team']:[r['played'],r['goalsFor'],r['goalsAgainst']] for r in doc['standings']}
    # Aliases from older caches can differ; do not guess a point adjustment.
    if stats != expected:
        return {}
    return {r['team']:r['points'] for r in doc['standings']}
