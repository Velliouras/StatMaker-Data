#!/usr/bin/env python3
from __future__ import annotations
import argparse, datetime as dt, json, math, sqlite3, subprocess, tempfile, zipfile
from pathlib import Path
from zoneinfo import ZoneInfo
import refresh_live_settlements as live

ROOT=Path(__file__).resolve().parents[1]
APP=ROOT/'data/statmaker/app_ready'; LEDGER=ROOT/'data/statmaker/canonical_recommendation_ledger.json'; VALIDITY=ROOT/'data/statmaker/fixture_validity.json'
ATHENS=ZoneInfo('Europe/Athens'); RETENTION=30; SAFETY_MS=60000; SCHEMA_VERSION=8
RULES_FINGERPRINT='pattern-policy-v2-final-read-model-v8-retire-asian-handicap-independent-precision-v1'
RETIRED_SUB_MARKET_KEYS={
    'RESULT_ASIAN_HANDICAP','HT_RESULT_ASIAN_HANDICAP',
    'ASIAN_MATCH_GOALS_TOTAL','ASIAN_FIRST_HALF_GOALS_TOTAL',
    'ASIAN_MATCH_CORNERS_TOTAL','ASIAN_CORNER_HANDICAP','CORNER_HANDICAP'
}

def load(path,default):
    try:return json.loads(path.read_text(encoding='utf-8-sig'))
    except Exception:return default

def invalidated_match_keys(low,high):
    root=load(VALIDITY,{})
    out=set()
    for row in root.get('dispositions',[]) if isinstance(root,dict) else []:
        if not isinstance(row,dict):continue
        key=str(row.get('matchKey') or '').strip(); day=str(row.get('localDate') or '')[:10]
        disposition=str(row.get('disposition') or '').strip().upper()
        if key and day and low.isoformat()<=day<=high.isoformat() and disposition in {'RESCHEDULED','CANCELLED','POSTPONED','ABANDONED'}:
            out.add(key)
    return out


def num(v,d=float('-inf')):
    try:
        x=float(v); return x if math.isfinite(x) else d
    except Exception:return d

def intval(v,d=0):
    try:return int(v)
    except Exception:return d

def rows(db,sql,args=()):
    c=db.execute(sql,args); n=[x[0] for x in c.description]; return [dict(zip(n,r)) for r in c.fetchall()]

def first(db,sql,args=()):
    r=rows(db,sql,args); return r[0] if r else None

def current_bundles():
    m=load(APP/'update_manifest.json',{}); current=''
    for a in m.get('artifacts',[]) if isinstance(m,dict) else []:
        if isinstance(a,dict) and a.get('id')=='app_ready_betting_bundle': current=Path(str(a.get('path') or '')).name
    p=[x for x in APP.glob('app_ready_betting_bundle-*.zip') if x.is_file()]
    return sorted(p,key=lambda x:(x.name!=current,-x.stat().st_mtime))[:2]

def final_candidates(db,gid):
    # Schema v8 measures the retired-market-free precision product. Only a generation built with
    # the exact v8 contract may trust persisted precision_eligible. Older bundles are re-evaluated
    # from immutable recommendation-time evidence using the same independent-confidence rule.
    generation=first(db,"SELECT rules_fingerprint FROM prepared_pattern_generation WHERE generation_id=? LIMIT 1",(gid,))
    rules=str((generation or {}).get('rules_fingerprint') or '')
    retired_sql=",".join("?" for _ in RETIRED_SUB_MARKET_KEYS)
    retired_args=tuple(sorted(RETIRED_SUB_MARKET_KEYS))
    candidate_columns={
        str(row[1])
        for row in db.execute("PRAGMA table_info(prepared_pattern_candidates)").fetchall()
    }
    if "precision_eligible" in candidate_columns and rules==RULES_FINGERPRINT:
        src=rows(
            db,
            "SELECT c.* FROM prepared_pattern_candidates c "
            "JOIN prepared_selections s "
            "ON s.competition_id=c.competition_id "
            "AND s.snapshot_version=c.snapshot_version "
            "AND s.selection_key=c.selection_key "
            "WHERE c.generation_id=? AND c.precision_eligible=1 "
            f"AND COALESCE(s.identity_sub_market_key,'') NOT IN ({retired_sql}) "
            "ORDER BY c.evidence_score DESC,c.source_order ASC",
            (gid,*retired_args),
        )
    else:
        src=rows(
            db,
            "SELECT c.* FROM prepared_pattern_candidates c "
            "JOIN prepared_selections s "
            "ON s.competition_id=c.competition_id "
            "AND s.snapshot_version=c.snapshot_version "
            "AND s.selection_key=c.selection_key "
            "WHERE c.generation_id=? AND c.recommendation_eligible=1 "
            "AND c.policy_premium_eligible=1 "
            "AND UPPER(TRIM(COALESCE(c.value_tier,'')))='STRONG_VALUE' "
            "AND c.selection_odd>=1.50 "
            f"AND COALESCE(s.identity_sub_market_key,'') NOT IN ({retired_sql}) "
            "AND COALESCE("
            "s.opponent_model_probability,"
            "CASE WHEN s.value_signal_conservative_probability IS NOT NULL "
            "AND s.value_signal_market_probability IS NOT NULL "
            "AND s.value_signal_conservative_probability>s.value_signal_market_probability "
            "AND ABS(s.value_signal_conservative_probability-s.bm_posterior_probability)>0.000000001 "
            "THEN s.value_signal_conservative_probability END"
            ")>=0.75 "
            "ORDER BY c.evidence_score DESC,c.source_order ASC",
            (gid,*retired_args),
        )
    exact={}
    for r in src:
        k=(str(r.get('competition_id') or ''),str(r.get('match_key') or ''),str(r.get('exact_recommendation_key') or ''))
        if k not in exact or num(r.get('selection_score'))>num(exact[k].get('selection_score')): exact[k]=r
    best={}
    for r in exact.values():
        k=(str(r.get('competition_id') or ''),str(r.get('match_key') or ''))
        rank=(num(r.get('selection_score')),num(r.get('strict_hit_rate')),intval(r.get('strict_sample')),num(r.get('selection_odd')))
        old=best.get(k); oldrank=None if old is None else (num(old.get('selection_score')),num(old.get('strict_hit_rate')),intval(old.get('strict_sample')),num(old.get('selection_odd')))
        if old is None or rank>oldrank: best[k]=r
    return list(best.values())

def runtime_match_key(match):
    return '|'.join((
        str(match.get('date') or '').strip(),
        str(match.get('homeTeam') or '').strip(),
        str(match.get('awayTeam') or '').strip(),
    ))

def kickoff_ms(m):
    for k in ('kickoffEpochMillis','kickoff_epoch_ms','kickoffTimestamp','timestamp'):
        try:
            x=int(m.get(k));
            if x>10_000_000_000:return x
            if x>1_000_000_000:return x*1000
        except Exception: pass
    text=str(m.get('kickoff') or '').strip(); day=str(m.get('date') or '')[:10]
    if text:
        try:
            x=dt.datetime.fromisoformat(text.replace('Z','+00:00')); x=x if x.tzinfo else x.replace(tzinfo=ATHENS); return int(x.timestamp()*1000)
        except Exception: pass
    if day and text:
        for fmt in ('%H:%M','%H:%M:%S'):
            try:return int(dt.datetime.combine(dt.date.fromisoformat(day),dt.datetime.strptime(text,fmt).time(),tzinfo=ATHENS).timestamp()*1000)
            except Exception: pass
    return None

def nullable(v):
    x=num(v); return None if x==float('-inf') else x

def tier(v):
    s=str(v or '').strip().upper(); return {'STRONG_VALUE':'Strong Value','VALUE':'Solid Value','LEAN_VALUE':'Marginal Value','NONE':'No signal','':'No signal'}.get(s,str(v or 'No signal').replace('_',' '))

def valid_fixture_identity(row):
    home=row.get('homeNames') if isinstance(row.get('homeNames'),list) else [row.get('homeTeam')]
    away=row.get('awayNames') if isinstance(row.get('awayNames'),list) else [row.get('awayTeam')]
    hk={live.normalize_team(x) for x in home if live.normalize_team(x)}
    ak={live.normalize_team(x) for x in away if live.normalize_team(x)}
    return bool(hk and ak and hk.isdisjoint(ak))

def extract(bundle,target=None):
    with tempfile.TemporaryDirectory() as td:
        dbp=Path(td)/'db.sqlite'
        try:
            with zipfile.ZipFile(bundle) as z:
                with z.open('databases/statmaker_prepared_betting.db') as s,dbp.open('wb') as d:
                    while True:
                        b=s.read(1024*1024)
                        if not b:break
                        d.write(b)
        except Exception:return []
        db=sqlite3.connect(f'file:{dbp}?mode=ro',uri=True)
        rejected_identity=0
        try:
            g=first(db,"SELECT * FROM prepared_pattern_generation WHERE state='ready' ORDER BY built_at_ms DESC LIMIT 1")
            if not g:return []
            gid=str(g.get('generation_id') or ''); built=intval(g.get('built_at_ms'))
            out=[]
            for c in final_candidates(db,gid):
                day=str(c.get('local_date') or '')[:10]
                if target and day!=target:continue
                comp=str(c.get('competition_id') or ''); snap=str(c.get('snapshot_version') or ''); sk=str(c.get('selection_key') or '')
                s=first(db,"SELECT * FROM prepared_selections WHERE competition_id=? AND snapshot_version=? AND selection_key=? LIMIT 1",(comp,snap,sk))
                if not s:continue
                pmk=str(s.get('match_key') or '')
                mr=first(db,"SELECT payload FROM prepared_matches WHERE competition_id=? AND snapshot_version=? AND match_key=? LIMIT 1",(comp,snap,pmk))
                if not mr:continue
                try:m=json.loads(str(mr.get('payload') or '{}'))
                except Exception:continue

                # Hard fixture-identity invariant. Candidate identity is the runtime
                # date|home|away key; selection -> prepared_match must resolve to that exact same
                # fixture. A selection from another match is never allowed to inherit c.match_key.
                candidate_match_key=str(c.get('match_key') or '').strip()
                payload_match_key=runtime_match_key(m)
                if not candidate_match_key or candidate_match_key != payload_match_key:
                    rejected_identity+=1
                    continue

                day=day or str(m.get('date') or '')[:10]
                if target and day!=target:continue
                ko=kickoff_ms(m)
                if ko is not None and built>=ko-SAFETY_MS:continue
                if ko is None:
                    gd=dt.datetime.fromtimestamp(built/1000,tz=dt.timezone.utc).astimezone(ATHENS).date().isoformat() if built else ''
                    if not day or day<=gd:continue
                sub=str(s.get('identity_sub_market_key') or '')
                if sub in RETIRED_SUB_MARKET_KEYS:continue
                hp=list(live._names_from_match_payload(m,'home')); ap=list(live._names_from_match_payload(m,'away'))
                if not hp or not ap:continue
                identity_probe={'homeNames':hp,'awayNames':ap,'homeTeam':str(m.get('homeTeam') or ''),'awayTeam':str(m.get('awayTeam') or '')}
                if not valid_fixture_identity(identity_probe):continue
                mp=nullable(s.get('opponent_model_probability')); post=nullable(s.get('bm_posterior_probability'))
                conservative=nullable(s.get('value_signal_conservative_probability'))
                signal_market=nullable(s.get('value_signal_market_probability'))
                independent=mp
                prediction_source='OPPONENT_ADJUSTED' if mp is not None else None
                if independent is None and conservative is not None and signal_market is not None and post is not None and conservative>signal_market and abs(conservative-post)>0.000000001:
                    independent=conservative
                    prediction_source='STRONG_PATTERN'
                if independent is None or independent<0.75:continue
                out.append({
                  'generationId':gid,'generationBuiltAtMs':built,'competitionId':comp,'snapshotVersion':snap,'selectionKey':sk,
                  'matchKey':candidate_match_key,'localDate':day,'leagueCode':str(c.get('league_code') or m.get('leagueCode') or '').upper(),
                  'competition':str(m.get('competition') or ''),'season':str(m.get('season') or ''),'homeTeam':str(m.get('homeTeam') or ''),'awayTeam':str(m.get('awayTeam') or ''),
                  'apiFixtureId':live._fixture_id_from_match_payload(m),'kickoffEpochMillis':ko,'homeNames':hp,'awayNames':ap,
                  'market':str(s.get('selection_market') or ''),'selection':str(s.get('selection_name') or ''),'team':s.get('selection_team'),'line':nullable(s.get('selection_line')),'odd':nullable(s.get('selection_odd')),
                  'broadGroup':s.get('identity_broad_group'),'family':s.get('identity_family'),'subMarketKey':sub,'teamSide':s.get('identity_team_side'),'selectionSide':s.get('identity_selection_side'),'selectionToken':s.get('identity_selection_token'),
                  'marketProbability':nullable(s.get('bm_market_probability')),'modelProbability':independent,'reliability':nullable(s.get('bm_sample_reliability')),'valueTier':tier(c.get('value_tier')),
                  'opponentAdjustedRequired':bool(intval(s.get('opponent_adjusted_required'))),'baseModelProbability':nullable(s.get('opponent_base_model_probability')),
                  'withoutFavoriteProbability':nullable(s.get('opponent_without_favorite_probability')),'withoutXgProbability':nullable(s.get('opponent_without_xg_probability')),'withoutFatigueProbability':nullable(s.get('opponent_without_fatigue_probability')),
                  'withoutInjuriesProbability':nullable(s.get('opponent_without_injuries_probability')),'withoutLineupProbability':nullable(s.get('opponent_without_lineup_probability')),'withoutFormationProbability':nullable(s.get('opponent_without_formation_probability')),'withoutSquadTurnoverProbability':nullable(s.get('opponent_without_squad_turnover_probability')),
                  'modifierProfile':s.get('opponent_modifier_profile'),'predictionSource':prediction_source,'requiredKind':live.SUBMARKET_REQUIREMENT.get(sub,'unsupported')})
            if rejected_identity:
                print(f"CANONICAL_LEDGER_IDENTITY_REJECTED bundle={bundle.name} rows={rejected_identity}")
            return out
        except sqlite3.Error:return []
        finally:db.close()

def merge(seq):
    d={}
    for r in seq:
        if not valid_fixture_identity(r):continue
        k=(str(r.get('competitionId') or ''),str(r.get('localDate') or '')[:10],str(r.get('matchKey') or ''))
        if not all(k):continue
        if k not in d or intval(r.get('generationBuiltAtMs'))>=intval(d[k].get('generationBuiltAtMs')):d[k]=r
    return list(d.values())

def git_show(commit,path,out=None):
    try:
        if out:
            with Path(out).open('wb') as h: subprocess.run(['git','show',f'{commit}:{path}'],cwd=ROOT,check=True,stdout=h,stderr=subprocess.DEVNULL)
            return b''
        return subprocess.run(['git','show',f'{commit}:{path}'],cwd=ROOT,check=True,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL).stdout
    except Exception:return None

def history(day):
    start=(day-dt.timedelta(days=1)).isoformat()+'T00:00:00Z'; end=day.isoformat()+'T23:59:59Z'
    try: commits=subprocess.run(['git','log','--format=%H',f'--since={start}',f'--until={end}','--','data/statmaker/app_ready/update_manifest.json'],cwd=ROOT,check=True,text=True,stdout=subprocess.PIPE).stdout.splitlines()[:24]
    except Exception:return [],0
    out=[]; n=0
    for commit in commits:
        raw=git_show(commit,'data/statmaker/app_ready/update_manifest.json')
        if not raw:continue
        try: man=json.loads(raw.decode()); path=next(str(a.get('path')) for a in man.get('artifacts',[]) if a.get('id')=='app_ready_betting_bundle')
        except Exception:continue
        with tempfile.TemporaryDirectory() as td:
            z=Path(td)/'b.zip'
            if git_show(commit,path,z) is None:continue
            n+=1; out.extend(extract(z,day.isoformat()))
    return merge(out),n

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--backfill-dates',type=int,default=30); a=ap.parse_args(); limit=max(0,min(30,a.backfill_dates))
    today=dt.datetime.now(dt.timezone.utc).astimezone(ATHENS).date(); low=today-dt.timedelta(days=RETENTION); high=today+dt.timedelta(days=14)
    old=load(LEDGER,{})
    invalidated=invalidated_match_keys(low,high)

    # Schema v8 retires all Asian/handicap markets and requires independent predictive confidence.
    # Older ledger rows are never carried forward blindly; historical bundles are re-materialized.
    existing=[]
    if isinstance(old,dict) and intval(old.get('schemaVersion'))>=SCHEMA_VERSION:
        for r in old.get('entries',[]):
            if isinstance(r,dict) and r.get('market') and r.get('selection') and low.isoformat()<=str(r.get('localDate') or '')[:10]<=high.isoformat() and valid_fixture_identity(r):existing.append(dict(r))
    old_schema=intval(old.get('schemaVersion')) if isinstance(old,dict) else 0
    done={str(x)[:10] for x in old.get('backfilledDates',[]) if isinstance(old,dict)} if old_schema>=SCHEMA_VERSION else set()
    cb=current_bundles(); current=[]
    for b in cb:current.extend(extract(b))
    allr=[r for r in [*existing,*current] if str(r.get('matchKey') or '').strip() not in invalidated]; processed=[]; hb=hr=0
    for off in range(1,RETENTION+1):
        if len(processed)>=limit:break
        day=today-dt.timedelta(days=off); iso=day.isoformat()
        if iso in done:continue
        r,n=history(day)
        # A completed backfill day is an authoritative replacement, not an append. This removes
        # any stale/corrupt identity row from an older ledger generation.
        allr=[x for x in allr if str(x.get('localDate') or '')[:10]!=iso]
        allr.extend(r); hb+=n; hr+=len(r); done.add(iso); processed.append(iso)
    entries=[r for r in merge(allr) if low.isoformat()<=str(r.get('localDate') or '')[:10]<=high.isoformat()]
    sem={'schemaVersion':SCHEMA_VERSION,'retentionDays':RETENTION,'source':'canonical-app-ready-default-independent-precision-no-asian-handicap-v8','backfilledDates':sorted(x for x in done if low.isoformat()<=x<=today.isoformat()),'invalidatedMatchKeys':sorted(invalidated),'entries':sorted(entries,key=lambda r:(str(r.get('localDate') or ''),str(r.get('matchKey') or '')))}
    prior=dict(old) if isinstance(old,dict) else {}; prior.pop('generatedAt',None); changed=prior!=sem
    if changed:
        tmp=LEDGER.with_suffix('.json.tmp'); tmp.write_text(json.dumps({'generatedAt':dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00','Z'),**sem},ensure_ascii=False,indent=2)+'\n',encoding='utf-8'); tmp.replace(LEDGER)
    counts={}
    for r in entries:counts[str(r.get('localDate') or '')[:10]]=counts.get(str(r.get('localDate') or '')[:10],0)+1
    print(f"canonical-ledger-v8 currentBundles={len(cb)} currentRows={len(merge(current))} backfilledDates={','.join(processed) or '-'} historyBundles={hb} historyRows={hr} ledgerRows={len(entries)} changed={changed} dateCounts={json.dumps(counts,sort_keys=True)}")
    return 0
if __name__=='__main__':raise SystemExit(main())
