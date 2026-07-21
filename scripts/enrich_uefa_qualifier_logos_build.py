#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
API_DATA_ROOT = ROOT / "data" / "api_football"
CONFIG_PATH = ROOT / "config" / "uefa_club_competitions.json"
ODDS_PATHS = [
    ROOT / "odds" / "odds_api_io" / "champions_league_odds.json",
    ROOT / "odds" / "odds_api_io" / "europa_league_odds.json",
    ROOT / "odds" / "odds_api_io" / "conference_league_odds.json",
]
REPORT = ROOT / "reports" / "uefa_qualifier_logo_enrichment.json"
COMMON = {"fc", "fk", "cf", "sc", "ac", "afc", "bk", "if", "sk", "nk"}

# Verified fallback for a provider spelling that currently has no cached API-Football logo metadata.
# Wikimedia serves a PNG here; the Android loader already supports normal HTTPS image URLs.
MANUAL_LOGOS = {
    "panathinaikos": "https://upload.wikimedia.org/wikipedia/commons/thumb/c/cf/Panathinaikos_FC_logo.png/250px-Panathinaikos_FC_logo.png",
    "panathinaikos athens": "https://upload.wikimedia.org/wikipedia/commons/thumb/c/cf/Panathinaikos_FC_logo.png/250px-Panathinaikos_FC_logo.png",
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
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text.lower())).strip()


def simple(value: Any) -> str:
    tokens = [t for t in norm(value).split() if t not in COMMON]
    while tokens and tokens[-1].isdigit() and len(tokens[-1]) == 4:
        tokens.pop()
    return " ".join(tokens)


def add_logo_candidate(
    name: Any,
    logo: Any,
    exact_candidates: dict[str, set[str]],
    simple_candidates: dict[str, set[str]],
) -> None:
    clean_name = str(name or "").strip()
    clean_logo = str(logo or "").strip()
    if not clean_name or not clean_logo.startswith("https://"):
        return
    exact_candidates.setdefault(norm(clean_name), set()).add(clean_logo)
    simple_candidates.setdefault(simple(clean_name), set()).add(clean_logo)


def collect_team_dicts(
    node: Any,
    exact_candidates: dict[str, set[str]],
    simple_candidates: dict[str, set[str]],
    parent_key: str = "",
) -> None:
    if isinstance(node, dict):
        # API-Football consistently wraps club identity as a `team` object.
        if parent_key == "team":
            add_logo_candidate(node.get("name"), node.get("logo"), exact_candidates, simple_candidates)
        for key, value in node.items():
            collect_team_dicts(value, exact_candidates, simple_candidates, str(key))
    elif isinstance(node, list):
        for value in node:
            collect_team_dicts(value, exact_candidates, simple_candidates, parent_key)


def collect() -> tuple[dict[str, str], dict[str, str]]:
    exact_candidates: dict[str, set[str]] = {}
    simple_candidates: dict[str, set[str]] = {}

    # Scan all cached API-Football JSON, not only raw_statistics. Many fixtures have no
    # statistics block even though another cached response contains the team identity/logo.
    for path in API_DATA_ROOT.rglob("*.json"):
        payload = load(path, None)
        if payload is None:
            continue
        collect_team_dicts(payload, exact_candidates, simple_candidates)

    for name, logo in MANUAL_LOGOS.items():
        add_logo_candidate(name, logo, exact_candidates, simple_candidates)

    exact = {k: next(iter(v)) for k, v in exact_candidates.items() if k and len(v) == 1}
    simplified = {k: next(iter(v)) for k, v in simple_candidates.items() if k and len(v) == 1}
    return exact, simplified


def lookup(names: list[Any], exact: dict[str, str], simplified: dict[str, str]) -> str | None:
    for name in names:
        key = norm(name)
        if key in exact:
            return exact[key]
    for name in names:
        key = simple(name)
        if key in simplified:
            return simplified[key]
    return None


def competition_alias_maps() -> dict[str, dict[str, str]]:
    payload = load(CONFIG_PATH, {})
    result: dict[str, dict[str, str]] = {}
    for competition in payload.get("competitions", []) or []:
        code = str(competition.get("leagueCode") or "").strip().upper()
        if not code:
            continue
        mapping: dict[str, str] = {}
        for canonical in competition.get("canonicalTeams", []) or []:
            name = str(canonical or "").strip()
            if name:
                mapping[norm(name)] = name
        for alias, canonical in (competition.get("aliases") or {}).items():
            alias_name = str(alias or "").strip()
            canonical_name = str(canonical or "").strip()
            if alias_name and canonical_name:
                mapping[norm(alias_name)] = canonical_name
        result[code] = mapping
    return result


def canonical_for(value: Any, mapping: dict[str, str]) -> str | None:
    key = norm(value)
    if not key:
        return None
    return mapping.get(key)


def main() -> int:
    exact, simplified = collect()
    aliases_by_code = competition_alias_maps()
    seen = added_home = added_away = both = aliases_applied = fully_mapped = 0
    missing: set[str] = set()

    for path in ODDS_PATHS:
        feed = load(path, {})
        league_code = str(feed.get("leagueCode") or "").strip().upper()
        mapping = aliases_by_code.get(league_code, {})

        for match in feed.get("matches", []) or []:
            seen += 1

            provider_home = str(match.get("providerHomeTeam") or match.get("homeTeam") or "").strip()
            provider_away = str(match.get("providerAwayTeam") or match.get("awayTeam") or "").strip()
            mapped_home = canonical_for(provider_home, mapping)
            mapped_away = canonical_for(provider_away, mapping)

            if mapped_home:
                if match.get("homeTeam") != mapped_home or match.get("canonicalHomeTeam") != mapped_home:
                    aliases_applied += 1
                match["homeTeam"] = mapped_home
                match["canonicalHomeTeam"] = mapped_home
            if mapped_away:
                if match.get("awayTeam") != mapped_away or match.get("canonicalAwayTeam") != mapped_away:
                    aliases_applied += 1
                match["awayTeam"] = mapped_away
                match["canonicalAwayTeam"] = mapped_away

            if mapped_home and mapped_away:
                match["teamMappingStatus"] = "matched"
                match["usableForStats"] = True
                fully_mapped += 1

            home = lookup(
                [match.get("homeTeam"), match.get("canonicalHomeTeam"), match.get("providerHomeTeam")],
                exact,
                simplified,
            )
            away = lookup(
                [match.get("awayTeam"), match.get("canonicalAwayTeam"), match.get("providerAwayTeam")],
                exact,
                simplified,
            )
            if home:
                if match.get("homeTeamLogo") != home:
                    match["homeTeamLogo"] = home
                    added_home += 1
            else:
                missing.add(str(match.get("homeTeam") or ""))
            if away:
                if match.get("awayTeamLogo") != away:
                    match["awayTeamLogo"] = away
                    added_away += 1
            else:
                missing.add(str(match.get("awayTeam") or ""))
            if match.get("homeTeamLogo") and match.get("awayTeamLogo"):
                both += 1
        save(path, feed)

    report = {
        "mode": "build-only UEFA qualifier alias and logo enrichment",
        "source": "all cached API-Football team objects plus verified manual fallbacks",
        "matchesSeen": seen,
        "aliasesApplied": aliases_applied,
        "fullyCanonicalMappedMatches": fully_mapped,
        "homeLogosAddedOrUpdated": added_home,
        "awayLogosAddedOrUpdated": added_away,
        "matchesWithBothLogos": both,
        "missingTeams": sorted(t for t in missing if t),
    }
    save(REPORT, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
