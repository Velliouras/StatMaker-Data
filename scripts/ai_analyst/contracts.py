"""Point-in-time inputs and strict, non-numeric LLM review contracts."""
import copy
import gzip
import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

VERSION = 'statmaker-ai-analyst-shadow-v2-local'
METRICS = {'GOALS_FT': ('FTHG', 'FTAG'), 'CORNERS': ('HC', 'AC'),
           'SHOTS': ('HS', 'AS'), 'SOT': ('HST', 'AST'), 'FOULS': ('HF', 'AF'),
           'YELLOW_CARDS': ('HY', 'AY')}
VERDICTS = {'SUPPORT', 'CAUTION', 'ABSTAIN'}
CODES = {'COVERAGE_LIMITED', 'MODEL_MARKET_GAP', 'HISTORICAL_VARIABILITY',
         'COHERENT_WITH_HISTORY', 'QUOTE_STALENESS', 'PUSH_RISK', 'NO_ADDITIONAL_EDGE'}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def timestamp(value):
    d = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if d.tzinfo is None:
        raise ValueError('Timezone required')
    return d.timestamp()


def utc(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def finite(value, low=None, high=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('Finite number required')
    if low is not None and value < low or high is not None and value > high:
        raise ValueError('Number outside contract')
    return float(value)


def within(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('Path escapes repository')
    return path


def read_bound(root, relative, bindings):
    path = within(root, relative)
    raw = path.read_bytes()
    bindings[relative] = digest(raw)
    return json.loads(gzip.decompress(raw) if path.suffix == '.gz' else raw)


def offer_key(row):
    return (row['fixture'], row['bookmaker'], tuple(row['spec']), row['sourceReceipt'], row['odd'])


def recent_stats(history, row, cutoff):
    """Descriptive prior counts only; no new probability or goal model."""
    result = []
    for team, side in ((row['_home'], 'HOME'), (row['_away'], 'AWAY')):
        prior = [r for r in history if r['_date'] + 86400 <= cutoff and team in (r['_home'], r['_away'])]
        prior.sort(key=lambda r: r['_date'])
        if prior:
            result.append(dict(kind='REST_DAYS', team=side,
                               value=(row['kickoff'] - prior[-1]['_date']) / 86400))
        for metric, keys in METRICS.items():
            values = []
            for r in prior:
                i = 0 if r['_home'] == team else 1
                stats = r.get('normalized_stats') or {}
                pair = [stats.get(k) for k in keys]
                if metric == 'GOALS_FT' and any(x is None for x in pair):
                    pair = [r.get('fthg'), r.get('ftag')]
                try:
                    pair = [finite(v, 0) for v in pair]
                except ValueError:
                    continue
                values.append((pair[i], pair[1-i], r['_date']))
            values = values[-8:]
            if values:
                n = len(values)
                result.append(dict(kind='PRIOR_COUNTS', team=side, metric=metric, sample=n,
                                   meanFor=sum(x[0] for x in values) / n,
                                   meanAgainst=sum(x[1] for x in values) / n,
                                   maxHistoricalKickoff=utc(max(x[2] for x in values))))
    return result


def build_packets(root, config, now):
    bindings = {}
    history = read_bound(root, config['historyPath'], bindings)
    packets = []
    for source in config['sources']:
        folder = source['directory']
        index = read_bound(root, folder + '/index.json', bindings)
        proof = read_bound(root, folder + '/publication_evidence.json', bindings)
        candidate = read_bound(root, folder + '/candidate.json', bindings)
        candidate_hash = bindings[folder + '/candidate.json']
        for item in index['cohorts']:
            name = item['path']
            if Path(name).name != name or not name.endswith('.json.gz'):
                raise ValueError('Invalid cohort filename')
            doc = read_bound(root, folder + '/' + name, bindings)
            if bindings[folder + '/' + name] != item['sha256']:
                raise ValueError('Source cohort hash changed')
            evidence = proof.get(name)
            if not evidence:
                continue
            if evidence['sha256'] != item['sha256'] or doc['candidateSha256'] != candidate_hash:
                raise ValueError('Source candidate/publication hash mismatch')
            published = timestamp(evidence['publiclyVerifiedAtUTC'])
            if not timestamp(doc['frozenAtUTC']) <= published <= now:
                raise ValueError('Invalid source publication clock')
            if doc['candidateId'] != candidate['candidateId']:
                raise ValueError('Mixed source candidate')
            model = doc['models'][source['model']]
            groups = defaultdict(list)
            for row in model['forecasts']:
                if row['kickoff'] > now + 120 and published < row['kickoff']:
                    groups[row['fixture']].append(row)
            selected = defaultdict(list)
            for row in model['selected']:
                selected[row['fixture']].append(row)
            for fixture, forecasts in sorted(groups.items()):
                first = forecasts[0]
                if any((r['_home'], r['_away'], r['kickoff']) !=
                       (first['_home'], first['_away'], first['kickoff']) for r in forecasts):
                    raise ValueError('Mixed fixture identity')
                if any(not r['featureCutoff'] <= r['observed'] <= now < r['kickoff'] or
                       now-r['observed'] > 86400 for r in forecasts):
                    continue
                forecast_keys = {offer_key(r) for r in forecasts}
                original = copy.deepcopy(selected[fixture])
                offers = []
                facts = []
                def fact(value):
                    identity = 'F' + str(len(facts) + 1).zfill(3)
                    facts.append(dict(id=identity, **value))
                    return identity
                fact(dict(kind='CONTEXT_AVAILABILITY', confirmedLineups=False,
                          injuries=False, weather=False, reason='NO_POINT_IN_TIME_CONTEXT_ADAPTER'))
                fact(dict(kind='MARKET_COVERAGE', metrics=sorted({r['spec'][0] for r in forecasts}),
                          quotes=len(forecasts), futureResultsIncluded=False))
                cutoff = min(r['featureCutoff'] for r in forecasts)
                for value in recent_stats(history, first, cutoff):
                    fact(value)
                for row in original:
                    if offer_key(row) not in forecast_keys:
                        raise ValueError('Selected offer absent from source forecasts')
                    p, push, odd = finite(row['p'], 0, 1), finite(row['push'], 0, 1), finite(row['odd'], 1)
                    if p + push > 1 + 1e-10 or abs(row['ev'] - (p*odd+push-1)) > 1e-9:
                        raise ValueError('Invalid immutable baseline probabilities/EV')
                    identity = digest(canonical(list(offer_key(row))))[:24]
                    fid = fact(dict(kind='BASELINE_OFFER', offerId=identity, spec=row['spec'],
                                    odd=odd, winProbability=p, pushProbability=push,
                                    expectedValue=row['ev'], quoteAgeHours=(now-row['observed'])/3600))
                    offers.append(dict(offerId=identity, factId=fid))
                fixture_ref = digest(fixture.encode())[:24]
                analysis = dict(schemaVersion=1, fixtureRef=fixture_ref, teams=['HOME', 'AWAY'],
                                facts=facts, offers=offers)
                binding = dict(sourceDirectory=folder, sourceModel=source['model'], sourceCohort=name,
                               sourceSha256=item['sha256'], sourcePublication=evidence,
                               sourceCandidateSha256=candidate_hash, fixture=fixture,
                               home=first['_home'], away=first['_away'], kickoff=first['kickoff'],
                               featureCutoff=cutoff, baselineRows=original)
                packet = dict(version=VERSION, preparedAtUTC=utc(now), binding=binding, analysis=analysis)
                packet['inputSha256'] = digest(canonical(packet))
                packets.append(packet)
    return sorted(packets, key=lambda p: (not bool(p['binding']['baselineRows']),
                                        p['binding']['kickoff'], p['analysis']['fixtureRef'])), bindings


def exact_keys(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError('Unexpected or missing AI fields')


def validate_review(review, packet):
    exact_keys(review, ('schemaVersion', 'fixtureRef', 'dataAssessment', 'reviews'))
    if type(review['schemaVersion']) is not int or review['schemaVersion'] != 1 or review['fixtureRef'] != packet['analysis']['fixtureRef']:
        raise ValueError('AI fixture/schema mismatch')
    allowed = {f['id'] for f in packet['analysis']['facts']}
    def text_and_refs(value):
        if not isinstance(value['note'], str) or not 1 <= len(value['note']) <= 800:
            raise ValueError('Invalid AI explanation')
        # Numbers are rendered from the unchanged baseline, never an LLM narrative.
        if any(c.isdigit() for c in value['note']) or '%' in value['note'] or 'http' in value['note'].lower():
            raise ValueError('AI explanation contains unsupported numbers/link')
        ids = value['factIds']
        if not isinstance(ids, list) or not ids or len(ids) > 12 or any(x not in allowed for x in ids):
            raise ValueError('AI explanation lacks valid fact citations')
    data = review['dataAssessment']
    exact_keys(data, ('level', 'factIds', 'note'))
    if data['level'] not in ('LIMITED', 'ADEQUATE'):
        raise ValueError('Unknown data assessment')
    text_and_refs(data)
    expected = {x['offerId'] for x in packet['analysis']['offers']}
    if not isinstance(review['reviews'], list) or len(review['reviews']) != len(expected):
        raise ValueError('AI must review every baseline offer exactly once')
    seen = set()
    for r in review['reviews']:
        exact_keys(r, ('offerId', 'verdict', 'reasonCodes', 'factIds', 'note'))
        if r['offerId'] not in expected or r['offerId'] in seen or r['verdict'] not in VERDICTS:
            raise ValueError('AI invented/duplicated an offer or verdict')
        seen.add(r['offerId'])
        if not isinstance(r['reasonCodes'], list) or not r['reasonCodes'] or any(c not in CODES for c in r['reasonCodes']):
            raise ValueError('Invalid AI reason codes')
        text_and_refs(r)
    return review
