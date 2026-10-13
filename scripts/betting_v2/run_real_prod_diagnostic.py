#!/usr/bin/env python3
"""Betting V2 real App-Ready offline evaluation, NOT the certified publisher.

Reads every fixture/selection from the exact PROD SQLite (or its App-Ready ZIP),
then uses existing dated history and frozen xG/Elo coefficients. No provider calls,
training, shadow preview, Android edits, or PROD writes. Output is a diagnostic,
not a certified tip: historical price observation time and calibration are unknown.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parent))

from audit_data import read_fixtures, number
from fixture_lookup import canonical_name
from phase1_cached_history import CachedHistoryBridge, team_key
from freeze_prematch_shadow import XG_PARAMS, ELO_PARAMS, PARAMS_VERSION
from scripts.betting_v2.production_sources import full_inventory, market_identity
from scripts.betting_v2.production_contract import Blocked
from walk_forward_goals import make_rows, model_lambdas
from walk_forward_elo import make_elo_rows, lambda_rates

ODDS_LOW, ODDS_HIGH, P_MIN = 1.80, 3.00, .60
COUNT_STATS = {
    'CORNERS': ('HC', 'AC'), 'SHOTS': ('HS', 'AS'),
    'SOT': ('HST', 'AST'), 'YELLOW_CARDS': ('HY', 'AY'),
    'GOALS_1H': ('HTHG', 'HTAG'),
    'GOALS_2H': ('H2HG', 'H2AG'),
}


def utc(value: str) -> datetime:
    dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if dt.utcoffset() is None:
        raise ValueError('Missing UTC offset')
    return dt.astimezone(timezone.utc)


def poisson(rate: float) -> list[float]:
    """Complete Poisson probabilities, including high-volume corners/shots markets."""
    if not math.isfinite(rate) or rate < 0 or rate > 500:
        raise ValueError('Invalid Poisson rate')
    # Series recurrence is stable for the observed football count ranges.
    maximum = min(1000, max(35, math.ceil(rate + 14 * math.sqrt(rate + 1))))
    if rate > 650:
        raise ValueError('Unsupported extreme rate')
    out = [math.exp(-rate)]
    for k in range(1, maximum + 1):
        out.append(out[-1] * rate / k)
    if 1 - sum(out) > 1e-8:
        raise ValueError('Poisson tail too large')
    return out


def probability(model: dict, h: float, a: float) -> tuple[float, float] | None:
    """Model (P[WIN], P[PUSH]) for an actual supported market/selection."""
    key = model['marketModelKey']
    side = model['side']
    line = model.get('line')
    if key in ('RESULT_1X2', 'HT_RESULT_1X2', 'RESULT_DOUBLE_CHANCE',
               'HT_RESULT_DOUBLE_CHANCE', 'RESULT_DNB', 'BTTS'):
        home, away = poisson(h), poisson(a)
        ph = sum(x * y for i, x in enumerate(home) for j, y in enumerate(away) if i > j)
        pd = sum(x * y for i, x in enumerate(home) for j, y in enumerate(away) if i == j)
        pa = max(0., 1. - ph - pd)
        result = {'HOME': ph, 'DRAW': pd, 'AWAY': pa,
                  'YES': (1 - home[0]) * (1 - away[0]),
                  'NO': home[0] + away[0] - home[0] * away[0],
                  'HOME_OR_DRAW': ph + pd, 'AWAY_OR_DRAW': pa + pd,
                  'HOME_OR_AWAY': ph + pa}
        if key == 'RESULT_DNB':
            return ((ph if side == 'HOME' else pa), pd) if side in ('HOME', 'AWAY') else None
        if key in ('RESULT_1X2', 'HT_RESULT_1X2') and side not in ('HOME', 'DRAW', 'AWAY'):
            return None
        if key in ('RESULT_DOUBLE_CHANCE', 'HT_RESULT_DOUBLE_CHANCE') and side not in (
                'HOME_OR_DRAW', 'AWAY_OR_DRAW', 'HOME_OR_AWAY'):
            return None
        if key == 'BTTS' and side not in ('YES', 'NO'):
            return None
        return (result[side], 0.) if side in result else None
    if key.endswith(('TOTAL', '_CORNERS', '_SOT', '_SHOTS', '_YELLOW_CARDS')):
        if side not in ('OVER', 'UNDER') or line is None:
            return None
        try:
            line = float(line)
        except (ValueError, TypeError):
            return None
        if line < 0 or line * 2 != int(line * 2):
            return None
        # Independent Poisson sum: home+away counts are Poisson(h+a).
        rate = h if key.startswith('HOME_') else a if key.startswith('AWAY_') else h + a
        counts = poisson(rate)
        over = sum(p for count, p in enumerate(counts) if count > line)
        under = sum(p for count, p in enumerate(counts) if count < line)
        push = max(0., 1. - over - under)
        return (over if side == 'OVER' else under, push)
    return None


def apply_real_period_labels(root: Path, history: list[dict]) -> tuple[list[dict], dict]:
    """Attach halftime/2nd-half labels FROM REAL completed enriched fixture records.

    Never synthesize missing period scores; exclude AET/PEN as not regulation FT.
    Also copy no postmatch field into prematch predictor for target matches.
    """
    index = json.loads((root / 'data/statmaker/domestic_enriched/index.json').read_text(encoding='utf-8'))
    originals = {}
    for league in index['leagues']:
        path = (root / league['output_path']).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            continue
        payload = json.loads(path.read_text(encoding='utf-8'))
        comp = payload.get('competition') or {}
        group = str(comp.get('league_code') or '') + '|' + str(comp.get('league') or '')
        for m in payload.get('matches', []):
            fid = str(m.get('fixture_id') or '')
            if fid:
                originals[(group, fid)] = m
    out = []
    counters = Counter()
    for m in history:
        original = originals.get((m['group'], str(m['fixture_id'])))
        if original is None:
            counters['MISSING_ORIGINAL'] += 1
            continue
        if original.get('status') != 'FT':
            counters['NON_REGULATION_MATCH'] += 1
            continue
        prepared = dict(m)
        stats = dict(prepared['stats'])
        h = number(original.get('hthg'))
        a = number(original.get('htag'))
        gh, ga = prepared['hg'], prepared['ag']
        if (h is not None and a is not None and h.is_integer() and a.is_integer()
                and 0 <= h <= gh and 0 <= a <= ga):
            stats.update(HTHG=int(h), HTAG=int(a),
                         H2HG=int(gh-h), H2AG=int(ga-a))
            counters['VERIFIED_PERIOD_LABELS'] += 1
        else:
            counters['MISSING_OR_INVALID_PERIOD_LABELS'] += 1
        prepared['stats'] = stats
        out.append(prepared)
    return out, dict(counters)


def normalize_identity(row: dict) -> dict:
    """No made-up prices/lines: normalize only literal Over/Under label + real line."""
    if row.get('blocker') != 'MISSING_COUNT_LINE_OR_SIDE':
        return row
    label = str(row.get('selection') or '')
    directions = re.findall(r'\b(OVER|UNDER)\b', label, flags=re.I)
    quoted_lines = re.findall(r'(?<![A-Za-z0-9])(\d+(?:\.\d+)?)(?![A-Za-z0-9])', label)
    if (len(directions)!=1 or len(quoted_lines)!=1 or row.get('line') is None
            or float(quoted_lines[0]) != float(row['line'])):
        return row
    candidate = dict(row, selection=directions[0].upper())
    try:
        updated = market_identity(candidate)
    except (Blocked, ValueError):
        return row
    updated.pop('blocker', None)
    updated['selection'] = row['selection']   # preserve genuine raw selection identity
    return updated


def historical_metric_rates(history: list[dict], targets: list[dict], cutoff: datetime):
    """Observed count rates only, no synthetic match statistics or provider calls."""
    by_team = defaultdict(list)
    for m in sorted(history, key=lambda r:r['date']):
        if m['date'] >= cutoff or m['date'].date() >= cutoff.date():
            continue
        group = m['group']
        for family,(hk,ak) in COUNT_STATS.items():
            hh, aa = number(m['stats'].get(hk)), number(m['stats'].get(ak))
            if hh is None or aa is None or hh < 0 or aa < 0:
                continue
            by_team[group,m['home'],family].append(('home', hh, aa))
            by_team[group,m['away'],family].append(('away', aa, hh))
    rates = {}
    for t in targets:
        for family in COUNT_STATS:
            home = by_team[t['group'], t['home'], family][-20:]
            away = by_team[t['group'], t['away'], family][-20:]
            hv = [r for r in home if r[0]=='home'][-8:]
            av = [r for r in away if r[0]=='away'][-8:]
            if min(len(home),len(away)) < 8 or min(len(hv),len(av)) < 3:
                continue
            def avg(rows,idx):return sum(r[idx] for r in rows)/len(rows)
            ha = .35*avg(home,1)+.35*avg(hv,1)+.30*avg(av,2)
            aa = .35*avg(away,1)+.35*avg(av,1)+.30*avg(hv,2)
            rates[t['group'], t['fixtureId'], family] = (max(.05,ha),max(.05,aa))
    return rates


def open_app_ready(path: Path, tmp: Path) -> Path:
    if path.suffix.lower() != '.zip':
        if not path.is_file(): raise FileNotFoundError(path)
        return path
    with ZipFile(path) as archive:
        dbs=[i for i in archive.infolist() if not i.is_dir() and i.filename.endswith(('.db','.sqlite','.sqlite3'))]
        if len(dbs)!=1:raise ValueError(f'Expected one SQLite in archive, found {len(dbs)}')
        with archive.open(dbs[0]) as f, (tmp/'app_ready.db').open('wb') as out:
            import shutil
            shutil.copyfileobj(f,out)
    return tmp/'app_ready.db'


def strongest_per_market(candidates: list[dict]) -> tuple[list[dict], int]:
    """At most one selection per fixture + market; keep maximum indicative EV."""
    ordered = sorted(candidates, key=lambda r: (-r['indicativeEV'],
                                               -r['winProbability'], r['kickoff']))
    unique = set()
    result = []
    for row in ordered:
        key = (row['competitionId'], row['fixtureId'], row['market'])
        if key in unique:
            continue
        unique.add(key)
        result.append(row)
    return result, len(candidates) - len(result)


def bind_bundled_bookmakers(path: Path, candidates: list[dict]) -> None:
    """Bind only exact fixture/market/selection/team/line/price in the same ZIP."""
    if path.suffix.lower() != '.zip':
        return
    quotes = defaultdict(list)
    with ZipFile(path) as archive:
        for name in archive.namelist():
            if not name.startswith('files/app_ready_odds/') or not name.endswith('.json'):
                continue
            payload = json.loads(archive.read(name))
            scopes = payload.get('leagues', [payload])
            for scope in scopes:
                for match in scope.get('matches', []):
                    for ordinal, market in enumerate(match.get('markets', [])):
                        key = (Path(name).stem, str(match.get('id')), market.get('market'),
                               market.get('selection'), market.get('team'), market.get('line'),
                               market.get('odds'))
                        quotes[key].append({'bookmaker': market.get('bookmaker'),
                                            'exactBookmakerOdds': market.get('exactBookmakerOdds'),
                                            'bundledOddsPath': name, 'marketOrdinal': ordinal,
                                            'match': {
                                                **{k: scope.get(k) for k in
                                                   ('leagueCode', 'country', 'competition', 'season')},
                                                **{k: v for k, v in match.items() if k in
                                                   ('id', 'date', 'kickoff', 'homeTeam', 'awayTeam',
                                                    'providerHomeTeam', 'providerAwayTeam',
                                                    'canonicalHomeTeam', 'canonicalAwayTeam',
                                                    'homeTeamLogo', 'awayTeamLogo',
                                                    'teamMappingStatus', 'usableForStats')},
                                            }})
    for row in candidates:
        matches = quotes.get((row['competitionId'], row['fixtureId'], row['market'],
                              row['selection'], row['team'], row['line'], row['odd']), [])
        row['bookmakerBinding'] = 'EXACT_UNIQUE_BUNDLED_QUOTE' if len(matches) == 1 else 'MISSING_OR_AMBIGUOUS'
        row['bookmaker'] = matches[0]['bookmaker'] if len(matches) == 1 else None
        row['bundledQuoteSources'] = matches


def evaluate(root: Path, db: Path, generation: str, now: datetime) -> dict:
    inventory, raw_blockers = full_inventory(db, generation)
    history, history_errors, groups = read_fixtures(root)
    history, period_diagnostics = apply_real_period_labels(root, history)
    bridge = CachedHistoryBridge(root, history, groups)
    history = [h for h in bridge.history if h['date'].date() < now.date()]
    by_code=defaultdict(set)
    for group in groups: by_code[group.split('|',1)[0]].add(group)
    names=defaultdict(set)
    for h in history:
        names[h['group'],canonical_name(h['home'])].add(h['home'])
        names[h['group'],canonical_name(h['away'])].add(h['away'])
    # Bind provider names to verified team IDs, never across league scopes.
    for raw in bridge.raw_history.values():
        if raw['ids'] and raw['date'].date() < now.date():
            names[raw['group'],canonical_name(raw['home'])].add(team_key(raw['leagueId'],raw['ids'][0]))
            names[raw['group'],canonical_name(raw['away'])].add(team_key(raw['leagueId'],raw['ids'][1]))
    fixtures={}
    fixture_trace=[]
    market_history_trace=[]
    def fixture_failure(reason, row, **details):
        fixture_trace.append(dict(reason=reason,competitionId=row.get('competitionId'),
            fixtureId=row.get('fixtureId'),leagueCode=row.get('leagueCode'),
            season=row.get('season'),kickoff=row.get('kickoff') or str(row.get('date') or ''),
            homeTeam=row.get('homeTeam'),awayTeam=row.get('awayTeam'),**details))
    reject=Counter()
    fixture_reject=Counter()
    seen_fixtures=set()
    future_fixtures=set()
    for r in inventory:
        fid=(r['competitionId'],r['fixtureId'])
        if fid in seen_fixtures:continue
        seen_fixtures.add(fid)
        if r['kickoff'] is None or utc(r['kickoff']) <= now:
            reject['PAST_OR_INVALID_KICKOFF']+=1
            fixture_reject['PAST_OR_INVALID_KICKOFF']+=1
            continue
        future_fixtures.add(fid)
        code = r['leagueCode']
        possible = by_code.get(code, set())
        if not possible:
            reject['NO_DOMESTIC_HISTORY_FOR_LEAGUE'] += 1
            fixture_reject['NO_DOMESTIC_HISTORY_FOR_LEAGUE'] += 1
            fixture_failure('NO_DOMESTIC_HISTORY_FOR_LEAGUE',r)
            continue
        # League title can change across historical seasons. Resolve by the
        # actual two independent team names in each scope, never a guessed alias.
        provider_identity,identity_reason,identity_evidence=bridge.resolve(r)
        matched=[provider_identity] if provider_identity else []
        for group in ([] if matched else sorted(possible)):
            sides = []
            for key in ('home', 'away'):
                choices = [r.get('canonicalHomeTeam' if key == 'home' else 'canonicalAwayTeam'),
                           r.get('homeTeam' if key == 'home' else 'awayTeam'),
                           r.get('providerHomeTeam' if key == 'home' else 'providerAwayTeam')]
                identified = set()
                for label in choices:
                    if label:
                        identified |= names.get((group, canonical_name(label)), set())
                sides.append(identified)
            if len(sides[0]) == len(sides[1]) == 1 and sides[0] != sides[1]:
                matched.append((group, next(iter(sides[0])), next(iter(sides[1]))))
        if not matched:
            reject['UNRESOLVED_HISTORICAL_TEAM_IDENTITY'] += 1
            fixture_reject['UNRESOLVED_HISTORICAL_TEAM_IDENTITY'] += 1
            fixture_failure('UNRESOLVED_HISTORICAL_TEAM_IDENTITY',r,
                providerResolution=identity_reason,providerEvidence=identity_evidence,
                historicalScopes=sorted(possible))
            continue
        if len(matched) != 1:
            reject['AMBIGUOUS_HISTORICAL_LEAGUE_SCOPE'] += 1
            fixture_reject['AMBIGUOUS_HISTORICAL_LEAGUE_SCOPE'] += 1
            fixture_failure('AMBIGUOUS_HISTORICAL_LEAGUE_SCOPE',r,
                matchingHistoricalScopes=[m[0] for m in matched])
            continue
        group, home, away = matched[0]
        fixtures[fid] = {'group': group, 'fixtureId': r['fixtureId'], 'date': utc(r['kickoff']),
                         'home': home, 'away': away,
                         'leagueCode': code, 'competitionId': r['competitionId'],
                         'homeTeam': r['homeTeam'], 'awayTeam': r['awayTeam'],
                          'identityMethod':'EXACT_API_IDS' if provider_identity else 'UNIQUE_ALIAS_MATCH'}
    targets=list(fixtures.values())
    synthetic=[{'group':x['group'],'fixture_id':x['fixtureId'],'date':x['date'],
                'home':x['home'],'away':x['away'],'hg':None,'ag':None,'stats':{},
                'prematch_target':True} for x in targets]
    xg_rows,_=make_rows(history+synthetic)
    elo_rows,_=make_elo_rows(history+synthetic)
    target_ids = {(t['group'], t['fixtureId']) for t in targets}
    xg_by_id = {(r.league, r.fixture): r for r in xg_rows if (r.league, r.fixture) in target_ids}
    elo_by_id = {(r.league, r.fixture): r for r in elo_rows if (r.league, r.fixture) in target_ids}
    metric_rates=historical_metric_rates(history,targets,now)
    models={}
    for t in targets:
        fid=t['fixtureId']; xg=xg_by_id.get((t['group'], fid))
        if xg:
            h,a=model_lambdas(xg,XG_PARAMS); mode='XG_PRIMARY'
        else:
            erow=elo_by_id.get((t['group'], fid))
            if erow is None:
                reject['NO_PRIOR_TEAM_FORM_FOR_GOALS_MODEL']+=1
                fixture_reject['NO_PRIOR_TEAM_FORM_FOR_GOALS_MODEL']+=1
                home_rows=[m for m in history if m['group']==t['group'] and t['home'] in (m['home'],m['away'])]
                away_rows=[m for m in history if m['group']==t['group'] and t['away'] in (m['home'],m['away'])]
                fixture_failure('NO_PRIOR_TEAM_FORM_FOR_GOALS_MODEL',t,
                    group=t['group'],identityMethod=t['identityMethod'],
                    priorHomeMatches=len(home_rows),priorAwayMatches=len(away_rows),
                    priorHomeVenue=sum(m['home']==t['home'] for m in home_rows),
                    priorAwayVenue=sum(m['away']==t['away'] for m in away_rows))
                continue
            h,a=lambda_rates(erow,ELO_PARAMS);mode='ELO_GOALS_FALLBACK_NO_XG'
        models[t['competitionId'],fid]=(h,a,mode)
    results=[]
    market_rejections=defaultdict(Counter)
    evaluated_markets=Counter()
    selection_rejections=Counter()
    def reject_selection(row, reason):
        reject[reason] += 1
        selection_rejections[reason] += 1
        market_rejections[row['market'] or 'UNQUOTED'][reason] += 1
    for item in inventory:
        r=normalize_identity(item)
        if r.get('blocker'):
            reject_selection(r, r['blocker']);continue
        try:odd=float(r['odd'])
        except (TypeError,ValueError):reject_selection(r, 'INVALID_PRICE');continue
        if not math.isfinite(odd) or not (ODDS_LOW < odd < ODDS_HIGH):
            reject_selection(r, 'ODDS_OUTSIDE_USER_RANGE');continue
        row_model=models.get((r['competitionId'],r['fixtureId']))
        if row_model is None:
            reject_selection(r, 'MISSING_REAL_HISTORY_FORECAST');continue
        h,a,mode=row_model;family=r['family']
        if family=='GOALS_FT':rates=(h,a)
        else:rates=metric_rates.get((fixtures[(r['competitionId'], r['fixtureId'])]['group'], r['fixtureId'], family)) if (r['competitionId'], r['fixtureId']) in fixtures else None
        if rates is None:
            match=fixtures.get((r['competitionId'],r['fixtureId']))
            fields=COUNT_STATS.get(family)
            valid_rows=[m for m in history if match and fields and m['group']==match['group']
                and (match['home'] in (m['home'],m['away']) or match['away'] in (m['home'],m['away']))
                and all(number(m['stats'].get(k)) is not None for k in fields)]
            market_history_trace.append(dict(competitionId=r['competitionId'],fixtureId=r['fixtureId'],
                leagueCode=r['leagueCode'],market=r['market'],selection=r['selection'],
                team=r.get('team'),line=r.get('line'),family=family,
                group=match['group'] if match else None,observedHistoryRows=len(valid_rows),
                requiredMetricFields=fields))
            reject_selection(r, 'MISSING_MARKET_SPECIFIC_HISTORY');continue
        try:p=probability(r,*rates)
        except (ValueError,OverflowError):p=None
        if p is None:
            reject_selection(r, 'UNSUPPORTED_SELECTION_OR_SETTLEMENT');continue
        evaluated_markets[r['market']] += 1
        win,push=p;ev=win*(odd-1)-(1-win-push)
        if win < P_MIN or ev<=0:
            reject_selection(r, 'LOW_MODEL_WIN_PROBABILITY_OR_INDICATIVE_EV');continue
        results.append({
            'fixtureId':r['fixtureId'],'competitionId':r['competitionId'],
            'league':r['leagueCode'],'kickoff':r['kickoff'],
            'homeTeam':r['homeTeam'],'awayTeam':r['awayTeam'],
            'market':r['market'],'selection':r['selection'],'team':r['team'],
            'line':r['line'],'odd':odd,'model':mode,'winProbability':round(win,6),
            'pushProbability':round(push,6),'indicativeEV':round(ev,6),
            'modelExpectedHomeGoals':round(h,4),'modelExpectedAwayGoals':round(a,4),
            'certified':False,'category':'UNVERIFIED_REAL_DATA_DIAGNOSTIC',
            'selectionId':r.get('selectionId'),
            'fixtureKey':r.get('fixtureKey'), 'snapshotVersion':r.get('snapshotVersion'),
            'sourceRecordSha256':r.get('sourceRecordSha256'),
            'marketModelKey':r['marketModelKey'], 'family':family,
            'probabilityModel':mode if family == 'GOALS_FT' else 'OBSERVED_MARKET_HISTORY_POISSON',
            'marketExpectedHomeCount':round(rates[0],6), 'marketExpectedAwayCount':round(rates[1],6),
        })
    raw_candidates = len(results)
    results, duplicates_removed = strongest_per_market(results)
    return {'status':'REAL_PROD_INPUT_DIAGNOSTIC','source':'CANONICAL_APP_READY_SQLITE',
            'generatedAt':now.isoformat(),'generationId':generation,
            'modelVersion':PARAMS_VERSION,
            'totalFixtureRows':len({(r['competitionId'],r['fixtureId']) for r in inventory}),
            'rawJoinedRows':len(inventory),
            'rawQuotedRows':sum(bool(r['market']) for r in inventory),
            'historyRows':len(history),'historicalPeriodDiagnostics':period_diagnostics,
            'historySourceErrors':dict(history_errors),
            'futureFixtures':len(future_fixtures),
            'phase1DataLineage':bridge.coverage(),
            'phase1RejectedFixtures':fixture_trace,
            'phase1MarketHistoryFailures':market_history_trace,
            'forecastableFixtures':len(models),
            'rawCandidateCountBeforeDeduplication':raw_candidates,
            'suppressedSameMatchSameMarket':duplicates_removed,
            'candidateCount':len(results),'candidates':results,
            'rawMarketCounts':dict(Counter(r['market'] or 'UNQUOTED' for r in inventory)),
            'evaluatedMarketCounts':dict(evaluated_markets),
            'candidateMarketCounts':dict(Counter(r['market'] for r in results)),
            'marketRejections':{k:dict(v) for k,v in market_rejections.items()},
            'fixtureRejections':dict(fixture_reject),
            'selectionRejections':dict(selection_rejections),
            'rejections':dict(reject.most_common()),
            'oddsNotIndependentlyTimestampVerified':True,
            'probabilitiesNotEmpiricallyCertified':True,'apiCalls':0,
            'prodWrites':0}


def main()->None:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repository-root',type=Path,required=True)
    p.add_argument('--app-ready',type=Path,required=True,help='Real App-Ready betting ZIP or SQLite')
    p.add_argument('--generation-id',required=True)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    now=datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory(prefix='statmaker-v2-prod-readonly-') as d:
        report=evaluate(args.repository_root,open_app_ready(args.app_ready,Path(d)),args.generation_id,now)
    manifest_path=args.repository_root/'data/statmaker/app_ready/update_manifest.json'
    manifest_bytes=manifest_path.read_bytes()
    manifest=json.loads(manifest_bytes)
    artifacts=[a for a in manifest['artifacts'] if a['id']=='app_ready_betting_bundle']
    if len(artifacts)!=1:
        raise ValueError('Missing or ambiguous canonical betting bundle')
    artifact=artifacts[0]
    bundle_sha=hashlib.sha256(args.app_ready.read_bytes()).hexdigest()
    if (args.app_ready.resolve() != (args.repository_root/artifact['path']).resolve()
            or bundle_sha != artifact['sha256']
            or args.generation_id != manifest['metadata']['preparedPatternGenerationId']):
        raise ValueError('Canonical bundle/generation mismatch')
    bind_bundled_bookmakers(args.app_ready, report['candidates'])
    report['sourceManifest']={'path':str(manifest_path.resolve()),
                              'sha256':hashlib.sha256(manifest_bytes).hexdigest(),
                              'generatedAt':manifest.get('generatedAt')}
    report['sourceBundle']={**artifact,'verifiedSha256':bundle_sha}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x',encoding='utf-8') as f:
        json.dump(report,f,ensure_ascii=False,indent=2,allow_nan=False)
    print(json.dumps({k:report[k] for k in ('rawJoinedRows','rawQuotedRows','historyRows',
                                        'forecastableFixtures','candidateCount','apiCalls')},ensure_ascii=False))
if __name__=='__main__':main()
