"""Recover exact historical provider payloads from fixed Git snapshots.

No provider API calls, invented prices, ROI tuning or live model changes.
Git commit time is a conservative archive observation, not an execution fill.
"""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import gzip, hashlib, json, math, sys, time, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import materialize_betting_v2_direct as direct
from betting_v2.index_quote_receipts import _price, PRICE_FIELDS, DIRECTION_FIELDS
from betting_v2.research_scope_expansion import expanded_spec, select_scope
from betting_v2.research_quotes import feature_cutoff, one_fixture_receipt, assess, stats
from betting_v2.research_model_revision import read
from betting_v2.research_csv_prices import write, digest
from betting_v2.research_market_decisions import fixture_favourite
from betting_v2.live_revised import load_revised
from betting_v2.research_app_markets import prepare_extended, outcome_extended
from betting_v2.validated_walk_forward import distribution
from betting_v2.exact_forecast_adapter import forecast

ROOT = Path(__file__).resolve().parents[2]
COUNT_METRICS = ('CORNERS', 'SHOTS', 'SOT', 'FOULS')


def absolute_time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def extract_snapshot(doc, source, max_age=86400):
    observed = absolute_time(source['committedAtUTC'])
    generated = absolute_time(doc.get('generatedAt'))
    if observed is None or generated is None or generated > observed:
        raise ValueError('Archive generation must precede its absolute commit time')
    offers = []
    rejected = Counter()
    for league in doc.get('leagues', []):
        for match in league.get('matches', []):
            kickoff = absolute_time(match.get('kickoff'))
            if not kickoff or not match.get('id') or match.get('teamMappingStatus') != 'matched':
                rejected['unapprovedIdentity'] += 1
                continue
            if observed >= kickoff:
                rejected['archivedAtOrAfterKickoff'] += 1
                continue
            event = dict(home=match.get('providerHomeTeam') or match.get('homeTeam'),
                         away=match.get('providerAwayTeam') or match.get('awayTeam'))
            for entry in match.get('providerMarkets', []):
                market = entry.get('market') or {}
                updated = absolute_time(market.get('updatedAt'))
                if entry.get('exactProviderPayload') is not True or not entry.get('bookmaker'):
                    rejected['notExactPayload'] += 1
                    continue
                if updated is None or updated > observed or observed - updated > max_age:
                    rejected['missingFutureOrStaleMarketTimestamp'] += 1
                    continue
                name = market.get('name') or entry.get('providerMarket')
                if not name:
                    rejected['missingMarketName'] += 1
                    continue
                for row in market.get('odds') or []:
                    if not isinstance(row, dict):
                        rejected['nonObjectOutcome'] += 1
                        continue
                    line = row.get('hdp') if row.get('hdp') is not None else row.get('line')
                    label = str(row.get('name') or row.get('label') or row.get('selection') or '').strip()
                    prices = []
                    for field in PRICE_FIELDS:
                        price = _price(row.get(field))
                        if price is not None:
                            if label: prices.append((label, price))
                            break
                    for field in DIRECTION_FIELDS:
                        price = _price(row.get(field))
                        if price is not None: prices.append((field.upper(), price))
                    for selection, price in prices:
                        offer = dict(providerMarketName=name, selection=selection, line=line)
                        spec = expanded_spec(offer, event)
                        if spec is None or 'CARD' in spec[0]:
                            rejected['unsupportedContractIncludingCards'] += 1
                            continue
                        offers.append(dict(spec=spec, odd=price, bookmaker=entry['bookmaker'],
                            observed=observed, kickoff=kickoff, featureCutoff=feature_cutoff(observed),
                            providerEventId=str(match['id']), sourceMarket=name,
                            sourceReceipt='git:' + source['sha'], sourceCommit=source['sha'],
                            sourceSha256=source['sha256'], marketUpdatedAtUTC=market['updatedAt'],
                            leagueCode=league.get('leagueCode'), canonicalHome=match.get('homeTeam'),
                            canonicalAway=match.get('awayTeam'), exactApiFootballFixtureMatchVerified=False,
                            executionEvidence=False))
    return offers, rejected


def remove_conflicts(quotes):
    unique = {}
    for quote in quotes:
        key = (quote['providerEventId'], quote['kickoff'], quote['sourceReceipt'],
               quote['bookmaker'], tuple(quote['spec']))
        previous = unique.get(key)
        if previous and (previous.get('ambiguous') or previous['odd'] != quote['odd']):
            quote = dict(quote, ambiguous=True)
        unique[key] = quote
    return [r for r in unique.values() if not r.get('ambiguous')], sum(r.get('ambiguous', False) for r in unique.values())


def exact_join(quotes, history, period):
    lookup = defaultdict(list)
    for row in history:
        lookup[int(row['_date']), row['_home'], row['_away']].append(row)
    start, end, feature_start = (absolute_time(period[k]) for k in
        ('kickoffFromUTC', 'kickoffBeforeUTC', 'featureFromUTC'))
    counters = Counter(); joined = []
    for q in quotes:
        if not start <= q['kickoff'] < end:
            counters['outsideEvaluationPeriod'] += 1; continue
        if q['featureCutoff'] < feature_start:
            counters['beforeFrozenModelState'] += 1; continue
        aliases = direct.UEFA_ALIASES.get(direct.league_code(q['leagueCode']), {})
        h = aliases.get(direct.name(q['canonicalHome']), direct.name(q['canonicalHome']))
        a = aliases.get(direct.name(q['canonicalAway']), direct.name(q['canonicalAway']))
        rows = lookup[int(q['kickoff']), h, a]
        if len(rows) != 1:
            counters['noUniqueExactCanonicalPairAndKickoff'] += 1; continue
        row = rows[0]
        joined.append(dict(q, fixture=f"{row['_date']}:{h}:{a}", _home=h, _away=a))
    return joined, counters


def load_snapshot(source, manifest, cache):
    target = cache / (source['sha'] + '.json')
    if not target.exists():
        url = f"https://raw.githubusercontent.com/{manifest['repository']}/{source['sha']}/{manifest['sourcePath']}"
        for attempt in range(3):
            try:
                with urllib.request.urlopen(url, timeout=55) as response:
                    raw = response.read(150_000_001)
                if len(raw) > 150_000_000: raise ValueError('Oversized archive payload')
                json.loads(raw)
                target.write_bytes(raw)
                break
            except (OSError, ValueError):
                if attempt == 2: raise
                time.sleep(1 + attempt)
    raw = target.read_bytes()
    binding = dict(source, sha256=digest(raw), bytes=len(raw),
        gitBlobSha1=hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest())
    return binding, json.loads(raw)


def collect(out, cache, history):
    manifest = read(out / 'manifest.json'); cache.mkdir(parents=True, exist_ok=True)
    counters = Counter(); quotes = []; bindings = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        for binding, doc in pool.map(lambda source: load_snapshot(source, manifest, cache), manifest['snapshots']):
            rows, rejected = extract_snapshot(doc, binding, manifest['maxMarketAgeSeconds'])
            bindings.append(binding); counters.update(rejected); quotes.extend(rows)
            print('ARCHIVE_SNAPSHOT', binding['committedAtUTC'], binding['bytes'], len(rows), flush=True)
    quotes, conflicts = remove_conflicts(quotes); counters['conflictingQuoteKeys'] = conflicts
    joined, rejected = exact_join(quotes, history, manifest['period']); counters.update(rejected)
    for packet, latest in [('earliest', False), ('latest', True)]:
        rows = one_fixture_receipt(joined, latest=latest)
        write(out / ('quotes_' + packet + '.json.gz'), rows, zipped=True)
    write(out / 'source_bindings.json', bindings)
    metrics = sorted({q['spec'][0] for q in joined})
    summary = dict(snapshots=len(bindings), downloadedBytes=sum(b['bytes'] for b in bindings),
        historyFixtures=len(history), historySha256=digest(json.dumps(history, sort_keys=True, separators=(',', ':')).encode()),
        mappedQuotesBeforeJoins=len(quotes), joinedQuotesBeforePacketChoice=len(joined),
        fixtures=len({q['fixture'] for q in joined}), counters=dict(counters),
        byMetric={m:dict(quotes=sum(q['spec'][0] == m for q in joined),
            fixtures=len({q['fixture'] for q in joined if q['spec'][0] == m})) for m in metrics},
        providerCalls=0, llmCalls=0, newMeanFits=0, appChanges=0,
        historicalCommitDatesAreExecutionProof=False, untouchedHoldout=False,
        identityRule='Existing approved canonical names plus unique exact kickoff; numeric cross-provider fixture ID not independently verified')
    write(out / 'coverage.json', summary)
    return summary


def settle_selected(picks, truth):
    scored = []; missing = Counter()
    for row in picks:
        raw = truth.get(row['fixture'])
        actual = direct.metric_values(raw, row['spec'][0]) if raw else None
        if actual is None:
            missing[row['spec'][0]] += 1; continue
        y, push = outcome_extended(*actual, row['spec'])
        scored.append(dict(row, y=y, actualPush=push))
    return scored, dict(missing)


def replay(out, history):
    source = ROOT / 'reports/betting_v2/combined_candidate_20261016'
    bound = read(source / 'source_binding.json')
    changed = [p for p, sha in bound['files'].items() if digest((ROOT / p).read_bytes()) != sha]
    if changed: raise ValueError('Frozen candidate inputs changed: ' + str(changed))
    banks = {p.name.split('_profiles')[0]: read(p) for p in source.glob('*_profiles.json')}
    truth = {f"{r['_date']}:{r['_home']}:{r['_away']}": r for r in history}
    results = {}
    for packet in ('earliest', 'latest'):
        quotes = read(out / ('quotes_' + packet + '.json.gz'))
        favourites = fixture_favourite(quotes)
        count_quotes = [q for q in quotes if q['spec'][0] in COUNT_METRICS]
        options = defaultdict(list); diagnostics = Counter()
        for cutoff in sorted({q['featureCutoff'] for q in count_quotes}):
            control, old, _ = load_revised(ROOT, history, cutoff)
            import copy
            candidate = copy.copy(control); candidate.cache = {}; candidate.uncertainty_cache = {}
            candidate.count_profiles = banks; profile = dict(old)
            for values in banks.values(): profile.update(values[0])
            candidate.revision_profile = profile; cache = {}
            for q in count_quotes:
                if q['featureCutoff'] != cutoff: continue
                h, a = q['_home'], q['_away']; metric = q['spec'][0]
                fav = favourites.get((q['fixture'], q['bookmaker'], q['sourceReceipt']))
                for name, model, parameters in [('control', control, old), ('coupled', candidate, profile)]:
                    key = name, h, a, metric
                    if key not in cache:
                        rates = model.predict(h, a, metric); full = model.predict(h, a, 'GOALS_FT')
                        if rates is None or full is None: cache[key] = None
                        else:
                            regime = 'XG' if min(len(model.xg[h]), len(model.xg[a])) >= 8 else 'ELO_NO_XG'
                            target = dict(metric=metric, regime=regime, means=list(rates[:2]))
                            cache[key] = (target, prepare_extended(target, parameters),
                                distribution(dict(metric='GOALS_FT', regime=regime, means=list(full[:2])), old))
                    pred = cache[key]
                    if pred is None:
                        diagnostics[name + '::missingPrediction::' + metric] += 1; continue
                    pick = assess(q, pred, parameters, fav, 'current_preference')
                    if pick is None: continue
                    options[name + '_point'].append(pick)
                    lower = model.bounds(h, a, q['spec'], q['odd'])
                    if lower is None: continue
                    p = min(pick['p'], lower['win']); ev = p * q['odd'] + lower['push'] - 1
                    support = lower[fav.lower()] if fav else None
                    if p < .6 or ev <= 0: continue
                    if metric in ('CORNERS', 'SHOTS', 'SOT') and support is not None and support < .6: continue
                    confidence = min(p, support) if support is not None else p
                    options[name + '_conservative'].append(dict(pick, p=p, push=lower['push'], ev=ev,
                        confidence=confidence, tier='HIGH' if confidence >= .8 else 'CONFIRMED' if confidence >= .7 else 'DEVELOPING'))
            print('ARCHIVE_REPLAY', packet, cutoff, {k: len(v) for k, v in options.items()}, flush=True)
        reports = {}
        for name in ('control_point', 'control_conservative', 'coupled_point', 'coupled_conservative'):
            # Selection precedes all actual-label lookups, including missing labels.
            selected = select_scope(options[name]); scored, missing = settle_selected(selected, truth)
            reports[name] = dict(overall=stats(scored), selectedBeforeSettlement=len(selected), missingActual=missing,
                byMetric={m:dict(scored=stats([r for r in scored if r['spec'][0] == m]),
                    selected=sum(r['spec'][0] == m for r in selected), missing=missing.get(m, 0)) for m in COUNT_METRICS},
                picks=scored, unscored=[r for r in selected if r['fixture'] not in {s['fixture'] for s in scored if s['spec'][0] == r['spec'][0]}])
        write(out / ('priced_' + packet + '.json'), dict(reports=reports, diagnostics=dict(diagnostics),
            quotes=len(count_quotes), fixtures=len({q['fixture'] for q in count_quotes}), untouchedHoldout=False,
            providerCalls=0, refits=0, thresholdsChanged=False, marketsSelectedUsingROI=False))
        results[packet] = {k: {a: b for a, b in v.items() if a not in ('picks', 'unscored')} for k, v in reports.items()}
        print('ARCHIVE_RESULT', packet, {k: v['overall'] for k, v in reports.items()}, flush=True)
    write(out / 'summary.json', dict(status='COMPLETE', results=results, providerCalls=0, llmCalls=0,
        historySha256=digest(json.dumps(history, sort_keys=True, separators=(',', ':')).encode()),
        frozenModelInputsVerified=True, frozenMeanFits=0, appChanges=0, untouchedHoldout=False,
        prospectiveCohortChanged=False, replayMetrics=list(COUNT_METRICS)))
    return results


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cache', type=Path, default=Path('/tmp/statmaker-git-price-cache'))
    parser.add_argument('--collect-only', action='store_true'); parser.add_argument('--replay-only', action='store_true')
    args = parser.parse_args(); args.output.mkdir(parents=True, exist_ok=True)
    history = direct.load_actual_results(absolute_time('2026-10-16T00:00:00Z'))['GLOBAL']
    if not args.replay_only: print('ARCHIVE_COVERAGE', collect(args.output, args.cache, history), flush=True)
    if not args.collect_only: replay(args.output, history)


if __name__ == '__main__': main()
