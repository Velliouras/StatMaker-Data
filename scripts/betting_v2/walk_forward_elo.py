#!/usr/bin/env python3
"""Independent Elo + observed-goals fallback research for matches lacking xG.

NO fake xG, NO provider requests, NO automatic STRONG or App-Ready publishing.
Unlike the primary xG branch, ELO_FALLBACK selects fixtures whose strictly
pre-kickoff xG history is insufficient. Tune/calibrate/holdout separately.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from itertools import product
import json
from math import exp, log
from pathlib import Path

from audit_data import read_fixtures, number
from walk_forward_goals import event_probs, outcomes
from elo_holdout_diagnostics import summarize_elo_holdout


@dataclass(frozen=True)
class EloParams:
    recent: float
    venue: float
    elo: float


@dataclass(frozen=True)
class EloRow:
    date: str
    league: str
    fixture: str
    kickoff_utc: str
    hgoals: int
    agoals: int
    attack_h: tuple[float, float, float]
    defense_h: tuple[float, float, float]
    attack_a: tuple[float, float, float]
    defense_a: tuple[float, float, float]
    elo_delta: float
    league_h: float
    league_a: float
    missing_prior_xg: bool


def _mean(values: list[float]) -> float:
    if not values:
        raise ValueError("No verified observed goals in feature window")
    return sum(values) / len(values)


def _xg_ready(home: list[dict], away: list[dict]) -> bool:
    """Must match strict prior-date xG model gates, without future game stats."""
    if min(len(home), len(away)) < 8:
        return False
    h, a = home[-20:], away[-20:]
    if not all(sum(x[k] is not None for x in w) >= 8
               for w in (h, a) for k in ("forxg", "againstxg")):
        return False
    if not all(all(x[k] is not None for x in w[-5:])
               for w in (h, a) for k in ("forxg", "againstxg")):
        return False
    return all(sum(x[k] is not None for x in w) >= 3
               for w in (list(x for x in h if x["venue"] == "home")[-8:],
                         list(x for x in a if x["venue"] == "away")[-8:])
               for k in ("forxg", "againstxg"))


def make_elo_rows(matches: list[dict]) -> tuple[list[EloRow], dict]:
    dates: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for m in matches:
        dates[(m["group"], m["date"].date().isoformat())].append(m)
    history: dict[tuple[str, str], list[dict]] = defaultdict(list)
    elo: dict[tuple[str, str], float] = defaultdict(lambda: 1500.0)
    scores: dict[str, list[tuple[int, int]]] = defaultdict(list)
    rows: list[EloRow] = []
    reasons: Counter = Counter()

    def profile(team: list[dict], venue: str, key: str) -> tuple[float, float, float]:
        recent = team[-5:]
        last = team[-20:]
        venue_window = [x for x in last if x["venue"] == venue][-8:]
        return (
            _mean([x[key] for x in recent]),
            _mean([x[key] for x in last]),
            _mean([x[key] for x in venue_window])
        )

    for (group, day), batch in sorted(dates.items()):
        prior = scores[group]
        baseline_h = _mean([p[0] for p in prior]) if prior else 1.4
        baseline_a = _mean([p[1] for p in prior]) if prior else 1.15
        for m in batch:
            home = history[group, m["home"]]
            away = history[group, m["away"]]
            if min(len(home), len(away)) < 8:
                reasons["too_few_prior_team_games"] += 1
                continue
            last_h, last_a = home[-20:], away[-20:]
            if (sum(x["venue"] == "home" for x in last_h) < 3 or
                    sum(x["venue"] == "away" for x in last_a) < 3):
                reasons["too_few_prior_venue_games"] += 1
                continue
            if len(prior) < 20:
                reasons["too_few_prior_league_games"] += 1
                continue
            missing_xg = not _xg_ready(home, away)
            rows.append(EloRow(
                date=day, league=group, fixture=m["fixture_id"],
                kickoff_utc=m["date"].isoformat(),
                hgoals=m["hg"], agoals=m["ag"],
                attack_h=profile(home, "home", "forgoals"),
                defense_h=profile(home, "home", "againstgoals"),
                attack_a=profile(away, "away", "forgoals"),
                defense_a=profile(away, "away", "againstgoals"),
                elo_delta=elo[group, m["home"]] - elo[group, m["away"]] + 55,
                league_h=baseline_h, league_a=baseline_a,
                missing_prior_xg=missing_xg
            ))
            reasons["elo_prematch_ready"] += 1
            if missing_xg:
                reasons["elo_fallback_due_to_insufficient_xg"] += 1

        # No results from the current UTC date are visible above.
        for m in batch:
            # A prospective target is feature-only; never inject invented FT
            # results/xG into the historical rating or form state.
            if m.get("prematch_target"):
                continue
            hkey, akey = (group, m["home"]), (group, m["away"])
            hxg, axg = number(m["stats"].get("HxG")), number(m["stats"].get("AxG"))
            history[hkey].append({
                "venue": "home", "forgoals": m["hg"], "againstgoals": m["ag"],
                "forxg": hxg, "againstxg": axg,
            })
            history[akey].append({
                "venue": "away", "forgoals": m["ag"], "againstgoals": m["hg"],
                "forxg": axg, "againstxg": hxg,
            })
            scores[group].append((m["hg"], m["ag"]))
            rdiff = elo[hkey] - elo[akey] + 55.0
            expected_h = 1.0 / (1.0 + 10.0 ** (-rdiff / 400.0))
            outcome = 1.0 if m["hg"] > m["ag"] else 0.5 if m["hg"] == m["ag"] else 0.0
            adjustment = 20.0 * (outcome - expected_h)
            elo[hkey] += adjustment
            elo[akey] -= adjustment
    return rows, dict(reasons)


def lambda_rates(row: EloRow, params: EloParams) -> tuple[float, float]:
    def combine(values: tuple[float, float, float]) -> float:
        return (params.venue * values[2] +
                (1 - params.venue) *
                (params.recent * values[0] + (1 - params.recent) * values[1]))

    def rate(attack: tuple[float, float, float],
             defense: tuple[float, float, float],
             baseline: float, sign: float) -> float:
        observed = 0.25 * baseline + 0.75 * (
            combine(attack) + combine(defense)) / 2.0
        return max(0.15, min(4.5,
                            observed * exp(sign * params.elo * row.elo_delta / 400.0)))
    return (
        rate(row.attack_h, row.defense_a, row.league_h, 1.0),
        rate(row.attack_a, row.defense_h, row.league_a, -1.0)
    )


def logloss(rows: list[EloRow], params: EloParams) -> float:
    if not rows:
        return float("inf")
    total = 0.0
    for row in rows:
        h, a = lambda_rates(row, params)
        p = event_probs(h, a)
        key = "1X2_HOME" if row.hgoals > row.agoals else (
            "1X2_DRAW" if row.hgoals == row.agoals else "1X2_AWAY")
        total -= log(max(1e-12, p[key]))
    return total / len(rows)


def forecasts(rows: list[EloRow], params: EloParams) -> list[dict]:
    records = []
    for r in rows:
        h, a = lambda_rates(r, params)
        p = event_probs(h, a)
        records.append({
            "date": r.date, "league": r.league, "leagueCode": r.league.split("|", 1)[0],
            "fixtureId": r.fixture, "kickoffUTC": r.kickoff_utc,
            "homeGoals": r.hgoals, "awayGoals": r.agoals,
            "probabilities": p,
            "expectedHomeGoals": h, "expectedAwayGoals": a,
            "observed": outcomes(r),
            "strategy": "ELO_GOALS_FALLBACK_NO_XG",
            "historicalXgSufficient": not r.missing_prior_xg,
            "notCertified": True,
        })
    return records


def walk_forward_elo(root: Path) -> tuple[dict, list[dict], list[dict]]:
    matches, problems, leagues = read_fixtures(root)
    rows, readiness = make_elo_rows(matches)
    rows.sort(key=lambda r: (r.date, r.league, r.fixture))
    # A model calibrated for the no-xG population must be trained on the
    # SAME no-xG population. Using the xG-rich population for ELO tuning
    # would silently reintroduce regime selection bias.
    fallback = [r for r in rows if r.missing_prior_xg]
    days = sorted({r.date for r in fallback})
    if len(days) < 50:
        return ({
            "contract": "betting-v2-elo-fallback-research-v1",
            "notCertified": True, "strategy": "ELO_GOALS_FALLBACK_NO_XG",
            "error": "Fewer than 50 distinct fallback-only pregame dates",
            "eloEligible": len(rows), "eloFallbackEligible": len(fallback),
            "providerCalls": 0, "liveStrongPicks": 0,
            "requiresSeparateCalibration": True,
        }, [], [])
    boundary = lambda frac: days[min(len(days) - 1, int(len(days) * frac))]
    fit, tune_end, cal_end = boundary(.55), boundary(.75), boundary(.90)
    tuning = [r for r in fallback if fit < r.date <= tune_end]
    calibration = [r for r in fallback if tune_end < r.date <= cal_end]
    untouched = [r for r in fallback if r.date > cal_end]
    if not tuning or not calibration or not untouched:
        return ({
            "contract": "betting-v2-elo-fallback-research-v1",
            "notCertified": True, "strategy": "ELO_GOALS_FALLBACK_NO_XG",
            "error": "Disjoint fallback-only tuning/calibration/holdout unavailable",
            "eloEligible": len(rows), "eloFallbackEligible": len(fallback),
            "providerCalls": 0, "liveStrongPicks": 0,
            "requiresSeparateCalibration": True,
        }, [], [])
    params = [EloParams(*x) for x in product((.25, .5), (.35, .65), (.0, .25, .5))]
    best = min(params, key=lambda p: logloss(tuning, p))
    calibration_forecasts = forecasts(calibration, best)
    holdout_forecasts = forecasts(untouched, best)
    holdout_diagnostics = summarize_elo_holdout(holdout_forecasts)
    return ({
        "contract": "betting-v2-elo-fallback-research-v1",
        "notCertified": True, "strategy": "ELO_GOALS_FALLBACK_NO_XG",
        "requiresSeparateCalibration": True, "requiresActualDatedBookmakerOdds": True,
        "requiresAdverseLineupBounds": True, "liveStrongPicks": 0,
        "dataSources": "local completed scores, venue form and pregame research Elo",
        "providerCalls": 0, "neverImputesXg": True,
        "leagues": len(leagues), "sourceErrors": dict(problems),
        "eloEligible": len(rows),
        "eloFallbackEligible": len(fallback),
        "tuningPopulation": "ELO_FALLBACK_ONLY_NO_XG",
        "readiness": readiness,
        "splits": {"fitEnd": fit, "tuneEnd": tune_end, "calibrationEnd": cal_end,
                   "tuneN": len(tuning), "fallbackTuneN": len(tuning),
                   "fallbackCalibrationN": len(calibration),
                   "fallbackHoldoutN": len(untouched)},
        "tunedParams": {"recent": best.recent, "venue": best.venue, "elo": best.elo},
        "tune1X2LogLoss": logloss(tuning, best),
        "holdoutFallback1X2LogLoss": logloss(untouched, best) if untouched else None,
        "retrospectiveFallbackHoldout": holdout_diagnostics,
        "holdoutCanCertifyStrong": False,
    }, calibration_forecasts, holdout_forecasts)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repository-root", type=Path, default=Path("."))
    ap.add_argument("--output", type=Path,
                    default=Path("reports/betting_v2/elo_fallback_research.json"))
    args = ap.parse_args()
    from publish_shadow import _safe_output, _atomic_write
    root = args.repository_root.resolve()
    report, cal, holdout = walk_forward_elo(root)
    _atomic_write(_safe_output(root, args.output), report)
    print(json.dumps({
        "eloEligible": report.get("eloEligible", 0),
        "fallbackEligible": report.get("eloFallbackEligible", 0),
        "calibrationFallback": len(cal), "holdoutFallback": len(holdout),
        "certified": False, "providerCalls": 0,
    }))


if __name__ == "__main__":
    main()
