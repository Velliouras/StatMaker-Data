"""Frozen research weights + their own prior-window profile on real live inputs.

Updates only completed UTC days after the saved pretest state. No refit, price
feature, empirical tier certification, or fallback to the old simulations.
"""
from collections import defaultdict
from datetime import datetime, timezone
import gzip, hashlib, json, math, sqlite3
from pathlib import Path
import numpy as np
from scipy.special import expit
from betting_v2.opponent_matchup import OpponentMatchup, timestamp
from betting_v2.validated_walk_forward import distribution
from betting_v2.exact_forecast_adapter import forecast, prepare
from betting_v2.joint_count import VERSION


class FrozenFit:
    def __init__(self, values):
        self.coef_ = np.asarray(values['coef'])
        self.intercept_ = np.asarray(values['intercept'])
    def predict(self, x):
        return np.exp(np.asarray(x @ self.coef_).ravel() + self.intercept_)
    def predict_proba(self, x):
        p = expit(np.asarray(x @ self.coef_.T + self.intercept_).ravel())
        return np.column_stack((1-p, p))


def restore(state, history, fit_before, metric_values, sample_counts=None):
    model = OpponentMatchup.__new__(OpponentMatchup)
    model.feature_exclusions = frozenset()
    model.use_goal_strength = True
    model.history = defaultdict(lambda: defaultdict(list))
    for team, metrics in state['history'].items():
        for metric, records in metrics.items():
            model.history[team][metric] = [tuple(r) for r in records]
    model.elo = defaultdict(lambda: 1500., state['elo'])
    model.xg = defaultdict(list, {k:[tuple(r) for r in v] for k,v in state['xg'].items()})
    model.groups = state['groups']
    model.matches = state['training']
    model.models, model.columns, model.feature_scales = {}, {}, {}
    model.train = defaultdict(list)
    # Training sample metadata was not exported separately. Reconstruct counts
    # from the same source history; never call an estimator's fit method.
    before = [r for r in history if r['_date'] < fit_before]
    if sample_counts is None and len(before) != model.matches:
        raise ValueError('Frozen model training source no longer matches')
    for metric, values in state['weights'].items():
        if metric == 'period':
            model.period_model = FrozenFit(values)
            model.period_columns = values['columns']
            model.period_scales = np.asarray(values['scales'])
        else:
            model.models[metric] = FrozenFit(values)
            model.columns[metric] = values['columns']
            model.feature_scales[metric] = np.asarray(values['scales'])
            count = (sample_counts['metrics'][metric] if sample_counts is not None else
                     sum(metric_values(r,metric) is not None for r in before))
            model.train[metric] = range(2*count)
    if 'period' not in state['weights']: model.period_model = None
    period_count = (sample_counts['periodObservations'] if sample_counts is not None else
        sum(sum(v>0 for v in metric_values(r,'GOALS_FT'))
            for r in before if metric_values(r,'GOALS_1H') is not None))
    model.period_train = range(period_count)
    return model


def load_live(root, history, cutoff):
    directory = Path(root)/'reports/betting_v2/corrected_20261015'
    inventory = json.loads((directory/'inventory.json').read_text())
    source_end = max(timestamp(f[2]) for f in inventory['folds'])
    if cutoff >= source_end:
        original = [r for r in history if r['_date'] < source_end]
        source_sha = hashlib.sha256(json.dumps(original,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        if source_sha != inventory['dataSha256']:
            raise ValueError('Frozen training history changed; rebuild paired weights/profile')
    folds = [f for f in inventory['folds'] if timestamp(f[1]) <= cutoff]
    if not folds: raise ValueError('No prior frozen model for publication date')
    fitday, stateday, _ = max(folds, key=lambda f:f[1])
    stem = 'full_'+stateday
    state_path = directory/(stem+'_pretest_state.json.gz')
    profile_path = directory/(stem+'_parameters.json')
    state_bytes, profile_bytes = state_path.read_bytes(), profile_path.read_bytes()
    state = json.loads(gzip.decompress(state_bytes)); profile = json.loads(profile_bytes)
    from materialize_betting_v2_direct import metric_values
    model = restore(state, history, timestamp(fitday), metric_values)
    # Exclude the current day even if its first matches already completed.
    for row in history:
        if timestamp(stateday) <= row['_date'] < cutoff:
            model.observe(row,metric_values)
    return model, profile, dict(model=VERSION, variant='full', fittedBefore=fitday,
        calibrationFrom=fitday, calibrationBefore=stateday, featureCutoffUTC=
        datetime.fromtimestamp(cutoff,timezone.utc).isoformat(),
        stateSha256=hashlib.sha256(state_bytes).hexdigest(),
        profileSha256=hashlib.sha256(profile_bytes).hexdigest(),
        researchDataSha256=inventory['dataSha256'], probabilityMethod='exact_joint_pmf',
        publicationTimeVerified=False, empiricalTiersValidated=False)


def run(db_path, root, as_of=None):
    import materialize_betting_v2_direct as direct
    now = as_of if as_of is not None else datetime.now(timezone.utc).timestamp()
    cutoff = math.floor(now/86400)*86400
    history = direct.load_actual_results(cutoff)['GLOBAL']
    from betting_v2.live_revised import load_revised
    model, profile, provenance = load_revised(root,history,cutoff)
    counts = defaultdict(int); pending = []; uncertainty = []; cache = {}
    with sqlite3.connect(db_path) as con:
        con.row_factory = sqlite3.Row
        # An additive UAT table: the old forecast and non-betting tables remain.
        con.execute('DROP TABLE IF EXISTS prepared_betting_v4_forecasts')
        con.execute('CREATE TABLE prepared_betting_v4_forecasts AS '
                    'SELECT * FROM prepared_betting_v2_forecasts WHERE 0')
        con.execute('CREATE UNIQUE INDEX idx_betting_v4_scope ON prepared_betting_v4_forecasts'
                    '(competition_id,snapshot_version,match_key,market_ordinal)')
        forecast_columns=[r[1] for r in con.execute('PRAGMA table_info(prepared_betting_v4_forecasts)')]
        for column in ('decision_win_probability','decision_push_probability','decision_ev','decision_home_scenario_probability','decision_away_scenario_probability'):
            con.execute('ALTER TABLE prepared_betting_v4_forecasts ADD COLUMN '+column+' REAL')
        con.execute('DROP TABLE IF EXISTS prepared_betting_v4_meta')
        con.execute('CREATE TABLE prepared_betting_v4_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        rows = con.execute('''SELECT f.*,q.ordinal,q.market,q.selection,q.team,q.line,q.odd
            FROM prepared_fixture_matches f JOIN prepared_betting_v2_quotes q
            ON q.competition_id=f.competition_id AND q.snapshot_version=f.snapshot_version
            AND q.match_key=f.match_key
            WHERE f.usable_for_stats=1 OR
            (f.competition_id IN ('champions_league','europa_league','conference_league')
            AND f.team_mapping_status='provider_identity' AND TRIM(f.id)<>''
            AND TRIM(f.home_team)<>'' AND TRIM(f.away_team)<>'')''').fetchall()
        for row in rows:
            ko = direct.kickoff(row['kickoff']) or direct.kickoff(row['date'])
            if ko < cutoff: continue
            aliases = direct.UEFA_ALIASES.get(direct.league_code(row['league_code']),{})
            def identity(keys):
                values = [aliases.get(direct.name(row[k]),direct.name(row[k])) for k in keys]
                return next((v for v in values if v in model.history),next((v for v in values if v),''))
            home = identity(('canonical_home_team','home_team','provider_home_team'))
            away = identity(('canonical_away_team','away_team','provider_away_team'))
            spec = direct.quote_market(row,home,away)
            if not spec: counts['unsupported'] += 1; continue
            metric = spec[0]; key = home,away,metric
            if key not in cache:
                prediction = model.predict(home,away,metric)
                full = model.predict(home,away,'GOALS_FT')
                if prediction is None or full is None: cache[key] = None
                else:
                    regime = 'XG' if len(model.xg[home])>=8 and len(model.xg[away])>=8 else 'ELO_NO_XG'
                    target = dict(metric=metric,regime=regime,means=list(prediction[:2]))
                    if metric in ('GOALS_1H','GOALS_2H'):
                        target.update(fullMeans=list(full[:2]),shares=[prediction[i]/full[i] for i in (0,1)])
                    fullrow = dict(metric='GOALS_FT',regime=regime,means=list(full[:2]))
                    full_dist = distribution(fullrow,profile)
                    cache[key] = target,prediction,full_dist,prepare(target,profile)
            if cache[key] is None: counts['no_frozen_team_model'] += 1; continue
            target,prediction,full,prepared = cache[key]
            result = forecast(target,profile,spec,prepared)
            # Card counts lack a verified bookmaker settlement contract.
            if 'CARD' in metric: counts['unverified_card_settlement'] += 1; continue
            if hasattr(model,'bounds'):
                bound=model.bounds(home,away,spec,row['odd'])
                if bound is not None:
                    if (not all(math.isfinite(v) for v in bound.values()) or
                            not 0<=bound['win']<=result['winProbability']+1e-12 or
                            not 0<=bound['push']<=result['pushProbability']+1e-12 or
                            bound['ev']>result['winProbability']*row['odd']+result['pushProbability']-1+1e-12):
                        raise ValueError('Invalid conservative decision sensitivity')
                    bound['win']=min(bound['win'],result['winProbability'])
                    bound['push']=min(bound['push'],result['pushProbability'])
                    bound['ev']=bound['win']*row['odd']+bound['push']-1
                    uncertainty.append((bound['win'],bound['push'],bound['ev'],bound['home'],bound['away'],
                        row['competition_id'],row['snapshot_version'],row['match_key'],row['ordinal']))
            pending.append((row['competition_id'],row['snapshot_version'],row['match_key'],row['ordinal'],
                row['market'],row['selection'],row['team'],row['line'],row['odd'],
                result['winProbability'],result['pushProbability'],VERSION,metric,0,*prediction[2:5],
                result['homeMean'],result['awayMean'],
                full.evaluate(('GOALS_FT','RESULT','HOME','',None))[0],
                full.evaluate(('GOALS_FT','RESULT','AWAY','',None))[0],
                result['scenarios']['HOME']['winProbability'],result['scenarios']['AWAY']['winProbability'],0,0))
        con.executemany('INSERT INTO prepared_betting_v4_forecasts ('+','.join(forecast_columns)+') VALUES ('+','.join('?' for _ in range(25))+')',pending)
        con.executemany('UPDATE prepared_betting_v4_forecasts SET decision_win_probability=?,decision_push_probability=?,decision_ev=?,'
            'decision_home_scenario_probability=?,decision_away_scenario_probability=? '
            'WHERE competition_id=? AND snapshot_version=? AND match_key=? AND market_ordinal=?',uncertainty)
        con.executemany('INSERT INTO prepared_betting_v4_meta VALUES (?,?)',
                        [(k,json.dumps(v)) for k,v in provenance.items()])
        con.commit()
        counts['forecasts'] = len(pending)
        counts['fixtures'] = len({r[:3] for r in pending})
        counts['providerCalls'] = 0
        counts['uncertaintyForecasts'] = len(uncertainty)
    return dict(counts)
