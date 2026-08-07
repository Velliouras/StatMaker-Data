#!/usr/bin/env python3
"""Rolling Domestic confirmed-lineup and formation watcher.

Design contract:
- Android never calls API-Football directly.
- Uses fixture mappings already produced by enrich_domestic_match_context.py.
- Queries only mapped fixtures close to kickoff.
- Batches up to 20 fixture ids per provider request.
- Hard provider request cap per run (default 3).
- Stops querying a fixture permanently once both lineups are confirmed.
- Keeps recent confirmed-XI/formation history in the shared context cache.
- Publishes current formation, usual formation and sample without inventing tactics.
- --attach-only performs zero provider calls and only re-attaches cached lineup context.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
ODDS_PATH = ROOT / "odds" / "odds_api_io" / "domestic_odds.json"
ALIASES_PATH = ROOT / "mappings" / "domestic_team_aliases.json"
CACHE_PATH = ROOT / "data" / "statmaker" / "domestic_match_context_cache.json"
REPORT_PATH = ROOT / "reports" / "domestic_lineup_watch.json"

BASE_URL = "https://v3.football.api-sports.io"
TIMEOUT_SECONDS = 30
DEFAULT_REQUEST_CAP = 3
LINEUP_LOOKAHEAD_MINUTES = 150
LINEUP_PAST_GRACE_MINUTES = 20
EARLY_RETRY_MINUTES = 60
LATE_RETRY_MINUTES = 25
MAX_LINEUP_HISTORY = 8
LINEUP_BATCH_SIZE = 20


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


def match_key(
    league_code: str,
    kickoff: dt.datetime,
    home: Any,
    away: Any,
    aliases: Dict[str, Dict[str, str]],
) -> str:
    return "|".join([
        league_code.upper(),
        kickoff.date().isoformat(),
        team_key(home, league_code, aliases),
        team_key(away, league_code, aliases),
    ])


def chunks(values: Sequence[int], size: int = LINEUP_BATCH_SIZE) -> Iterable[List[int]]:
    for start in range(0, len(values), size):
        yield list(values[start:start + size])


def minutes_since(value: Any, now: dt.datetime) -> Optional[float]:
    parsed = parse_dt(value)
    if parsed is None:
        return None
    return max(0.0, (now - parsed).total_seconds() / 60.0)


def normalize_formation(value: Any) -> Optional[str]:
    text = str(value or "").strip().replace("–", "-").replace("—", "-")
    if not text:
        return None
    parts = [part for part in re.split(r"[^0-9]+", text) if part]
    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        return None
    if len(numbers) not in {2, 3, 4} or any(number <= 0 for number in numbers) or sum(numbers) != 10:
        return None
    return "-".join(str(number) for number in numbers)


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
        safe_params = {key: value for key, value in params.items() if value is not None and str(value) != ""}
        record: Dict[str, Any] = {"path": path, "params": safe_params}
        self.report.setdefault("calls", []).append(record)
        try:
            request = Request(
                f"{BASE_URL}{path}?{urlencode(safe_params)}",
                headers={
                    "x-apisports-key": self.api_key,
                    "Accept": "application/json",
                    "User-Agent": "StatMaker-Data domestic rolling lineup watcher",
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


def feed_rows(feed: Dict[str, Any], aliases: Dict[str, Dict[str, str]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for league in feed.get("leagues", []) or []:
        if not isinstance(league, dict):
            continue
        code = str(league.get("leagueCode") or "").upper()
        for match in league.get("matches", []) or []:
            if not isinstance(match, dict):
                continue
            kickoff = parse_dt(match.get("kickoff") or match.get("date"))
            if kickoff is None:
                continue
            home = match.get("canonicalHomeTeam") or match.get("homeTeam")
            away = match.get("canonicalAwayTeam") or match.get("awayTeam")
            if not home or not away:
                continue
            rows.append({
                "match": match,
                "leagueCode": code,
                "kickoff": kickoff,
                "home": str(home),
                "away": str(away),
                "key": match_key(code, kickoff, home, away, aliases),
            })
    return rows


def historical_lineups(cache: Dict[str, Any], team_id: int, exclude_fixture_id: Optional[int] = None) -> List[Dict[str, Any]]:
    rows = list((cache.setdefault("teamLineups", {}).get(str(team_id)) or []))
    if exclude_fixture_id is not None:
        rows = [row for row in rows if int(row.get("fixtureId") or 0) != exclude_fixture_id]
    rows.sort(key=lambda row: str(row.get("kickoff") or ""), reverse=True)
    return rows[:MAX_LINEUP_HISTORY]


def lineup_continuity(cache: Dict[str, Any], team_id: int, fixture_id: int, starters: List[Dict[str, Any]]) -> Optional[float]:
    rows = historical_lineups(cache, team_id, fixture_id)
    if len(rows) < 3:
        return None
    counts: Counter[int] = Counter()
    for row in rows:
        counts.update(
            int(player.get("id") or 0)
            for player in row.get("starters", []) or []
            if int(player.get("id") or 0) > 0
        )
    regulars = {player_id for player_id, _ in counts.most_common(11)}
    current = {int(player.get("id") or 0) for player in starters if int(player.get("id") or 0) > 0}
    if not regulars or not current:
        return None
    return round(len(regulars.intersection(current)) / max(1, len(regulars)), 4)


def usual_formation(cache: Dict[str, Any], team_id: int, fixture_id: int) -> Tuple[Optional[str], int]:
    rows = historical_lineups(cache, team_id, fixture_id)
    formations = [normalize_formation(row.get("formation")) for row in rows]
    formations = [formation for formation in formations if formation]
    if not formations:
        return None, 0
    counts = Counter(formations)
    best_count = max(counts.values())
    candidates = {formation for formation, count in counts.items() if count == best_count}
    # historical_lineups is newest-first, so ties resolve to the most recently used shape.
    usual = next(formation for formation in formations if formation in candidates)
    return usual, len(formations)


def extract_lineups(item: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    result: Dict[int, Dict[str, Any]] = {}
    for block in item.get("lineups", []) or []:
        if not isinstance(block, dict):
            continue
        try:
            team_id = int((block.get("team") or {}).get("id"))
        except (TypeError, ValueError):
            continue
        starters: List[Dict[str, Any]] = []
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
            result[team_id] = {
                "starters": starters,
                "formation": normalize_formation(block.get("formation")),
            }
    return result


def record_lineup(
    cache: Dict[str, Any],
    team_id: int,
    fixture_id: int,
    kickoff: str,
    starters: List[Dict[str, Any]],
    formation: Optional[str],
) -> None:
    catalog = cache.setdefault("teamLineups", {})
    rows = [
        row for row in (catalog.get(str(team_id)) or [])
        if int(row.get("fixtureId") or 0) != fixture_id
    ]
    rows.append({
        "fixtureId": fixture_id,
        "kickoff": kickoff,
        "starters": starters,
        "formation": formation,
    })
    rows.sort(key=lambda row: str(row.get("kickoff") or ""), reverse=True)
    catalog[str(team_id)] = rows[:MAX_LINEUP_HISTORY]


def should_query(mapping: Dict[str, Any], kickoff: dt.datetime, now: dt.datetime) -> bool:
    if mapping.get("lineupConfirmed") is True:
        return False
    minutes_to_kickoff = (kickoff - now).total_seconds() / 60.0
    if minutes_to_kickoff < -LINEUP_PAST_GRACE_MINUTES or minutes_to_kickoff > LINEUP_LOOKAHEAD_MINUTES:
        return False
    elapsed = minutes_since(mapping.get("lineupQueriedAt"), now)
    if elapsed is None:
        return True
    retry_after = EARLY_RETRY_MINUTES if minutes_to_kickoff > 90.0 else LATE_RETRY_MINUTES
    return elapsed >= retry_after


def refresh_lineups(
    rows: List[Dict[str, Any]],
    cache: Dict[str, Any],
    api: ApiBudget,
    now: dt.datetime,
    report: Dict[str, Any],
) -> None:
    mappings = cache.setdefault("matchMappings", {})
    by_fixture: Dict[int, Tuple[Dict[str, Any], Dict[str, Any]]] = {}
    for row in rows:
        mapping = mappings.get(row["key"]) or {}
        fixture_id = mapping.get("fixtureId")
        if not fixture_id or not should_query(mapping, row["kickoff"], now):
            continue
        fixture_id = int(fixture_id)
        by_fixture[fixture_id] = (row, mapping)

    fixture_ids = sorted(by_fixture)
    report["eligibleFixtures"] = len(fixture_ids)
    for batch in chunks(fixture_ids):
        if api.remaining <= 0:
            report["deferredFixtures"] += len(batch)
            continue
        items = api.get("/fixtures", {
            "ids": "-".join(str(fixture_id) for fixture_id in batch),
            "timezone": "UTC",
        })
        if items is None:
            continue
        by_id: Dict[int, Dict[str, Any]] = {}
        for item in items:
            try:
                fixture_id = int((item.get("fixture") or {}).get("id"))
            except (TypeError, ValueError):
                continue
            by_id[fixture_id] = item

        queried_at = iso_utc(now)
        for fixture_id in batch:
            row, mapping = by_fixture[fixture_id]
            mapping["lineupQueriedAt"] = queried_at
            lineups = extract_lineups(by_id.get(fixture_id, {}))
            home_id = int(mapping.get("homeTeamId") or 0)
            away_id = int(mapping.get("awayTeamId") or 0)
            if home_id not in lineups or away_id not in lineups:
                continue

            home_lineup = lineups[home_id]
            away_lineup = lineups[away_id]
            home_starters = home_lineup["starters"]
            away_starters = away_lineup["starters"]
            home_usual, home_sample = usual_formation(cache, home_id, fixture_id)
            away_usual, away_sample = usual_formation(cache, away_id, fixture_id)

            mapping["homeLineupContinuity"] = lineup_continuity(cache, home_id, fixture_id, home_starters)
            mapping["awayLineupContinuity"] = lineup_continuity(cache, away_id, fixture_id, away_starters)
            mapping["homeFormation"] = home_lineup.get("formation")
            mapping["awayFormation"] = away_lineup.get("formation")
            mapping["homeUsualFormation"] = home_usual
            mapping["awayUsualFormation"] = away_usual
            mapping["homeFormationSample"] = home_sample
            mapping["awayFormationSample"] = away_sample
            mapping["lineupConfirmed"] = True
            mapping["homeStarters"] = home_starters
            mapping["awayStarters"] = away_starters

            kickoff = str(mapping.get("kickoff") or iso_utc(row["kickoff"]))
            record_lineup(cache, home_id, fixture_id, kickoff, home_starters, home_lineup.get("formation"))
            record_lineup(cache, away_id, fixture_id, kickoff, away_starters, away_lineup.get("formation"))
            report["confirmedLineupsAdded"] += 1
            if home_lineup.get("formation") and away_lineup.get("formation"):
                report["confirmedFormationPairsAdded"] += 1
        report["lineupBatches"] += 1


def attach_cached_context(
    feed: Dict[str, Any],
    rows: List[Dict[str, Any]],
    cache: Dict[str, Any],
    now: dt.datetime,
    report: Dict[str, Any],
) -> None:
    mappings = cache.setdefault("matchMappings", {})
    generated_at = iso_utc(now)
    for row in rows:
        mapping = mappings.get(row["key"]) or {}
        fixture_id = mapping.get("fixtureId")
        if not fixture_id:
            continue
        match = row["match"]
        context = match.get("squadContext")
        if not isinstance(context, dict):
            context = {}
            match["squadContext"] = context
        context["apiFootballFixtureId"] = int(fixture_id)
        context["generatedAt"] = generated_at

        for side, prefix, team_id_key in (
            ("home", "home", "homeTeamId"),
            ("away", "away", "awayTeamId"),
        ):
            team = context.get(side)
            if not isinstance(team, dict):
                team = {}
                context[side] = team
            team_id = int(mapping.get(team_id_key) or 0)
            if team_id > 0:
                team["teamId"] = team_id
            team["lineupCovered"] = bool(mapping.get("lineupQueriedAt"))
            team["lineupConfirmed"] = bool(mapping.get("lineupConfirmed"))
            team["lineupContinuity"] = mapping.get(f"{prefix}LineupContinuity")
            team["formation"] = mapping.get(f"{prefix}Formation")
            team["usualFormation"] = mapping.get(f"{prefix}UsualFormation")
            team["formationSample"] = int(mapping.get(f"{prefix}FormationSample") or 0)

        report["matchesWithCachedLineupContext"] += 1


def prune_lineup_history(cache: Dict[str, Any], now: dt.datetime) -> None:
    cutoff = now - dt.timedelta(days=240)
    catalog = cache.setdefault("teamLineups", {})
    for team_id, rows in list(catalog.items()):
        kept: List[Dict[str, Any]] = []
        for row in rows or []:
            kickoff = parse_dt(row.get("kickoff"))
            if kickoff is None or kickoff >= cutoff:
                kept.append(row)
        kept.sort(key=lambda row: str(row.get("kickoff") or ""), reverse=True)
        catalog[team_id] = kept[:MAX_LINEUP_HISTORY]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--attach-only", action="store_true")
    args = parser.parse_args()

    try:
        request_cap = max(0, int(os.getenv("DOMESTIC_LINEUP_API_REQUEST_CAP", str(DEFAULT_REQUEST_CAP))))
    except ValueError:
        request_cap = DEFAULT_REQUEST_CAP

    feed = load(ODDS_PATH, {})
    aliases = build_alias_lookup(load(ALIASES_PATH, {}))
    cache = load(CACHE_PATH, {"schemaVersion": 1, "matchMappings": {}, "teamLineups": {}})
    now = now_utc()
    report: Dict[str, Any] = {
        "generatedAt": iso_utc(now),
        "attachOnly": bool(args.attach_only),
        "requestCap": 0 if args.attach_only else request_cap,
        "requestsUsed": 0,
        "feedMatches": 0,
        "eligibleFixtures": 0,
        "deferredFixtures": 0,
        "lineupBatches": 0,
        "confirmedLineupsAdded": 0,
        "confirmedFormationPairsAdded": 0,
        "matchesWithCachedLineupContext": 0,
        "calls": [],
        "policy": {
            "lookaheadMinutes": LINEUP_LOOKAHEAD_MINUTES,
            "pastGraceMinutes": LINEUP_PAST_GRACE_MINUTES,
            "batchSize": LINEUP_BATCH_SIZE,
            "earlyRetryMinutes": EARLY_RETRY_MINUTES,
            "lateRetryMinutes": LATE_RETRY_MINUTES,
            "maxLineupHistory": MAX_LINEUP_HISTORY,
            "formationSource": "provider confirmed lineup only",
        },
    }

    if not isinstance(feed, dict) or not feed.get("leagues"):
        report["status"] = "skipped_empty_feed"
        save(REPORT_PATH, report)
        return 0

    rows = feed_rows(feed, aliases)
    report["feedMatches"] = len(rows)

    if not args.attach_only:
        api_key = os.getenv("API_FOOTBALL_KEY", "").strip()
        if api_key and request_cap > 0:
            api = ApiBudget(api_key, request_cap, report)
            refresh_lineups(rows, cache, api, now, report)
            report["requestsUsed"] = api.used
        else:
            report["status"] = "attach_only_missing_key_or_zero_cap"

    attach_cached_context(feed, rows, cache, now, report)
    prune_lineup_history(cache, now)
    save(ODDS_PATH, feed)
    save(CACHE_PATH, cache)
    report["teamsWithLineupHistory"] = sum(
        1 for rows in (cache.get("teamLineups") or {}).values() if rows
    )
    report["requestCapRespected"] = report["requestsUsed"] <= report["requestCap"]
    report.setdefault("status", "ok")
    save(REPORT_PATH, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
