#!/usr/bin/env python3
"""Small, independent current-season results and official standings read model.

No emulator, odds refresh, historical statistics backfill or Actions artifacts.
Only successful provider responses advance freshness. Missing responses preserve
the last valid season. Quota is allocated by the common Actions startup guard.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from scripts.build_league_tables_program import CONTRACT, COMPLETED, current_leagues, instant, normalize_fixture, goal_events, read, write
from api_football_budget import BudgetStop, priority

FOLDER = 'data/statmaker/priority_refresh'
TIERS = ('primary', 'europe', 'background')
LIVE_STATUSES = {'1H', 'HT', '2H', 'ET', 'BT', 'P'}


def due(stamp, minutes, now):
    parsed = instant(stamp)
    # A delayed run must not cause the next quarter-hour to be skipped.
    # Independent cron/watchdog wakeups share the same UTC time bucket.
    return parsed is None or now.timestamp()//(minutes*60) > parsed.timestamp()//(minutes*60)


def current_policy(root, policy):
    if not (root/'data/statmaker/domestic_enriched/index.json').is_file():
        return policy
    primary = {'E0','SP1','D1','I1','F1','G1','G2','CL','EL','UECL'}
    europe = set(policy.get('europeCountries') or [r['country'] for r in policy['leagueIds'].values()
                                                 if r['tier'] in ('primary','europe')])
    rows = {}
    for code, r in current_leagues(root).items():
        rows[str(r['api_football_league_id'])] = dict(code=code, season=int(r['api_football_season']),
            country=r['country'], competition=r['league'], displaySeason=r['app_season'],
            tier='primary' if code in primary else 'europe' if r['country'] in europe else 'background')
    for r in read(root/'data/statmaker/uefa_current_stats.json').get('competitions', []):
        code = {'CONF':'UECL'}.get(r['leagueCode'],r['leagueCode'])
        if code in ('CL','EL','UECL'):
            rows[str(r['apiFootballLeagueId'])] = dict(code=code, season=int(r['season']), country='UEFA',
                competition=r['name'], displaySeason=r['displaySeason'], tier='primary')
    if rows:
        policy = {**policy, 'leagueIds': rows}
        write(root/'config/api_football_refresh_policy.json', policy)
    return policy


def merge_matches(old, raw, league_id, season, code, aliases):
    result = {int(f['fixture_id']): f for f in old if f.get('fixture_id')}
    for row in raw:
        league = row.get('league') or {}
        if league.get('id') != int(league_id) or league.get('season') != season:
            continue
        # UEFA League Tables concern the league phase and subsequent rounds.
        if code in ('CL', 'EL', 'UECL') and any(s in str(league.get('round', '')).lower()
                                               for s in ('qualif', 'preliminary')):
            continue
        match = normalize_fixture(row, code, aliases)
        if not match or not match.get('date_utc'):
            continue
        previous = result.get(match['fixture_id'], {})
        clock = (row.get('fixture') or {}).get('status') or {}
        match['elapsed'] = clock.get('elapsed') if type(clock.get('elapsed')) is int and 0 <= clock['elapsed'] <= 130 else None
        match['extra_time'] = clock.get('extra') if type(clock.get('extra')) is int and 0 <= clock['extra'] <= 60 else None
        # Never replace a verified final score with a transient NS/null response.
        if previous.get('status') in COMPLETED and match['status'] not in COMPLETED:
            continue
        if match['status'] in COMPLETED and any(not isinstance(match.get(k), int)
                                                for k in ('home_goals', 'away_goals')):
            continue
        result[match['fixture_id']] = {**previous, **match,
            'normalized_stats': previous.get('normalized_stats') or {}}
    return sorted(result.values(), key=lambda x: (x['date_utc'], x['fixture_id']))


def refresh_live_goals(doc, provider, tier, now, allowance=12):
    """Fetch only changed live goals; share the existing provider cap and budget guard."""
    calls = 0
    for match in doc.get('fixtures', []):
        status = match.get('status')
        if status not in LIVE_STATUSES and not (status in COMPLETED and match.get('live_goal_tracking')):
            continue
        score = [match.get('home_goals'), match.get('away_goals')]
        if any(type(n) is not int or n < 0 for n in score):
            continue
        match['live_goal_tracking'] = True
        prior_score = match.get('goal_events_score')
        valid = prior_score == score and len(match.get('goal_events', [])) == sum(score)
        if not valid:
            # VAR corrections must remove stale names immediately, even if the event read fails.
            match.pop('goal_events', None)
            match.pop('goal_events_score', None)
        if sum(score) == 0:
            match.update(goal_events=[], goal_events_score=score, goal_events_status=status,
                         goal_events_checked_at=now.isoformat())
            continue
        if valid and status in COMPLETED and match.get('goal_events_status') == status:
            continue
        if valid and match.get('goal_events_status') == status and not due(match.get('goal_events_checked_at'), 15, now):
            continue
        if calls >= allowance:
            continue
        calls += 1
        raw = provider.get('fixtures/events', {'fixture': int(match['fixture_id'])}, tier)
        verified = goal_events(raw, match)
        if verified is not None:
            match.update(goal_events=verified, goal_events_score=score, goal_events_status=status,
                         goal_events_checked_at=now.isoformat())
    return calls


def standings(raw, league_id, season, aliases):
    groups = []
    for response in raw:
        league = response.get('league') or {}
        if league.get('id') == int(league_id) and league.get('season') == season:
            groups.extend(league.get('standings') or [])
    rows = []
    for group in groups:
        for item in group:
            team, games = item.get('team') or {}, item.get('all') or {}
            goals = games.get('goals') or {}
            values = [item.get('rank'), games.get('played'), games.get('win'), games.get('draw'),
                      games.get('lose'), goals.get('for'), goals.get('against'), item.get('points')]
            if not team.get('name') or any(not isinstance(v, int) for v in values):
                continue
            rows.append(dict(zip(('rank','played','wins','draws','losses','goalsFor','goalsAgainst','points'), values),
                             team=aliases.get(team.get('id')) or team['name'], teamId=team.get('id'),
                             teamLogo=team.get('logo'), group=item.get('group')))
    # Separate groups cannot be displayed as a single ranked table. Retain them
    # explicitly; consumers may use a verified local table until group UI exists.
    return rows, len(groups) == 1 and len(rows) == len(groups[0]) and len({r['rank'] for r in rows}) == len(rows)


class Provider:
    def __init__(self, cap=180):
        self.calls, self.cap = 0, cap

    def get(self, endpoint, params, tier):
        if self.calls >= self.cap:
            raise BudgetStop('priority refresh per-run cap='+str(self.cap))
        key = os.environ.get('API_FOOTBALL_KEY')
        if not key:
            raise RuntimeError('Provider key is missing')
        request = Request('https://v3.football.api-sports.io/'+endpoint+'?'+urlencode(params),
                          headers={'x-apisports-key': key, 'User-Agent': 'StatMaker-Priority-Refresh'})
        with priority(tier):
            self.calls += 1
            with urlopen(request, timeout=30) as response:
                body = response.read(6_000_001)
            if len(body) > 6_000_000:
                raise ValueError('Oversized provider response')
            doc = json.loads(body)
            if doc.get('errors') or not isinstance(doc.get('response'), list):
                raise ValueError('Invalid provider response')
            time.sleep(.25)
            return doc['response']


def refresh(root, provider, now):
    policy = current_policy(root, read(root/'config/api_football_refresh_policy.json'))
    folder = root/FOLDER
    docs, specs = {}, policy['leagueIds']
    for league_id, row in specs.items():
        code = row['code']
        old = read(folder/(code+'.json'))
        if old.get('season') != row['season']:
            old = {}
        seed = read(root/'data/statmaker/league_tables'/(code+'.json'))
        if seed.get('season') != row['season']:
            seed = {}
        docs[code] = {**seed, **old, 'contract': CONTRACT, 'leagueCode': code,
                      'season': row['season'], 'country': row['country'], 'competition': row['competition'],
                      'displaySeason': row['displaySeason'], 'apiFootballLeagueId': int(league_id)}
    errors, stopped = [], []
    # One batched date query serves every competition, including late final whistles.
    global_rows = []
    primary_due = any(due(d.get('resultsCheckedAtUTC'), policy['tiers'][specs[str(d['apiFootballLeagueId'])]['tier']]['resultsMinutes'], now)
                      for d in docs.values())
    if primary_due:
        try:
            for offset in (0, -1, -2):
                # Today's final status: every five minutes. Previous-day repair:
                # every 15 minutes, keeping daily calls bounded despite faster FT checks.
                if offset and now.minute % 15 >= 5:
                    continue
                global_rows.extend(provider.get('fixtures', {'date': (now+timedelta(days=offset)).date().isoformat(),
                                                            'timezone': 'UTC'}, 'primary'))
        except BudgetStop as exc:
            stopped.append(exc.reason)
            global_rows = None
        except (OSError, ValueError, RuntimeError) as exc:
            errors.append('results: '+type(exc).__name__)
            global_rows = None
    stamp = now.isoformat()
    for tier in TIERS:
        for league_id, row in specs.items():
            if row['tier'] != tier:
                continue
            code, season = row['code'], row['season']
            doc = docs[code]
            aliases = {int(f[side+'_team_id']): f[side+'_team'] for f in doc.get('fixtures', [])
                       for side in ('home','away') if f.get(side+'_team_id') and f.get(side+'_team')}
            cadence = policy['tiers'][tier]
            try:
                # A complete season/calendar request seeds all results and future
                # fixtures; daily reconciliation also catches postponed dates.
                if due(doc.get('seasonReconciledAtUTC'), 1440, now):
                    raw = provider.get('fixtures', {'league': int(league_id), 'season': season}, tier)
                    if raw:
                        doc['fixtures'] = merge_matches(doc.get('fixtures', []), raw, league_id, season, code, aliases)
                        doc['seasonReconciledAtUTC'] = stamp
                if global_rows is not None and primary_due and due(doc.get('resultsCheckedAtUTC'), cadence['resultsMinutes'], now):
                    doc['fixtures'] = merge_matches(doc.get('fixtures', []), global_rows, league_id, season, code, aliases)
                    doc['resultsCheckedAtUTC'] = stamp
                active = any(instant(f.get('date_utc')) and
                             now-timedelta(hours=6) <= instant(f['date_utc']) <= now+timedelta(hours=18)
                             for f in doc.get('fixtures', []))
                interval = cadence['standingsMinutes'] if active else 1440
                if due(doc.get('standingsCheckedAtUTC'), interval, now):
                    raw = provider.get('standings', {'league': int(league_id), 'season': season}, tier)
                    table, single = standings(raw, league_id, season, aliases)
                    doc['standingsCheckedAtUTC'] = stamp
                    if table:
                        doc.update(standings=table, singleStandingsGroup=single, standingsObservedAtUTC=stamp)
            except BudgetStop as exc:
                stopped.append(tier+': '+exc.reason)
                break
            except (OSError, ValueError, RuntimeError) as exc:
                errors.append(code+': '+type(exc).__name__)
    event_allowance = 12
    for tier in TIERS:
        for row in specs.values():
            doc = docs[row['code']]
            if row['tier'] != tier or doc.get('resultsCheckedAtUTC') != stamp or event_allowance <= 0:
                continue
            before = provider.calls
            try:
                refresh_live_goals(doc, provider, tier, now, event_allowance)
            except BudgetStop as exc:
                stopped.append('live goals: '+exc.reason)
                event_allowance = 0
            except (OSError, ValueError, RuntimeError) as exc:
                errors.append(row['code']+' live goals: '+type(exc).__name__)
            event_allowance -= provider.calls - before
    if provider.calls == 0 and not stopped and not errors and (folder/'index.json').is_file():
        print('PRIORITY_REFRESH_ALREADY_FRESH apiCalls=0')
        return read(folder/'index.json')
    catalogue = []
    for code, doc in docs.items():
        doc['generatedAtUTC'] = stamp
        write(folder/(code+'.json'), doc)
        catalogue.append({k:doc[k] for k in ('leagueCode','country','competition','season','displaySeason')})
    index = dict(contract=CONTRACT, schemaVersion=1, generatedAtUTC=stamp, leagues=catalogue,
                 apiCalls=provider.calls, providerStops=stopped, errors=errors,
                 cadence=policy['tiers'], totalStop=6000, backgroundStop=4500, reserve=1500)
    write(folder/'index.json', index)
    print(json.dumps(index, ensure_ascii=False))
    if errors and not any(d.get('resultsCheckedAtUTC') == stamp for d in docs.values()):
        raise RuntimeError('No fresh results response; last valid data retained')
    return index


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--max-requests', type=int, default=180)
    args = parser.parse_args()
    if not 1 <= args.max_requests <= 180:
        parser.error('Per-run maximum is 180')
    refresh(Path(__file__).resolve().parents[1], Provider(args.max_requests), datetime.now(timezone.utc).replace(microsecond=0))
