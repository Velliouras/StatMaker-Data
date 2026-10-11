"""Chronological out-of-time evidence and frozen policy, computed only on explicit future runs."""
from __future__ import annotations

from collections import defaultdict
import math

from .production_contract import TIERS, digest, evidence, number, require
from .production_models import distribution, probability, unresolved_tail

UNCERTAINTY_METHOD = "chronological-fixture-block-error-adverse-drift-quality-tail-v1"


def observed_predictions(model: dict, cases: list[dict], plan: dict) -> list[dict]:
    result = []
    for case in cases:
        cells = distribution(model, case["features"], plan)
        adverse_cells = distribution(model, case["adverseFeatures"], plan)
        base = probability(case["offer"], cells)
        adverse = probability(case["offer"], adverse_cells)
        require(case["labelAvailableAtMs"] <= plan["evaluateAtMs"], "UNOBSERVED_HOLDOUT_LABEL")
        result.append({**case, "central": base, "adverseCentral": adverse,
                       "numericalTail": unresolved_tail(cells),
                       "adverseNumericalTail": unresolved_tail(adverse_cells)})
    return result


def block_win_interval(cases: list[dict], plan: dict, minimum: int) -> tuple[float, float, int]:
    import numpy as np
    fixtures = defaultdict(list)
    for case in cases:
        fixtures[(case["offer"]["competitionId"], case["offer"]["fixtureId"])].append(case)
    blocks = list(fixtures.values())
    require(len(blocks) >= minimum, "INSUFFICIENT_INDEPENDENT_CALIBRATION_FIXTURES")
    rng = np.random.default_rng(plan["bootstrapSeed"])
    samples = []
    for _ in range(plan["bootstrapReplicates"]):
        sample = [c for i in rng.integers(0, len(blocks), len(blocks)) for c in blocks[int(i)]]
        samples.append(sum(c["outcome"] == 1 for c in sample)/len(sample))
    return (float(np.quantile(samples, plan["confidenceAlpha"]/2)),
            float(np.quantile(samples, 1-plan["confidenceAlpha"]/2)), len(blocks))


def uncertainty_bins(cases: list[dict], plan: dict) -> list[dict]:
    """Freeze bins and error allowances BEFORE reading untouched holdout outcomes."""
    import numpy as np
    ordered = sorted(cases, key=lambda c: c["central"]["win"])
    require(len(ordered) >= plan["minimumCalibrationCases"], "INSUFFICIENT_CALIBRATION_CASES")
    bins = []
    for indices in np.array_split(np.arange(len(ordered)), int(plan["calibrationBinCount"])):
        require(len(indices) >= plan["minimumCalibrationBinCases"], "INSUFFICIENT_COMPARABLE_BIN")
        group = [ordered[int(i)] for i in indices]
        lower, upper, independent = block_win_interval(group, plan, plan["minimumIndependentCalibrationFixtures"])
        win_rate = sum(c["outcome"] == 1 for c in group)/len(group)
        p = sum(c["central"]["win"] for c in group)/len(group)
        chronological = sorted(group, key=lambda c: c["atMs"])
        period_groups = [chronological[:len(group)//2], chronological[len(group)//2:]]
        require(all(period_groups), "INSUFFICIENT_DRIFT_PERIODS")
        period_errors = [abs(sum(c["central"]["win"] for c in g)/len(g)
                             -sum(c["outcome"] == 1 for c in g)/len(g)) for g in period_groups]
        quality_groups = [[c for c in group if c["seasonTransition"]],
                          [c for c in group if not c["seasonTransition"]]]
        quality_errors = [max(0, sum(c["central"]["win"] for c in g)/len(g)
                              -sum(c["outcome"] == 1 for c in g)/len(g)) for g in quality_groups if g]
        components = {
            "outOfSampleModelError": max(0.0, p-win_rate),
            "sampleUncertainty": max(0.0, win_rate-lower),
            "drift": max(period_errors)-min(period_errors),
            "misspecification": max(period_errors)+max(c["numericalTail"] for c in group),
            "dataQuality": max(quality_errors),
        }
        bins.append({"from": group[0]["central"]["win"], "to": group[-1]["central"]["win"],
                     "cases": len(group), "independentFixtures": independent, "observedWin": win_rate, "winLower": lower,
                     "winUpper": upper, "calibrationReliability": 1-max(period_errors),
                     "uncertaintyComponents": components})
    # Calibration data, not a hand-chosen p>=70%/sample8 cutoff.
    edges = [0.0]+[(bins[i]["to"]+bins[i+1]["from"])/2 for i in range(len(bins)-1)]+[1.0]
    require(all(a < b for a, b in zip(edges, edges[1:])), "AMBIGUOUS_EMPIRICAL_PROBABILITY_BINS")
    for i, bin in enumerate(bins):
        bin["from"], bin["to"] = edges[i], edges[i+1]
    return bins


def tail_adjusted_bin(bin: dict, tail: float) -> dict:
    components = dict(bin["uncertaintyComponents"])
    components["misspecification"] += tail
    return dict(bin, uncertaintyComponents=components)


def conservative(central: dict, bin: dict) -> dict:
    margin = sum(bin["uncertaintyComponents"].values())
    win = max(0.0, central["win"]-margin)
    # A single mixture towards a loss scenario gives coherent WIN/PUSH/LOSE, not two bounds.
    retained = win/central["win"] if central["win"] > 0 else 0.0
    push = central["push"] * retained
    return {"win": win, "push": push, "lose": 1-win-push}


def bin_for(p: float, bins: list[dict]) -> dict:
    require(0 <= p <= 1, "INVALID_FORECAST_WIN")
    matches = [b for i, b in enumerate(bins) if b["from"] <= p
               and (p < b["to"] or i == len(bins)-1 and p <= b["to"])]
    require(len(matches) == 1, "UNCALIBRATED_OR_AMBIGUOUS_BIN")
    return matches[0]


def freeze_tiers(cases: list[dict], bins: list[dict], adverse_bins: list[dict], plan: dict) -> dict:
    import numpy as np
    eligible = []
    for case in cases:
        bin = tail_adjusted_bin(bin_for(case["central"]["win"], bins), case["numericalTail"])
        p = conservative(case["central"], bin)
        adverse = conservative(case["adverseCentral"], tail_adjusted_bin(
            bin_for(case["adverseCentral"]["win"], adverse_bins), case["adverseNumericalTail"]))
        odd = case["offer"]["odd"]
        if (1.8 < odd < 3 and p["win"] >= .6 and adverse["win"] >= .6
                and p["win"]*odd+p["push"] > 1 and adverse["win"]*odd+adverse["push"] > 1):
            eligible.append((p["win"], bin["calibrationReliability"],
                             min(1.0, 2*sum(bin["uncertaintyComponents"].values())), bin["cases"]))
    require(bool(eligible), "NO_CALIBRATION_SUPPORTED_STRONG_POLICY")
    quality = sorted(eligible, key=lambda v: (v[0], v[1], -v[2], v[3]))
    policy = {}
    for tier in TIERS:
        row = quality[int(np.floor(plan["tierQualityQuantiles"][tier]*(len(quality)-1)))]
        policy[tier] = {"minimumWin": row[0], "minimumReliability": row[1],
                        "maximumIntervalWidth": row[2], "minimumComparableCases": row[3]}
    return policy


def tier_for(p: dict, bin: dict, policy: dict, cases: int) -> str | None:
    for tier in TIERS:
        rule = policy[tier]
        if (p["win"] >= rule["minimumWin"] and bin["calibrationReliability"] >= rule["minimumReliability"]
                and min(1.0, 2*sum(bin["uncertaintyComponents"].values())) <= rule["maximumIntervalWidth"]
                and cases >= rule["minimumComparableCases"]):
            return tier
    return None


def signature(case: dict) -> tuple:
    offer = case["offer"]
    return offer["competitionId"], offer["fixtureId"], offer["selectionId"]


def event_correlation(left: dict, right: dict, cells: list[list[float]]) -> float:
    from .production_models import event_outcome
    lp, rp, joint = 0.0, 0.0, 0.0
    for h, row in enumerate(cells):
        for a, p in enumerate(row):
            l = float(event_outcome(left, h, a) == 1)
            r = float(event_outcome(right, h, a) == 1)
            lp += l*p
            rp += r*p
            joint += l*r*p
    variance = lp*(1-lp)*rp*(1-rp)
    return (joint-lp*rp)/math.sqrt(variance) if variance > 0 else 0.0


def scenario_groups(offers: list[dict], cells: list[list[float]], threshold: float) -> dict[str, str]:
    parent = list(range(len(offers)))
    def root(i):
        while parent[i] != i:
            i = parent[i]
        return i
    for i in range(len(offers)):
        for j in range(i):
            if event_correlation(offers[i], offers[j], cells) >= threshold:
                parent[root(i)] = root(j)
    members = defaultdict(list)
    for i, offer in enumerate(offers):
        members[root(i)].append(offer["selectionId"])
    return {offer["selectionId"]: digest(sorted(members[root(i)]))
            for i, offer in enumerate(offers)}


def evaluate_holdout(cases: list[dict], calibration: list[dict], bins: list[dict], adverse_bins: list[dict],
                     policy: dict, model: dict, plan: dict) -> dict:
    """No tuning/retry against holdout; entire frozen policy fails if a gate fails."""
    import numpy as np
    from scipy.stats import ks_2samp
    require(len(cases) >= plan["minimumHoldoutCases"], "INSUFFICIENT_UNTOUCHED_HOLDOUT")
    require(len({signature(c) for c in cases}) == len(cases), "DUPLICATE_HOLDOUT_QUOTE")
    candidates = []
    for case in cases:
        b = tail_adjusted_bin(bin_for(case["central"]["win"], bins), case["numericalTail"])
        p = conservative(case["central"], b)
        ap = conservative(case["adverseCentral"], tail_adjusted_bin(
            bin_for(case["adverseCentral"]["win"], adverse_bins), case["adverseNumericalTail"]))
        tier = tier_for(p, b, policy, len(cases))
        odd = case["offer"]["odd"]
        if tier and 1.8 < odd < 3 and p["win"] >= .6 and ap["win"] >= .6:
            ev = p["win"]*odd+p["push"]-1
            if ev > 0 and ap["win"]*odd+ap["push"]-1 > 0:
                candidates.append(dict(case, conservative=p, tier=tier, ev=ev))
    # Exactly the same correlation/selection path is evaluated as will be published.
    fixture_groups = defaultdict(list)
    for c in candidates:
        fixture_groups[(c["offer"]["competitionId"], c["offer"]["fixtureId"], c["atMs"])].append(c)
    selected = []
    for group in fixture_groups.values():
        cells = distribution(model, group[0]["features"], plan)
        groups = scenario_groups([c["offer"] for c in group], cells, plan["correlationThreshold"])
        distinct = {}
        for c in sorted(group, key=lambda c: (-c["conservative"]["win"], -bin_for(c["central"]["win"], bins)["calibrationReliability"],
                                           -c["ev"], c["offer"]["selectionId"])):
            distinct.setdefault(groups[c["offer"]["selectionId"]], c)
        selected.extend(distinct.values())
    require(len(selected) >= plan["minimumHoldoutStrongCases"], "INSUFFICIENT_PRICED_STRONG_HOLDOUT")
    fixture_returns = defaultdict(list)
    for c in selected:
        value = c["offer"]["odd"]-1 if c["outcome"] == 1 else 0.0 if c["outcome"] == 0 else -1.0
        fixture_returns[(c["offer"]["competitionId"], c["offer"]["fixtureId"])].append(value)
    blocks = list(fixture_returns.values())
    rng = np.random.default_rng(plan["bootstrapSeed"])
    require(plan["bootstrapReplicates"] > 0 and len(blocks) >= plan["minimumIndependentFixtures"],
            "INSUFFICIENT_INDEPENDENT_PROFITABILITY_BLOCKS")
    samples = []
    for _ in range(plan["bootstrapReplicates"]):
        sample = [v for index in rng.integers(0, len(blocks), len(blocks)) for v in blocks[int(index)]]
        samples.append(sum(sample)/len(sample))
    roi_lower = float(np.quantile(samples, plan["confidenceAlpha"]/2))
    require(roi_lower > 0, "NONPOSITIVE_INDEPENDENT_PROFITABILITY_BOUND")
    drift = ks_2samp([c["central"]["win"] for c in calibration], [c["central"]["win"] for c in cases])
    require(float(drift.pvalue) >= plan["confidenceAlpha"], "HOLDOUT_DRIFT_REVALIDATION_REQUIRED")
    def segments_for(pool, frozen_bins, central_key, tail_key):
        segments = []
        for bin in frozen_bins:
            group = [c for c in pool if bin_for(c[central_key]["win"], frozen_bins) is bin]
            require(len(group) >= plan["minimumHoldoutBinCases"], "UNSUPPORTED_HOLDOUT_COHORT_BIN")
            lower, upper, independent = block_win_interval(group, plan, plan["minimumIndependentHoldoutBinFixtures"])
            rate = sum(c["outcome"] == 1 for c in group)/len(group)
            center = sum(c[central_key]["win"] for c in group)/len(group)
            conservative_mean = sum(conservative(c[central_key], tail_adjusted_bin(bin, c[tail_key]))["win"]
                                    for c in group)/len(group)
            require(abs(center-rate) <= plan["maximumHoldoutCalibrationError"]
                    and conservative_mean <= lower, "HOLDOUT_CALIBRATION_OR_INTERVAL_COVERAGE_FAILURE")
            segments.append({"cases": len(group), "independentFixtures": independent,
                             "observedWin": rate, "centralWin": center,
                             "conservativeMeanWin": conservative_mean,
                             "observedWinInterval": [lower, upper], "absoluteError": abs(center-rate)})
        return segments
    segments = segments_for(cases, bins, "central", "numericalTail")
    adverse_segments = segments_for([c for c in cases if c["historicalAdverseRealized"]],
                                    adverse_bins, "adverseCentral", "adverseNumericalTail")
    brier = sum((c["central"]["win"]-float(c["outcome"] == 1))**2 for c in cases)/len(cases)
    baseline = sum(c["outcome"] == 1 for c in calibration)/len(calibration)
    baseline_brier = sum((baseline-float(c["outcome"] == 1))**2 for c in cases)/len(cases)
    # Price reference is decisive probability for push contracts, never bookmaker-derived model input.
    require(all("verifiedNoVigDecisiveWin" in c["quote"] for c in cases), "MISSING_INDEPENDENT_BOOKMAKER_BASELINE")
    decisive = [c for c in cases if c["outcome"] != 0]
    require(bool(decisive), "NO_DECISIVE_HOLDOUT_CASES")
    market_brier = sum((c["quote"]["verifiedNoVigDecisiveWin"]-float(c["outcome"] == 1))**2 for c in decisive)/len(decisive)
    model_decisive_brier = sum((c["central"]["win"]/(1-c["central"]["push"])-float(c["outcome"] == 1))**2 for c in decisive)/len(decisive)
    require(brier <= baseline_brier and model_decisive_brier <= market_brier, "NO_INDEPENDENT_BASELINE_IMPROVEMENT")
    equity = peak = drawdown = 0.0
    for c in sorted(selected, key=lambda c: c["atMs"]):
        equity += c["offer"]["odd"]-1 if c["outcome"] == 1 else 0 if c["outcome"] == 0 else -1
        peak = max(peak, equity)
        drawdown = max(drawdown, peak-equity)
    return {"settledCases": len(cases), "issuedStrongCases": len(selected), "independentFixtures": len(blocks),
            "unitStakeRoi": sum(sum(b) for b in blocks)/sum(len(b) for b in blocks),
            "unitStakeRoiLowerBound": roi_lower, "maximumUnitStakeDrawdown": drawdown,
            "expectedUnitStakeRoi": sum(c["ev"] for c in selected)/len(selected),
            "brier": brier, "baselineBrier": baseline_brier, "noVigBrier": market_brier,
            "modelDecisiveBrier": model_decisive_brier,
            "driftPValue": float(drift.pvalue), "errorBySegment": segments,
            "adverseErrorBySegment": adverse_segments,
            "priceRecords": [c["quote"] for c in cases], "revalidationRequired": False}
