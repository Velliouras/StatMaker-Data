#!/usr/bin/env python3
"""Enrich StatMaker Domestic odds with bounded API-Football match context.

Contract:
- Android never calls API-Football directly.
- Reuse cached fixture mappings and lineup history aggressively.
- Hard request cap per workflow run (default 10).
- Injuries are batched by up to 20 fixture ids.
- Confirmed lineups are requested only close to kickoff and are never inferred.
- Player importance is derived only from previously observed confirmed starting XIs;
  when history is insufficient, importance remains null.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
ODDS_PATH = ROOT / "odds" / "odds_api_io" / "domestic_odds.json"
REGISTRY_PATH = ROOT / "data" / "statmaker" / "domestic_live_july_registry.json"
ALIASES_PATH = ROOT / "mappings" / "domestic_team_aliases.json"
CACHE_PATH = ROOT / "data" / "statmaker" / "domestic_match_context_cache.json"
REPORT_PATH = ROOT / "reports" / "domestic_match_context_enrichment.json"

BASE_URL = "https://v3.football.api-sports.io"
TIMEOUT_SECONDS = 30
DEFAULT_REQUEST_CAP = 10
MAPPING_RESERVE_REQUESTS = 4
LOOKAHEAD_HOURS = 72
MAPPING_RETRY_HOURS = 8
LINEUP_LOOKAHEAD_MINUTES = 150
LINEUP_PAST_GRACE_MINUTES = 20
MAX_LINEUP_HISTORY = 8
MIN_LINEUPS_FOR_IMPORTANCE = 3


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_utc(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_dt(value: Any) -> Optional[dt.datetime]:
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


def load(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def norm(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    words = [w for w in text.split() if w not in {"fc", "cf", "sc", "afc", "fk", "ac", "if"}]
    return " ".join(words)


def parse_season(value: Any) -> Optional[int]:
    match = re.search(r"\b(20\d{2})\b", str(value or ""))
    return int(match.group(1)) if match else None


def chunks(values: Sequence[int], size: int = 20) -> Iterable[List[int]]:
    for start in range(0, len(values), size):
        yield list(values[start:start + size])


def hours_since(value: Any, now: dt.datetime) -> Optional[float]:
    parsed = parse_dt(value)
    if parsed is None:
        return None
    return max(0.0, (now - parsed).total_seconds() / 3600.0)


class ApiBudget:
    def __init__(self, api_key: str, maximum: int, report: Dict[str, Any]):
        self.api_key = api_key
        self.maximum = max(0, maximum)
        self.used = 0
        self.report = report

    @property
    def remaining(self) -> int:
        return max(0, self.maximum - self.used)

    def get(self, path: str, params: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
        if not self.api_key or self.used >= self.maximum:
            return None
        self.used += 1
        safe_params = {k: v for k, v in params.items() if v is not None and str(v) != ""}
        record: Dict[str, Any] = {"path": path, "params": safe_params}
        self.report.setdefault("calls", []).append(record)
        try:
            query = urlencode(safe_params)
            request = Request(
                f"{BASE_URL}{path}?{query}",
                headers={
                    "x-apisports-key": self.api_key,
                    "Accept": "application/json",
                    "User-Agent": "StatMaker-Data domestic context enrichment",
                },
                method="GET",
            )
            with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                payload = json.loads(response.read().decode("utf-8"))
            errors = payload.get("errors") if isinstance(payload, dict) else None
            if errors:
                record["error"] = errors
                return None
            items = payload.get("response") if isinstance(payload, dict) else None
            record["results"] = len(items or []) if isinstance(items, list) else 0
            return [item for item in (items or []) if isinstance(item, dict)]
        except Exception as exc:
            record["error"] = str(exc)[:300]
            return None


def build_alias_lookup(payload: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    out: Dict[str, Dict[str, str]] = {}
    for league_code, families in ((payload.get("aliases") or {}).items()):
        league: Dict[str, str] = {}
        for canonical, variants in (families or {}).items():
            canonical_key = norm(canonical)
            if canonical_key:
                league[canonical_key] = canonical_key
            for variant in variants or []:
                key = norm(variant)
                if key and canonical_key:
                    league[key] = canonical_key
        out[str(league_code).upper()] = league
    return out


def team_key(name: Any, league_code: str, aliases: Dict[str, Dict[str, str]]) -> str:
    key = norm(name)
    return aliases.get(league_code.upper(), {}).get(key, key)


def match_key(league_code: str, kickoff: dt.datetime, home: Any, away: Any, aliases: Dict[str, Dict[str, str]]) -> str:
    return "|".join([
        league_code.upper(),
        kickoff.date().isoformat(),
        team_key(home, league_code, aliases),
        team_key(away, league_code, aliases),
    ])


def registry_by_code(payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {
        str(item.get("leagueCode") or "").upper(): item
        for item in payload.get("leagues", []) or []
        if isinstance(item, dict) and str(item.get("leagueCode") or "").strip()
    }


def candidate_matches(feed: Dict[str, Any], aliases: Dict[str, Dict[str, str]], now: dt.datetime) -> List[Dict[str, Any]]:
    upper = now + dt.timedelta(hours=LOOKAHEAD_HOURS)
    lower = now - dt.timedelta(hours=1)
    out: List[Dict[str, Any]] = []
    for league in feed.get("leagues", []) or []:
        if not isinstance(league, dict):
            continue
        code = str(league.get("leagueCode") or "").upper()
        for match in league.get("matches", []) or []:
            if not isinstance(match, dict):
                continue
            kickoff = parse_dt(match.get("kickoff") or match.get("date"))
            if kickoff is None or kickoff < lower or kickoff > upper:
                match.pop("squadContext", None)
                continue
            home = match.get("canonicalHomeTeam") or match.get("homeTeam")
            away = match.get("canonicalAwayTeam") or match.get("awayTeam")
            if not home or not away:
                continue
            out.append({
                "league": league,
                "match": match,
                "leagueCode": code,
                "kickoff": kickoff,
                "home": str(home),
                "away": str(away),
                "key": match_key(code, kickoff, home, away, aliases),
            })
    return out


def fixture_record(item: Dict[str, Any], league_code: str, aliases: Dict[str, Dict[str, str]]) -> Optional[Dict[str, Any]]:
    fixture = item.get("fixture") or {}
    teams = item.get("teams") or {}
    home = teams.get("home") or {}
    away = teams.get("away") or {}
    kickoff = parse_dt(fixture.get("date"))
    try:
        fixture_id = int(fixture.get("id"))
        home_id = int(home.get("id"))
        away_id = int(away.get("id"))
    except (TypeError, ValueError):
        return None
    if kickoff is None or fixture_id <= 0:
        return None
    return {
        "fixtureId": fixture_id,
        "kickoff": kickoff,
        "homeId": home_id,
        "awayId": away_id,
        "homeKey": team_key(home.get("name"), league_code, aliases),
        "awayKey": team_key(away.get("name"), league_code, aliases),
    }


def map_missing_fixtures(candidates, cache, registry, aliases, api, now, report) -> None:
    mappings = cache.setdefault("matchMappings", {})
    by_league: Dict[str, List[Dict[str, Any]]] = {}
    for row in candidates:
        cached = mappings.get(row["key"]) or {}
        if cached.get("fixtureId"):
            continue
        elapsed = hours_since(cached.get("lastMappingAttemptAt"), now)
        if elapsed is not None and elapsed < MAPPING_RETRY_HOURS:
            continue
        by_league.setdefault(row["leagueCode"], []).append(row)

    for code, rows in sorted(by_league.items(), key=lambda pair: min(x["kickoff"] for x in pair[1])):
        if api.remaining <= MAPPING_RESERVE_REQUESTS:
            break
        meta = registry.get(code, {})
        league_id = meta.get("apiFootballLeagueId") or meta.get("api_football_league_id")
        season = parse_season(meta.get("targetApiSeason") or meta.get("season") or rows[0]["league"].get("season"))
        if not league_id or season is None:
            continue
        dates = [row["kickoff"].date() for row in rows]
        items = api.get("/fixtures", {
            "league": league_id,
            "season": season,
            "from": min(dates).isoformat(),
            "to": max(dates).isoformat(),
            "timezone": "UTC",
        })
        attempt_at = iso_utc(now)
        for row in rows:
            mappings.setdefault(row["key"], {})["lastMappingAttemptAt"] = attempt_at
        if items is None:
            continue
        available = [fixture_record(item, code, aliases) for item in items]
        available = [item for item in available if item is not None]
        for row in rows:
            home_key = team_key(row["home"], code, aliases)
            away_key = team_key(row["away"], code, aliases)
            matches = [item for item in available if item["homeKey"] == home_key and item["awayKey"] == away_key]
            if not matches:
                continue
            best = min(matches, key=lambda item: abs((item["kickoff"] - row["kickoff"]).total_seconds()))
            if abs((best["kickoff"] - row["kickoff"]).total_seconds()) > 12 * 3600:
                continue
            mappings[row["key"]] = {
                **(mappings.get(row["key"]) or {}),
                "fixtureId": best["fixtureId"],
                "kickoff": iso_utc(best["kickoff"]),
                "homeTeamId": best["homeId"],
                "awayTeamId": best["awayId"],
                "homeTeam": row["home"],
                "awayTeam": row["away"],
                "leagueCode": code,
                "lastMappingAttemptAt": attempt_at,
            }
            report["fixtureMappingsAdded"] += 1


def should_refresh_injuries(mapping, kickoff, now) -> bool:
    elapsed = hours_since(mapping.get("injuriesQueriedAt"), now)
    if elapsed is None:
        return True
    hours_to_kickoff = max(0.0, (kickoff - now).total_seconds() / 3600.0)
    return elapsed >= (4.0 if hours_to_kickoff <= 24.0 else 12.0)


def refresh_injuries(candidates, cache, api, now, report) -> None:
    mappings = cache.setdefault("matchMappings", {})
    fixture_ids: List[int] = []
    by_fixture: Dict[int, Dict[str, Any]] = {}
    for row in candidates:
        mapping = mappings.get(row["key"]) or {}
        fixture_id = mapping.get("fixtureId")
        if not fixture_id or not should_refresh_injuries(mapping, row["kickoff"], now):
            continue
        fixture_id = int(fixture_id)
        fixture_ids.append(fixture_id)
        by_fixture[fixture_id] = mapping

    for batch in chunks(sorted(set(fixture_ids)), 20):
        if api.remaining <= 0:
            break
        items = api.get("/injuries", {"ids": "-".join(str(x) for x in batch), "timezone": "UTC"})
        if items is None:
            continue
        grouped: Dict[int, List[Dict[str, Any]]] = {fixture_id: [] for fixture_id in batch}
        for item in items:
            try:
                fixture_id = int((item.get("fixture") or {}).get("id"))
            except (TypeError, ValueError):
                continue
            if fixture_id not in grouped:
                continue
            player = item.get("player") or {}
            team = item.get("team") or {}
            try:
                player_id = int(player.get("id"))
            except (TypeError, ValueError):
                player_id = None
            try:
                team_id = int(team.get("id"))
            except (TypeError, ValueError):
                team_id = None
            grouped[fixture_id].append({
                "playerId": player_id,
                "playerName": str(player.get("name") or "").strip(),
                "type": str(player.get("type") or "Missing Fixture").strip(),
                "reason": str(player.get("reason") or "").strip(),
                "teamId": team_id,
            })
        queried_at = iso_utc(now)
        for fixture_id in batch:
            mapping = by_fixture.get(fixture_id)
            if mapping is None:
                continue
            mapping["injuriesQueriedAt"] = queried_at
            mapping["injuries"] = grouped.get(fixture_id, [])
            report["injuryFixturesRefreshed"] += 1
        report["injuryBatches"] += 1


def historical_lineups(cache, team_id, exclude_fixture_id=None):
    rows = list((cache.setdefault("teamLineups", {}).get(str(team_id)) or []))
    if exclude_fixture_id is not None:
        rows = [row for row in rows if int(row.get("fixtureId") or 0) != exclude_fixture_id]
    rows.sort(key=lambda row: str(row.get("kickoff") or ""), reverse=True)
    return rows[:MAX_LINEUP_HISTORY]


def player_history(cache, team_id, player_id, exclude_fixture_id=None):
    rows = historical_lineups(cache, team_id, exclude_fixture_id)
    if len(rows) < MIN_LINEUPS_FOR_IMPORTANCE:
        return None, None
    starts = 0
    position = None
    for row in rows:
        for player in row.get("starters", []) or []:
            if int(player.get("id") or 0) == player_id:
                starts += 1
                if not position and player.get("position"):
                    position = str(player.get("position"))
                break
    return round(starts / len(rows), 4), position


def lineup_continuity(cache, team_id, fixture_id, starters):
    rows = historical_lineups(cache, team_id, fixture_id)
    if len(rows) < MIN_LINEUPS_FOR_IMPORTANCE:
        return None
    counts: Counter[int] = Counter()
    for row in rows:
        counts.update(int(player.get("id") or 0) for player in row.get("starters", []) or [] if int(player.get("id") or 0) > 0)
    regulars = {player_id for player_id, _ in counts.most_common(11)}
    current = {int(player.get("id") or 0) for player in starters if int(player.get("id") or 0) > 0}
    if not regulars or not current:
        return None
    return round(len(regulars.intersection(current)) / max(1, len(regulars)), 4)


def extract_lineups(item):
    result = {}
    for block in item.get("lineups", []) or []:
        if not isinstance(block, dict):
            continue
        try:
            team_id = int((block.get("team") or {}).get("id"))
        except (TypeError, ValueError):
            continue
        starters = []
        for row in block.get("startXI", []) or []:
            player = row.get("player") or {}
            try:
                player_id = int(player.get("id"))
            except (TypeError, ValueError):
                continue
            starters.append({
                "id": player_id,
                "name": str(player.get("name") or "").strip(),
                "position": str(player.get("pos") or "").strip() or None,
            })
        if len(starters) >= 7:
            result[team_id] = starters
    return result


def record_lineup(cache, team_id, fixture_id, kickoff, starters):
    catalog = cache.setdefault("teamLineups", {})
    rows = [row for row in (catalog.get(str(team_id)) or []) if int(row.get("fixtureId") or 0) != fixture_id]
    rows.append({"fixtureId": fixture_id, "kickoff": kickoff, "starters": starters})
    rows.sort(key=lambda row: str(row.get("kickoff") or ""), reverse=True)
    catalog[str(team_id)] = rows[:MAX_LINEUP_HISTORY]


def refresh_lineups(candidates, cache, api, now, report) -> None:
    mappings = cache.setdefault("matchMappings", {})
    ids: List[int] = []
    by_fixture: Dict[int, Dict[str, Any]] = {}
    for row in candidates:
        minutes_to_kickoff = (row["kickoff"] - now).total_seconds() / 60.0
        if minutes_to_kickoff < -LINEUP_PAST_GRACE_MINUTES or minutes_to_kickoff > LINEUP_LOOKAHEAD_MINUTES:
            continue
        mapping = mappings.get(row["key"]) or {}
        fixture_id = mapping.get("fixtureId")
        if not fixture_id or mapping.get("lineupConfirmed") is True:
            continue
        fixture_id = int(fixture_id)
        ids.append(fixture_id)
        by_fixture[fixture_id] = mapping

    for batch in chunks(sorted(set(ids)), 20):
        if api.remaining <= 0:
            break
        items = api.get("/fixtures", {"ids": "-".join(str(x) for x in batch), "timezone": "UTC"})
        if items is None:
            continue
        by_id = {}
        for item in items:
            try:
                fixture_id = int((item.get("fixture") or {}).get("id"))
            except (TypeError, ValueError):
                continue
            by_id[fixture_id] = item
        queried_at = iso_utc(now)
        for fixture_id in batch:
            mapping = by_fixture.get(fixture_id)
            if mapping is None:
                continue
            mapping["lineupQueriedAt"] = queried_at
            lineups = extract_lineups(by_id.get(fixture_id, {}))
            home_id = int(mapping.get("homeTeamId") or 0)
            away_id = int(mapping.get("awayTeamId") or 0)
            if home_id not in lineups or away_id not in lineups:
                continue
            kickoff = str(mapping.get("kickoff") or "")
            mapping["homeLineupContinuity"] = lineup_continuity(cache, home_id, fixture_id, lineups[home_id])
            mapping["awayLineupContinuity"] = lineup_continuity(cache, away_id, fixture_id, lineups[away_id])
            mapping["lineupConfirmed"] = True
            mapping["homeStarters"] = lineups[home_id]
            mapping["awayStarters"] = lineups[away_id]
            record_lineup(cache, home_id, fixture_id, kickoff, lineups[home_id])
            record_lineup(cache, away_id, fixture_id, kickoff, lineups[away_id])
            report["confirmedLineupsAdded"] += 1
        report["lineupBatches"] += 1


def public_player(row, team_id, fixture_id, cache, confirmed_starters):
    player_id = row.get("playerId")
    if player_id and int(player_id) in confirmed_starters:
        return None
    player_name = str(row.get("playerName") or "").strip()
    if not player_name:
        return None
    importance = None
    position = None
    if player_id:
        importance, position = player_history(cache, team_id, int(player_id), fixture_id)
    status = "QUESTIONABLE" if "question" in str(row.get("type") or "").lower() else "MISSING"
    return {
        "id": int(player_id) if player_id else None,
        "name": player_name,
        "status": status,
        "reason": str(row.get("reason") or "").strip() or None,
        "position": position,
        "importance": importance,
    }


def attach_context(candidates, cache, now, report) -> None:
    mappings = cache.setdefault("matchMappings", {})
    generated_at = iso_utc(now)
    for row in candidates:
        mapping = mappings.get(row["key"]) or {}
        fixture_id = mapping.get("fixtureId")
        if not fixture_id:
            row["match"].pop("squadContext", None)
            continue
        fixture_id = int(fixture_id)
        home_id = int(mapping.get("homeTeamId") or 0)
        away_id = int(mapping.get("awayTeamId") or 0)
        home_starters = {int(item.get("id") or 0) for item in mapping.get("homeStarters", []) or []}
        away_starters = {int(item.get("id") or 0) for item in mapping.get("awayStarters", []) or []}
        injuries = mapping.get("injuries", []) or []
        home_players = [player for player in (
            public_player(item, home_id, fixture_id, cache, home_starters)
            for item in injuries if int(item.get("teamId") or 0) == home_id
        ) if player is not None]
        away_players = [player for player in (
            public_player(item, away_id, fixture_id, cache, away_starters)
            for item in injuries if int(item.get("teamId") or 0) == away_id
        ) if player is not None]
        injuries_covered = bool(mapping.get("injuriesQueriedAt"))
        lineup_covered = bool(mapping.get("lineupQueriedAt"))
        row["match"]["squadContext"] = {
            "apiFootballFixtureId": fixture_id,
            "generatedAt": generated_at,
            "home": {
                "teamId": home_id or None,
                "injuriesCovered": injuries_covered,
                "lineupCovered": lineup_covered,
                "lineupConfirmed": bool(mapping.get("lineupConfirmed")),
                "lineupContinuity": mapping.get("homeLineupContinuity"),
                "unavailablePlayers": home_players,
            },
            "away": {
                "teamId": away_id or None,
                "injuriesCovered": injuries_covered,
                "lineupCovered": lineup_covered,
                "lineupConfirmed": bool(mapping.get("lineupConfirmed")),
                "lineupContinuity": mapping.get("awayLineupContinuity"),
                "unavailablePlayers": away_players,
            },
        }
        report["matchesWithContext"] += 1
        report["publishedUnavailablePlayers"] += len(home_players) + len(away_players)


def prune_cache(cache, now) -> None:
    cutoff = now - dt.timedelta(days=3)
    mappings = cache.setdefault("matchMappings", {})
    for key in list(mappings):
        kickoff = parse_dt((mappings.get(key) or {}).get("kickoff"))
        if kickoff is not None and kickoff < cutoff:
            del mappings[key]
    for team_id, rows in list(cache.setdefault("teamLineups", {}).items()):
        kept = []
        for row in rows:
            kickoff = parse_dt(row.get("kickoff"))
            if kickoff is None or kickoff >= now - dt.timedelta(days=240):
                kept.append(row)
        kept.sort(key=lambda row: str(row.get("kickoff") or ""), reverse=True)
        cache["teamLineups"][team_id] = kept[:MAX_LINEUP_HISTORY]


def main() -> int:
    api_key = os.getenv("API_FOOTBALL_KEY", "").strip()
    try:
        request_cap = max(0, int(os.getenv("DOMESTIC_CONTEXT_API_REQUEST_CAP", str(DEFAULT_REQUEST_CAP))))
    except ValueError:
        request_cap = DEFAULT_REQUEST_CAP
    feed = load(ODDS_PATH, {})
    registry = registry_by_code(load(REGISTRY_PATH, {}))
    aliases = build_alias_lookup(load(ALIASES_PATH, {}))
    cache = load(CACHE_PATH, {"schemaVersion": 1, "matchMappings": {}, "teamLineups": {}})
    now = now_utc()
    report: Dict[str, Any] = {
        "generatedAt": iso_utc(now),
        "requestCap": request_cap,
        "requestsUsed": 0,
        "candidateMatches": 0,
        "fixtureMappingsAdded": 0,
        "injuryBatches": 0,
        "injuryFixturesRefreshed": 0,
        "lineupBatches": 0,
        "confirmedLineupsAdded": 0,
        "matchesWithContext": 0,
        "publishedUnavailablePlayers": 0,
        "calls": [],
        "policy": {
            "lookaheadHours": LOOKAHEAD_HOURS,
            "mappingRetryHours": MAPPING_RETRY_HOURS,
            "injuryBatchSize": 20,
            "lineupBatchSize": 20,
            "lineupLookaheadMinutes": LINEUP_LOOKAHEAD_MINUTES,
            "importanceSource": "confirmed starting-XI history only",
        },
    }
    if not isinstance(feed, dict) or not feed.get("leagues"):
        report["status"] = "skipped_empty_feed"
        save(REPORT_PATH, report)
        return 0
    if not api_key or request_cap <= 0:
        report["status"] = "skipped_missing_key_or_zero_cap"
        save(REPORT_PATH, report)
        return 0

    api = ApiBudget(api_key, request_cap, report)
    candidates = candidate_matches(feed, aliases, now)
    report["candidateMatches"] = len(candidates)
    map_missing_fixtures(candidates, cache, registry, aliases, api, now, report)
    refresh_injuries(candidates, cache, api, now, report)
    refresh_lineups(candidates, cache, api, now, report)
    attach_context(candidates, cache, now, report)
    prune_cache(cache, now)

    cache["schemaVersion"] = 1
    cache["generatedAt"] = iso_utc(now)
    report["requestsUsed"] = api.used
    report["requestCapRespected"] = api.used <= request_cap
    report["cachedMappings"] = len(cache.get("matchMappings", {}))
    report["teamsWithLineupHistory"] = len(cache.get("teamLineups", {}))
    report["status"] = "ok"

    save(ODDS_PATH, feed)
    save(CACHE_PATH, cache)
    save(REPORT_PATH, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
