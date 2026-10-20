"""Chronological cached-price replay; never imports retrospective picks to UAT ledger."""
import gzip
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from .features import History, counts
from .feed import OUT, digest, iso, load_history, write
from .model import Bank, conservative, favourite_guard, POLICY_ID, eligible_quote
from .train import ROOT


def main():
    rows = load_history(False)
    actual = {(int(r["_date"]), r["_home"], r["_away"]): r for r in rows}
    bank = Bank(OUT / "model")
    reports = {}
    for suffix in ("earliest", "latest"):
        path = ROOT / f"reports/betting_v2/market_first_counts_20261018/quotes_{suffix}.json.gz"
        raw = path.read_bytes(); quotes = json.loads(gzip.decompress(raw))
        quotes = [q for q in quotes if q["spec"][0] in ("SHOTS", "SOT") and q["spec"][1] in ("TOTAL", "TEAM")
                  and q["spec"][2] in ("OVER", "UNDER") and q["featureCutoff"] >= bank.meta["calibrationBefore"]
                  and 1.8 < q["odd"] < 3]
        index = {}; features = []; order = []
        for q in sorted(quotes, key=lambda q: q["featureCutoff"]):
            key = q["featureCutoff"], q["_home"], q["_away"], q["kickoff"]
            if key not in index:
                index[key] = len(order); order.append(key)
        history = History(rows)
        for cutoff, h, a, ko in order:
            history.advance(cutoff)
            row = actual.get((int(ko), h, a))
            league = (row or {}).get("_home_group", "unknown")
            features.extend(history.pair(h, a, league, ko))
        batch = bank.predict(features)
        packets = defaultdict(list)
        for q in quotes:
            packets[q["fixture"], q["spec"][0], q["observed"]].append(q)
        picks = {}; audit = defaultdict(int)
        for packet, qs in sorted(packets.items(), key=lambda kv: kv[0][2]):
            fixture, label, observed = packet
            group = fixture, label
            if group in picks:
                continue
            options = []
            for q in qs:
                metric = "shots" if label == "SHOTS" else "sot"
                _, scope, side, team, line = q["spec"]
                i = index[q["featureCutoff"], q["_home"], q["_away"], q["kickoff"]] * 2
                pair = features[i:i + 2]
                if min(pair[0][4:6]) < 8:
                    continue
                replicas = [dict(shots=r["shots"][i:i + 2], sot=r["sot"][i:i + 2], alpha=r["alpha"]) for r in batch]
                p, push = conservative(replicas, metric, scope, team, side, line)
                ev = p * q["odd"] + push - 1
                audit["pricedForecasts"] += 1
                if not eligible_quote(p, push, q["odd"]):
                    continue
                if not favourite_guard(pair, replicas, q.get("_favourite"), metric, scope, team, side):
                    audit["favouriteGuard"] += 1; continue
                result = actual.get((int(q["kickoff"]), q["_home"], q["_away"]))
                c = counts(result) if result else None
                if c is None:
                    audit["missingActual"] += 1; continue
                h, a = c[:2] if metric == "shots" else c[2:]
                value = h + a if scope == "TOTAL" else h if team == "HOME" else a
                status = "VOID" if value == line else "WON" if (value > line if side == "OVER" else value < line) else "LOST"
                options.append(dict(fixture=fixture, metric=label, spec=q["spec"], odd=q["odd"], bookmaker=q["bookmaker"],
                                    p=p, push=push, ev=ev, featureCutoff=q["featureCutoff"], observed=observed,
                                    outcome=status, returned=1. if status == "VOID" else q["odd"] if status == "WON" else 0.))
            if options:
                picks[group] = sorted(options, key=lambda r: (-r["p"], -r["ev"], str(r["spec"])))[0]
        selected = list(picks.values())
        def summary(items):
            settled = [r for r in items if r["outcome"] != "VOID"]
            profit = sum(r["returned"] - 1 for r in items)
            return dict(issued=len(items), settled=len(settled), won=sum(r["outcome"] == "WON" for r in items),
                        lost=sum(r["outcome"] == "LOST" for r in items), pushes=len(items) - len(settled),
                        profitUnits=profit, roi=profit / len(items) if items else None)
        reports[suffix] = dict(summary=summary(selected), byMarket={m: summary([r for r in selected if r["metric"] == m]) for m in ("SHOTS", "SOT")},
                               audit=dict(audit), picks=selected, quotesSha256=digest(raw))
        print("SHOTS_UAT_REPLAY", suffix, json.dumps(reports[suffix]["summary"]), dict(audit), flush=True)
    report = dict(modelId=bank.meta["model"], policyId=POLICY_ID, generatedAt=iso(datetime.now(timezone.utc).timestamp()),
                  status="DEVELOPMENT_REPLAY_NOT_PROSPECTIVE", trainingFixtures=bank.meta["trainingFixtures"],
                  calibrationFixtures=bank.meta["calibrationFixtures"], validationBefore=iso(bank.meta["calibrationBefore"]),
                  overlappingQuoteVariants=True, untouchedHoldout=False, profitabilityCertified=False,
                  performanceLedgerImports=0, variants=reports)
    write(OUT / "validation.json", report)


if __name__ == "__main__": main()
