"""Reconcile delayed official tables against verified regular-season finals.

The official table stays immutable. Projection is allowed only when every team's
official W/D/L and goals match its chronological prefix of completed fixtures.
This preserves point deductions and rejects incomplete or mixed-phase histories.
Unresolved ties keep the official table until the provider resolves the sporting order.
"""
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import re

FIELDS = ('played', 'wins', 'draws', 'losses', 'goalsFor', 'goalsAgainst')
FINAL = {'FT', 'AET', 'PEN'}
CL_STAGE = re.compile(r'league stage\s*-\s*([1-8])', re.IGNORECASE)


def totals(matches, team_id):
    result = dict.fromkeys(FIELDS, 0)
    for match in matches:
        home = match['home_team_id'] == team_id
        scored = match['home_goals' if home else 'away_goals']
        conceded = match['away_goals' if home else 'home_goals']
        result['played'] += 1
        result['wins' if scored > conceded else 'draws' if scored == conceded else 'losses'] += 1
        result['goalsFor'] += scored
        result['goalsAgainst'] += conceded
    return result


def champions_league_projection(doc, now):
    """Return the provisional MD1–7 table, or None if the evidence is incomplete.

    UEFA's live table uses Article 18(a–e) and shares ranks on exact ties.
    The final matchday needs disciplinary and coefficient data; leave that
    table to the provider. https://www.uefa.com/uefachampionsleague/news/0291-1bd88ae04870-e1e038c319e3-1000--champions-league-league-phase-standings-how-t/
    """
    official = doc.get('officialStandings') or []
    if (doc.get('leagueCode'), doc.get('apiFootballLeagueId'), doc.get('country')) != ('CL', 2, 'UEFA'):
        return None
    if not doc.get('officialSingleStandingsGroup') or len(official) != 36:
        return None
    teams = {r.get('teamId') for r in official}
    if (len(teams) != 36 or any(type(t) is not int or t <= 0 for t in teams)
            or {r.get('rank') for r in official} != set(range(1, 37))
            or any(not str(r.get('team') or '').strip()
                   or any(type(r.get(k)) is not int or r[k] < 0 for k in FIELDS + ('points',))
                   or r['points'] != 3 * r['wins'] + r['draws'] for r in official)):
        return None

    rounds, by_team = defaultdict(Counter), defaultdict(list)
    fixture_ids, opponent_pairs = set(), set()
    for match in doc.get('fixtures', []):
        phase = CL_STAGE.fullmatch(str(match.get('round') or '').strip())
        if not phase:
            continue
        md = int(phase[1])
        fid, home, away = (match.get(k) for k in ('fixture_id', 'home_team_id', 'away_team_id'))
        if (type(fid) is not int or fid <= 0 or fid in fixture_ids
                or home not in teams or away not in teams or home == away):
            return None
        pair = frozenset((home, away))
        if pair in opponent_pairs:
            return None
        opponent_pairs.add(pair)
        fixture_ids.add(fid)
        rounds[md].update((home, away))
        if match.get('status') not in FINAL:
            continue
        if (md == 8 or match['status'] != 'FT'
                or any(type(match.get(k)) is not int or match[k] < 0 for k in ('home_goals', 'away_goals'))):
            return None
        try:
            kickoff = datetime.fromisoformat(str(match.get('date_utc')).replace('Z', '+00:00'))
        except ValueError:
            return None
        if kickoff.utcoffset() is None or kickoff > now:
            return None
        for team in (home, away):
            by_team[team].append((kickoff.astimezone(timezone.utc), fid, match))
    if set(rounds) != set(range(1, 9)) or any(dict(members) != dict.fromkeys(teams, 1)
                                               for members in rounds.values()):
        return None

    ranked, additions, included = [], Counter(), Counter()
    for row in official:
        team = row['teamId']
        matches = [m for _, _, m in sorted(by_team[team], key=lambda x: x[:2])]
        played = row['played']
        if played > len(matches) or totals(matches[:played], team) != {k: row[k] for k in FIELDS}:
            return None
        included.update(m['fixture_id'] for m in matches[:played])
        additions.update(m['fixture_id'] for m in matches[played:])
        full = totals(matches, team)
        pts = 3 * full['wins'] + full['draws']
        away = [m for m in matches if m['away_team_id'] == team]
        key = (pts, full['goalsFor'] - full['goalsAgainst'], full['goalsFor'],
               sum(m['away_goals'] for m in away), full['wins'],
               sum(m['away_goals'] > m['home_goals'] for m in away))
        ranked.append((key, {**row, **full, 'points': pts}))
    if not additions or any(n != 2 for n in (*included.values(), *additions.values())):
        return None
    ranked.sort(key=lambda pair: (tuple(-n for n in pair[0]), pair[1]['team'].casefold()))
    rank, previous = 0, None
    for index, (key, row) in enumerate(ranked, 1):
        if key != previous:
            rank = index
        row['rank'] = rank
        previous = key
    return [row for _, row in ranked], sorted(additions)


def reconcile(doc, now):
    """Refresh the app-facing table without provider calls or additive state."""
    if 'officialStandings' not in doc and doc.get('standings'):
        # Migration: prior producers published the official provider table only.
        doc['officialStandings'] = deepcopy(doc['standings'])
        doc['officialSingleStandingsGroup'] = doc.get('singleStandingsGroup', False)
    official = deepcopy(doc.get('officialStandings') or [])
    if not official:
        return False
    was = doc.get('standings')
    doc['standings'] = official
    doc['singleStandingsGroup'] = doc.get('officialSingleStandingsGroup', False)
    doc['standingsSource'] = 'official'
    doc.pop('standingsReconciledAtUTC', None)
    report = {'status': 'official', 'checkedAtUTC': now.isoformat(), 'appliedFixtureIds': []}
    doc['standingsReconciliation'] = report

    def defer(reason):
        report.update(status='awaiting_official', reason=reason)
        return was != doc['standings']

    if (doc.get('leagueCode'), doc.get('apiFootballLeagueId'), doc.get('country')) == ('CL', 2, 'UEFA'):
        projection = champions_league_projection(doc, now)
        if projection is None:
            if sum(r.get('played', 0) for r in official) < 2 * sum(
                f.get('status') in FINAL and CL_STAGE.fullmatch(str(f.get('round') or '').strip()) is not None
                for f in doc.get('fixtures', [])
            ):
                return defer('champions_league_projection_not_verified')
            return was != doc['standings']
        doc['standings'], additions = projection
        doc['standingsSource'] = 'champions_league_verified_finals'
        doc['standingsReconciledAtUTC'] = now.isoformat()
        report.update(status='reconciled', appliedFixtureIds=additions)
        return was != doc['standings']
    if not doc['singleStandingsGroup'] or doc.get('leagueCode') in {'CL', 'EL', 'UECL'}:
        return defer('phase_or_competition_rules')
    teams = {row.get('teamId') for row in official}
    if (any(type(t) is not int or t <= 0 for t in teams) or len(teams) != len(official)
            or len({r.get('rank') for r in official}) != len(official)
            or any(any(type(r.get(k)) is not int or r[k] < 0 for k in FIELDS)
                   or type(r.get('points')) is not int or type(r.get('rank')) is not int
                   or r['rank'] < 1 or r['played'] != r['wins'] + r['draws'] + r['losses']
                   for r in official)):
        return defer('invalid_official_table')
    by_team = defaultdict(list)
    seen = set()
    for match in doc.get('fixtures', []):
        if match.get('status') not in {'FT', 'AET', 'PEN'}:
            continue
        try:
            kickoff = datetime.fromisoformat(str(match['date_utc']).replace('Z', '+00:00'))
            if kickoff.utcoffset() is None or kickoff > now:
                return defer('invalid_final_date')
            fid, home, away = (match[k] for k in ('fixture_id', 'home_team_id', 'away_team_id'))
            if (match['status'] != 'FT' or any(type(n) is not int or n <= 0 for n in (fid, home, away))
                    or home == away or home not in teams or away not in teams
                    or any(type(match.get(k)) is not int or match[k] < 0
                           for k in ('home_goals', 'away_goals'))):
                return defer('invalid_or_out_of_phase_final')
            if fid in seen:
                return defer('duplicate_fixture')
            seen.add(fid)
            phase = re.sub(r'\s*-\s*\d+\s*$', '', str(match.get('round', ''))).strip().lower()
            if phase != 'regular season':
                return defer('phase_or_competition_rules')
            for team in (home, away):
                by_team[team].append((kickoff.astimezone(timezone.utc), fid, match))
        except (KeyError, TypeError, ValueError, OverflowError):
            return defer('invalid_final')
    projected, additions, changed_teams = [], set(), set()
    for row in official:
        matches = [entry[2] for entry in sorted(by_team[row['teamId']], key=lambda x: x[:2])]
        played = row['played']
        if len(matches) < played or totals(matches[:played], row['teamId']) != {k: row[k] for k in FIELDS}:
            return defer('official_prefix_not_verified')
        full = totals(matches, row['teamId'])
        offset = row['points'] - 3 * row['wins'] - row['draws']
        projected.append({**row, **full, 'points': 3 * full['wins'] + full['draws'] + offset})
        additions.update(m['fixture_id'] for m in matches[played:])
        if len(matches) > played:
            changed_teams.add(row['teamId'])
    if not additions:
        return was != doc['standings']
    ties = defaultdict(list)
    original_points = {r['teamId']: r['points'] for r in official}
    for row in projected:
        ties[row['points']].append(row['teamId'])
    if any(len(group) > 1 and (len({original_points[t] for t in group}) > 1
                              or changed_teams.intersection(group)) for group in ties.values()):
        return defer('new_tie_requires_official_sporting_order')
    # Existing equal-points groups retain the provider's sporting tie-break order.
    projected.sort(key=lambda r: (-r['points'], r['rank']))
    for rank, row in enumerate(projected, 1):
        row['rank'] = rank
    doc['standings'] = projected
    doc['standingsSource'] = 'official_plus_verified_finals'
    doc['standingsReconciledAtUTC'] = now.isoformat()
    report.update(status='reconciled', appliedFixtureIds=sorted(additions))
    return was != projected
