#!/usr/bin/env python3
"""Near-real-time postmatch statistics; no live polling, no betting and no API on idle runs.

The existing Priority Results producer is the source of fixture IDs and final status.
Read only finished matches from the last ten hours, import verified /fixtures/statistics,
and never refetch already-complete match stats. The shared atomic API budget guard is
required by the workflow and preserves the 1,500/day reserve.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

import api_football_fetch_fixture_stats as stats
from expand_domestic_full_stats import expand_fixture
try:
    from api_football_budget import BudgetStop, priority
except ImportError:
    from api_budget_runtime.api_football_budget import BudgetStop, priority

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = "data/statmaker/monitoring/recent_finished_match_stats.json"
FINALS = {"FT", "AET", "PEN"}
MAX_CALLS = 32
RETRY_MINUTES = 12
WINDOW_HOURS = 10
PRIMARY = {"G1", "E0", "SP1", "I1", "D1", "F1"}
UEFA = {"CL", "EL", "UECL"}


def read(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}


def write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def instant(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (TypeError, ValueError, OverflowError):
        return None


def has_stats(item):
    values = item.get("normalized_stats")
    return isinstance(values, dict) and any(value is not None for value in values.values())


def both_sides(values):
    return (
        isinstance(values, dict)
        and any(k.startswith("H") and v is not None for k, v in values.items())
        and any(k.startswith("A") and v is not None for k, v in values.items())
    )


def cache_catalog(root):
    index = read(root / "data/statmaker/domestic_enriched/index.json")
    catalog = {}
    for row in index.get("leagues", []):
        code = str(row.get("league_code") or "")
        season = str(row.get("api_football_season") or "")
        filename = str(row.get("cache_path") or "")
        if not code or not season or not filename.startswith("data/api_football/fixture_stats/") or ".." in Path(filename).parts:
            continue
        catalog[(code, season)] = row
    return catalog


def candidates(root, now, state=None):
    state = state or {}
    catalog = cache_catalog(root)
    result = []
    for path in sorted((root / "data/statmaker/priority_refresh").glob("*.json")):
        doc = read(path)
        code = str(doc.get("leagueCode") or "")
        season = str(doc.get("season") or "")
        entry = catalog.get((code, season))
        if not entry or int(entry.get("api_football_league_id") or 0) != int(doc.get("apiFootballLeagueId") or 0):
            continue
        cache_path = root / entry["cache_path"]
        cache = read(cache_path)
        if (str(cache.get("season")) != season
                or int(cache.get("league_id") or 0) != int(doc["apiFootballLeagueId"])):
            continue  # Never merge ambiguous/cross-season identities.
        by_id = stats.cached_fixture_map(cache)
        for row in doc.get("fixtures", []):
            if row.get("status") not in FINALS or not isinstance(row.get("fixture_id"), int):
                continue
            if not isinstance(row.get("home_goals"), int) or not isinstance(row.get("away_goals"), int):
                continue
            kickoff = instant(row.get("date_utc"))
            if kickoff is None or not timedelta(minutes=60) <= now - kickoff <= timedelta(hours=WINDOW_HOURS):
                continue
            fixture_id = row["fixture_id"]
            cached = by_id.get(fixture_id, {})
            if has_stats(cached) and both_sides(cached.get("normalized_stats")):
                continue
            previous = state.get(str(fixture_id)) or {}
            last = instant(previous.get("lastAttemptUTC"))
            if last is not None and now - last < timedelta(minutes=RETRY_MINUTES):
                continue
            result.append((code, entry, row, cache_path, cache))
    return sorted(result, key=lambda x: (
        0 if x[0] in PRIMARY else 1 if x[0] in UEFA else 2,
        -(instant(x[2]["date_utc"]).timestamp()),
        x[2]["fixture_id"],
    ))


def source_fixture(code, entry, row):
    league_id = int(entry["api_football_league_id"])
    fixture = {
        "fixture": {"id": row["fixture_id"], "date": row["date_utc"],
                    "status": {"short": row["status"]}},
        "league": {"id": league_id, "name": entry["league"],
                   "country": entry["country"], "season": int(entry["api_football_season"]),
                   "round": row.get("round")},
        "teams": {
            "home": {"id": row.get("home_team_id"), "name": row.get("home_team")},
            "away": {"id": row.get("away_team_id"), "name": row.get("away_team")},
        },
        "goals": {"home": row["home_goals"], "away": row["away_goals"]},
        "score": {"fulltime": {"home": row["home_goals"], "away": row["away_goals"]}},
    }
    return fixture


def refresh(root=ROOT, now=None, maximum=24, fetch=None):
    now = now or datetime.now(timezone.utc)
    if not 1 <= maximum <= MAX_CALLS:
        raise ValueError("Per-run cap must be 1..32")
    state_path = root / STATE_PATH
    history = read(state_path)
    attempts = history.get("attempts") or {}
    selected = candidates(root, now, attempts)
    if not selected:
        print("RECENT_POSTMATCH_STATS_IDLE no provider calls")
        return {"requests": 0, "updated": 0, "pending": 0, "stateChanged": False}
    api_key = os.getenv("API_FOOTBALL_KEY", "").strip()
    if fetch is None and not api_key:
        raise RuntimeError("API_FOOTBALL_KEY missing")
    fetch = fetch or stats.api_get
    counter = {"count": 0}
    changed = False
    touched = {}
    received = 0
    for code, entry, row, path, cache in selected:
        if counter["count"] >= maximum:
            break
        fid = row["fixture_id"]
        level = "primary" if code in PRIMARY or code in UEFA else "europe"
        try:
            with priority(level):
                payload = fetch(api_key, "fixtures/statistics", {"fixture": fid}, counter, maximum)
        except BudgetStop:
            break
        except (OSError, ValueError, RuntimeError) as exc:
            print("RECENT_POSTMATCH_PROVIDER_RETRY", fid, type(exc).__name__)
            attempts[str(fid)] = {"lastAttemptUTC": now.isoformat(), "status": "ERROR"}
            continue
        if stats.api_errors(payload) or not isinstance(payload.get("response"), list):
            attempts[str(fid)] = {"lastAttemptUTC": now.isoformat(), "status": "ERROR"}
            continue
        raw = stats.response_items(payload)
        fixture = source_fixture(code, entry, row)
        merged = stats.merge_cached_fixture(
            stats.cached_fixture_map(cache).get(fid, {}), fixture, "priority_final_verified"
        )
        merged["home_team_id"] = row.get("home_team_id")
        merged["away_team_id"] = row.get("away_team_id")
        normalized = stats.normalize_statistics(raw, fixture)
        merged["raw_statistics"] = raw
        merged["normalized_stats"] = normalized
        merged["statsAttemptedAtUtc"] = now.isoformat().replace("+00:00", "Z")
        merged["statsAttemptedAtBasis"] = "ACTUAL_PROVIDER_RESPONSE"
        merged["statsProviderResponse"] = "POPULATED" if both_sides(normalized) else "EMPTY_OR_PARTIAL"
        merged, _ = expand_fixture(merged)
        previous = stats.cached_fixture_map(cache).get(fid, {})
        if has_stats(merged) and merged.get("normalized_stats") != previous.get("normalized_stats"):
            by_id = stats.cached_fixture_map(cache)
            by_id[fid] = merged
            cache["fixtures"] = sorted(by_id.values(),
                                       key=lambda a: (str(a.get("date") or ""), int(a.get("fixture_id") or 0)))
            touched[str(path)] = cache
            changed = True
            received += 1
        if both_sides(merged.get("normalized_stats")):
            attempts.pop(str(fid), None)
        else:
            attempts[str(fid)] = {"lastAttemptUTC": now.isoformat(), "status": "EMPTY_OR_PARTIAL"}
    # Preserve incomplete attempts for subsequent provider publication, but never
    # keep growing historical state for fixtures outside the live window.
    attempts = {key: value for key, value in attempts.items()
                if (instant(value.get("lastAttemptUTC")) is not None
                    and now - instant(value["lastAttemptUTC"]) < timedelta(days=2))}
    state_changed = attempts != (history.get("attempts") or {})
    for path, data in touched.items():
        write(path, data)
    if state_changed:
        write(state_path, {"schemaVersion": 1, "attempts": attempts})
    print("RECENT_POSTMATCH_STATS", json.dumps({
        "requests": counter["count"], "updated": received,
        "eligible": len(selected), "stateChanged": state_changed
    }))
    return {"requests": counter["count"], "updated": received,
            "pending": len(attempts), "stateChanged": state_changed}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-requests", type=int, default=24)
    args = ap.parse_args()
    outcome = refresh(maximum=args.max_requests)
    output = os.getenv("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as f:
            f.write("updated=" + ("true" if outcome["updated"] else "false") + "\n")
            f.write("state_changed=" + ("true" if outcome["stateChanged"] else "false") + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
