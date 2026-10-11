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
}


def utc(value: str) -> datetime:
    dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if dt.utcoffset() is None:
        raise ValueError('Missing UTC offset')
    return dt.astimezone(timezone.utc)


def poisson(rate: float, max_goals: int = 35) -> list[float]:
    if not math.isfinite(rate) or rate < 0:
        raise ValueError('Invalid Poisson rate')
    out = [math.exp(-rate)]
    for k in range(1, max_goals + 1):
        out.append(out[-1] * rate / k)
    if 1-sum(out) > 1e-8:
        raise ValueError('Poisson tail too large')
    return out


def probability(model: dict, h: float, a: float) -> tuple[float,float] | None:
    key = model['marketModelKey']
    side = model['side']
    line = model.get('line')
    home, away = poisson(h), poisson(a)
    if key in ('RESULT_1X2', 'HT_RESULT_1X2', 'RESULT_DOUBLE_CHANCE',
               'HT_RESULT_DOUBLE_CHANCE', 'RESULT_DNB', 'BTTS'):
        ph = sum(x*y for i,x in enumerate(home) for j,y in enumerate(away) if i > j)
        pd = sum(x*y for i,x in enumerate(home) for j,y in enumerate(away) if i == j)
        pa = max(0., 1. - ph - pd)
        result = {'HOME': ph, 'DRAW': pd, 'AWAY': pa, 'YES': (1-home[0])*(1-away[0]),
                  'NO': home[0]+away[0]-home[0]*away[0],
                  'HOME_OR_DRAW': ph+pd, 'AWAY_OR_DRAW': pa+pd,
                  'HOME_OR_AWAY': ph+pa}
        if key == 'RESULT_DNB':
            return ((ph if side == 'HOME' else pa), pd) if side in ('HOME','AWAY') else None
        if key in ('RESULT_1X2','HT_RESULT_1X2') and side not in ('HOME','DRAW','AWAY'):
            return None
        if key in ('RESULT_DOUBLE_CHANCE','HT_RESULT_DOUBLE_CHANCE') and side not in ('HOME_OR_DRAW','AWAY_OR_DRAW','HOME_OR_AWAY'):
            return None
        if key == 'BTTS' and side not in ('YES','NO'):
            return None
        return (result[side], 0.) if side in result else None
    if key.endswith('TOTAL') or key.endswith('_CORNERS') or key.endswith('_SOT') or key.endswith('_SHOTS') or key.endswith('_YELLOW_CARDS'):
        if side not in ('OVER', 'UNDER') or line is None:
            return None
        line = float(line)
        if line < 0 or (line*2) != int(line*2):
            return None
        if key.startswith('HOME_'):
            counts = home
        elif key.startswith('AWAY_'):
            counts = away
        else:
            counts = [sum(home[i]*away[n-i] for i in range(max(0,n-len(away)+1),min(n+1,len(home))))
                      for n in range(len(home)+len(away)-1)]
        over = sum(p for count,p in enumerate(counts) if count > line)
        under = sum(p for count,p in enumerate(counts) if count < line)
        push = max(0., 1. - over - under)
        return (over if side == 'OVER' else under, push)
    return None


def normalize_identity(row: dict) -> dict:
    """No made-up prices/lines: normalize only literal Over/Under label + real line."""
    if row.get('blocker') != 'MISSING_COUNT_LINE_OR_SIDE':
        return row
    label = str(row.get('selection') or '')
    m = re.fullmatch(r'\s*(Over|Under)\s+(\d+(?:\.\d+)?)\s*', label, re.I)
    if not m or row.get('line') is None or float(m.group(2)) != float(row['line']):
        return row
    candidate = dict(row, selection=m.group(1).upper())
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
            rates[t['fixtureId'],family] = (max(.05,ha),max(.05,aa))
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


def evaluate(root: Path, db: Path, generation: str, now: datetime) -> dict:
    inventory, raw_blockers = full_inventory(db, generation)
    history, history_errors, groups = read_fixtures(root)
    history = [h for h in history if h['date'].date() < now.date()]
    by_code=defaultdict(set)
    for group in groups: by_code[group.split('|',1)[0]].add(group)
    names=defaultdict(set)
    for h in history:
        names[h['group'],canonical_name(h['home'])].add(h['home'])
        names[h['group'],canonical_name(h['away'])].add(h['away'])
    fixtures={}
    reject=Counter()
    for r in inventory:
        fid=(r['competitionId'],r['fixtureId'])
        if fid in fixtures:continue
        if r['kickoff'] is None or utc(r['kickoff']) <= now:
            reject['PAST_OR_INVALID_KICKOFF']+=1
            continue
        code=r['leagueCode']; possible=by_code.get(code,set())
        if len(possible)!=1:
            reject['NO_UNIQUE_DOMESTIC_HISTORY_SCOPE']+=1
            continue
        group=next(iter(possible)); sides=[]
        for key in ('home','away'):
            choices=[r.get('canonicalHomeTeam' if key=='home' else 'canonicalAwayTeam'),
                     r.get('homeTeam' if key=='home' else 'awayTeam'),
                     r.get('providerHomeTeam' if key=='home' else 'providerAwayTeam')]
            identified=set()
            for label in choices:
                if label: identified |= names.get((group,canonical_name(label)),set())
            sides.append(identified)
        if len(sides[0])!=1 or len(sides[1])!=1 or sides[0]==sides[1]:
            reject['UNRESOLVED_HISTORICAL_TEAM_IDENTITY']+=1
            continue
        fixtures[fid]={'group':group,'fixtureId':r['fixtureId'],'date':utc(r['kickoff']),
                       'home':next(iter(sides[0])),'away':next(iter(sides[1])),
                       'leagueCode':code,'competitionId':r['competitionId'],
                       'homeTeam':r['homeTeam'],'awayTeam':r['awayTeam']}
    targets=list(fixtures.values())
    synthetic=[{'group':x['group'],'fixture_id':x['fixtureId'],'date':x['date'],
                'home':x['home'],'away':x['away'],'hg':None,'ag':None,'stats':{},
                'prematch_target':True} for x in targets]
    xg_rows,_=make_rows(history+synthetic)
    elo_rows,_=make_elo_rows(history+synthetic)
    xg_by_id={r.fixture:r for r in xg_rows if r.fixture in {t['fixtureId'] for t in targets}}
    elo_by_id={r.fixture:r for r in elo_rows if r.fixture in {t['fixtureId'] for t in targets}}
    metric_rates=historical_metric_rates(history,targets,now)
    models={}
    for t in targets:
        fid=t['fixtureId']; xg=xg_by_id.get(fid)
        if xg:
            h,a=model_lambdas(xg,XG_PARAMS); mode='XG_PRIMARY'
        else:
            erow=elo_by_id.get(fid)
            if erow is None:
                reject['NO_PRIOR_TEAM_FORM_FOR_GOALS_MODEL']+=1;continue
            h,a=lambda_rates(erow,ELO_PARAMS);mode='ELO_GOALS_FALLBACK_NO_XG'
        models[t['competitionId'],fid]=(h,a,mode)
    results=[]
    for item in inventory:
        r=normalize_identity(item)
        if r.get('blocker'):
            reject[r['blocker']]+=1;continue
        try:odd=float(r['odd'])
        except (TypeError,ValueError):reject['INVALID_PRICE']+=1;continue
        if not math.isfinite(odd) or not (ODDS_LOW < odd < ODDS_HIGH):
            reject['ODDS_OUTSIDE_USER_RANGE']+=1;continue
        row_model=models.get((r['competitionId'],r['fixtureId']))
        if row_model is None:
            reject['MISSING_REAL_HISTORY_FORECAST']+=1;continue
        h,a,mode=row_model;family=r['family']
        if family=='GOALS_FT':rates=(h,a)
        else:rates=metric_rates.get((r['fixtureId'],family))
        if rates is None:
            reject['MISSING_MARKET_SPECIFIC_HISTORY']+=1;continue
        try:p=probability(r,*rates)
        except (ValueError,OverflowError):p=None
        if p is None:
            reject['UNSUPPORTED_SELECTION_OR_SETTLEMENT']+=1;continue
        win,push=p;ev=win*(odd-1)-(1-win-push)
        if win < P_MIN or ev<=0:
            reject['LOW_MODEL_WIN_PROBABILITY_OR_INDICATIVE_EV']+=1;continue
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
        })
    results.sort(key=lambda r:(-r['indicativeEV'],-r['winProbability'],r['kickoff']))
    return {'status':'REAL_PROD_INPUT_DIAGNOSTIC','source':'CANONICAL_APP_READY_SQLITE',
            'generatedAt':now.isoformat(),'generationId':generation,
            'modelVersion':PARAMS_VERSION,
            'totalFixtureRows':len({(r['competitionId'],r['fixtureId']) for r in inventory}),
            'rawJoinedRows':len(inventory),
            'rawQuotedRows':sum(bool(r['market']) for r in inventory),
            'historyRows':len(history),'forecastableFixtures':len(models),
            'candidateCount':len(results),'candidates':results,
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
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x',encoding='utf-8') as f:
        json.dump(report,f,ensure_ascii=False,indent=2,allow_nan=False)
    print(json.dumps({k:report[k] for k in ('rawJoinedRows','rawQuotedRows','historyRows',
                                        'forecastableFixtures','candidateCount','apiCalls')},ensure_ascii=False))
if __name__=='__main__':main()