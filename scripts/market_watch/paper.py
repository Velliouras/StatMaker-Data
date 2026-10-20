"""Permanent rolling first-price paper source; reuse the frozen count architecture."""
import argparse,json
from datetime import datetime,timezone
from .build import ROOT,read,write


def main():
    import sys
    sys.path.insert(0,str(ROOT/"scripts"))
    from betting_v2 import forward_market_first as source
    from betting_v2.research_csv_prices import digest
    parser=argparse.ArgumentParser();parser.add_argument("--record-publication",action="store_true");args=parser.parse_args()
    config_path=ROOT/"config/market_watch_count_source.json";cfg=read(config_path)
    source.CONFIG=config_path  # Existing freeze records this exact configuration hash.
    out=ROOT/"reports/market_watch/count_paper_v1";out.mkdir(parents=True,exist_ok=True)
    old=read(ROOT/"reports/betting_v2/forward_market_first_20261018/index.json",{})
    index=read(out/"index.json",dict(schema=1,cohorts=[],marketKeys=[]))
    index["marketKeys"]=sorted(set(map(tuple,index["marketKeys"]))|set(map(tuple,old.get("marketKeys",[]))))
    if args.record_publication:
        source.record_publication(out,index);source.score(out,index);return
    changed=[p for p,h in cfg["files"].items() if not (ROOT/p).exists() or digest((ROOT/p).read_bytes())!=h]
    now=datetime.now(timezone.utc).timestamp();runtime=dict(cfg,freezeBeforeUTC=datetime.fromtimestamp(now+cfg["horizonDays"]*86400,timezone.utc).isoformat())
    name,audit=source.freeze(out,index,runtime) if not changed else (None,dict(changedSourceFiles=changed))
    write(out/"index.json",index);source.score(out,index)
    write(out/"status.json",dict(generatedAtUTC=datetime.now(timezone.utc).isoformat(),newCohort=name,audit=audit,
                                  modelChanged=False,providerCalls=0,llmCalls=0,rollingHorizonDays=cfg["horizonDays"]))
    print("MARKET_WATCH_PAPER",json.dumps(dict(newCohort=name,audit=audit)),flush=True)


if __name__=="__main__":main()
