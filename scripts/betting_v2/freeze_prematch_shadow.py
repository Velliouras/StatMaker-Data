#!/usr/bin/env python3
"""Freeze strict, prior-date xG/Elo probability snapshots for scheduled games.

Zero provider requests. Reads only the existing Data/main enriched FT archive
and the existing, verified Odds-API.io domestic schedule. Requires one unique
mapped league, unambiguous canonical HOME/AWAY team identities, a real provider
event ID and a future UTC kickoff. No fake xG, invented FT score or alias
guessing; no App-Ready/Android writes; not certified value or betting picks.

All snapshot probabilities are frozen before kickoff with a local-source
hash and fixed previously tuned research parameters. This does NOT establish
an independently verified API-Football future fixture crosswalk or historical
bookmaker quote provenance. No STRONG or ROI claim.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import gzip
import json
from pathlib import Path

from audit_data import read_fixtures
from fixture_lookup import canonical_name, as_utc
from walk_forward_goals import (
    Parameters, make_rows, model_lambdas, event_probs,
)
from walk_forward_elo import (
    EloParams, make_elo_rows, lambda_rates,
)

# Fixed from the independently tested 2026-10-10 research calibration;
# never re-tune on present/future football results in the normal odds job.
XG_PARAMS = Parameters(.25, .65, .80, .50)
ELO_PARAMS = EloParams(.50, .35, .50)
PARAMS_VERSION = "statmaker-v2-frozen-pilot-2026-10-10-v1"
SCHEDULE_PATH = "odds/odds_api_io/domestic_odds.json"
OUTPUT_DIR = "reports/betting_v2/forward_snapshots"
MAX_DAYS = 8
MAX_TARGETS = 1200


def _json_bytes(obj: object) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _historical_name_map(matches: list[dict]) -> dict[tuple[str, str], set[str]]:
    """Never resolve colliding canonical names to an arbitrary team."""
    names: dict[tuple[str, str], set[str]] = defaultdict(set)
    for m in matches:
        for field in ("home", "away"):
            raw = str(m[field])
            names[(m["group"], canonical_name(raw))].add(raw)
    return names


def _group_index(matches: list[dict]) -> dict[str, set[str]]:
    groups: dict[str, set[str]] = defaultdict(set)
    for m in matches:
        groups[m["group"].split("|", 1)[0]].add(m["group"])
    return groups


def verified_targets(feed: dict, matches: list[dict], now: datetime,
                     *, horizon_days: int = MAX_DAYS) -> tuple[list[dict], Counter]:
    """Resolve scheduled bookmaker fixtures against historical team entities.

    Note: provider event ID is not yet a verified independent API-Football ID.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone aware")
    if not 0 < horizon_days <= MAX_DAYS:
        raise ValueError("horizon outside bounded research limit")
    groups = _group_index(matches)
    names = _historical_name_map(matches)
    reasons: Counter = Counter()
    selected: list[dict] = []
    seen: set[tuple[str, str]] = set()
    if not isinstance(feed, dict) or not isinstance(feed.get("leagues"), list):
        raise ValueError("Missing official domestic odds schedule")
    for league in feed["leagues"]:
        if not isinstance(league, dict):
            continue
        code = str(league.get("leagueCode") or "").strip()
        compatible = groups.get(code, set())
        if len(compatible) != 1:
            reasons["UNKNOWN_OR_AMBIGUOUS_HISTORIC_COMPETITION"] += len(
                league.get("matches") or []
            )
            continue
        group = next(iter(compatible))
        for event in league.get("matches") or []:
            reasons["scheduleFixturesSeen"] += 1
            if not isinstance(event, dict):
                reasons["NON_OBJECT_PROVIDER_EVENT"] += 1
                continue
            ident = str(event.get("id") or "").strip()
            when = as_utc(event.get("kickoff"))
            if not ident or when is None:
                reasons["NO_PROVIDER_ID_OR_UTC_KICKOFF"] += 1
                continue
            if not now < when <= now + timedelta(days=horizon_days):
                reasons["NOT_FUTURE_IN_WINDOW"] += 1
                continue
            if event.get("scheduleVerified") is not True:
                reasons["UNVERIFIED_ODDS_PROVIDER_SCHEDULE"] += 1
                continue
            if event.get("teamMappingStatus") != "matched":
                reasons["UNVERIFIED_TEAM_MAPPING"] += 1
                continue
            home = canonical_name(event.get("canonicalHomeTeam"))
            away = canonical_name(event.get("canonicalAwayTeam"))
            hnames, anames = names.get((group, home), set()), names.get((group, away), set())
            if len(hnames) != 1 or len(anames) != 1 or home == away:
                reasons["AMBIGUOUS_OR_UNKNOWN_HISTORICAL_TEAM"] += 1
                continue
            key = (code, ident)
            if key in seen:
                reasons["DUPLICATE_PROVIDER_EVENT_ID"] += 1
                continue
            seen.add(key)
            selected.append({
                "providerEventId": ident,
                "leagueCode": code,
                "group": group,
                "home": next(iter(hnames)),
                "away": next(iter(anames)),
                "providerHomeTeam": str(event.get("providerHomeTeam") or ""),
                "providerAwayTeam": str(event.get("providerAwayTeam") or ""),
                "kickoff": when,
                "kickoffUTC": when.isoformat(),
            })
            if len(selected) > MAX_TARGETS:
                raise ValueError("Too many scheduled targets")
    return sorted(selected, key=lambda x:(x["kickoff"],x["leagueCode"],x["providerEventId"])), reasons


def freeze(root: Path, now: datetime, *, horizon_days: int = MAX_DAYS,
           previous_matches: list[dict] | None = None,
           schedule: dict | None = None) -> dict:
    """Scores future events using strictly earlier **completed** dated matches.

    Rows with 'prematch_target' have no observed result and are explicitly
    excluded by BOTH historic xG and ELO state-update loops. Predictions
    are never trained on target match results or future dated results.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone aware")
    now = now.astimezone(timezone.utc)
    root = root.resolve()
    history = previous_matches if previous_matches is not None else read_fixtures(root)[0]
    payload = schedule if schedule is not None else json.loads(
        (root / SCHEDULE_PATH).read_text(encoding="utf-8")
    )
    targets, rejected = verified_targets(payload, history, now,
                                         horizon_days=horizon_days)
    # For the historical stream, disallow anything occurring on the UTC as-of
    # date or later, even if that match may have already finished today.
    # This exactly mirrors the prior-UTC-calendar-date research feature gate.
    safe_history = [
        m for m in history
        if m["date"].astimezone(timezone.utc).date() < now.date()
    ]
    # SHA-256 of the actual feature-bearing history and source fixture schedule:
    # a later regenerated archive may not be substituted for the real as-of
    # input without a detectable content-hash change.
    history_hash = sha256(_json_bytes([
        {
            "group": m["group"], "id": m["fixture_id"],
            "date": m["date"].isoformat(), "home": m["home"], "away": m["away"],
            "homeGoals": m["hg"], "awayGoals": m["ag"],
            "HxG": m["stats"].get("HxG"), "AxG": m["stats"].get("AxG"),
        } for m in sorted(
            safe_history, key=lambda x: (x["group"], x["date"], x["fixture_id"])
        )
    ])).hexdigest()
    schedule_hash = sha256(_json_bytes(payload)).hexdigest()
    records: list[dict] = []
    by_day: dict[str, list[dict]] = defaultdict(list)
    for t in targets:
        by_day[t["kickoff"].date().isoformat()].append(t)

    for day in sorted(by_day):
        ready = [m for m in safe_history if m["date"].date().isoformat() < day]
        future = [{
            "group": t["group"], "fixture_id": "provider:" + t["providerEventId"],
            "date": t["kickoff"], "home": t["home"], "away": t["away"],
            "hg": None, "ag": None, "stats": {}, "prematch_target": True,
        } for t in by_day[day]]
        combined = ready + future
        xg_rows, _ = make_rows(combined)
        elo_rows, _ = make_elo_rows(combined)
        xg_by_id = {r.fixture:r for r in xg_rows
                    if r.fixture.startswith("provider:")}
        elo_by_id = {r.fixture:r for r in elo_rows
                     if r.fixture.startswith("provider:")}
        for t in by_day[day]:
            key = "provider:" + t["providerEventId"]
            selected_model = ""
            row = xg_by_id.get(key)
            if row is not None:
                home_lambda, away_lambda = model_lambdas(row, XG_PARAMS)
                selected_model = "XG_PRIMARY"
            else:
                erow = elo_by_id.get(key)
                if erow is None:
                    rejected["INSUFFICIENT_PRIOR_GOALS_OR_XG_HISTORY"] += 1
                    continue
                if not erow.missing_prior_xg:
                    rejected["XG_READY_BUT_PRIMARY_GATED"] += 1
                    continue
                home_lambda, away_lambda = lambda_rates(erow, ELO_PARAMS)
                selected_model = "ELO_GOALS_FALLBACK_NO_XG"
            if t["kickoff"] <= now:
                raise ValueError("Attempted snapshot after kickoff")
            records.append({
                "contract": "statmaker-v2-immutable-shadow-forecast-v1",
                "providerEventId": t["providerEventId"],
                "leagueCode": t["leagueCode"],
                "providerHomeTeam": t["providerHomeTeam"],
                "providerAwayTeam": t["providerAwayTeam"],
                "kickoffUTC": t["kickoffUTC"],
                "forecastComputedAtUTC": now.isoformat(),
                "strategy": selected_model,
                "modelVersion": PARAMS_VERSION,
                "historicalFeatureArchiveSha256": history_hash,
                "providerScheduleSnapshotSha256": schedule_hash,
                "parameters": asdict(XG_PARAMS if row is not None else ELO_PARAMS),
                "expectedHomeGoals": round(home_lambda, 6),
                "expectedAwayGoals": round(away_lambda, 6),
                "probabilities": {
                    k: round(v, 8)
                    for k,v in event_probs(home_lambda, away_lambda).items()
                },
                "historicalDataCutoffExclusiveUTC": now.date().isoformat(),
                "modelFeatureAsOfProvenByLocalSource": True,
                "independentApiFootballFixtureId": None,
                "independentCrossProviderIdentityVerified": False,
                "independentBookmakerQuoteTimeVerified": False,
                "completeQuoteSelectionUniverseVerified": False,
                "noObservedTargetScoreUsed": True,
                "certifiedStrong": False,
                "researchOnly": True,
            })
    counters = Counter(x["strategy"] for x in records)
    return {
        "contract": "statmaker-v2-immutable-shadow-snapshot-set-v1",
        "forecastComputedAtUTC": now.isoformat(),
        "modelVersion": PARAMS_VERSION,
        "modelParametersFrozen": True,
        "historicalFeatureArchiveSha256": history_hash,
        "providerScheduleSnapshotSha256": schedule_hash,
        "apiCalls": 0,
        "outputDestination": "SHADOW_RESEARCH_ONLY",
        "certificationStatus": "BLOCKED",
        "realStrongSelections": 0,
        "independentCrossProviderIdentityVerified": False,
        "bookmakerPriceUpdatedAtVerified": False,
        "scheduledProviderFixturesEligible": len(targets),
        "forecastCount": len(records),
        "modelCounts": dict(sorted(counters.items())),
        "rejected": dict(sorted(rejected.items())),
        "forecastData": records,
    }


def write_snapshot(root: Path, result: dict, *, now: datetime) -> Path:
    if result.get("certificationStatus") != "BLOCKED":
        raise ValueError("Refuse certification or production write")
    root = root.resolve()
    target_dir = root / OUTPUT_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    payload = _json_bytes(result)
    key = sha256(payload).hexdigest()[:16]
    when = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = target_dir / f"{when}_{key}.json.gz"
    # Never overwrite a previous recorded point-in-time forecast.
    # Deterministic gzip: no embedded original filename or varying mtime.
    with path.open("xb") as stream:
        with gzip.GzipFile(filename="", mode="wb", fileobj=stream,
                           compresslevel=7, mtime=0) as zipped:
            zipped.write(payload + b"\n")
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repository-root", type=Path, default=Path("."))
    ap.add_argument("--horizon-days", type=int, default=MAX_DAYS)
    ap.add_argument("--report", type=Path,
                    default=Path("reports/betting_v2/pilot_forward_shadow_diagnostics.json"))
    ap.add_argument("--archive", action="store_true")
    args = ap.parse_args()
    root = args.repository_root.resolve()
    now = datetime.now(timezone.utc)
    result = freeze(root, now, horizon_days=args.horizon_days)
    # Diagnostics only when invoked by manual QA; production odds cycle
    # explicitly opts into append-only research snapshot archive.
    report = (root / args.report).resolve()
    report.relative_to(root / "reports/betting_v2")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_bytes(_json_bytes({
        k: v for k,v in result.items() if k != "forecastData"
    }) + b"\n")
    saved = None
    if args.archive and result["forecastCount"]:
        saved = write_snapshot(root, result, now=now)
    print(json.dumps({
        "forecastCount": result["forecastCount"],
        "modelCounts": result["modelCounts"],
        "rejected": result["rejected"],
        "archived": saved.relative_to(root).as_posix() if saved else None,
        "providerCalls": 0, "certifiedStrong": 0,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
