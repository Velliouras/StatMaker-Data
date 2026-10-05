#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

import backtest_elo_betting_ab as ab

ROOT = ab.ROOT
REPORT_JSON = ROOT / "reports" / "elo_betting_weight_calibration.json"
REPORT_MD = ROOT / "reports" / "elo_betting_weight_calibration.md"
DEFAULT_WEIGHTS = (0.00, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40)


def hybrid_rank_with_weight(
    row: dict[str, Any],
    market_preferred: bool,
    three_way_result: bool,
    simulation_weight: float,
):
    ledger = ab.ledger
    odd = ledger.num(row.get("selection_odd"))
    market_probability = ledger.num(row.get("bm_market_probability"))
    posterior = ledger.num(row.get("bm_posterior_probability"))
    reliability = ledger._clamp01(ledger.num(row.get("bm_sample_reliability"), 0.0))
    if not (
        odd > 1.01
        and ledger._valid_probability(market_probability)
        and ledger._valid_probability(posterior)
    ):
        return None

    fixture_probability = ledger.num(row.get("opponent_model_probability"))
    model_backed = ledger._valid_probability(fixture_probability)
    base_probability = fixture_probability if model_backed else posterior
    if not ledger._valid_probability(base_probability):
        return None

    raw_simulation = ledger.nullable(row.get("simulation_probability"))
    if raw_simulation is not None and not ledger._valid_probability(raw_simulation):
        raw_simulation = None

    # 0% is a true base-only control: simulation neither changes probability nor gates the pick.
    simulation_probability = raw_simulation if simulation_weight > 0.0 else None
    probability = (
        base_probability * (1.0 - simulation_weight)
        + simulation_probability * simulation_weight
        if simulation_probability is not None
        else base_probability
    )

    edge = probability - market_probability
    expected_value = probability * odd - 1.0
    simulation_edge = (
        simulation_probability - market_probability
        if simulation_probability is not None
        else None
    )
    simulation_agreement = (
        1.0 - ledger._clamp01(abs(simulation_probability - base_probability) / 0.20)
        if simulation_probability is not None
        else None
    )
    legacy_support = ledger._clamp01(ledger.num(row.get("selection_score"), 0.0))
    hit_rate = ledger._clamp01(
        ledger.num(row.get("strict_hit_rate") or row.get("bm_hit_rate"), 0.0)
    )

    if odd > 4.50 or market_probability < 0.18:
        return None

    consensus = max(probability, market_probability) if market_preferred else probability
    if three_way_result and not market_preferred and odd > 2.75:
        if not (
            model_backed
            and probability >= (0.43 if odd > 3.50 else 0.40)
            and edge >= 0.02
            and reliability >= 0.50
        ):
            return None
    elif odd > 4.00:
        if not (probability >= 0.38 and reliability >= 0.55 and edge >= 0.02):
            return None
    elif odd > 3.50:
        if not (consensus >= 0.36 and reliability >= 0.50 and edge >= -0.01):
            return None
    elif odd > 3.00:
        if not (consensus >= 0.34 and reliability >= 0.45 and edge >= -0.03):
            return None
    elif consensus < 0.32:
        return None

    longshot_penalty = (
        0.18 if odd > 3.50 else
        0.08 if odd > 3.00 else
        0.03 if odd > 2.50 else
        0.0
    )

    mature_fallback = (
        reliability >= 0.70
        and legacy_support >= 0.70
        and hit_rate >= 0.65
    )
    simulation_accepts = (
        simulation_probability is None
        or (
            simulation_probability >= 0.50
            and ledger.num(simulation_edge, 0.0) >= -0.01
        )
    )
    if not (
        simulation_accepts
        and probability >= 0.56
        and reliability >= 0.58
        and legacy_support >= 0.60
        and hit_rate >= 0.60
        and edge >= 0.01
        and expected_value >= 0.015
        and longshot_penalty <= 0.08
        and (model_backed or mature_fallback)
    ):
        return None

    positive_edge = ledger._clamp01(max(edge, 0.0) / 0.12)
    positive_ev = ledger._clamp01(max(expected_value, 0.0) / 0.25)
    value_support = positive_edge * 0.45 + positive_ev * 0.55
    market_preference_penalty = 0.08 if three_way_result and not market_preferred else 0.0
    simulation_adjustment = 0.0
    if simulation_probability is not None:
        edge_signal = max(-1.0, min(1.0, ledger.num(simulation_edge, 0.0) / 0.10))
        agreement_signal = max(
            -1.0,
            min(1.0, ((ledger.num(simulation_agreement, 0.5) - 0.5) * 2.0)),
        )
        simulation_adjustment = edge_signal * 0.025 + agreement_signal * 0.015

    ranking = ledger._clamp01(
        consensus * 0.45
        + legacy_support * 0.30
        + hit_rate * 0.10
        + reliability * 0.10
        + value_support * 0.05
        + simulation_adjustment
        - longshot_penalty
        - market_preference_penalty
    )
    return (
        ranking,
        probability,
        legacy_support,
        reliability,
        -odd,
        base_probability,
        simulation_probability,
        simulation_edge,
        simulation_agreement,
    )


def evaluate_weights(
    db_path: Path,
    day: str,
    weights: tuple[float, ...],
) -> tuple[list[dict[str, Any]], dict[float, list[dict[str, Any]]], int]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    ledger = ab.ledger
    original_rank = ledger._hybrid_rank
    try:
        generation = ab.latest_generation(con)
        if generation is None:
            return [], {w: [] for w in weights}, 0
        gid, built = generation

        # Existing contract: legacy simulation at the unchanged 30% simulation weight.
        legacy = ab.candidate_records(con, gid, built, day)

        con.execute("BEGIN")
        overlay_rows = ab.apply_elo_overlay(con)
        results: dict[float, list[dict[str, Any]]] = {}
        for weight in weights:
            ledger._hybrid_rank = (
                lambda row, market_preferred, three_way_result, w=weight:
                hybrid_rank_with_weight(row, market_preferred, three_way_result, w)
            )
            results[weight] = ab.candidate_records(con, gid, built, day)
        con.rollback()
        return legacy, results, overlay_rows
    finally:
        ledger._hybrid_rank = original_rank
        con.close()


def subset(picks: list[dict[str, Any]], dates: set[str]) -> list[dict[str, Any]]:
    return [row for row in picks if str(row.get("date") or "") in dates]


def metric_or_floor(summary: dict[str, Any], key: str, floor: float = -999.0) -> float:
    value = summary.get(key)
    return float(value) if value is not None else floor


def choose_training_weight(
    summaries: dict[float, dict[str, Any]],
) -> float:
    max_graded = max((int(s.get("graded") or 0) for s in summaries.values()), default=0)
    eligible = [
        (weight, summary)
        for weight, summary in summaries.items()
        if int(summary.get("graded") or 0) >= max(1, int(max_graded * 0.90))
        and metric_or_floor(summary, "settlementCoverage", 0.0) >= 0.85
    ]
    if not eligible:
        eligible = list(summaries.items())
    eligible.sort(
        key=lambda pair: (
            metric_or_floor(pair[1], "hitRate"),
            metric_or_floor(pair[1], "roi"),
            -abs(pair[0] - 0.30),
        ),
        reverse=True,
    )
    return eligible[0][0]


def recommendation(
    chosen: float,
    training: dict[str, Any],
    holdout: dict[str, Any],
    current30_holdout: dict[str, Any],
    legacy_holdout: dict[str, Any],
) -> dict[str, Any]:
    def ge(a: Any, b: Any) -> bool:
        return a is not None and b is not None and float(a) >= float(b)

    enough = int(holdout.get("graded") or 0) >= 30
    improves_current = (
        ge(holdout.get("hitRate"), current30_holdout.get("hitRate"))
        and ge(holdout.get("roi"), current30_holdout.get("roi"))
    )
    improves_legacy = (
        ge(holdout.get("hitRate"), legacy_holdout.get("hitRate"))
        and ge(holdout.get("roi"), legacy_holdout.get("roi"))
    )
    promote = enough and improves_current and improves_legacy
    reason = (
        "holdout confirms both hit-rate and ROI improvement versus current 30% Elo and legacy baseline"
        if promote
        else "holdout does not confirm a robust improvement on both hit-rate and ROI"
    )
    return {
        "trainingSelectedWeight": chosen,
        "holdoutGraded": int(holdout.get("graded") or 0),
        "enoughHoldout": enough,
        "beatsCurrent30OnHitRateAndRoi": improves_current,
        "beatsLegacyOnHitRateAndRoi": improves_legacy,
        "promoteWeightChange": promote,
        "reason": reason,
    }


def pct(value: Any) -> str:
    return "-" if value is None else f"{float(value) * 100:.1f}%"


def dec(value: Any) -> str:
    return "-" if value is None else f"{float(value):.2f}"


def row_md(label: str, summary: dict[str, Any]) -> str:
    return (
        f"| {label} | {summary['picks']} | {summary['graded']} | "
        f"{summary['won']} | {summary['lost']} | {pct(summary['hitRate'])} | "
        f"{pct(summary['roi'])} | {dec(summary['averageOdds'])} |"
    )


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Elo Betting Weight Calibration",
        "",
        f"- Window: **{report['window']['from']} → {report['window']['to']}**",
        f"- Historical snapshot: **latest App-Ready manifest at/before {report['snapshotLocalHour']:02d}:00 Europe/Athens**",
        f"- Monte Carlo runs: **{report['simulationRuns']}** per processed day",
        f"- Processed days: **{report['processedDays']}** / {report['requestedDays']}",
        f"- Calibration days: **{len(report['split']['trainingDates'])}**",
        f"- Holdout days: **{len(report['split']['holdoutDates'])}**",
        "",
        "## Calibration window",
        "",
        "| Variant | Picks | Graded | Won | Lost | Hit rate | ROI | Avg odds |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
        row_md("Legacy simulation @30%", report["training"]["legacy"]),
    ]
    for key in report["weights"]:
        lines.append(row_md(f"Elo {int(round(float(key) * 100))}%", report["training"]["elo"][key]))

    lines.extend([
        "",
        "## Holdout window",
        "",
        "| Variant | Picks | Graded | Won | Lost | Hit rate | ROI | Avg odds |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
        row_md("Legacy simulation @30%", report["holdout"]["legacy"]),
    ])
    for key in report["weights"]:
        lines.append(row_md(f"Elo {int(round(float(key) * 100))}%", report["holdout"]["elo"][key]))

    rec = report["recommendation"]
    chosen_key = f"{float(rec['trainingSelectedWeight']):.2f}"
    lines.extend([
        "",
        "## Selection",
        "",
        f"- Training-best Elo weight: **{int(round(float(chosen_key) * 100))}%**",
        f"- Holdout result for that weight: **{pct(report['holdout']['elo'][chosen_key]['hitRate'])} hit rate**, **{pct(report['holdout']['elo'][chosen_key]['roi'])} ROI**",
        f"- Current 30% Elo holdout: **{pct(report['holdout']['elo']['0.30']['hitRate'])} hit rate**, **{pct(report['holdout']['elo']['0.30']['roi'])} ROI**",
        f"- Legacy holdout: **{pct(report['holdout']['legacy']['hitRate'])} hit rate**, **{pct(report['holdout']['legacy']['roi'])} ROI**",
        f"- Promote weight change: **{'YES' if rec['promoteWeightChange'] else 'NO'}**",
        f"- Reason: {rec['reason']}.",
        "",
        "## Guardrails",
        "",
        "- Strong thresholds, ranking coefficients, odd limits and one-pick-per-match logic are unchanged.",
        "- Only the base/simulation blend weight changes across Elo variants.",
        "- 0% Elo is a true base-only control: simulation is removed from both blend and simulation gate.",
        "- The weight is selected only on earlier calibration dates; the final seven processed dates are untouched holdout.",
        "- No Android or PROD Betting configuration is changed by this workflow.",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Calibrate Elo Betting simulation weight with temporal holdout")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--runs", type=int, default=10_000)
    parser.add_argument("--snapshot-local-hour", type=int, default=11)
    parser.add_argument("--holdout-days", type=int, default=7)
    args = parser.parse_args()

    days = max(1, min(30, args.days))
    runs_count = max(1_000, min(10_000, args.runs))
    snapshot_hour = max(0, min(23, args.snapshot_local_hour))
    holdout_days = max(3, min(10, args.holdout_days))
    weights = DEFAULT_WEIGHTS

    today = dt.datetime.now(dt.timezone.utc).astimezone(ab.ATHENS).date()
    requested_dates = [today - dt.timedelta(days=i) for i in range(1, days + 1)]
    settlement_index = ab.SettlementIndex()

    legacy_all: list[dict[str, Any]] = []
    elo_all: dict[float, list[dict[str, Any]]] = {w: [] for w in weights}
    skipped: list[dict[str, str]] = []
    processed: list[str] = []
    overlay_total = 0

    for target in reversed(requested_dates):
        day = target.isoformat()
        print(f"CALIBRATION_DAY start={day}", flush=True)
        commits = ab.manifest_commits_for_local_day(target)
        commit = ab.choose_snapshot_commit(target, commits, snapshot_hour)
        if not commit:
            skipped.append({"date": day, "reason": "NO_MANIFEST_COMMIT"})
            continue
        bundle_path = ab.bundle_path_at_commit(commit)
        if not bundle_path:
            skipped.append({"date": day, "reason": "NO_BETTING_BUNDLE"})
            continue

        with tempfile.TemporaryDirectory(prefix="statmaker-elo-cal-") as td:
            root = Path(td)
            db_path = root / "prepared.db"
            if not ab.extract_prepared_db(commit, bundle_path, db_path):
                skipped.append({"date": day, "reason": "BUNDLE_EXTRACT_FAILED"})
                continue
            if not ab.has_target_candidates(db_path, day):
                skipped.append({"date": day, "reason": "NO_TARGET_CANDIDATES"})
                continue

            ab.prune_db_to_day(db_path, day)
            work_root = root / "work"
            work_root.mkdir(parents=True, exist_ok=True)
            ok, error = ab.materialize_historical_simulations(
                commit, db_path, work_root, runs_count
            )
            if not ok:
                skipped.append({
                    "date": day,
                    "reason": "SIMULATION_FAILED",
                    "detail": error[:300],
                })
                continue

            legacy, by_weight, overlay_rows = evaluate_weights(db_path, day, weights)
            overlay_total += overlay_rows
            legacy = ab.settle_picks(legacy, settlement_index)
            legacy_all.extend(legacy)
            for weight in weights:
                settled = ab.settle_picks(by_weight[weight], settlement_index)
                elo_all[weight].extend(settled)
            processed.append(day)
            counts = ",".join(
                f"{int(w*100)}={len(by_weight[w])}" for w in weights
            )
            print(
                f"CALIBRATION_DAY done={day} legacy={len(legacy)} {counts}",
                flush=True,
            )

    if len(processed) <= holdout_days:
        raise SystemExit(
            f"Not enough processed dates for temporal holdout: processed={len(processed)} holdout={holdout_days}"
        )

    holdout_dates = set(processed[-holdout_days:])
    training_dates = set(processed[:-holdout_days])

    training_legacy = ab.summarize(subset(legacy_all, training_dates))
    holdout_legacy = ab.summarize(subset(legacy_all, holdout_dates))
    full_legacy = ab.summarize(legacy_all)

    training_elo = {
        w: ab.summarize(subset(elo_all[w], training_dates))
        for w in weights
    }
    holdout_elo = {
        w: ab.summarize(subset(elo_all[w], holdout_dates))
        for w in weights
    }
    full_elo = {
        w: ab.summarize(elo_all[w])
        for w in weights
    }

    chosen = choose_training_weight(training_elo)
    rec = recommendation(
        chosen,
        training_elo[chosen],
        holdout_elo[chosen],
        holdout_elo[0.30],
        holdout_legacy,
    )

    weight_keys = [f"{w:.2f}" for w in weights]
    report = {
        "schemaVersion": 1,
        "generatedAt": dt.datetime.now(dt.timezone.utc)
            .replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "requestedDays": days,
        "processedDays": len(processed),
        "processedDates": processed,
        "skippedDays": skipped,
        "window": {
            "from": min((d.isoformat() for d in requested_dates), default=""),
            "to": max((d.isoformat() for d in requested_dates), default=""),
        },
        "snapshotLocalHour": snapshot_hour,
        "simulationRuns": runs_count,
        "eloOverlayRowsApplied": overlay_total,
        "weights": weight_keys,
        "split": {
            "trainingDates": sorted(training_dates),
            "holdoutDates": sorted(holdout_dates),
        },
        "contract": {
            "thresholdsChanged": False,
            "rankingCoefficientsChanged": False,
            "onePickPerMatchChanged": False,
            "onlyVariable": "base/simulation probability blend weight",
            "legacyBaselineWeight": 0.30,
            "eloModel": "team-elo-v1",
            "eloSimulation": "match-monte-carlo-v2-elo",
        },
        "training": {
            "legacy": training_legacy,
            "elo": {f"{w:.2f}": training_elo[w] for w in weights},
        },
        "holdout": {
            "legacy": holdout_legacy,
            "elo": {f"{w:.2f}": holdout_elo[w] for w in weights},
        },
        "full": {
            "legacy": full_legacy,
            "elo": {f"{w:.2f}": full_elo[w] for w in weights},
        },
        "recommendation": rec,
    }

    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    REPORT_MD.write_text(render_markdown(report), encoding="utf-8")

    print("ELO_WEIGHT_CALIBRATION_OK")
    print(json.dumps({
        "trainingSelectedWeight": chosen,
        "recommendation": rec,
        "training": {f"{w:.2f}": training_elo[w] for w in weights},
        "holdout": {f"{w:.2f}": holdout_elo[w] for w in weights},
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
