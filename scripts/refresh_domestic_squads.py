#!/usr/bin/env python3
"""Low-frequency Domestic current-squad enrichment.

Contract:
- Uses API-Football /players/squads only for teams with upcoming Domestic fixtures.
- Current squad snapshots are cached for almost one week; fresh teams cost zero calls.
- Hard call cap plus a daily-quota reserve protects higher-priority stats workflows.
- Squad turnover is not interpreted as better/worse. It only measures how much of the
  recent confirmed-XI core is still present in the provider's current squad.
- Arrival/departure counts come from objective changes between provider squad snapshots.
- --attach-only performs zero provider calls and only republishes cached squad context.
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
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
ODDS_PATH = ROOT / "odds" / "odds_api_io" / "domestic_odds.json"
ALIASES_PATH = ROOT / "mappings" / "domestic_team_aliases.json"
CACHE_PATH = ROOT / "data" / "statmaker" / "domestic_match_context_cache.json"
REPORT_PATH = ROOT / "reports" / "domestic_squad_context.json"

BASE_URL = "https://v3.football.api-sports.io"
TIMEOUT_SECONDS = 30
DEFAULT_REQUEST_CAP = 40
DEFAULT_DAILY_QUOTA_RESERVE = 1500
LOOKAHEAD_HOURS = 72
SQUAD_REFRESH_HOURS = 144  # six days; provider recommends roughly weekly refresh
CHANGE_LOOKBACK_DAYS = 45
MIN_LINEUPS_FOR_CORE = 3
MAX_LINEUP_HISTORY = 8


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


def hours_since(value: Any, now: dt.datetime) -> Optional[float]:
    parsed = parse_dt(value)
    if parsed is None:
        return None
    return max(0.0, (now - parsed).total_seconds() / 3600.0)


class ApiBudget:
    def __init__(self, api_key: str, maximum: int, daily_reserve: int, report: Dict[str, Any]):
        self.api_key = api_key
        self.maximum = max(0, maximum)
        self.daily_reserve = max(0, daily_reserve)
        self.used = 0
        self.daily_limit: Optional[int] = None
        self.daily_remaining: Optional[int] = None
        self.report = report

    @property
    def remaining(self) -> int:
        return max(0, self.maximum - self.used)

    @property
    def quota_safe(self) -> bool:
        return self.daily_remaining is None or self.daily_remaining > self.daily_reserve

    def get(self, path: str, params: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
        if not self.api_key or self.used >= self.maximum or not self.quota_safe:
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
                    "User-Agent": "StatMaker-Data domestic squad context",
                },
                method="GET",
            )
            with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                payload = json.loads(response.read().decode("utf-8"))
                try:
                    self.daily_limit = int(response.headers.get("x-ratelimit-requests-limit") or 0) or self.daily_limit
                except (TypeError, ValueError):
                    pass
                try:
                    self.daily_remaining = int(response.headers.get("x-ratelimit-requests-remaining") or 0)
                except (TypeError, ValueError):
                    pass
            record["dailyRemaining"] = self.daily_remaining
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


def team_candidates(rows: List[Dict[str, Any]], cache: Dict[str, Any], now: dt.datetime) -> List[Dict[str, Any]]:
    mappings = cache.setdefault("matchMappings", {})
    upper = now + dt.timedelta(hours=LOOKAHEAD_HOURS)
    lower = now - dt.timedelta(minutes=20)
    by_team: Dict[int, Dict[str, Any]] = {}
    for row in rows:
        if row["kickoff"] < lower or row["kickoff"] > upper:
            continue
        mapping = mappings.get(row["key"]) or {}
        for side, id_key, name in (
            ("home", "homeTeamId", row["home"]),
            ("away", "awayTeamId", row["away"]),
        ):
            try:
                team_id = int(mapping.get(id_key) or 0)
            except (TypeError, ValueError):
                continue
            if team_id <= 0:
                continue
            existing = by_team.get(team_id)
            candidate = {
                "teamId": team_id,
                "teamName": name,
                "side": side,
                "kickoff": row["kickoff"],
            }
            if existing is None or row["kickoff"] < existing["kickoff"]:
                by_team[team_id] = candidate
    return sorted(by_team.values(), key=lambda item: item["kickoff"])


def parse_squad(items: Optional[List[Dict[str, Any]]], team_id: int) -> Optional[List[Dict[str, Any]]]:
    for item in items or []:
        team = item.get("team") or {}
        try:
            response_team_id = int(team.get("id") or 0)
        except (TypeError, ValueError):
            response_team_id = 0
        if response_team_id != team_id:
            continue
        players: List[Dict[str, Any]] = []
        for player in item.get("players", []) or []:
            try:
                player_id = int(player.get("id") or 0)
            except (TypeError, ValueError):
                continue
            if player_id <= 0:
                continue
            players.append({
                "id": player_id,
                "name": str(player.get("name") or "").strip(),
                "position": str(player.get("position") or "").strip() or None,
            })
        if players:
            return players
    return None


def core_continuity(cache: Dict[str, Any], team_id: int, current_players: List[Dict[str, Any]]) -> Tuple[Optional[float], int]:
    rows = list((cache.setdefault("teamLineups", {}).get(str(team_id)) or []))
    rows.sort(key=lambda row: str(row.get("kickoff") or ""), reverse=True)
    rows = rows[:MAX_LINEUP_HISTORY]
    if len(rows) < MIN_LINEUPS_FOR_CORE:
        return None, 0
    counts: Counter[int] = Counter()
    for row in rows:
        counts.update(
            int(player.get("id") or 0)
            for player in row.get("starters", []) or []
            if int(player.get("id") or 0) > 0
        )
    core = [player_id for player_id, _ in counts.most_common(11) if player_id > 0]
    if not core:
        return None, 0
    current = {int(player.get("id") or 0) for player in current_players if int(player.get("id") or 0) > 0}
    return round(len(set(core).intersection(current)) / len(core), 4), len(core)


def recent_changes(previous: Dict[str, Any], new_players: List[Dict[str, Any]], now: dt.datetime) -> Tuple[int, int, Optional[str]]:
    old_ids = {int(player.get("id") or 0) for player in previous.get("players", []) or [] if int(player.get("id") or 0) > 0}
    new_ids = {int(player.get("id") or 0) for player in new_players if int(player.get("id") or 0) > 0}
    if old_ids:
        arrivals = len(new_ids - old_ids)
        departures = len(old_ids - new_ids)
        if arrivals or departures:
            return arrivals, departures, iso_utc(now)

    changed_at = parse_dt(previous.get("lastChangeAt"))
    if changed_at is not None and changed_at >= now - dt.timedelta(days=CHANGE_LOOKBACK_DAYS):
        return (
            int(previous.get("recentArrivals") or 0),
            int(previous.get("recentDepartures") or 0),
            iso_utc(changed_at),
        )
    return 0, 0, None


def refresh_squads(
    candidates: List[Dict[str, Any]],
    cache: Dict[str, Any],
    api: ApiBudget,
    now: dt.datetime,
    report: Dict[str, Any],
) -> None:
    catalog = cache.setdefault("teamSquads", {})
    eligible: List[Dict[str, Any]] = []
    for candidate in candidates:
        current = catalog.get(str(candidate["teamId"])) or {}
        elapsed = hours_since(current.get("queriedAt"), now)
        if elapsed is None or elapsed >= SQUAD_REFRESH_HOURS:
            eligible.append(candidate)

    report["eligibleTeams"] = len(eligible)
    for candidate in eligible:
        if api.remaining <= 0 or not api.quota_safe:
            report["deferredTeams"] += 1
            continue
        team_id = int(candidate["teamId"])
        items = api.get("/players/squads", {"team": team_id})
        players = parse_squad(items, team_id)
        if not players:
            continue
        previous = catalog.get(str(team_id)) or {}
        arrivals, departures, changed_at = recent_changes(previous, players, now)
        continuity, sample = core_continuity(cache, team_id, players)
        catalog[str(team_id)] = {
            "teamId": team_id,
            "teamName": candidate["teamName"],
            "queriedAt": iso_utc(now),
            "players": players,
            "squadContinuity": continuity,
            "squadContinuitySample": sample,
            "recentArrivals": arrivals,
            "recentDepartures": departures,
            "lastChangeAt": changed_at,
        }
        report["squadsRefreshed"] += 1
        if continuity is not None:
            report["squadsWithCoreContinuity"] += 1


def attach_squad_context(
    rows: List[Dict[str, Any]],
    cache: Dict[str, Any],
    now: dt.datetime,
    report: Dict[str, Any],
) -> None:
    mappings = cache.setdefault("matchMappings", {})
    catalog = cache.setdefault("teamSquads", {})
    generated_at = iso_utc(now)
    for row in rows:
        mapping = mappings.get(row["key"]) or {}
        if not mapping.get("fixtureId"):
            continue
        match = row["match"]
        context = match.get("squadContext")
        if not isinstance(context, dict):
            context = {}
            match["squadContext"] = context
        context["apiFootballFixtureId"] = int(mapping.get("fixtureId"))
        context["generatedAt"] = generated_at

        attached = False
        for side, id_key in (("home", "homeTeamId"), ("away", "awayTeamId")):
            try:
                team_id = int(mapping.get(id_key) or 0)
            except (TypeError, ValueError):
                team_id = 0
            if team_id <= 0:
                continue
            snapshot = catalog.get(str(team_id)) or {}
            if not snapshot.get("players"):
                continue
            team = context.get(side)
            if not isinstance(team, dict):
                team = {}
                context[side] = team
            team["teamId"] = team_id
            team["squadCovered"] = True
            team["squadContinuity"] = snapshot.get("squadContinuity")
            team["squadContinuitySample"] = int(snapshot.get("squadContinuitySample") or 0)
            team["recentArrivals"] = int(snapshot.get("recentArrivals") or 0)
            team["recentDepartures"] = int(snapshot.get("recentDepartures") or 0)
            attached = True
        if attached:
            report["matchesWithSquadContext"] += 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--attach-only", action="store_true")
    args = parser.parse_args()

    try:
        request_cap = max(0, int(os.getenv("DOMESTIC_SQUAD_API_REQUEST_CAP", str(DEFAULT_REQUEST_CAP))))
    except ValueError:
        request_cap = DEFAULT_REQUEST_CAP
    try:
        daily_reserve = max(0, int(os.getenv("API_FOOTBALL_DAILY_RESERVE", str(DEFAULT_DAILY_QUOTA_RESERVE))))
    except ValueError:
        daily_reserve = DEFAULT_DAILY_QUOTA_RESERVE

    feed = load(ODDS_PATH, {})
    aliases = build_alias_lookup(load(ALIASES_PATH, {}))
    cache = load(CACHE_PATH, {"schemaVersion": 1, "matchMappings": {}, "teamLineups": {}, "teamSquads": {}})
    now = now_utc()
    report: Dict[str, Any] = {
        "generatedAt": iso_utc(now),
        "attachOnly": bool(args.attach_only),
        "requestCap": 0 if args.attach_only else request_cap,
        "dailyQuotaReserve": daily_reserve,
        "requestsUsed": 0,
        "dailyQuotaLimit": None,
        "dailyQuotaRemaining": None,
        "candidateTeams": 0,
        "eligibleTeams": 0,
        "deferredTeams": 0,
        "squadsRefreshed": 0,
        "squadsWithCoreContinuity": 0,
        "matchesWithSquadContext": 0,
        "calls": [],
        "policy": {
            "lookaheadHours": LOOKAHEAD_HOURS,
            "refreshHours": SQUAD_REFRESH_HOURS,
            "changeLookbackDays": CHANGE_LOOKBACK_DAYS,
            "minimumConfirmedLineupsForCore": MIN_LINEUPS_FOR_CORE,
            "squadSource": "API-Football /players/squads",
            "strengthDirection": "none; turnover only dampens historical confidence in app model",
        },
    }

    if not isinstance(feed, dict) or not feed.get("leagues"):
        report["status"] = "skipped_empty_feed"
        save(REPORT_PATH, report)
        return 0

    rows = feed_rows(feed, aliases)
    candidates = team_candidates(rows, cache, now)
    report["candidateTeams"] = len(candidates)

    if not args.attach_only:
        api_key = os.getenv("API_FOOTBALL_KEY", "").strip()
        if api_key and request_cap > 0:
            api = ApiBudget(api_key, request_cap, daily_reserve, report)
            refresh_squads(candidates, cache, api, now, report)
            report["requestsUsed"] = api.used
            report["dailyQuotaLimit"] = api.daily_limit
            report["dailyQuotaRemaining"] = api.daily_remaining
        else:
            report["status"] = "attach_only_missing_key_or_zero_cap"

    attach_squad_context(rows, cache, now, report)
    save(ODDS_PATH, feed)
    save(CACHE_PATH, cache)
    report["cachedSquads"] = sum(
        1 for snapshot in (cache.get("teamSquads") or {}).values() if snapshot.get("players")
    )
    report["requestCapRespected"] = report["requestsUsed"] <= report["requestCap"]
    report.setdefault("status", "ok")
    save(REPORT_PATH, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
