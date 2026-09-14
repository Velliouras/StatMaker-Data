#!/usr/bin/env python3
"""Prune stale Domestic rows from a resumable App-Ready stats DB.

The checkpoint DB is a reusable cache, not an authoritative source. Repository Domestic
artifacts are full league/season snapshots. When a fixture is corrected/removed upstream,
a restored checkpoint may still contain the old row because the Android importer upserts
current rows but does not delete rows that disappeared.

This repair is deliberately fail-closed:
- it may delete stale rows that are absent from the current canonical artifact;
- it never fabricates or inserts a missing canonical row;
- if a canonical row is missing from the checkpoint, the fast-recovery path aborts and a
  normal producer run is required.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


def db_season(app_season: str) -> str:
    value = str(app_season or "").strip()
    aliases = {
        "2025-2026": "2526",
        "2025 - 2026": "2526",
        "25/26": "2526",
        "2024-2025": "2425",
        "2024 - 2025": "2425",
        "24/25": "2425",
        "2023-2024": "2324",
        "2023 - 2024": "2324",
        "23/24": "2324",
    }
    return aliases.get(value, value or "2526")


def score(match: dict, *keys: str):
    for key in keys:
        value = match.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    score_root = match.get("score")
    if isinstance(score_root, dict):
        fulltime = score_root.get("fulltime")
        if isinstance(fulltime, dict):
            side = "home" if "home" in keys[0] else "away"
            value = fulltime.get(side)
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    pass
    return None


def canonical_rows(artifact: dict) -> set[tuple[str, str, str]]:
    rows: set[tuple[str, str, str]] = set()
    for match in artifact.get("matches", []) or []:
        if not isinstance(match, dict):
            continue
        date = str(match.get("date_utc") or match.get("date") or "").strip()[:10]
        home = str(match.get("home_team") or match.get("homeTeam") or "").strip()
        away = str(match.get("away_team") or match.get("awayTeam") or "").strip()
        home_goals = score(match, "home_goals", "home_score", "fthg")
        away_goals = score(match, "away_goals", "away_score", "ftag")
        if not date or not home or not away or home_goals is None or away_goals is None:
            continue
        rows.add((date, home, away))
    return rows


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: reconcile_app_ready_stats_checkpoint.py DB_PATH DOMESTIC_INDEX")

    db_path = Path(sys.argv[1])
    index_path = Path(sys.argv[2])
    repo_root = index_path.resolve().parents[3]

    if not db_path.is_file():
        raise SystemExit(f"missing checkpoint stats DB: {db_path}")
    index = json.loads(index_path.read_text(encoding="utf-8-sig"))
    leagues = index.get("leagues", []) if isinstance(index, dict) else []
    if not isinstance(leagues, list) or not leagues:
        raise SystemExit("invalid/empty Domestic enriched index")

    db = sqlite3.connect(str(db_path))
    deleted_total = 0
    checked = 0
    try:
        db.execute("BEGIN IMMEDIATE")
        for row in leagues:
            if not isinstance(row, dict):
                continue
            completed = int(row.get("completed_fixtures", 0) or 0)
            if completed <= 0:
                continue
            code = str(row.get("league_code") or row.get("leagueCode") or "").strip().upper()
            season = db_season(row.get("app_season") or row.get("appSeason") or row.get("season"))
            output_path = str(row.get("output_path") or "").strip()
            if not code or not season or not output_path:
                raise SystemExit(f"invalid canonical scope code={code!r} season={season!r} path={output_path!r}")

            artifact_path = repo_root / output_path
            if not artifact_path.is_file():
                raise SystemExit(f"missing canonical Domestic artifact: {output_path}")
            artifact = json.loads(artifact_path.read_text(encoding="utf-8-sig"))
            canonical = canonical_rows(artifact)
            if len(canonical) != completed:
                raise SystemExit(
                    f"canonical artifact/index mismatch {code}@{season}: "
                    f"rows={len(canonical)} completed={completed}"
                )

            actual_rows = db.execute(
                "SELECT id,date_text,home_team,away_team FROM matches WHERE season=? AND division=?",
                (season, code),
            ).fetchall()
            actual = {(str(date), str(home), str(away)): int(row_id)
                      for row_id, date, home, away in actual_rows}

            missing = sorted(canonical - set(actual))
            if missing:
                sample = "; ".join("|".join(item) for item in missing[:5])
                raise SystemExit(
                    f"fast recovery cannot fabricate missing rows {code}@{season}: "
                    f"missing={len(missing)} sample={sample}"
                )

            stale = sorted(set(actual) - canonical)
            for identity in stale:
                db.execute("DELETE FROM matches WHERE id=?", (actual[identity],))
            if stale:
                print(
                    "APP_READY_CHECKPOINT_PRUNED",
                    f"scope={code}@{season}",
                    f"deleted={len(stale)}",
                    "sample=" + ";".join("|".join(item) for item in stale[:5]),
                )
            deleted_total += len(stale)
            checked += 1

            after = int(db.execute(
                "SELECT COUNT(*) FROM matches WHERE season=? AND division=?",
                (season, code),
            ).fetchone()[0])
            if after != completed:
                raise SystemExit(
                    f"checkpoint reconciliation failed {code}@{season}: "
                    f"after={after} canonical={completed}"
                )

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    print(
        "APP_READY_CHECKPOINT_RECONCILED",
        f"scopes={checked}",
        f"deletedStaleRows={deleted_total}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
