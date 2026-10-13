#!/usr/bin/env python3
"""Build a verified UEFA participant feed from the canonical App-Ready ZIP.

Repairs the no-op UEFA backfill when live odds feeds have zero matches.
Reads a SHA256-verified existing PROD App-Ready artifact ONLY; it never
fabricates match statistics or calls external providers. Exports team/date
metadata, not odds prices. Feed entries remain league-scoped.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile

FILES={"CL":"champions_league_odds.json",
       "EL":"europa_league_odds.json",
       "UECL":"conference_league_odds.json"}
ALIASES={"CONF":"UECL","CONFERENCE_LEAGUE":"UECL"}


def _date(value):
    try:
        d=datetime.fromisoformat(str(value or "").replace("Z","+00:00"))
        return d.astimezone(timezone.utc) if d.tzinfo else None
    except (ValueError,TypeError):
        return None


def load_bundle(root):
    raw=json.loads((root/"data/statmaker/app_ready/update_manifest.json").read_text(encoding="utf-8"))
    chosen=[x for x in raw.get("artifacts") or [] if x.get("id")=="app_ready_betting_bundle"]
    if len(chosen)!=1:
        raise ValueError("Expected one canonical betting artifact")
    item=chosen[0]
    path=(root/item["path"]).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file() or path.suffix!=".zip":
        raise ValueError("Unsafe or absent canonical App-Ready ZIP")
    actual=hashlib.sha256(path.read_bytes()).hexdigest()
    if actual!=item["sha256"]:
        raise ValueError("Canonical App-Ready checksum mismatch")
    return path,actual


def create_feeds(root:Path,feed_dir:Path,now:datetime)->dict:
    root=root.resolve()
    archive,checksum=load_bundle(root)
    feed_dir.mkdir(parents=True,exist_ok=True)
    records={code:{} for code in FILES}
    counters=Counter()
    with ZipFile(archive) as z:
        for name in z.namelist():
            if not name.startswith("files/app_ready_odds/") or not name.endswith(".json"):
                continue
            payload=json.loads(z.read(name))
            for scope in payload.get("leagues",[payload]):
                code=ALIASES.get(str(scope.get("leagueCode") or "").upper(),
                                 str(scope.get("leagueCode") or "").upper())
                if code not in FILES:
                    continue
                for match in scope.get("matches") or []:
                    if not isinstance(match,dict):
                        continue
                    date=_date(match.get("kickoff") or match.get("date"))
                    if not date or date<=now:
                        counters["EXPIRED_OR_INVALID_FIXTURE"]+=1
                        continue
                    home=str(match.get("canonicalHomeTeam") or
                             match.get("homeTeam") or match.get("providerHomeTeam") or "").strip()
                    away=str(match.get("canonicalAwayTeam") or
                             match.get("awayTeam") or match.get("providerAwayTeam") or "").strip()
                    if not home or not away or home.casefold()==away.casefold():
                        counters["INVALID_TEAM_PAIR"]+=1
                        continue
                    key=(date.isoformat(),home.casefold(),away.casefold())
                    records[code][key]={
                        "canonicalHomeTeam":home,"canonicalAwayTeam":away,
                        "kickoff":date.isoformat(),
                        "source":"SHA256_VERIFIED_CANONICAL_APP_READY",
                    }
    result={}
    for code,file in FILES.items():
        path=feed_dir/file
        original=json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        live=original.get("matches") or []
        unique={}
        for m in live:
            key=(_date(m.get("kickoff") or m.get("date")),
                 str(m.get("canonicalHomeTeam") or m.get("homeTeam") or "").casefold(),
                 str(m.get("canonicalAwayTeam") or m.get("awayTeam") or "").casefold())
            if all(key) and key[0]>now:
                unique[key]=m
        live_count=len(unique)
        for m in records[code].values():
            key=(_date(m["kickoff"]),m["canonicalHomeTeam"].casefold(),m["canonicalAwayTeam"].casefold())
            unique.setdefault(key,m)
        prepared={**original,"leagueCode":code,
                  "matches":list(unique.values()),
                  "phase1CanonicalBundleChecksum":checksum}
        path.write_text(json.dumps(prepared,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        result[code]={"liveFixtures":live_count,
                      "canonicalSupplementalFixtures":len(records[code]),
                      "finalFixtures":len(unique),
                      "uniqueTeamNames":len({n for m in unique.values() for n in (
                           m.get("canonicalHomeTeam") or m.get("homeTeam"),
                           m.get("canonicalAwayTeam") or m.get("awayTeam")) if n})}
    if not any(v["finalFixtures"] for v in result.values()):
        raise ValueError("UEFA live and canonical App-Ready feeds are both empty; refusing a no-op backfill")
    return {"source":"SHA256_VERIFIED_CANONICAL_APP_READY_PLUS_LIVE",
            "bundleSha256":checksum,"byLeague":result,"excluded":dict(counters),
            "apiCalls":0,"noOddsPricesExported":True}


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--root",type=Path,default=Path("."))
    ap.add_argument("--feed-dir",type=Path,required=True)
    ap.add_argument("--report",type=Path,required=True)
    args=ap.parse_args()
    result=create_feeds(args.root,args.feed_dir,datetime.now(timezone.utc))
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("UEFA_CANONICAL_FEED_REPAIRED",json.dumps(result["byLeague"],ensure_ascii=False))


if __name__=="__main__":
    main()
