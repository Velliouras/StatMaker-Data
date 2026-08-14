#!/usr/bin/env python3
"""Fetch Domestic upcoming schedule + exact odds from Odds-API.io.

Domestic source contract:
  - API-Football is the only active Domestic history/statistics source.
  - Football-Data CSV is inactive archive/reference only.
  - Odds-API.io is the only active Domestic odds source.
  - The Android app reads repository JSON artifacts and must not call either API directly.

Outputs:
  odds/odds_api_io/domestic_odds.json
  reports/domestic_odds_debug.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from odds_api_io_market_audit import (
    AUDIT_ONLY_FAMILIES,
    market_audit_report,
    provider_market_text,
    record_market_audit,
    run_market_audit_self_check,
)

SCRIPT_VERSION = "domestic-odds-api-io-v5-strict-provider-country-market-audit"
BASE_URL = "https://api.odds-api.io/v3"
SPORT = "football"
DEFAULT_BOOKMAKERS = "Bet365,Unibet"
RATE_LIMIT_STOP_BELOW = 20
MAX_EVENTS_PER_MULTI_CALL = 10

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "domestic_leagues.json"
ALIASES_PATH = ROOT / "mappings" / "domestic_team_aliases.json"
OUT_PATH = ROOT / "odds" / "odds_api_io" / "domestic_odds.json"
REPORT_PATH = ROOT / "reports" / "domestic_odds_debug.json"

SUPPORTED_MARKETS = {
    "1X2",
    "MATCH_GOALS",
    "FIRST_HALF_GOALS",
    "BTTS",
    "TEAM_TOTAL_GOALS",
    "MATCH_CORNERS",
    "TEAM_CORNERS",
    "MATCH_CARDS",
    "TEAM_CARDS",
    "MATCH_SHOTS",
    "TEAM_SHOTS",
    "MATCH_SHOTS_ON_TARGET",
    "TEAM_SHOTS_ON_TARGET",
    "DOUBLE_CHANCE",
}
EMITTED_MARKET_COUNT_KEYS = [
    "1X2",
    "BTTS",
    "MATCH_GOALS",
    "TEAM_TOTAL_GOALS",
    "FIRST_HALF_GOALS",
    "MATCH_CORNERS",
    "TEAM_CORNERS",
    "MATCH_CARDS",
    "TEAM_CARDS",
    "MATCH_SHOTS",
    "TEAM_SHOTS",
    "MATCH_SHOTS_ON_TARGET",
    "TEAM_SHOTS_ON_TARGET",
    "DOUBLE_CHANCE",
]
COMMON_SUFFIXES = {"fc", "fk", "cf", "sc", "ac", "afc", "bk", "if"}
PROVIDER_EXCLUSION_QUALIFIERS = {
    "simulated", "reality", "srl", "virtual", "women", "reserves", "reserve",
    "u23", "u21", "u20", "u19", "youth", "friendly", "cup",
}

COUNTRY_ALIASES = {
    "usa": ["usa", "united states", "mls"],
    "england": ["england", "english"],
    "scotland": ["scotland", "scottish"],
    "germany": ["germany", "german"],
    "italy": ["italy", "italian"],
    "spain": ["spain", "spanish"],
    "france": ["france", "french"],
    "netherlands": ["netherlands", "dutch", "holland"],
    "belgium": ["belgium", "belgian"],
    "portugal": ["portugal", "portuguese"],
    "turkey": ["turkey", "turkish"],
    "greece": ["greece", "greek"],
    "argentina": ["argentina", "argentine"],
    "austria": ["austria", "austrian"],
    "brazil": ["brazil", "brasileirao", "brasileiro"],
    "china": ["china", "chinese"],
    "denmark": ["denmark", "danish"],
    "finland": ["finland", "finnish"],
    "ireland": ["ireland", "republic of ireland"],
    "japan": ["japan", "japanese", "j1"],
    "mexico": ["mexico", "mexican"],
    "norway": ["norway", "norwegian"],
    "poland": ["poland", "polish"],
    "romania": ["romania", "romanian"],
    "russia": ["russia", "russian"],
    "sweden": ["sweden", "swedish"],
    "switzerland": ["switzerland", "swiss"],
}


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_text(value: Any, *, drop_suffixes: bool = False) -> str:
    text = str(value or "").strip().lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    words = [w for w in re.sub(r"\s+", " ", text).strip().split(" ") if w]
    if drop_suffixes:
        words = [w for w in words if w not in COMMON_SUFFIXES]
    return " ".join(words)


def to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        try:
            number = float(str(value).strip().replace(",", "."))
        except ValueError:
            return None
    if not (1.01 <= number <= 1000.0):
        return None
    return round(number, 4)


def line_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return round(float(str(value).strip().replace(",", ".")), 2)
    except (TypeError, ValueError):
        return None


def is_half_line(value: Optional[float]) -> bool:
    if value is None:
        return False
    return abs((value * 10) % 10 - 5) < 0.0001


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def compact(value: Any, limit: int = 1200) -> Any:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        text = str(value)
    return value if len(text) <= limit else text[:limit] + "..."


def registry_summary(config: Dict[str, Any]) -> Dict[str, Any]:
    leagues = config.get("leagues", []) if isinstance(config.get("leagues"), list) else []
    enabled = [x for x in leagues if bool(x.get("enabled", True))]
    odds_enabled = [x for x in enabled if bool(x.get("enabledForOdds", True))]
    betting_enabled = [x for x in enabled if bool(x.get("enabledForBetting", True))]
    return {
        "registryVersion": config.get("version"),
        "registryLeagueCount": len(leagues),
        "enabledLeagueCount": len(enabled),
        "enabledForOddsCount": len(odds_enabled),
        "enabledForBettingCount": len(betting_enabled),
        "csvImport": "inactive_archive_only",
        "statsSource": "api-football",
        "oddsSource": "odds-api-io",
    }


def output_debug(generated_at: str, debug: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "generatedAt": generated_at,
        "scriptVersion": SCRIPT_VERSION,
        "registry": debug.get("registry", {}),
        "dryRun": debug.get("dryRun", False),
        "bookmakersRequested": debug.get("bookmakersRequested", []),
        "leaguesRequested": debug.get("leaguesRequested", []),
        "leaguesMatched": debug.get("leaguesMatched", []),
        "leaguesMissing": debug.get("leaguesMissing", []),
        "leagueReports": debug.get("leagueReports", []),
        "unmatchedTeams": unique_unmatched_teams(debug.get("unmatchedTeams", [])),
        "rawMarketCounts": debug.get("rawMarketCounts", {}),
        "classifiedMarketCounts": debug.get("classifiedMarketCounts", {}),
        "skippedMarketReasons": skipped_market_reasons(debug),
        "skippedMarketSummary": skipped_market_summary(debug),
        "skippedMarketExamples": debug.get("skippedMarketExamples", []),
        "emittedMarketCounts": debug.get("emittedMarketCounts", {key: 0 for key in EMITTED_MARKET_COUNT_KEYS}),
        **market_audit_report(debug),
        "marketAuditSelfCheck": debug.get("marketAuditSelfCheck"),
        "rateLimitRemaining": debug.get("rateLimitRemaining"),
        "warnings": debug.get("warnings", []),
    }


def empty_output(generated_at: str, debug: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "schemaVersion": 3,
        "source": "odds-api-io",
        "provider": "Odds-API.io",
        "generatedAt": generated_at,
        "dataContract": {
            "statsSource": "API-Football domestic history/stat JSON",
            "oddsSource": "Odds-API.io exact bookmaker odds",
            "csvImport": "inactive_archive_only",
            "appRule": "Android app reads repository JSON only; it must not call Odds-API.io directly.",
            "emptyState": "If no valid exact odds + historical support exists, show only: Δεν βρέθηκαν αγορές",
        },
        "registry": debug.get("registry", {}),
        "leagues": [],
        "debug": output_debug(generated_at, debug),
    }


def update_rate_limit(debug: Dict[str, Any], headers: Any) -> None:
    remaining = headers.get("x-ratelimit-remaining") or headers.get("X-RateLimit-Remaining")
    limit = headers.get("x-ratelimit-limit") or headers.get("X-RateLimit-Limit")
    reset = headers.get("x-ratelimit-reset") or headers.get("X-RateLimit-Reset")
    debug["rateLimitLimit"] = limit
    debug["rateLimitReset"] = reset
    if remaining is not None:
        try:
            debug["rateLimitRemaining"] = int(str(remaining))
        except ValueError:
            debug["rateLimitRemaining"] = remaining


def should_stop_for_rate_limit(debug: Dict[str, Any]) -> bool:
    remaining = debug.get("rateLimitRemaining")
    return isinstance(remaining, int) and remaining < RATE_LIMIT_STOP_BELOW


def api_get(path: str, params: Dict[str, Any], debug: Dict[str, Any], *, allow_error: bool = True) -> Any:
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
            update_rate_limit(debug, response.headers)
            data = json.loads(body) if body else None
            record.update({
                "status": response.status,
                "durationMs": int((time.time() - started) * 1000),
                "bodyLength": len(body),
                "rateLimitRemaining": debug.get("rateLimitRemaining"),
                "sample": compact(data, 1400),
            })
            debug.setdefault("apiCalls", []).append(record)
            return data
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        update_rate_limit(debug, exc.headers)
        record.update({
            "status": exc.code,
            "durationMs": int((time.time() - started) * 1000),
            "rateLimitRemaining": debug.get("rateLimitRemaining"),
            "error": body[:1600] or str(exc),
        })
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


def load_aliases() -> Dict[str, Dict[str, str]]:
    if not ALIASES_PATH.exists():
        return {}
    raw = read_json(ALIASES_PATH).get("aliases", {})
    mapping: Dict[str, Dict[str, str]] = {}
    for league_code, teams in raw.items():
        league_map: Dict[str, str] = {}
        for canonical, aliases in teams.items():
            league_map[normalize_text(canonical, drop_suffixes=True)] = canonical
            for alias in aliases or []:
                league_map[normalize_text(alias, drop_suffixes=True)] = canonical
        mapping[league_code] = league_map
    return mapping


TEAM_NAME_PREFIX_TOKENS = {
    "club", "clube", "deportivo", "deportes", "sporting", "atletico",
    "association", "asociacion", "fotbal", "fotboll", "football",
    "sociedad", "racing", "royal", "real", "cd", "cs", "acs", "asc",
    "rks", "wks", "kks", "ks", "lkp", "gks", "afk",
}


def simplified_team_name(value: Any) -> str:
    words = normalize_text(value, drop_suffixes=True).split()
    while len(words) > 1 and words[0] in TEAM_NAME_PREFIX_TOKENS:
        words.pop(0)
    while len(words) > 1 and words[-1].isdigit() and len(words[-1]) == 4:
        words.pop()
    return " ".join(words)


def record_unmatched_team(debug: Dict[str, Any], league_code: str, provider_team: str, normalized: str) -> None:
    debug.setdefault("unmatchedTeams", []).append({
        "leagueCode": league_code,
        "providerTeam": provider_team,
        "normalized": normalized,
    })


def canonical_team_info(name: str, league_code: str, aliases: Dict[str, Dict[str, str]], debug: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    normalized = normalize_text(name, drop_suffixes=True)
    simplified = simplified_team_name(normalized)
    league_aliases = aliases.get(league_code, {})
    for candidate in dict.fromkeys([normalized, simplified]):
        if candidate and candidate in league_aliases:
            canonical = league_aliases[candidate]
            return canonical, canonical
    record_unmatched_team(debug, league_code, str(name or "").strip(), normalized)
    provider_name = str(name or "").strip()
    return provider_name, None

def choose_leagues(config: Dict[str, Any], mode: str, target: str) -> List[Dict[str, Any]]:
    all_leagues = config.get("leagues", []) if isinstance(config.get("leagues"), list) else []
    leagues = [
        x for x in all_leagues
        if bool(x.get("enabled", True)) and bool(x.get("enabledForOdds", True))
    ]
    if mode == "league":
        return [x for x in leagues if str(x.get("leagueCode", "")).upper() == target.upper()]
    if mode == "group":
        groups = config.get("groups", {}) if isinstance(config.get("groups"), dict) else {}
        codes = {str(x).upper() for x in groups.get(target, [])}
        if not codes:
            codes = {str(x.get("leagueCode", "")).upper() for x in leagues if x.get("group") == target}
        return [x for x in leagues if str(x.get("leagueCode", "")).upper() in codes]
    return leagues


def discover_provider_leagues(api_key: str, debug: Dict[str, Any]) -> List[Dict[str, Any]]:
    data = api_get("/leagues", {"apiKey": api_key, "sport": SPORT, "all": "true"}, debug, allow_error=True)
    if not isinstance(data, list):
        debug.setdefault("warnings", []).append("League discovery returned no list from Odds-API.io.")
        return []
    provider = [x for x in data if isinstance(x, dict)]
    debug["providerLeagueCount"] = len(provider)
    debug["providerLeagueSample"] = [provider_league_summary(x) for x in provider[:50]]
    return provider


def provider_league_summary(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": item.get("name"),
        "slug": item.get("slug"),
        "sport": item.get("sport"),
        "eventsCount": item.get("eventsCount"),
    }


def country_aliases_for(country: Any) -> List[str]:
    key = normalize_text(country)
    return COUNTRY_ALIASES.get(key, [key])


def provider_country_matches(config_league: Dict[str, Any], provider_item: Dict[str, Any]) -> bool:
    haystack = normalize_text(f"{provider_item.get('name', '')} {provider_item.get('slug', '')}")
    aliases = [normalize_text(x) for x in country_aliases_for(config_league.get("country")) if normalize_text(x)]
    return any(alias and alias in haystack for alias in aliases)


def provider_has_unrequested_qualifier(config_league: Dict[str, Any], provider_item: Dict[str, Any]) -> bool:
    requested = normalize_text(
        f"{config_league.get('competition', '')} {' '.join(config_league.get('searchTerms', []) or [])}"
    )
    haystack = normalize_text(f"{provider_item.get('name', '')} {provider_item.get('slug', '')}")
    return any(
        qualifier in haystack and qualifier not in requested
        for qualifier in PROVIDER_EXCLUSION_QUALIFIERS
    )


def match_provider_league(config_league: Dict[str, Any], provider_leagues: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    configured_slug = str(config_league.get("providerLeagueSlug") or "").strip()
    if configured_slug:
        for item in provider_leagues:
            if (
                str(item.get("slug") or "") == configured_slug
                and provider_country_matches(config_league, item)
                and not provider_has_unrequested_qualifier(config_league, item)
            ):
                return item
        return None

    search_terms = [normalize_text(x) for x in config_league.get("searchTerms", [])]
    best: Tuple[int, Optional[Dict[str, Any]]] = (0, None)
    for item in provider_leagues:
        if not provider_country_matches(config_league, item):
            continue
        if provider_has_unrequested_qualifier(config_league, item):
            continue
        haystack = normalize_text(f"{item.get('name', '')} {item.get('slug', '')}")
        score = 0
        for term in search_terms:
            if not term:
                continue
            if term in haystack:
                score = max(score, 100 + len(term))
            else:
                words = [w for w in term.split() if len(w) > 2]
                hits = sum(1 for w in words if w in haystack)
                if words and hits == len(words):
                    score = max(score, 70 + hits)
                elif hits >= 2:
                    score = max(score, 20 + hits)
        if score > best[0]:
            best = (score, item)
    return best[1] if best[0] >= 50 else None


def iso_window(horizon_days: int) -> Tuple[str, str]:
    now = dt.datetime.now(dt.timezone.utc)
    start = now.replace(microsecond=0)
    end = (now + dt.timedelta(days=horizon_days)).replace(microsecond=0)
    return start.isoformat().replace("+00:00", "Z"), end.isoformat().replace("+00:00", "Z")


def fetch_events_for_league(api_key: str, slug: str, horizon_days: int, debug: Dict[str, Any]) -> List[Dict[str, Any]]:
    start, end = iso_window(horizon_days)
    data = api_get("/events", {
        "apiKey": api_key,
        "sport": SPORT,
        "league": slug,
        "status": "pending,live",
        "from": start,
        "to": end,
        "limit": 500,
    }, debug, allow_error=True)
    return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []


def event_id(event: Dict[str, Any]) -> str:
    return str(event.get("id") or event.get("eventId") or event.get("uuid") or "").strip()


def event_home(event: Dict[str, Any]) -> str:
    return str(event.get("home") or event.get("homeTeam") or event.get("home_team") or "").strip()


def event_away(event: Dict[str, Any]) -> str:
    return str(event.get("away") or event.get("awayTeam") or event.get("away_team") or "").strip()


def event_kickoff(event: Dict[str, Any]) -> str:
    return str(event.get("date") or event.get("kickoff") or event.get("startTime") or "").strip()


def fetch_odds(api_key: str, event_ids: List[str], bookmakers: str, debug: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for start in range(0, len(event_ids), MAX_EVENTS_PER_MULTI_CALL):
        if should_stop_for_rate_limit(debug):
            debug.setdefault("warnings", []).append("Stopped before odds fetch because rateLimitRemaining is below guard.")
            break
        chunk = event_ids[start:start + MAX_EVENTS_PER_MULTI_CALL]
        if not chunk:
            continue
        if len(chunk) == 1:
            data = api_get("/odds", {"apiKey": api_key, "eventId": chunk[0], "bookmakers": bookmakers}, debug, allow_error=True)
        else:
            data = api_get("/odds/multi", {"apiKey": api_key, "eventIds": ",".join(chunk), "bookmakers": bookmakers}, debug, allow_error=True)
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    eid = event_id(item)
                    if eid:
                        result[eid] = item
        elif isinstance(data, dict):
            eid = event_id(data)
            if eid:
                result[eid] = data
    return result


def bookmaker_blocks(event_odds: Dict[str, Any]) -> List[Tuple[str, List[Dict[str, Any]]]]:
    blocks: List[Tuple[str, List[Dict[str, Any]]]] = []
    raw = event_odds.get("bookmakers") or event_odds.get("odds") or []
    if isinstance(raw, dict):
        raw = [{"name": key, **(value if isinstance(value, dict) else {"markets": value})} for key, value in raw.items()]
    if isinstance(raw, list):
        for block in raw:
            if not isinstance(block, dict):
                continue
            name = str(block.get("name") or block.get("bookmaker") or block.get("key") or block.get("title") or "").strip()
            markets = block.get("markets") or block.get("odds") or []
            if isinstance(markets, dict):
                markets = [{"name": key, "odds": value} for key, value in markets.items()]
            if isinstance(markets, list):
                blocks.append((name, [m for m in markets if isinstance(m, dict)]))
    return blocks


def raw_market_name(market: Dict[str, Any]) -> str:
    return str(market.get("name") or market.get("market") or market.get("type") or market.get("key") or "").strip()


def outcome_rows(market: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = market.get("outcomes") or market.get("odds") or market.get("prices") or []
    if isinstance(raw, dict):
        rows = []
        for key, value in raw.items():
            if isinstance(value, dict):
                rows.append({"name": key, **value})
            else:
                rows.append({"name": key, "odds": value})
        return rows
    return [x for x in raw if isinstance(x, dict)] if isinstance(raw, list) else []


def row_price(row: Dict[str, Any]) -> Optional[float]:
    return to_float(row.get("odds") or row.get("price") or row.get("decimal") or row.get("value"))


def row_name(row: Dict[str, Any]) -> str:
    return str(row.get("name") or row.get("selection") or row.get("label") or row.get("side") or "").strip()


def row_line(row: Dict[str, Any]) -> Optional[float]:
    explicit = line_float(row.get("line") or row.get("point") or row.get("points") or row.get("handicap") or row.get("hdp") or row.get("max"))
    if explicit is not None:
        return explicit
    return line_from_text(row_name(row))


def line_from_text(value: str) -> Optional[float]:
    text = str(value or "")
    match = re.search(r"(?<!\d)(\d+(?:[.,]\d+)?)(?!\d)", text)
    return line_float(match.group(1)) if match else None


def row_side_price(row: Dict[str, Any], side: str) -> Optional[float]:
    return to_float(row.get(side))


def record_skipped_market(
    debug: Dict[str, Any],
    market: str,
    reason: str,
    row: str = "",
    *,
    family_override: Optional[str] = None,
) -> None:
    key = f"{market}|{reason}"
    summary = debug.setdefault("skippedMarketSummary", {})
    bucket = summary.setdefault(key, {"market": market, "reason": reason, "count": 0})
    bucket["count"] += 1
    family = family_override or market_family_from_name(market)
    reason_key = f"{family}|{reason}"
    reasons = debug.setdefault("skippedMarketReasons", {})
    reason_bucket = reasons.setdefault(reason_key, {"family": family, "reason": reason, "count": 0})
    reason_bucket["count"] += 1
    examples = debug.setdefault("skippedMarketExamples", [])
    if len(examples) < 50:
        item = {"market": market, "reason": reason}
        if row:
            item["row"] = row
        examples.append(item)


def record_raw_market(debug: Dict[str, Any], raw_name: str, family: str) -> None:
    raw_counts = debug.setdefault("rawMarketCounts", {})
    raw_counts[raw_name] = int(raw_counts.get(raw_name, 0)) + 1
    family_counts = debug.setdefault("classifiedMarketCounts", {})
    family_counts[family] = int(family_counts.get(family, 0)) + 1


def market_family_from_name(name: str) -> str:
    n = normalize_text(name)
    if n in {"ml", "money line", "moneyline"}:
        return "1X2"
    if any(token in n for token in ["both teams", "btts"]):
        return "BTTS"
    if "corner" in n:
        return "CORNERS"
    if any(token in n for token in ["card", "booking", "yellow", "red card"]):
        return "CARDS"
    if "shot on target" in n or "shots on target" in n or "on target" in n:
        return "SHOTS_ON_TARGET"
    if "shot" in n:
        return "SHOTS"
    if any(token in n for token in ["moneyline", "match result", "match winner", "1x2", "winner"]):
        return "1X2"
    if any(token in n for token in ["team total", "team goals"]):
        return "TEAM_TOTAL_GOALS"
    if (
        any(token in n for token in ["1st half", "first half", "half time", "halftime", "ht"])
        and any(token in n for token in ["goal", "goals", "total", "totals", "over under", "goal line"])
    ):
        return "FIRST_HALF_GOALS"
    if any(token in n for token in ["total", "over under", "goals"]):
        return "MATCH_GOALS"
    return "OTHER"


def is_team_market(name: str) -> bool:
    n = normalize_text(name)
    return "team" in n or "home" in n or "away" in n


def team_from_market_or_row(market_name: str, row: Dict[str, Any], home: str, away: str) -> Optional[str]:
    hay = normalize_text(f"{market_name} {row_name(row)}")
    if "home" in hay:
        return home
    if "away" in hay:
        return away
    home_norm = normalize_text(home, drop_suffixes=True)
    away_norm = normalize_text(away, drop_suffixes=True)
    if home_norm and home_norm in hay:
        return home
    if away_norm and away_norm in hay:
        return away
    return None


def add_market(out: List[Dict[str, Any]], market: str, selection: str, odds: Optional[float], bookmaker: str, *, line: Optional[float] = None, team: Optional[str] = None) -> None:
    if odds is None or market not in SUPPORTED_MARKETS:
        return
    item: Dict[str, Any] = {
        "market": market,
        "selection": selection,
        "odds": odds,
        "bookmaker": bookmaker,
        "confidence": "high",
        "exactBookmakerOdds": True,
    }
    if line is not None:
        item["line"] = line
    if team:
        item["team"] = team
    out.append(item)


def normalize_market(market: Dict[str, Any], bookmaker: str, home: str, away: str, debug: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw_name = raw_market_name(market)
    family = market_family_from_name(raw_name)
    audit = record_market_audit(
        debug,
        raw_name,
        {
            "bookmaker": bookmaker,
            "fixture": f"{home} - {away}",
            "marketSample": compact(market, 700),
        },
        classification_text=provider_market_text(market),
    )
    record_raw_market(debug, raw_name, family)
    if audit["status"] != "supported":
        record_skipped_market(
            debug,
            raw_name,
            audit["reason"],
            family_override=audit["family"],
        )
        return []
    rows = outcome_rows(market)
    out: List[Dict[str, Any]] = []
    if not rows:
        record_skipped_market(debug, raw_name, "no outcome rows")
        return out

    if family == "1X2":
        for row in rows:
            home_price = row_side_price(row, "home")
            draw_price = row_side_price(row, "draw")
            away_price = row_side_price(row, "away")
            if home_price is not None or draw_price is not None or away_price is not None:
                add_market(out, "1X2", "Home", home_price, bookmaker, team=home)
                add_market(out, "1X2", "Draw", draw_price, bookmaker)
                add_market(out, "1X2", "Away", away_price, bookmaker, team=away)
                continue
            name = row_name(row)
            n = normalize_text(name, drop_suffixes=True)
            price = row_price(row)
            if n in {"draw", "x", "tie"}:
                add_market(out, "1X2", "Draw", price, bookmaker)
            elif n == normalize_text(home, drop_suffixes=True) or "home" in n:
                add_market(out, "1X2", "Home", price, bookmaker, team=home)
            elif n == normalize_text(away, drop_suffixes=True) or "away" in n:
                add_market(out, "1X2", "Away", price, bookmaker, team=away)
        return out

    if family == "DOUBLE_CHANCE":
        for row in rows:
            direct_1x = to_float(row.get("1X") or row.get("1x"))
            direct_12 = to_float(row.get("12"))
            direct_x2 = to_float(row.get("X2") or row.get("x2") or row.get("2X") or row.get("2x"))
            if direct_1x is not None or direct_12 is not None or direct_x2 is not None:
                add_market(out, "DOUBLE_CHANCE", "1X", direct_1x, bookmaker)
                add_market(out, "DOUBLE_CHANCE", "12", direct_12, bookmaker)
                add_market(out, "DOUBLE_CHANCE", "X2", direct_x2, bookmaker)
                continue

            label = normalize_text(row_name(row), drop_suffixes=True)
            price = to_float(row.get("under") or row.get("over")) or row_price(row)
            if not label or price is None:
                continue
            if label in {"1x", "home or draw", "home draw"} or label.endswith(" or draw"):
                add_market(out, "DOUBLE_CHANCE", "1X", price, bookmaker)
            elif label in {"x2", "2x", "draw or away", "away or draw"} or label.startswith("draw or "):
                add_market(out, "DOUBLE_CHANCE", "X2", price, bookmaker)
            elif label in {"12", "home or away", "no draw"} or (" or " in label and "draw" not in label):
                add_market(out, "DOUBLE_CHANCE", "12", price, bookmaker)
            else:
                record_skipped_market(debug, raw_name, "unrecognized Double Chance row", row_name(row))
        return out

    if family == "BTTS":
        for row in rows:
            yes_price = row_side_price(row, "yes")
            no_price = row_side_price(row, "no")
            if yes_price is not None or no_price is not None:
                add_market(out, "BTTS", "Yes", yes_price, bookmaker)
                add_market(out, "BTTS", "No", no_price, bookmaker)
                continue
            name = row_name(row)
            n = normalize_text(name)
            price = row_price(row)
            if n in {"yes", "y", "both teams to score yes"} or "yes" in n:
                add_market(out, "BTTS", "Yes", price, bookmaker)
            elif n in {"no", "n", "both teams to score no"} or "no" in n:
                add_market(out, "BTTS", "No", price, bookmaker)
        return out

    base_market = {
        "MATCH_GOALS": "MATCH_GOALS",
        "FIRST_HALF_GOALS": "FIRST_HALF_GOALS",
        "TEAM_TOTAL_GOALS": "TEAM_TOTAL_GOALS",
        "CORNERS": "TEAM_CORNERS" if is_team_market(raw_name) else "MATCH_CORNERS",
        "CARDS": "TEAM_CARDS" if is_team_market(raw_name) else "MATCH_CARDS",
        "SHOTS": "TEAM_SHOTS" if is_team_market(raw_name) else "MATCH_SHOTS",
        "SHOTS_ON_TARGET": "TEAM_SHOTS_ON_TARGET" if is_team_market(raw_name) else "MATCH_SHOTS_ON_TARGET",
    }.get(family)

    if not base_market:
        record_skipped_market(debug, raw_name, "unsupported market family")
        return out

    for row in rows:
        name = row_name(row)
        n = normalize_text(name)
        line = row_line(row) or row_line(market) or line_from_text(raw_name)
        team = team_from_market_or_row(raw_name, row, home, away) if base_market.startswith("TEAM_") else None
        if base_market == "TEAM_TOTAL_GOALS" and not team:
            market_norm = normalize_text(raw_name)
            if "home" in market_norm:
                team = home
            elif "away" in market_norm:
                team = away
        if base_market.startswith("TEAM_") and not team:
            record_skipped_market(debug, raw_name, "team market without clear team", name)
            continue
        if line is None:
            record_skipped_market(debug, raw_name, "line missing", name)
            continue
        if not is_half_line(line):
            record_skipped_market(debug, raw_name, "non half-line skipped", name)
            continue
        label_prefix = team if team else {
            "MATCH_GOALS": "Goals",
            "FIRST_HALF_GOALS": "1H Goals",
            "MATCH_CORNERS": "Corners",
            "MATCH_CARDS": "Cards",
            "MATCH_SHOTS": "Shots",
            "MATCH_SHOTS_ON_TARGET": "Shots on Target",
        }.get(base_market, family.title())
        over_price = row_side_price(row, "over")
        under_price = row_side_price(row, "under")
        if over_price is not None or under_price is not None:
            add_market(out, base_market, "Over" if base_market in {"MATCH_GOALS", "FIRST_HALF_GOALS"} else f"{label_prefix} Over {line:g}", over_price, bookmaker, line=line, team=team)
            add_market(out, base_market, "Under" if base_market in {"MATCH_GOALS", "FIRST_HALF_GOALS"} else f"{label_prefix} Under {line:g}", under_price, bookmaker, line=line, team=team)
        elif "under" in n:
            add_market(out, base_market, "Under" if base_market in {"MATCH_GOALS", "FIRST_HALF_GOALS"} else f"{label_prefix} Under {line:g}", row_price(row), bookmaker, line=line, team=team)
        elif "over" in n:
            add_market(out, base_market, "Over" if base_market in {"MATCH_GOALS", "FIRST_HALF_GOALS"} else f"{label_prefix} Over {line:g}", row_price(row), bookmaker, line=line, team=team)
        else:
            side = normalize_text(row.get("side"))
            if side == "under":
                add_market(out, base_market, "Under" if base_market in {"MATCH_GOALS", "FIRST_HALF_GOALS"} else f"{label_prefix} Under {line:g}", row_price(row), bookmaker, line=line, team=team)
            elif side == "over":
                add_market(out, base_market, "Over" if base_market in {"MATCH_GOALS", "FIRST_HALF_GOALS"} else f"{label_prefix} Over {line:g}", row_price(row), bookmaker, line=line, team=team)
    return out


def dedupe_markets(markets: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    best: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
    for item in markets:
        key = (
            str(item.get("market") or ""),
            str(item.get("selection") or ""),
            str(item.get("line") or ""),
            str(item.get("team") or ""),
        )
        if key not in best or float(item.get("odds") or 0) > float(best[key].get("odds") or 0):
            best[key] = item
    return sorted(best.values(), key=lambda x: (str(x.get("market")), str(x.get("team", "")), str(x.get("selection")), float(x.get("odds") or 0)))


def normalize_event_match(config_league: Dict[str, Any], event: Dict[str, Any], odds: Optional[Dict[str, Any]], aliases: Dict[str, Dict[str, str]], debug: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    league_code = str(config_league.get("leagueCode"))
    home_raw = event_home(event)
    away_raw = event_away(event)
    if not home_raw or not away_raw:
        debug.setdefault("warnings", []).append(f"Skipped event without teams: {event_id(event)}")
        return None
    home, canonical_home = canonical_team_info(home_raw, league_code, aliases, debug)
    away, canonical_away = canonical_team_info(away_raw, league_code, aliases, debug)
    if canonical_home and canonical_away:
        mapping_status = "matched"
    elif canonical_home or canonical_away:
        mapping_status = "partial"
    else:
        mapping_status = "unmatched"
    kickoff = event_kickoff(event)
    date = kickoff[:10] if kickoff else ""
    markets: List[Dict[str, Any]] = []
    if odds:
        for bookmaker, market_list in bookmaker_blocks(odds):
            for market in market_list:
                markets.extend(normalize_market(market, bookmaker, home, away, debug))
    markets = dedupe_markets(markets)
    return {
        "id": event_id(event),
        "date": date,
        "kickoff": kickoff,
        "providerHomeTeam": home_raw,
        "providerAwayTeam": away_raw,
        "homeTeam": home,
        "awayTeam": away,
        "canonicalHomeTeam": canonical_home,
        "canonicalAwayTeam": canonical_away,
        "teamMappingStatus": mapping_status,
        "usableForStats": mapping_status == "matched",
        "markets": markets,
    }


def build_output(config: Dict[str, Any], selected: List[Dict[str, Any]], api_key: str, dry_run: bool, bookmakers: str, debug: Dict[str, Any]) -> Dict[str, Any]:
    generated_at = now_utc()
    aliases = load_aliases()
    debug["generatedAt"] = generated_at
    debug["scriptVersion"] = SCRIPT_VERSION
    debug["registry"] = registry_summary(config)
    debug["dryRun"] = dry_run
    debug["bookmakersRequested"] = [x.strip() for x in bookmakers.split(",") if x.strip()]
    debug["leaguesRequested"] = [x.get("leagueCode") for x in selected]
    debug["marketAuditPolicy"] = {
        "auditOnlyFamilies": sorted(AUDIT_ONLY_FAMILIES),
        "normalMarketsUnchanged": True,
        "extraApiCalls": 0,
    }

    output = empty_output(generated_at, debug)
    if dry_run:
        debug["marketAuditSelfCheck"] = run_market_audit_self_check()
        debug.setdefault("warnings", []).append(
            "Dry run: market classification self-check passed; skipped Odds-API.io calls and production output writes."
        )
        output["debug"] = output_debug(generated_at, debug)
        return output

    provider_leagues = discover_provider_leagues(api_key, debug)
    if should_stop_for_rate_limit(debug):
        debug.setdefault("warnings", []).append("Stopped after league discovery because rateLimitRemaining is below guard.")
        output["debug"] = output_debug(generated_at, debug)
        return output

    for league in selected:
        league_code = str(league.get("leagueCode"))
        try:
            provider = match_provider_league(league, provider_leagues)
            if not provider:
                debug.setdefault("leaguesMissing", []).append({
                    "leagueCode": league_code,
                    "country": league.get("country"),
                    "competition": league.get("competition"),
                    "apiFootballLeagueId": league.get("apiFootballLeagueId"),
                    "reason": "provider league slug not found with strict country guard",
                })
                continue
            slug = str(provider.get("slug") or "")
            debug.setdefault("leaguesMatched", []).append({
                "leagueCode": league_code,
                "country": league.get("country"),
                "competition": league.get("competition"),
                "apiFootballLeagueId": league.get("apiFootballLeagueId"),
                "providerLeagueSlug": slug,
                "providerName": provider.get("name"),
            })
            if should_stop_for_rate_limit(debug):
                debug.setdefault("warnings", []).append("Stopped before events fetch because rateLimitRemaining is below guard.")
                break
            events = fetch_events_for_league(api_key, slug, int(config.get("horizonDays") or 21), debug)
            event_ids = [event_id(e) for e in events if event_id(e)]
            odds_by_event = fetch_odds(api_key, event_ids, bookmakers, debug) if event_ids else {}
            matches = []
            events_without_markets = 0
            events_without_team_mapping = 0
            for event in events:
                match = normalize_event_match(league, event, odds_by_event.get(event_id(event)) or event, aliases, debug)
                if match and match["markets"] and match.get("usableForStats"):
                    matches.append(match)
                elif match and match["markets"]:
                    events_without_team_mapping += 1
                elif match:
                    events_without_markets += 1
            matched_pairs = sum(1 for m in matches if m.get("teamMappingStatus") == "matched")
            partial_pairs = sum(1 for m in matches if m.get("teamMappingStatus") == "partial")
            unmatched_pairs = sum(1 for m in matches if m.get("teamMappingStatus") == "unmatched")
            output["leagues"].append({
                "leagueCode": league_code,
                "country": league.get("country"),
                "competition": league.get("competition"),
                "season": league.get("season"),
                "apiFootballLeagueId": league.get("apiFootballLeagueId"),
                "enabledForStats": bool(league.get("enabledForStats", True)),
                "enabledForOdds": bool(league.get("enabledForOdds", True)),
                "enabledForBetting": bool(league.get("enabledForBetting", True)),
                "providerLeagueSlug": slug,
                "providerName": provider.get("name"),
                "matches": matches,
            })
            debug.setdefault("leagueReports", []).append({
                "leagueCode": league_code,
                "country": league.get("country"),
                "competition": league.get("competition"),
                "apiFootballLeagueId": league.get("apiFootballLeagueId"),
                "providerLeagueSlug": slug,
                "eventsFetched": len(events),
                "eventsWithOddsResponse": len(odds_by_event),
                "eventsWithoutMappedMarkets": events_without_markets,
                "eventsWithMarketsButUnmatchedTeams": events_without_team_mapping,
                "matchesEmitted": len(matches),
                "marketsEmitted": sum(len(m.get("markets", [])) for m in matches),
                "matchedTeamPairs": matched_pairs,
                "partialTeamPairs": partial_pairs,
                "unmatchedTeamPairs": unmatched_pairs,
            })
        except Exception as exc:  # keep partial output safe
            debug.setdefault("warnings", []).append(f"{league_code}: {exc}")
        if should_stop_for_rate_limit(debug):
            debug.setdefault("warnings", []).append("Stopped safely because rateLimitRemaining is below guard; partial output written.")
            break

    output["debug"] = output_debug(generated_at, debug)
    return output


def skipped_market_summary(debug: Dict[str, Any]) -> List[Dict[str, Any]]:
    summary = list(debug.get("skippedMarketSummary", {}).values())
    return sorted(summary, key=lambda item: (-int(item.get("count") or 0), str(item.get("market") or ""), str(item.get("reason") or "")))


def skipped_market_reasons(debug: Dict[str, Any]) -> List[Dict[str, Any]]:
    reasons = list(debug.get("skippedMarketReasons", {}).values())
    return sorted(reasons, key=lambda item: (-int(item.get("count") or 0), str(item.get("family") or ""), str(item.get("reason") or "")))


def unique_unmatched_teams(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out = []
    for item in items:
        key = (
            str(item.get("leagueCode") or ""),
            normalize_text(item.get("providerTeam") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def emitted_market_counts(output: Dict[str, Any]) -> Dict[str, int]:
    counts = {key: 0 for key in EMITTED_MARKET_COUNT_KEYS}
    for league in output.get("leagues", []) or []:
        for match in league.get("matches", []) or []:
            for market in match.get("markets", []) or []:
                key = str(market.get("market") or "")
                if key in counts:
                    counts[key] += 1
    return counts


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update Domestic Odds-API.io schedule + exact odds JSON.")
    parser.add_argument("--mode", choices=["all", "group", "league"], default=os.getenv("STATMAKER_DOMESTIC_MODE", "all"))
    parser.add_argument("--target", default=os.getenv("STATMAKER_DOMESTIC_TARGET", "all_initial"))
    parser.add_argument("--dry-run", action="store_true", default=os.getenv("STATMAKER_DOMESTIC_DRY_RUN", "false").lower() == "true")
    parser.add_argument("--bookmakers", default=os.getenv("ODDS_API_IO_BOOKMAKERS", DEFAULT_BOOKMAKERS))
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    debug: Dict[str, Any] = {"warnings": [], "apiCalls": []}
    config = read_json(CONFIG_PATH)
    selected = choose_leagues(config, args.mode, args.target)
    if not selected:
        debug.setdefault("warnings", []).append(f"No Domestic leagues selected for mode={args.mode} target={args.target}.")
    api_key = os.getenv("ODDS_API_IO_KEY", "").strip()
    if not api_key and not args.dry_run:
        debug.setdefault("warnings", []).append("ODDS_API_IO_KEY is missing; wrote structural output without API calls.")
        args.dry_run = True
    output = build_output(config, selected, api_key, args.dry_run, args.bookmakers, debug)
    debug["emittedMarketCounts"] = emitted_market_counts(output)
    output.setdefault("debug", {})["emittedMarketCounts"] = debug["emittedMarketCounts"]
    output.setdefault("debug", {})["rawMarketCounts"] = debug.get("rawMarketCounts", {})
    output.setdefault("debug", {})["classifiedMarketCounts"] = debug.get("classifiedMarketCounts", {})
    output.setdefault("debug", {})["skippedMarketReasons"] = skipped_market_reasons(debug)
    output.setdefault("debug", {})["skippedMarketSummary"] = skipped_market_summary(debug)
    output.setdefault("debug", {}).update(market_audit_report(debug))
    report = dict(debug)
    report["registry"] = registry_summary(config)
    report["outputPath"] = str(OUT_PATH.relative_to(ROOT))
    report["reportPath"] = str(REPORT_PATH.relative_to(ROOT))
    report["partialOutput"] = bool(debug.get("warnings")) or should_stop_for_rate_limit(debug)
    report["leaguesEmitted"] = [x.get("leagueCode") for x in output.get("leagues", [])]
    report["matchesEmitted"] = sum(len(x.get("matches", [])) for x in output.get("leagues", []))
    report["marketsEmitted"] = sum(len(m.get("markets", [])) for x in output.get("leagues", []) for m in x.get("matches", []))
    report["emittedMarketCounts"] = debug["emittedMarketCounts"]
    report["unmatchedTeams"] = unique_unmatched_teams(debug.get("unmatchedTeams", []))
    report["skippedMarketReasons"] = skipped_market_reasons(debug)
    report["skippedMarketSummary"] = skipped_market_summary(debug)
    report["skippedMarketExamples"] = debug.get("skippedMarketExamples", [])
    report.update(market_audit_report(debug))
    if not args.dry_run:
        write_json(OUT_PATH, output)
        write_json(REPORT_PATH, report)
    print(json.dumps({
        "output": str(OUT_PATH.relative_to(ROOT)),
        "report": str(REPORT_PATH.relative_to(ROOT)),
        "scriptVersion": SCRIPT_VERSION,
        "dryRun": args.dry_run,
        "registry": report["registry"],
        "leaguesRequested": debug.get("leaguesRequested", []),
        "leaguesMatched": debug.get("leaguesMatched", []),
        "leaguesMissing": debug.get("leaguesMissing", []),
        "matchesEmitted": report["matchesEmitted"],
        "marketsEmitted": report["marketsEmitted"],
        "scriptVersion": SCRIPT_VERSION,
        "marketAuditSelfCheck": debug.get("marketAuditSelfCheck"),
        "productionFilesWritten": not args.dry_run,
        "warnings": debug.get("warnings", []),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
