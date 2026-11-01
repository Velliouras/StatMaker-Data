"""Reconcile delayed official tables against verified regular-season finals.

The official table stays immutable. Projection is allowed only when every team's
official W/D/L and goals match its chronological prefix of completed fixtures.
This preserves point deductions and rejects incomplete or mixed-phase histories.
Unresolved ties keep the official table until the provider resolves the sporting order.
"""
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import re

FIELDS = ('played', 'wins', 'draws', 'losses', 'goalsFor', 'goalsAgainst')


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
