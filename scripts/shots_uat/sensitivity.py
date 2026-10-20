"""Fixed-grid diagnostics; no refit, issue, threshold optimization or provider call."""
import argparse
import gzip
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .features import History, counts, normalized
from .feed import (MARKETS, OUT, bind_quote, favourite, iso, load_history,
                   load_quote_sources, raw_quotes, timestamp, write)
from .model import Bank, conservative, distribution, favourite_guard, probabilities
from .sources import Identities
from .train import ROOT


POLICIES = [(f"p{round(p*100)}_full", p, "full", "win") for p in (.60, .58, .55, .52, .50)] + [
    ("p60_direction_elo", .60, "direction_elo", "win"),
    ("p55_direction_elo", .55, "direction_elo", "win"),
    ("p60_no_team_guard", .60, "none", "win"),
    ("p55_no_team_guard", .55, "none", "win"),
    ("p60_conditional", .60, "full", "conditional"),
    ("p60_mean_of_fits", .60, "full", "mean"),
]


def guard_modes(pair, replicas, fav, metric, scope, team, side):
    direction = scope == "TOTAL" or (fav in ("HOME", "AWAY") and
                 (team == fav) == (side == "OVER") and pair[0 if fav == "HOME" else 1][1] > 0)
    return dict(full=favourite_guard(pair, replicas, fav, metric, scope, team, side),
                direction_elo=direction, none=True)


def forecast(replicas, metric, scope, team, side, line):
    values = [probabilities(distribution(r, metric, scope, team), side, line) for r in replicas]
    p, push = min(v[0] for v in values), min(v[1] for v in values)
    return dict(p=p, push=push, meanP=sum(v[0] for v in values) / len(values),
                meanPush=sum(v[1] for v in values) / len(values))


def accepted(q, policy):
    _, minimum, guard, mode = policy
    p, push = (q["meanP"], q["meanPush"]) if mode == "mean" else (q["p"], q["push"])
    tested = p / (1 - push) if mode == "conditional" else p
    return tested >= minimum and p * q["odd"] + push - 1 > 0 and q["guards"][guard]


def select(options, policy):
    packets = defaultdict(list)
    for q in options:
        if accepted(q, policy):
            packets[q["group"], q["observed"]].append(q)
    picked = {}
    for (group, observed), qs in sorted(packets.items(), key=lambda kv: kv[0][1]):
        if group not in picked:
            picked[group] = sorted(qs, key=lambda r: (-r["p"], -(r["p"] * r["odd"] + r["push"] - 1), r["tie"]))[0]
    return list(picked.values())


def forward(history_rows, bank, now):
    leagues, raw, sources = load_quote_sources(now)
    identity = Identities(ROOT, history_rows); history = History(history_rows); history.advance(now)
    records = []; audit = Counter(); by_day = defaultdict(Counter)
    for league in leagues:
        if not league.get("enabledForBetting", True):
            continue
        for m in league.get("matches", []):
            ko = timestamp(m.get("kickoff"))
            if not now + 900 < ko < now + 7 * 86400:
                continue
            day = datetime.fromtimestamp(ko, ZoneInfo("Europe/Athens")).date().isoformat()
            audit["fixtures"] += 1; by_day[day]["fixtures"] += 1
            quotes = raw_quotes(m, raw)
            if not quotes:
                continue
            audit["withShotsOdds"] += 1; by_day[day]["withShotsOdds"] += 1
            bound, reason = identity.bind(league, m)
            if bound is None:
                audit[reason] += 1; by_day[day][reason] += 1; continue
            m, h, a, api = bound
            pair = history.pair(h, a, league["leagueCode"], ko)
            if min(pair[0][4:6]) < 8:
                audit["insufficientHistory"] += 1; continue
            audit["forecastable"] += 1; by_day[day]["forecastable"] += 1
            replicas = bank.predict(pair); cache = {}; guards = {}
            for q in quotes:
                audit["quotes"] += 1; by_day[day]["quotes"] += 1
                if not 1.8 < float(q["odds"]) < 3:
                    audit["oddsTooLow" if float(q["odds"]) <= 1.8 else "oddsTooHigh"] += 1; continue
                audit["oddsInBounds"] += 1; by_day[day]["oddsInBounds"] += 1
                binding = bind_quote(m, q, raw)
                if not binding:
                    audit["unbound"] += 1; continue
                side, observed = binding
                if not now - 86400 <= observed <= now:
                    audit["stale"] += 1; continue
                metric, scope = MARKETS[q["market"]]
                team = "HOME" if normalized(q.get("team")) == normalized(m["homeTeam"]) else "AWAY" if normalized(q.get("team")) == normalized(m["awayTeam"]) else ""
                if scope == "TEAM" and not team:
                    continue
                line = float(q["line"])
                if line < 0 or (line * 2) % 1:
                    continue
                key = metric, scope, team, side, line
                if key not in cache:
                    cache[key] = forecast(replicas, *key)
                gkey = metric, scope, team, side, q["bookmaker"]
                if gkey not in guards:
                    guards[gkey] = guard_modes(pair, replicas, favourite(m, q["bookmaker"], raw, now), metric, scope, team, side)
                tie = hashlib.sha256(json.dumps(["SHOTS_SOT_CATBOOST_V1", f"api:{api}:{metric}", q["market"], team, side, line, q["bookmaker"], q["odds"]]).encode()).hexdigest()
                # A comparison snapshot ranks every offer together. Do not favour an
                # older timestamp as if this were historical first publication.
                records.append(dict(cache[key], group=f"api:{api}:{metric}", observed=now, quoteObserved=observed,
                    day=day, odd=float(q["odds"]), tie=tie, guards=guards[gkey], metric=metric, scope=scope,
                    spec=[metric, scope, side, team, line], home=m["homeTeam"], away=m["awayTeam"],
                    bookmaker=q["bookmaker"], competitionId=league["competitionId"]))
    return records, dict(audit), {k:dict(v) for k,v in by_day.items()}, sources


def history_options(history_rows, bank, suffix):
    path = ROOT / f"reports/betting_v2/market_first_counts_20261018/quotes_{suffix}.json.gz"
    raw = path.read_bytes(); qs = json.loads(gzip.decompress(raw))
    qs = [q for q in qs if q["spec"][0] in ("SHOTS", "SOT") and q["spec"][1] in ("TOTAL", "TEAM")
          and q["spec"][2] in ("OVER", "UNDER") and q["featureCutoff"] >= bank.meta["calibrationBefore"]
          and 1.8 < q["odd"] < 3]
    actual = {(int(r["_date"]),r["_home"],r["_away"]):r for r in history_rows}
    keys = list(dict.fromkeys((q["featureCutoff"],q["_home"],q["_away"],q["kickoff"]) for q in sorted(qs,key=lambda q:q["featureCutoff"])))
    history = History(history_rows); pairs = {}; features = []
    for key in keys:
        cutoff,h,a,ko = key; history.advance(cutoff)
        league=(actual.get((int(ko),h,a)) or {}).get("_home_group","unknown")
        pair=history.pair(h,a,league,ko); pairs[key]=(len(features),pair); features.extend(pair)
    batch=bank.predict(features); records=[]; cache={}; guards={}
    for q in qs:
        key=(q["featureCutoff"],q["_home"],q["_away"],q["kickoff"]);i,pair=pairs[key]
        if min(pair[0][4:6])<8:continue
        label,scope,side,team,line=q["spec"];metric="shots" if label=="SHOTS" else "sot"
        replicas=[dict(shots=r["shots"][i:i+2],sot=r["sot"][i:i+2],alpha=r["alpha"]) for r in batch]
        fkey=(key,metric,scope,team,side,line)
        if fkey not in cache:cache[fkey]=forecast(replicas,metric,scope,team,side,line)
        gkey=(key,metric,scope,team,side,q.get("_favourite"))
        if gkey not in guards:guards[gkey]=guard_modes(pair,replicas,q.get("_favourite"),metric,scope,team,side)
        records.append(dict(cache[fkey],group=str((q["fixture"],label)),observed=q["observed"],odd=q["odd"],
            guards=guards[gkey],metric=metric,spec=q["spec"],tie=str(q["spec"]),home=q["_home"],away=q["_away"],
            kickoff=q["kickoff"],actualKey=(int(q["kickoff"]),q["_home"],q["_away"])))
    return records,actual,hashlib.sha256(raw).hexdigest()


def grade(rows, actual):
    graded=[]; missing=0
    for q in rows:
        result=actual.get(tuple(q["actualKey"]));c=counts(result) if result else None
        if c is None:missing+=1;continue
        _,scope,side,team,line=q["spec"]
        h,a=c[:2] if q["metric"]=="shots" else c[2:]
        value=h+a if scope=="TOTAL" else h if team=="HOME" else a
        returned=1. if value==line else q["odd"] if (value>line if side=="OVER" else value<line) else 0.
        graded.append(dict(q,returned=returned))
    profit=sum(q["returned"]-1 for q in graded)
    return dict(selected=len(rows),settled=len(graded),missingActual=missing,won=sum(q["returned"]>1 for q in graded),
                lost=sum(q["returned"]==0 for q in graded),pushes=sum(q["returned"]==1 for q in graded),
                profitUnits=profit,roi=profit/len(graded) if graded else None)


def main():
    parser=argparse.ArgumentParser();parser.add_argument("--model-dir",type=Path,default=OUT/"model")
    parser.add_argument("--as-of",default="2026-10-19T19:55:44.362950Z")
    parser.add_argument("--output",type=Path,required=True);args=parser.parse_args()
    bank=Bank(args.model_dir);now=timestamp(args.as_of);rows=load_history()
    options,audit,coverage,sources=forward(rows,bank,now);reports={};days=sorted(coverage)
    for policy in POLICIES:
        picks=select(options,policy)
        reports[policy[0]]=dict(total=len(picks),byDay={d:sum(q["day"]==d for q in picks) for d in days},
            byMetric=dict(Counter(q["metric"] for q in picks)),picks=picks)
    print("FORWARD",json.dumps({k:dict(total=v["total"],byDay=v["byDay"]) for k,v in reports.items()}),flush=True)
    print("COVERAGE",json.dumps(audit),flush=True)
    historical={}
    frozen=load_history(False)
    for suffix in ("earliest","latest"):
        options,actual,sha=history_options(frozen,bank,suffix)
        historical[suffix]=dict(quotesSha256=sha,policies={p[0]:grade(select(options,p),actual) for p in POLICIES})
        print("HISTORICAL",suffix,json.dumps(historical[suffix]["policies"]),flush=True)
    write(args.output,dict(status="POST_HOC_FIXED_GRID_DIAGNOSTIC",asOf=args.as_of,
        modelBankSha256=hashlib.sha256((args.model_dir/"bank.json").read_bytes()).hexdigest(),
        deployed=False,untouchedHoldout=False,profitabilityCertified=False,providerCalls=0,modelFits=0,
        policies=POLICIES,forward=reports,coverage=coverage,audit=audit,sources=sources,historical=historical))


if __name__=="__main__":main()
