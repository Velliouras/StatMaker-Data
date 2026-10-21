"""Current UAT decisions from frozen point models and exact current bookmaker quotes.

No fits, provider calls or retrospective selections. Rejected/unissued markets
are evaluated again on the next run; only issued ledger families are reserved.
"""
import hashlib
import json
import math
from collections import Counter, defaultdict
from .build import ROOT, OUT, read, write, config
from .catalog import app_market, mapped_spec, families
from .policy import time
from .quotes import current_quotes

POLICY = 'FAVOURITE_MARKET_POINT_UAT_V2'
SHOT_MARKETS = {'MATCH_SHOTS', 'TEAM_SHOTS', 'MATCH_SHOTS_ON_TARGET', 'TEAM_SHOTS_ON_TARGET'}
ATTACK = {'GOALS_FT', 'GOALS_1H', 'GOALS_2H', 'CORNERS', 'SHOTS', 'SOT'}


def preference(spec, favourite):
    """Support the favourite in directional markets; neutral props stand alone."""
    metric, scope, side, team, line = spec
    if 'CARD' in metric:
        return None
    if scope == 'TEAM' and metric in ATTACK or scope == 'BOTH_HALVES' and team:
        if favourite not in ('HOME', 'AWAY'):
            return None
        positive = side in ('OVER', 'YES')
        return 0 if (team == favourite) == positive else None
    if scope in ('RESULT', 'DC', 'DNB', 'RESULT_TOTAL', 'HTFT', 'EXACT', 'MARGIN'):
        if favourite not in ('HOME', 'AWAY'):
            return None
        if scope == 'DC':
            return 0 if side in ('HOME_OR_AWAY', 'HOME_OR_DRAW' if favourite == 'HOME' else 'AWAY_OR_DRAW') else None
        if scope == 'HTFT':
            return 0 if side.endswith('_' + favourite) else None
        if scope == 'EXACT':
            home, away = map(int, str(line).replace('-', ':').split(':'))
            return 0 if (home > away if favourite == 'HOME' else away > home) else None
        return 0 if side == favourite or side.startswith(favourite + '_') else None
    return 1


def receipt_favourites(lookup):
    groups = defaultdict(lambda: defaultdict(set))
    for group in lookup.values():
        for q in group['rows']:
            if q['providerMarketName'].strip().casefold() not in ('ml', '1x2', 'match result'):
                continue
            side = {'1':'HOME', 'X':'DRAW', '2':'AWAY'}.get(str(q['selection']).upper(), str(q['selection']).upper())
            if side in ('HOME', 'DRAW', 'AWAY'):
                groups[str(q['providerEventId']), q['bookmaker'], q['sourceReceipt']][side].add(q['odd'])
    result = {}
    for key, values in groups.items():
        if set(values) != {'HOME', 'DRAW', 'AWAY'} or any(len(v) != 1 for v in values.values()):
            continue
        odds = {k:next(iter(v)) for k,v in values.items()}
        if not all(1 < v < 1000 for v in odds.values()) or not .85 <= sum(1/v for v in odds.values()) <= 1.25:
            continue
        winner = min(odds, key=odds.get)
        if winner != 'DRAW' and list(odds.values()).count(odds[winner]) == 1:
            result[key] = winner
    return result


def candidates(actual, now, leagues, used):
    from scripts.shots_uat.sources import Identities
    from betting_v2.research_quotes import feature_cutoff
    from betting_v2.live_revised import load_revised
    from betting_v2.exact_forecast_adapter import forecast
    from .all_paper import bank
    cfg = config(); audit = Counter(); identity = Identities(ROOT, actual)
    lookup = current_quotes(now); favourites = receipt_favourites(lookup)
    bindings = defaultdict(list)
    for league in leagues:
        if not league.get('enabledForBetting', True):
            continue
        for match in league.get('matches', []):
            try:kickoff = time(match['kickoff'])
            except (ValueError,TypeError,KeyError):continue
            if not now + 900 < kickoff < now + 7*86400:
                continue
            bound, reason = identity.bind(league, match)
            if bound is not None:
                bindings[str(match['id']), kickoff].append((league, bound, reason))
    proposals = []
    for group in lookup.values():
        for original in group['rows']:
            if not 1.8 < original['odd'] < 3 or not now + 900 < original['kickoff'] < now + 7*86400:
                continue
            choices = bindings.get((str(original['providerEventId']), original['kickoff']), [])
            if len(choices) != 1:
                audit['unboundFixture'] += 1; continue
            league, (match, home, away, api), reason = choices[0]
            if (original['_providerHome'], original['_providerAway']) != (match.get('providerHomeTeam'), match.get('providerAwayTeam')):
                audit['providerTeamMismatch'] += 1; continue
            event = {'home': original['_providerHome'], 'away': original['_providerAway']}
            spec = mapped_spec(original, event)
            market = app_market(spec) if spec else None
            if not market or market in SHOT_MARKETS or not cfg['markets'][market].get('uatPilotEnabled'):
                continue
            if used & {(str(api), f) for f in families(spec)}:
                continue
            favourite = favourites.get((str(original['providerEventId']), original['bookmaker'], original['sourceReceipt']))
            rank = preference(spec, favourite)
            if rank is None:
                audit['opposesFavouriteOrMissingEvidence'] += 1; continue
            # An ambiguous current selection/price never becomes an app issue.
            duplicates = [q['odd'] for q in group['rows'] if q['selection'] == original['selection']
                          and q.get('line') == original.get('line')
                          and q.get('providerSelectionId') == original.get('providerSelectionId')]
            if set(duplicates) != {original['odd']}:
                audit['ambiguousPrice'] += 1; continue
            proposals.append(dict(original, spec=spec, appMarket=market, _home=home, _away=away,
                _favourite=favourite, preference=rank, featureCutoff=feature_cutoff(original['observed']),
                sourceMarket=original['providerMarketName'], fixture=f'{original["kickoff"]}:{home}:{away}'))
    # The numeric code and every model artifact retain their existing hash bindings.
    frozen = read(ROOT/'config/market_watch_all_source.json')
    changed = [p for p,h in frozen['files'].items() if p.startswith('scripts/betting_v2/') and
               (not (ROOT/p).exists() or hashlib.sha256((ROOT/p).read_bytes()).hexdigest() != h)]
    if changed:
        raise ValueError('Frozen numerical code changed: ' + str(changed))
    numeric_history=list({(r['_date'],r['_home'],r['_away']):r for r in actual}.values())
    selected = []; prepared_count = 0
    for cutoff in sorted({q['featureCutoff'] for q in proposals}):
        model, profile, meta = load_revised(ROOT, numeric_history, cutoff)
        cache = {}
        fingerprint = hashlib.sha256(json.dumps(meta, sort_keys=True).encode()).hexdigest()
        for q in proposals:
            if q['featureCutoff'] != cutoff:
                continue
            key = q['_home'], q['_away'], q['spec'][0]
            if key not in cache:
                cache[key] = bank(model, *key); prepared_count += 1
            values = cache[key]
            if not values:
                audit['missingModelPrediction'] += 1; continue
            row, pf, prepared = values[0]
            try:
                point = forecast(row, pf, q['spec'], prepared)
            except ValueError as error:
                if str(error) == 'Half allocation missing':
                    audit['missingPeriodAllocation'] += 1; continue
                raise
            p, push = point['winProbability'], point['pushProbability']
            if not (.6 <= p <= 1 and 0 <= push and p + push <= 1.000001 and p*q['odd'] + push > 1):
                audit['probabilityOrEV'] += 1; continue
            model_id = 'COUNT_COUPLED_V1' if q['appMarket'] in ('MATCH_CORNERS','TEAM_CORNERS','MATCH_FOULS','TEAM_FOULS') else 'ALL_MARKETS_JOINT_V1'
            sid = hashlib.sha256(json.dumps([POLICY, q['fixture'], q['spec'], q['bookmaker'], q['odd'], fingerprint], sort_keys=True).encode()).hexdigest()
            selected.append(dict(q, _monitorId=sid, _monitorP=p, _monitorPush=push, _monitorModel=model_id,
                _monitorPolicy=POLICY, _modelFingerprint=fingerprint, ev=p*q['odd']+push-1))
    audit.update(eligibleOptions=len(selected), currentProposals=len(proposals), preparedPairs=prepared_count)
    write(OUT/'recommendation_coverage.json', dict(generatedAt=now, policyId=POLICY, audit=dict(audit), providerCalls=0, refits=0))
    return selected
