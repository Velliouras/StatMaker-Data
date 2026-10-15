"""Offline recovery experiment. No provider calls or live publishing.

Frozen count models; earlier calibration fits mean/dispersion, later test scores
coherent count distributions. Existing V3 is the paired baseline. Research only:
cached final stats have event-time cutoffs, not historical publication receipts.
"""
from pathlib import Path
import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
import sys
import time

import numpy as np
from scipy.optimize import minimize
from scipy.stats import nbinom, poisson

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import materialize_betting_v2_direct as direct

METRICS = ('GOALS_FT', 'CORNERS', 'SHOTS', 'SOT', 'YELLOW_CARDS', 'CARDS', 'FOULS')
LINES = {'GOALS_FT': (1.5, 2.5, 3.5), 'CORNERS': (7.5, 9.5, 11.5),
         'SHOTS': (17.5, 23.5, 29.5), 'SOT': (5.5, 8.5, 11.5),
         'YELLOW_CARDS': (2.5, 4.5, 6.5), 'CARDS': (2.5, 4.5, 6.5),
         'FOULS': (17.5, 23.5, 29.5)}


def ts(s):
    return datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()


def write(path, value):
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')
    tmp.replace(path)


def distribution(mu, scale, alpha):
    mu = max(1e-8, mu * scale)
    dist = poisson(mu) if alpha < 1e-6 else nbinom(1 / alpha, 1 / (1 + alpha * mu))
    end = int(dist.ppf(1 - 1e-10))
    p = dist.pmf(np.arange(end + 1))
    return p / p.sum()


def fit_calibration(rows):
    mu = np.array([v for r in rows for v in r['means']])
    y = np.array([v for r in rows for v in r['actual']])
    if len(rows) < 100:
        return dict(scale=1., alpha=0., n=len(rows), fitted=False)
    def loss(theta):
        scale, alpha = np.exp(theta)
        return -float(nbinom.logpmf(y, 1 / alpha, 1 / (1 + alpha * mu * scale)).mean())
    result = minimize(loss, [0., np.log(.1)], bounds=[(-.7, .7), (-12., 2.)], method='L-BFGS-B')
    if not result.success:
        raise RuntimeError('Calibration did not converge: ' + result.message)
    scale, alpha = np.exp(result.x)
    return dict(scale=float(scale), alpha=float(alpha), n=len(rows), fitted=True)


def forecasts(model, history, start, end):
    out = []
    for r in history:
        if not start <= r['_date'] < end:
            continue
        h, a = r['_home'], r['_away']
        # Separate truly observed pre-cutoff xG from the no-xG population.
        regime = 'XG' if len(model.xg[h]) >= 8 and len(model.xg[a]) >= 8 else 'ELO_NO_XG'
        for metric in METRICS:
            actual = direct.metric_values(r, metric)
            if actual is None or any(v != int(v) for v in actual):
                continue
            pred = model.predict(h, a, metric)
            if pred is None:
                continue
            out.append(dict(fixture=f"{r['_date']}:{h}:{a}", date=r['_date'],
                            metric=metric, regime=regime, means=pred[:2], actual=actual))
    return out


def events(row, scale, alpha):
    h, a = (distribution(v, scale, alpha) for v in row['means'])
    joint = h[:, None] * a[None, :]
    hi, ai = np.indices(joint.shape)
    hg, ag = row['actual']
    out = []
    emitted = set()
    metric = row['metric']
    def add(name, mask, outcome):
        if name in emitted:
            return
        emitted.add(name)
        out.append((name, float(joint[mask].sum()), int(outcome)))
    for line in LINES[metric]:
        add('MATCH_OVER_' + str(line), hi + ai > line, hg + ag > line)
        # Same line for both teams; this is a fixed diagnostic grid, not odds.
        for side, idx, actual in [('HOME', hi, hg), ('AWAY', ai, ag)]:
            teamline = line / 2
            teamline = int(teamline) + .5
            add('TEAM_' + side + '_OVER_' + str(teamline), idx > teamline, actual > teamline)
    if metric == 'GOALS_FT':
        add('1X2_HOME', hi > ai, hg > ag)
        add('1X2_DRAW', hi == ai, hg == ag)
        add('1X2_AWAY', hi < ai, hg < ag)
        add('DC_1X', hi >= ai, hg >= ag)
        add('DC_X2', hi <= ai, hg <= ag)
        add('DC_12', hi != ai, hg != ag)
        add('BTTS_YES', (hi > 0) & (ai > 0), hg > 0 and ag > 0)
    return out


def summary(rows):
    p = np.array([r['p'] for r in rows]); q = np.array([r['q'] for r in rows]); y = np.array([r['y'] for r in rows])
    def scores(v):
        clipped = np.clip(v, 1e-12, 1 - 1e-12)
        bins = []
        for lo, hi in [(0,.2),(.2,.4),(.4,.6),(.6,.7),(.7,.8),(.8,1.00001)]:
            mask = (v >= lo) & (v < hi)
            if mask.any():
                bins.append(dict(lo=lo, n=int(mask.sum()), predicted=float(v[mask].mean()), observed=float(y[mask].mean())))
        return dict(brier=float(((v-y)**2).mean()), logLoss=float(-(y*np.log(clipped)+(1-y)*np.log1p(-clipped)).mean()), bins=bins)
    # Paired fixture-cluster bootstrap; nested lines are not independent bets.
    groups = defaultdict(list)
    for r in rows:
        groups[r['fixture']].append((r['q']-r['y'])**2 - (r['p']-r['y'])**2)
    sums = np.array([sum(v) for v in groups.values()]); counts = np.array([len(v) for v in groups.values()])
    rng = np.random.default_rng(15102026)
    diffs = []
    for _ in range(1000):
        ix = rng.integers(len(sums), size=len(sums)); diffs.append(float(sums[ix].sum()/counts[ix].sum()))
    return dict(n=len(rows), fixtures=len(groups), baseline=scores(p), candidate=scores(q),
                pairedBrierDelta95=list(map(float,np.quantile(diffs,[.025,.975]))),
                roi=None, yield_=None, pricedBets=0,
                priceLimitation='Fixed event grid, no bookmaker prices joined; not an ROI backtest')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args(); args.output.mkdir(parents=True, exist_ok=True)
    output = args.output
    lock = output / 'running.lock'
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.write(fd, str(os.getpid()).encode()); os.close(fd)
    try:
        source = hashlib.sha256(b''.join((Path(__file__).parent / f).read_bytes() for f in
                 ['opponent_matchup.py','monotone_count.py','recovery_walk_forward.py'])).hexdigest()
        history = direct.load_actual_results(ts('2026-10-15T08:23:22Z'))['GLOBAL']
        inventory = dict(matches=len(history), metrics={m:sum(direct.metric_values(r,m) is not None for r in history) for m in METRICS},
                         sourceSha256=source, pid=os.getpid(), providerCalls=0,
                         priorWorkRecovered=True, publicationTimeVerified=False)
        write(output/'inventory.json', inventory)
        print('START', json.dumps(inventory), flush=True)
        folds = [('2026-07-01','2026-08-01','2026-09-01'),
                 ('2026-08-01','2026-09-01','2026-10-01'),
                 ('2026-09-01','2026-10-01','2026-10-15')]
        all_events = []
        for fitday, testday, endday in folds:
            checkpoint = output / (testday+'.json')
            if checkpoint.exists():
                saved=json.loads(checkpoint.read_text())
                if saved['sourceSha256'] != source:
                    raise ValueError('Checkpoint source mismatch')
                all_events.extend(saved['events']); continue
            began=time.time(); at=ts(fitday+'T00:00:00Z'); test=ts(testday+'T00:00:00Z'); end=ts(endday+'T00:00:00Z')
            print('FIT',fitday,flush=True)
            model=direct.MatchupLeague(history,at)
            calibration=forecasts(model,history,at,test)
            grouped=defaultdict(list)
            for r in calibration:
                grouped[(r['metric'],r['regime'])].append(r)
            params={k:fit_calibration(v) for k,v in grouped.items()}
            print('CALIBRATED',testday,'seconds',round(time.time()-began,1),flush=True)
            later=forecasts(model,history,test,end)
            result=[]
            for r in later:
                param=params.get((r['metric'],r['regime']),dict(scale=1.,alpha=0.))
                raw=events(r,1.,0.); calibrated=events(r,param['scale'],param['alpha'])
                for (market,p,y),(other,q,yy) in zip(raw,calibrated):
                    assert market==other and y==yy
                    result.append(dict(fixture=r['fixture'],market=market,metric=r['metric'],regime=r['regime'],p=p,q=q,y=y,fold=testday))
            saved=dict(sourceSha256=source,trainBefore=fitday,calibrationBefore=testday,testBefore=endday,
                       training=model.matches,parameters={'::'.join(k):v for k,v in params.items()},
                       seconds=round(time.time()-began,2),events=result)
            write(checkpoint,saved); all_events.extend(result)
            print('CHECKPOINT',checkpoint,'events',len(result),'seconds',saved['seconds'],flush=True)
        grouped=defaultdict(list)
        for r in all_events:
            grouped[(r['metric'],r['regime'],r['market'])].append(r)
        report={ '::'.join(k):summary(v) for k,v in grouped.items() }
        write(output/'comparison.json',dict(researchOnly=True,notUntouchedProspectiveValidation=True,
              model='V3 Poisson versus prior-window mean/negative-binomial calibration',byMarket=report,
              warning='Previously inspected periods; no live eligibility promotion from these scores.'))
        print('COMPLETE',output/'comparison.json',flush=True)
    finally:
        lock.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
