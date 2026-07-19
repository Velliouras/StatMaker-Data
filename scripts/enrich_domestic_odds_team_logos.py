#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple

ROOT = Path(__file__).resolve().parents[1]
CACHE_ROOT = ROOT / "data" / "api_football" / "fixture_stats"
ODDS_PATH = ROOT / "odds" / "odds_api_io" / "domestic_odds.json"
ALIASES_PATH = ROOT / "mappings" / "domestic_team_aliases.json"
REPORT_PATH = ROOT / "reports" / "domestic_team_logo_enrichment.json"

COMMON_CLUB_TOKENS = {"fc", "fk", "cf", "sc", "ac", "afc", "bk", "if"}


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
    return re.sub(r"\s+", " ", text)


def simplified(value: Any) -> str:
    tokens = [token for token in norm(value).split() if token not in COMMON_CLUB_TOKENS]
    while tokens and tokens[-1].isdigit() and len(tokens[-1]) == 4:
        tokens.pop()
    return " ".join(tokens)


def collect_logos() -> Dict[Tuple[str, str], str]:
    logos: Dict[Tuple[str, str], str] = {}
    for path in CACHE_ROOT.rglob("fixture_stats.json"):
        payload = load(path, {})
        league_id = str(payload.get("league_id") or "").strip()
        if not league_id:
            continue
        for fixture in payload.get("fixtures", []) or []:
            for block in fixture.get("raw_statistics", []) or []:
                if not isinstance(block, dict):
                    continue
                team = block.get("team") or {}
                if not isinstance(team, dict):
                    continue
                name = str(team.get("name") or "").strip()
                logo = str(team.get("logo") or "").strip()
                key = norm(name)
                if key and logo.startswith("https://"):
                    logos.setdefault((league_id, key), logo)
    return logos


def build_unique_simplified_index(logos: Dict[Tuple[str, str], str]) -> Dict[Tuple[str, str], str]:
    candidates: Dict[Tuple[str, str], set[str]] = {}
    for (league_id, name_key), logo in logos.items():
        simple_key = simplified(name_key)
        if not simple_key:
            continue
        candidates.setdefault((league_id, simple_key), set()).add(logo)
    return {
        key: next(iter(values))
        for key, values in candidates.items()
        if len(values) == 1
    }


def alias_candidates(league_code: str, names: Iterable[Any], aliases_payload: Dict[str, Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        text = str(value or "").strip()
        key = norm(text)
        if text and key and key not in seen:
            seen.add(key)
            out.append(text)

    for name in names:
        add(name)

    league_aliases = ((aliases_payload.get("aliases") or {}).get(league_code) or {})
    input_keys = {norm(name) for name in out if norm(name)}
    for canonical, variants in league_aliases.items():
        family = [canonical] + list(variants or [])
        family_keys = {norm(value) for value in family if norm(value)}
        if input_keys.intersection(family_keys):
            for value in family:
                add(value)

    return out


def lookup_logo(
    league_id: str,
    league_code: str,
    names: Iterable[Any],
    logos: Dict[Tuple[str, str], str],
    simplified_index: Dict[Tuple[str, str], str],
    aliases_payload: Dict[str, Any],
) -> tuple[str | None, str]:
    candidates = alias_candidates(league_code, names, aliases_payload)

    for name in candidates:
        key = norm(name)
        logo = logos.get((league_id, key)) if league_id and key else None
        if logo:
            return logo, "exact_or_verified_alias"

    for name in candidates:
        key = simplified(name)
        logo = simplified_index.get((league_id, key)) if league_id and key else None
        if logo:
            return logo, "unique_simplified"

    return None, "missing"


def main() -> int:
    feed = load(ODDS_PATH, {})
    aliases_payload = load(ALIASES_PATH, {})
    logos = collect_logos()
    simplified_index = build_unique_simplified_index(logos)
    matches_seen = 0
    home_added = 0
    away_added = 0
    fully_covered = 0
    exact_or_alias_hits = 0
    simplified_hits = 0
    missing_home: list[str] = []
    missing_away: list[str] = []

    for league in feed.get("leagues", []) or []:
        league_code = str(league.get("leagueCode") or "").strip().upper()
        league_id = str(league.get("apiFootballLeagueId") or league.get("api_football_league_id") or "").strip()
        for match in league.get("matches", []) or []:
            matches_seen += 1
            home_logo, home_mode = lookup_logo(
                league_id,
                league_code,
                [match.get("homeTeam"), match.get("canonicalHomeTeam"), match.get("providerHomeTeam")],
                logos,
                simplified_index,
                aliases_payload,
            )
            away_logo, away_mode = lookup_logo(
                league_id,
                league_code,
                [match.get("awayTeam"), match.get("canonicalAwayTeam"), match.get("providerAwayTeam")],
                logos,
                simplified_index,
                aliases_payload,
            )

            if home_logo and match.get("homeTeamLogo") != home_logo:
                match["homeTeamLogo"] = home_logo
                home_added += 1
            if away_logo and match.get("awayTeamLogo") != away_logo:
                match["awayTeamLogo"] = away_logo
                away_added += 1

            for mode in (home_mode, away_mode):
                if mode == "exact_or_verified_alias":
                    exact_or_alias_hits += 1
                elif mode == "unique_simplified":
                    simplified_hits += 1

            if match.get("homeTeamLogo") and match.get("awayTeamLogo"):
                fully_covered += 1
            else:
                if not match.get("homeTeamLogo"):
                    missing_home.append(f"{league_code}:{match.get('homeTeam')}")
                if not match.get("awayTeamLogo"):
                    missing_away.append(f"{league_code}:{match.get('awayTeam')}")

    save(ODDS_PATH, feed)
    save(REPORT_PATH, {
        "mode": "domestic-odds-team-logo-enrichment",
        "bettingEngineTouched": False,
        "oddsSemanticsTouched": False,
        "source": "API-Football cached raw_statistics team metadata",
        "mappingPolicy": "exact canonical/provider names, verified aliases, then unique simplified match only",
        "matchesSeen": matches_seen,
        "homeLogosAddedOrUpdated": home_added,
        "awayLogosAddedOrUpdated": away_added,
        "matchesWithBothLogos": fully_covered,
        "logoIndexEntries": len(logos),
        "exactOrVerifiedAliasHits": exact_or_alias_hits,
        "uniqueSimplifiedHits": simplified_hits,
        "missingHomeTeams": sorted(set(missing_home)),
        "missingAwayTeams": sorted(set(missing_away)),
    })
    print(json.dumps({
        "matchesSeen": matches_seen,
        "homeLogosAddedOrUpdated": home_added,
        "awayLogosAddedOrUpdated": away_added,
        "matchesWithBothLogos": fully_covered,
        "logoIndexEntries": len(logos),
        "exactOrVerifiedAliasHits": exact_or_alias_hits,
        "uniqueSimplifiedHits": simplified_hits,
        "missingHomeTeams": len(set(missing_home)),
        "missingAwayTeams": len(set(missing_away)),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
