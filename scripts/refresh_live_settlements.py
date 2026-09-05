#!/usr/bin/env python3
"""Publish a quota-safe rolling settlement feed for canonical StatMaker recommendations.

The Android app never calls API-Football directly. This producer runs in StatMaker-Data and:
- polls the API-Football scoreboard for the current/previous UTC day (two base requests/run),
- derives the current + previous canonical one-per-match recommendation universes directly from
  the already-checked-out App-Ready betting bundles,
- keeps only completed fixtures that can settle one of those canonical recommendations,
- calls /fixtures/statistics only when the recommendation actually requires a detailed metric,
- retries only the missing required metric family with backoff, and
- preserves a rolling repository feed so UAT/PROD settle from the same result source.

This design deliberately avoids a second recommendation engine and does not add a new workflow.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import sys
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import api_football_daily_quota_guard as quota_guard
import api_football_fetch_fixture_stats as stats_fetch

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "data" / "statmaker" / "domestic_live_july_registry.json"
APP_READY_DIR = ROOT / "data" / "statmaker" / "app_ready"
APP_READY_MANIFEST = APP_READY_DIR / "update_manifest.json"
CACHE_PATH = ROOT / "data" / "api_football" / "live_settlement_cache.json"
FEED_PATH = ROOT / "data" / "statmaker" / "live_settlements.json"

SCHEMA_VERSION = 2
DEFAULT_MAX_REQUESTS = 80
RETENTION_DAYS = 14
MAX_STATS_ATTEMPTS = 6
COMPLETED = {"FT", "AET", "PEN"}

# Retry delays after each failed detailed-stat attempt. Targeted calls are cheap enough to keep
# trying for several hours, but never hammer the API every 15 minutes indefinitely.
STATS_RETRY_DELAYS_MINUTES = (0, 15, 45, 120, 360, 720)

# The settlement contract is intentionally keyed by the normalized sub-market identity persisted
# in the App-Ready DB. Score/half-time families require no /fixtures/statistics request.
SUBMARKET_REQUIREMENT: Dict[str, str] = {
    # Full-time score only.
    "RESULT_1X2": "score",
    "RESULT_DOUBLE_CHANCE": "score",
    "RESULT_DNB": "score",
    "RESULT_ASIAN_HANDICAP": "score",
    "RESULT_WINNING_MARGIN": "score",
    "RESULT_CORRECT_SCORE": "score",
    "BTTS": "score",
    "FULL_TIME_MATCH_TOTAL": "score",
    "ASIAN_MATCH_GOALS_TOTAL": "score",
    "HOME_TEAM_TOTAL": "score",
    "AWAY_TEAM_TOTAL": "score",
    "TEAM_TOTAL": "score",
    "GOALS_ODD_EVEN": "score",
    "GOAL_BANDS": "score",
    "BTTS_GOALS_COMBO": "score",
    "RESULT_GOALS_COMBO": "score",
    # Score + half-time score only.
    "HT_RESULT_1X2": "half_time",
    "HT_RESULT_DOUBLE_CHANCE": "half_time",
    "HT_RESULT_ASIAN_HANDICAP": "half_time",
    "RESULT_HT_FT": "half_time",
    "FIRST_HALF_MATCH_TOTAL": "half_time",
    "ASIAN_FIRST_HALF_GOALS_TOTAL": "half_time",
    "HOME_TEAM_1H_TOTAL": "half_time",
    "AWAY_TEAM_1H_TOTAL": "half_time",
    "TEAM_1H_TOTAL": "half_time",
    "SECOND_HALF_MATCH_TOTAL": "half_time",
    "HOME_TEAM_2H_TOTAL": "half_time",
    "AWAY_TEAM_2H_TOTAL": "half_time",
    "TEAM_2H_TOTAL": "half_time",
    "GOAL_BOTH_HALVES": "half_time",
    "HOME_SCORE_BOTH_HALVES": "half_time",
    "AWAY_SCORE_BOTH_HALVES": "half_time",
    "TEAM_SCORE_BOTH_HALVES": "half_time",
    # Detailed fixture statistics.
    "MATCH_CORNERS_TOTAL": "corners",
    "ASIAN_MATCH_CORNERS_TOTAL": "corners",
    "ASIAN_CORNER_HANDICAP": "corners",
    "HOME_TEAM_CORNERS": "corners",
    "AWAY_TEAM_CORNERS": "corners",
    "TEAM_CORNERS": "corners",
    "CORNER_RESULT_1X2": "corners",
    "CORNER_HANDICAP": "corners",
    "MATCH_CARDS_TOTAL": "cards",
    "HOME_TEAM_CARDS": "cards",
    "AWAY_TEAM_CARDS": "cards",
    "TEAM_CARDS": "cards",
    "MATCH_YELLOW_CARDS_TOTAL": "yellow_cards",
    "HOME_TEAM_YELLOW_CARDS": "yellow_cards",
    "AWAY_TEAM_YELLOW_CARDS": "yellow_cards",
    "TEAM_YELLOW_CARDS": "yellow_cards",
    "MATCH_RED_CARD": "red_cards",
    "MATCH_SHOTS_TOTAL": "shots",
    "HOME_TEAM_SHOTS": "shots",
    "AWAY_TEAM_SHOTS": "shots",
    "TEAM_SHOTS": "shots",
    "SHOTS_RESULT_1X2": "shots",
    "MATCH_SOT_TOTAL": "shots_on_target",
    "HOME_TEAM_SOT": "shots_on_target",
    "AWAY_TEAM_SOT": "shots_on_target",
    "TEAM_SOT": "shots_on_target",
    "SOT_RESULT_1X2": "shots_on_target",
    "MATCH_FOULS_TOTAL": "fouls",
    "HOME_TEAM_FOULS": "fouls",
    "AWAY_TEAM_FOULS": "fouls",
    "TEAM_FOULS": "fouls",
}

REQUIRED_FIELDS: Dict[str, Tuple[str, ...]] = {
    "corners": ("HC", "AC"),
    "cards": ("HY", "AY", "HR", "AR"),
    "yellow_cards": ("HY", "AY"),
    "red_cards": ("HR", "AR"),
    "shots": ("HS", "AS"),
    "shots_on_target": ("HST", "AST"),
    "fouls": ("HF", "AF"),
}

# Verified API-Football competition ids already used by StatMaker's UEFA capability audit.
UEFA_PROVIDER_ROWS: Dict[int, Dict[str, Any]] = {
    2: {
        "leagueCode": "CL",
        "country": "Europe",
        "competition": "UEFA Champions League",
        "display_name": "UEFA Champions League",
        "lifecycle": "active",
    },
    3: {
        "leagueCode": "EL",
        "country": "Europe",
        "competition": "UEFA Europa League",
        "display_name": "UEFA Europa League",
        "lifecycle": "active",
    },
    848: {
        "leagueCode": "CONF",
        "country": "Europe",
        "competition": "UEFA Europa Conference League",
        "display_name": "UEFA Europa Conference League",
        "lifecycle": "active",
    },
}


@dataclass(frozen=True)
class SettlementRequirement:
    generation_id: str
    competition_id: str
    match_key: str
    local_date: str
    league_code: str
    api_fixture_id: Optional[int]
    home_names: Tuple[str, ...]
    away_names: Tuple[str, ...]
    required_kind: str
    sub_market_key: str


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_now() -> str:
    return now_utc().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json_if_changed(path: Path, payload: Dict[str, Any], semantic_key: str) -> bool:
    existing = load_json(path, {})
    if isinstance(existing, dict) and existing.get(semantic_key) == payload.get(semantic_key):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


def as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def fixture_date(value: Any) -> str:
    text = str(value or "").strip()
    return text[:10] if len(text) >= 10 else ""


def normalize_team(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    stop = {"fc", "fk", "cf", "sc", "ac", "afc", "club", "pfc", "sk", "if", "bk"}
    tokens = [token for token in text.split() if token and token not in stop]
    return " ".join(tokens)


def team_matches(provider_name: str, accepted_names: Sequence[str]) -> bool:
    provider = normalize_team(provider_name)
    if not provider:
        return False
    for candidate in accepted_names:
        key = normalize_team(candidate)
        if not key:
            continue
        if provider == key:
            return True
        # Fail closed on fuzzy identity. Only harmless whole-token order differences are allowed.
        p_tokens = provider.split()
        c_tokens = key.split()
        if len(p_tokens) >= 2 and len(c_tokens) >= 2 and set(p_tokens) == set(c_tokens):
            return True
    return False


def registry_rows() -> List[Dict[str, Any]]:
    root = load_json(REGISTRY_PATH, {})
    rows = root.get("leagues", []) if isinstance(root, dict) else []
    return [row for row in rows if isinstance(row, dict) and row.get("enabledForStats") is not False]


def provider_season(row: Dict[str, Any]) -> str:
    return str(
        row.get("targetApiSeason")
        or row.get("historyApiSeason")
        or row.get("season")
        or ""
    ).strip()


def choose_registry_row(
    fixture: Dict[str, Any],
    rows_by_provider: Dict[int, List[Dict[str, Any]]],
) -> Optional[Dict[str, Any]]:
    league = fixture.get("league") or {}
    provider_id = as_int(league.get("id"))
    if provider_id is None:
        return None

    uefa = UEFA_PROVIDER_ROWS.get(provider_id)
    if uefa is not None:
        return {
            **uefa,
            "season": str(league.get("season") or "").strip(),
            "app_season": str(league.get("season") or "").strip(),
        }

    candidates = rows_by_provider.get(provider_id, [])
    if not candidates:
        return None
    season = str(league.get("season") or "").strip()
    exact = [row for row in candidates if provider_season(row) == season]
    if len(exact) == 1:
        return exact[0]
    active = [row for row in (exact or candidates) if str(row.get("lifecycle") or "").lower() == "active"]
    return (active or exact or candidates)[0]


def _current_betting_bundle_names() -> Set[str]:
    manifest = load_json(APP_READY_MANIFEST, {})
    names: Set[str] = set()
    if isinstance(manifest, dict):
        for item in manifest.get("artifacts", []) or []:
            if isinstance(item, dict) and item.get("id") == "app_ready_betting_bundle":
                path = str(item.get("path") or "").strip()
                if path:
                    names.add(Path(path).name)
    return names


def betting_bundle_paths() -> List[Path]:
    """Use current + retained previous bundles already present in the workflow checkout.

    App-Ready deliberately keeps the current and previous immutable generations. Reading those local
    ZIPs costs no API-Football quota and adds no new GitHub workflow or artifact download.
    """
    current = _current_betting_bundle_names()
    candidates = [path for path in APP_READY_DIR.glob("app_ready_betting_bundle-*.zip") if path.is_file()]
    return sorted(candidates, key=lambda path: (path.name not in current, -path.stat().st_mtime))[:2]


def _best_final_candidate_refs(connection: sqlite3.Connection, generation_id: str) -> Set[Tuple[str, str]]:
    rows = connection.execute(
        """
        SELECT competition_id, snapshot_version, selection_key, match_key,
               exact_recommendation_key, selection_score, evidence_score, source_order,
               strict_hit_rate, strict_sample, selection_odd
        FROM prepared_pattern_candidates
        WHERE generation_id=? AND recommendation_eligible=1
        ORDER BY evidence_score DESC, source_order ASC
        """,
        (generation_id,),
    ).fetchall()

    strongest_exact: Dict[Tuple[str, str, str], Tuple[Any, ...]] = {}
    for row in rows:
        exact_key = (str(row[0]), str(row[3]), str(row[4]))
        previous = strongest_exact.get(exact_key)
        if previous is None or float(row[5]) > float(previous[5]):
            strongest_exact[exact_key] = row

    strongest_match: Dict[Tuple[str, str], Tuple[Any, ...]] = {}
    for row in strongest_exact.values():
        match_key = (str(row[0]), str(row[3]))
        previous = strongest_match.get(match_key)
        rank = (float(row[5]), float(row[8]), int(row[9]), float(row[10]))
        previous_rank = None if previous is None else (
            float(previous[5]), float(previous[8]), int(previous[9]), float(previous[10])
        )
        if previous is None or rank > previous_rank:
            strongest_match[match_key] = row

    return {(str(row[0]), str(row[2])) for row in strongest_match.values()}


def _names_from_match_payload(match: Dict[str, Any], side: str) -> Tuple[str, ...]:
    prefix = "home" if side == "home" else "away"
    candidates = [
        match.get(f"{prefix}Team"),
        match.get(f"provider{prefix.capitalize()}Team"),
        match.get(f"canonical{prefix.capitalize()}Team"),
    ]
    out: List[str] = []
    seen: Set[str] = set()
    for value in candidates:
        text = str(value or "").strip()
        key = normalize_team(text)
        if text and key and key not in seen:
            out.append(text)
            seen.add(key)
    return tuple(out)


def _fixture_id_from_match_payload(match: Dict[str, Any]) -> Optional[int]:
    squad = match.get("squadContext")
    if isinstance(squad, dict):
        value = as_int(squad.get("apiFootballFixtureId"))
        if value is not None:
            return value
    for key in ("apiFootballFixtureId", "fixtureId", "fixture_id"):
        value = as_int(match.get(key))
        if value is not None:
            return value
    return None


def requirements_from_bundle(path: Path) -> List[SettlementRequirement]:
    with tempfile.TemporaryDirectory(prefix="statmaker-settlement-") as temp_dir:
        db_path = Path(temp_dir) / "statmaker_prepared_betting.db"
        try:
            with zipfile.ZipFile(path, "r") as archive:
                member = "databases/statmaker_prepared_betting.db"
                if member not in archive.namelist():
                    return []
                with archive.open(member) as source, db_path.open("wb") as target:
                    while True:
                        block = source.read(1024 * 1024)
                        if not block:
                            break
                        target.write(block)
        except (OSError, zipfile.BadZipFile) as exc:
            print(f"live-settlement could not inspect {path.name}: {exc}", file=sys.stderr)
            return []

        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            generation_row = connection.execute(
                """
                SELECT generation_id
                FROM prepared_pattern_generation
                WHERE state='ready'
                ORDER BY built_at_ms DESC
                LIMIT 1
                """
            ).fetchone()
            if generation_row is None:
                return []
            generation_id = str(generation_row[0])
            final_refs = _best_final_candidate_refs(connection, generation_id)
            if not final_refs:
                return []

            rows = connection.execute(
                """
                SELECT c.competition_id, c.selection_key, c.match_key, c.local_date,
                       c.league_code, m.payload, s.identity_sub_market_key
                FROM prepared_pattern_candidates c
                JOIN prepared_selections s
                  ON s.competition_id=c.competition_id
                 AND s.snapshot_version=c.snapshot_version
                 AND s.selection_key=c.selection_key
                JOIN prepared_matches m
                  ON m.competition_id=s.competition_id
                 AND m.snapshot_version=s.snapshot_version
                 AND m.match_key=s.match_key
                WHERE c.generation_id=? AND c.recommendation_eligible=1
                """,
                (generation_id,),
            ).fetchall()
        except sqlite3.Error as exc:
            print(f"live-settlement invalid prepared DB {path.name}: {exc}", file=sys.stderr)
            return []
        finally:
            connection.close()

    requirements: List[SettlementRequirement] = []
    for competition_id, selection_key, match_key, local_date, league_code, payload, sub_market_key in rows:
        if (str(competition_id), str(selection_key)) not in final_refs:
            continue
        try:
            match = json.loads(str(payload))
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(match, dict):
            continue
        sub = str(sub_market_key or "").strip()
        required = SUBMARKET_REQUIREMENT.get(sub, "unsupported")
        home_names = _names_from_match_payload(match, "home")
        away_names = _names_from_match_payload(match, "away")
        if not home_names or not away_names:
            continue
        requirements.append(
            SettlementRequirement(
                generation_id=generation_id,
                competition_id=str(competition_id or "").strip(),
                match_key=str(match_key or "").strip(),
                local_date=str(local_date or match.get("date") or "").strip()[:10],
                league_code=str(league_code or match.get("leagueCode") or "").strip().upper(),
                api_fixture_id=_fixture_id_from_match_payload(match),
                home_names=home_names,
                away_names=away_names,
                required_kind=required,
                sub_market_key=sub,
            )
        )
    return requirements


def canonical_requirements() -> List[SettlementRequirement]:
    merged: Dict[Tuple[str, str, str, str], SettlementRequirement] = {}
    for bundle in betting_bundle_paths():
        for row in requirements_from_bundle(bundle):
            key = (row.competition_id, row.match_key, row.sub_market_key, row.generation_id)
            merged[key] = row
    return list(merged.values())


def _date_distance(left: str, right: str) -> Optional[int]:
    try:
        a = dt.date.fromisoformat(left[:10])
        b = dt.date.fromisoformat(right[:10])
    except ValueError:
        return None
    return abs((a - b).days)


def requirements_for_fixture(
    fixture: Dict[str, Any],
    registry: Dict[str, Any],
    requirements: Sequence[SettlementRequirement],
) -> List[SettlementRequirement]:
    fixture_id = stats_fetch.fixture_identity(fixture)
    summary = stats_fetch.fixture_summary(fixture)
    home = str(summary.get("home_team") or "").strip()
    away = str(summary.get("away_team") or "").strip()
    date_text = fixture_date(summary.get("date"))
    league_code = str(registry.get("leagueCode") or "").strip().upper()

    exact_id = [row for row in requirements if fixture_id is not None and row.api_fixture_id == fixture_id]
    if exact_id:
        return exact_id

    matched: List[SettlementRequirement] = []
    for row in requirements:
        distance = _date_distance(row.local_date, date_text)
        if distance is None or distance > 1:
            continue
        if row.league_code and league_code and row.league_code != league_code:
            # UEFA historical aliases can differ only for Conference; name identity still fails closed.
            if not ({row.league_code, league_code} <= {"CONF", "UECL"}):
                continue
        if team_matches(home, row.home_names) and team_matches(away, row.away_names):
            matched.append(row)
    return matched


def normalized_stats_ready_for(stats: Any, required_kinds: Iterable[str]) -> bool:
    required = {kind for kind in required_kinds if kind in REQUIRED_FIELDS}
    if not required:
        return True
    if not isinstance(stats, dict):
        return False
    return all(
        all(stats.get(field) is not None for field in REQUIRED_FIELDS[kind])
        for kind in required
    )


def missing_required_kinds(stats: Any, required_kinds: Iterable[str]) -> List[str]:
    required = sorted({kind for kind in required_kinds if kind in REQUIRED_FIELDS})
    if not isinstance(stats, dict):
        return required
    return [
        kind for kind in required
        if any(stats.get(field) is None for field in REQUIRED_FIELDS[kind])
    ]


def merge_normalized_stats(existing: Any, incoming: Any) -> Dict[str, Any]:
    merged: Dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
    if isinstance(incoming, dict):
        for key, value in incoming.items():
            if value is not None or merged.get(key) is None:
                merged[key] = value
    return merged


def cached_map(root: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    result: Dict[int, Dict[str, Any]] = {}
    for item in root.get("fixtures", []) if isinstance(root, dict) else []:
        if not isinstance(item, dict):
            continue
        fixture_id = as_int(item.get("fixtureId"))
        if fixture_id is not None:
            result[fixture_id] = item
    return result


def prune_cache(rows: Dict[int, Dict[str, Any]], today: dt.date) -> None:
    cutoff = today - dt.timedelta(days=RETENTION_DAYS)
    stale: List[int] = []
    for fixture_id, item in rows.items():
        date_text = fixture_date(item.get("dateUtc"))
        try:
            date_value = dt.date.fromisoformat(date_text)
        except ValueError:
            stale.append(fixture_id)
            continue
        if date_value < cutoff:
            stale.append(fixture_id)
    for fixture_id in stale:
        rows.pop(fixture_id, None)


def settlement_row(
    fixture: Dict[str, Any],
    registry: Dict[str, Any],
    existing: Dict[str, Any],
    matched_requirements: Sequence[SettlementRequirement],
) -> Dict[str, Any]:
    summary = stats_fetch.fixture_summary(fixture)
    league = fixture.get("league") or {}
    status = stats_fetch.fixture_status_short(fixture)
    teams = fixture.get("teams") or {}
    home_block = teams.get("home") or {}
    away_block = teams.get("away") or {}
    required_stats = sorted({row.required_kind for row in matched_requirements if row.required_kind in REQUIRED_FIELDS})
    generations = sorted({row.generation_id for row in matched_requirements if row.generation_id})
    sub_markets = sorted({row.sub_market_key for row in matched_requirements if row.sub_market_key})
    previous_required = existing.get("requiredStats") if isinstance(existing.get("requiredStats"), list) else []
    previous_generations = existing.get("sourceGenerationIds") if isinstance(existing.get("sourceGenerationIds"), list) else []
    previous_sub_markets = existing.get("subMarketKeys") if isinstance(existing.get("subMarketKeys"), list) else []
    required_stats = sorted(set(required_stats) | {str(x) for x in previous_required})
    generations = sorted(set(generations) | {str(x) for x in previous_generations})
    sub_markets = sorted(set(sub_markets) | {str(x) for x in previous_sub_markets})

    return {
        "fixtureId": summary.get("fixture_id"),
        "dateUtc": summary.get("date"),
        "leagueCode": str(registry.get("leagueCode") or "").strip(),
        "country": str(registry.get("country") or "").strip(),
        "competition": str(registry.get("competition") or registry.get("display_name") or "").strip(),
        "season": str(registry.get("app_season") or registry.get("targetAppSeason") or registry.get("season") or "").strip(),
        "apiFootballLeagueId": as_int(league.get("id")),
        "apiFootballSeason": str(league.get("season") or "").strip(),
        "homeTeam": summary.get("home_team"),
        "awayTeam": summary.get("away_team"),
        "homeTeamId": as_int(home_block.get("id")),
        "awayTeamId": as_int(away_block.get("id")),
        "status": status,
        "homeGoals": summary.get("home_goals"),
        "awayGoals": summary.get("away_goals"),
        "homeHalfGoals": summary.get("hthg"),
        "awayHalfGoals": summary.get("htag"),
        "normalizedStats": existing.get("normalizedStats") if isinstance(existing.get("normalizedStats"), dict) else {},
        "requiredStats": required_stats,
        "sourceGenerationIds": generations,
        "subMarketKeys": sub_markets,
        "statsAttempts": int(existing.get("statsAttempts") or 0),
        "lastStatsAttemptAt": str(existing.get("lastStatsAttemptAt") or "").strip(),
        "statsFetched": bool(existing.get("statsFetched", False)),
    }


def retry_due(row: Dict[str, Any], now: dt.datetime) -> bool:
    attempts = int(row.get("statsAttempts") or 0)
    if attempts >= MAX_STATS_ATTEMPTS:
        return False
    if attempts == 0:
        return True
    last_text = str(row.get("lastStatsAttemptAt") or "").strip()
    if not last_text:
        return True
    try:
        last = dt.datetime.fromisoformat(last_text.replace("Z", "+00:00"))
        if last.tzinfo is None:
            last = last.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return True
    delay = STATS_RETRY_DELAYS_MINUTES[min(attempts, len(STATS_RETRY_DELAYS_MINUTES) - 1)]
    return now >= last + dt.timedelta(minutes=delay)


def synthetic_fixture_from_cache(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "fixture": {"id": row.get("fixtureId"), "date": row.get("dateUtc"), "status": {"short": row.get("status")}},
        "league": {"id": row.get("apiFootballLeagueId"), "season": row.get("apiFootballSeason")},
        "teams": {
            "home": {"id": row.get("homeTeamId"), "name": row.get("homeTeam")},
            "away": {"id": row.get("awayTeamId"), "name": row.get("awayTeam")},
        },
        "goals": {"home": row.get("homeGoals"), "away": row.get("awayGoals")},
        "score": {"halftime": {"home": row.get("homeHalfGoals"), "away": row.get("awayHalfGoals")}},
    }


def fetch_required_stats(
    api_key: str,
    fixture: Dict[str, Any],
    row: Dict[str, Any],
    request_state: Dict[str, int],
    max_requests: int,
) -> bool:
    required = row.get("requiredStats") if isinstance(row.get("requiredStats"), list) else []
    missing_before = missing_required_kinds(row.get("normalizedStats"), required)
    if not missing_before or not retry_due(row, now_utc()) or request_state["count"] >= max_requests:
        row["statsFetched"] = normalized_stats_ready_for(row.get("normalizedStats"), required)
        return False

    fixture_id = as_int(row.get("fixtureId")) or stats_fetch.fixture_identity(fixture)
    if fixture_id is None:
        return False

    row["statsAttempts"] = int(row.get("statsAttempts") or 0) + 1
    row["lastStatsAttemptAt"] = iso_now()
    try:
        payload = stats_fetch.api_get(
            api_key,
            "fixtures/statistics",
            {"fixture": fixture_id},
            request_state,
            max_requests,
        )
        raw = stats_fetch.response_items(payload)
        normalized = stats_fetch.normalize_statistics(raw, fixture)
        row["normalizedStats"] = merge_normalized_stats(row.get("normalizedStats"), normalized)
    except stats_fetch.RequestLimitReached:
        return False
    except Exception as error:
        print(f"live-settlement stats fetch failed fixture={fixture_id}: {error}", file=sys.stderr)
        return False

    row["statsFetched"] = normalized_stats_ready_for(row.get("normalizedStats"), required)
    return True


def fetch_fixture_dates(
    api_key: str,
    dates: Iterable[str],
    request_state: Dict[str, int],
    max_requests: int,
) -> List[Dict[str, Any]]:
    fixtures: List[Dict[str, Any]] = []
    for date_text in dates:
        try:
            payload = stats_fetch.api_get(
                api_key,
                "fixtures",
                {"date": date_text},
                request_state,
                max_requests,
            )
        except stats_fetch.RequestLimitReached:
            break
        except Exception as error:
            print(f"live-settlement fixture poll failed date={date_text}: {error}", file=sys.stderr)
            continue
        error_text = stats_fetch.api_errors(payload)
        if error_text:
            print(f"live-settlement provider errors date={date_text}: {error_text}", file=sys.stderr)
        fixtures.extend(stats_fetch.response_items(payload))
    return fixtures


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh rolling StatMaker canonical settlement feed")
    parser.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS)
    args = parser.parse_args()
    if args.max_requests < 2:
        print("ERROR: --max-requests must be at least 2", file=sys.stderr)
        return 2

    api_key = os.environ.get("API_FOOTBALL_KEY", "").strip()
    if not api_key:
        print("ERROR: API_FOOTBALL_KEY is required", file=sys.stderr)
        return 2

    quota_guard.install(stats_fetch)

    requirements = canonical_requirements()
    unsupported = [row for row in requirements if row.required_kind == "unsupported"]
    if unsupported:
        unsupported_keys = sorted({row.sub_market_key for row in unsupported})
        print(
            "live-settlement WARNING unsupported canonical settlement markets=" + ",".join(unsupported_keys),
            file=sys.stderr,
        )

    registry = registry_rows()
    rows_by_provider: Dict[int, List[Dict[str, Any]]] = {}
    for row in registry:
        provider_id = as_int(row.get("api_football_league_id") or row.get("apiFootballLeagueId"))
        if provider_id is not None:
            rows_by_provider.setdefault(provider_id, []).append(row)

    existing_root = load_json(CACHE_PATH, {})
    cache = cached_map(existing_root if isinstance(existing_root, dict) else {})
    utc_today = now_utc().date()
    prune_cache(cache, utc_today)

    request_state = {"count": 0}
    poll_dates = [(utc_today - dt.timedelta(days=1)).isoformat(), utc_today.isoformat()]
    fixtures = fetch_fixture_dates(api_key, poll_dates, request_state, args.max_requests)

    in_scope_completed = 0
    canonical_completed = 0
    stats_requests = 0
    skipped_no_recommendation = 0

    for fixture in fixtures:
        if stats_fetch.fixture_status_short(fixture) not in COMPLETED:
            continue
        registry_row = choose_registry_row(fixture, rows_by_provider)
        if registry_row is None:
            continue
        in_scope_completed += 1

        matched_requirements = requirements_for_fixture(fixture, registry_row, requirements)
        if not matched_requirements:
            skipped_no_recommendation += 1
            continue

        fixture_id = stats_fetch.fixture_identity(fixture)
        if fixture_id is None:
            continue
        canonical_completed += 1
        existing = cache.get(fixture_id, {})
        row = settlement_row(fixture, registry_row, existing, matched_requirements)
        if row.get("homeGoals") is None or row.get("awayGoals") is None:
            continue

        if fetch_required_stats(api_key, fixture, row, request_state, args.max_requests):
            stats_requests += 1
        cache[fixture_id] = row

    # Retry only previously captured canonical fixtures whose exact required metric is still absent.
    # This does not need another scoreboard request and lets late statistics arrive with controlled
    # backoff even after the fixture moves outside the current/previous UTC polling dates.
    for fixture_id, row in sorted(cache.items()):
        if request_state["count"] >= args.max_requests:
            break
        if str(row.get("status") or "").upper() not in COMPLETED:
            continue
        required = row.get("requiredStats") if isinstance(row.get("requiredStats"), list) else []
        if not missing_required_kinds(row.get("normalizedStats"), required):
            row["statsFetched"] = True
            continue
        fixture = synthetic_fixture_from_cache(row)
        if fetch_required_stats(api_key, fixture, row, request_state, args.max_requests):
            stats_requests += 1

    ordered = sorted(
        cache.values(),
        key=lambda item: (str(item.get("dateUtc") or ""), int(item.get("fixtureId") or 0)),
    )
    cache_payload = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": iso_now(),
        "retentionDays": RETENTION_DAYS,
        "canonicalRequirementCount": len(requirements),
        "fixtures": ordered,
    }
    cache_changed = write_json_if_changed(CACHE_PATH, cache_payload, "fixtures")

    feed_rows = [
        {
            "fixtureId": item.get("fixtureId"),
            "dateUtc": item.get("dateUtc"),
            "leagueCode": item.get("leagueCode"),
            "country": item.get("country"),
            "competition": item.get("competition"),
            "season": item.get("season"),
            "homeTeam": item.get("homeTeam"),
            "awayTeam": item.get("awayTeam"),
            "status": item.get("status"),
            "homeGoals": item.get("homeGoals"),
            "awayGoals": item.get("awayGoals"),
            "homeHalfGoals": item.get("homeHalfGoals"),
            "awayHalfGoals": item.get("awayHalfGoals"),
            "normalizedStats": item.get("normalizedStats") or {},
            "requiredStats": item.get("requiredStats") or [],
            "missingRequiredStats": missing_required_kinds(
                item.get("normalizedStats"), item.get("requiredStats") or []
            ),
        }
        for item in ordered
        if str(item.get("status") or "").upper() in COMPLETED
        and item.get("homeGoals") is not None
        and item.get("awayGoals") is not None
    ]
    feed_payload = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": iso_now(),
        "source": "api-football-live-settlement",
        "completedStatuses": sorted(COMPLETED),
        "retentionDays": RETENTION_DAYS,
        "fixtures": feed_rows,
    }
    feed_changed = write_json_if_changed(FEED_PATH, feed_payload, "fixtures")

    print(
        "live-settlement "
        f"dates={','.join(poll_dates)} polled={len(fixtures)} inScopeCompleted={in_scope_completed} "
        f"canonicalRequirements={len(requirements)} canonicalCompleted={canonical_completed} "
        f"skippedNoRecommendation={skipped_no_recommendation} statsRequests={stats_requests} "
        f"feedFixtures={len(feed_rows)} requests={request_state['count']} "
        f"cacheChanged={cache_changed} feedChanged={feed_changed} quota={json.dumps(quota_guard.status())}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
