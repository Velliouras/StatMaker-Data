#!/usr/bin/env python3
"""Fail closed on Domestic provider/competition/team identity corruption.

The App-Ready publisher runs this before expensive read-model generation. Validation
uses repository data only and therefore costs zero provider/API quota.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import canonical_team_identity
import domestic_live_july_pipeline as pipeline
import update_domestic_odds_api_io as odds

ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = ROOT / "data" / "statmaker" / "domestic_enriched" / "index.json"
ODDS_PATH = ROOT / "odds" / "odds_api_io" / "domestic_odds.json"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def validate_cache_competition_identity(errors: list[str]) -> tuple[int, int]:
    index = load_json(INDEX_PATH)
    rows = index.get("leagues", []) if isinstance(index, dict) else []
    if not rows:
        errors.append("Domestic enriched index is empty")
        return 0, 0

    checked_caches = 0
    checked_fixtures = 0
    for row in rows:
        if not isinstance(row, dict):
            errors.append("invalid non-object Domestic enriched index row")
            continue
        code = str(row.get("league_code") or "?").strip().upper()
        season = str(row.get("app_season") or row.get("api_football_season") or "?")
        expected = as_int(row.get("api_football_league_id"))
        cache_rel = str(row.get("cache_path") or "").strip()
        if expected is None or not cache_rel:
            errors.append(f"{code}@{season}: missing provider id/cache path")
            continue

        cache_path = ROOT / cache_rel
        if not cache_path.is_file():
            errors.append(f"{code}@{season}: missing cache {cache_rel}")
            continue
        cache = load_json(cache_path)
        checked_caches += 1

        actual = as_int(cache.get("league_id") if isinstance(cache, dict) else None)
        if actual is not None and actual != expected:
            errors.append(
                f"{code}@{season}: cache league_id={actual} but index expects {expected} ({cache_rel})"
            )

        fixtures = cache.get("fixtures", []) if isinstance(cache, dict) else []
        if not isinstance(fixtures, list):
            errors.append(f"{code}@{season}: cache fixtures is not a list")
            continue

        bad_sources: list[str] = []
        for fixture in fixtures:
            if not isinstance(fixture, dict):
                continue
            checked_fixtures += 1
            source = fixture.get("source_league")
            if not isinstance(source, dict):
                continue
            source_id = as_int(source.get("id"))
            if source_id is not None and source_id != expected:
                bad_sources.append(
                    f"{fixture.get('fixture_id')}:{source_id}:{source.get('name') or '?'}"
                )
                if len(bad_sources) >= 8:
                    break
        if bad_sources:
            errors.append(
                f"{code}@{season}: fixture source league mismatch expected={expected} "
                + "samples=" + ",".join(bad_sources)
            )
    return checked_caches, checked_fixtures


def validate_betting_team_identity(errors: list[str]) -> tuple[int, int]:
    registry_root = pipeline.load_json(pipeline.REGISTRY_PATH, {})
    registry = registry_root.get("leagues", []) if isinstance(registry_root, dict) else []
    indexes = canonical_team_identity.domestic_indexes(odds, pipeline, registry)
    feed = load_json(ODDS_PATH)

    checked_matches = 0
    skipped_schedule_only = 0
    for league in feed.get("leagues", []) if isinstance(feed, dict) else []:
        if not isinstance(league, dict):
            continue
        code = str(league.get("leagueCode") or "").strip().upper()
        index = indexes.get(code)
        for match in league.get("matches", []) or []:
            if not isinstance(match, dict):
                continue
            markets = match.get("markets", []) or []
            if not markets:
                skipped_schedule_only += 1
                continue
            checked_matches += 1

            status = str(match.get("teamMappingStatus") or "").strip().lower()
            if status != "matched" or match.get("usableForStats") is not True:
                errors.append(f"{code}: betting fixture not canonically matched id={match.get('id')}")
                continue
            if index is None:
                errors.append(f"{code}: missing canonical team identity index")
                continue

            provider_home = str(match.get("providerHomeTeam") or "").strip()
            provider_away = str(match.get("providerAwayTeam") or "").strip()
            stored_home = str(match.get("homeTeam") or "").strip()
            stored_away = str(match.get("awayTeam") or "").strip()
            if not provider_home or not provider_away or not stored_home or not stored_away:
                errors.append(f"{code}: blank betting fixture identity id={match.get('id')}")
                continue

            resolved_home, home_policy, home_candidates = index.resolve(provider_home)
            resolved_away, away_policy, away_candidates = index.resolve(provider_away)
            if resolved_home is None:
                errors.append(
                    f"{code}: unresolved home provider={provider_home!r} id={match.get('id')} "
                    f"policy={home_policy} candidates={list(home_candidates)[:4]}"
                )
            elif not canonical_team_identity.equivalent(resolved_home, stored_home):
                errors.append(
                    f"{code}: home identity mismatch provider={provider_home!r} "
                    f"resolved={resolved_home!r} stored={stored_home!r} id={match.get('id')}"
                )

            if resolved_away is None:
                errors.append(
                    f"{code}: unresolved away provider={provider_away!r} id={match.get('id')} "
                    f"policy={away_policy} candidates={list(away_candidates)[:4]}"
                )
            elif not canonical_team_identity.equivalent(resolved_away, stored_away):
                errors.append(
                    f"{code}: away identity mismatch provider={provider_away!r} "
                    f"resolved={resolved_away!r} stored={stored_away!r} id={match.get('id')}"
                )

            if canonical_team_identity.equivalent(stored_home, stored_away):
                errors.append(f"{code}: home/away canonical identity collision id={match.get('id')}")

            canonical_home = str(match.get("canonicalHomeTeam") or "").strip()
            canonical_away = str(match.get("canonicalAwayTeam") or "").strip()
            if canonical_home and not canonical_team_identity.equivalent(canonical_home, stored_home):
                errors.append(f"{code}: canonicalHomeTeam disagrees with homeTeam id={match.get('id')}")
            if canonical_away and not canonical_team_identity.equivalent(canonical_away, stored_away):
                errors.append(f"{code}: canonicalAwayTeam disagrees with awayTeam id={match.get('id')}")

    return checked_matches, skipped_schedule_only


def main() -> int:
    errors: list[str] = []
    checked_caches, checked_fixtures = validate_cache_competition_identity(errors)
    checked_betting, schedule_only = validate_betting_team_identity(errors)

    if errors:
        raise SystemExit(
            "DOMESTIC_PROVIDER_IDENTITY_INVALID\n - " + "\n - ".join(errors[:80])
        )

    print(
        "DOMESTIC_PROVIDER_IDENTITY_OK",
        f"caches={checked_caches}",
        f"fixtures={checked_fixtures}",
        f"bettingMatches={checked_betting}",
        f"scheduleOnlySkipped={schedule_only}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
