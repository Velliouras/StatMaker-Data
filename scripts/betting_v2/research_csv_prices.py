"""Archive-only Football-Data prices joined to immutable daily predictions.

No fit, LLM, provider API, app publication or outcome-dependent choice. Closing
quotes are a source-labelled retrospective benchmark, not timestamped fills.
Pre-closing quotes are retained but never replayed against match-day features.
"""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import csv
import gzip
import hashlib
import io
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from betting_v2.opponent_matchup import normalized, timestamp
from betting_v2.exact_forecast_adapter import prepare
from betting_v2.validated_walk_forward import distribution
from betting_v2.joint_count import outcome
from betting_v2.research_quotes import assess, select, stats, MODES

ROOT = Path(__file__).resolve().parents[2]
MAX_BYTES = 8_000_000
BOOKS = ('Bet365', 'Pinnacle')
PREFIXES = {'Bet365': ('B365',), 'Pinnacle': ('PS', 'P')}
MAIN = 'E0 E1 E2 E3 EC SC0 SC1 SC2 SC3 D1 D2 I1 I2 SP1 SP2 F1 F2 N1 B1 P1 T1 G1'.split()
EXTRA = 'ARG AUT BRA CHN DNK FIN IRL JPN MEX NOR POL ROU RUS SWE SWZ USA'.split()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def write(path, value, zipped=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
    if zipped:
        raw = gzip.compress(raw, mtime=0)
        json.loads(gzip.decompress(raw))
    temp = path.with_suffix(path.suffix+'.tmp')
    temp.write_bytes(raw)
    if temp.read_bytes() != raw:
        raise ValueError('Checkpoint bytes changed')
    temp.replace(path)


def sources():
    """38 configured app competitions with official CSV archive coverage."""
    return ([dict(code=c, layout='main', url=f'https://www.football-data.co.uk/mmz4281/2627/{c}.csv') for c in MAIN]
            + [dict(code=c, layout='extra', url=f'https://www.football-data.co.uk/new/{c}.csv') for c in EXTRA])


def get_source(source, directory):
    path = directory/(source['code']+'.csv.gz')
    if path.exists():
        raw = gzip.decompress(path.read_bytes())
        cached = True
    else:
        request = urllib.request.Request(source['url'], headers={'User-Agent': 'StatMaker archive-only research/1.0'})
        with urllib.request.urlopen(request, timeout=25) as response:
            if not re.fullmatch(r'https://(?:www\.)?football-data\.co\.uk/.*', response.url):
                raise ValueError('Unexpected download redirect')
            raw = response.read(MAX_BYTES+1)
        if len(raw) > MAX_BYTES:
            raise ValueError('CSV exceeds download bound')
        # Validate before replacing a resumable cache. HTML errors are not CSV.
        parse_csv(raw, source)
        temp = path.with_suffix('.tmp')
        temp.write_bytes(gzip.compress(raw, mtime=0))
        if gzip.decompress(temp.read_bytes()) != raw:
            raise ValueError('CSV checkpoint round-trip failed')
        temp.replace(path)
        cached = False
    return raw, dict(source, bytes=len(raw), sha256=digest(raw), cached=cached,
                     retrievedAtUTC=datetime.now(timezone.utc).isoformat(), cachePath=path.name)


def parse_date(value):
    for fmt in ('%d/%m/%Y', '%d/%m/%y', '%Y-%m-%d'):
        try:
            return datetime.strptime(value.strip(), fmt).date().isoformat()
        except (ValueError, AttributeError):
            pass
    return None


def number(value, count=False):
    try:
        x = float(value)
    except (ValueError, TypeError):
        return None
    if not math.isfinite(x):
        return None
    if count:
        return int(x) if x >= 0 and x.is_integer() else None
    return x if x > 1 else None


def first(row, *keys):
    return next((row[k] for k in keys if row.get(k, '').strip()), '')


def parse_csv(raw, source):
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = raw.decode('cp1252')
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    if len(headers) != len(set(headers)) or 'Date' not in headers or not ({'HomeTeam', 'Home'} & set(headers)):
        raise ValueError('Invalid CSV header')
    result = []
    for ordinal, row in enumerate(reader, 2):
        if None in row or any(v is None for v in row.values()):
            raise ValueError(f'Malformed CSV row {ordinal}')
        if not any(v.strip() for v in row.values()):
            continue
        result.append(dict(source=source, row=ordinal, data=row))
    return result


def row_quotes(row):
    """Keep early/closing sets separate; Max/Avg/exchanges are not bookmakers."""
    quotes = []
    for book, prefixes in PREFIXES.items():
        for price_type, marker in [('pre_closing', ''), ('closing', 'C')]:
            for scope, outcomes in [('RESULT', [('HOME', 'H'), ('DRAW', 'D'), ('AWAY', 'A')]),
                                    ('TOTAL', [('OVER', '>2.5'), ('UNDER', '<2.5')])]:
                for side, suffix in outcomes:
                    values = [(prefix+marker+suffix, number(row.get(prefix+marker+suffix))) for prefix in prefixes]
                    values = [(column, odd) for column, odd in values if odd is not None]
                    if not values:
                        continue
                    if len({v for _, v in values}) != 1:
                        continue  # Conflicting PS/P aliases must not choose a convenient price.
                    quotes.append(dict(bookmaker=book, priceType=price_type, sourceColumns=[c for c, _ in values],
                        odd=values[0][1], spec=('GOALS_FT', scope, side, '', 2.5 if scope == 'TOTAL' else None),
                        observedAtUTC=None, exactObservationTimeKnown=False))
    return quotes


def validate_input(doc):
    if doc['schema'] != 1 or doc['testLabelsUsedForSelection'] or doc['untouchedHoldout']:
        raise ValueError('Unexpected frozen input contract')
    seen = set()
    for fold in doc['folds']:
        if not timestamp(fold['fitBefore']) < timestamp(fold['profileBefore']) == timestamp(fold['testFrom']) < timestamp(fold['testBefore']):
            raise ValueError('Invalid chronological model window')
        for f in fold['fixtures']:
            if f['fixture'] in seen:
                raise ValueError('Repeated frozen fixture')
            seen.add(f['fixture'])
            if not timestamp(fold['testFrom']) <= f['featureCutoff'] <= f['date'] < timestamp(fold['testBefore']):
                raise ValueError('Prediction outside frozen test window')
            if f['featureCutoff'] != math.floor(f['date']/86400)*86400:
                raise ValueError('Prediction features are not UTC-day batched')
            if set(f['predictions']) != {'full', 'separate_ft_selected_period'}:
                raise ValueError('Models do not have the same paired cohort')
            if len(f['actual']) != 2 or any(number(v, True) is None for v in f['actual']):
                raise ValueError('Invalid frozen FT label')
            if f['utcDate'] != datetime.fromtimestamp(f['date'], timezone.utc).date().isoformat():
                raise ValueError('Frozen identity date mismatch')
            for row in f['predictions'].values():
                if row['metric'] != 'GOALS_FT' or len(row['means']) != 2 or any(not math.isfinite(v) or v <= 0 for v in row['means']):
                    raise ValueError('Invalid immutable FT prediction')


def recover_schedule_aliases(rows, doc):
    """Resolve names from >=2 dates/opponents, anchored only in existing aliases.

    No names are compared fuzzily. No score, probability or price is read.
    Newly inferred aliases cannot support another inference. Every nonempty
    schedule constraint must agree on one country-scoped canonical identity.
    """
    aliases = {code:dict(values) for code, values in doc['aliases'].items()}
    index = defaultdict(list)
    for fold in doc['folds']:
        for f in fold['fixtures']:
            index[f['league'],f['utcDate']].append(f)
    constraints = defaultdict(list)
    for item in rows:
        row, source = item['data'], item['source']
        code = source['code']; day = parse_date(row.get('Date'))
        if day is None or not doc['testFrom'] <= day < doc['testBefore']:
            continue
        if source['layout'] == 'main' and row.get('Div') != code:
            continue
        raw = {s:normalized(first(row, *keys)) for s,keys in [('home',('HomeTeam','Home')),('away',('AwayTeam','Away'))]}
        existing = doc['aliases'].get(code,{})
        known = {s:existing.get(v) for s,v in raw.items()}
        for side, other in [('home','away'),('away','home')]:
            if not raw[side] or known[side] or not known[other]:
                continue
            candidates = [f for f in index[code,day] if f[other] == known[other]]
            if candidates:
                constraints[code,raw[side]].append(dict(csvRow=item['row'],sourceUrl=source['url'],
                    date=day,opponent=known[other],side=side,
                    candidates=sorted({f[side] for f in candidates}),fixtures=[f['fixture'] for f in candidates]))
    accepted, rejected = [], []
    for (code, raw), evidence in sorted(constraints.items()):
        common = set.intersection(*(set(v['candidates']) for v in evidence))
        if len(common) != 1:
            rejected.append(dict(league=code,alias=raw,reason='CONFLICTING_OR_AMBIGUOUS_SCHEDULE',evidence=evidence))
            continue
        if len({e['date'] for e in evidence}) < 2 or len({e['opponent'] for e in evidence}) < 2:
            rejected.append(dict(league=code,alias=raw,reason='NEEDS_TWO_DATES_AND_DISTINCT_EXISTING_OPPONENTS',evidence=evidence))
            continue
        target = next(iter(common))
        aliases.setdefault(code,{})[raw] = target
        accepted.append(dict(league=code,alias=raw,canonical=target,evidence=evidence))
    return dict(doc,aliases=aliases), dict(accepted=accepted,rejected=rejected,
        labelsPricesAndProbabilitiesUsed=False,minimumDistinctDates=2,minimumDistinctExistingOpponents=2,
        inferredAliasesUsedAsAnchors=False,method='Exact league/date/home-away schedule constraints; no fuzzy match')


def join_rows(rows, doc):
    """Unique league/date/country-alias pair, then independent score validation.

    CSV kickoff timezone is undocumented: do not fabricate an exact UTC match.
    No fuzzy names, adjacent dates, or result-based candidate selection.
    """
    index = defaultdict(list)
    for fold in doc['folds']:
        for f in fold['fixtures']:
            index[f['league'], f['utcDate'], f['home'], f['away']].append((f, fold))
    counts = Counter()
    rejected = []
    joined = {}
    conflicts = set()
    inventory = Counter()
    for item in rows:
        row, source = item['data'], item['source']
        code = source['code']
        day = parse_date(row.get('Date'))
        quotes = row_quotes(row)
        for q in quotes:
            inventory[code, q['bookmaker'], q['priceType'], q['spec'][1]] += 1
        counts['rows'] += 1
        if day is None:
            reason = 'INVALID_DATE'
        elif not doc['testFrom'] <= day < doc['testBefore']:
            counts['outsideFrozenTestWindows'] += 1
            continue
        elif source['layout'] == 'main' and row.get('Div') != code:
            reason = 'DIVISION_MISMATCH'
        else:
            aliases = doc['aliases'].get(code, {})
            home = aliases.get(normalized(first(row, 'HomeTeam', 'Home')))
            away = aliases.get(normalized(first(row, 'AwayTeam', 'Away')))
            candidates = index[code, day, home, away] if home and away else []
            if not home or not away:
                reason = 'UNKNOWN_EXPLICIT_ALIAS'
            elif len(candidates) != 1:
                reason = 'NO_UNIQUE_FROZEN_PREDICTION'
            else:
                f, fold = candidates[0]
                goals = [number(first(row, 'FTHG', 'HG'), True), number(first(row, 'FTAG', 'AG'), True)]
                result = first(row, 'FTR', 'Res')
                expected = 'H' if goals[0] is not None and goals[1] is not None and goals[0] > goals[1] else 'A' if goals[0] is not None and goals[1] is not None and goals[0] < goals[1] else 'D'
                if None in goals:
                    reason = 'MISSING_FINAL_SCORE'
                elif goals != f['actual'] or result and result != expected:
                    reason = 'FINAL_SCORE_CONFLICT'
                else:
                    entry = dict(fixture=f['fixture'], league=code, csvDate=day, csvTime=row.get('Time'),
                        csvRow=item['row'], sourceUrl=source['url'], sourceSha256=item['sha256'],
                        joinMethod='unique league + CSV date equal UTC date + existing country-scoped aliases; FT score cross-check',
                        quotes=quotes, featureCutoff=f['featureCutoff'])
                    previous = joined.get(f['fixture'])
                    if previous and previous['quotes'] != quotes:
                        conflicts.add(f['fixture'])
                    else:
                        joined[f['fixture']] = entry
                    counts['joinedRows'] += 1
                    continue
        counts[reason] += 1
        rejected.append(dict(league=code, date=day, home=first(row, 'HomeTeam', 'Home'),
            away=first(row, 'AwayTeam', 'Away'), reason=reason, csvRow=item['row'], sourceUrl=source['url']))
    for key in conflicts:
        joined.pop(key, None)
    counts['duplicateConflictingFixturesExcluded'] = len(conflicts)
    return sorted(joined.values(), key=lambda v:v['fixture']), dict(counters=dict(counts), rejectedRows=rejected,
        quoteInventory=[dict(league=k[0], bookmaker=k[1], priceType=k[2], market=k[3], quotes=v)
                        for k, v in sorted(inventory.items())])


def replay(doc, joined):
    byfixture = {q['fixture']: q for q in joined}
    reports = {}
    for book in BOOKS:
        options = {(name, policy): [] for name in ('full', 'separate_ft_selected_period') for policy in MODES}
        priced = set()
        for fold in doc['folds']:
            for f in fold['fixtures']:
                saved = byfixture.get(f['fixture'])
                if saved is None:
                    continue
                quotes = [q for q in saved['quotes'] if q['bookmaker'] == book and q['priceType'] == 'closing']
                if not quotes:
                    continue
                priced.add(f['fixture'])
                triplet = {q['spec'][2]: q['odd'] for q in quotes if q['spec'][1] == 'RESULT'}
                fav = None
                if set(triplet) == {'HOME', 'DRAW', 'AWAY'}:
                    side = min(triplet, key=triplet.get)
                    if side != 'DRAW' and triplet[side] <= 1.8:
                        fav = side
                for name, row in f['predictions'].items():
                    profile = fold['profiles'][name]
                    prediction = row, prepare(row, profile), distribution(row, profile)
                    for q in quotes:
                        target = dict(q, fixture=f['fixture'], league=f['league'], testFrom=fold['testFrom'],
                            featureCutoff=f['featureCutoff'], sourceUrl=saved['sourceUrl'],
                            sourceSha256=saved['sourceSha256'], csvRow=saved['csvRow'])
                        for policy in MODES:
                            pick = assess(target, prediction, profile, fav, policy)
                            if pick is not None:
                                y, push = outcome(*f['actual'], q['spec'])
                                options[name, policy].append(dict(pick, y=y, actualPush=push))
        for (name, policy), candidates in options.items():
            picks = select(candidates)
            key = book+'::'+name+'::'+policy
            reports[key] = dict(overall=stats(picks), pricedFixtures=len(priced),
                byMonth={f['testFrom']: stats([p for p in picks if p['testFrom'] == f['testFrom']]) for f in doc['folds']},
                byMarket={s: stats([p for p in picks if p['spec'][1] == s]) for s in ('RESULT', 'TOTAL')},
                byLeague={c: stats([p for p in picks if p['league'] == c]) for c in sorted({p['league'] for p in picks})},
                byTier={t: stats([p for p in picks if p['tier'] == t]) for t in ('HIGH', 'CONFIRMED', 'DEVELOPING')},
                picks=picks)
            print('CSV_REPLAY', key, json.dumps(reports[key]['overall']), flush=True)
    return reports


def run(args):
    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    cache = args.source_cache or out/'sources'
    cache.mkdir(exist_ok=True)
    manifest = json.loads(args.manifest.read_text())
    raw = args.input.read_bytes()
    if digest(raw) != manifest['inputSha256']:
        raise ValueError('Immutable prediction input hash mismatch')
    doc = json.loads(gzip.decompress(raw))
    validate_input(doc)
    write(out/'status.json', dict(status='DOWNLOADING', llmCalls=0, sportsProviderCalls=0))
    rows, inventory, errors = [], [], []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(get_source, s, cache): s for s in sources()}
        for future in as_completed(futures):
            source = futures[future]
            try:
                data, receipt = future.result()
                entries = parse_csv(data, source)
                for item in entries:
                    item['sha256'] = receipt['sha256']
                rows.extend(entries)
                inventory.append(receipt)
                print('CSV_SOURCE', source['code'], len(entries), receipt['sha256'], flush=True)
            except (OSError, ValueError, UnicodeError, csv.Error) as error:
                errors.append(dict(source, error=type(error).__name__+': '+str(error)))
            write(out/'source_inventory.json', dict(sources=sorted(inventory, key=lambda v:v['code']), errors=errors,
                publicCsvRequests=sum(not v['cached'] for v in inventory)+len(errors), sportsProviderCalls=0, llmCalls=0))
    if args.resolve_schedule_aliases:
        doc, alias_evidence = recover_schedule_aliases(rows, doc)
        write(out/'schedule_alias_evidence.json', alias_evidence)
    joined, coverage = join_rows(rows, doc)
    write(out/'joined_quotes.json.gz', joined, zipped=True)
    write(out/'coverage.json', coverage)
    write(out/'status.json', dict(status='REPLAYING', joinedFixtures=len(joined), sourceFailures=len(errors)))
    reports = replay(doc, joined)
    write(out/'replay.json.gz', dict(reports=reports), zipped=True)
    result = dict(status='COMPLETE_WITH_SOURCE_FAILURES' if errors else 'COMPLETE',
        inputSha256=manifest['inputSha256'], trainingDataSha256=doc['trainingDataSha256'],
        inputCommit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        sourceFiles=len(inventory), sourceFailures=errors, rawCsvRows=len(rows), joinedFixtures=len(joined),
        closingPricedFixtures=len({r['fixture'] for r in joined if any(q['priceType']=='closing' for q in r['quotes'])}),
        reports={k:{a:v for a,v in report.items() if a!='picks'} for k,report in reports.items()},
        testFrom=doc['testFrom'], testBefore=doc['testBefore'], untouchedHoldout=False,
        testLabelsUsedForSelection=False, newFits=0, llmCalls=0, sportsProviderCalls=0,
        appPublication=False, primaryBookmaker='Bet365', secondaryBookmaker='Pinnacle',
        scheduleAliasRecovery=args.resolve_schedule_aliases,
        recoveredScheduleAliases=len(alias_evidence['accepted']) if args.resolve_schedule_aliases else 0,
        sourceCache=str(cache),
        policies=list(MODES), priceView='source-labelled closing only',
        exactObservationTimesKnown=False, historicalPublicationTimesVerified=False,
        limitations=[
            'Already inspected development months, not an untouched holdout.',
            'Source-labelled closing prices have no individual quote timestamps or executable-fill verification.',
            'CSV kickoff timezone is unspecified; date/pair joins are explicit and are not exact UTC kickoff joins.',
            'Original daily features exclude match-day results but historical publication times remain unverified.',
            'Pre-closing prices retained for coverage only: match-day features may postdate their collection.',
            'Only 1X2 and total 2.5 are priced here. Missing markets and prices are not synthesized.',
            'Bookmakers, policies, months, tiers and league subsets overlap; no winning subset is selected.',
            'Intervals resample fixture clusters, without a serial-dependence or multiple-comparison adjustment.',
            'No commission, staking optimization or guaranteed profitability is assumed.'])
    write(out/'summary.json', result)
    write(out/'status.json', {k:result[k] for k in ('status','joinedFixtures','closingPricedFixtures','sourceFiles','llmCalls','sportsProviderCalls')})
    lines = ['# Historical CSV price research', '', f"Status: {result['status']}. Joined {len(joined)} fixtures from {len(rows)} CSV rows.",
        '', '| Bookmaker / model / policy | Bets | Fixtures | ROI | 95% fixture interval |',
        '|---|---:|---:|---:|---|']
    for key, report in reports.items():
        s = report['overall']
        ci = s.get('roi95')
        interval = f'{ci[0]:.2%} to {ci[1]:.2%}' if ci else 'insufficient clusters'
        lines.append(f"| {key} | {s['bets']} | {s['fixtures']} | {s.get('roi',0):.2%} | {interval} |")
    lines += ['', *['- '+x for x in result['limitations']], '', '0 LLM calls; 0 sports-provider requests; no app publication or model fitting.']
    (out/'SUMMARY.md').write_text('\n'.join(lines)+'\n')
    print('CSV_COMPLETE', result['status'], len(joined), 'fixtures', flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source-cache', type=Path)
    parser.add_argument('--resolve-schedule-aliases', action='store_true')
    run(parser.parse_args())


if __name__ == '__main__':
    main()
