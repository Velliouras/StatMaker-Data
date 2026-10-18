"""Prior-month count-price calibration with frozen monthly rate models.

August and September are development periods. No current prospective snapshots,
app parameters, betting thresholds or rate coefficients are changed.
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import fcntl, json, tempfile, sys
from pathlib import Path
import numpy as np

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import materialize_betting_v2_direct as direct
from betting_v2.research_git_archive import COUNT_METRICS, absolute_time, collect, settle_selected, pick_summary
from betting_v2.research_model_revision import read
from betting_v2.research_csv_prices import write, digest
from betting_v2.reliable_counts import ReliableCounts, metric_values
from betting_v2.reliable_goals import ReliableGoals
from betting_v2.paired_count import fit_profile
from betting_v2.research_app_markets import prepare_extended, outcome_extended
from betting_v2.exact_forecast_adapter import forecast
from betting_v2.validated_walk_forward import distribution
from betting_v2.research_market_decisions import fixture_favourite
from betting_v2.market_calibration import references, fit_weight, pooled_probability
from betting_v2.research_quotes import assess
from betting_v2.research_scope_expansion import select_scope

ROOT = Path(__file__).resolve().parents[2]
FOLDS = ('2026-08-01', '2026-09-01')
ARMS = ('control_point', 'control_conservative', 'coupled_point', 'coupled_conservative',
        'matched_reference_point', 'matched_reference_conservative', 'prior_cap_point', 'prior_cap_conservative')


def fold_before(cutoff):
    eligible = [day for day in FOLDS if absolute_time(day + 'T00:00:00Z') <= cutoff]
    if not eligible: raise ValueError('Quote features precede every frozen monthly model')
    return max(eligible)


def prior_records(frame, metric, cutoff):
    # A whole day after kickoff is reserved for the count label to arrive.
    # Historical publication times are not independently verified.
    return [r for r in frame if r['metric'] == metric and r['kickoff'] + 86400 <= cutoff]


def price_bank(frame, metric, cutoff):
    rows = prior_records(frame, metric, cutoff)
    point = fit_weight(rows, cutoff)
    weeks = sorted({int(r['kickoff'] // 604800) for r in rows})
    rng = np.random.default_rng(18102026)
    replicas = [fit_weight(rows, cutoff, dict(zip(weeks, rng.exponential(size=len(weeks)).tolist()))) for _ in range(12)]
    return dict(point=point, replicas=replicas, labelLagSeconds=86400,
        independentReferenceEvents=sum(bool(r.get('referenceIndependent')) for r in rows),
        sameBookBenchmarkEvents=sum(not r.get('referenceIndependent',False) for r in rows),
        uncertainty='Independent weekly price-weight sensitivity; not a calibrated risk guarantee')


def capped_probability(win, push, reference, weight):
    return min(win, pooled_probability(win, push, reference, weight))


def verify_inputs(out):
    binding = read(out / 'input_bindings.json')
    changed = [path for path, sha in binding['files'].items() if digest((ROOT / path).read_bytes()) != sha]
    if digest((out / 'history.json.gz').read_bytes()) != binding['historySha256']: changed.append('history.json.gz')
    if changed: raise ValueError('Research input bindings changed: ' + str(changed))
    return binding


def calibrate_laws(out):
    banks = {}
    source = ROOT / 'reports/betting_v2/all_market_revision_20261016'
    for day in FOLDS:
        banks[day] = {}
        for metric in COUNT_METRICS:
            path = source / f'{metric}_{day}_calibration.json.gz'
            rows = read(path); cutoff = absolute_time(day + 'T00:00:00Z')
            if any(r['date'] >= cutoff for r in rows): raise ValueError('Count-law calibration crosses model start')
            target = out / f'{metric}_{day}_coupled_profiles.json'
            source_sha = digest(path.read_bytes())
            if target.exists():
                stored = read(target)
                if stored['calibrationSha256'] != source_sha: raise ValueError('Changed cached count calibration')
                values = stored['profiles']
            else:
                values = [fit_profile(rows)]
                for i in range(12):
                    values.append(fit_profile([dict(r, means=r['bootstrapMeans'][i]) for r in rows]))
                write(target, dict(calibrationSha256=source_sha, trainingBefore=cutoff, profiles=values))
            banks[day][metric] = values
            print('PRIOR_COUNT_PROFILE', day, metric, len(rows), flush=True)
    return banks


class FrozenMonthlyModels:
    def __init__(self, history, profiles):
        self.history = sorted(history, key=lambda r: r['_date']); self.profiles = profiles
        self.fold = None; self.clock = None; self.cache = {}

    def advance(self, cutoff):
        if cutoff % 86400 != 0: raise ValueError('Feature clock requires a complete UTC day')
        day = fold_before(cutoff)
        if day != self.fold:
            source = ROOT / 'reports/betting_v2/all_market_revision_20261016'
            self.counts = {m: ReliableCounts.restore(read(source / f'{m}_{day}_pretest_state.json.gz')) for m in COUNT_METRICS}
            self.old = {m: read(source / f'{m}_{day}_profiles.json') for m in COUNT_METRICS}
            goal = ROOT / 'reports/betting_v2/model_revision_20261016'
            self.ft = ReliableGoals.restore(read(goal / f'recency_{day}_pretest_state.json.gz'))
            self.goal_profile = read(goal / f'recency_{day}_profile.json')
            self.fold = day; self.clock = absolute_time(day + 'T00:00:00Z')
            if any(m.clock != self.clock for m in [self.ft, *self.counts.values()]):
                raise ValueError('Monthly state clock differs from declared model start')
        if cutoff < self.clock: raise ValueError('Research feature clock moved backwards')
        for row in self.history:
            if self.clock <= row['_date'] < cutoff:
                self.ft.observe(row, direct.metric_values)
                for model in self.counts.values(): model.observe(row, metric_values)
        self.clock = cutoff
        for model in [self.ft, *self.counts.values()]: model.clock = cutoff
        self.cache = {}

    def prediction(self, home, away, metric, name):
        key = home, away, metric, name
        if key not in self.cache:
            core = self.counts[metric]; rates = core.predict_bank(home, away)
            full = self.ft.predict(home, away, 'GOALS_FT')
            if rates is None or full is None: self.cache[key] = None
            else:
                profiles = self.old[metric] if name == 'control' else self.profiles[self.fold][metric]
                if len(rates) != len(profiles) or len(rates) != 13: raise ValueError('Incomplete prior parameter bank')
                regime = 'XG' if min(len(core.xg[home]), len(core.xg[away])) >= 8 else 'ELO_NO_XG'
                rows = [dict(metric=metric, regime=regime, means=means.tolist()) for means in rates]
                prepared = [prepare_extended(row, p) for row, p in zip(rows, profiles)]
                full_regime = 'XG' if min(len(self.ft.xg[home]), len(self.ft.xg[away])) >= 8 else 'ELO_NO_XG'
                full_distribution = distribution(dict(metric='GOALS_FT', regime=full_regime, means=list(full[:2])), self.goal_profile)
                self.cache[key] = dict(rows=rows, profiles=profiles, prepared=prepared, full=full_distribution)
        return self.cache[key]


def lower_pick(pick, values, fav):
    point, *replicas = values
    def low(key):
        return min(float(key(point)), float(np.quantile([key(v) for v in replicas], .1)))
    p = low(lambda r: r['winProbability']); push = low(lambda r: r['pushProbability'])
    support = low(lambda r: r['scenarios'][fav]['winProbability'] or 0.) if fav else None
    ev = p * pick['odd'] + push - 1
    if p < .6 or ev <= 0: return None
    if pick['spec'][0] in ('CORNERS', 'SHOTS', 'SOT') and support is not None and support < .6: return None
    confidence = min(p, support) if support is not None else p
    return dict(pick, p=p, push=push, ev=ev, confidence=confidence,
        tier='HIGH' if confidence >= .8 else 'CONFIRMED' if confidence >= .7 else 'DEVELOPING')


def cap_pick(pick, p):
    ev = p * pick['odd'] + pick['push'] - 1
    if p < .6 or ev <= 0: return None
    confidence = min(pick['confidence'], p)
    return dict(pick, p=p, ev=ev, confidence=confidence,
        tier='HIGH' if confidence >= .8 else 'CONFIRMED' if confidence >= .7 else 'DEVELOPING')


def replay(out, history, laws):
    binding = verify_inputs(out); truth = {f"{r['_date']}:{r['_home']}:{r['_away']}": r for r in history}
    results = {}
    for packet in ('latest', 'earliest'):
        quotes_path = out / f'quotes_{packet}.json.gz'; all_quotes = read(quotes_path)
        refs, reference_audit = references(all_quotes); favourites = fixture_favourite(all_quotes)
        quotes = [dict(q, reference=refs.get(i)) for i, q in enumerate(all_quotes) if q['spec'][0] in COUNT_METRICS]
        signature = digest((digest(quotes_path.read_bytes()) + digest(Path(__file__).read_bytes()) +
            digest((out / 'input_bindings.json').read_bytes()) + digest((out / 'manifest.json').read_bytes())).encode())
        checkpoint = out / f'checkpoint_{packet}.json.gz'
        saved = read(checkpoint) if checkpoint.exists() else dict(signature=signature, options={}, frame=[], fits={}, done=[])
        if saved['signature'] != signature: raise ValueError('Prior replay checkpoint input mismatch')
        options = defaultdict(list, saved['options']); frame = saved['frame']; fits = saved['fits']; done = set(saved['done'])
        models = FrozenMonthlyModels(history, laws); diagnostics = Counter(saved.get('diagnostics', {}))
        cutoffs = sorted({q['featureCutoff'] for q in quotes})
        for cutoff in cutoffs:
            if cutoff in done: continue
            models.advance(cutoff)
            month_start = absolute_time(models.fold + 'T00:00:00Z')
            for q in quotes:
                if q['featureCutoff'] != cutoff: continue
                metric = q['spec'][0]; h, a = q['_home'], q['_away']
                fav = favourites.get((q['fixture'], q['bookmaker'], q['sourceReceipt']))
                reference = q['reference']; fit_key = models.fold + '::' + metric
                if fit_key not in fits: fits[fit_key] = price_bank(frame, metric, month_start)
                fit = fits[fit_key]; current = None
                for name in ('control', 'coupled'):
                    pred = models.prediction(h, a, metric, name)
                    if pred is None:
                        diagnostics[name + '::missingPrediction::' + metric] += 1; continue
                    row, profile, prepared = pred['rows'][0], pred['profiles'][0], pred['prepared'][0]
                    value = forecast(row, profile, q['spec'], prepared)
                    pick = assess(q, (row, prepared, pred['full']), profile, fav, 'current_preference')
                    if pick is not None:
                        options[name + '_point'].append(pick)
                        values = [value] + [forecast(r, p, q['spec'], prep) for r, p, prep in
                            zip(pred['rows'][1:], pred['profiles'][1:], pred['prepared'][1:])]
                        conservative = lower_pick(pick, values, fav)
                        if conservative: options[name + '_conservative'].append(conservative)
                        if name == 'coupled' and reference and fit['point']['fitted']:
                            options['matched_reference_point'].append(pick)
                            if conservative: options['matched_reference_conservative'].append(conservative)
                            cap = capped_probability(value['winProbability'], value['pushProbability'], reference['q'], fit['point']['weight'])
                            capped = cap_pick(pick, cap)
                            if capped: options['prior_cap_point'].append(capped)
                            if conservative:
                                replica_caps = [capped_probability(v['winProbability'], v['pushProbability'], reference['q'], f['weight'])
                                    for v, f in zip(values[1:], fit['replicas'])]
                                lower = min(conservative['p'], cap, float(np.quantile(replica_caps, .1)))
                                capped = cap_pick(conservative, lower)
                                if capped: options['prior_cap_conservative'].append(capped)
                    if name == 'coupled': current = value
                # Labels are read only after this quote's fixed policies have selected.
                actual = direct.metric_values(truth[q['fixture']], metric) if q['fixture'] in truth else None
                if current is not None and reference and actual is not None:
                    y, push = outcome_extended(*actual, q['spec'])
                    if not push and current['pushProbability'] < 1:
                        frame.append(dict(metric=metric, fixture=q['fixture'], kickoff=q['kickoff'],
                            statConditional=current['winProbability'] / (1 - current['pushProbability']),
                            marketConditional=reference['q'], y=y, referenceIndependent=reference['independent']))
            done.add(cutoff)
            write(checkpoint, dict(signature=signature, options=dict(options), frame=frame, fits=fits,
                done=sorted(done), diagnostics=dict(diagnostics)), zipped=True)
            write(out / 'progress.json', dict(packet=packet, stage='REPLAY', done=len(done), days=len(cutoffs),
                providerCalls=0, llmCalls=0))
            print('PRIOR_COUNT_DAY', packet, cutoff, {k: len(v) for k, v in options.items()}, flush=True)
        reports = {}
        for arm in ARMS:
            selected = select_scope(options[arm]); scored, missing = settle_selected(selected, truth)
            months = sorted({datetime.fromtimestamp(r['kickoff'], timezone.utc).strftime('%Y-%m') for r in scored})
            reports[arm] = dict(overall=pick_summary(scored), selectedBeforeSettlement=len(selected), missingActual=missing,
                byMonth={month: pick_summary([r for r in scored if datetime.fromtimestamp(r['kickoff'], timezone.utc).strftime('%Y-%m') == month]) for month in months},
                byMetric={m: pick_summary([r for r in scored if r['spec'][0] == m]) for m in COUNT_METRICS}, picks=scored)
        write(out / f'priced_{packet}.json', dict(reports=reports, referenceAudit=reference_audit, diagnostics=dict(diagnostics),
            priceFits=fits, providerCalls=0, llmCalls=0, newRateFits=0, roiTuning=False, untouchedHoldout=False))
        results[packet] = {arm: {k: v for k, v in report.items() if k != 'picks'} for arm, report in reports.items()}
        next_fit = {m: price_bank(frame, m, absolute_time('2026-10-01T00:00:00Z')) for m in COUNT_METRICS}
        write(out / f'prior_before_october_{packet}.json', dict(fits=next_fit, deployed=False,
            rule='Prior outcomes with at least 24h lag; no ROI fitting; not applied to existing frozen forecasts'))
        print('PRIOR_COUNT_RESULT', packet, {k: v['overall'] for k, v in reports.items()}, flush=True)
    write(out / 'summary.json', dict(status='COMPLETE', results=results, providerCalls=0, llmCalls=0,
        newRateFits=0, distributionCalibrationFits=104, monthlyRateModels=list(FOLDS),
        inputBindingsVerified=True, appChanges=0, prospectiveSnapshotsChanged=False, roiTuning=False,
        untouchedHoldout=False, allArms=list(ARMS), historicalPublicationOfResultsVerified=False))
    render_report(out, results)
    write(out / 'progress.json', dict(status='COMPLETE', providerCalls=0, llmCalls=0))


def render_report(out, results):
    def cell(s): return f"{s['bets']} / {s['roi']:+.2%}" if s.get('roi') is not None else f"{s['bets']} / —"
    lines = ['# Προηγούμενες τιμές και βαθμονόμηση count αγορών', '',
        'Αύγουστος–Σεπτέμβριος 2026. Κάθε πρόβλεψη χρησιμοποιεί μηνιαίο μοντέλο εκπαιδευμένο πριν από τον χρόνο των χαρακτηριστικών της. '
        'Οι μέσοι ρυθμοί παραμένουν παγωμένοι. Η νέα διόρθωση πιθανοτήτων μαθαίνεται με προηγούμενο log loss, με καθυστέρηση τουλάχιστον 24 ωρών για τα αποτελέσματα.', '',
        '| Πολιτική | Τελευταίες τιμές: επιλογές / ROI | Πρώτες τιμές: επιλογές / ROI |', '|---|---:|---:|']
    for arm in ARMS: lines.append(f"| {arm} | {cell(results['latest'][arm]['overall'])} | {cell(results['earliest'][arm]['overall'])} |")
    lines += ['', 'Οι matched_reference και prior_cap πολιτικές χρησιμοποιούν την ίδια επιλέξιμη βάση με πλήρεις αποδόσεις και τουλάχιστον 30 προηγούμενους αγώνες βαθμονόμησης ανά metric. '
        'Η πρώτη περίοδος χωρίς προηγούμενες τιμές δεν ενεργοποιεί τη νέα διόρθωση. Οι άλλες πολιτικές διατηρούν όλες τις διαθέσιμες αγορές.', '',
        'Όπου δεν υπάρχει άλλος bookmaker, η ίδια πηγή τιμών σημειώνεται ως benchmark και δεν θεωρείται ανεξάρτητη επιβεβαίωση. '
        'Οι παραλλαγές Bet365 no-latency δεν λογίζονται διαφορετικός bookmaker.', '',
        'Τα δύο πακέτα τιμών επικαλύπτονται. Πρόκειται για ιστορικά δεδομένα ανάπτυξης, όχι ανεξάρτητη απόδειξη κερδοφορίας. '
        'Δεν μεταβλήθηκαν κανόνες ή αγορές βάσει ROI, ούτε οι ενεργές μελλοντικές προβλέψεις. Οι τιμές είναι αρχειοθετημένες παρατηρήσεις και όχι αποδείξεις εκτέλεσης.', '',
        'Πλήρη στοιχεία ανά μήνα και αγορά, κέρδος, αβεβαιότητα ανά αγώνα, προηγούμενα βάρη βαθμονόμησης και αβαθμολόγητες επιλογές υπάρχουν στα JSON. '
        'Η αβεβαιότητα είναι έλεγχος ευαισθησίας παραμέτρων και δεν εγγυάται σωστά όρια πιθανότητας. Οι μελλοντικές τιμές πρέπει να αξιολογηθούν χωριστά.']
    (out / 'RESULTS_GR.md').write_text('\n'.join(lines) + '\n')


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--stage', choices=('collect', 'replay'), default='replay')
    args = parser.parse_args(); out = args.output; out.mkdir(parents=True, exist_ok=True)
    lock = Path(tempfile.gettempdir()) / ('statmaker-prior-count-' + digest(str(out.resolve()).encode())[:16] + '.lock')
    with lock.open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        verify_inputs(out); history = read(out / 'history.json.gz')
        if args.stage == 'collect':
            direct.load_actual_results(absolute_time('2026-10-16T00:00:00Z'))  # Approved alias map only.
            collect(out, Path('/tmp/statmaker-prior-git-price-cache'), history)
        else:
            replay(out, history, calibrate_laws(out))


if __name__ == '__main__': main()
