#!/usr/bin/env python3
"""Remove only orphaned historical rows in a checkpoint-generated App-Ready stats DB.

The emulator's historical import is an upsert, not a snapshot replacement. If
provider fixture discovery removes/reschedules a match, its old match_key can
remain in the checkpoint and violate the existing immutable-bundle scope guard.

Compare exact canonical match keys for ONE requested scope. Refuse to delete
anything if the canonical artifact is incomplete, ambiguous, has duplicate
keys or if any canonical key is missing from SQLite. Never invent matches or
change scores, odds, recommendations, schemas, or published history.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def db_season(app_season: str) -> str:
    season = str(app_season).strip()
    return {
        "2025-2026": "2526", "2025 - 2026": "2526", "25/26": "2526",
        "2024-2025": "2425", "2024 - 2025": "2425", "24/25": "2425",
        "2023-2024": "2324", "2023 - 2024": "2324", "23/24": "2324",
    }.get(season, season or "2526")


def canonical_key(season: str, division: str, match: dict) -> str:
    date = str(match.get("date_utc") or match.get("date") or "")[:10].strip()
    home = str(match.get("home_team") or match.get("homeTeam") or "").strip()
    away = str(match.get("away_team") or match.get("awayTeam") or "").strip()
    if not date or not home or not away:
        raise ValueError(f"Canonical fixture missing date/team fields: {match.get('fixture_id')}")
    # Exactly DomesticApiArtifactImporter.buildKey() in StatMaker/main.
    return "|".join(part.strip().lower() for part in (season, division, date, home, away))


def reconcile(
    database: Path,
    index_path: Path,
    repo_root: Path,
    league_code: str = "F1",
    app_season: str = "2025-2026",
) -> tuple[int, int]:
    index = json.loads(index_path.read_text(encoding="utf-8"))
    scope = [
        row for row in index.get("leagues", [])
        if row.get("league_code") == league_code and row.get("app_season") == app_season
    ]
    if len(scope) != 1:
        raise ValueError(f"Expected one canonical league scope {league_code}@{app_season}: {len(scope)}")
    row = scope[0]
    source_path = str(row.get("output_path") or "")
    source_file = (repo_root / source_path).resolve()
    if not source_path.startswith("data/statmaker/domestic_enriched/") or not source_file.is_relative_to(repo_root.resolve()):
        raise ValueError(f"Unsafe canonical source file {source_path!r}")
    payload = json.loads(source_file.read_text(encoding="utf-8"))
    matches = payload.get("matches")
    expected_count = int(row.get("completed_fixtures") or 0)
    if not isinstance(matches, list) or expected_count <= 0 or len(matches) != expected_count:
        raise ValueError(
            f"Canonical {league_code}@{app_season} incomplete: "
            f"index={expected_count} source={len(matches) if isinstance(matches, list) else 'invalid'}"
        )
    season = db_season(app_season)
    expected_keys = [canonical_key(season, league_code, match) for match in matches]
    if len(set(expected_keys)) != expected_count:
        raise ValueError(f"Duplicate canonical fixture match keys for {league_code}@{season}")
    expected = set(expected_keys)

    db = sqlite3.connect(database)
    try:
        db.execute("BEGIN IMMEDIATE")
        actual = {
            entry[0]: (entry[1], entry[2], entry[3])
            for entry in db.execute(
                "SELECT match_key, date_text, home_team, away_team "
                "FROM matches WHERE season=? AND division=?",
                (season, league_code),
            )
        }
        if len(actual) != db.execute(
            "SELECT COUNT(*) FROM matches WHERE season=? AND division=?",
            (season, league_code),
        ).fetchone()[0]:
            raise ValueError(f"Duplicate SQLite match keys in {league_code}@{season}")
        missing = sorted(expected - actual.keys())
        orphaned = sorted(actual.keys() - expected)
        if missing:
            raise ValueError(
                f"Refusing to prune {league_code}@{season}: "
                f"{len(missing)} canonical matches MISSING from DB, first={missing[:3]}"
            )
        # Every key being removed is proven absent from the canonical fixture set.
        if orphaned:
            db.executemany(
                "DELETE FROM matches WHERE season=? AND division=? AND match_key=?",
                [(season, league_code, key) for key in orphaned],
            )
        remaining = {entry[0] for entry in db.execute(
            "SELECT match_key FROM matches WHERE season=? AND division=?",
            (season, league_code),
        )}
        if remaining != expected:
            raise ValueError(f"Stats DB mismatch remains for {league_code}@{season}")
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("SQLite integrity check failed after scope reconciliation")
        db.commit()
        for key in orphaned[:15]:
            print("APP_READY_STATS_ORPHAN_PRUNED", key, actual[key], flush=True)
        print(
            "APP_READY_STATS_SCOPE_RECONCILED",
            f"scope={league_code}@{season}",
            f"canonical={expected_count}",
            f"before={len(actual)}",
            f"removed={len(orphaned)}",
            f"after={len(remaining)}",
            flush=True,
        )
        return len(orphaned), len(remaining)
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("index", type=Path)
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--league-code", default="F1")
    parser.add_argument("--app-season", default="2025-2026")
    args = parser.parse_args()
    reconcile(args.database, args.index, args.repo_root, args.league_code, args.app_season)
