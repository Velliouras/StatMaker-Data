#!/usr/bin/env python3
"""Rehydrate eligible Domestic betting matches from the exact provider archive.

This script performs zero provider/API calls. It can repair an existing schedule-only
or unmapped canonical fixture and can also recreate a fixture that was dropped from
the canonical feed entirely when both provider team names resolve uniquely against
the authoritative current roster / StatMaker support aliases. Markets are rebuilt
only from exact archived Odds-API.io payloads; no price is estimated or synthesized.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple

import domestic_live_july_pipeline as pipeline
import domestic_market_expansion_v15
import domestic_odds_expansion
import update_domestic_odds_api_io as odds

ROOT = Path(__file__).resolve().parents[1]
FEED_PATH = ROOT / "odds" / "odds_api_io" / "domestic_odds.json"
ARCHIVE_PATH = ROOT / "odds" / "odds_api_io" / "domestic_provider_markets.json"
INDEX_PATH = ROOT / "data" / "statmaker" / "domestic_enriched" / "index.json"
REPORT_PATH = ROOT / "reports" / "domestic_archive_betting_rehydrate.json"

_GENERIC_TEAM_TOKENS = {
    "club", "fc", "cf", "sc", "ac", "afc", "fk", "bk", "if",
    "ca", "cd", "sd", "ud", "de", "da", "do", "dos", "das", "the",
}


def load(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _variants(name: Any) -> set[str]:
    raw = str(name or "").strip()
    if not raw:
        return set()
    return {
        value for value in (
            odds.normalize_text(raw, drop_suffixes=True),
            odds.simplified_team_name(raw),
        )
        if value
    }


def _historical_aliases(registry: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
    aliases = pipeline.generated_aliases(registry)
    index = load(INDEX_PATH, {})
    for row in index.get("leagues", []) or []:
        if not isinstance(row, dict):
            continue
        code = str(row.get("league_code") or row.get("leagueCode") or "").strip()
        output_path = str(row.get("output_path") or "").strip()
        if not code or not output_path:
            continue
        artifact = load(ROOT / output_path, {})
        canonical_names = {
            str(match.get(key) or "").strip()
            for match in artifact.get("matches", []) or []
            if isinstance(match, dict)
            for key in ("home_team", "away_team")
            if str(match.get(key) or "").strip()
        }
        owners: Dict[str, set[str]] = {}
        for canonical in canonical_names:
            for variant in _variants(canonical):
                owners.setdefault(variant, set()).add(canonical)
        bucket = aliases.setdefault(code, {})
        for variant, candidates in owners.items():
            if len(candidates) == 1:
                bucket.setdefault(variant, next(iter(candidates)))
    return aliases


def _current_roster_variants(registry: Sequence[Dict[str, Any]]) -> Dict[str, set[str]]:
    """Return normalized authoritative target-season roster membership by league."""
    target_season_by_code = {
        str(row.get("leagueCode") or "").strip().upper(): str(
            row.get("targetAppSeason")
            or row.get("app_season")
            or ""
        ).strip()
        for row in registry
        if isinstance(row, dict) and str(row.get("leagueCode") or "").strip()
    }
    roster_payload = load(pipeline.ROSTER_PATH, {})
    result: Dict[str, set[str]] = {}
    for row in roster_payload.get("leagues", []) or []:
        if not isinstance(row, dict):
            continue
        code = str(row.get("leagueCode") or "").strip().upper()
        if not code:
            continue
        target_season = target_season_by_code.get(code, "")
        roster_season = str(row.get("appSeason") or "").strip()
        if target_season and roster_season and roster_season != target_season:
            continue
        variants = result.setdefault(code, set())
        for team in row.get("teams", []) or []:
            variants.update(_variants(team))
    return result


def _is_current_roster_member(name: str, roster_variants: set[str]) -> bool:
    if not roster_variants:
        return True
    return bool(_variants(name).intersection(roster_variants))


def _parse_kickoff(value: Any) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _current_schedule_by_league(
    registry: Sequence[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Load exact target-season API-Football fixture identity with zero API calls."""
    result: Dict[str, List[Dict[str, Any]]] = {}
    for league in registry:
        if not isinstance(league, dict):
            continue
        code = str(league.get("leagueCode") or "").strip().upper()
        target_season = str(
            league.get("targetApiSeason")
            or league.get("season")
            or ""
        ).strip()
        if not code or not target_season:
            continue
        cache_league = dict(league)
        cache_league["season"] = target_season
        cache_league["historyApiSeason"] = target_season
        cache = load(pipeline.stats_fetch.cache_path_for(cache_league), {})
        fixtures: List[Dict[str, Any]] = []
        for fixture in cache.get("fixtures", []) or []:
            if not isinstance(fixture, dict):
                continue
            home = str(fixture.get("home_team") or "").strip()
            away = str(fixture.get("away_team") or "").strip()
            kickoff = str(fixture.get("date") or "").strip()
            if not home or not away or not kickoff:
                continue
            fixtures.append({
                "date": kickoff[:10],
                "kickoff": kickoff,
                "homeTeam": home,
                "awayTeam": away,
            })
        if fixtures:
            result[code] = fixtures
    return result


def _same_team(left: Any, right: Any) -> bool:
    return bool(_variants(left).intersection(_variants(right)))


def _resolve_from_current_schedule(
    code: str,
    archive_match: Dict[str, Any],
    home: str | None,
    away: str | None,
    schedules: Dict[str, List[Dict[str, Any]]],
) -> Tuple[str | None, str | None, bool]:
    """Resolve provider naming only when current fixture identity is unique.

    No fuzzy-name guess is used. Candidate fixtures must match the same date, any
    already-resolved side, and (when available) a close kickoff. If both provider
    sides are unresolved, kickoff identity must reduce the candidates to exactly one.
    """
    date = str(
        archive_match.get("date")
        or archive_match.get("kickoff")
        or ""
    )[:10]
    candidates = [
        row for row in schedules.get(code, [])
        if str(row.get("date") or "") == date
    ]
    if not candidates:
        return home, away, False

    provider_kickoff = _parse_kickoff(archive_match.get("kickoff"))
    compatible: List[Tuple[float, Dict[str, Any]]] = []
    for row in candidates:
        if home and not _same_team(home, row.get("homeTeam")):
            continue
        if away and not _same_team(away, row.get("awayTeam")):
            continue
        fixture_kickoff = _parse_kickoff(row.get("kickoff"))
        delta_minutes = 10_000.0
        if provider_kickoff is not None and fixture_kickoff is not None:
            delta_minutes = abs(
                (provider_kickoff - fixture_kickoff).total_seconds()
            ) / 60.0
            if delta_minutes > 180.0:
                continue
        compatible.append((delta_minutes, row))

    if not compatible:
        return home, away, False

    if home is None and away is None:
        close = [item for item in compatible if item[0] <= 90.0]
        if len(close) != 1:
            return home, away, False
        chosen = close[0][1]
    else:
        if len(compatible) != 1:
            return home, away, False
        chosen = compatible[0][1]

    return (
        home or str(chosen.get("homeTeam") or "").strip() or None,
        away or str(chosen.get("awayTeam") or "").strip() or None,
        True,
    )


def _meaningful(text: str) -> set[str]:
    return {
        token for token in text.split()
        if token not in _GENERIC_TEAM_TOKENS and (len(token) >= 4 or token.isdigit())
    }


def _resolve_team(name: Any, code: str, aliases: Dict[str, Dict[str, str]]) -> Tuple[str | None, str]:
    raw = str(name or "").strip()
    normalized = odds.normalize_text(raw, drop_suffixes=True)
    simplified = odds.simplified_team_name(normalized)
    bucket = aliases.get(code, {})

    for candidate in dict.fromkeys([normalized, simplified]):
        canonical = bucket.get(candidate)
        if canonical:
            return str(canonical), "exact_historical_alias"

    provider_tokens = set(simplified.split())
    provider_meaningful = _meaningful(simplified)
    candidates: Dict[str, set[str]] = {}
    if provider_tokens and provider_meaningful:
        for alias_key, canonical in bucket.items():
            alias_text = odds.simplified_team_name(alias_key)
            alias_tokens = set(alias_text.split())
            if not alias_tokens or not provider_meaningful.intersection(_meaningful(alias_text)):
                continue
            if alias_tokens.issubset(provider_tokens) or provider_tokens.issubset(alias_tokens):
                candidates.setdefault(str(canonical), set()).add(alias_key)

    if len(candidates) == 1:
        return next(iter(candidates)), "unique_historical_token_containment"
    return None, "unresolved_or_ambiguous"


def _match_key(match: Dict[str, Any]) -> Tuple[str, str, str]:
    return (
        str(match.get("date") or match.get("kickoff") or "")[:10],
        odds.normalize_text(match.get("providerHomeTeam") or match.get("homeTeam"), drop_suffixes=True),
        odds.normalize_text(match.get("providerAwayTeam") or match.get("awayTeam"), drop_suffixes=True),
    )


def _archive_maps(archive_league: Dict[str, Any]) -> tuple[Dict[str, Dict[str, Any]], Dict[Tuple[str, str, str], Dict[str, Any]]]:
    matches = [row for row in archive_league.get("matches", []) or [] if isinstance(row, dict)]
    by_id = {str(row.get("id") or ""): row for row in matches if str(row.get("id") or "")}
    by_key = {_match_key(row): row for row in matches}
    return by_id, by_key


def _normalize_archived_markets(
    archive_match: Dict[str, Any],
    home: str,
    away: str,
    debug: Dict[str, Any],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for payload in archive_match.get("providerMarkets", []) or []:
        if not isinstance(payload, dict) or payload.get("exactProviderPayload") is not True:
            continue
        bookmaker = str(payload.get("bookmaker") or "").strip()
        raw_market = payload.get("market")
        if not bookmaker or not isinstance(raw_market, dict):
            continue
        for row in odds.normalize_market(raw_market, bookmaker, home, away, debug):
            if row.get("exactBookmakerOdds") is True:
                rows.append(row)
    return odds.dedupe_markets(rows)


def rebuild(feed: Dict[str, Any], archive: Dict[str, Any], registry: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    domestic_odds_expansion.install(odds, pipeline)
    domestic_market_expansion_v15.install(odds, pipeline)
    aliases = _historical_aliases(registry)
    current_rosters = _current_roster_variants(registry)
    current_schedules = _current_schedule_by_league(registry)
    archive_leagues = {
        str(league.get("leagueCode") or ""): league
        for league in archive.get("leagues", []) or []
        if isinstance(league, dict)
    }
    today = pipeline.today_utc().isoformat()
    debug: Dict[str, Any] = {"warnings": []}
    report: Dict[str, Any] = {
        "generatedAt": pipeline.now_utc(),
        "source": "exact Odds-API.io provider archive",
        "providerCalls": 0,
        "syntheticOdds": False,
        "leaguesVisited": 0,
        "archiveMatchesVisited": 0,
        "matchesConsidered": 0,
        "matchesWithArchive": 0,
        "matchesRehydrated": 0,
        "matchesResolvedByCurrentSchedule": 0,
        "matchesCreatedFromArchive": 0,
        "matchesRepairedExisting": 0,
        "healthyMatchesPreserved": 0,
        "expiredArchiveMatchesSkipped": 0,
        "archiveMatchesWithoutCanonicalMarkets": 0,
        "marketsRehydrated": 0,
        "unresolvedHistoricalTeams": [],
        "rejectedOutsideCurrentRoster": [],
        "rehydratedMatches": [],
    }

    for league in feed.get("leagues", []) or []:
        if not isinstance(league, dict):
            continue
        code = str(league.get("leagueCode") or "").strip()
        archive_league = archive_leagues.get(code)
        if not code or not archive_league:
            continue
        report["leaguesVisited"] += 1

        existing_matches = [
            row for row in league.get("matches", []) or []
            if isinstance(row, dict)
        ]
        league["matches"] = existing_matches
        existing_by_id = {
            str(row.get("id") or ""): row
            for row in existing_matches
            if str(row.get("id") or "")
        }
        existing_by_provider_key = {
            _match_key(row): row
            for row in existing_matches
        }
        existing_by_canonical_key = {
            (
                str(row.get("date") or row.get("kickoff") or "")[:10],
                odds.normalize_text(
                    row.get("canonicalHomeTeam") or row.get("homeTeam"),
                    drop_suffixes=True,
                ),
                odds.normalize_text(
                    row.get("canonicalAwayTeam") or row.get("awayTeam"),
                    drop_suffixes=True,
                ),
            ): row
            for row in existing_matches
        }

        archive_matches = [
            row for row in archive_league.get("matches", []) or []
            if isinstance(row, dict)
        ]
        for archive_match in archive_matches:
            report["archiveMatchesVisited"] += 1
            date = str(
                archive_match.get("date")
                or archive_match.get("kickoff")
                or ""
            )[:10]
            if date and date < today:
                report["expiredArchiveMatchesSkipped"] += 1
                continue

            archive_id = str(archive_match.get("id") or "")
            existing = (
                existing_by_id.get(archive_id)
                or existing_by_provider_key.get(_match_key(archive_match))
            )
            if (
                existing is not None
                and existing.get("usableForStats") is True
                and bool(existing.get("markets") or [])
            ):
                report["healthyMatchesPreserved"] += 1
                continue

            report["matchesConsidered"] += 1
            report["matchesWithArchive"] += 1
            provider_home = (
                archive_match.get("providerHomeTeam")
                or (existing or {}).get("providerHomeTeam")
                or (existing or {}).get("homeTeam")
            )
            provider_away = (
                archive_match.get("providerAwayTeam")
                or (existing or {}).get("providerAwayTeam")
                or (existing or {}).get("awayTeam")
            )
            home, home_policy = _resolve_team(provider_home, code, aliases)
            away, away_policy = _resolve_team(provider_away, code, aliases)
            if not home or not away:
                before_home, before_away = home, away
                home, away, schedule_resolved = _resolve_from_current_schedule(
                    code,
                    archive_match,
                    home,
                    away,
                    current_schedules,
                )
                if schedule_resolved:
                    report["matchesResolvedByCurrentSchedule"] += 1
                    if before_home is None and home is not None:
                        home_policy = "unique_current_schedule_identity"
                    if before_away is None and away is not None:
                        away_policy = "unique_current_schedule_identity"
            if not home or not away:
                report["unresolvedHistoricalTeams"].append({
                    "leagueCode": code,
                    "matchId": archive_id,
                    "providerHomeTeam": provider_home,
                    "providerAwayTeam": provider_away,
                    "homeResolved": home,
                    "awayResolved": away,
                })
                continue

            roster_variants = current_rosters.get(code, set())
            if roster_variants and (
                not _is_current_roster_member(home, roster_variants)
                or not _is_current_roster_member(away, roster_variants)
            ):
                report["rejectedOutsideCurrentRoster"].append({
                    "leagueCode": code,
                    "matchId": archive_id,
                    "providerHomeTeam": provider_home,
                    "providerAwayTeam": provider_away,
                    "homeResolved": home,
                    "awayResolved": away,
                })
                continue

            canonical_key = (
                date,
                odds.normalize_text(home, drop_suffixes=True),
                odds.normalize_text(away, drop_suffixes=True),
            )
            if existing is None:
                existing = existing_by_canonical_key.get(canonical_key)
                if (
                    existing is not None
                    and existing.get("usableForStats") is True
                    and bool(existing.get("markets") or [])
                ):
                    report["healthyMatchesPreserved"] += 1
                    continue

            markets = _normalize_archived_markets(
                archive_match,
                home,
                away,
                debug,
            )
            if not markets:
                report["archiveMatchesWithoutCanonicalMarkets"] += 1
                continue

            payload = {
                "providerHomeTeam": str(provider_home or "").strip(),
                "providerAwayTeam": str(provider_away or "").strip(),
                "homeTeam": home,
                "awayTeam": away,
                "canonicalHomeTeam": home,
                "canonicalAwayTeam": away,
                "teamMappingStatus": "matched",
                "usableForStats": True,
                "markets": markets,
                "scheduleOnly": False,
                "scheduleVerified": True,
                "bettingRehydratedFromArchive": True,
            }

            created = existing is None
            if created:
                existing = {
                    "id": archive_match.get("id"),
                    "date": archive_match.get("date") or date,
                    "kickoff": archive_match.get("kickoff"),
                    **payload,
                }
                existing_matches.append(existing)
                if archive_id:
                    existing_by_id[archive_id] = existing
                existing_by_provider_key[_match_key(archive_match)] = existing
                existing_by_canonical_key[canonical_key] = existing
                report["matchesCreatedFromArchive"] += 1
            else:
                existing.update(payload)
                report["matchesRepairedExisting"] += 1

            report["matchesRehydrated"] += 1
            report["marketsRehydrated"] += len(markets)
            report["rehydratedMatches"].append({
                "leagueCode": code,
                "matchId": archive_id,
                "homeTeam": home,
                "awayTeam": away,
                "homeMappingPolicy": home_policy,
                "awayMappingPolicy": away_policy,
                "marketCount": len(markets),
                "createdFromArchive": created,
            })

        existing_matches.sort(
            key=lambda row: (
                str(row.get("kickoff") or row.get("date") or ""),
                str(row.get("id") or ""),
            )
        )

    # Keep diagnostics compact and deterministic.
    report["unresolvedHistoricalTeams"] = report["unresolvedHistoricalTeams"][:100]
    report["rejectedOutsideCurrentRoster"] = report["rejectedOutsideCurrentRoster"][:100]
    report["normalizationWarnings"] = debug.get("warnings", [])[:100]
    if report["matchesRehydrated"]:
        feed["generatedAt"] = report["generatedAt"]
    feed.setdefault("debug", {})["archiveBettingRehydrate"] = report
    if hasattr(odds, "emitted_market_counts"):
        feed.setdefault("debug", {})["emittedMarketCounts"] = odds.emitted_market_counts(feed)
    return report

def main() -> int:
    if not FEED_PATH.exists() or not ARCHIVE_PATH.exists():
        print("ERROR: Domestic feed/provider archive is missing.")
        return 2
    registry_payload = load(pipeline.REGISTRY_PATH, {})
    registry = registry_payload.get("leagues", []) if isinstance(registry_payload, dict) else []
    if not registry:
        print("ERROR: Domestic registry is empty.")
        return 3

    feed = load(FEED_PATH, {})
    archive = load(ARCHIVE_PATH, {})
    report = rebuild(feed, archive, registry)
    save(FEED_PATH, feed)
    save(REPORT_PATH, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
