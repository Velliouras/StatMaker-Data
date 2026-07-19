#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, Tuple

ROOT = Path(__file__).resolve().parents[1]
CACHE_ROOT = ROOT / "data" / "api_football" / "fixture_stats"
ODDS_PATH = ROOT / "odds" / "odds_api_io" / "domestic_odds.json"
REPORT_PATH = ROOT / "reports" / "domestic_team_logo_enrichment.json"


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


def main() -> int:
    feed = load(ODDS_PATH, {})
    logos = collect_logos()
    matches_seen = 0
    home_added = 0
    away_added = 0
    fully_covered = 0

    for league in feed.get("leagues", []) or []:
        league_id = str(league.get("apiFootballLeagueId") or league.get("api_football_league_id") or "").strip()
        for match in league.get("matches", []) or []:
            matches_seen += 1
            home_key = norm(match.get("homeTeam"))
            away_key = norm(match.get("awayTeam"))
            home_logo = logos.get((league_id, home_key)) if league_id and home_key else None
            away_logo = logos.get((league_id, away_key)) if league_id and away_key else None

            if home_logo and match.get("homeTeamLogo") != home_logo:
                match["homeTeamLogo"] = home_logo
                home_added += 1
            if away_logo and match.get("awayTeamLogo") != away_logo:
                match["awayTeamLogo"] = away_logo
                away_added += 1
            if match.get("homeTeamLogo") and match.get("awayTeamLogo"):
                fully_covered += 1

    save(ODDS_PATH, feed)
    save(REPORT_PATH, {
        "mode": "domestic-odds-team-logo-enrichment",
        "bettingEngineTouched": False,
        "oddsSemanticsTouched": False,
        "source": "API-Football cached raw_statistics team metadata",
        "matchesSeen": matches_seen,
        "homeLogosAddedOrUpdated": home_added,
        "awayLogosAddedOrUpdated": away_added,
        "matchesWithBothLogos": fully_covered,
        "logoIndexEntries": len(logos),
    })
    print(json.dumps({
        "matchesSeen": matches_seen,
        "homeLogosAddedOrUpdated": home_added,
        "awayLogosAddedOrUpdated": away_added,
        "matchesWithBothLogos": fully_covered,
        "logoIndexEntries": len(logos),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
