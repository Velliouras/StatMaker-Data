#!/usr/bin/env python3
from __future__ import annotations

import difflib
import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import update_domestic_odds_api_io as odds

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "domestic_leagues.json"
LIVE = ROOT / "data" / "statmaker" / "domestic_live_july_registry.json"
ALIASES = ROOT / "mappings" / "domestic_team_aliases.json"
REPORT = ROOT / "reports" / "domestic_mapping_audit.json"
CACHE_ROOT = ROOT / "data" / "api_football" / "fixture_stats"
MIN_REMAINING = 25
BAD_QUALIFIERS = {
    "simulated", "reality", "srl", "women", "reserves", "reserve",
    "u23", "u21", "u20", "u19", "youth", "friendly", "cup"
}


def load(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def norm(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def simplified(value: Any) -> str:
    return odds.simplified_team_name(str(value or ""))


def token_key(value: Any) -> str:
    return " ".join(sorted(x for x in simplified(value).split() if x))


def league_id_to_code(config: Dict[str, Any]) -> Dict[int, str]:
    out: Dict[int, str] = {}
    for row in config.get("leagues", []) or []:
        try:
            out[int(row.get("apiFootballLeagueId"))] = str(row.get("leagueCode") or "")
        except (TypeError, ValueError):
            pass
    return out


def canonical_teams(config: Dict[str, Any]) -> Dict[str, List[str]]:
    by_id = league_id_to_code(config)
    result: Dict[str, set[str]] = {}
    for path in CACHE_ROOT.glob("**/fixture_stats.json"):
        payload = load(path, {})
        try:
            code = by_id.get(int(payload.get("league_id")))
        except (TypeError, ValueError):
            code = None
        if not code:
            continue
        bucket = result.setdefault(code, set())
        for fixture in payload.get("fixtures", []) or []:
            if not isinstance(fixture, dict):
                continue
            for key in ("home_team", "away_team"):
                name = str(fixture.get(key) or "").strip()
                if name:
                    bucket.add(name)
    return {code: sorted(names) for code, names in result.items()}


def explicit_aliases() -> Dict[str, Dict[str, str]]:
    raw = load(ALIASES, {}).get("aliases", {})
    result: Dict[str, Dict[str, str]] = {}
    for code, teams in raw.items():
        bucket = result.setdefault(str(code), {})
        for canonical, aliases in (teams or {}).items():
            variants = [canonical, *(aliases or [])]
            for variant in variants:
                for key in (norm(variant), simplified(variant), token_key(variant)):
                    if key:
                        bucket[key] = canonical
    return result


def generated_aliases(config: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    result = explicit_aliases()
    for code, names in canonical_teams(config).items():
        bucket = result.setdefault(code, {})
        owners: Dict[str, set[str]] = {}
        for canonical in names:
            for key in (norm(canonical), simplified(canonical), token_key(canonical)):
                if key:
                    owners.setdefault(key, set()).add(canonical)
        for key, values in owners.items():
            if len(values) == 1:
                bucket.setdefault(key, next(iter(values)))
    return result


def country_terms(country: Any) -> List[str]:
    key = norm(country)
    extras = {
        "czech republic": ["czech republic", "czechia", "czech"],
        "usa": ["usa", "united states", "mls"],
    }
    return extras.get(key, odds.country_aliases_for(country))


def bad_qualifier_penalty(config_row: Dict[str, Any], provider: Dict[str, Any]) -> int:
    requested = norm(f"{config_row.get('competition', '')} {' '.join(config_row.get('searchTerms', []) or [])}")
    hay = norm(f"{provider.get('name', '')} {provider.get('slug', '')}")
    penalty = 0
    for token in BAD_QUALIFIERS:
        if token in hay and token not in requested:
            penalty += 500
    return penalty


def division_penalty(config_row: Dict[str, Any], provider: Dict[str, Any]) -> int:
    req = norm(f"{config_row.get('competition', '')} {' '.join(config_row.get('searchTerms', []) or [])}")
    hay = norm(f"{provider.get('name', '')} {provider.get('slug', '')}")
    pairs = [
        ("j1", ["j2", "j3"]),
        ("j2", ["j1", "j3"]),
        ("j3", ["j1", "j2"]),
        ("serie a", ["serie b", "serie c"]),
        ("serie b", ["serie a", "serie c"]),
        ("league one", ["league two"]),
        ("league two", ["league one"]),
    ]
    for wanted, wrongs in pairs:
        if wanted in req and any(wrong in hay for wrong in wrongs):
            return 700
    return 0


def league_score(config_row: Dict[str, Any], provider: Dict[str, Any]) -> int:
    hay = norm(f"{provider.get('name', '')} {provider.get('slug', '')}")
    countries = [norm(x) for x in country_terms(config_row.get("country"))]
    if not any(x and x in hay for x in countries):
        return -10000
    score = 0
    terms = [config_row.get("competition", ""), *(config_row.get("searchTerms", []) or [])]
    for raw_term in terms:
        term = norm(raw_term)
        if not term:
            continue
        if term in hay:
            score = max(score, 300 + len(term))
        words = [w for w in term.split() if len(w) > 2]
        hits = sum(1 for w in words if w in hay)
        if words and hits == len(words):
            score = max(score, 180 + 10 * hits)
        elif hits >= 2:
            score = max(score, 80 + 5 * hits)
    configured = str(config_row.get("providerLeagueSlug") or "").strip()
    if configured and str(provider.get("slug") or "") == configured:
        score += 250
    score -= bad_qualifier_penalty(config_row, provider)
    score -= division_penalty(config_row, provider)
    return score


def provider_candidates(config_row: Dict[str, Any], providers: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ranked = []
    for item in providers:
        score = league_score(config_row, item)
        if score <= 0:
            continue
        ranked.append({
            "name": item.get("name"),
            "slug": item.get("slug"),
            "eventsCount": item.get("eventsCount"),
            "score": score,
        })
    return sorted(ranked, key=lambda x: (-int(x.get("score") or 0), -int(x.get("eventsCount") or 0), str(x.get("slug") or "")))[:5]


def map_team(provider_name: str, code: str, aliases: Dict[str, Dict[str, str]], canonicals: Dict[str, List[str]]) -> Tuple[Optional[str], str, List[Dict[str, Any]]]:
    bucket = aliases.get(code, {})
    keys = [norm(provider_name), simplified(provider_name), token_key(provider_name)]
    for key in keys:
        if key and key in bucket:
            return bucket[key], "exact-normalized", []

    p_simple = simplified(provider_name)
    p_tokens = set(p_simple.split())
    suggestions: List[Tuple[float, str, str]] = []
    for canonical in canonicals.get(code, []):
        c_simple = simplified(canonical)
        c_tokens = set(c_simple.split())
        if not c_simple:
            continue
        if p_tokens and c_tokens and (p_tokens <= c_tokens or c_tokens <= p_tokens):
            shorter = min(len(p_tokens), len(c_tokens))
            longer = max(len(p_tokens), len(c_tokens))
            score = 0.92 + (0.05 * shorter / max(longer, 1))
        else:
            score = difflib.SequenceMatcher(None, p_simple, c_simple).ratio()
        if score >= 0.68:
            suggestions.append((score, canonical, c_simple))
    suggestions.sort(reverse=True)
    top = [
        {"canonical": canonical, "score": round(score, 4)}
        for score, canonical, _ in suggestions[:3]
    ]
    if suggestions:
        best = suggestions[0]
        second = suggestions[1][0] if len(suggestions) > 1 else 0.0
        if best[0] >= 0.9 and best[0] - second >= 0.08:
            return best[1], "high-confidence-suggestion", top
    return None, "unmatched", top


def event_teams(events: Iterable[Dict[str, Any]]) -> List[str]:
    names = set()
    for event in events:
        if not isinstance(event, dict):
            continue
        for name in (odds.event_home(event), odds.event_away(event)):
            if name:
                names.add(name)
    return sorted(names)


def main() -> int:
    api_key = os.getenv("ODDS_API_IO_KEY", "").strip()
    if not api_key:
        raise SystemExit("ODDS_API_IO_KEY is required")

    config = load(CONFIG, {})
    live = load(LIVE, {})
    config_by_code = {str(x.get("leagueCode") or ""): x for x in config.get("leagues", []) or []}
    live_codes = [str(x.get("leagueCode") or "") for x in live.get("leagues", []) or []]
    selected = [config_by_code[c] for c in live_codes if c in config_by_code]
    if not selected:
        selected = [x for x in config.get("leagues", []) or [] if bool(x.get("enabled", True))]

    aliases = generated_aliases(config)
    canonicals = canonical_teams(config)
    debug: Dict[str, Any] = {"warnings": [], "apiCalls": []}
    providers = odds.discover_provider_leagues(api_key, debug)

    league_rows = []
    team_rows = []
    for row in selected:
        code = str(row.get("leagueCode") or "")
        candidates = provider_candidates(row, providers)
        chosen = candidates[0] if candidates else None
        league_rows.append({
            "leagueCode": code,
            "country": row.get("country"),
            "competition": row.get("competition"),
            "configuredSlug": row.get("providerLeagueSlug"),
            "bestCandidate": chosen,
            "candidates": candidates,
        })
        if not chosen or int(chosen.get("eventsCount") or 0) <= 0:
            continue
        remaining = debug.get("rateLimitRemaining")
        if isinstance(remaining, int) and remaining <= MIN_REMAINING:
            debug.setdefault("warnings", []).append("Stopped event audit at rate-limit guard")
            break
        events = odds.fetch_events_for_league(api_key, str(chosen.get("slug") or ""), int(config.get("horizonDays") or 21), debug)
        mapped = []
        unmatched = []
        for provider_team in event_teams(events):
            canonical, status, suggestions = map_team(provider_team, code, aliases, canonicals)
            item = {
                "providerTeam": provider_team,
                "canonicalTeam": canonical,
                "status": status,
                "suggestions": suggestions,
            }
            (mapped if canonical else unmatched).append(item)
        team_rows.append({
            "leagueCode": code,
            "providerLeagueSlug": chosen.get("slug"),
            "eventsFetched": len(events),
            "providerTeamCount": len(mapped) + len(unmatched),
            "mappedTeamCount": len(mapped),
            "unmatchedTeamCount": len(unmatched),
            "mapped": mapped,
            "unmatched": unmatched,
        })

    save(REPORT, {
        "mode": "domestic-league-team-mapping-audit",
        "productionOddsTouched": False,
        "bettingEngineTouched": False,
        "selectedLeagueCount": len(selected),
        "canonicalLeagueCount": len(canonicals),
        "leagueMappings": league_rows,
        "teamMappings": team_rows,
        "rateLimitRemaining": debug.get("rateLimitRemaining"),
        "warnings": debug.get("warnings", []),
        "apiCallCount": len(debug.get("apiCalls", [])),
    })
    print(json.dumps({
        "selectedLeagueCount": len(selected),
        "teamAuditedLeagueCount": len(team_rows),
        "rateLimitRemaining": debug.get("rateLimitRemaining"),
        "apiCallCount": len(debug.get("apiCalls", [])),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
