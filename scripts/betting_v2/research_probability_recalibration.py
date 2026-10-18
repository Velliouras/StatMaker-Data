"""Fixed chronological calibration trial and same-contract price attribution."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json, math, sys
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import materialize_betting_v2_direct as direct
from betting_v2.research_prior_count_prices import FrozenMonthlyModels, lower_pick
from betting_v2.reliable_counts import ReliableCounts, metric_values
from betting_v2.reliable_goals import ReliableGoals
from betting_v2.research_model_revision import read
from betting_v2.research_csv_prices import write, digest
from betting_v2.research_git_archive import absolute_time, COUNT_METRICS, settle_selected, pick_summary
from betting_v2.research_market_decisions import fixture_favourite
from betting_v2.research_quotes import assess
from betting_v2.exact_forecast_adapter import forecast
from betting_v2.research_scope_expansion import select_scope
from betting_v2.probability_recalibration import bank, probability, decision, canonical_event

ROOT = Path(__file__).resolve().parents[2]
PRIOR = ROOT/'reports/betting_v2/prior_market_calibration_20261018'
OCTOBER = ROOT/'reports/betting_v2/git_archive_expansion_20261016'
FOLDS = ('2026-08-01', '2026-09-01', '2026-10-01')
ARMS = ('base_point', 'base_conservative', 'matched_point', 'matched_conservative', 'calibrated_point', 'calibrated_conservative')


class MonthlyModels(FrozenMonthlyModels):
    def advance(self, cutoff):
        eligible = [d for d in FOLDS if absolute_time(d+'T00:00:00Z') <= cutoff]
        if not eligible or cutoff % 86400:
            raise ValueError('No frozen model before complete-day feature cutoff')
        day = max(eligible)
        if day != self.fold:
            source = ROOT/'reports/betting_v2/all_market_revision_20261016'
            self.counts = {m: ReliableCounts.restore(read(source/f'{m}_{day}_pretest_state.json.gz')) for m in COUNT_METRICS}
            self.old = {m: read(source/f'{m}_{day}_profiles.json') for m in COUNT_METRICS}
            goals = ROOT/'reports/betting_v2/model_revision_20261016'
            self.ft = ReliableGoals.restore(read(goals/f'recency_{day}_pretest_state.json.gz'))
            self.goal_profile = read(goals/f'recency_{day}_profile.json')
            if day == '2026-10-01':
                cal = read(ROOT/'reports/betting_v2/model_revision_calibration_20261016/recency_2026-10-01_selected_calibration.json')
                self.goal_profile['_selected_joint'] = cal['point']['theta']
            self.fold = day; self.clock = absolute_time(day+'T00:00:00Z')
            if any(m.clock != self.clock for m in [self.ft, *self.counts.values()]):
                raise ValueError('Frozen model clock mismatch')
        if cutoff < self.clock:
            raise ValueError('Feature clock moved backwards')
        for row in self.history:
            if self.clock <= row['_date'] < cutoff:
                self.ft.observe(row, direct.metric_values)
                for model in self.counts.values(): model.observe(row, metric_values)
        self.clock = cutoff
        for model in [self.ft, *self.counts.values()]: model.clock = cutoff
        self.cache = {}


def contract(q):
    return q['fixture'], str(q['providerEventId']), q['bookmaker'], tuple(q['spec'])


def score_probabilities(rows, field):
    if not rows: return dict(events=0, fixtures=0)
    counts = Counter(r['fixture'] for r in rows)
    weights = np.asarray([1/counts[r['fixture']] for r in rows]); weights /= weights.sum()
    p = np.clip([r[field] for r in rows], 1e-12, 1-1e-12); y = np.asarray([r['yOver'] for r in rows])
    return dict(events=len(rows), fixtures=len(counts),
        logLoss=float(weights @ (-y*np.log(p)-(1-y)*np.log1p(-p))),
        brier=float(weights @ ((p-y)**2)), meanProbability=float(weights @ p), eventRate=float(weights @ y))


def price_attribution(first, last, picks):
    # Do not optimize a new selection or silently substitute bookmaker/line.
    def index(rows):
        groups = defaultdict(list)
        for q in rows: groups[contract(q)].append(q)
        return {k:v[0] for k,v in groups.items() if len({r['odd'] for r in v}) == 1}
    left, right = index(first), index(last)
    common = set(left) & set(right)
    rows = []
    for p in picks:
        key = contract(p)
        if key not in common: continue
        a, b = left[key], right[key]
        rows.append(dict(fixture=p['fixture'], spec=p['spec'], bookmaker=p['bookmaker'],
            firstOdd=a['odd'], lastOdd=b['odd'], firstObserved=a['observed'], lastObserved=b['observed'],
            firstProfit=p['y']*a['odd']+p['actualPush']-1,
            lastProfit=p['y']*b['odd']+p['actualPush']-1,
            lastPriceInOriginalBand=1.8 < b['odd'] < 3))
    return dict(commonQuotedContracts=len(common), firstOnly=len(set(left)-set(right)), lastOnly=len(set(right)-set(left)),
        fixedEarliestPicks=len(picks), matchedFixedEarliestPicks=len(rows),
        firstProfitUnits=sum(r['firstProfit'] for r in rows), lastProfitUnits=sum(r['lastProfit'] for r in rows),
        meanOddChange=float(np.mean([r['lastOdd']-r['firstOdd'] for r in rows])) if rows else None,
        lastOutsideOriginalOddsBand=sum(not r['lastPriceInOriginalBand'] for r in rows), rows=rows,
        limitation='Price substitution on fixed earliest decisions only; later price availability is not execution evidence or a new executable policy.')


def main():
    p = argparse.ArgumentParser(); p.add_argument('--output', type=Path, required=True); args = p.parse_args()
    out = args.output
    binding = read(out/'input_bindings.json')
    changed = [s for s,h in binding['files'].items() if digest((ROOT/s).read_bytes()) != h]
    if changed: raise ValueError('Changed locked input: '+str(changed))
    signature = digest((out/'input_bindings.json').read_bytes())
    history = read(PRIOR/'history.json.gz'); truth = {f"{r['_date']}:{r['_home']}:{r['_away']}":r for r in history}
    profiles = {}
    for day in FOLDS:
        profiles[day] = {m:(read(PRIOR/f'{m}_{day}_coupled_profiles.json')['profiles'] if day != '2026-10-01'
            else read(ROOT/f'reports/betting_v2/combined_candidate_20261016/{m}_profiles.json')) for m in COUNT_METRICS}
    results = {}; documents = {}; quote_packets = {}
    for packet in ('earliest', 'latest'):
        all_quotes = read(PRIOR/f'quotes_{packet}.json.gz') + read(OCTOBER/f'quotes_{packet}.json.gz')
        favourites = fixture_favourite(all_quotes)
        quotes = [q for q in all_quotes if q['spec'][0] in COUNT_METRICS]
        quote_packets[packet] = quotes
        checkpoint = out/f'checkpoint_{packet}.json.gz'
        saved = read(checkpoint) if checkpoint.exists() else dict(signature=signature, done=[], options={}, frame=[], fits={}, reliability=[])
        if saved['signature'] != signature: raise ValueError('Changed replay checkpoint inputs')
        done = set(saved['done']); options = defaultdict(list, saved['options']); frame = saved['frame']; fits = saved['fits']; reliability = saved['reliability']
        models = MonthlyModels(history, profiles)
        for cutoff in sorted({q['featureCutoff'] for q in quotes}):
            if cutoff in done: continue
            models.advance(cutoff); start = absolute_time(models.fold+'T00:00:00Z')
            seen = set()
            for q in quotes:
                if q['featureCutoff'] != cutoff: continue
                metric, scope = q['spec'][:2]
                pred = models.prediction(q['_home'], q['_away'], metric, 'coupled')
                if pred is None: continue
                row, profile, prepared = pred['rows'][0], pred['profiles'][0], pred['prepared'][0]
                value = forecast(row, profile, q['spec'], prepared)
                supported = scope in ('TOTAL', 'TEAM')
                key = models.fold+'::'+metric+'::'+scope
                if supported and key not in fits: fits[key] = bank(frame, metric, scope, start)
                fit = fits.get(key); active = bool(fit and fit['point']['fitted'])
                fav = favourites.get((q['fixture'], q['bookmaker'], q['sourceReceipt']))
                pick = assess(q, (row, prepared, pred['full']), profile, fav, 'current_preference')
                if pick is not None:
                    options['base_point'].append(pick)
                    values = [value] + [forecast(r, p, q['spec'], prep) for r,p,prep in zip(pred['rows'][1:], pred['profiles'][1:], pred['prepared'][1:])]
                    lower = lower_pick(pick, values, fav)
                    if lower: options['base_conservative'].append(lower)
                    if active:
                        options['matched_point'].append(pick)
                        if lower: options['matched_conservative'].append(lower)
                        point = probability(q['spec'], value['winProbability'], value['pushProbability'], fit['point']['theta'])
                        adjusted = decision(pick, point)
                        if adjusted: options['calibrated_point'].append(adjusted)
                        if lower:
                            replicas = [probability(q['spec'], v['winProbability'], v['pushProbability'], f['theta']) for v,f in zip(values[1:], fit['replicas'])]
                            adjusted = decision(lower, min(point, float(np.quantile(replicas, .1))))
                            if adjusted: options['calibrated_conservative'].append(adjusted)
                # Current labels never enter this month's already frozen fit.
                actual = direct.metric_values(truth[q['fixture']], metric) if q['fixture'] in truth else None
                if supported and actual is not None:
                    from betting_v2.research_app_markets import outcome_extended
                    y, push = outcome_extended(*actual, q['spec'])
                    if not push and value['pushProbability'] < 1:
                        event = canonical_event(q['spec'], value['winProbability'], value['pushProbability'], y)
                        event_key = (q['fixture'], metric, scope, q['spec'][3], q['spec'][4], q['bookmaker'], q['sourceReceipt'])
                        if event_key not in seen:
                            seen.add(event_key)
                            record = dict(metric=metric, scope=scope, fixture=q['fixture'], kickoff=q['kickoff'], pOver=event[0], yOver=event[1])
                            frame.append(record)
                            if active:
                                over = (metric, scope, 'OVER', q['spec'][3], q['spec'][4])
                                pcal = probability(over, event[0], 0., fit['point']['theta'])
                                reliability.append(dict(record, calibrated=pcal, month=models.fold))
            done.add(cutoff)
            write(checkpoint, dict(signature=signature, done=sorted(done), options=dict(options), frame=frame, fits=fits, reliability=reliability), zipped=True)
            write(out/'progress.json', dict(packet=packet, completedDays=len(done), status='RUNNING', providerCalls=0, llmCalls=0))
            print('RECALIBRATION_DAY', packet, cutoff, {k:len(v) for k,v in options.items()}, flush=True)
        reports = {}
        for arm in ARMS:
            selected = select_scope(options[arm]); scored, missing = settle_selected(selected, truth)
            months = sorted({datetime.fromtimestamp(r['kickoff'], timezone.utc).strftime('%Y-%m') for r in scored})
            reports[arm] = dict(overall=pick_summary(scored), selectedBeforeSettlement=len(selected), missingActual=missing,
                byMonth={m:pick_summary([r for r in scored if datetime.fromtimestamp(r['kickoff'], timezone.utc).strftime('%Y-%m') == m]) for m in months},
                byMetric={m:pick_summary([r for r in scored if r['spec'][0] == m]) for m in COUNT_METRICS}, picks=scored)
        reproduced = {}
        for suffix in ('point', 'conservative'):
            original = [r for source in (PRIOR, OCTOBER) for r in read(source/f'priced_{packet}.json')['reports']['coupled_'+suffix]['picks']]
            expected = {contract(r):r for r in original}
            actual = {contract(r):r for r in reports['base_'+suffix]['picks']}
            if set(expected) != set(actual):
                raise ValueError('Base selection changed '+packet+' '+suffix+': '+str(len(set(expected)^set(actual))))
            deviation = max((abs(actual[k][v]-expected[k][v]) for k in expected for v in ('p', 'push', 'confidence')), default=0.)
            if deviation > 1e-10: raise ValueError('Base decision probability changed '+str(deviation))
            reproduced[suffix] = dict(bets=len(actual), maxProbabilityDeviation=deviation)
        proper_scores = {}
        for month in FOLDS[1:]:
            rows = [r for r in reliability if r['month'] == month]
            proper_scores[month] = dict(raw=score_probabilities(rows, 'pOver'), calibrated=score_probabilities(rows, 'calibrated'))
        doc = dict(reports=reports, fits=fits, properScores=proper_scores, baseReproduction=reproduced, providerCalls=0, llmCalls=0,
            untouchedHoldout=False, thresholdsChanged=False, roiTuning=False, deployed=False)
        write(out/f'priced_{packet}.json', doc); documents[packet] = doc
        results[packet] = {k:{a:b for a,b in v.items() if a != 'picks'} for k,v in reports.items()}
        write(out/f'calibration_bank_{packet}.json', dict(fits=fits, deployed=False, signature=signature))
        print('RECALIBRATION_RESULT', packet, {k:v['overall'] for k,v in reports.items()}, flush=True)
    attribution = {}
    for arm in ('base_point', 'base_conservative', 'calibrated_point', 'calibrated_conservative'):
        attribution[arm] = price_attribution(quote_packets['earliest'], quote_packets['latest'], documents['earliest']['reports'][arm]['picks'])
    write(out/'same_contract_prices.json', attribution)
    write(out/'summary.json', dict(status='COMPLETE', results=results,
        properScores={p:d['properScores'] for p,d in documents.items()},
        priceAttribution={k:{a:b for a,b in v.items() if a != 'rows'} for k,v in attribution.items()},
        inputSignature=signature, providerCalls=0, llmCalls=0, deployed=False, untouchedHoldout=False))
    lines = ['# Νέα βαθμονόμηση πιθανοτήτων — 18/10/2026', '',
        'Αναπτυξιακή χρονολογική δοκιμή. Εκπαίδευση μόνο σε προηγούμενους μήνες· αμετάβλητα όρια. Πρώτες και τελευταίες τιμές δεν αθροίζονται.', '',
        '| Πολιτική | Πρώτες τιμές: επιλογές / ROI | Τελευταίες τιμές: επιλογές / ROI |', '|---|---:|---:|']
    for arm in ARMS:
        cells = []
        for packet in ('earliest', 'latest'):
            s=results[packet][arm]['overall']; cells.append(str(s['bets'])+' / '+(f"{s['roi']:+.2%}" if 'roi' in s else '—'))
        lines.append('| '+arm+' | '+' | '.join(cells)+' |')
    lines += ['', 'Τα base περιλαμβάνουν και τον Αύγουστο. Τα matched/calibrated μόνο τα γεγονότα με διαθέσιμη προγενέστερη βαθμονόμηση, από Σεπτέμβριο. Η σύγκριση της επίδρασης της διόρθωσης γίνεται με τα matched.', '',
        'Η πλήρης νέα πιθανότητα αξιολογείται με proper scores σε όλα τα κοινά μη-push γεγονότα, όχι μόνο στις επιλεγμένες προτάσεις. Η απόφαση επιτρέπει μόνο μείωση από την αρχική στατιστική πιθανότητα. Τα σενάρια φαβορί παραμένουν ξεχωριστοί αρχικοί περιορισμοί και δεν περιγράφονται ως εκ νέου βαθμονομημένα.', '',
        'Τα calibration_bank αρχεία είναι επαναχρησιμοποιήσιμα ερευνητικά παραμετρικά σύνολα. Δεν άλλαξαν ενεργό UAT, PROD ή προηγούμενα παγωμένα στιγμιότυπα.', '',
        '[Πρωτόκολλο](PROTOCOL.md) · [Πλήρη αποτελέσματα](summary.json) · [Ίδιες γραμμές/ίδιοι bookmakers](same_contract_prices.json)']
    (out/'RESULTS_GR.md').write_text('\n'.join(lines)+'\n')


if __name__ == '__main__': main()
