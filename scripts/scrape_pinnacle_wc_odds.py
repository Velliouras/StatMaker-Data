#!/usr/bin/env python3
"""
StatMaker Pinnacle World Cup odds scraper.

Purpose:
- Runs outside the Android app, from GitHub Actions.
- Reads StatMaker's World Cup fixture JSON.
- Opens Pinnacle football / FIFA World Cup matchups pages.
- Extracts conservative pre-match odds only when a fixture and market can be matched.
- Writes app-compatible odds JSON plus a debug report and snapshot.

This is intentionally conservative. It never invents odds. If a match/market cannot be
mapped with confidence, it is left out and explained in debug_report.json.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

PINNACLE_URLS = [
    "https://www.pinnacle.com/en/soccer/fifa-world-cup/matchups",
    "https://www.pinnacle.com/en/soccer/matchups",
]

FIXTURES_PATH = Path(os.getenv("STATMAKER_WC_FIXTURES", "world-cup/world_cup_2026.json"))
OUTPUT_PATH = Path(os.getenv("STATMAKER_PINNACLE_WC_ODDS_OUTPUT", "odds/pinnacle/world_cup_odds.json"))
DEBUG_PATH = Path(os.getenv("STATMAKER_PINNACLE_WC_ODDS_DEBUG", "odds/pinnacle/debug_report.json"))
SNAPSHOT_PATH = Path(os.getenv("STATMAKER_PINNACLE_PROBE_SNAPSHOT", "odds/pinnacle/pinnacle_probe_snapshot.txt"))
TIMEOUT_MS = int(os.getenv("STATMAKER_PINNACLE_TIMEOUT_MS", "22000"))
MAX_FIXTURES = int(os.getenv("STATMAKER_WC_ODDS_MAX_FIXTURES", "64"))
MIN_ODD = float(os.getenv("STATMAKER_MIN_VALID_ODD", "1.01"))
MAX_ODD = float(os.getenv("STATMAKER_MAX_VALID_ODD", "1000"))
MAX_NETWORK_API_CANDIDATES = int(os.getenv("STATMAKER_PINNACLE_MAX_NETWORK_API_CANDIDATES", "80"))
MAX_NETWORK_BODY_CHARS = int(os.getenv("STATMAKER_PINNACLE_MAX_NETWORK_BODY_CHARS", "6000"))

BOOKMAKER = "Pinnacle"
SOURCE = "pinnacle"
COMPETITION = "World Cup"
SEASON = "2026"
SCRIPT_VERSION = "pinnacle-wc-odds-v6-arcadia-ou-btts-parser"

NETWORK_API_KEYWORDS = [
    "api",
    "event",
    "events",
    "market",
    "markets",
    "price",
    "prices",
    "odds",
    "sports",
    "matchup",
    "matchups",
    "prematch",
    "straight",
    "line",
    "coupon",
    "graphql",
    "json",
]

KEYWORDS = [
    "football",
    "soccer",
    "world cup",
    "fifa",
    "odds",
    "matchups",
    "spread",
    "money line",
    "total",
    "over",
    "under",
    "both teams",
]

TEAM_ALIASES = {
    "usa": ["united states", "usa", "u.s.a", "u.s.", "us"],
    "united states": ["united states", "usa", "u.s.a", "u.s.", "us"],
    "czech republic": ["czech republic", "czechia", "czech rep"],
    "czechia": ["czechia", "czech republic", "czech rep"],
    "ivory coast": ["ivory coast", "cote d'ivoire", "cote d ivoire", "côte d’ivoire", "côte d'ivoire"],
    "bosnia & herzegovina": ["bosnia & herzegovina", "bosnia and herzegovina", "bosnia herzegovina", "bosnia"],
    "bosnia and herzegovina": ["bosnia & herzegovina", "bosnia and herzegovina", "bosnia herzegovina", "bosnia"],
    "south korea": ["south korea", "korea republic", "republic of korea", "korea rep"],
    "dr congo": ["dr congo", "congo dr", "d.r. congo", "democratic republic of congo"],
    "cape verde": ["cape verde", "cabo verde"],
    "curacao": ["curacao", "curaçao"],
    "turkey": ["turkey", "turkiye", "türkiye"],
}


@dataclass(frozen=True)
class Fixture:
    match_id: str
    date: str
    time: str
    home_team: str
    away_team: str
    status: str

    @property
    def title(self) -> str:
        return f"{self.home_team} vs {self.away_team}"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_text(value: str) -> str:
    value = clean_text(value).lower()
    value = value.replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def team_variants(team: str) -> list[str]:
    norm = normalize_text(team)
    variants = TEAM_ALIASES.get(norm, [team])
    normalized = [normalize_text(item) for item in variants]
    if norm not in normalized:
        normalized.append(norm)
    return [item for item in normalized if item]


def contains_team(text: str, team: str) -> bool:
    norm_text = normalize_text(text)
    return any(variant in norm_text for variant in team_variants(team))


def line_has_team(line: str, team: str) -> bool:
    return any(variant in normalize_text(line) for variant in team_variants(team))


def load_fixtures() -> list[Fixture]:
    if not FIXTURES_PATH.exists():
        return []
    data = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))
    fixtures: list[Fixture] = []
    for item in data.get("matches", []):
        status = clean_text(item.get("status")).lower()
        if status in {"finished", "complete", "completed", "ft"}:
            continue
        home = clean_text(item.get("homeTeam") or item.get("team1"))
        away = clean_text(item.get("awayTeam") or item.get("team2"))
        if not home or not away:
            continue
        fixtures.append(
            Fixture(
                match_id=clean_text(item.get("matchId") or item.get("id") or f"{home}_{away}"),
                date=clean_text(item.get("date")),
                time=clean_text(item.get("time")),
                home_team=home,
                away_team=away,
                status=status,
            )
        )
    return fixtures[:MAX_FIXTURES]


def parse_decimal_odd(value: str) -> float | None:
    token = clean_text(value)
    match = re.fullmatch(r"(\d{1,3})[\.,](\d{2,3})", token)
    if not match:
        return None
    odd = float(f"{match.group(1)}.{match.group(2)}")
    return round(odd, 2) if MIN_ODD <= odd <= MAX_ODD else None


def odds_tokens(text: str) -> list[float]:
    seen: list[float] = []
    # Decimal odds. This deliberately excludes dates/times and integers.
    for raw in re.findall(r"(?<!\d)(?:1[\.,]0[1-9]|1[\.,][1-9]\d|[2-9][\.,]\d{2}|[1-9]\d[\.,]\d{2})(?!\d)", text or ""):
        odd = parse_decimal_odd(raw)
        if odd is not None:
            seen.append(odd)
    return seen


def unique_market(markets: list[dict[str, Any]], candidate: dict[str, Any]) -> None:
    key = (candidate.get("market"), candidate.get("selection"), candidate.get("team"), candidate.get("line"))
    for existing in markets:
        existing_key = (existing.get("market"), existing.get("selection"), existing.get("team"), existing.get("line"))
        if existing_key == key:
            return
    markets.append(candidate)


def extract_candidate_urls(html: str, base_url: str) -> list[str]:
    candidates: set[str] = set()
    for raw in re.findall(r"(?:src|href)=[\"']([^\"']+)[\"']", html or "", re.IGNORECASE):
        url = urljoin(base_url, raw)
        lower = url.lower()
        if any(token in lower for token in ["api", "event", "market", "coupon", "odds", "sports", "prematch", "matchup", "graphql", "json", "chunk", "js"]):
            candidates.add(url)
    for raw in re.findall(r"https?://[^\"'<>\\\s]+", html or ""):
        lower = raw.lower()
        if any(token in lower for token in ["api", "event", "market", "coupon", "odds", "sports", "prematch", "matchup", "graphql", "json"]):
            candidates.add(raw)
    return sorted(candidates)[:100]


def is_api_candidate_url(url: str) -> bool:
    lower = (url or "").lower()
    return any(token in lower for token in NETWORK_API_KEYWORDS)


def summarize_json_root(value: Any, depth: int = 0) -> Any:
    """Small JSON shape preview for diagnostics. Never returns raw huge payloads."""
    if depth >= 3:
        if isinstance(value, dict):
            return {"type": "object", "keys": list(value.keys())[:12]}
        if isinstance(value, list):
            return {"type": "array", "length": len(value)}
        return type(value).__name__
    if isinstance(value, dict):
        preview: dict[str, Any] = {"type": "object", "keys": list(value.keys())[:20]}
        children: dict[str, Any] = {}
        for key, item in list(value.items())[:8]:
            children[str(key)] = summarize_json_root(item, depth + 1)
        if children:
            preview["children"] = children
        return preview
    if isinstance(value, list):
        preview = {"type": "array", "length": len(value)}
        if value:
            preview["first"] = summarize_json_root(value[0], depth + 1)
        return preview
    return type(value).__name__




def json_dumps_safe(value: Any, limit: int = 200000) -> str:
    """Serialize for diagnostics only, bounded so debug files do not explode."""
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    return text[:limit]


def iter_dicts(value: Any, max_depth: int = 8, depth: int = 0) -> Any:
    if depth > max_depth:
        return
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_dicts(child, max_depth, depth + 1)
    elif isinstance(value, list):
        for child in value:
            yield from iter_dicts(child, max_depth, depth + 1)


def get_nested_text(value: Any, keys: list[str]) -> str:
    if not isinstance(value, dict):
        return ""
    for key in keys:
        if key in value and value.get(key) is not None:
            return clean_text(value.get(key))
    return ""


def as_decimal_from_price(value: Any) -> float | None:
    """Convert an exact API price representation to decimal odds.

    This never estimates bookmaker odds. It only converts explicit numeric/string
    prices that the API already provides. American prices are converted to their
    decimal equivalent because the source value is still exact bookmaker data.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        # Decimal odds directly supplied.
        if MIN_ODD <= number <= MAX_ODD and not float(number).is_integer() or (1.01 <= number <= 99.99):
            if 1.01 <= number <= 99.99:
                return round(number, 2)
        # American odds conversion, e.g. -110 or +250.
        if abs(number) >= 100 and abs(number) <= 100000:
            if number > 0:
                return round(1.0 + number / 100.0, 2)
            return round(1.0 + 100.0 / abs(number), 2)
        return None
    if isinstance(value, str):
        token = clean_text(value)
        dec = parse_decimal_odd(token)
        if dec is not None:
            return dec
        if re.fullmatch(r"[+-]?\d{3,5}", token):
            return as_decimal_from_price(int(token))
    return None


def extract_price_from_outcome(outcome: dict[str, Any]) -> tuple[float | None, str | None]:
    """Find explicit price inside an API outcome/price object."""
    price_keys = [
        "price", "decimalPrice", "decimalOdds", "odds", "value", "currentPrice",
        "americanPrice", "americanOdds", "displayPrice", "formattedPrice",
    ]
    for key in price_keys:
        if key in outcome:
            odd = as_decimal_from_price(outcome.get(key))
            if odd is not None:
                return odd, key
    # Some APIs nest prices under price/value objects.
    for key in ["prices", "priceData", "oddsData", "display"]:
        nested = outcome.get(key)
        if isinstance(nested, dict):
            odd, source = extract_price_from_outcome(nested)
            if odd is not None:
                return odd, f"{key}.{source}"
    return None, None


def extract_id(value: Any) -> str:
    if value is None:
        return ""
    return clean_text(value)


def collect_participants(obj: Any, max_depth: int = 5) -> list[dict[str, Any]]:
    """Collect participant-like objects with id/name/alignment from a matchup payload."""
    participants: list[dict[str, Any]] = []
    participant_list_keys = ["participants", "competitors", "teams", "runners"]

    def walk(value: Any, depth: int) -> None:
        if depth > max_depth:
            return
        if isinstance(value, dict):
            for key in participant_list_keys:
                items = value.get(key)
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict):
                            name = get_nested_text(item, ["name", "displayName", "fullName", "teamName", "title", "label", "description"])
                            if not name and isinstance(item.get("participant"), dict):
                                name = get_nested_text(item["participant"], ["name", "displayName", "fullName", "teamName", "title", "label"])
                            if name:
                                nested_participant = item.get("participant") if isinstance(item.get("participant"), dict) else {}
                                participant_id = (
                                    item.get("id")
                                    or item.get("participantId")
                                    or item.get("participantID")
                                    or item.get("teamId")
                                    or item.get("competitorId")
                                    or nested_participant.get("id")
                                )
                                participants.append({
                                    "id": extract_id(participant_id),
                                    "name": name,
                                    "alignment": clean_text(item.get("alignment") or item.get("side") or item.get("designation") or item.get("type") or item.get("homeAway")),
                                    "rawKeys": list(item.keys())[:16],
                                })
            # Direct home/away objects.
            for side_key in ["home", "away", "team1", "team2"]:
                item = value.get(side_key)
                if isinstance(item, dict):
                    name = get_nested_text(item, ["name", "displayName", "fullName", "teamName", "title", "label"])
                    if name:
                        participants.append({
                            "id": extract_id(item.get("id") or item.get("participantId") or item.get("teamId")),
                            "name": name,
                            "alignment": side_key,
                            "rawKeys": list(item.keys())[:16],
                        })
            for child in value.values():
                walk(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                walk(child, depth + 1)

    walk(obj, 0)
    # De-duplicate by id/name/alignment.
    seen: set[tuple[str, str, str]] = set()
    unique: list[dict[str, Any]] = []
    for item in participants:
        key = (str(item.get("id") or ""), normalize_text(str(item.get("name") or "")), normalize_text(str(item.get("alignment") or "")))
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def map_fixture_participants(obj: Any, fixture: Fixture) -> tuple[dict[str, Any], dict[str, Any]]:
    participants = collect_participants(obj)
    home_candidates = [item for item in participants if contains_team(str(item.get("name") or ""), fixture.home_team)]
    away_candidates = [item for item in participants if contains_team(str(item.get("name") or ""), fixture.away_team)]

    def preferred(items: list[dict[str, Any]], side: str) -> dict[str, Any] | None:
        if not items:
            return None
        side_norm = normalize_text(side)
        aligned = [item for item in items if side_norm in normalize_text(str(item.get("alignment") or ""))]
        if aligned:
            return aligned[0]
        return items[0]

    home = preferred(home_candidates, "home")
    away = preferred(away_candidates, "away")
    debug = {
        "participantsFound": len(participants),
        "participantsSample": participants[:8],
        "homeCandidates": home_candidates[:4],
        "awayCandidates": away_candidates[:4],
        "homeMapped": home,
        "awayMapped": away,
    }
    if not home or not away:
        return {}, debug
    return {"home": home, "away": away}, debug


def collect_market_objects(obj: Any, max_depth: int = 7) -> list[dict[str, Any]]:
    markets: list[dict[str, Any]] = []
    for item in iter_dicts(obj, max_depth=max_depth):
        # Market-like object with outcome/price lists.
        if any(isinstance(item.get(key), list) for key in ["prices", "outcomes", "selections", "runners"]):
            markets.append(item)
        # Direct market objects are often inside a list named markets.
        maybe_markets = item.get("markets")
        if isinstance(maybe_markets, list):
            for market in maybe_markets:
                if isinstance(market, dict):
                    markets.append(market)
    # De-duplicate by object id.
    seen: set[int] = set()
    unique: list[dict[str, Any]] = []
    for item in markets:
        oid = id(item)
        if oid in seen:
            continue
        seen.add(oid)
        unique.append(item)
    return unique


def collect_outcomes(market: dict[str, Any]) -> list[dict[str, Any]]:
    outcomes: list[dict[str, Any]] = []
    for key in ["prices", "outcomes", "selections", "runners"]:
        items = market.get(key)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    outcomes.append(item)
    return outcomes


def outcome_kind(outcome: dict[str, Any], participant_map: dict[str, dict[str, Any]], fixture: Fixture) -> tuple[str | None, str | None]:
    """Return home/draw/away only from explicit participant id/name/designation."""
    text = normalize_text(json_dumps_safe(outcome, limit=5000))
    label_text = normalize_text(" ".join(clean_text(outcome.get(key)) for key in ["name", "label", "description", "designation", "side", "type", "title"] if key in outcome))

    if any(token in label_text.split() for token in ["draw", "tie"]):
        return "draw", "explicit_label"
    if label_text in {"x"} or " draw " in f" {label_text} ":
        return "draw", "explicit_label"

    # Participant id mapping is the strongest exact signal.
    possible_id_keys = ["participantId", "participantID", "participant_id", "teamId", "competitorId", "runnerId", "id"]
    ids = {extract_id(outcome.get(key)) for key in possible_id_keys if key in outcome and outcome.get(key) is not None}
    for nested_key in ["participant", "team", "competitor", "runner"]:
        nested = outcome.get(nested_key)
        if isinstance(nested, dict):
            ids.update(extract_id(nested.get(key)) for key in ["id", "participantId", "teamId", "competitorId"] if nested.get(key) is not None)
            nested_name = get_nested_text(nested, ["name", "displayName", "fullName", "teamName", "title", "label"])
            if nested_name:
                if contains_team(nested_name, fixture.home_team):
                    return "home", f"{nested_key}.name"
                if contains_team(nested_name, fixture.away_team):
                    return "away", f"{nested_key}.name"

    home_id = extract_id((participant_map.get("home") or {}).get("id"))
    away_id = extract_id((participant_map.get("away") or {}).get("id"))
    if home_id and home_id in ids:
        return "home", "participant_id"
    if away_id and away_id in ids:
        return "away", "participant_id"

    # Explicit team names in the outcome itself are acceptable.
    if contains_team(text, fixture.home_team):
        return "home", "explicit_team_text"
    if contains_team(text, fixture.away_team):
        return "away", "explicit_team_text"

    # Side/designation can be acceptable only when participant map is already exact for this fixture.
    side = normalize_text(clean_text(outcome.get("side") or outcome.get("designation") or outcome.get("alignment") or outcome.get("type")))
    if side in {"home", "team1"}:
        return "home", "explicit_side"
    if side in {"away", "team2"}:
        return "away", "explicit_side"
    return None, None


def market_text(market: dict[str, Any]) -> str:
    return normalize_text(" ".join(clean_text(market.get(key)) for key in ["name", "label", "description", "type", "key", "marketType", "period", "betType"] if key in market))


def is_1x2_market_candidate(market: dict[str, Any]) -> bool:
    text = market_text(market)
    # Keep broad because Arcadia keys can be terse, but still reject obvious non-1X2 markets.
    reject = ["total", "spread", "handicap", "corner", "card", "player", "team total", "both teams"]
    if any(token in text for token in reject):
        return False
    accept = ["moneyline", "money line", "match odds", "1x2", "3 way", "three way", "winner", "full time result"]
    if any(token in text for token in accept):
        return True
    outcomes = collect_outcomes(market)
    return len(outcomes) >= 3


def extract_numeric_line_from_obj(value: Any) -> float | None:
    """Return an explicit betting line from a market/outcome object.

    This is not an odds guess. It only reads line/points/handicap/total values
    already present in the API payload, or explicit text such as "Over 2.5".
    """
    if isinstance(value, dict):
        for key in [
            "line", "points", "point", "handicap", "spread", "total", "totalPoints",
            "value", "threshold", "limit", "number",
        ]:
            raw = value.get(key)
            if isinstance(raw, bool) or raw is None:
                continue
            if isinstance(raw, (int, float)):
                number = float(raw)
                if 0 <= number <= 20:
                    return round(number, 2)
            if isinstance(raw, str):
                token = clean_text(raw)
                m = re.fullmatch(r"\d{1,2}(?:[\.,][05])?", token)
                if m:
                    return round(float(token.replace(",", ".")), 2)
        for key in ["name", "label", "description", "title", "displayName", "marketName"]:
            text = clean_text(value.get(key))
            m = re.search(r"\b(?:over|under|o|u)\s*(\d{1,2}(?:[\.,][05])?)\b", text, re.IGNORECASE)
            if m:
                return round(float(m.group(1).replace(",", ".")), 2)
            m = re.search(r"\b(\d{1,2}(?:[\.,][05])?)\s*(?:goals?|total)\b", text, re.IGNORECASE)
            if m:
                return round(float(m.group(1).replace(",", ".")), 2)
    return None


def outcome_label_text(outcome: dict[str, Any]) -> str:
    return normalize_text(" ".join(
        clean_text(outcome.get(key))
        for key in ["name", "label", "description", "designation", "side", "type", "title", "displayName"]
        if key in outcome
    ))


def outcome_total_side(outcome: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return over/under only from explicit API labels/designations."""
    label = outcome_label_text(outcome)
    raw = normalize_text(json_dumps_safe(outcome, limit=2500))
    if re.search(r"\b(over|o)\b", label):
        return "Over", "explicit_outcome_label"
    if re.search(r"\b(under|u)\b", label):
        return "Under", "explicit_outcome_label"
    # If the payload uses terse codes, accept common explicit fields only.
    for key in ["designation", "type", "side", "name", "label"]:
        value = normalize_text(clean_text(outcome.get(key)))
        if value in {"over", "o"}:
            return "Over", f"explicit_{key}"
        if value in {"under", "u"}:
            return "Under", f"explicit_{key}"
    # Do not infer from arbitrary raw JSON unless the words are explicit and isolated.
    if re.search(r"\b(over|under)\b", raw):
        if re.search(r"\bover\b", raw) and not re.search(r"\bunder\b", raw):
            return "Over", "explicit_raw_over"
        if re.search(r"\bunder\b", raw) and not re.search(r"\bover\b", raw):
            return "Under", "explicit_raw_under"
    return None, None


def is_total_market_candidate(market: dict[str, Any]) -> bool:
    text = market_text(market)
    if any(token in text for token in ["corner", "card", "player", "team total", "team goals", "spread", "handicap"]):
        return False
    if any(token in text for token in ["total", "totals", "over under", "over/under", "match goals", "game total"]):
        return True
    outcomes = collect_outcomes(market)
    sides = {outcome_total_side(outcome)[0] for outcome in outcomes}
    return "Over" in sides and "Under" in sides and extract_numeric_line_from_obj(market) is not None


def build_total_goals_from_api_market(market: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    debug: dict[str, Any] = {
        "marketKeys": list(market.keys())[:24],
        "marketText": market_text(market),
        "outcomesChecked": 0,
        "accepted": False,
        "notes": [],
    }
    if not is_total_market_candidate(market):
        debug["notes"].append("market rejected: not an explicit match total goals candidate")
        return [], debug

    market_line = extract_numeric_line_from_obj(market)
    mapped: dict[str, dict[str, Any]] = {}
    outcomes_debug: list[dict[str, Any]] = []
    for outcome in collect_outcomes(market):
        debug["outcomesChecked"] += 1
        side, side_source = outcome_total_side(outcome)
        odd, price_source = extract_price_from_outcome(outcome)
        line = extract_numeric_line_from_obj(outcome) or market_line
        outcomes_debug.append({
            "keys": list(outcome.keys())[:18],
            "side": side,
            "sideSource": side_source,
            "line": line,
            "odd": odd,
            "priceSource": price_source,
            "sample": json_dumps_safe(outcome, limit=700),
        })
        if side in {"Over", "Under"} and odd is not None and line is not None:
            key = f"{side}:{line:g}"
            if key not in mapped:
                mapped[key] = {"side": side, "line": float(line), "odd": odd, "sideSource": side_source, "priceSource": price_source}

    debug["outcomesSample"] = outcomes_debug[:10]
    debug["mapped"] = mapped
    markets: list[dict[str, Any]] = []
    lines = sorted({item["line"] for item in mapped.values()})
    for line in lines:
        over = mapped.get(f"Over:{line:g}")
        under = mapped.get(f"Under:{line:g}")
        if not over or not under:
            continue
        if not (MIN_ODD <= float(over["odd"]) <= MAX_ODD and MIN_ODD <= float(under["odd"]) <= MAX_ODD):
            continue
        for item in [over, under]:
            unique_market(markets, {
                "market": "MATCH_GOALS",
                "selection": f"{item['side']} {line:g} Goals",
                "team": None,
                "line": line,
                "odd": round(float(item["odd"]), 2),
                "confidence": "high",
                "extraction": "pinnacle_arcadia_api_exact_match_goals",
            })

    if not markets:
        debug["notes"].append("market rejected: explicit Over/Under pair with same line and exact prices not found")
        return [], debug
    debug["accepted"] = True
    debug["notes"].append("accepted exact API mapped match goals Over/Under market")
    return markets, debug


def outcome_yes_no(outcome: dict[str, Any]) -> tuple[str | None, str | None]:
    label = outcome_label_text(outcome)
    for key in ["name", "label", "description", "designation", "side", "type", "title"]:
        value = normalize_text(clean_text(outcome.get(key)))
        if value in {"yes", "y"}:
            return "Yes", f"explicit_{key}"
        if value in {"no", "n"}:
            return "No", f"explicit_{key}"
    if re.search(r"\byes\b", label):
        return "Yes", "explicit_outcome_label"
    if re.search(r"\bno\b", label):
        return "No", "explicit_outcome_label"
    return None, None


def is_btts_market_candidate(market: dict[str, Any]) -> bool:
    text = market_text(market)
    if any(token in text for token in ["both teams", "btts", "both teams to score", "team to score"]):
        return True
    # Do not accept generic yes/no markets unless the market text identifies BTTS.
    return False


def build_btts_from_api_market(market: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    debug: dict[str, Any] = {
        "marketKeys": list(market.keys())[:24],
        "marketText": market_text(market),
        "outcomesChecked": 0,
        "accepted": False,
        "notes": [],
    }
    if not is_btts_market_candidate(market):
        debug["notes"].append("market rejected: not an explicit BTTS candidate")
        return [], debug
    mapped: dict[str, dict[str, Any]] = {}
    outcomes_debug: list[dict[str, Any]] = []
    for outcome in collect_outcomes(market):
        debug["outcomesChecked"] += 1
        yn, yn_source = outcome_yes_no(outcome)
        odd, price_source = extract_price_from_outcome(outcome)
        outcomes_debug.append({
            "keys": list(outcome.keys())[:18],
            "yesNo": yn,
            "yesNoSource": yn_source,
            "odd": odd,
            "priceSource": price_source,
            "sample": json_dumps_safe(outcome, limit=700),
        })
        if yn in {"Yes", "No"} and odd is not None and yn not in mapped:
            mapped[yn] = {"odd": odd, "yesNoSource": yn_source, "priceSource": price_source}
    debug["outcomesSample"] = outcomes_debug[:10]
    debug["mapped"] = mapped
    if not all(side in mapped for side in ["Yes", "No"]):
        debug["notes"].append("market rejected: exact Yes/No BTTS mapping not complete")
        return [], debug

    markets: list[dict[str, Any]] = []
    for label in ["Yes", "No"]:
        odd = float(mapped[label]["odd"])
        if not (MIN_ODD <= odd <= MAX_ODD):
            debug["notes"].append(f"market rejected: {label} odd outside accepted bounds")
            return [], debug
        unique_market(markets, {
            "market": "BTTS",
            "selection": label,
            "team": None,
            "line": None,
            "odd": round(odd, 2),
            "confidence": "high",
            "extraction": "pinnacle_arcadia_api_exact_btts",
        })
    debug["accepted"] = True
    debug["notes"].append("accepted exact API mapped BTTS market")
    return markets, debug


def build_1x2_from_api_market(market: dict[str, Any], fixture: Fixture, participant_map: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    debug: dict[str, Any] = {
        "marketKeys": list(market.keys())[:24],
        "marketText": market_text(market),
        "outcomesChecked": 0,
        "mappedKinds": {},
        "accepted": False,
        "notes": [],
    }
    if not is_1x2_market_candidate(market):
        debug["notes"].append("market rejected: not a 1X2/moneyline candidate")
        return [], debug

    mapped: dict[str, dict[str, Any]] = {}
    outcomes_debug: list[dict[str, Any]] = []
    for outcome in collect_outcomes(market):
        debug["outcomesChecked"] += 1
        odd, price_source = extract_price_from_outcome(outcome)
        kind, kind_source = outcome_kind(outcome, participant_map, fixture)
        outcomes_debug.append({
            "keys": list(outcome.keys())[:18],
            "kind": kind,
            "kindSource": kind_source,
            "odd": odd,
            "priceSource": price_source,
            "sample": json_dumps_safe(outcome, limit=700),
        })
        if kind in {"home", "draw", "away"} and odd is not None and kind not in mapped:
            mapped[kind] = {"odd": odd, "kindSource": kind_source, "priceSource": price_source}

    debug["outcomesSample"] = outcomes_debug[:10]
    debug["mappedKinds"] = mapped
    if not all(kind in mapped for kind in ["home", "draw", "away"]):
        debug["notes"].append("market rejected: exact home/draw/away outcome mapping not complete")
        return [], debug

    home_odd = float(mapped["home"]["odd"])
    draw_odd = float(mapped["draw"]["odd"])
    away_odd = float(mapped["away"]["odd"])
    accepted, notes, confidence = validate_1x2_triplet(home_odd, draw_odd, away_odd)
    debug["notes"].extend(notes)
    if not accepted or confidence != "high":
        return [], debug

    debug["accepted"] = True
    return [
        {
            "market": "1X2",
            "selection": f"{fixture.home_team} Win",
            "team": fixture.home_team,
            "line": None,
            "odd": home_odd,
            "confidence": "high",
            "extraction": "pinnacle_arcadia_api_exact_1x2",
        },
        {
            "market": "1X2",
            "selection": "Draw",
            "team": None,
            "line": None,
            "odd": draw_odd,
            "confidence": "high",
            "extraction": "pinnacle_arcadia_api_exact_1x2",
        },
        {
            "market": "1X2",
            "selection": f"{fixture.away_team} Win",
            "team": fixture.away_team,
            "line": None,
            "odd": away_odd,
            "confidence": "high",
            "extraction": "pinnacle_arcadia_api_exact_1x2",
        },
    ], debug


def build_markets_from_api_payloads(fixture: Fixture, api_payloads: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Extract exact odds from Pinnacle Arcadia JSON payloads.

    This does not infer odds from nearby numbers. It only accepts a market when the
    JSON maps home/draw/away outcomes to explicit participant ids/names/designations.
    """
    debug: dict[str, Any] = {
        "strategy": "pinnacle_arcadia_api_exact_mapping",
        "payloadsChecked": len(api_payloads),
        "candidateObjectsChecked": 0,
        "participantMappedObjects": 0,
        "marketCandidatesChecked": 0,
        "accepted": False,
        "notes": [],
        "samples": [],
    }
    markets: list[dict[str, Any]] = []

    for payload in api_payloads:
        root = payload.get("jsonRoot")
        url = str(payload.get("url") or "")
        for obj in iter_dicts(root, max_depth=8):
            obj_text = json_dumps_safe(obj, limit=30000)
            if not (contains_team(obj_text, fixture.home_team) and contains_team(obj_text, fixture.away_team)):
                continue
            if "market" not in normalize_text(obj_text) and "price" not in normalize_text(obj_text):
                continue
            debug["candidateObjectsChecked"] += 1
            participant_map, participant_debug = map_fixture_participants(obj, fixture)
            if not participant_map:
                if len(debug["samples"]) < 8:
                    debug["samples"].append({
                        "url": url,
                        "reason": "fixture teams present but participants could not be mapped exactly",
                        "participantDebug": participant_debug,
                        "objectKeys": list(obj.keys())[:24],
                    })
                continue
            debug["participantMappedObjects"] += 1
            for market in collect_market_objects(obj):
                debug["marketCandidatesChecked"] += 1
                one_x_two_markets, one_x_two_debug = build_1x2_from_api_market(market, fixture, participant_map)
                total_markets, total_debug = build_total_goals_from_api_market(market)
                btts_markets, btts_debug = build_btts_from_api_market(market)
                if len(debug["samples"]) < 10:
                    debug["samples"].append({
                        "url": url,
                        "participantDebug": participant_debug,
                        "marketDebug": {
                            "1X2": one_x_two_debug,
                            "MATCH_GOALS": total_debug,
                            "BTTS": btts_debug,
                        },
                    })
                for candidate in one_x_two_markets + total_markets + btts_markets:
                    unique_market(markets, candidate)
            if markets:
                debug["accepted"] = True
                debug["notes"].append(f"accepted exact API mapped markets from {url}; counts={count_market_types(markets)}")
                # Continue scanning payloads after finding 1X2, because totals/BTTS can live in
                # separate market containers or nearby objects. Stop only after this fixture has
                # gathered all high-confidence markets visible in this candidate object.
                return markets, debug

    if not markets:
        debug["notes"].append("No exact API-mapped 1X2/MATCH_GOALS/BTTS market found; singles remain empty.")
    return markets, debug


def count_fixture_pairs_in_text(text: str, fixtures: list[Fixture], limit: int = 12) -> tuple[int, list[dict[str, Any]]]:
    samples: list[dict[str, Any]] = []
    matched_count = 0
    for fixture in fixtures:
        home_found = contains_team(text, fixture.home_team)
        away_found = contains_team(text, fixture.away_team)
        if home_found and away_found:
            matched_count += 1
            if len(samples) < limit:
                samples.append({
                    "matchId": fixture.match_id,
                    "homeTeam": fixture.home_team,
                    "awayTeam": fixture.away_team,
                })
    return matched_count, samples


def analyse_response_body(url: str, status: int | None, content_type: str, body: str, fixtures: list[Fixture]) -> dict[str, Any]:
    text = body or ""
    json_root: Any | None = None
    json_error: str | None = None
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("[") or "json" in (content_type or "").lower():
        try:
            json_root = json.loads(text)
        except Exception as exc:  # noqa: BLE001 - diagnostics only.
            json_error = f"{type(exc).__name__}: {exc}"

    fixture_pairs_count, fixture_pairs_sample = count_fixture_pairs_in_text(text, fixtures)
    keyword_hits = {
        keyword: keyword in normalize_text(text)
        for keyword in ["soccer", "football", "world cup", "fifa", "odds", "market", "markets", "price", "prices", "event", "events"]
    }
    odds_count = len(odds_tokens(text))
    score = 0
    if status == 200:
        score += 20
    if json_root is not None:
        score += 40
    score += min(fixture_pairs_count * 12, 60)
    score += min(odds_count, 30)
    score += sum(3 for hit in keyword_hits.values() if hit)

    return {
        "url": url,
        "status": status,
        "contentType": content_type,
        "bodyLength": len(text),
        "oddsLikeNumbers": odds_count,
        "fixturePairsInBody": fixture_pairs_count,
        "fixturePairsSample": fixture_pairs_sample,
        "keywordHits": keyword_hits,
        "jsonParsed": json_root is not None,
        "jsonError": json_error,
        "jsonShape": summarize_json_root(json_root) if json_root is not None else None,
        "score": score,
        "bodySample": text[:MAX_NETWORK_BODY_CHARS],
    }


def keyword_presence(visible_text: str, html: str) -> dict[str, dict[str, bool]]:
    vt = normalize_text(visible_text)
    ht = normalize_text(html)
    return {
        keyword: {
            "visibleText": keyword in vt,
            "html": keyword in ht,
        }
        for keyword in KEYWORDS
    }


def split_lines(text: str) -> list[str]:
    return [clean_text(line) for line in (text or "").splitlines() if clean_text(line)]


def find_fixture_window(visible_text: str, html: str, fixture: Fixture) -> tuple[str, str | None]:
    """Return a local text window that appears to contain both teams."""
    lines = split_lines(visible_text)
    home_indices = [idx for idx, line in enumerate(lines) if line_has_team(line, fixture.home_team)]
    away_indices = [idx for idx, line in enumerate(lines) if line_has_team(line, fixture.away_team)]

    best_pair: tuple[int, int] | None = None
    best_distance = 999999
    for hi in home_indices:
        for ai in away_indices:
            distance = abs(hi - ai)
            if distance < best_distance:
                best_distance = distance
                best_pair = (hi, ai)

    if best_pair and best_distance <= 28:
        start = max(0, min(best_pair) - 14)
        end = min(len(lines), max(best_pair) + 30)
        return "\n".join(lines[start:end]), f"visible lines {start}-{end}, team distance={best_distance}"

    # Fallback: inspect normalized HTML/text, useful when teams are in embedded JSON/script text.
    combined = clean_text(visible_text + "\n" + html)
    norm_combined = normalize_text(combined)
    positions_home = [norm_combined.find(v) for v in team_variants(fixture.home_team) if norm_combined.find(v) >= 0]
    positions_away = [norm_combined.find(v) for v in team_variants(fixture.away_team) if norm_combined.find(v) >= 0]
    if positions_home and positions_away:
        best_home = min(positions_home)
        best_away = min(positions_away)
        if abs(best_home - best_away) <= 6000:
            center = min(best_home, best_away)
            start = max(0, center - 1800)
            end = min(len(combined), center + 4200)
            return combined[start:end], f"combined text/html window, distance={abs(best_home - best_away)}"

    return "", None


def is_draw_label(line: str) -> bool:
    norm = normalize_text(line)
    return norm in {"draw", "x", "tie"} or norm.startswith("draw ")


def first_odd_in_text(text: str) -> float | None:
    tokens = odds_tokens(text)
    return tokens[0] if tokens else None


def find_odd_near_label(lines: list[str], label_index: int, max_ahead: int = 4) -> tuple[float | None, str | None]:
    """Find the first decimal odd on the label line or shortly after it."""
    for offset in range(0, max_ahead + 1):
        idx = label_index + offset
        if idx >= len(lines):
            break
        odd = first_odd_in_text(lines[idx])
        if odd is not None:
            return odd, f"line {idx} (+{offset})"
    return None, None


def validate_1x2_triplet(home_odd: float, draw_odd: float, away_odd: float) -> tuple[bool, list[str], str]:
    """Validate a 1X2 triplet without estimating or inferring prices.

    Important StatMaker rule: single bet odds must be exact bookmaker odds.
    Therefore this function is only called after explicit Home/Draw/Away mapping has
    already been found. It performs sanity checks; it does not infer missing prices.
    """
    notes: list[str] = []
    values = [home_odd, draw_odd, away_odd]
    spread = max(values) - min(values)
    implied_sum = sum(1.0 / value for value in values if value > 0)

    if not all(MIN_ODD <= value <= MAX_ODD for value in values):
        notes.append("rejected: at least one 1X2 odd is outside accepted decimal-odds bounds")
    if all(round(value, 2) == round(values[0], 2) for value in values):
        notes.append("rejected: all three 1X2 odds are identical; likely generic/nearby tokens, not exact 1X2")
    if not (0.95 <= implied_sum <= 1.55):
        notes.append(f"rejected: implied probability sum out of expected range ({implied_sum:.3f})")

    if notes:
        return False, notes, "rejected"

    notes.append(
        f"accepted: explicit labelled Home/Draw/Away odds; exact single odds only; "
        f"spread={spread:.2f}, impliedSum={implied_sum:.3f}"
    )
    return True, notes, "high"


def extract_1x2_markets(window: str, fixture: Fixture) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Extract labelled 1X2 odds only when Home/Draw/Away are clearly mapped.

    The first scraper version took the first three decimal tokens in a fixture window.
    That produced false prices when nearby generic tokens repeated, e.g. 2.25/2.25/2.25.
    This version requires labelled team lines plus an explicit Draw line. If confidence
    is not sufficient, it emits no 1X2 market and explains why in debug_report.json.
    """
    markets: list[dict[str, Any]] = []
    lines = split_lines(window)
    tokens = odds_tokens(window)
    diagnostic: dict[str, Any] = {
        "strategy": "labelled_home_draw_away",
        "oddsTokensInWindow": tokens[:24],
        "candidateCount": 0,
        "accepted": False,
        "notes": [],
    }

    home_indices = [idx for idx, line in enumerate(lines) if line_has_team(line, fixture.home_team)]
    away_indices = [idx for idx, line in enumerate(lines) if line_has_team(line, fixture.away_team)]
    draw_indices = [idx for idx, line in enumerate(lines) if is_draw_label(line)]
    diagnostic["homeLineIndices"] = home_indices[:8]
    diagnostic["drawLineIndices"] = draw_indices[:8]
    diagnostic["awayLineIndices"] = away_indices[:8]

    candidates: list[dict[str, Any]] = []
    for home_idx in home_indices:
        for draw_idx in draw_indices:
            for away_idx in away_indices:
                # Most sportsbook 1X2 blocks are Home / Draw / Away. Keep this conservative.
                if not (home_idx <= draw_idx <= away_idx):
                    continue
                if away_idx - home_idx > 18:
                    continue
                home_odd, home_odd_source = find_odd_near_label(lines, home_idx)
                draw_odd, draw_odd_source = find_odd_near_label(lines, draw_idx)
                away_odd, away_odd_source = find_odd_near_label(lines, away_idx)
                if home_odd is None or draw_odd is None or away_odd is None:
                    continue
                accepted, notes, confidence = validate_1x2_triplet(home_odd, draw_odd, away_odd)
                candidate = {
                    "homeLine": home_idx,
                    "drawLine": draw_idx,
                    "awayLine": away_idx,
                    "lineSpan": away_idx - home_idx,
                    "homeOdd": home_odd,
                    "drawOdd": draw_odd,
                    "awayOdd": away_odd,
                    "homeOddSource": home_odd_source,
                    "drawOddSource": draw_odd_source,
                    "awayOddSource": away_odd_source,
                    "accepted": accepted,
                    "confidence": confidence,
                    "notes": notes,
                }
                candidates.append(candidate)

    diagnostic["candidateCount"] = len(candidates)
    diagnostic["candidates"] = candidates[:8]

    accepted_candidates = [candidate for candidate in candidates if candidate.get("accepted")]
    if not accepted_candidates:
        if not draw_indices:
            diagnostic["notes"].append("No explicit Draw line found near fixture; 1X2 skipped.")
        elif not candidates:
            diagnostic["notes"].append("No Home/Draw/Away labelled odds candidate found; 1X2 skipped.")
        else:
            diagnostic["notes"].append("All labelled 1X2 candidates rejected by confidence checks.")
        return markets, diagnostic

    best = sorted(
        accepted_candidates,
        key=lambda item: (0 if item.get("confidence") == "high" else 1, int(item.get("lineSpan") or 999)),
    )[0]
    diagnostic["accepted"] = True
    diagnostic["selectedCandidate"] = best

    home_odd = float(best["homeOdd"])
    draw_odd = float(best["drawOdd"])
    away_odd = float(best["awayOdd"])
    confidence = str(best.get("confidence") or "medium")

    unique_market(markets, {
        "market": "1X2",
        "selection": f"{fixture.home_team} Win",
        "team": fixture.home_team,
        "line": None,
        "odd": home_odd,
        "confidence": confidence,
        "extraction": "labelled_home_draw_away",
    })
    unique_market(markets, {
        "market": "1X2",
        "selection": "Draw",
        "team": None,
        "line": None,
        "odd": draw_odd,
        "confidence": confidence,
        "extraction": "labelled_home_draw_away",
    })
    unique_market(markets, {
        "market": "1X2",
        "selection": f"{fixture.away_team} Win",
        "team": fixture.away_team,
        "line": None,
        "odd": away_odd,
        "confidence": confidence,
        "extraction": "labelled_home_draw_away",
    })
    return markets, diagnostic


def extract_total_markets(window: str) -> list[dict[str, Any]]:
    markets: list[dict[str, Any]] = []
    text = clean_text(window)

    patterns = [
        # Over 2.5 1.91 / Under 2.5 1.94
        r"\b(Over|Under)\s+([0-9]+(?:\.[05])?)\s+((?:1[\.,][0-9]{2})|(?:[2-9][\.,][0-9]{2})|(?:[1-9][0-9][\.,][0-9]{2}))",
        # O 2.5 1.91 / U 2.5 1.94
        r"\b(O|U)\s*([0-9]+(?:\.[05])?)\s+((?:1[\.,][0-9]{2})|(?:[2-9][\.,][0-9]{2})|(?:[1-9][0-9][\.,][0-9]{2}))",
    ]
    for pattern in patterns:
        for side, line_raw, odd_raw in re.findall(pattern, text, re.IGNORECASE):
            odd = parse_decimal_odd(odd_raw)
            if odd is None:
                continue
            line = float(line_raw)
            side_label = "Over" if side.lower().startswith("o") else "Under"
            unique_market(markets, {
                "market": "MATCH_GOALS",
                "selection": f"{side_label} {line:g} Goals",
                "team": None,
                "line": line,
                "odd": odd,
                "confidence": "high",
                "extraction": "explicit_over_under_line",
            })
    return markets


def extract_btts_markets(window: str) -> list[dict[str, Any]]:
    markets: list[dict[str, Any]] = []
    lowered = clean_text(window).lower()
    if "both teams" not in lowered and "btts" not in lowered:
        return markets
    # Look in a short BTTS context if possible.
    match = re.search(r"(both teams(?: to score)?|btts).{0,260}", window, re.IGNORECASE | re.DOTALL)
    context = match.group(0) if match else window
    for label in ["Yes", "No"]:
        m = re.search(label + r"\s+((?:1[\.,][0-9]{2})|(?:[2-9][\.,][0-9]{2})|(?:[1-9][0-9][\.,][0-9]{2}))", context, re.IGNORECASE)
        if not m:
            continue
        odd = parse_decimal_odd(m.group(1))
        if odd is None:
            continue
        unique_market(markets, {
            "market": "BTTS",
            "selection": label,
            "team": None,
            "line": None,
            "odd": odd,
            "confidence": "high",
            "extraction": "explicit_btts_yes_no_line",
        })
    return markets


def build_markets_from_window(window: str, fixture: Fixture) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    markets: list[dict[str, Any]] = []
    extraction_debug: dict[str, Any] = {}

    one_x_two_markets, one_x_two_debug = extract_1x2_markets(window, fixture)
    extraction_debug["1X2"] = one_x_two_debug
    for candidate in one_x_two_markets:
        unique_market(markets, candidate)

    total_markets = extract_total_markets(window)
    extraction_debug["MATCH_GOALS"] = {"marketsExtracted": len(total_markets)}
    for candidate in total_markets:
        unique_market(markets, candidate)

    btts_markets = extract_btts_markets(window)
    extraction_debug["BTTS"] = {"marketsExtracted": len(btts_markets)}
    for candidate in btts_markets:
        unique_market(markets, candidate)

    return markets, extraction_debug


def count_market_types(markets: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for market in markets:
        key = str(market.get("market") or "UNKNOWN")
        counts[key] = counts.get(key, 0) + 1
    return counts


def select_best_page(fixtures: list[Fixture]) -> tuple[dict[str, Any], str, str, list[dict[str, Any]]]:
    attempts: list[dict[str, Any]] = []
    best: dict[str, Any] | None = None
    best_visible = ""
    best_html = ""
    api_payloads: list[dict[str, Any]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1365, "height": 900},
            locale="en-GB",
            timezone_id="Europe/London",
        )

        for url in PINNACLE_URLS:
            response_status: int | None = None
            final_url = url
            title = ""
            visible_text = ""
            html = ""
            error: str | None = None
            network_candidates: list[dict[str, Any]] = []
            seen_candidate_urls: set[str] = set()
            page = context.new_page()

            def capture_response(response: Any) -> None:
                if len(network_candidates) >= MAX_NETWORK_API_CANDIDATES:
                    return
                response_url = str(getattr(response, "url", "") or "")
                if not is_api_candidate_url(response_url):
                    return
                if response_url in seen_candidate_urls:
                    return
                seen_candidate_urls.add(response_url)
                status = None
                content_type = ""
                body = ""
                body_error = None
                try:
                    status = int(response.status)
                    headers = response.headers or {}
                    content_type = clean_text(headers.get("content-type") or headers.get("Content-Type") or "")
                    # Pull bodies only for likely useful API/JSON responses. JS chunks are already
                    # listed from HTML candidates and are usually too noisy for exact odds extraction.
                    lower_url = response_url.lower()
                    if status == 200 and (
                        "json" in content_type.lower()
                        or any(token in lower_url for token in ["api", "event", "market", "odds", "matchup", "sportsbook"])
                    ):
                        body = response.text()
                except Exception as exc:  # noqa: BLE001 - diagnostics must not crash scraping.
                    body_error = f"{type(exc).__name__}: {exc}"

                candidate = analyse_response_body(response_url, status, content_type, body, fixtures)
                if body_error:
                    candidate["bodyReadError"] = body_error
                # Keep parsed Arcadia JSON in memory for exact odds extraction. Do not dump
                # full payloads to debug_report.json; they are summarized separately.
                if status == 200 and body and "json" in (content_type or "").lower():
                    try:
                        parsed_root = json.loads(body)
                        if "guest.api.arcadia.pinnacle.com" in response_url.lower() and "matchup" in response_url.lower():
                            api_payloads.append({"url": response_url, "jsonRoot": parsed_root})
                    except Exception:
                        pass
                network_candidates.append(candidate)

            page.on("response", capture_response)
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
                response_status = response.status if response else None
                page.wait_for_timeout(4500)
                title = page.title() or ""
                final_url = page.url or url
                visible_text = page.locator("body").inner_text(timeout=7000) if page.locator("body").count() else ""
                html = page.content()
            except PlaywrightTimeoutError as exc:
                error = f"timeout: {exc}"
            except Exception as exc:  # noqa: BLE001 - diagnostics should capture site-specific failures.
                error = f"{type(exc).__name__}: {exc}"
            finally:
                try:
                    page.close()
                except Exception:
                    pass

            html_api_candidates = extract_candidate_urls(html, final_url)
            network_candidates_sorted = sorted(network_candidates, key=lambda item: int(item.get("score") or 0), reverse=True)
            result = {
                "requestedUrl": url,
                "finalUrl": final_url,
                "httpStatus": response_status,
                "title": title,
                "visibleTextLength": len(visible_text or ""),
                "htmlLength": len(html or ""),
                "oddsLikeNumbersInVisibleText": len(odds_tokens(visible_text)),
                "oddsLikeNumbersInHtml": len(odds_tokens(html)),
                "scriptApiCandidatesCount": len(html_api_candidates),
                "htmlApiCandidatesSample": html_api_candidates[:25],
                "networkApiCandidatesCount": len(network_candidates_sorted),
                "networkApiCandidatesTop": network_candidates_sorted[:12],
                "error": error,
            }
            score = 0
            if response_status == 200:
                score += 40
            score += min(result["visibleTextLength"] // 100, 40)
            score += min(result["oddsLikeNumbersInVisibleText"], 80)
            score += min(result["scriptApiCandidatesCount"], 25)
            if network_candidates_sorted:
                score += min(int(network_candidates_sorted[0].get("score") or 0), 80)
            result["score"] = score
            attempts.append(result)

            if best is None or score > int(best.get("score") or 0):
                best = result
                best_visible = visible_text
                best_html = html

        context.close()
        browser.close()

    # Do not attach the attempts list to the original `best` dict object when it
    # is also one of the entries inside `attempts`; that creates a circular
    # reference and json.dumps(debug) fails on GitHub Actions.
    if best is None:
        best_summary: dict[str, Any] = {"error": "No Pinnacle candidate page could be inspected."}
    else:
        best_summary = dict(best)
    best_summary["attempts"] = [dict(item) for item in attempts]
    return best_summary, best_visible, best_html, api_payloads


def make_empty_feed(generated_at: str) -> dict[str, Any]:
    return {
        "source": SOURCE,
        "bookmaker": BOOKMAKER,
        "generatedAt": generated_at,
        "country": "International",
        "competition": COMPETITION,
        "season": SEASON,
        "oddsPolicy": {
            "singleOdds": "exact_bookmaker_odds_only",
            "noApproximateSingles": True,
            "emitMarketOnlyWhenConfidence": "high",
            "betBuilderTotalsMayBeEstimatedInAndroid": True,
        },
        "matches": [],
    }


def write_snapshot(best_page: dict[str, Any], visible_text: str, html: str, fixtures: list[Fixture], matched_debug: list[dict[str, Any]]) -> None:
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    fixture_presence = []
    for fixture in fixtures[:16]:
        fixture_presence.append({
            "matchId": fixture.match_id,
            "date": fixture.date,
            "time": fixture.time,
            "homeTeam": fixture.home_team,
            "awayTeam": fixture.away_team,
            "homeInVisibleText": contains_team(visible_text, fixture.home_team),
            "awayInVisibleText": contains_team(visible_text, fixture.away_team),
            "homeInHtml": contains_team(html, fixture.home_team),
            "awayInHtml": contains_team(html, fixture.away_team),
        })

    lines = [
        "StatMaker Pinnacle probe snapshot",
        json.dumps({
            "generatedAt": utc_now(),
            "scriptVersion": SCRIPT_VERSION,
            "source": SOURCE,
            "bookmaker": BOOKMAKER,
            "bestPage": best_page,
            "fixturesLoaded": len(fixtures),
            "snapshotNote": "Diagnostic only. Do not consume this file from the Android app.",
        }, ensure_ascii=False, indent=2),
        "",
        "===== Keyword presence =====",
        json.dumps(keyword_presence(visible_text, html), ensure_ascii=False, indent=2),
        "",
        "===== Fixture presence sample =====",
        json.dumps(fixture_presence, ensure_ascii=False, indent=2),
        "",
        "===== Matched fixture extraction sample =====",
        json.dumps(matched_debug[:12], ensure_ascii=False, indent=2),
        "",
        "===== HTML script/API candidates =====",
        "\n".join(extract_candidate_urls(html, best_page.get("finalUrl") or "")) or "None found.",
        "",
        "===== Network/API response candidates =====",
        json.dumps(best_page.get("networkApiCandidatesTop", []), ensure_ascii=False, indent=2),
        "",
        "===== Visible text sample =====",
        (visible_text or "")[:14000] or "<empty>",
        "",
        "===== HTML sample =====",
        (html or "")[:14000] or "<empty>",
        "",
    ]
    SNAPSHOT_PATH.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    generated_at = utc_now()
    fixtures = load_fixtures()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEBUG_PATH.parent.mkdir(parents=True, exist_ok=True)

    errors: list[str] = []
    best_page, visible_text, html, api_payloads = select_best_page(fixtures)

    output = make_empty_feed(generated_at)
    matched_debug: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []

    for fixture in fixtures:
        window, window_reason = find_fixture_window(visible_text, html, fixture)
        if not window:
            unmatched.append({
                "matchId": fixture.match_id,
                "date": fixture.date,
                "time": fixture.time,
                "homeTeam": fixture.home_team,
                "awayTeam": fixture.away_team,
                "reason": "fixture teams not found close enough in Pinnacle visible text/html",
            })
            continue

        api_markets, api_extraction_debug = build_markets_from_api_payloads(fixture, api_payloads)
        if api_markets:
            markets = api_markets
            visible_markets: list[dict[str, Any]] = []
            visible_extraction_debug: dict[str, Any] = {"skipped": "API exact mapping accepted; visible-text parsing not needed."}
        else:
            visible_markets, visible_extraction_debug = build_markets_from_window(window, fixture)
            # Visible-text extraction is still exact/high-confidence only. It is kept as a
            # fallback, but current Pinnacle layout usually has no labelled Home/Draw/Away.
            markets = visible_markets

        extraction_debug = {
            "API": api_extraction_debug,
            "VISIBLE_TEXT": visible_extraction_debug,
        }
        debug_item = {
            "matchId": fixture.match_id,
            "date": fixture.date,
            "time": fixture.time,
            "homeTeam": fixture.home_team,
            "awayTeam": fixture.away_team,
            "windowReason": window_reason,
            "oddsTokensInWindow": odds_tokens(window)[:24],
            "marketsExtracted": len(markets),
            "marketCounts": count_market_types(markets),
            "extractionDebug": extraction_debug,
            "windowSample": window[:1800],
        }
        matched_debug.append(debug_item)

        if not markets:
            unmatched.append({
                "matchId": fixture.match_id,
                "date": fixture.date,
                "time": fixture.time,
                "homeTeam": fixture.home_team,
                "awayTeam": fixture.away_team,
                "reason": "fixture found, but no conservative odds market could be extracted",
                "windowReason": window_reason,
                "oddsTokensInWindow": odds_tokens(window)[:12],
                "extractionDebug": extraction_debug,
            })
            continue

        output["matches"].append({
            "date": fixture.date,
            "homeTeam": fixture.home_team,
            "awayTeam": fixture.away_team,
            "markets": markets,
        })

    debug = {
        "source": SOURCE,
        "bookmaker": BOOKMAKER,
        "generatedAt": generated_at,
        "scriptVersion": SCRIPT_VERSION,
        "fixturesPath": str(FIXTURES_PATH),
        "outputPath": str(OUTPUT_PATH),
        "debugPath": str(DEBUG_PATH),
        "snapshotPath": str(SNAPSHOT_PATH),
        "bestPage": best_page,
        "apiPayloadsLoaded": len(api_payloads),
        "apiPayloads": [
            {
                "url": str(payload.get("url") or ""),
                "jsonShape": summarize_json_root(payload.get("jsonRoot")),
            }
            for payload in api_payloads[:12]
        ],
        "fixturesLoaded": len(fixtures),
        "matchesMatched": len(output["matches"]),
        "matchesWithMarkets": len(output["matches"]),
        "marketsFound": sum(len(match["markets"]) for match in output["matches"]),
        "marketCounts": count_market_types([market for match in output["matches"] for market in match["markets"]]),
        "matchedFixtureDebug": matched_debug,
        "unmatchedFixtures": unmatched,
        "errors": errors,
        "notes": [
            "Single bet odds policy: exact bookmaker odds only; no approximate or inferred singles are emitted.",
            "Bet Builder total odds may be estimated only in the Android app when actual builder prices are unavailable; single selection odds remain exact-only.",
            "This scraper rejects low-confidence 1X2 extraction, including identical generic/nearby tokens.",
            "Only high-confidence labelled Home/Draw/Away 1X2 markets are emitted. Inspect matchedFixtureDebug/extractionDebug after each run.",
            "If too few markets are emitted, inspect bestPage.networkApiCandidatesTop for Pinnacle JSON/API endpoints rather than accepting nearby visible-text odds.",
            "This version parses Pinnacle Arcadia API JSON first, including exact 1X2, match goals Over/Under, and BTTS when explicitly mapped.",
        ],
    }

    OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    DEBUG_PATH.write_text(json.dumps(debug, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_snapshot(best_page, visible_text, html, fixtures, matched_debug)


if __name__ == "__main__":
    main()
