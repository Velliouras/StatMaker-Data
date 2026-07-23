#!/usr/bin/env python3
"""Build compact verified UEFA participant support history for the Android app.

The Android UEFA PB/BB path must not call API-Football directly. This script derives
support history only from repository-published Domestic enriched artifacts and writes
one compact JSON file consumed by UefaSupportHistoryRepository.

No synthetic rows are created. A participant is exported only when at least
``--min-matches`` completed verified matches are present in repository sources.
"""
from __future__ import annotations

import argparse
import json
import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = ROOT / "data" / "statmaker" / "domestic_enriched" / "index.json"
ALIASES_PATH = ROOT / "mappings" / "domestic_team_aliases.json"
DEFAULT_OUTPUT = ROOT / "data" / "statmaker" / "uefa_support_history.json"

DEFAULT_MIN_MATCHES = 10
DEFAULT_MAX_MATCHES_PER_TEAM = 12

CLUB_TOKENS = {
    "fc", "fk", "cf", "sc", "ac", "afc", "bk", "if", "sk", "nk", "pfc",
    "gnk", "hsk", "rks", "kks", "msk",
}
LOCATION_SUFFIXES = {
    "athens", "istanbul", "amsterdam", "dublin", "belgrade", "thessaloniki",
}


def load_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def normalize_team_key(value: Any) -> str:
    text = unicodedata.normalize("NFD", str(value or ""))
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    tokens = [token for token in text.split() if token and token not in CLUB_TOKENS]
    if len(tokens) > 1 and tokens[-1] in LOCATION_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def normalized_code(value: Any) -> str:
    code = str(value or "").strip().upper()
    return "ROU" if code == "ROM" else code


def aliases_by_league() -> Dict[str, Dict[str, List[str]]]:
    root = load_json(ALIASES_PATH, {})
    raw = root.get("aliases", {}) if isinstance(root, dict) else {}
    result: Dict[str, Dict[str, List[str]]] = {}
    for code, mapping in raw.items():
        if not isinstance(mapping, dict):
            continue
        result[normalized_code(code)] = {
            str(canonical).strip(): list(dict.fromkeys([
                str(canonical).strip(),
                *[str(alias).strip() for alias in aliases if str(alias).strip()],
            ]))
            for canonical, aliases in mapping.items()
            if str(canonical).strip() and isinstance(aliases, list)
        }
    return result


def canonicalizer(mapping: Mapping[str, Sequence[str]]):
    owners: MutableMapping[str, set[str]] = defaultdict(set)
    for canonical, aliases in mapping.items():
        for value in [canonical, *aliases]:
            key = normalize_team_key(value)
            if key:
                owners[key].add(canonical)
    safe = {key: next(iter(values)) for key, values in owners.items() if len(values) == 1}

    def resolve(value: Any) -> str:
        name = str(value or "").strip()
        return safe.get(normalize_team_key(name), name)

    return resolve


def first_non_null(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def integer_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def date_value(item: Mapping[str, Any]) -> str:
    raw = str(first_non_null(item, "date", "date_utc", "dateUtc", "fixture_date", "fixtureDate") or "").strip()
    return raw[:10] if len(raw) >= 10 else raw


def normalized_stats(item: Mapping[str, Any]) -> Mapping[str, Any]:
    value = item.get("normalized_stats")
    if isinstance(value, dict):
        return value
    value = item.get("stats")
    return value if isinstance(value, dict) else {}


def convert_match(
    item: Mapping[str, Any],
    *,
    league: str,
    season: str,
    resolve_team,
) -> Dict[str, Any] | None:
    home = resolve_team(first_non_null(item, "home_team", "homeTeam", "HomeTeam"))
    away = resolve_team(first_non_null(item, "away_team", "awayTeam", "AwayTeam"))
    date = date_value(item)
    home_goals = integer_or_none(first_non_null(item, "home_goals", "homeGoals", "FTHG", "fthg", "home_score"))
    away_goals = integer_or_none(first_non_null(item, "away_goals", "awayGoals", "FTAG", "ftag", "away_score"))
    if not date or not home or not away or home_goals is None or away_goals is None:
        return None

    stats = normalized_stats(item)
    result: Dict[str, Any] = {
        "competition": league or "Domestic history",
        "season": season,
        "stage": str(first_non_null(item, "round", "stage") or "Domestic history"),
        "date": date,
        "homeTeam": home,
        "awayTeam": away,
        "homeGoals": home_goals,
        "awayGoals": away_goals,
        "sourceLabel": "StatMaker verified API-Football domestic history",
    }

    stat_fields = {
        "homeShots": "HS",
        "awayShots": "AS",
        "homeShotsOnTarget": "HST",
        "awayShotsOnTarget": "AST",
        "homeCorners": "HC",
        "awayCorners": "AC",
        "homeYellowCards": "HY",
        "awayYellowCards": "AY",
        "homeRedCards": "HR",
        "awayRedCards": "AR",
        "homePossessionPct": "HPossession",
        "awayPossessionPct": "APossession",
    }
    for output_key, source_key in stat_fields.items():
        value = integer_or_none(stats.get(source_key))
        if value is not None:
            result[output_key] = value

    venue = first_non_null(item, "venue", "venue_name", "venueName")
    if venue:
        result["venue"] = str(venue).strip()
    return result


def match_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        item.get("date"),
        normalize_team_key(item.get("homeTeam")),
        normalize_team_key(item.get("awayTeam")),
        item.get("homeGoals"),
        item.get("awayGoals"),
    )


def iter_index_rows(index: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    rows = index.get("leagues", []) if isinstance(index, dict) else []
    for row in rows:
        if isinstance(row, dict):
            yield row


def build_support_history(
    *,
    min_matches: int = DEFAULT_MIN_MATCHES,
    max_matches_per_team: int = DEFAULT_MAX_MATCHES_PER_TEAM,
) -> Dict[str, Any]:
    index = load_json(INDEX_PATH, {})
    aliases = aliases_by_league()
    participant_rows: Dict[str, Dict[str, Any]] = {}
    ambiguous_keys: set[str] = set()

    index_rows = list(iter_index_rows(index))
    rows_by_path = {
        str(row.get("output_path") or "").strip(): row
        for row in index_rows
        if str(row.get("output_path") or "").strip()
    }
    enriched_dir = ROOT / "data" / "statmaker" / "domestic_enriched"
    artifact_paths = {ROOT / path for path in rows_by_path}
    if enriched_dir.is_dir():
        artifact_paths.update(path for path in enriched_dir.rglob("*.json") if path.name != "index.json")

    for artifact_path in sorted(artifact_paths):
        try:
            relative_path = str(artifact_path.relative_to(ROOT)).replace("\\", "/")
        except ValueError:
            relative_path = ""
        row = rows_by_path.get(relative_path, {})
        artifact = load_json(artifact_path, {})
        if not isinstance(artifact, dict):
            continue

        competition = artifact.get("competition", {}) if isinstance(artifact.get("competition"), dict) else {}
        league_code = normalized_code(
            competition.get("league_code") or row.get("league_code") or row.get("leagueCode")
        )
        country = str(competition.get("country") or row.get("country") or "").strip()
        league = str(competition.get("league") or row.get("league") or league_code).strip()
        season = str(
            competition.get("season")
            or competition.get("app_season")
            or row.get("app_season")
            or ""
        ).strip()
        alias_mapping = aliases.get(league_code, {})
        resolve_team = canonicalizer(alias_mapping)

        converted: List[Dict[str, Any]] = []
        seen_matches: set[tuple[Any, ...]] = set()
        for raw_match in artifact.get("matches", []) or []:
            if not isinstance(raw_match, dict):
                continue
            match = convert_match(raw_match, league=league, season=season, resolve_team=resolve_team)
            if match is None:
                continue
            key = match_key(match)
            if key in seen_matches:
                continue
            seen_matches.add(key)
            converted.append(match)

        matches_by_team: MutableMapping[str, List[Dict[str, Any]]] = defaultdict(list)
        display_name_by_key: Dict[str, str] = {}
        for match in converted:
            for team in (str(match["homeTeam"]), str(match["awayTeam"])):
                key = normalize_team_key(team)
                if not key:
                    continue
                display_name_by_key.setdefault(key, team)
                matches_by_team[key].append(match)

        aliases_by_canonical_key = {
            normalize_team_key(canonical): list(dict.fromkeys([canonical, *values]))
            for canonical, values in alias_mapping.items()
        }

        for team_key, matches in matches_by_team.items():
            unique_matches = {match_key(match): match for match in matches}
            ordered = sorted(
                unique_matches.values(),
                key=lambda item: str(item.get("date") or ""),
                reverse=True,
            )
            if len(ordered) < min_matches:
                continue
            canonical = display_name_by_key[team_key]
            participant_aliases = aliases_by_canonical_key.get(team_key, [canonical])
            existing = participant_rows.get(team_key)
            candidate = {
                "providerTeam": canonical,
                "canonicalTeam": canonical,
                "aliases": list(dict.fromkeys(participant_aliases)),
                "country": country,
                "leagueCode": league_code,
                "matches": ordered[:max_matches_per_team],
            }
            if existing is None:
                participant_rows[team_key] = candidate
                continue

            same_owner = (
                normalize_team_key(existing.get("canonicalTeam")) == normalize_team_key(canonical)
                and str(existing.get("country") or "").strip().casefold() == country.casefold()
            )
            if not same_owner:
                ambiguous_keys.add(team_key)
                continue

            merged_matches = {
                match_key(match): match
                for match in [*(existing.get("matches", []) or []), *candidate["matches"]]
            }
            existing["matches"] = sorted(
                merged_matches.values(),
                key=lambda item: str(item.get("date") or ""),
                reverse=True,
            )[:max_matches_per_team]
            existing["aliases"] = list(dict.fromkeys([
                *(existing.get("aliases", []) or []),
                *candidate["aliases"],
            ]))

    for ambiguous_key in ambiguous_keys:
        participant_rows.pop(ambiguous_key, None)

    participants = sorted(
        participant_rows.values(),
        key=lambda item: (str(item.get("country") or ""), str(item.get("canonicalTeam") or "")),
    )
    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": "StatMaker verified repository Domestic enriched history",
        "minimumMatchesPerParticipant": min_matches,
        "maximumMatchesPerParticipant": max_matches_per_team,
        "participantCount": len(participants),
        "participants": participants,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--min-matches", type=int, default=DEFAULT_MIN_MATCHES)
    parser.add_argument("--max-matches-per-team", type=int, default=DEFAULT_MAX_MATCHES_PER_TEAM)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    min_matches = max(1, int(args.min_matches))
    max_matches = max(min_matches, int(args.max_matches_per_team))
    payload = build_support_history(
        min_matches=min_matches,
        max_matches_per_team=max_matches,
    )
    if not payload["participants"]:
        raise SystemExit("No verified UEFA support-history participants were produced")
    write_json(args.output, payload)
    print(json.dumps({
        "output": str(args.output),
        "participants": payload["participantCount"],
        "minMatches": min_matches,
        "maxMatchesPerTeam": max_matches,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
