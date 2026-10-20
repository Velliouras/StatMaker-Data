"""Chronological numerical challenger; exact cached prices, never a ledger writer."""
import argparse
import gzip
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from catboost import CatBoostClassifier, Pool
from scipy.optimize import minimize
from scipy.special import expit, logit

from .features import FEATURES, History, counts, training_frame
from .feed import load_history, write
from .model import Bank, distribution, favourite_guard, fit_bank, probabilities
from .train import ROOT, HISTORY, timestamp

OUT = ROOT / "reports/shots_uat/market_context_20261020"
BOUNDARIES = [timestamp(d) for d in ("2026-08-01", "2026-09-01", "2026-10-01", "2026-11-01")]
GROUPS = [(m, s) for m in ("SHOTS", "SOT") for s in ("TOTAL", "TEAM")]
SEEDS = (17, 31, 71)
MODELS = ("count_lower", "count_mean", "market", "linear", "context_mean", "context_lower")
EXTRA = ["market_logit", "count_market_gap", "line", "overround", "lead_days",
         "count_mean", "count_relative_line", "replica_range"]
CONTEXT_FEATURES = EXTRA + [p + f for p in ("home_", "away_") for f in FEATURES[:-1]] + ["metric", "scope", "team", "league"]
CATS = list(range(len(CONTEXT_FEATURES) - 4, len(CONTEXT_FEATURES)))


def log(message, **fields):
    print(message, json.dumps(fields, ensure_ascii=False), flush=True)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def safe_logit(p):
    return logit(np.clip(p, 1e-6, 1 - 1e-6))


def dump_gz(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(json.dumps(value, ensure_ascii=False, allow_nan=True, separators=(",", ":")).encode(), mtime=0))


def banks(exact_directory):
    result = {BOUNDARIES[2]: Bank(exact_directory)}
    assert result[BOUNDARIES[2]].meta["calibrationBefore"] == BOUNDARIES[2]
    missing = [(BOUNDARIES[0], "2026-07-01"), (BOUNDARIES[1], "2026-08-01")]
    x = None
    for before, train_day in missing:
        directory = OUT / "oof_banks" / datetime.fromtimestamp(before, timezone.utc).strftime("%Y%m%d")
        if not (directory / "bank.json").exists():
            if x is None:
                x, sh, st, times = training_frame(load_history(False))
                log("OOF_FEATURES", fixtures=len(times) // 2)
            meta = fit_bank(x, sh, st, times, timestamp(train_day), before, directory)
            meta.update(historySha256=sha(HISTORY), outOfFold=True)
            write(directory / "bank.json", meta)
        result[before] = Bank(directory)
        log("OOF_BANK", before=before, training=result[before].meta["trainingRows"] // 2,
            calibration=result[before].meta["calibrationRows"] // 2, sha256=sha(directory / "bank.json"))
    return result


def make_frame(variant, model_banks):
    source = ROOT / f"reports/betting_v2/market_first_counts_20261018/quotes_{variant}.json.gz"
    cache = OUT / f"frame_{variant}.json.gz"
    inputs = dict(quotes=sha(source), history=sha(HISTORY),
                  banks={str(k): sha(v.directory / "bank.json") for k, v in model_banks.items()})
    if cache.exists():
        doc = json.loads(gzip.decompress(cache.read_bytes()))
        if doc["inputs"] == inputs:
            log("FRAME_CACHED", variant=variant, rows=len(doc["rows"]), audit=doc["audit"])
            return doc
    raw = json.loads(gzip.decompress(source.read_bytes()))
    audit = Counter(); grouped = defaultdict(list)
    for q in raw:
        metric, scope, side, team, line = q["spec"]
        if metric not in ("SHOTS", "SOT") or scope not in ("TOTAL", "TEAM"):
            continue
        audit["rawSideQuotes"] += 1
        assert q["featureCutoff"] <= q["observed"] < q["kickoff"]
        if line < 0 or (line * 2) % 1:
            audit["unsupportedLine"] += 1; continue
        grouped[(q["fixture"], metric, scope, team, line, q["bookmaker"], q["sourceReceipt"])].append(q)
    pairs = []
    history_rows = load_history(False)
    actual = {(int(r["_date"]), r["_home"], r["_away"]): r for r in history_rows}
    for group in grouped.values():
        side = {q["spec"][2]: q for q in group}
        if set(side) != {"OVER", "UNDER"} or len(group) != 2:
            audit["unpairedOrAmbiguous"] += 1; continue
        o, u = side["OVER"], side["UNDER"]
        assert all(o[k] == u[k] for k in ("observed", "featureCutoff", "kickoff", "_home", "_away"))
        if not all(1 < q["odd"] < 1000 for q in (o, u)):
            audit["invalidPrice"] += 1; continue
        r = actual.get((int(o["kickoff"]), o["_home"], o["_away"]))
        if r is None or counts(r) is None:
            audit["missingActual"] += 1; continue
        pairs.append((o, u, r))
    keys = sorted(set((o["featureCutoff"], o["_home"], o["_away"], o["kickoff"]) for o, _, _ in pairs))
    history = History(history_rows); features = defaultdict(list); feature_index = {}; replicas = {}
    for key in keys:
        cutoff, h, a, ko = key; history.advance(cutoff)
        row = actual[int(ko), h, a]
        pair = history.pair(h, a, row.get("_home_group", "unknown"), ko)
        if min(pair[0][4:6]) < 8:
            continue
        before = max(b for b in model_banks if b <= cutoff)
        assert model_banks[before].meta["calibrationBefore"] <= cutoff < ko
        i = len(features[before]); features[before].extend(pair)
        feature_index[key] = before, i, pair
    for before, xs in features.items():
        batch = model_banks[before].predict(xs)
        for key, (b, i, pair) in feature_index.items():
            if b == before:
                replicas[key] = [dict(shots=r["shots"][i:i+2], sot=r["sot"][i:i+2], alpha=r["alpha"]) for r in batch]
    forecast_cache = {}; guard_cache = {}; rows = []
    for o, u, result in pairs:
        key = o["featureCutoff"], o["_home"], o["_away"], o["kickoff"]
        if key not in feature_index:
            audit["insufficientHistory"] += 1; continue
        metric, scope, _, team, line = o["spec"]
        m = "shots" if metric == "SHOTS" else "sot"
        before, _, pair = feature_index[key]; reps = replicas[key]
        fkey = key, metric, scope, team, line
        if fkey not in forecast_cache:
            probs = [probabilities(distribution(r, m, scope, team), "OVER", line) for r in reps]
            forecast_cache[fkey] = probs
        ps = forecast_cache[fkey]; push = float(np.mean([p[1] for p in ps]))
        over_ps = np.asarray([p[0] / (1 - p[1]) for p in ps])
        market = (1 / o["odd"]) / (1 / o["odd"] + 1 / u["odd"])
        mean = float(np.mean(over_ps))
        predicted_count = float(np.mean([sum(r[m]) if scope == "TOTAL" else r[m][0 if team == "HOME" else 1] for r in reps]))
        sh, ass, st, at = counts(result)
        h, a = (sh, ass) if metric == "SHOTS" else (st, at)
        value = h + a if scope == "TOTAL" else h if team == "HOME" else a
        label = None if value == line else int(value > line)
        x = [float(safe_logit(market)), float(safe_logit(mean) - safe_logit(market)), line,
             1 / o["odd"] + 1 / u["odd"] - 1, (o["kickoff"] - o["observed"]) / 86400,
             predicted_count, (line - predicted_count) / max(1, np.sqrt(predicted_count)),
             float(np.max(over_ps) - np.min(over_ps))] + pair[0][:-1] + pair[1][:-1] + [metric, scope, team or "TOTAL", pair[0][-1]]
        assert len(x) == len(CONTEXT_FEATURES)
        guards = {}
        for direction in ("OVER", "UNDER"):
            gkey = key, metric, scope, team, direction, o.get("_favourite")
            if gkey not in guard_cache:
                guard_cache[gkey] = favourite_guard(pair, reps, o.get("_favourite"), m, scope, team, direction)
            guards[direction] = guard_cache[gkey]
        rows.append(dict(fixture=o["fixture"], metric=metric, scope=scope, team=team, line=line,
                         kickoff=o["kickoff"], featureCutoff=o["featureCutoff"], observed=o["observed"],
                         bookmaker=o["bookmaker"], sourceReceipt=o["sourceReceipt"], sourceSha256=o["sourceSha256"],
                         odds={"OVER":o["odd"], "UNDER":u["odd"]}, market=market,
                         count_mean=mean, count_replicas=over_ps.tolist(), push=push, label=label, actual=value,
                         guards=guards, x=x, group=GROUPS.index((metric, scope)), baseBefore=before))
    audit["pairedRows"] = len(rows); audit["fixtures"] = len(set(r["fixture"] for r in rows))
    doc = dict(inputs=inputs, audit=dict(audit), features=CONTEXT_FEATURES, rows=rows)
    dump_gz(cache, doc); log("FRAME_BUILT", variant=variant, audit=audit)
    return doc


def fixture_weights(rows, normalize=True):
    sizes = Counter(r["fixture"] for r in rows)
    w = np.asarray([1 / sizes[r["fixture"]] for r in rows])
    return w / w.sum() if normalize else w * len(rows) / w.sum()


def validate_training(rows, cutoff):
    if not rows or any(r["label"] not in (0, 1) or r["kickoff"] + 86400 >= cutoff
                       or not r["baseBefore"] <= r["featureCutoff"] <= r["observed"] < r["kickoff"] for r in rows):
        raise ValueError("Challenger training violates chronological information boundary")


def fit_linear(rows, cutoff):
    validate_training(rows, cutoff)
    y = np.asarray([r["label"] for r in rows]); w = fixture_weights(rows)
    offset = safe_logit([r["market"] for r in rows])
    x = np.zeros((len(rows), 5))
    x[:, 0] = safe_logit([r["count_mean"] for r in rows]) - offset
    x[np.arange(len(rows)), 1 + np.asarray([r["group"] for r in rows])] = 1
    def objective(theta):
        z = offset + x @ theta
        return float(w @ (np.logaddexp(0, z) - y * z) + .01 * (theta @ theta) / 2), x.T @ (w * (expit(z) - y)) + .01 * theta
    fit = minimize(objective, np.zeros(5), jac=True, method="L-BFGS-B", bounds=[(0, 1)] + [(-2, 2)] * 4,
                   options=dict(ftol=1e-13, gtol=1e-9, maxiter=2000))
    if not fit.success:
        raise ValueError(f"Linear fit failed: {fit.message}")
    return dict(parameters=fit.x.tolist(), objective=float(fit.fun), gradient=fit.jac.tolist(), trainingRows=len(rows),
                trainingFixtures=len(set(r["fixture"] for r in rows)))


def fit_context(rows, directory, cutoff):
    validate_training(rows, cutoff)
    directory.mkdir(parents=True, exist_ok=True)
    baseline = safe_logit([r["market"] for r in rows])
    p = Pool([r["x"] for r in rows], [r["label"] for r in rows], weight=fixture_weights(rows, False),
             baseline=baseline, cat_features=CATS, feature_names=CONTEXT_FEATURES)
    models = []
    for seed in SEEDS:
        model = CatBoostClassifier(iterations=120, depth=3, learning_rate=.03, l2_leaf_reg=20,
                                   loss_function="Logloss", random_seed=seed, thread_count=2,
                                   verbose=False, allow_writing_files=False)
        model.fit(p)
        model.save_model(str(directory / f"context-{seed}.json"), format="json")
        models.append(model)
    return models


def forecast(rows, linear, context):
    market = np.asarray([r["market"] for r in rows]); offset = safe_logit(market)
    theta = np.asarray(linear["parameters"])
    residual = safe_logit([r["count_mean"] for r in rows]) - offset
    lp = expit(offset + theta[0] * residual + theta[1 + np.asarray([r["group"] for r in rows])])
    p = Pool([r["x"] for r in rows], cat_features=CATS, feature_names=CONTEXT_FEATURES)
    cp = np.asarray([expit(offset + m.predict(p, prediction_type="RawFormulaVal")) for m in context])
    return dict(market=market, count_mean=np.asarray([r["count_mean"] for r in rows]), linear=lp,
                count_lower=np.asarray([min(r["count_replicas"]) for r in rows]),
                context_mean=cp.mean(axis=0), context_lower=cp.min(axis=0)), cp


def interval(values, seed=504, replicas=2000):
    a = np.asarray(values, dtype=float)
    if len(a) < 2:
        return None
    rng = np.random.default_rng(seed)
    means = a[rng.integers(0, len(a), (replicas, len(a)))].mean(axis=1)
    return np.quantile(means, [.025, .975]).tolist()


def proper(rows, preds):
    valid = [i for i, r in enumerate(rows) if r["label"] is not None]
    result = {}; fixture_losses = {}
    for name, prediction in preds.items():
        # Only the mean count/context forecasts are coherent point forecasts of
        # OVER. The conservative envelopes are selection rules, not a binary law.
        if name.endswith("lower"):
            continue
        by = defaultdict(list)
        for i in valid:
            y = rows[i]["label"]; p = float(np.clip(prediction[i], 1e-9, 1 - 1e-9))
            by[rows[i]["fixture"]].append((-y * np.log(p) - (1-y) * np.log(1-p), (y-p)**2))
        fm = {f: np.mean(v, axis=0) for f, v in by.items()}; fixture_losses[name] = fm
        score = np.mean(list(fm.values()), axis=0)
        result[name] = dict(rows=len(valid), fixtures=len(fm), logLoss=float(score[0]), brier=float(score[1]))
    for model in ("linear", "context_mean"):
        for ref in ("market", "count_mean"):
            common = sorted(set(fixture_losses[model]) & set(fixture_losses[ref]))
            d = np.asarray([fixture_losses[model][f] - fixture_losses[ref][f] for f in common])
            result[f"{model}_minus_{ref}"] = dict(logLoss=float(d[:,0].mean()), brier=float(d[:,1].mean()),
                                                  logLossFixture95=interval(d[:,0]), brierFixture95=interval(d[:,1]))
    return result


def select(rows, name, prediction, context_replicas):
    packets = defaultdict(list)
    for i, r in enumerate(rows):
        for side in ("OVER", "UNDER"):
            odd = r["odds"][side]
            if not 1.8 < odd < 3 or not r["guards"][side]:
                continue
            if name == "count_lower":
                cond = min(r["count_replicas"]) if side == "OVER" else 1 - max(r["count_replicas"])
            elif name == "context_lower":
                cond = float(context_replicas[:,i].min()) if side == "OVER" else float(1 - context_replicas[:,i].max())
            else:
                cond = float(prediction[i]) if side == "OVER" else float(1 - prediction[i])
            p = cond * (1 - r["push"]); ev = p * odd + r["push"] - 1
            if p < .55 or ev <= 0:
                continue
            outcome = "PUSH" if r["label"] is None else "WON" if r["label"] == int(side == "OVER") else "LOST"
            returned = 1. if outcome == "PUSH" else odd if outcome == "WON" else 0.
            spec = [r["metric"],r["scope"],side,r["team"],r["line"]]
            packets[(r["fixture"],r["metric"]),r["observed"]].append(dict(fixture=r["fixture"],metric=r["metric"],
                spec=spec, observed=r["observed"], kickoff=r["kickoff"], odd=odd, bookmaker=r["bookmaker"],
                sourceReceipt=r["sourceReceipt"], p=p, push=r["push"], ev=ev, outcome=outcome, returned=returned))
    picked = {}
    for (group, observed), qs in sorted(packets.items(), key=lambda kv:kv[0][1]):
        if group not in picked:
            picked[group] = sorted(qs,key=lambda q:(-q["p"],-q["ev"],str(q["spec"])))[0]
    return list(picked.values())


def betting_summary(picks):
    profit = sum(q["returned"] - 1 for q in picks)
    by = defaultdict(list)
    for q in picks:
        by[q["fixture"]].append(q["returned"] - 1)
    ci = None
    if len(by) >= 2:
        a = np.asarray([[sum(v), len(v)] for v in by.values()]); rng=np.random.default_rng(505)
        s = a[rng.integers(0,len(a),(2000,len(a)))].sum(axis=1)
        ci = np.quantile(s[:,0] / s[:,1], [.025,.975]).tolist()
    return dict(selected=len(picks), fixtures=len(by), won=sum(q["outcome"]=="WON" for q in picks),
                lost=sum(q["outcome"]=="LOST" for q in picks), pushes=sum(q["outcome"]=="PUSH" for q in picks),
                profitUnits=profit, roi=profit/len(picks) if picks else None, roiFixture95=ci,
                expectedProfitUnits=sum(q["ev"] for q in picks))


def main():
    parser=argparse.ArgumentParser();parser.add_argument("--base-dir",type=Path,required=True);args=parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    model_banks = banks(args.base_dir)
    frames = {variant:make_frame(variant, model_banks) for variant in ("earliest","latest")}
    reports={}; all_picks={}; forecast_export={}
    for start, end in zip(BOUNDARIES[1:3], BOUNDARIES[2:4]):
        month=datetime.fromtimestamp(start,timezone.utc).strftime("%Y-%m")
        train=[r for r in frames["earliest"]["rows"] if r["kickoff"]+86400<start and r["label"] is not None]
        assert train and all(r["featureCutoff"]<r["kickoff"]<start-86400 for r in train)
        linear=fit_linear(train,start); context=fit_context(train,OUT/"models"/month,start)
        meta=dict(linear=linear, countBanks={str(k):v.meta for k,v in model_banks.items()},
                  trainBefore=start, trainMaxResultAvailable=max(r["kickoff"]+86400 for r in train),
                  features=CONTEXT_FEATURES, trainingQuoteVariant="earliest", modelId="SHOTS_SOT_MARKET_CONTEXT_CHALLENGER_V1",
                  featureImportance={name:float(value) for name,value in zip(CONTEXT_FEATURES,
                      np.mean([m.get_feature_importance() for m in context],axis=0))})
        write(OUT/"models"/month/"bank.json",meta)
        log("CHALLENGER_FIT",month=month,linear=linear)
        reports[month]={};all_picks[month]={};forecast_export[month]={}
        for variant, frame in frames.items():
            test=[r for r in frame["rows"] if start<=r["featureCutoff"]<end]
            assert not (set(r["fixture"] for r in train)&set(r["fixture"] for r in test))
            pred, cp=forecast(test,linear,context)
            scores=proper(test,pred); bets={}; picks_by_model={}
            for name in MODELS:
                picks=select(test,name,pred[name],cp)
                bets[name]=betting_summary(picks);picks_by_model[name]=picks
                bets[name]["byMarket"]={m:betting_summary([q for q in picks if q["metric"]==m]) for m in ("SHOTS","SOT")}
            reports[month][variant]=dict(training=linear,testRows=len(test),testFixtures=len(set(r["fixture"] for r in test)),
                                        scores=scores,betting=bets,
                byMarket={m:proper([r for r in test if r["metric"]==m],
                                  {n:p[np.asarray([r["metric"]==m for r in test])] for n,p in pred.items()}) for m in ("SHOTS","SOT")})
            all_picks[month][variant]=picks_by_model
            forecast_export[month][variant]=[dict(fixture=r["fixture"],spec=[r["metric"],r["scope"],"OVER",r["team"],r["line"]],
                observed=r["observed"],bookmaker=r["bookmaker"],label=r["label"],push=r["push"],
                forecasts={n:float(p[i]) for n,p in pred.items()}) for i,r in enumerate(test)]
            log("EVALUATED",month=month,variant=variant,rows=len(test),fixtures=reports[month][variant]["testFixtures"],
                scores={n:scores[n] for n in ("count_mean","market","linear","context_mean")},
                betting={n:{k:v for k,v in bets[n].items() if k!="byMarket"} for n in MODELS})
    summary=dict(status="CHRONOLOGICAL_DEVELOPMENT_COMPARISON",untouchedHoldout=False,profitabilityCertified=False,
                 deployed=False,providerCalls=0,ledgerWrites=0,policyChanges=0,historySha256=sha(HISTORY),
                 protocolSha256=sha(OUT/"PROTOCOL.md"),deployedBankSha256=sha(args.base_dir/"bank.json"),
                 generatedAt=datetime.now(timezone.utc).isoformat(),inputs={v:f["inputs"] for v,f in frames.items()},
                 audit={v:f["audit"] for v,f in frames.items()},reports=reports)
    write(OUT/"summary.json",summary);dump_gz(OUT/"selected.json.gz",all_picks);dump_gz(OUT/"forecasts.json.gz",forecast_export)
    log("COMPLETE",output=str(OUT/"summary.json"))


if __name__=="__main__":
    main()
