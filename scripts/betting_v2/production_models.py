"""Matchup-first fitted count models. No bookmaker prices enter model features or fitting."""
from __future__ import annotations

from collections import defaultdict
import math

from .production_contract import Blocked, digest, number, require
from .production_context import bound_value, verify_receipt


def context_values(context: dict | None, target: dict, at_ms: int, columns: list[str]) -> list[float]:
    """Optional verified injury features, with explicit missing-observation flags.

    Missing information is NOT assumed to mean a fully available squad.
    Every fixture uses the same feature schema: count plus observed flag for
    each requested team, regardless of whether the provider replied.
    """
    if not columns:
        return []
    require(columns == ["homeUnavailablePlayers", "awayUnavailablePlayers"],
            "UNKNOWN_OPTIONAL_CONTEXT_FEATURE_SCHEMA")
    missing = [0.0, 0.0, 0.0, 0.0]
    if context is None or not (0 < context.get("observedAtMs", 0) <= at_ms
                               < context.get("validUntilMs", 0)):
        return missing
    require(context.get("fixtureId") == str(target["fixture_id"]), "CONTEXT_FIXTURE_MISMATCH")
    record = context["providerRecord"]
    require(digest(record) == context["providerRecordSha256"], "CONTEXT_HASH_MISMATCH")
    require(record["fixtureId"] == str(target["fixture_id"])
            and record["homeTeamId"] == str(target["home_team_id"])
            and record["awayTeamId"] == str(target["away_team_id"]), "CONTEXT_TEAM_BINDING_MISMATCH")
    verify_receipt(record["receipt"])
    require(context["observedAtMs"] == record["receipt"]["clientReceivedAtMs"],
            "INVENTED_CONTEXT_TIME")
    require(context["scenarioKind"] == "BASE", "UNKNOWN_CONTEXT_SCENARIO")
    result = []
    for name in columns:
        binding = context["fieldBindings"][name]
        require(binding["types"] == ["Missing Fixture"], "WRONG_CONTEXT_STATUS_SEMANTICS")
        count = number(context["values"][name])
        require(count == bound_value(record, binding), "UNBOUND_CONTEXT_VALUE")
        result.extend((count, 1.0))  # 1 = verified receipt was available
    return result


def features(history: list[dict], target: dict, at_ms: int, family: str, mode: str,
             parameters: dict, context: dict, context_columns: list[str]) -> tuple[list[float], list[dict], list[dict]]:
    """Strength, opponent-adjusted attack/defence, form, venue and season coverage."""
    require(at_ms < target["kickoffEpochMs"], "FEATURES_AFTER_KICKOFF")
    eligible = sorted((r for r in history if r["leagueCode"] == target["leagueCode"]
                       and r["kickoffEpochMs"] < at_ms and r["availableAtMs"] <= at_ms),
                      key=lambda r: (r["kickoffEpochMs"], str(r["fixture_id"])))
    ratings = defaultdict(float)  # Equal unobserved prior is not a verified ELO feature.
    seen = defaultdict(int)
    appearances = defaultdict(list)
    scale, k = number(parameters["eloScale"]), number(parameters["eloK"])
    require(scale > 0 and k > 0, "INVALID_ELO_SEARCH_PARAMETER")
    for row in eligible:
        h, a = str(row["home_team_id"]), str(row["away_team_id"])
        rh, ra = ratings[h], ratings[a]
        expected = 1 / (1 + 10 ** max(-300, min(300, (ra - rh) / scale)))
        hg, ag = row["goals"]
        actual = 1.0 if hg > ag else 0.0 if hg < ag else 0.5
        for team, opponent_rating, side in ((h, ra, 0), (a, rh, 1)):
            appearances[team].append((row, side, opponent_rating))
        delta = k * (actual - expected)
        ratings[h] += delta
        ratings[a] -= delta
        seen[h] += 1
        seen[a] += 1
    home, away = str(target["home_team_id"]), str(target["away_team_id"])
    require(seen[home] >= parameters["minimumHistory"] and seen[away] >= parameters["minimumHistory"],
            "INSUFFICIENT_REAL_ELO_HISTORY")
    values, used = [ratings[home] / scale, ratings[away] / scale], {}
    fallback_missing_xg = False
    for team, side in ((home, 0), (away, 1)):
        history_rows = appearances[team]
        for window_name in ("recentWindow", "longWindow", "venueWindow"):
            pool = history_rows if window_name != "venueWindow" else [v for v in history_rows if v[1] == side]
            pool = pool[-parameters[window_name]:]
            require(len(pool) >= parameters["minimumHistory"], "INCOMPLETE_FORM_OR_VENUE_WINDOW")
            require(all(family in r["counts"] for r, _, _ in pool), "MISSING_MARKET_SPECIFIC_HISTORY")
            if mode == "XG_PRIMARY":
                require(all("xg" in r for r, _, _ in pool), "MISSING_REAL_XG_XGA")
            elif mode == "ELO_GOALS_FALLBACK_NO_XG":
                # The fallback requires INSUFFICIENT historical xG coverage;
                # it does not require xG to be absent from every prior match.
                # Existing observed xG values are simply not model features.
                incomplete = [r for r, _, _ in pool if "xg" not in r]
                require(all(r.get("providerXgUnavailable") is True for r in incomplete),
                        "UNVERIFIED_MISSING_PROVIDER_XG")
                if incomplete:
                    fallback_missing_xg = True
            else:
                require(False, "UNKNOWN_EVIDENCE_MODE")
            scored = [r["counts"][family][s] for r, s, _ in pool]
            conceded = [r["counts"][family][1-s] for r, s, _ in pool]
            goals_for = [r["goals"][s] for r, s, _ in pool]
            goals_against = [r["goals"][1-s] for r, s, _ in pool]
            mean = lambda v: sum(v) / len(v)
            values.extend((mean(scored), mean(conceded), mean(goals_for), mean(goals_against),
                           mean([opponent / scale for _, _, opponent in pool]),
                           len(pool), mean([float(r["season"] != target["season"]) for r, _, _ in pool])))
            if mode == "XG_PRIMARY":
                values.extend((mean([r["xg"][s] for r, s, _ in pool]),
                               mean([r["xg"][1-s] for r, s, _ in pool])))
            for r, _, _ in pool:
                used[str(r["fixture_id"])] = r
    if mode == "ELO_GOALS_FALLBACK_NO_XG":
        require(fallback_missing_xg, "XG_READY_USE_PRIMARY_MODEL")
    values.extend(context_values(context, target, at_ms, context_columns))
    return values, list(used.values()), eligible


def fit_count_model(rows: list[dict], parameters: dict, columns: list[str]) -> dict:
    from sklearn.linear_model import PoissonRegressor
    from sklearn.preprocessing import StandardScaler
    import numpy as np
    require(bool(rows), "NO_TRAINING_ROWS")
    x = np.asarray([r["features"] for r in rows], dtype=float)
    y = np.asarray([r["target"] for r in rows], dtype=float)
    require(np.isfinite(x).all() and np.isfinite(y).all() and (y >= 0).all()
            and np.equal(np.floor(y), y).all(), "NON_COUNT_TRAINING_TARGET")
    scale = StandardScaler().fit(x)
    xs = scale.transform(x)
    fits = [PoissonRegressor(alpha=number(parameters["regularization"]),
                            max_iter=int(parameters["maximumFitIterations"])).fit(xs, y[:, side])
            for side in (0, 1)]
    residual = y - np.column_stack([model.predict(xs) for model in fits])
    variance = residual.var(axis=0)
    mean_y = y.mean(axis=0)
    dispersion = np.maximum(0, (variance - mean_y) / np.maximum(mean_y ** 2, np.finfo(float).eps))
    correlation = float(np.corrcoef(residual.T)[0, 1]) if np.all(variance > 0) else 0.0
    require(math.isfinite(correlation), "INVALID_FITTED_COUNT_CORRELATION")
    return {"parameters": parameters, "contextColumns": columns,
            "mean": scale.mean_.tolist(), "scale": scale.scale_.tolist(),
            "coefficients": [m.coef_.tolist() for m in fits],
            "intercepts": [float(m.intercept_) for m in fits],
            "dispersion": dispersion.tolist(), "correlation": correlation,
            "rateMultiplier": [1.0, 1.0]}


def rates(model: dict, x: list[float]) -> list[float]:
    scaled = [(v-m)/s for v, m, s in zip(x, model["mean"], model["scale"])]
    require(len(x) == len(model["mean"]), "FEATURE_SCHEMA_MISMATCH")
    result = []
    for side in (0, 1):
        predictor = model["intercepts"][side] + sum(a*b for a, b in zip(scaled, model["coefficients"][side]))
        require(math.isfinite(predictor) and predictor < 700, "UNUSABLE_COUNT_RATE")
        result.append(math.exp(predictor) * model["rateMultiplier"][side])
    return result


def distribution(model: dict, x: list[float], plan: dict) -> list[list[float]]:
    """NB/Poisson marginals + fitted Gaussian copula; common score/count basis."""
    import numpy as np
    from scipy.integrate import quad
    from scipy.stats import norm, nbinom, poisson
    from functools import lru_cache
    marginals = []
    for mean, alpha in zip(rates(model, x), model["dispersion"]):
        rv = nbinom(1/alpha, 1/(1+alpha*mean)) if alpha > 0 else poisson(mean)
        maximum = int(rv.ppf(1-plan["tailMassTolerance"]))
        require(0 <= maximum <= plan["maximumDistributionCount"], "UNRESOLVED_DISTRIBUTION_TAIL")
        cdf = np.asarray([0.0] + [float(rv.cdf(v)) for v in range(maximum+1)] + [1.0])
        marginals.append(cdf)
    # Rectangle integration, no Monte Carlo certification or random simulated counts.
    rho = model["correlation"]
    require(-1 < rho < 1, "DEGENERATE_COUNT_CORRELATION")
    @lru_cache(maxsize=None)
    def copula(u, v):
        if u <= 0 or v <= 0:
            return 0.0
        if u >= 1:
            return float(v)
        if v >= 1:
            return float(u)
        upper, other = float(norm.ppf(u)), float(norm.ppf(v))
        return float(quad(lambda t: norm.pdf(t)*norm.cdf((other-rho*t)/math.sqrt(1-rho*rho)),
                          -math.inf, upper, epsabs=1e-11)[0])
    h, a = marginals
    cells = [[max(0.0, copula(h[i+1], a[j+1])-copula(h[i], a[j+1])
                  -copula(h[i+1], a[j])+copula(h[i], a[j]))
              for j in range(len(a)-1)] for i in range(len(h)-1)]
    # Last row/column are explicit overflow, never silently renormalized into plausible scores.
    tail = sum(cells[-1]) + sum(row[-1] for row in cells[:-1])
    require(tail <= plan["tailMassTolerance"] * 2 + 1e-9, "UNRESOLVED_JOINT_TAIL")
    require(abs(sum(map(sum, cells))-1) <= 1e-6, "INCOHERENT_COUNT_DISTRIBUTION")
    return cells


def event_outcome(row: dict, home: int, away: int) -> int:
    """-1 loss, 0 refund, 1 win. Integer lines include PUSH."""
    side, market = row["side"], row["marketModelKey"]
    if market in ("RESULT_1X2", "HT_RESULT_1X2"):
        return 1 if side == ("HOME" if home > away else "AWAY" if away > home else "DRAW") else -1
    if market in ("RESULT_DOUBLE_CHANCE", "HT_RESULT_DOUBLE_CHANCE"):
        won = {"HOME_OR_DRAW": home >= away, "AWAY_OR_DRAW": away >= home,
               "HOME_OR_AWAY": home != away}
        require(side in won, "UNKNOWN_DC_SIDE")
        return 1 if won[side] else -1
    if market == "RESULT_DNB":
        require(side in ("HOME", "AWAY"), "UNKNOWN_DNB_SIDE")
        return 0 if home == away else 1 if (home > away) == (side == "HOME") else -1
    if market == "BTTS":
        require(side in ("YES", "NO"), "UNKNOWN_BTTS_SIDE")
        return 1 if (home > 0 and away > 0) == (side == "YES") else -1
    require(side in ("OVER", "UNDER"), "UNKNOWN_COUNT_SIDE")
    count = home if row.get("teamSide") == "HOME" else away if row.get("teamSide") == "AWAY" else home+away
    line = number(row["line"])
    return 0 if count == line else 1 if (count > line) == (side == "OVER") else -1


def probability(row: dict, cells: list[list[float]]) -> dict:
    values = {"win": 0.0, "push": 0.0, "lose": 0.0}
    # Overflow cannot be resolved to an exact score; block lines that reach the tail cutoff.
    if row.get("line") is not None:
        require(row["line"] < min(len(cells)-1, len(cells[0])-1), "LINE_REACHES_UNRESOLVED_TAIL")
    for h, probs in enumerate(cells):
        for a, p in enumerate(probs):
            outcome = event_outcome(row, h, a)
            values[{1: "win", 0: "push", -1: "lose"}[outcome]] += p
    require(abs(sum(values.values())-1) <= 1e-6, "INCOHERENT_SETTLEMENT_DISTRIBUTION")
    return values


def unresolved_tail(cells: list[list[float]]) -> float:
    return sum(cells[-1])+sum(row[-1] for row in cells[:-1])


def calibrate_distribution(model: dict, rows: list[dict], plan: dict) -> dict:
    from scipy.optimize import minimize
    import numpy as np
    require(bool(rows), "NO_CHRONOLOGICAL_CALIBRATION")
    def objective(log_scales):
        trial = dict(model, rateMultiplier=np.exp(log_scales).tolist())
        losses = []
        for row in rows:
            cells = distribution(trial, row["features"], plan)
            h, a = row["target"]
            require(h < len(cells)-1 and a < len(cells[0])-1, "CALIBRATION_COUNT_IN_TAIL")
            losses.append(-math.log(max(np.finfo(float).tiny, cells[int(h)][int(a)])))
        return sum(losses)/len(losses)
    fit = minimize(objective, [0.0, 0.0], bounds=plan["calibrationLogScaleBounds"],
                   method="L-BFGS-B")
    require(bool(fit.success) and np.isfinite(fit.fun), "DISTRIBUTION_CALIBRATION_FAILED")
    return dict(model, rateMultiplier=np.exp(fit.x).tolist())


def select_parameters(rows_by_parameters: list[tuple[dict, list[dict]]], plan: dict) -> tuple[dict, list[dict]]:
    """Model/window/strength parameters selected ONLY using earlier walk-forward folds."""
    best = None
    for params, rows in rows_by_parameters:
        losses = []
        for fold in plan["walkForwardFolds"]:
            train = [r for r in rows if fold["fitStartMs"] <= r["atMs"] <= fold["fitEndMs"]
                     and r["labelAvailableAtMs"] <= fold["fitEndMs"]]
            test = [r for r in rows if fold["testStartMs"] <= r["atMs"] <= fold["testEndMs"]
                    and r["labelAvailableAtMs"] <= fold["testEndMs"]]
            if len(train) < plan["minimumTrainCases"] or not test:
                continue
            model = fit_count_model(train, params, plan["contextColumns"])
            for r in test:
                cells = distribution(model, r["features"], plan)
                h, a = map(int, r["target"])
                if h >= len(cells)-1 or a >= len(cells[0])-1:
                    losses.append(float("inf"))
                else:
                    losses.append(-math.log(max(1e-300, cells[h][a])))
        if losses:
            score = sum(losses)/len(losses)
            if math.isfinite(score) and (best is None or score < best[0]):
                best = score, params, rows
    require(best is not None, "NO_SUCCESSFUL_INDEPENDENT_MODEL_SELECTION_FOLD")
    return best[1], best[2]
