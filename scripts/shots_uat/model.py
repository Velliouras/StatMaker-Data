"""CatBoost mean + conditional accuracy; coherent NB/binomial shot counts."""
import hashlib
import json
import math
from pathlib import Path
import numpy as np
from catboost import CatBoostRegressor, CatBoostClassifier, Pool
from .features import FEATURES

SEEDS = (17, 31, 71)
POLICY_ID = "SHOTS_SOT_P60_UAT_V4"
MIN_WIN_PROBABILITY = .60


def eligible_quote(win, push, odd):
    return (MIN_WIN_PROBABILITY <= win <= 1 and 0 <= push <= 1 and win + push <= 1.000001
            and 1.8 < odd < 3 and win * odd + push - 1 > 0)


def pool(x, y=None, weight=None):
    return Pool(x, label=y, weight=weight, cat_features=[len(FEATURES) - 1], feature_names=FEATURES)


def fit_bank(x, shots, sot, times, train_before, calibrate_before, output):
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    t = np.asarray(times); sh = np.asarray(shots); st = np.asarray(sot)
    train = np.flatnonzero(t + 86400 < train_before)
    calibration = np.flatnonzero((t >= train_before) & (t + 86400 < calibrate_before))
    if len(train) < 1000 or len(calibration) < 50:
        raise ValueError("Insufficient chronological training/calibration data")
    positive = train[sh[train] > 0]
    report = dict(schemaVersion=1, model="SHOTS_SOT_CATBOOST_V1", features=FEATURES,
                  trainBefore=train_before, calibrationBefore=calibrate_before,
                  trainingRows=len(train), calibrationRows=len(calibration), replicas=[])
    for seed in SEEDS:
        common = dict(iterations=220, depth=5, learning_rate=.045, l2_leaf_reg=5,
                      random_seed=seed, thread_count=2, verbose=False, allow_writing_files=False)
        shots_model = CatBoostRegressor(loss_function="Poisson", **common)
        shots_model.fit(pool([x[i] for i in train], sh[train]))
        # CrossEntropy permits fractional labels. Shot counts are binomial exposure weights.
        accuracy_model = CatBoostClassifier(loss_function="CrossEntropy", **common)
        accuracy_model.fit(pool([x[i] for i in positive], st[positive] / sh[positive], sh[positive]))
        cp = pool([x[i] for i in calibration])
        mu = np.exp(shots_model.predict(cp, prediction_type="RawFormulaVal"))
        accuracy = 1 / (1 + np.exp(-accuracy_model.predict(cp, prediction_type="RawFormulaVal")))
        mean_scale = float(np.clip(sh[calibration].sum() / mu.sum(), .75, 1.25))
        accuracy_scale = float(np.clip(st[calibration].sum() / np.maximum(1e-9, (sh[calibration] * accuracy).sum()), .75, 1.25))
        mu *= mean_scale
        alpha = float(np.clip(np.sum((sh[calibration] - mu) ** 2 - sh[calibration]) / np.sum(mu ** 2), .001, .5))
        files = []
        for name, model in (("shots", shots_model), ("accuracy", accuracy_model)):
            path = output / f"{name}-{seed}.json"
            model.save_model(str(path), format="json")
            files.append(dict(path=path.name, sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
        report["replicas"].append(dict(seed=seed, files=files, meanScale=mean_scale,
                                       accuracyScale=accuracy_scale, alpha=alpha))
    (output / "bank.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


class Bank:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.meta = json.loads((self.directory / "bank.json").read_text())
        if self.meta["features"] != FEATURES:
            raise ValueError("Feature contract changed")
        self.models = []
        for replica in self.meta["replicas"]:
            pair = []
            for index, f in enumerate(replica["files"]):
                path = self.directory / f["path"]
                if hashlib.sha256(path.read_bytes()).hexdigest() != f["sha256"]:
                    raise ValueError("Model hash mismatch")
                model = CatBoostRegressor() if index == 0 else CatBoostClassifier()
                model.load_model(str(path), format="json"); pair.append(model)
            self.models.append((replica, *pair))

    def predict(self, features):
        result = []
        p = pool(features)
        for config, shots, accuracy in self.models:
            mu = np.exp(shots.predict(p, prediction_type="RawFormulaVal")) * config["meanScale"]
            ratio = np.clip(1 / (1 + np.exp(-accuracy.predict(p, prediction_type="RawFormulaVal"))) * config["accuracyScale"], .05, .8)
            result.append(dict(shots=mu.tolist(), sot=(mu * ratio).tolist(), alpha=config["alpha"]))
        return result


def pmf(mean, alpha, limit=240):
    """NB2; binomial thinning preserves the same alpha for SOT."""
    if not 0 < mean < 120 or not .0001 <= alpha <= 1:
        raise ValueError("Invalid count distribution")
    shape = 1 / alpha; p = shape / (shape + mean)
    values = [math.exp(shape * math.log(p))]
    for k in range(1, limit + 1):
        values.append(values[-1] * (k - 1 + shape) / k * (1 - p))
    if sum(values) < 1 - 1e-8:
        raise ValueError("Count support truncation too large")
    return np.asarray(values)


def distribution(replica, metric, scope, team):
    h, a = [pmf(v, replica["alpha"]) for v in replica[metric]]
    return np.convolve(h, a) if scope == "TOTAL" else h if team == "HOME" else a


def probabilities(values, side, line):
    if side not in ("OVER", "UNDER") or not math.isfinite(line) or line < 0 or (line * 2) % 1:
        raise ValueError("Only integer/half-line counts are supported")
    n = np.arange(len(values))
    win = float(values[n > line].sum() if side == "OVER" else values[n < line].sum())
    push = float(values[n == line].sum())
    return win, push


def conservative(replicas, metric, scope, team, side, line):
    values = [probabilities(distribution(r, metric, scope, team), side, line) for r in replicas]
    # Sensitivity lower envelope, not a confidence interval or empirical hit rate.
    return min(v[0] for v in values), min(v[1] for v in values)


def favourite_guard(features, replicas, favourite, metric, scope, team, side):
    # A+B Over/Under does not assert which team wins or has more shots.
    # Directional favourite safeguards apply to team props only.
    if scope == "TOTAL":
        return True
    if scope != "TEAM":
        return False
    if favourite not in ("HOME", "AWAY"):
        return False
    index = 0 if favourite == "HOME" else 1
    if features[index][1] <= 0:
        return False
    # Require both ELO agreement and >=60% shot-count dominance across all replicas.
    for r in replicas:
        h, a = [pmf(v, r["alpha"]) for v in r[metric]]
        dominance = float(np.dot(h, np.r_[0., np.cumsum(a)[:-1]]))
        if favourite == "AWAY":
            dominance = float(np.dot(a, np.r_[0., np.cumsum(h)[:-1]]))
        if dominance < .6:
            return False
    # A team prop cannot oppose the market favourite's attacking direction.
    return (team == favourite) == (side == "OVER")
