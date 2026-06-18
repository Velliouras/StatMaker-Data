#!/usr/bin/env python3
"""Fetch normalized World Cup odds from Odds-API.io.

Outputs:
  odds/odds_api_io/world_cup_odds.json
  odds/odds_api_io/debug_report.json

Policy:
  - No scraping.
  - API key must come from ODDS_API_IO_KEY.
  - Single-bet odds are emitted only when the API market is mapped with high confidence.
  - No approximate/inferred odds.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

SCRIPT_VERSION = "odds-api-io-wc-v2-full-statmaker-market-discovery"
BASE_URL = "https://api.odds-api.io/v3"
SPORT = "football"
DEFAULT_BOOKMAKERS = "Bet365,Unibet"
MAIN_TOTAL_LINES = {1.5, 2.5, 3.5}
MAX_EVENTS_PER_MULTI_CALL = 10

DISCOVERY_TARGETS = [
    "1X2",
    "MATCH_GOALS",
    "BTTS",
    "TEAM_TOTAL_GOALS",
    "CORNERS",
    "CARDS",
    "SHOTS",
    "SHOTS_ON_TARGET",
]


ROOT = Path(__file__).resolve().parents[1]
FIXTURES_PATH = ROOT / "world-cup" / "world_cup_2026.json"
OUT_DIR = ROOT / "odds" / "odds_api_io"
OUTPUT_PATH = OUT_DIR / "world_cup_odds.json"
DEBUG_PATH = OUT_DIR / "debug_report.json"

TEAM_ALIASES = {
    "usa": "united states",
    "us": "united states",
    "u s a": "united states",
    "united states of america": "united states",
    "czech republic": "czechia",
    "bosnia herzegovina": "bosnia and herzegovina",
    "bosnia & herzegovina": "bosnia and herzegovina",
    "dr congo": "congo dr",
    "d r congo": "congo dr",
    "democratic republic of congo": "congo dr",
    "ivory coast": "cote divoire",
    "côte divoire": "cote divoire",
    "curacao": "curacao",
    "curaçao": "curacao",
    "south korea": "korea republic",
    "korea republic": "korea republic",
    "turkey": "turkiye",
    "turkiye": "turkiye",
    "saudi arabia": "saudi arabia",
    "cape verde": "cape verde",
    "new zealand": "new zealand",
}


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return TEAM_ALIASES.get(text, text)


def team_pair_key(home: str, away: str) -> Tuple[str, str]:
    return normalize_text(home), normalize_text(away)


def to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        raw = str(value).strip().replace(",", ".")
        if not raw:
            return None
        try:
            number = float(raw)
        except ValueError:
            return None
    if not (1.01 <= number <= 1000.0):
        return None
    return round(number, 4)


def line_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return round(float(str(value).strip().replace(",", ".")), 2)
    except (TypeError, ValueError):
        return None


def compact(value: Any, limit: int = 1200) -> Any:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        text = str(value)
    if len(text) <= limit:
        return value
    return text[:limit] + "..."


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def api_get(path: str, params: Dict[str, Any], debug: Dict[str, Any], *, allow_error: bool = False) -> Any:
    url = f"{BASE_URL}{path}?{urlencode({k: v for k, v in params.items() if v is not None})}"
    safe_params = dict(params)
    if "apiKey" in safe_params:
        safe_params["apiKey"] = "***"
    started = time.time()
    record: Dict[str, Any] = {"path": path, "params": safe_params}
    try:
        req = Request(url, headers={"User-Agent": "StatMaker-Data/1.0"})
        with urlopen(req, timeout=45) as response:
            body = response.read().decode("utf-8")
            record.update(
                {
                    "status": response.status,
                    "durationMs": int((time.time() - started) * 1000),
                    "rateLimitLimit": response.headers.get("x-ratelimit-limit"),
                    "rateLimitRemaining": response.headers.get("x-ratelimit-remaining"),
                    "rateLimitReset": response.headers.get("x-ratelimit-reset"),
                    "bodyLength": len(body),
                }
            )
            data = json.loads(body) if body else None
            record["sample"] = compact(data, 1800)
            debug.setdefault("apiCalls", []).append(record)
            return data
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        record.update(
            {
                "status": exc.code,
                "durationMs": int((time.time() - started) * 1000),
                "error": body[:2000] or str(exc),
            }
        )
        debug.setdefault("apiCalls", []).append(record)
        if allow_error:
            return None
        raise RuntimeError(f"Odds-API.io HTTP {exc.code} for {path}: {body[:300]}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        record.update({"durationMs": int((time.time() - started) * 1000), "error": str(exc)})
        debug.setdefault("apiCalls", []).append(record)
        if allow_error:
            return None
        raise RuntimeError(f"Odds-API.io request failed for {path}: {exc}") from exc


def load_fixtures() -> List[Dict[str, Any]]:
    data = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))
    return list(data.get("matches") or [])


def is_unplayed(match: Dict[str, Any]) -> bool:
    status = normalize_text(match.get("status"))
    if status in {"finished", "complete", "completed", "settled"}:
        return False
    return match.get("homeGoals") is None and match.get("awayGoals") is None


def fixture_date_range(fixtures: List[Dict[str, Any]]) -> Tuple[str, str]:
    dates: List[dt.date] = []
    for match in fixtures:
        date_raw = match.get("date")
        if not date_raw:
            continue
        try:
            dates.append(dt.date.fromisoformat(str(date_raw)))
        except ValueError:
            continue
    if not dates:
        today = dt.datetime.now(dt.timezone.utc).date()
        return f"{today.isoformat()}T00:00:00Z", f"{(today + dt.timedelta(days=14)).isoformat()}T23:59:59Z"
    start = min(dates) - dt.timedelta(days=2)
    end = max(dates) + dt.timedelta(days=2)
    return f"{start.isoformat()}T00:00:00Z", f"{end.isoformat()}T23:59:59Z"


def discover_leagues(api_key: str, debug: Dict[str, Any]) -> List[str]:
    override = os.getenv("ODDS_API_IO_LEAGUES", "").strip()
    if override:
        leagues = [x.strip() for x in override.split(",") if x.strip()]
        debug["leagueDiscovery"] = {"mode": "env_override", "selected": leagues}
        return leagues

    data = api_get("/leagues", {"apiKey": api_key, "sport": SPORT, "all": "true"}, debug, allow_error=True)
    leagues = data if isinstance(data, list) else []
    candidates: List[str] = []
    samples = []
    for item in leagues:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        slug = str(item.get("slug") or "")
        haystack = normalize_text(f"{name} {slug}")
        if "world" in haystack and ("cup" in haystack or "fifa" in haystack):
            if slug:
                candidates.append(slug)
                samples.append({"name": name, "slug": slug, "eventsCount": item.get("eventsCount")})
    debug["leagueDiscovery"] = {
        "mode": "api",
        "leaguesSeen": len(leagues),
        "selected": candidates[:10],
        "samples": samples[:20],
        "fallback": "no league filter" if not candidates else None,
    }
    return candidates[:5]


def fetch_events(api_key: str, leagues: List[str], fixtures: List[Dict[str, Any]], debug: Dict[str, Any]) -> List[Dict[str, Any]]:
    start, end = fixture_date_range(fixtures)
    base_params = {
        "apiKey": api_key,
        "sport": SPORT,
        "status": "pending,live",
        "from": start,
        "to": end,
        "limit": 5000,
    }
    events: List[Dict[str, Any]] = []
    if leagues:
        for league in leagues:
            params = dict(base_params)
            params["league"] = league
            data = api_get("/events", params, debug, allow_error=True)
            if isinstance(data, list):
                events.extend([x for x in data if isinstance(x, dict)])
    else:
        data = api_get("/events", base_params, debug, allow_error=True)
        if isinstance(data, list):
            events.extend([x for x in data if isinstance(x, dict)])

    seen = set()
    deduped = []
    for event in events:
        event_id = str(event.get("id") or "")
        if not event_id or event_id in seen:
            continue
        seen.add(event_id)
        deduped.append(event)
    debug["eventsFetch"] = {
        "from": start,
        "to": end,
        "eventsFetched": len(deduped),
        "sample": [event_summary(e) for e in deduped[:20]],
    }
    return deduped


def event_summary(event: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": event.get("id"),
        "home": event.get("home"),
        "away": event.get("away"),
        "date": event.get("date"),
        "status": event.get("status"),
        "league": event.get("league"),
        "bookmakerCount": event.get("bookmakerCount"),
    }


def match_events_to_fixtures(fixtures: List[Dict[str, Any]], events: List[Dict[str, Any]], debug: Dict[str, Any]) -> List[Tuple[Dict[str, Any], Dict[str, Any], str]]:
    event_by_pair: Dict[Tuple[str, str], Dict[str, Any]] = {}
    event_by_reverse: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for event in events:
        home = str(event.get("home") or "")
        away = str(event.get("away") or "")
        if not home or not away:
            continue
        pair = team_pair_key(home, away)
        event_by_pair[pair] = event
        event_by_reverse[(pair[1], pair[0])] = event

    matches: List[Tuple[Dict[str, Any], Dict[str, Any], str]] = []
    unmatched = []
    for fixture in fixtures:
        if not is_unplayed(fixture):
            continue
        pair = team_pair_key(str(fixture.get("homeTeam") or ""), str(fixture.get("awayTeam") or ""))
        event = event_by_pair.get(pair)
        if event:
            matches.append((fixture, event, "same"))
            continue
        event = event_by_reverse.get(pair)
        if event:
            matches.append((fixture, event, "reversed"))
            continue
        unmatched.append({
            "matchId": fixture.get("matchId"),
            "homeTeam": fixture.get("homeTeam"),
            "awayTeam": fixture.get("awayTeam"),
            "date": fixture.get("date"),
        })

    debug["eventMatching"] = {
        "unplayedFixtures": sum(1 for f in fixtures if is_unplayed(f)),
        "matched": len(matches),
        "unmatched": len(unmatched),
        "unmatchedSample": unmatched[:30],
        "matchedSample": [
            {
                "matchId": f.get("matchId"),
                "fixture": f"{f.get('homeTeam')} - {f.get('awayTeam')}",
                "eventId": e.get("id"),
                "event": f"{e.get('home')} - {e.get('away')}",
                "orientation": orientation,
            }
            for f, e, orientation in matches[:30]
        ],
    }
    return matches


def fetch_odds_for_matches(api_key: str, matches: List[Tuple[Dict[str, Any], Dict[str, Any], str]], bookmakers: str, debug: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    event_ids = [str(event.get("id")) for _, event, _ in matches if event.get("id") is not None]
    result_by_id: Dict[str, Dict[str, Any]] = {}
    for start in range(0, len(event_ids), MAX_EVENTS_PER_MULTI_CALL):
        chunk = event_ids[start:start + MAX_EVENTS_PER_MULTI_CALL]
        if not chunk:
            continue
        path = "/odds/multi" if len(chunk) > 1 else "/odds"
        params = {"apiKey": api_key, "bookmakers": bookmakers}
        if len(chunk) > 1:
            params["eventIds"] = ",".join(chunk)
        else:
            params["eventId"] = chunk[0]
        data = api_get(path, params, debug, allow_error=True)
        if isinstance(data, list):
            for event_odds in data:
                if isinstance(event_odds, dict) and event_odds.get("id") is not None:
                    result_by_id[str(event_odds.get("id"))] = event_odds
        elif isinstance(data, dict) and data.get("id") is not None:
            result_by_id[str(data.get("id"))] = data
    debug["oddsFetch"] = {
        "requestedEventIds": len(event_ids),
        "eventsWithOddsResponse": len(result_by_id),
        "bookmakersRequested": [x.strip() for x in bookmakers.split(",") if x.strip()],
    }
    return result_by_id


def market_name(market: Dict[str, Any]) -> str:
    parts = [market.get("name"), market.get("market"), market.get("type"), market.get("key")]
    return normalize_text(" ".join(str(p or "") for p in parts))


def raw_market_display_name(market: Dict[str, Any]) -> str:
    return str(market.get("name") or market.get("market") or market.get("type") or market.get("key") or "UNKNOWN")


def discovery_categories(raw_name: str) -> List[str]:
    name = normalize_text(raw_name)
    categories: List[str] = []

    if any(token in name for token in ["ml", "moneyline", "match result", "match winner", "1x2"]):
        categories.append("1X2")

    if any(token in name for token in ["both teams", "btts", "both teams to score"]):
        categories.append("BTTS")

    if "corner" in name or "corners" in name:
        categories.append("CORNERS")

    if any(token in name for token in ["booking", "bookings", "card", "cards", "yellow", "red card"]):
        categories.append("CARDS")

    if any(token in name for token in ["shot on target", "shots on target", "on target"]):
        categories.append("SHOTS_ON_TARGET")
    elif "shot" in name or "shots" in name:
        categories.append("SHOTS")

    if any(token in name for token in ["team total", "team goals", "teamtotal"]):
        categories.append("TEAM_TOTAL_GOALS")
    elif any(token in name for token in ["goals over under", "over under", "total", "totals"]):
        # Corners/cards can also have totals. Keep MATCH_GOALS only when the name is not a non-goal stat market.
        if not any(cat in categories for cat in ["CORNERS", "CARDS", "SHOTS", "SHOTS_ON_TARGET"]):
            categories.append("MATCH_GOALS")

    return categories or ["OTHER"]


def odds_shape_sample(market: Dict[str, Any], limit: int = 500) -> Any:
    rows = market.get("odds")
    if not isinstance(rows, list):
        return compact(rows, limit)
    return compact(rows[:2], limit)


def observe_raw_market(debug: Dict[str, Any], fixture: Dict[str, Any], bookmaker: str, market: Dict[str, Any]) -> None:
    raw_name = raw_market_display_name(market)
    normalized = market_name(market)

    inventory = debug.setdefault("rawMarketInventory", {})
    inv = inventory.setdefault(raw_name, {"count": 0, "bookmakers": {}, "examples": []})
    inv["count"] += 1
    inv["bookmakers"][bookmaker] = inv["bookmakers"].get(bookmaker, 0) + 1
    if len(inv["examples"]) < 3:
        inv["examples"].append({
            "matchId": fixture.get("matchId"),
            "fixture": f"{fixture.get('homeTeam')} - {fixture.get('awayTeam')}",
            "bookmaker": bookmaker,
            "marketName": raw_name,
            "normalizedName": normalized,
            "oddsShape": odds_shape_sample(market),
        })

    discovery = debug.setdefault("statMakerMarketDiscovery", {})
    for category in discovery_categories(raw_name):
        bucket = discovery.setdefault(category, {"rawMarketCount": 0, "marketNames": {}, "examples": []})
        bucket["rawMarketCount"] += 1
        bucket["marketNames"][raw_name] = bucket["marketNames"].get(raw_name, 0) + 1
        if len(bucket["examples"]) < 12:
            bucket["examples"].append({
                "matchId": fixture.get("matchId"),
                "fixture": f"{fixture.get('homeTeam')} - {fixture.get('awayTeam')}",
                "bookmaker": bookmaker,
                "marketName": raw_name,
                "normalizedName": normalized,
                "oddsShape": odds_shape_sample(market),
            })


def finalize_market_discovery(debug: Dict[str, Any]) -> None:
    discovery = debug.setdefault("statMakerMarketDiscovery", {})
    for category in DISCOVERY_TARGETS:
        bucket = discovery.setdefault(category, {"rawMarketCount": 0, "marketNames": {}, "examples": []})
        names = bucket.get("marketNames", {}) if isinstance(bucket.get("marketNames"), dict) else {}
        top_names = sorted(names.items(), key=lambda item: item[1], reverse=True)[:30]
        bucket["status"] = "found" if bucket.get("rawMarketCount", 0) else "not_found"
        bucket["uniqueMarketNames"] = len(names)
        bucket["topMarketNames"] = [{"name": name, "count": count} for name, count in top_names]
        bucket.pop("marketNames", None)

    raw_inventory = debug.get("rawMarketInventory", {})
    if isinstance(raw_inventory, dict):
        top_inventory = []
        for name, entry in sorted(raw_inventory.items(), key=lambda item: item[1].get("count", 0), reverse=True)[:80]:
            top_inventory.append({
                "name": name,
                "count": entry.get("count", 0),
                "bookmakers": entry.get("bookmakers", {}),
                "examples": entry.get("examples", []),
            })
        debug["rawMarketInventoryTop"] = top_inventory
        debug.pop("rawMarketInventory", None)


def extract_1x2(market: Dict[str, Any], fixture: Dict[str, Any], orientation: str, bookmaker: str, debug_bucket: Dict[str, int]) -> List[Dict[str, Any]]:
    name = market_name(market)
    if not any(token in name for token in ["ml", "moneyline", "match result", "match winner", "1x2"]):
        return []
    odds_rows = market.get("odds")
    if not isinstance(odds_rows, list):
        return []
    out = []
    for row in odds_rows:
        if not isinstance(row, dict):
            continue
        home_odd = to_float(row.get("home"))
        draw_odd = to_float(row.get("draw"))
        away_odd = to_float(row.get("away"))
        if home_odd and draw_odd and away_odd:
            if orientation == "same":
                fixture_home_odd, fixture_away_odd = home_odd, away_odd
            else:
                fixture_home_odd, fixture_away_odd = away_odd, home_odd
            base = base_market_meta(market, bookmaker, "odds_api_io_exact_1x2")
            out.extend([
                {**base, "market": "1X2", "selection": "Home Win", "team": fixture.get("homeTeam"), "odds": fixture_home_odd},
                {**base, "market": "1X2", "selection": "Draw", "team": None, "odds": draw_odd},
                {**base, "market": "1X2", "selection": "Away Win", "team": fixture.get("awayTeam"), "odds": fixture_away_odd},
            ])
            debug_bucket["1X2"] = debug_bucket.get("1X2", 0) + 3
            break
    return out


def extract_match_goals(market: Dict[str, Any], bookmaker: str, debug_bucket: Dict[str, int]) -> List[Dict[str, Any]]:
    name = market_name(market)
    if "team total" in name or "teamtotal" in name:
        return []
    if not any(token in name for token in ["over under", "total", "totals", "goals"]):
        return []
    odds_rows = market.get("odds")
    if not isinstance(odds_rows, list):
        return []
    out = []
    for row in odds_rows:
        if not isinstance(row, dict):
            continue
        line = line_float(row.get("max") or row.get("points") or row.get("line") or row.get("handicap") or row.get("hdp"))
        if line not in MAIN_TOTAL_LINES:
            continue
        over_odd = to_float(row.get("over"))
        under_odd = to_float(row.get("under"))
        if over_odd and under_odd:
            base = base_market_meta(market, bookmaker, "odds_api_io_exact_match_goals")
            out.extend([
                {**base, "market": "MATCH_GOALS", "selection": f"Over {line:g} Goals", "line": line, "side": "over", "odds": over_odd},
                {**base, "market": "MATCH_GOALS", "selection": f"Under {line:g} Goals", "line": line, "side": "under", "odds": under_odd},
            ])
            debug_bucket["MATCH_GOALS"] = debug_bucket.get("MATCH_GOALS", 0) + 2
    return out


def extract_btts(market: Dict[str, Any], bookmaker: str, debug_bucket: Dict[str, int]) -> List[Dict[str, Any]]:
    name = market_name(market)
    if not any(token in name for token in ["both teams", "btts", "both teams to score"]):
        return []
    odds_rows = market.get("odds")
    if not isinstance(odds_rows, list):
        return []
    out = []
    for row in odds_rows:
        if not isinstance(row, dict):
            continue
        yes_odd = to_float(row.get("yes"))
        no_odd = to_float(row.get("no"))
        if yes_odd and no_odd:
            base = base_market_meta(market, bookmaker, "odds_api_io_exact_btts")
            out.extend([
                {**base, "market": "BTTS", "selection": "Yes", "odds": yes_odd},
                {**base, "market": "BTTS", "selection": "No", "odds": no_odd},
            ])
            debug_bucket["BTTS"] = debug_bucket.get("BTTS", 0) + 2
            break
    return out


def extract_team_totals(market: Dict[str, Any], fixture: Dict[str, Any], orientation: str, bookmaker: str, debug_bucket: Dict[str, int]) -> List[Dict[str, Any]]:
    name = market_name(market)
    if not any(token in name for token in ["team total", "team goals", "teamtotal"]):
        return []
    odds_rows = market.get("odds")
    if not isinstance(odds_rows, list):
        return []
    out = []
    for row in odds_rows:
        if not isinstance(row, dict):
            continue
        line = line_float(row.get("max") or row.get("points") or row.get("line") or row.get("handicap") or row.get("hdp"))
        if line is None or line not in {0.5, 1.5, 2.5}:
            continue
        side = normalize_text(row.get("team") or row.get("side") or row.get("designation") or market.get("side") or market.get("team"))
        if side in {"home", normalize_text(fixture.get("homeTeam"))}:
            team = fixture.get("homeTeam") if orientation == "same" else fixture.get("awayTeam")
        elif side in {"away", normalize_text(fixture.get("awayTeam"))}:
            team = fixture.get("awayTeam") if orientation == "same" else fixture.get("homeTeam")
        else:
            team = row.get("team") or market.get("team")
        over_odd = to_float(row.get("over"))
        under_odd = to_float(row.get("under"))
        if over_odd and under_odd:
            base = base_market_meta(market, bookmaker, "odds_api_io_exact_team_totals")
            out.extend([
                {**base, "market": "TEAM_TOTAL_GOALS", "selection": f"{team} Over {line:g} Goals", "team": team, "line": line, "side": "over", "odds": over_odd},
                {**base, "market": "TEAM_TOTAL_GOALS", "selection": f"{team} Under {line:g} Goals", "team": team, "line": line, "side": "under", "odds": under_odd},
            ])
            debug_bucket["TEAM_TOTAL_GOALS"] = debug_bucket.get("TEAM_TOTAL_GOALS", 0) + 2
    return out


def base_market_meta(market: Dict[str, Any], bookmaker: str, extraction: str) -> Dict[str, Any]:
    return {
        "bookmaker": bookmaker,
        "sourceMarketName": market.get("name") or market.get("market") or market.get("type"),
        "updatedAt": market.get("updatedAt"),
        "confidence": "high",
        "extraction": extraction,
    }


def normalize_event_odds(fixture: Dict[str, Any], event: Dict[str, Any], orientation: str, odds_event: Dict[str, Any], debug: Dict[str, Any]) -> Dict[str, Any]:
    bookmakers = odds_event.get("bookmakers") if isinstance(odds_event, dict) else None
    markets_out: List[Dict[str, Any]] = []
    raw_market_names: Dict[str, int] = {}
    accepted_counts: Dict[str, int] = {}

    if isinstance(bookmakers, dict):
        for bookmaker, markets in bookmakers.items():
            if not isinstance(markets, list):
                continue
            for market in markets:
                if not isinstance(market, dict):
                    continue
                raw_name = raw_market_display_name(market)
                observe_raw_market(debug, fixture, str(bookmaker), market)
                raw_market_names[raw_name] = raw_market_names.get(raw_name, 0) + 1
                markets_out.extend(extract_1x2(market, fixture, orientation, str(bookmaker), accepted_counts))
                markets_out.extend(extract_match_goals(market, str(bookmaker), accepted_counts))
                markets_out.extend(extract_btts(market, str(bookmaker), accepted_counts))
                markets_out.extend(extract_team_totals(market, fixture, orientation, str(bookmaker), accepted_counts))

    return {
        "matchId": fixture.get("matchId"),
        "homeTeam": fixture.get("homeTeam"),
        "awayTeam": fixture.get("awayTeam"),
        "date": fixture.get("date"),
        "time": fixture.get("time"),
        "status": fixture.get("status"),
        "sourceEvent": {
            "id": event.get("id"),
            "home": event.get("home"),
            "away": event.get("away"),
            "date": event.get("date"),
            "status": event.get("status"),
            "league": event.get("league"),
            "orientation": orientation,
        },
        "markets": markets_out,
        "debug": {
            "rawMarketCounts": raw_market_names,
            "acceptedMarketCounts": accepted_counts,
            "bookmakersSeen": list(bookmakers.keys()) if isinstance(bookmakers, dict) else [],
        },
    }


def main() -> int:
    debug: Dict[str, Any] = {
        "source": "odds_api_io",
        "generatedAt": now_utc(),
        "scriptVersion": SCRIPT_VERSION,
        "fixturesPath": str(FIXTURES_PATH.relative_to(ROOT)),
        "outputPath": str(OUTPUT_PATH.relative_to(ROOT)),
        "debugPath": str(DEBUG_PATH.relative_to(ROOT)),
        "policy": {
            "singleOdds": "exact_api_mapped_odds_only",
            "noApproximateSingles": True,
            "emittedMarkets": ["1X2", "MATCH_GOALS", "BTTS", "TEAM_TOTAL_GOALS"],
            "discoveryTargets": DISCOVERY_TARGETS,
            "matchGoalsLines": sorted(MAIN_TOTAL_LINES),
            "note": "Discovery is diagnostic-only for corners/cards/shots/SOT; only emittedMarkets are written as normalized odds until mapping is approved.",
        },
    }

    try:
        api_key = os.getenv("ODDS_API_IO_KEY", "").strip()
        if not api_key:
            raise RuntimeError("Missing required environment variable ODDS_API_IO_KEY")
        bookmakers = os.getenv("ODDS_API_IO_BOOKMAKERS", DEFAULT_BOOKMAKERS).strip() or DEFAULT_BOOKMAKERS
        debug["bookmakersRequested"] = [x.strip() for x in bookmakers.split(",") if x.strip()]

        fixtures = load_fixtures()
        unplayed = [f for f in fixtures if is_unplayed(f)]
        debug["fixturesLoaded"] = len(fixtures)
        debug["unplayedFixtures"] = len(unplayed)

        leagues = discover_leagues(api_key, debug)
        events = fetch_events(api_key, leagues, unplayed or fixtures, debug)
        matched = match_events_to_fixtures(fixtures, events, debug)
        odds_by_event_id = fetch_odds_for_matches(api_key, matched, bookmakers, debug)

        normalized_matches = []
        global_counts: Dict[str, int] = {}
        matches_with_markets = 0
        for fixture, event, orientation in matched:
            event_id = str(event.get("id"))
            odds_event = odds_by_event_id.get(event_id)
            if not odds_event:
                continue
            normalized = normalize_event_odds(fixture, event, orientation, odds_event, debug)
            if normalized["markets"]:
                matches_with_markets += 1
            for market in normalized["markets"]:
                m = market.get("market")
                global_counts[m] = global_counts.get(m, 0) + 1
            normalized_matches.append(normalized)

        output = {
            "source": "odds_api_io",
            "provider": "Odds-API.io",
            "generatedAt": debug["generatedAt"],
            "scriptVersion": SCRIPT_VERSION,
            "oddsPolicy": debug["policy"],
            "bookmakersRequested": debug["bookmakersRequested"],
            "marketsRequested": ["1X2", "MATCH_GOALS", "BTTS", "TEAM_TOTAL_GOALS"],
            "matches": normalized_matches,
        }
        debug["summary"] = {
            "matchesNormalized": len(normalized_matches),
            "matchesWithMarkets": matches_with_markets,
            "marketsFound": sum(global_counts.values()),
            "marketCounts": global_counts,
        }
        finalize_market_discovery(debug)
        output["marketDiscoverySummary"] = {
            key: {
                "status": value.get("status"),
                "rawMarketCount": value.get("rawMarketCount"),
                "uniqueMarketNames": value.get("uniqueMarketNames"),
                "topMarketNames": value.get("topMarketNames", [])[:10],
            }
            for key, value in debug.get("statMakerMarketDiscovery", {}).items()
            if key in DISCOVERY_TARGETS
        }
        write_json(OUTPUT_PATH, output)
        write_json(DEBUG_PATH, debug)
        return 0
    except Exception as exc:
        debug["error"] = str(exc)
        empty_output = {
            "source": "odds_api_io",
            "provider": "Odds-API.io",
            "generatedAt": debug["generatedAt"],
            "scriptVersion": SCRIPT_VERSION,
            "oddsPolicy": debug.get("policy", {}),
            "matches": [],
            "error": str(exc),
        }
        write_json(OUTPUT_PATH, empty_output)
        write_json(DEBUG_PATH, debug)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
