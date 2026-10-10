#!/usr/bin/env python3
"""Walk-forward research baseline for V2 result and goals probabilities.

Read-only local input: StatMaker-Data/main domestic_enriched schema v3.
Produces diagnostics, NEVER STRONG picks or production forecast certificates.
No odds data => ROI and +EV cannot be certified here.

Usage:
 python research/betting_v2/walk_forward_goals.py \
   --data-root ../StatMaker-Data --output /tmp/v2-goals-research.json
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import date
from itertools import product
import json
from math import exp, lgamma, log, sqrt
from pathlib import Path

from audit_data import read_fixtures, number

MIN_TEAM = 8
MIN_VENUE = 3
MIN_XG = 8


def mean(values: list[float], fallback: float) -> float:
    return sum(values) / len(values) if values else fallback


@dataclass(frozen=True)
class Parameters:
    recent_weight: float
    venue_weight: float
    xg_weight: float
    elo_weight: float


@dataclass
class PrematchRow:
    date: str
    league: str
    fixture: str
    hgoals: int
    agoals: int
    home_att_recent_xg: float
    home_att_long_xg: float
    home_att_venue_xg: float
    home_att_recent_goals: float
    home_att_long_goals: float
    home_att_venue_goals: float
    away_def_recent_xg: float
    away_def_long_xg: float
    away_def_venue_xg: float
    away_def_recent_goals: float
    away_def_long_goals: float
    away_def_venue_goals: float
    away_att_recent_xg: float
    away_att_long_xg: float
    away_att_venue_xg: float
    away_att_recent_goals: float
    away_att_long_goals: float
    away_att_venue_goals: float
    home_def_recent_xg: float
    home_def_long_xg: float
    home_def_venue_xg: float
    home_def_recent_goals: float
    home_def_long_goals: float
    home_def_venue_goals: float
    elo_delta: float
    league_home_mean: float
    league_away_mean: float


def make_rows(matches: list[dict]) -> tuple[list[PrematchRow], dict]:
    """One strictly prior-date event stream per league; xG/score are labels only."""
    dated: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for m in matches:
        dated[(m["group"], m["date"].date().isoformat())].append(m)
    history: dict[tuple[str, str], list[dict]] = defaultdict(list)
    elo: dict[tuple[str, str], float] = defaultdict(lambda: 1500.0)
    league_scores: dict[str, list[tuple[int, int]]] = defaultdict(list)
    rows: list[PrematchRow] = []
    counts = defaultdict(int)

    def feature(team: list[dict], which: str, venue: str, subset: str) -> tuple[float, float, float, float, float, float]:
        last = team[-20:]
        recent = last[-5:]
        at_venue = [x for x in last if x["venue"] == venue][-8:]
        fallback = 1.4
        def vals(source: list[dict], key: str) -> list[float]:
            return [float(x[key]) for x in source if x[key] is not None]
        return (
            mean(vals(recent, which + "xg"), fallback),
            mean(vals(last, which + "xg"), fallback),
            mean(vals(at_venue, which + "xg"), fallback),
            mean(vals(recent, which + "goals"), fallback),
            mean(vals(last, which + "goals"), fallback),
            mean(vals(at_venue, which + "goals"), fallback),
        )

    for group, today in sorted(dated):
        batch = dated[(group, today)]
        prior = league_scores[group]
        league_h = mean([x[0] for x in prior], 1.4)
        league_a = mean([x[1] for x in prior], 1.15)

        for m in batch:
            home = history[(group, m["home"])]
            away = history[(group, m["away"])]
            if min(len(home), len(away)) < MIN_TEAM:
                counts["short_team_history"] += 1
                continue
            home_venue = [x for x in home if x["venue"] == "home"]
            away_venue = [x for x in away if x["venue"] == "away"]
            if min(len(home_venue), len(away_venue)) < MIN_VENUE:
                counts["short_venue_history"] += 1
                continue
            if min(sum(x["forxg"] is not None for x in home),
                   sum(x["forxg"] is not None for x in away)) < MIN_XG:
                counts["short_xg_history"] += 1
                continue
            if not all(sum(x["forxg"] is not None for x in side) >= MIN_VENUE
                       for side in (home_venue, away_venue)):
                counts["short_venue_xg"] += 1
                continue
            ha = feature(home, "for", "home", "attack")
            ad = feature(away, "against", "away", "defense")
            aa = feature(away, "for", "away", "attack")
            hd = feature(home, "against", "home", "defense")
            if len(prior) < 20:
                counts["short_league_history"] += 1
                continue
            rows.append(PrematchRow(
                today, group, m["fixture_id"], m["hg"], m["ag"],
                *ha, *ad, *aa, *hd,
                elo[(group, m["home"])] - elo[(group, m["away"])],
                league_h, league_a
            ))
            counts["forecastable_prematch_rows"] += 1

        # IMPORTANT: update only after forecasting ALL same-date fixtures.
        for m in batch:
            hkey = (group, m["home"])
            akey = (group, m["away"])
            hxg = number(m["stats"].get("HxG"))
            axg = number(m["stats"].get("AxG"))
            history[hkey].append({
                "venue": "home", "forxg": hxg, "againstxg": axg,
                "forgoals": m["hg"], "againstgoals": m["ag"]
            })
            history[akey].append({
                "venue": "away", "forxg": axg, "againstxg": hxg,
                "forgoals": m["ag"], "againstgoals": m["hg"]
            })
            league_scores[group].append((m["hg"], m["ag"]))
            # Research ELO rating; not an independently certified provider rating.
            diff = elo[hkey] - elo[akey] + 55.0
            expected = 1.0 / (1.0 + 10.0 ** (-diff / 400.0))
            actual = 1.0 if m["hg"] > m["ag"] else 0.5 if m["hg"] == m["ag"] else 0.0
            delta = 20.0 * (actual - expected)
            elo[hkey] += delta
            elo[akey] -= delta
    return rows, dict(counts)


def model_lambdas(row: PrematchRow, p: Parameters) -> tuple[float, float]:
    def side(r_xg: float, l_xg: float, v_xg: float,
             r_goals: float, l_goals: float, v_goals: float,
             other_rxg: float, other_lxg: float, other_vxg: float,
             other_rg: float, other_lg: float, other_vg: float,
             prior_mean: float, sign: float) -> float:
        recent = p.recent_weight
        venue = p.venue_weight
        xg = p.xg_weight
        att_xg = venue * v_xg + (1.0 - venue) * (recent * r_xg + (1 - recent) * l_xg)
        def_xg = venue * other_vxg + (1.0 - venue) * (recent * other_rxg + (1 - recent) * other_lxg)
        att_goals = venue * v_goals + (1.0 - venue) * (recent * r_goals + (1 - recent) * l_goals)
        def_goals = venue * other_vg + (1.0 - venue) * (recent * other_rg + (1 - recent) * other_lg)
        attacking = xg * att_xg + (1.0 - xg) * att_goals
        defending = xg * def_xg + (1.0 - xg) * def_goals
        raw = 0.25 * prior_mean + 0.75 * (attacking + defending) / 2.0
        return max(0.15, min(4.5, raw * exp(sign * p.elo_weight * row.elo_delta / 400.0)))

    h = side(row.home_att_recent_xg, row.home_att_long_xg, row.home_att_venue_xg,
             row.home_att_recent_goals, row.home_att_long_goals, row.home_att_venue_goals,
             row.away_def_recent_xg, row.away_def_long_xg, row.away_def_venue_xg,
             row.away_def_recent_goals, row.away_def_long_goals, row.away_def_venue_goals,
             row.league_home_mean, 1.0)
    a = side(row.away_att_recent_xg, row.away_att_long_xg, row.away_att_venue_xg,
             row.away_att_recent_goals, row.away_att_long_goals, row.away_att_venue_goals,
             row.home_def_recent_xg, row.home_def_long_xg, row.home_def_venue_xg,
             row.home_def_recent_goals, row.home_def_long_goals, row.home_def_venue_goals,
             row.league_away_mean, -1.0)
    return h, a


def poisson_probs(rate: float, max_goals: int = 15) -> list[float]:
    values = [exp(-rate)]
    for i in range(1, max_goals + 1):
        values.append(values[-1] * rate / i)
    values[-1] += max(0.0, 1.0 - sum(values))
    return values


def event_probs(h: float, a: float) -> dict[str, float]:
    ph = poisson_probs(h)
    pa = poisson_probs(a)
    result = {
        "1X2_HOME": sum(x * y for i, x in enumerate(ph)
                          for j, y in enumerate(pa) if i > j),
        "1X2_DRAW": sum(x * y for i, x in enumerate(ph)
                          for j, y in enumerate(pa) if i == j),
        "1X2_AWAY": sum(x * y for i, x in enumerate(ph)
                          for j, y in enumerate(pa) if i < j),
        "HOME_OVER_1_5": sum(ph[2:]),
        "AWAY_OVER_1_5": sum(pa[2:]),
        "MATCH_OVER_2_5": 1.0 - sum(ph[i] * pa[j] for i in range(3)
                                  for j in range(3 - i))
    }
    return result


def outcomes(row: PrematchRow) -> dict[str, int]:
    h, a = row.hgoals, row.agoals
    return {
        "1X2_HOME": int(h > a), "1X2_DRAW": int(h == a),
        "1X2_AWAY": int(h < a), "HOME_OVER_1_5": int(h >= 2),
        "AWAY_OVER_1_5": int(a >= 2),
        "MATCH_OVER_2_5": int(h + a >= 3)
    }


def measures(rows: list[PrematchRow], p: Parameters) -> dict:
    if not rows:
        return {"n": 0, "score": None}
    nll = 0.0
    brier = defaultdict(float)
    buckets: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        home, away = model_lambdas(row, p)
        forecasts = event_probs(home, away)
        actual = outcomes(row)
        observed_label = "1X2_HOME" if row.hgoals > row.agoals else (
            "1X2_DRAW" if row.hgoals == row.agoals else "1X2_AWAY")
        nll -= log(max(1e-12, forecasts[observed_label]))
        for key, predicted in forecasts.items():
            brier[key] += (predicted - actual[key]) ** 2
            bin_key = f"{min(9, int(predicted * 10)) / 10:.1f}"
            buckets[key][bin_key].append((predicted, actual[key]))
    return {
        "n": len(rows), "score": nll / len(rows),
        "brier": {k: round(v / len(rows), 5) for k, v in brier.items()},
        "buckets": {k: {b: {
            "n": len(v), "forecastMean": round(mean([e[0] for e in v], 0), 4),
            "observedHitRate": round(mean([e[1] for e in v], 0), 4)
        } for b, v in groups.items()} for k, groups in buckets.items()}
    }


def walk_forward(data_root: Path) -> tuple[dict, list[dict], list[dict]]:
    matches, source_errors, leagues = read_fixtures(data_root)
    rows, readiness = make_rows(matches)
    rows.sort(key=lambda r: (r.date, r.league, r.fixture))
    unique_dates = sorted({r.date for r in rows})
    if len(unique_dates) < 50:
        return ({"contract": "betting-v2-goals-research-v1", "notCertified": True,
                 "error": "Fewer than 50 days of historical eligible fixtures",
                 "prematchRows": len(rows)}, [], [])
    # Chronological segments by calendar date, never random train/test split.
    def partition(frac: float) -> str:
        return unique_dates[min(len(unique_dates) - 1, int(len(unique_dates) * frac))]
    fit_end, tune_end, calibration_end = partition(0.55), partition(0.75), partition(0.90)
    tune = [r for r in rows if fit_end < r.date <= tune_end]
    calibration = [r for r in rows if tune_end < r.date <= calibration_end]
    holdout = [r for r in rows if r.date > calibration_end]
    combos = [Parameters(*p) for p in product(
        (0.25, 0.5), (0.35, 0.65), (0.5, 0.8), (0.0, 0.25, 0.50))]
    # No model weights are chosen from the holdout. A separate publisher must
    # additionally check actual dated bookmaker prices and independent ROI.
    scores = sorted(((measures(tune, p)["score"], p) for p in combos),
                    key=lambda row: row[0] if row[0] is not None else 1e6)
    best_nll, best = scores[0]
    def export_predictions(split: list[PrematchRow]) -> list[dict]:
        predicted = []
        for row in split:
            expected_home, expected_away = model_lambdas(row, best)
            predicted.append({
                "date": row.date, "league": row.league,
                "fixtureId": row.fixture, "homeGoals": row.hgoals,
                "awayGoals": row.agoals,
                "expectedHomeGoals": expected_home, "expectedAwayGoals": expected_away,
                "probabilities": event_probs(expected_home, expected_away),
                "observed": outcomes(row)
            })
        return predicted

    calibration_predictions = export_predictions(calibration)
    holdout_predictions = export_predictions(holdout)

    return ({
        "contract": "betting-v2-goals-research-v1",
        "notCertified": True,
        "doesNotProduceForecastArtifacts": True,
        "doesNotVerifyBettingValueOrROI": True,
        "dataSource": "StatMaker-Data/main domestic_enriched",
        "method": "strict prior-calendar-date features; walk-forward Elo/xG/goals/venue",
        "limits": [
            "Independent Poisson model is research baseline only",
            "No historic market odds joined; positive EV/ROI unverified",
            "No lineup or injury source yet; adverse scenario not verified",
            "No cross-market model certification, no selection permitted",
            "Calibration buckets describe error but do not guarantee conservative bounds"
        ],
        "sourceErrors": dict(source_errors),
        "leagues": len(leagues),
        "eligiblePrematchRows": len(rows),
        "readiness": readiness,
        "splits": {
            "training_history_end": fit_end,
            "tuning_end": tune_end,
            "calibration_end": calibration_end,
            "holdout_after": calibration_end,
            "tune_count": len(tune),
            "calibration_count": len(calibration),
            "holdout_count": len(holdout)
        },
        "tunedParams": asdict(best),
        "tune1X2LogLoss": round(best_nll, 6),
        "calibration": measures(calibration, best),
        "untouchedHoldout": measures(holdout, best)
    }, calibration_predictions, holdout_predictions)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-root", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--holdout-predictions", type=Path, default=None)
    ap.add_argument("--calibration-predictions", type=Path, default=None)
    args = ap.parse_args()
    report, calibration_predictions, holdout_predictions = walk_forward(args.data_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")
    if args.calibration_predictions is not None:
        args.calibration_predictions.parent.mkdir(parents=True, exist_ok=True)
        with args.calibration_predictions.open("w", encoding="utf-8") as out:
            for forecast in calibration_predictions:
                out.write(json.dumps(forecast, ensure_ascii=False) + "\n")
        print("Independent calibration probabilities:", args.calibration_predictions)
    if args.holdout_predictions is not None:
        args.holdout_predictions.parent.mkdir(parents=True, exist_ok=True)
        with args.holdout_predictions.open("w", encoding="utf-8") as out:
            for forecast in holdout_predictions:
                out.write(json.dumps(forecast, ensure_ascii=False) + "\n")
        print("Holdout model probabilities (RESEARCH ONLY):", args.holdout_predictions)
    print("V2 research written:", args.output)
    print("Certified picks produced: 0 (requires independent price/ROI and model audit)")


if __name__ == "__main__":
    main()
