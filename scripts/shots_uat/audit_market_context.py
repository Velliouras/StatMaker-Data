"""Independent artifact/source arithmetic audit, plus original replay parity."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import numpy as np
from catboost import CatBoostClassifier, Pool
from scipy.special import expit, logit

from .features import counts
from .feed import load_history, write
from .model import Bank
from .sensitivity import history_options, select as original_select
from .train import ROOT
from .market_context import OUT, BOUNDARIES, CATS, CONTEXT_FEATURES


def read_gz(path):
    return json.loads(gzip.decompress(path.read_bytes()))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    p=argparse.ArgumentParser();p.add_argument("--base-dir",type=Path,required=True);args=p.parse_args()
    summary=json.loads((OUT/"summary.json").read_text());picks=read_gz(OUT/"selected.json.gz")
    forecasts=read_gz(OUT/"forecasts.json.gz"); actual={(int(r["_date"]),r["_home"],r["_away"]):r for r in load_history(False)}
    totals=dict(sourcePairs=0,outcomes=0,probabilityRows=0,properScores=0,selectedPayouts=0,baselineContracts=0)
    raw_model_max=0.
    for variant in ("earliest","latest"):
        frame=read_gz(OUT/f"frame_{variant}.json.gz")
        source=ROOT/f"reports/betting_v2/market_first_counts_20261018/quotes_{variant}.json.gz"
        assert sha(source)==frame["inputs"]["quotes"]
        raw=read_gz(source); index={(q["fixture"],q["bookmaker"],q["sourceReceipt"],tuple(q["spec"])):q for q in raw}
        for r in frame["rows"]:
            o,u=[index[r["fixture"],r["bookmaker"],r["sourceReceipt"],(r["metric"],r["scope"],s,r["team"],r["line"])] for s in ("OVER","UNDER")]
            assert r["odds"]==dict(OVER=o["odd"],UNDER=u["odd"])
            assert r["market"]==(1/o["odd"])/(1/o["odd"]+1/u["odd"])
            assert r["baseBefore"]<=r["featureCutoff"]<=r["observed"]<r["kickoff"]
            c=counts(actual[int(r["kickoff"]),o["_home"],o["_away"]]);h,a=c[:2] if r["metric"]=="SHOTS" else c[2:]
            value=h+a if r["scope"]=="TOTAL" else h if r["team"]=="HOME" else a
            assert r["actual"]==value
            assert r["label"]==(None if value==r["line"] else int(value>r["line"]))
            assert r["push"]==0., "Archive audit currently expects the observed half lines"
            totals["sourcePairs"]+=1;totals["outcomes"]+=1
        for start,end in zip(BOUNDARIES[1:3],BOUNDARIES[2:4]):
            from datetime import datetime,timezone
            month=datetime.fromtimestamp(start,timezone.utc).strftime("%Y-%m")
            test=[r for r in frame["rows"] if start<=r["featureCutoff"]<end]
            f=forecasts[month][variant];assert len(f)==len(test)
            bank=json.loads((OUT/"models"/month/"bank.json").read_text())
            assert bank["trainMaxResultAvailable"]<start
            xs=[r["x"] for r in test]; offset=logit([r["market"] for r in test])
            cp=[]
            for seed in (17,31,71):
                model=CatBoostClassifier();model.load_model(str(OUT/"models"/month/f"context-{seed}.json"),format="json")
                plain=model.predict(Pool(xs,cat_features=CATS,feature_names=CONTEXT_FEATURES),prediction_type="RawFormulaVal")
                anchored=model.predict(Pool(xs,baseline=offset,cat_features=CATS,feature_names=CONTEXT_FEATURES),prediction_type="RawFormulaVal")
                error=float(np.max(np.abs(anchored-plain-offset)));raw_model_max=max(raw_model_max,error)
                assert error<1e-6
                cp.append(expit(plain+offset))
            cp=np.asarray(cp);theta=np.asarray(bank["linear"]["parameters"])
            lin=expit(offset+theta[0]*(logit([r["count_mean"] for r in test])-offset)+theta[1+np.array([r["group"] for r in test])])
            for i,(r,z) in enumerate(zip(test,f)):
                assert (r["fixture"],r["observed"],r["bookmaker"])==(z["fixture"],z["observed"],z["bookmaker"])
                assert abs(z["forecasts"]["linear"]-lin[i])<1e-10
                assert abs(z["forecasts"]["context_mean"]-cp[:,i].mean())<1e-10
                totals["probabilityRows"]+=1
            ids={v:i for i,v in enumerate(sorted(set(r["fixture"] for r in test)))}
            g=np.array([ids[r["fixture"]] for r in test]);num=np.bincount(g);y=np.array([r["label"] for r in test])
            for name in ("count_mean","market","linear","context_mean"):
                pr=np.clip([z["forecasts"][name] for z in f],1e-9,1-1e-9)
                ll=-(y*np.log(pr)+(1-y)*np.log(1-pr));bs=(y-pr)**2
                calculated=[float(np.mean(np.bincount(g,weights=a)/num)) for a in (ll,bs)]
                saved=summary["reports"][month][variant]["scores"][name]
                assert np.allclose(calculated,[saved["logLoss"],saved["brier"]],atol=1e-12,rtol=0)
                totals["properScores"]+=2
            for name, selected in picks[month][variant].items():
                for q in selected:
                    matching=[r for r in test if r["fixture"]==q["fixture"] and r["observed"]==q["observed"]
                              and r["bookmaker"]==q["bookmaker"] and r["metric"]==q["metric"]
                              and r["scope"]==q["spec"][1] and r["team"]==q["spec"][3] and r["line"]==q["spec"][4]]
                    assert len(matching)==1;r=matching[0];side=q["spec"][2]
                    returned=q["odd"] if r["label"]==int(side=="OVER") else 0.
                    assert q["returned"]==returned and r["guards"][side]
                    assert .55<=q["p"]<=1 and 1.8<q["odd"]<3 and q["ev"]>0
                    totals["selectedPayouts"]+=1
                saved=summary["reports"][month][variant]["betting"][name]
                assert saved["selected"]==len(selected)
                assert abs(saved["profitUnits"]-sum(q["returned"]-1 for q in selected))<1e-12
        original,_,_=history_options(load_history(False),Bank(args.base_dir),variant)
        chosen=original_select(original,("p55_full",.55,"full","win"))
        old={(q["home"],q["away"],q["kickoff"],tuple(q["spec"]),q["odd"]) for q in chosen}
        new=set()
        for q in picks["2026-10"][variant]["count_lower"]:
            r=next(r for r in frame["rows"] if r["fixture"]==q["fixture"])
            source_q=next(v for v in raw if v["fixture"]==q["fixture"])
            new.add((source_q["_home"],source_q["_away"],q["kickoff"],tuple(q["spec"]),q["odd"]))
        assert old==new,(variant,len(old),len(new),old^new)
        totals["baselineContracts"]+=len(new)
    audit=dict(status="PASS",checks=totals,maxCatBoostBaselineArithmeticDifference=raw_model_max,
               baselineReplay="Exact parity with original P55 selector, 82 earliest / 86 latest",
               sourceCodeSha256=sha(ROOT/"scripts/shots_uat/market_context.py"),
               scriptSha256=sha(Path(__file__)),protocolSha256=sha(OUT/"PROTOCOL.md"))
    write(OUT/"audit.json",audit);print(json.dumps(audit),flush=True)


if __name__=="__main__":main()
