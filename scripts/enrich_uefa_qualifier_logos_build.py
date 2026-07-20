#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CACHE_ROOT = ROOT / "data" / "api_football" / "fixture_stats"
ODDS_PATHS = [
    ROOT / "odds" / "odds_api_io" / "champions_league_odds.json",
    ROOT / "odds" / "odds_api_io" / "europa_league_odds.json",
    ROOT / "odds" / "odds_api_io" / "conference_league_odds.json",
]
REPORT = ROOT / "reports" / "uefa_qualifier_logo_enrichment.json"
COMMON = {"fc", "fk", "cf", "sc", "ac", "afc", "bk", "if", "sk", "nk"}


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


def collect() -> tuple[dict[str, str], dict[str, str]]:
    exact_candidates: dict[str, set[str]] = {}
    simple_candidates: dict[str, set[str]] = {}
    for path in CACHE_ROOT.rglob("fixture_stats.json"):
        payload = load(path, {})
        for fixture in payload.get("fixtures", []) or []:
            for block in fixture.get("raw_statistics", []) or []:
                if not isinstance(block, dict):
                    continue
                team = block.get("team") or {}
                if not isinstance(team, dict):
                    continue
                name = str(team.get("name") or "").strip()
                logo = str(team.get("logo") or "").strip()
                if not name or not logo.startswith("https://"):
                    continue
                exact_candidates.setdefault(norm(name), set()).add(logo)
                simple_candidates.setdefault(simple(name), set()).add(logo)
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


def main() -> int:
    exact, simplified = collect()
    seen = added_home = added_away = both = 0
    missing: set[str] = set()
    for path in ODDS_PATHS:
        feed = load(path, {})
        for match in feed.get("matches", []) or []:
            seen += 1
            home = lookup([match.get("homeTeam"), match.get("canonicalHomeTeam"), match.get("providerHomeTeam")], exact, simplified)
            away = lookup([match.get("awayTeam"), match.get("canonicalAwayTeam"), match.get("providerAwayTeam")], exact, simplified)
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
        "mode": "build-only UEFA qualifier logo enrichment",
        "source": "API-Football cached raw_statistics team metadata",
        "matchesSeen": seen,
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
