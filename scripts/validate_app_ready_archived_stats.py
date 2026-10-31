#!/usr/bin/env python3
"""Validate retired historical scopes against immutable repository fixture identities.

The rolling Domestic index deliberately drops prior-season support after its
grace period, but an upserted emulator checkpoint can legitimately retain those
matches. Preserve them ONLY if the archived API-Football artifact proves every
SQLite match key. Never delete history, silently bless extra scopes or weaken
current-index checks.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from scripts.reconcile_app_ready_stats_scope import canonical_key


def normalize_code(value: object) -> str:
    code = str(value or "").strip().upper()
    return "ROU" if code == "ROM" else code


def historical_app_season(db_season: str) -> tuple[int, str]:
    match = re.fullmatch(r"(\d{2})(\d{2})", str(db_season))
    if not match:
        raise ValueError(f"Unexpected non-archival stats scope season {db_season!r}")
    first, second = (2000 + int(part) for part in match.groups())
    if second != first + 1 or not 2020 <= first <= 2090:
        raise ValueError(f"Invalid archival season {db_season!r}")
    return first, f"{first}-{second}"


def archived_candidates(repo_root: Path, years: set[int]) -> dict:
    archive_root = (Path(repo_root) / "data/statmaker/domestic_enriched").resolve()
    if not archive_root.is_dir():
        raise ValueError(f"Missing canonical Domestic archive directory {archive_root}")
    candidates = {}
    for year in sorted(years):
        for path in sorted(archive_root.glob(f"*_{year}.json")):
            if not path.resolve().is_relative_to(archive_root):
                raise ValueError(f"Unsafe canonical archive path {path}")
            payload = json.loads(path.read_text(encoding="utf-8"))
            competition = payload.get("competition") or {}
            scope = (str(competition.get("app_season") or ""), normalize_code(competition.get("league_code")))
            if scope[0] == f"{year}-{year + 1}":
                candidates.setdefault(scope, []).append((path, payload))
    return candidates


def canonical_archived_scope(repo_root: Path, season: str, code: str, *, app_season=None, _candidates=None) -> tuple[Path, dict, list[str]]:
    """Resolve one retired scope with the same identity proof used at publication."""
    if app_season is None:
        year, app_season = historical_app_season(season)
    else:
        label = re.fullmatch(r"(20\d{2})\s*-\s*(20\d{2})", str(app_season))
        if not label or int(label[2]) != int(label[1]) + 1:
            raise ValueError(f"Invalid canonical archive season {app_season!r}")
        year, app_season = int(label[1]), f"{label[1]}-{label[2]}"
        if season not in (app_season, label[1][-2:] + label[2][-2:]):
            raise ValueError(f"Archive/SQLite season mismatch {app_season!r}/{season!r}")
    code = normalize_code(code)
    catalog = _candidates if _candidates is not None else archived_candidates(repo_root, {year})
    candidates = catalog.get((app_season, code), [])
    if len(candidates) != 1:
        raise ValueError(f"Unverified archived scope {code}@{season}: matching source artifacts={len(candidates)}")
    path, payload = candidates[0]
    competition = payload["competition"]
    source = payload.get("source") or {}
    if (str(source.get("provider") or "").strip().lower() != "api-football"
            or str(competition.get("api_football_season") or "") != str(year)):
        raise ValueError(f"Incorrect archived provider/season {path.name}")
    matches = payload.get("matches")
    completed = (payload.get("readiness") or {}).get("completed_fixtures")
    if not isinstance(matches, list) or not matches or completed != len(matches):
        raise ValueError(f"Incomplete archived fixture scope {path.name}")
    source_keys, fixture_ids = [], []
    for match in matches:
        if (not isinstance(match, dict)
                or str(match.get("app_season") or app_season) != app_season
                or normalize_code(match.get("league_code") or code) != code):
            raise ValueError(f"Wrong canonical archived fixture scope {path.name}")
        source_keys.append(canonical_key(season, code, match))
        fixture_ids.append(str(match.get("fixture_id") or ""))
    if (len(set(source_keys)) != len(matches)
            or not all(fixture_ids) or len(set(fixture_ids)) != len(matches)):
        raise ValueError(f"Duplicate or unidentified canonical fixtures {path.name}")
    return path, payload, source_keys


def validated_archived_scope_counts(
    database: Path,
    canonical_index: dict,
    repo_root: Path,
    actual: dict[tuple[str, str], int],
    expected: dict[tuple[str, str], int],
) -> dict[tuple[str, str], int]:
    """Return verified extras to extend the builder's strict scope expectation.

    The active canonical index remains authoritative: no archived artifact may
    override it. A retired scope is accepted only if precisely ONE versioned
    repository archive matches its identity AND its complete SQLite key set.
    """
    extras = sorted(set(actual) - set(expected))
    if not extras:
        return {}
    if not isinstance(canonical_index.get("leagues"), list):
        raise ValueError("Invalid current canonical Domestic index")
    candidates = archived_candidates(repo_root, {historical_app_season(season)[0] for season, _ in extras})
    accepted: dict[tuple[str, str], int] = {}
    connection = sqlite3.connect(f"file:{Path(database).resolve()}?mode=ro", uri=True)
    try:
        for season, code in extras:
            path, payload, source_keys = canonical_archived_scope(repo_root, season, code, _candidates=candidates)

            db_rows = [
                (str(match_key), str(date_text or "")[:10], str(home or ""), str(away or ""))
                for db_season, division, match_key, date_text, home, away in connection.execute(
                    "SELECT season, division, match_key, date_text, home_team, away_team "
                    "FROM matches WHERE season=?", (season,)
                )
                if normalize_code(division) == code
            ]
            keys = [row[0] for row in db_rows]
            if (len(keys) != actual[(season, code)]
                    or len(set(keys)) != len(keys)
                    or set(keys) != set(source_keys)):
                raise ValueError(
                    f"Archived scope exact-key mismatch {code}@{season}: "
                    f"db={len(keys)} archive={len(source_keys)} "
                    f"missing={len(set(source_keys) - set(keys))} "
                    f"orphaned={len(set(keys) - set(source_keys))}"
                )
            for match_key, date, home, away in db_rows:
                if match_key != "|".join((season, code.lower(), date, home.strip().lower(), away.strip().lower())):
                    raise ValueError(f"Inconsistent SQLite fixture identity {match_key}")
            accepted[(season, code)] = len(source_keys)
            print(
                "APP_READY_ARCHIVED_STATS_SCOPE_VERIFIED",
                f"scope={code}@{season}",
                f"matches={len(source_keys)}",
                f"source={path.name}",
                flush=True,
            )
    finally:
        connection.close()
    return accepted
