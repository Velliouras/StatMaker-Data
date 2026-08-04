#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
CACHE_ROOT = ROOT / "data" / "api_football" / "fixture_stats"
ODDS_PATH = ROOT / "odds" / "odds_api_io" / "domestic_odds.json"
ALIASES_PATH = ROOT / "mappings" / "domestic_team_aliases.json"
REGISTRY_PATH = ROOT / "data" / "statmaker" / "domestic_live_july_registry.json"
REPORT_PATH = ROOT / "reports" / "domestic_team_logo_enrichment.json"

API_FOOTBALL_BASE = "https://v3.football.api-sports.io"
API_FOOTBALL_TIMEOUT_SECONDS = 25
MAX_API_TEAM_LOOKUPS = 24
COMMON_CLUB_TOKENS = {"fc", "fk", "cf", "sc", "ac", "afc", "bk", "if"}
API_SPORTS_TEAM_LOGO = "https://media.api-sports.io/football/teams/{team_id}.png"

# Verified provider-name families whose current Odds-API.io labels differ from
# historical API-Football names stored in StatMaker caches.
VERIFIED_LOGO_ALIASES: Dict[str, Dict[str, tuple[str, ...]]] = {
    "SWE": {
        "Hammarby": ("Hammarby IF", "Hammarby FF"),
    },
    "CHN": {
        "Chongqing Tonglianglong FC": (
            "Chongqing Tonglianglong",
            "Chongqing Tongliang Long",
        ),
        "Dalian Yingbo FC": ("Dalian Yingbo", "Dalian Zhixing"),
        "Qingdao Hainiu": ("Qingdao Hainiu FC", "Qingdao Jonoon"),
        "Qingdao West Coast FC": ("Qingdao West Coast", "Qingdao Youth Island"),
        "Chengdu Rongcheng": ("Chengdu Rongcheng FC", "Chengdu Better City"),
        "Liaoning Tieren": ("Liaoning Tieren FC", "Shenyang Urban"),
        "Shenzhen Peng City": ("Shenzhen Peng City FC", "Sichuan Jiuniu"),
        "Zhejiang Prof.": (
            "Zhejiang Professional",
            "Zhejiang Pro",
            "Zhejiang FC",
            "Hangzhou Greentown",
        ),
    },
}


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


def valid_team_id(value: Any) -> str | None:
    try:
        team_id = int(value)
    except (TypeError, ValueError):
        return None
    return str(team_id) if team_id > 0 else None


def add_logo(
    logos: Dict[Tuple[str, str], str],
    league_id: str,
    name: Any,
    logo: Any,
) -> bool:
    name_key = norm(name)
    logo_url = str(logo or "").strip()
    if not league_id or not name_key or not logo_url.startswith("https://"):
        return False
    before = len(logos)
    logos.setdefault((league_id, name_key), logo_url)
    return len(logos) > before


def collect_logos() -> tuple[Dict[Tuple[str, str], str], Dict[str, int]]:
    logos: Dict[Tuple[str, str], str] = {}
    source_counts = {
        "fixtureTeamIdEntries": 0,
        "rawStatisticsEntries": 0,
    }

    for path in CACHE_ROOT.rglob("fixture_stats.json"):
        payload = load(path, {})
        league_id = str(payload.get("league_id") or "").strip()
        if not league_id:
            continue

        for fixture in payload.get("fixtures", []) or []:
            if not isinstance(fixture, dict):
                continue

            # Newer caches may expose fixture team IDs even when raw statistics
            # are unavailable. Retain this deterministic media URL fallback.
            for name_key, id_key in (
                ("home_team", "home_team_id"),
                ("away_team", "away_team_id"),
            ):
                team_id = valid_team_id(fixture.get(id_key))
                if team_id and add_logo(
                    logos,
                    league_id,
                    fixture.get(name_key),
                    API_SPORTS_TEAM_LOGO.format(team_id=team_id),
                ):
                    source_counts["fixtureTeamIdEntries"] += 1

            for block in fixture.get("raw_statistics", []) or []:
                if not isinstance(block, dict):
                    continue
                team = block.get("team") or {}
                if not isinstance(team, dict):
                    continue
                if add_logo(logos, league_id, team.get("name"), team.get("logo")):
                    source_counts["rawStatisticsEntries"] += 1

    return logos, source_counts


def build_unique_index(
    logos: Dict[Tuple[str, str], str],
    *,
    simplified_names: bool,
) -> Dict[str, str]:
    candidates: Dict[str, set[str]] = {}
    for (_, name_key), logo in logos.items():
        key = simplified(name_key) if simplified_names else norm(name_key)
        # Cross-league fallback is only safe for sufficiently specific names.
        # Single-token names such as Rangers/United can refer to unrelated clubs.
        if key and len(key.split()) >= 2:
            candidates.setdefault(key, set()).add(logo)
    return {
        key: next(iter(values))
        for key, values in candidates.items()
        if len(values) == 1
    }


def build_unique_league_simplified_index(
    logos: Dict[Tuple[str, str], str],
) -> Dict[Tuple[str, str], str]:
    candidates: Dict[Tuple[str, str], set[str]] = {}
    for (league_id, name_key), logo in logos.items():
        key = simplified(name_key)
        if key:
            candidates.setdefault((league_id, key), set()).add(logo)
    return {
        key: next(iter(values))
        for key, values in candidates.items()
        if len(values) == 1
    }


def alias_candidates(
    league_code: str,
    names: Iterable[Any],
    aliases_payload: Dict[str, Any],
) -> list[str]:
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

    families: list[tuple[str, list[str]]] = []
    league_aliases = ((aliases_payload.get("aliases") or {}).get(league_code) or {})
    for canonical, variants in league_aliases.items():
        families.append((str(canonical), [str(value) for value in variants or []]))
    for canonical, variants in VERIFIED_LOGO_ALIASES.get(league_code, {}).items():
        families.append((canonical, list(variants)))

    input_keys = {norm(name) for name in out if norm(name)}
    for canonical, variants in families:
        family = [canonical] + variants
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
    league_simplified_index: Dict[Tuple[str, str], str],
    global_exact_index: Dict[str, str],
    global_simplified_index: Dict[str, str],
    aliases_payload: Dict[str, Any],
) -> tuple[str | None, str]:
    candidates = alias_candidates(league_code, names, aliases_payload)

    for name in candidates:
        key = norm(name)
        logo = logos.get((league_id, key)) if league_id and key else None
        if logo:
            return logo, "league_exact_or_verified_alias"

    for name in candidates:
        key = simplified(name)
        logo = league_simplified_index.get((league_id, key)) if league_id and key else None
        if logo:
            return logo, "league_unique_simplified"

    # Team logos are global, not competition-specific. A globally unique name
    # safely covers promoted/relegated teams whose logo exists in another cache.
    for name in candidates:
        logo = global_exact_index.get(norm(name))
        if logo:
            return logo, "global_unique_exact_or_alias"

    for name in candidates:
        logo = global_simplified_index.get(simplified(name))
        if logo:
            return logo, "global_unique_simplified"

    return None, "missing"


def registry_by_code() -> Dict[str, Dict[str, Any]]:
    payload = load(REGISTRY_PATH, {})
    return {
        str(item.get("leagueCode") or "").strip().upper(): item
        for item in payload.get("leagues", []) or []
        if isinstance(item, dict) and str(item.get("leagueCode") or "").strip()
    }


def parse_season(value: Any) -> int | None:
    match = re.search(r"\b(20\d{2})\b", str(value or ""))
    return int(match.group(1)) if match else None


def resolve_api_season(league: Dict[str, Any], registry_entry: Dict[str, Any]) -> int | None:
    # Current/target season must take precedence for promoted teams and newly
    # started competitions. Historical season is only a fallback.
    for key in (
        "targetApiSeason",
        "target_api_season",
        "apiFootballSeason",
        "api_football_season",
        "historyApiSeason",
        "history_api_season",
        "season",
        "appSeason",
        "app_season",
    ):
        for source in (league, registry_entry):
            season = parse_season(source.get(key))
            if season is not None:
                return season
    return None


def fetch_api_team_catalog(
    api_key: str,
    league_id: str,
    season: int,
) -> list[Dict[str, Any]]:
    query = urlencode({"league": league_id, "season": season})
    request = Request(
        f"{API_FOOTBALL_BASE}/teams?{query}",
        headers={
            "x-apisports-key": api_key,
            "Accept": "application/json",
            "User-Agent": "StatMaker-Data domestic logo enrichment",
        },
        method="GET",
    )
    with urlopen(request, timeout=API_FOOTBALL_TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read().decode("utf-8"))
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if errors:
        raise RuntimeError(f"API-Football errors: {errors}")
    items = payload.get("response") if isinstance(payload, dict) else []
    return [item for item in items or [] if isinstance(item, dict)]


def unresolved_leagues(
    feed: Dict[str, Any],
    logos: Dict[Tuple[str, str], str],
    aliases_payload: Dict[str, Any],
) -> list[Dict[str, Any]]:
    league_simplified = build_unique_league_simplified_index(logos)
    global_exact = build_unique_index(logos, simplified_names=False)
    global_simplified = build_unique_index(logos, simplified_names=True)
    unresolved: list[Dict[str, Any]] = []

    for league in feed.get("leagues", []) or []:
        league_code = str(league.get("leagueCode") or "").strip().upper()
        league_id = str(
            league.get("apiFootballLeagueId")
            or league.get("api_football_league_id")
            or ""
        ).strip()
        needs_catalog = False
        for match in league.get("matches", []) or []:
            for side in ("home", "away"):
                logo_key = f"{side}TeamLogo"
                if str(match.get(logo_key) or "").startswith("https://"):
                    continue
                names = [
                    match.get(f"{side}Team"),
                    match.get(f"canonical{side.title()}Team"),
                    match.get(f"provider{side.title()}Team"),
                ]
                logo, _ = lookup_logo(
                    league_id,
                    league_code,
                    names,
                    logos,
                    league_simplified,
                    global_exact,
                    global_simplified,
                    aliases_payload,
                )
                if not logo:
                    needs_catalog = True
                    break
            if needs_catalog:
                break
        if needs_catalog and league_id:
            unresolved.append(league)
    return unresolved


def enrich_from_api_catalogs(
    feed: Dict[str, Any],
    logos: Dict[Tuple[str, str], str],
    aliases_payload: Dict[str, Any],
) -> Dict[str, Any]:
    api_key = os.environ.get("API_FOOTBALL_KEY", "").strip()
    report: Dict[str, Any] = {
        "enabled": bool(api_key),
        "lookupsAttempted": 0,
        "lookupsSucceeded": 0,
        "teamsAdded": 0,
        "errors": [],
    }
    if not api_key:
        return report

    registry = registry_by_code()
    candidates = unresolved_leagues(feed, logos, aliases_payload)
    for league in candidates[:MAX_API_TEAM_LOOKUPS]:
        league_code = str(league.get("leagueCode") or "").strip().upper()
        league_id = str(
            league.get("apiFootballLeagueId")
            or league.get("api_football_league_id")
            or ""
        ).strip()
        season = resolve_api_season(league, registry.get(league_code, {}))
        if not league_id or season is None:
            report["errors"].append({
                "leagueCode": league_code,
                "reason": "missing_league_id_or_season",
            })
            continue

        report["lookupsAttempted"] += 1
        try:
            items = fetch_api_team_catalog(api_key, league_id, season)
        except Exception as exc:  # Keep logo enrichment non-fatal for odds.
            report["errors"].append({
                "leagueCode": league_code,
                "leagueId": league_id,
                "season": season,
                "reason": str(exc)[:300],
            })
            continue

        report["lookupsSucceeded"] += 1
        for item in items:
            team = item.get("team") or {}
            if not isinstance(team, dict):
                continue
            team_id = valid_team_id(team.get("id"))
            logo = team.get("logo") or (
                API_SPORTS_TEAM_LOGO.format(team_id=team_id) if team_id else None
            )
            if add_logo(logos, league_id, team.get("name"), logo):
                report["teamsAdded"] += 1

    report["lookupLimit"] = MAX_API_TEAM_LOOKUPS
    report["unresolvedLeagueCandidates"] = len(candidates)
    return report


def main() -> int:
    feed = load(ODDS_PATH, {})
    aliases_payload = load(ALIASES_PATH, {})
    logos, source_counts = collect_logos()
    api_report = enrich_from_api_catalogs(feed, logos, aliases_payload)

    league_simplified_index = build_unique_league_simplified_index(logos)
    global_exact_index = build_unique_index(logos, simplified_names=False)
    global_simplified_index = build_unique_index(logos, simplified_names=True)

    matches_seen = 0
    home_added = 0
    away_added = 0
    fully_covered = 0
    hit_counts: Dict[str, int] = {}
    missing_home: list[str] = []
    missing_away: list[str] = []

    for league in feed.get("leagues", []) or []:
        league_code = str(league.get("leagueCode") or "").strip().upper()
        league_id = str(
            league.get("apiFootballLeagueId")
            or league.get("api_football_league_id")
            or ""
        ).strip()
        for match in league.get("matches", []) or []:
            matches_seen += 1
            home_logo, home_mode = lookup_logo(
                league_id,
                league_code,
                [match.get("homeTeam"), match.get("canonicalHomeTeam"), match.get("providerHomeTeam")],
                logos,
                league_simplified_index,
                global_exact_index,
                global_simplified_index,
                aliases_payload,
            )
            away_logo, away_mode = lookup_logo(
                league_id,
                league_code,
                [match.get("awayTeam"), match.get("canonicalAwayTeam"), match.get("providerAwayTeam")],
                logos,
                league_simplified_index,
                global_exact_index,
                global_simplified_index,
                aliases_payload,
            )

            if home_logo and match.get("homeTeamLogo") != home_logo:
                match["homeTeamLogo"] = home_logo
                home_added += 1
            if away_logo and match.get("awayTeamLogo") != away_logo:
                match["awayTeamLogo"] = away_logo
                away_added += 1

            for mode in (home_mode, away_mode):
                hit_counts[mode] = hit_counts.get(mode, 0) + 1

            if match.get("homeTeamLogo") and match.get("awayTeamLogo"):
                fully_covered += 1
            else:
                if not match.get("homeTeamLogo"):
                    missing_home.append(f"{league_code}:{match.get('homeTeam')}")
                if not match.get("awayTeamLogo"):
                    missing_away.append(f"{league_code}:{match.get('awayTeam')}")

    save(ODDS_PATH, feed)
    report = {
        "mode": "domestic-odds-team-logo-enrichment",
        "bettingEngineTouched": False,
        "oddsSemanticsTouched": False,
        "source": "API-Football cached metadata, cross-league unique mappings, and current team catalog fallback",
        "mappingPolicy": "league exact/verified aliases, league unique simplified, global unique exact, global unique simplified",
        "matchesSeen": matches_seen,
        "homeLogosAddedOrUpdated": home_added,
        "awayLogosAddedOrUpdated": away_added,
        "matchesWithBothLogos": fully_covered,
        "logoIndexEntries": len(logos),
        "fixtureTeamIdEntries": source_counts["fixtureTeamIdEntries"],
        "rawStatisticsEntries": source_counts["rawStatisticsEntries"],
        "lookupHitCounts": hit_counts,
        "apiFootballCatalogFallback": api_report,
        "missingHomeTeams": sorted(set(missing_home)),
        "missingAwayTeams": sorted(set(missing_away)),
    }
    save(REPORT_PATH, report)
    print(json.dumps({
        "matchesSeen": matches_seen,
        "homeLogosAddedOrUpdated": home_added,
        "awayLogosAddedOrUpdated": away_added,
        "matchesWithBothLogos": fully_covered,
        "logoIndexEntries": len(logos),
        "fixtureTeamIdEntries": source_counts["fixtureTeamIdEntries"],
        "rawStatisticsEntries": source_counts["rawStatisticsEntries"],
        "lookupHitCounts": hit_counts,
        "apiFootballCatalogFallback": api_report,
        "missingHomeTeams": len(set(missing_home)),
        "missingAwayTeams": len(set(missing_away)),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
