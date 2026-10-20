"""Read immutable pre-kickoff forecasts; build UAT state and public dashboard data."""
import argparse,gzip,hashlib,json,sys
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path
from .policy import decision,time,statistics

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/"data/statmaker/uat/market_watch"
sys.path.insert(0,str(ROOT/"scripts"))


def read(path,default=None):
    return json.loads(path.read_text()) if path.exists() else default


def write(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)+"\n")


def config():return read(ROOT/"config/market_watch.json")


def allowed(market,now=None):
    cfg=config()
    if cfg is None:return market in ("SHOTS","SOT")
    state=read(OUT/"state.json")
    if state is None:return cfg["markets"].get(market,{}).get("bootstrapActive",False)
    now=now or datetime.now(timezone.utc).timestamp()
    if now-time(state["generatedAt"])>cfg["maximumStateAgeHours"]*3600:return False
    return state["markets"].get(market,{}).get("status")=="ACTIVE"


def valid_contract(row,cfg):
    return (cfg["minimumWinProbability"]<=row["p"]<=1 and row["push"]>=0 and row["p"]+row["push"]<=1.000001
            and cfg["minimumOddsExclusive"]<row["odd"]<cfg["maximumOddsExclusive"]
            and row["p"]*row["odd"]+row["push"]-1>0 and row["observed"]<=row["published"]<row["kickoff"])


def shot_records(cfg,actual,now):
    from scripts.shots_uat.feed import settle
    ledgers=[read(ROOT/"data/statmaker/uat/shots_v1/ledger.json",{}),read(OUT/"paper_ledger.json",{})]
    unique={r["selectionId"]:r for d in ledgers for r in d.get("rows",[])}
    result=[];audit=Counter()
    for original in unique.values():
        if not original.get("publishedAt"):audit["unpublished"]+=1;continue
        # Do not combine the old P55 selection policy with the restored P60 cohort.
        if original.get("policyId") not in ("SHOTS_SOT_P60_UAT_V4","SHOTS_SOT_SCOPE_GUARD_V2"):
            audit["otherPolicy"]+=1;continue
        r=settle(original,actual,now)
        row=dict(id=r["selectionId"],fixture=str(r["apiFixtureId"]),market=r["metric"].upper(),
                 kickoff=time(r["kickoff"]),published=time(r["publishedAt"]),observed=time(r["quoteUpdatedAt"]),
                 p=r["winProbability"],push=r["pushProbability"],odd=r["odd"],outcome=r["outcome"],returned=r["settlementReturn"],
                 model=r["model"],policy=r["policyId"],source="PUBLISHED_UAT_P60",home=r["homeTeam"],away=r["awayTeam"])
        if valid_contract(row,cfg):result.append(row)
        else:audit["invalidContract"]+=1
    return result,dict(audit)


def shadow_records(cfg,actual,now):
    rows=[];audit=Counter();candidates=[]
    from materialize_betting_v2_direct import metric_values
    from betting_v2.research_app_markets import outcome_extended
    truth={f"{r['_date']}:{r['_home']}:{r['_away']}":r for r in actual}
    sources=[]
    for folder in (ROOT/"reports/betting_v2/forward_market_first_20261018",ROOT/"reports/market_watch/count_paper_v1"):
        index=read(folder/"index.json",{});proofs=read(folder/"publication_evidence.json",{})
        sources.extend((folder,entry,proofs.get(entry["path"])) for entry in index.get("cohorts",[]))
    sources.sort(key=lambda v:time(v[1]["frozenAtUTC"]))
    seen=set()
    for folder,entry,proof in sources:
        if not proof:audit["noPublicationProof"]+=1;continue
        raw=(folder/entry["path"]).read_bytes();digest=hashlib.sha256(raw).hexdigest()
        if digest!=entry["sha256"] or digest!=proof["sha256"]:raise ValueError("Immutable market cohort hash mismatch")
        doc=json.loads(gzip.decompress(raw));published=time(proof["publiclyVerifiedAtUTC"])
        reserved=set(map(tuple,doc.get("marketKeys",[])))
        for q in doc["models"]["coupled_conservative"]["selected"]:
            metric,scope,side,team,line=q["spec"]
            if metric not in ("CORNERS","FOULS"):continue
            key=q["fixture"],metric
            if key in seen:audit["duplicateLaterPacket"]+=1;continue
            lower=q.get("_lower",{});p=lower.get("p",q["p"]);push=lower.get("push",q.get("push",0.))
            identity=hashlib.sha256(json.dumps([key,q["bookmaker"],q["spec"],q["odd"]],sort_keys=True).encode()).hexdigest()
            r=dict(id=identity,fixture=q["fixture"],market=metric,kickoff=q["kickoff"],observed=q["observed"],published=published,
                   p=p,push=push,odd=q["odd"],outcome="PENDING",returned=None,model="COUNT_COUPLED_V1",
                   policy="COUNT_COUPLED_P60_CONSERVATIVE",source="PUBLISHED_PAPER",home=q["_home"],away=q["_away"])
            if not valid_contract(r,cfg):audit["invalidOrLateContract"]+=1;continue
            if now>q["kickoff"]+3*3600 and (label:=truth.get(q["fixture"])) is not None:
                values=metric_values(label,metric)
                if values is not None:
                    y,void=outcome_extended(*values,q["spec"])
                    r.update(outcome="VOID" if void else "WON" if y else "LOST",returned=1. if void else q["odd"] if y else 0.)
            rows.append(r)
            if q["kickoff"]>now+900:candidates.append(dict(q,_monitorId=identity,_monitorP=p,_monitorPush=push))
        seen.update(reserved)
    return rows,dict(audit),candidates


def historical():
    result={}
    validation=read(ROOT/"data/statmaker/uat/shots_v1/validation.json",{})
    for variant,data in validation.get("variants",{}).items():
        for market,s in data.get("byMarket",{}).items():
            result.setdefault(market,{})[variant]=dict(s,source="COUNT_CATBOOST_P60_DEVELOPMENT",untouchedHoldout=False)
    for variant in ("earliest","latest"):
        d=read(ROOT/f"reports/betting_v2/market_first_counts_20261018/priced_{variant}.json",{})
        for market,s in d.get("reports",{}).get("coupled_conservative",{}).get("byMetric",{}).items():
            if market in ("CORNERS","FOULS"):result.setdefault(market,{})[variant]=dict(s,source="COUNT_COUPLED_DEVELOPMENT",untouchedHoldout=False)
    return result


def refresh_current_feed(now):
    """Apply pause/resume immediately to existing public issues, without repricing."""
    from scripts.shots_uat.feed import publish_read_model,compatible_p60_payload,digest
    base=ROOT/"data/statmaker/uat/shots_v1";index=read(base/"p55/latest.json")
    if not index:return
    path=base/"p55"/index["artifact"]
    if not path.exists():return
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=index["sha256"]:raise ValueError("Read model hash mismatch")
    payload=json.loads(raw)
    def current(rows):
        return [r for r in rows if allowed(r["metric"].upper(),now) and r["winProbability"]>=.60
                and time(r["kickoff"])>now and r.get("latestQuoteObservedAt")
                and now-86400<=time(r["latestQuoteObservedAt"])<=now]
    payload["candidates"]=current(payload["issued"]);payload["candidateCount"]=len(payload["candidates"])
    payload["marketCandidates"]=current(payload.get("marketIssued",[]));payload["marketCandidateCount"]=len(payload["marketCandidates"])
    payload["minimumWinProbability"]=.60
    payload["generatedAt"]=datetime.fromtimestamp(now,timezone.utc).isoformat()
    payload["generationId"]=digest(json.dumps([payload["generationId"],payload["generatedAt"],payload["candidates"],payload["marketCandidates"]],sort_keys=True).encode())
    publish_read_model(base/"p55",payload);publish_read_model(base,compatible_p60_payload(payload))


def main():
    p=argparse.ArgumentParser();p.add_argument("--cached-history-only",action="store_true");args=p.parse_args()
    from scripts.shots_uat.feed import load_history
    now=datetime.now(timezone.utc).timestamp();cfg=config();actual=load_history(not args.cached_history_only)
    first,a=shot_records(cfg,actual,now);second,b,candidates=shadow_records(cfg,actual,now);rows=first+second
    old=read(OUT/"state.json",{});events=read(OUT/"events.json",dict(events=[]));markets={};history=historical()
    for metric,settings in cfg["markets"].items():
        selected=[r for r in rows if r["market"]==metric]
        data=decision(selected,old.get("markets",{}).get(metric),cfg,metric,now)
        if data["transition"]:events["events"].append(dict(at=now,market=metric,modelId=settings["modelId"],
            previous=data["previousStatus"],status=data["status"],reason=data["reason"],settled=data["total"]["settled"],roi=data["total"]["roi"]))
        markets[metric]=dict(data,**settings,historical=history.get(metric,{}),records=selected)
    state=dict(schemaVersion=1,policyId=cfg["policyId"],generatedAt=datetime.fromtimestamp(now,timezone.utc).isoformat(),
               policy=cfg,markets=markets,events=events["events"][-100:],audit=dict(shots=a,shadow=b),
               noProviderCalls=True,automaticUatRouting=True,profitabilityCertified=False,
               limitations=["Οι κανόνες είναι λειτουργική πολιτική UAT, όχι στατιστική πιστοποίηση.",
                   "Το ιστορικό ανάπτυξης δεν προσμετράται ως προδημοσιευμένη επίδοση.",
                   "Οι αγορές χωρίς μοντέλο δεν ενεργοποιούνται από θετικό ROI άλλου μοντέλου."])
    write(OUT/"state.json",state);write(OUT/"events.json",events);write(OUT/"shadow_candidates.json",dict(generatedAt=state["generatedAt"],rows=candidates))
    refresh_current_feed(now)
    docs=ROOT/"docs/market-watch";write(docs/"data.json",state)
    template=(ROOT/"scripts/market_watch/dashboard.html").read_text()
    # A standalone generated page works on Pages and as a local HTML preview.
    embedded=json.dumps(state,ensure_ascii=False,separators=(",",":")).replace("<","\\u003c")
    (docs/"index.html").write_text(template.replace("__MARKET_DATA__",embedded));(ROOT/"docs/.nojekyll").touch()
    lines=["# StatMaker · Market Watch","",f"Ενημέρωση: {state['generatedAt']}","", "| Αγορά | Κατάσταση | Settled | ROI | Εκκρεμείς |", "|---|---|---:|---:|---:|"]
    for m,d in markets.items():
        roi="—" if d["total"]["roi"] is None else f"{d['total']['roi']:+.2%}"
        lines.append(f"| {d['name']} | {d['status']} | {d['total']['settled']} | {roi} | {d['pending']} |")
    lines += ["", "[Dashboard source](index.html) · [Δεδομένα](data.json)","", "Το ιστορικό ανάπτυξης εμφανίζεται χωριστά στο dashboard. Οι αρχικές SHOTS/SOT είναι ενεργές ως UAT baseline."]
    (docs/"README.md").write_text("\n".join(lines)+"\n")
    print(json.dumps({m:dict(status=d["status"],settled=d["total"]["settled"],pending=d["pending"],roi=d["total"]["roi"]) for m,d in markets.items()}),flush=True)


if __name__=="__main__":main()
