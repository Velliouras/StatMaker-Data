#!/usr/bin/env python3
"""Research-only xG recovery from a locally saved Understat league JSON.

STRICTLY OFFLINE: no HTTP clients, API-Football calls, bookmaker requests,
Android writes or GitHub Actions. Never changes canonical statistics.
Data is imported only from an explicitly provided existing local JSON file.
The overlay is NOT a historically timestamped prediction source.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from math import isfinite
from pathlib import Path
import re
import unicodedata

from publish_shadow import _safe_output, _atomic_write

LEAGUES = {
    "E0": "EPL", "D1": "Bundesliga", "SP1": "La_Liga",
    "I1": "Serie_A", "F1": "Ligue_1"
}
ALLOWED_SEASONS = range(2024, 2028)
# Explicitly verified from the EPL 2026 mirror; never perform fuzzy joins.
DEFAULT_ALIASES = {
    "E0": {"Hull City": "Hull", "Newcastle": "Newcastle United"},
}


def name_key(value: object) -> str:
    s = unicodedata.normalize("NFKD", str(value or "")).casefold()
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(re.findall(r"[a-z0-9]+", s))


def required_xg(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError):
        return None
    return number if isfinite(number) and 0 <= number <= 20 else None


def valid_utc(value: str) -> datetime:
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("Explicit timezone required for observedAtUTC")
    return timestamp.astimezone(timezone.utc)


def provider_fixture_rows(payload: dict) -> list[dict]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        raise ValueError("Understat export must contain JSON object/list")
    rows = payload.get("datesData", payload.get("dates"))
    if not isinstance(rows, list):
        raise ValueError("Understat season JSON has no datesData/dates fixture list")
    return rows


def _number_of_goals(value: object) -> int | None:
    try:
        i = int(str(value))
        return i if i >= 0 and str(value) == str(i) else None
    except (ValueError, TypeError):
        return None


def recovery_rows(enriched: dict, provider: dict, league_code: str,
                  observed_at: str, aliases: dict[str, str] | None = None) -> dict:
    """Only supplement source-missing xG for exact scored/dated team fixtures."""
    observed_time = valid_utc(observed_at)
    aliases = aliases or {}
    if any(name_key(k) == "" or name_key(v) == "" for k, v in aliases.items()):
        raise ValueError("Invalid aliases")
    norm_aliases = {name_key(k): name_key(v) for k, v in aliases.items()}
    if str((enriched.get("competition") or {}).get("league_code") or "") != league_code:
        raise ValueError("League code does not match canonical cache")
    index = defaultdict(list)
    counters = Counter()
    for p in provider_fixture_rows(provider):
        if not isinstance(p, dict) or p.get("isResult") is not True:
            continue
        score = p.get("goals") or {}
        goals = (_number_of_goals(score.get("h")), _number_of_goals(score.get("a")))
        xg_data = p.get("xG") or {}
        xg = (required_xg(xg_data.get("h")), required_xg(xg_data.get("a")))
        if None in goals or None in xg:
            counters["understat_missing_score_or_xg"] += 1
            continue
        h, a = name_key((p.get("h") or {}).get("title")), name_key((p.get("a") or {}).get("title"))
        if not h or not a or h == a:
            counters["understat_invalid_teams"] += 1
            continue
        try:
            # Understat season 'datetime' is timezone-naive. Never falsely
            # attribute it to UTC; 12h is only a matching tolerance, and
            # the canonical UTC date must be within one day of the date label.
            local_naive = datetime.fromisoformat(str(p.get("datetime")))
            if local_naive.tzinfo is not None:
                local_naive = local_naive.astimezone(timezone.utc).replace(tzinfo=None)
        except ValueError:
            counters["understat_invalid_date"] += 1
            continue
        key = (h, a, *goals)
        index[key].append((local_naive, p.get("id"), xg))
    recovered = []
    seen_source_ids = set()
    for m in enriched.get("matches") or []:
        if not isinstance(m, dict) or m.get("status") not in {"FT", "AET", "PEN"}:
            continue
        stats = m.get("normalized_stats") or {}
        if required_xg(stats.get("HxG")) is not None and required_xg(stats.get("AxG")) is not None:
            counters["already_has_both_canonical_xg"] += 1
            continue
        try:
            canonical_time = valid_utc(str(m["date_utc"]))
            hg, ag = int(m["home_goals"]), int(m["away_goals"])
        except (KeyError, ValueError, TypeError):
            counters["invalid_canonical_identity"] += 1
            continue
        if hg < 0 or ag < 0 or not m.get("fixture_id"):
            counters["invalid_canonical_identity"] += 1
            continue
        home = norm_aliases.get(name_key(m.get("home_team")), name_key(m.get("home_team")))
        away = norm_aliases.get(name_key(m.get("away_team")), name_key(m.get("away_team")))
        candidates = [
            record for record in index.get((home, away, hg, ag), [])
            if abs((record[0] - canonical_time.replace(tzinfo=None)).total_seconds()) <= 12 * 3600
        ]
        if len(candidates) != 1:
            counters["unmatched_or_ambiguous"] += 1
            continue
        naive, uid, (hxg, axg) = candidates[0]
        if not uid or uid in seen_source_ids:
            counters["duplicate_understat_id"] += 1
            continue
        # Do not overwrite an already observed canonical value with
        # a different provider scale, including partial source xG.
        existing_h, existing_a = required_xg(stats.get("HxG")), required_xg(stats.get("AxG"))
        if ((existing_h is not None and abs(hxg - existing_h) > 0.01) or
                (existing_a is not None and abs(axg - existing_a) > 0.01)):
            counters["provider_xg_conflict"] += 1
            continue
        seen_source_ids.add(uid)
        recovered.append({
            "leagueCode": league_code,
            "fixtureId": str(m["fixture_id"]),
            "kickoffUTC": canonical_time.isoformat(),
            "homeTeam": str(m["home_team"]),
            "awayTeam": str(m["away_team"]),
            "homeGoals": hg, "awayGoals": ag,
            "homeXg": existing_h if existing_h is not None else hxg,
            "awayXg": existing_a if existing_a is not None else axg,
            "understatMatchId": str(uid),
            "source": "UNDERSTAT_INDEPENDENT_XG",
            "sourceObservedAtUTC": observed_time.isoformat(),
            "historicalAsOfVerified": False,
        })
    return {
        "contract": "betting-v2-understat-xg-research-overlay-v1",
        "leagueCode": league_code, "source": "UNDERSTAT",
        "sourceObservedAtUTC": observed_time.isoformat(),
        "historicalAsOfVerified": False,
        "certified": False,
        "apiFootballCalls": 0,
        "recovered": recovered,
        "counts": dict(sorted(counters.items())),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repository-root", type=Path, default=Path("."))
    ap.add_argument("--league", choices=sorted(LEAGUES), required=True)
    ap.add_argument("--season", type=int, choices=ALLOWED_SEASONS, required=True)
    ap.add_argument("--input", type=Path, required=True,
                    help="Already saved Understat league JSON; no network access")
    ap.add_argument("--aliases", type=Path, help="Optional explicit canonical-name -> Understat-name JSON")
    ap.add_argument("--output", type=Path, default=None)
    args = ap.parse_args()
    root = args.repository_root.resolve()
    index = json.loads((root / "data/statmaker/domestic_enriched/index.json").read_text())
    matches = [item for item in index["leagues"]
               if item.get("league_code") == args.league and
               str(item.get("api_football_season")) == str(args.season)]
    if len(matches) != 1:
        raise ValueError("Expected a unique canonical league/season from index")
    canonical_path = (root / str(matches[0]["output_path"])).resolve()
    if not canonical_path.is_relative_to(root / "data/statmaker/domestic_enriched"):
        raise ValueError("Unsafe canonical cache path")
    enriched = json.loads(canonical_path.read_text())
    data = json.loads(args.input.read_text(encoding="utf-8"))
    aliases = dict(DEFAULT_ALIASES.get(args.league, {}))
    if args.aliases:
        aliases.update(json.loads(args.aliases.read_text()))
    observed = datetime.now(timezone.utc).isoformat()
    overlay = recovery_rows(enriched, data, args.league, observed, aliases)
    overlay["understatSeason"] = args.season
    overlay["retrievalMode"] = "LOCAL_UNDERSTAT_JSON_NO_NETWORK"
    # Per-league/season default prevents one recovery pass from overwriting
    # a previously recovered competition.
    output = args.output or Path(
        f"reports/betting_v2/understat_xg_{args.league}_{args.season}.json"
    )
    _atomic_write(_safe_output(root, output), overlay)
    print(json.dumps({"recovered": len(overlay["recovered"]),
                      "output": str(output),
                      "apiFootballCalls": 0, "certified": False}))


if __name__ == "__main__":
    main()
