#!/usr/bin/env python3
"""Build a zero-live-call priority audit for the full Domestic API-Football registry.

The audit joins config/api_football_enrichment_leagues.json with the existing
fixture-statistics cache. It never calls API-Football and never touches odds.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "api_football_enrichment_leagues.json"
CACHE_ROOT = ROOT / "data" / "api_football" / "fixture_stats"
JSON_OUT = ROOT / "reports" / "api_football_domestic_priority_audit.json"
CSV_OUT = ROOT / "reports" / "api_football_domestic_priority_audit.csv"
MD_OUT = ROOT / "reports" / "api_football_domestic_priority_audit.md"

COMPLETED = {"FT", "AET", "PEN"}
FIELD_GROUPS = {
    "xg": ("HxG", "AxG"),
    "saves": ("HSaves", "ASaves"),
    "possession": ("HPossession", "APossession"),
    "passes": ("HPasses", "APasses"),
    "accurate_passes": ("HPassesAccurate", "APassesAccurate"),
}


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def slug(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower() or "unknown"


def cache_path_for(league: Dict[str, Any]) -> Path:
    return CACHE_ROOT / slug(league.get("country")) / slug(league.get("display_name")) / str(league.get("season")) / "fixture_stats.json"


def parse_date(value: Any) -> Optional[dt.date]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return dt.date.fromisoformat(text[:10])
        except ValueError:
            return None


def has_value(value: Any) -> bool:
    return value is not None and value != ""


def pct(count: int, denominator: int) -> float:
    return round(count / denominator, 4) if denominator else 0.0


def fixture_status(item: Dict[str, Any]) -> str:
    return str(item.get("status") or "").upper()


def group_has_values(stats: Dict[str, Any], fields: Iterable[str]) -> bool:
    return all(has_value(stats.get(field)) for field in fields)


def classify_timing(first_date: Optional[dt.date], today: dt.date) -> Dict[str, Any]:
    if first_date is None:
        return {"active": False, "starts_late_july_early_august": False, "days_from_today": None}
    days = (first_date - today).days
    late_july_early_august = (first_date.month == 7 and first_date.day >= 20) or (first_date.month == 8 and first_date.day <= 10)
    return {
        "active": first_date <= today,
        "starts_late_july_early_august": late_july_early_august,
        "days_from_today": days,
    }


def priority(row: Dict[str, Any]) -> tuple[int, str]:
    if row["starts_late_july_early_august"] and not row["active"]:
        rank = 1
    elif row["active"] and row["completed_fixtures"] > row["fixtures_with_statistics"]:
        rank = 2
    elif not row["cache_exists"]:
        rank = 3
    elif row["fixtures_with_statistics"] < row["completed_fixtures"]:
        rank = 4
    else:
        rank = 5
    return rank, f"{row['country']}|{row['league']}"


def audit_league(league: Dict[str, Any], today: dt.date) -> Dict[str, Any]:
    path = cache_path_for(league)
    cache = load_json(path, {})
    fixtures = cache.get("fixtures") if isinstance(cache, dict) else []
    fixtures = fixtures if isinstance(fixtures, list) else []

    fixture_dates = [date for date in (parse_date(item.get("date")) for item in fixtures) if date is not None]
    first_date = min(fixture_dates) if fixture_dates else None
    last_date = max(fixture_dates) if fixture_dates else None
    completed = [item for item in fixtures if fixture_status(item) in COMPLETED]
    with_stats = [item for item in completed if isinstance(item.get("normalized_stats"), dict) and "raw_statistics" in item]

    field_counts: Dict[str, int] = {name: 0 for name in FIELD_GROUPS}
    for item in with_stats:
        stats = item.get("normalized_stats") or {}
        for name, fields in FIELD_GROUPS.items():
            if group_has_values(stats, fields):
                field_counts[name] += 1

    timing = classify_timing(first_date, today)
    row = {
        "country": league.get("country"),
        "league": league.get("display_name"),
        "league_code": league.get("leagueCode") or league.get("football_data_code"),
        "api_football_league_id": league.get("api_football_league_id"),
        "season": str(league.get("season")),
        "app_season": league.get("app_season"),
        "priority_group": league.get("priority_group"),
        "enabled": bool(league.get("enabled")),
        "cache_exists": path.exists(),
        "cache_path": str(path.relative_to(ROOT)).replace("\\", "/"),
        "fixtures_in_cache": len(fixtures),
        "completed_fixtures": len(completed),
        "fixtures_with_statistics": len(with_stats),
        "statistics_coverage": pct(len(with_stats), len(completed)),
        "first_fixture_date": first_date.isoformat() if first_date else None,
        "last_fixture_date": last_date.isoformat() if last_date else None,
        **timing,
        "xg_fixtures": field_counts["xg"],
        "xg_coverage": pct(field_counts["xg"], len(with_stats)),
        "saves_fixtures": field_counts["saves"],
        "saves_coverage": pct(field_counts["saves"], len(with_stats)),
        "possession_fixtures": field_counts["possession"],
        "possession_coverage": pct(field_counts["possession"], len(with_stats)),
        "passes_fixtures": field_counts["passes"],
        "passes_coverage": pct(field_counts["passes"], len(with_stats)),
        "accurate_passes_fixtures": field_counts["accurate_passes"],
        "accurate_passes_coverage": pct(field_counts["accurate_passes"], len(with_stats)),
    }
    rank, _ = priority(row)
    row["priority_rank"] = rank
    row["priority_reason"] = {
        1: "starts late July / early August and is not active yet",
        2: "active league with incomplete fixture-statistics cache",
        3: "no cache exists",
        4: "cache exists but completed fixtures are incomplete",
        5: "cache currently complete for known completed fixtures",
    }[rank]
    return row


def write_csv(rows: List[Dict[str, Any]]) -> None:
    CSV_OUT.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0].keys()) if rows else []
    with CSV_OUT.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_markdown(rows: List[Dict[str, Any]], generated_at: str) -> None:
    lines = [
        "# API-Football Domestic priority audit",
        "",
        f"Generated: `{generated_at}`",
        "",
        "Zero live API calls. Start dates are inferred from fixture dates already present in repository cache; `null` means the cache cannot prove the date yet.",
        "",
        "| Priority | Country | League | Season | First fixture | Active | Cache | Completed | With stats | Stats coverage | xG | Saves | Possession | Passes |",
        "|---:|---|---|---|---|:---:|:---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row['priority_rank']} | {row['country']} | {row['league']} | {row['season']} | {row['first_fixture_date'] or 'unknown'} | "
            f"{'yes' if row['active'] else 'no'} | {'yes' if row['cache_exists'] else 'no'} | {row['completed_fixtures']} | "
            f"{row['fixtures_with_statistics']} | {row['statistics_coverage']:.1%} | {row['xg_coverage']:.1%} | "
            f"{row['saves_coverage']:.1%} | {row['possession_coverage']:.1%} | {row['passes_coverage']:.1%} |"
        )
    MD_OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    config = load_json(CONFIG_PATH, {})
    leagues = [item for item in config.get("leagues", []) if isinstance(item, dict)]
    today = dt.datetime.now(dt.timezone.utc).date()
    rows = [audit_league(league, today) for league in leagues]
    rows.sort(key=priority)
    generated_at = now_utc()

    payload = {
        "generated_at": generated_at,
        "source": "config registry + repository fixture-statistics cache",
        "live_api_calls_made": 0,
        "registry_leagues": len(rows),
        "cache_present_leagues": sum(1 for row in rows if row["cache_exists"]),
        "active_leagues": sum(1 for row in rows if row["active"]),
        "late_july_early_august_leagues": sum(1 for row in rows if row["starts_late_july_early_august"]),
        "priority_rule": {
            "1": "future late-July/early-August start",
            "2": "active with incomplete statistics",
            "3": "no cache",
            "4": "cache incomplete",
            "5": "known completed fixtures currently covered",
        },
        "leagues": rows,
    }
    JSON_OUT.parent.mkdir(parents=True, exist_ok=True)
    JSON_OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_csv(rows)
    write_markdown(rows, generated_at)
    print(f"Domestic priority audit: leagues={len(rows)} cache={payload['cache_present_leagues']} live_api_calls=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
