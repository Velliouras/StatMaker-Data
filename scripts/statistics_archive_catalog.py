"""Keep independently verified historical statistics outside the rolling live index.

No provider requests; the active index always wins. Only complete, uniquely
identified API-Football archives may extend the statistics-only catalogue.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from scripts.reconcile_app_ready_stats_scope import db_season
from scripts.validate_app_ready_archived_stats import canonical_archived_scope, normalize_code


def statistics_catalog(root: Path, index: dict) -> dict:
    root = root.resolve()
    active = list(index.get('leagues') or [])
    scopes = {(db_season(row.get('app_season') or ''), normalize_code(row.get('league_code')))
              for row in active}
    archive_dir = root / 'data/statmaker/domestic_enriched'
    archived = []
    for path in sorted(archive_dir.glob('*_[0-9][0-9][0-9][0-9].json')):
        payload = json.loads(path.read_text(encoding='utf-8'))
        competition = payload.get('competition') or {}
        app_season = str(competition.get('app_season') or '')
        code = normalize_code(competition.get('league_code'))
        season = db_season(app_season)
        if (season, code) in scopes or not code or not payload.get('matches'):
            continue
        # Never reinterpret a fixture's calendar year as a competition season.
        if not re.fullmatch(r'20\d{2}(?:-20\d{2})?', app_season):
            raise ValueError(f'Unknown archived competition season: {path.name}')
        verified_path, verified, keys = canonical_archived_scope(
            root, season, code, app_season=app_season)
        if verified_path != path:
            raise ValueError(f'Ambiguous statistics archive: {path.name}')
        for fixture in verified['matches']:
            if fixture.get('status') not in ('FT', 'AET', 'PEN'):
                raise ValueError(f'Unfinished archived fixture: {path.name}')
            for side in ('home', 'away'):
                value = fixture.get(side + '_goals', fixture.get(side + '_score',
                    fixture.get('fthg' if side == 'home' else 'ftag')))
                if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or int(value) != value:
                    raise ValueError(f'Missing verified archived score: {path.name}')
        archived.append(dict(competition, league_code=code, app_season=app_season,
            output_path=path.relative_to(root).as_posix(), completed_fixtures=len(keys),
            fixtures_with_score=len(keys), bb_ready_candidate=(verified.get('readiness') or {}).get('bb_ready_candidate', False),
            statistics_archived=True))
        scopes.add((season, code))
    return dict(index, leagues=active + archived)
