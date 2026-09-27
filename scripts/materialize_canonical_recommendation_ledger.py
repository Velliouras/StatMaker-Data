#!/usr/bin/env python3
from __future__ import annotations
import argparse, datetime as dt, json, math, sqlite3, subprocess, tempfile, zipfile
from pathlib import Path
from zoneinfo import ZoneInfo
import refresh_live_settlements as live

ROOT=Path(__file__).resolve().parents[1]
APP_PROD=ROOT/'data/statmaker/app_ready'
APP_UAT=ROOT/'data/statmaker/app_ready_uat/probability-first-v1'
LEDGER_PROD=ROOT/'data/statmaker/canonical_recommendation_ledger.json'
LEDGER_UAT=ROOT/'data/statmaker/canonical_recommendation_ledger_uat.json'
VALIDITY=ROOT/'data/statmaker/fixture_validity.json'
APP=APP_PROD
LEDGER=LEDGER_PROD
MANIFEST_REL='data/statmaker/app_ready/update_manifest.json'
LEDGER_SOURCE='canonical-app-ready-probability-first-strong-singles-ledger-v9'
MODE_LABEL='prod'
ATHENS=ZoneInfo('Europe/Athens'); RETENTION=30; SAFETY_MS=60000; SCHEMA_VERSION=9

# Permanent product retirement. Legacy parsers may still recognize these identities for old
# persisted rows, but they must never re-enter the canonical recommendation/performance ledger.
RETIRED_RAW_MARKETS={
    'ASIAN_HANDICAP','ASIAN_HANDICAP_1H','ASIAN_GOALS','ASIAN_GOALS_1H',
    'ASIAN_CORNERS','ASIAN_CORNER_HANDICAP','CORNER_HANDICAP'
}
RETIRED_SUBMARKETS={
    'RESULT_ASIAN_HANDICAP','HT_RESULT_ASIAN_HANDICAP','ASIAN_MATCH_GOALS_TOTAL',
    'ASIAN_FIRST_HALF_GOALS_TOTAL','ASIAN_MATCH_CORNERS_TOTAL',
    'ASIAN_CORNER_HANDICAP','CORNER_HANDICAP'
}

def retired_market(raw_market, sub_market):
    return (
        str(raw_market or '').strip().upper() in RETIRED_RAW_MARKETS
        or str(sub_market or '').strip().upper() in RETIRED_SUBMARKETS
    )

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

def _clamp01(value):
    return max(0.0,min(1.0,float(value)))

def _valid_probability(value):
    x=num(value)
    return x!=float('-inf') and 0.01<=x<=0.99

def sane_exact_odd(identity_family, selection_side, line, odd):
    if odd <= 1.0 or odd > 100.0:
        return False
    if identity_family in {"MATCH_GOALS", "ASIAN_GOALS"}:
        if selection_side == "OVER" and line is not None and line <= 1.5 and odd >= 5.0:
            return False
        if selection_side == "OVER" and line is not None and line <= 2.5 and odd >= 8.0:
            return False
        if selection_side == "UNDER" and line is not None and line >= 4.5 and odd >= 8.0:
            return False
    elif identity_family == "TEAM_GOALS":
        if selection_side == "OVER" and line is not None and line <= 0.5 and odd >= 5.0:
            return False
        if selection_side == "OVER" and line is not None and line <= 1.5 and odd >= 10.0:
            return False
    elif identity_family == "BTTS":
        return odd < 8.0
    elif identity_family in {"FIRST_HALF_GOALS", "TEAM_FIRST_HALF_GOALS", "ASIAN_GOALS_1H"}:
        if selection_side == "OVER" and line is not None and line <= 0.5 and odd >= 6.0:
            return False
    elif identity_family in {"MATCH_CORNERS", "ASIAN_CORNERS"}:
        if line is not None and line <= 8.5 and selection_side == "OVER" and odd >= 8.0:
            return False
    elif identity_family == "TEAM_CORNERS":
        if line is not None and line <= 3.5 and selection_side == "OVER" and odd >= 8.0:
            return False
    elif identity_family in {"Correct Score", "Half-time / Full-time", "Winning Margin"}:
        return odd <= 40.0
    return True

def _probability_first_rank(row, market_preferred):
    market_probability=num(row.get('bm_market_probability'))
    odd=num(row.get('selection_odd'))
    if not (_valid_probability(market_probability) and odd>1.0):
        return None

    fixture_probability=num(row.get('opponent_model_probability'))
    model_backed=_valid_probability(fixture_probability)
    posterior=num(row.get('bm_posterior_probability'))
    reliability_input=num(row.get('bm_sample_reliability'),0.0)
    historical_fallback_allowed=(
        not model_backed
        and bool(market_preferred)
        and _valid_probability(posterior)
        and posterior>=0.55
        and reliability_input>=0.60
    )
    probability=fixture_probability if model_backed else (posterior if historical_fallback_allowed else float('-inf'))
    if not _valid_probability(probability):
        return None

    edge=probability-market_probability
    expected_value=probability*odd-1.0
    if edge<=0.0 or expected_value<=0.0:
        return None

    overall=_clamp01(row.get('strict_hit_rate') or row.get('bm_hit_rate') or 0.0)
    bits=''.join(ch for ch in str(row.get('historical_outcomes_bits') or '') if ch in '01')
    recent_size=min(10,len(bits))
    if recent_size:
        recent_bits=bits[-recent_size:]
        recent=sum(ch=='1' for ch in recent_bits)/recent_size
    else:
        recent=overall
    if len(bits)>recent_size and recent_size:
        prior_bits=bits[:-recent_size]
        prior=sum(ch=='1' for ch in prior_bits)/len(prior_bits)
    else:
        prior=overall

    trend_adjustment=num(row.get('score_trend_adjustment'),0.0)
    direction_adjustment=0.025 if trend_adjustment>0 else (-0.025 if trend_adjustment<0 else 0.0)
    recent_direction=max(-0.05,min(0.05,(recent-prior)*0.18))
    trend_support=_clamp01(recent*0.58+overall*0.42+direction_adjustment+recent_direction)

    sample=max(0,intval(row.get('strict_sample') or row.get('bm_sample')))
    sample_maturity=sample/(sample+12.0)
    reliability=_clamp01(_clamp01(reliability_input)*0.65+sample_maturity*0.35)

    if model_backed:
        context_values=[
            row.get('opponent_without_favorite_probability'),
            row.get('opponent_without_xg_probability'),
            row.get('opponent_without_fatigue_probability'),
            row.get('opponent_without_injuries_probability'),
            row.get('opponent_without_lineup_probability'),
            row.get('opponent_without_formation_probability'),
            row.get('opponent_without_squad_turnover_probability'),
        ]
        coverage=sum(1 for value in context_values if _valid_probability(value))/7.0
        base=row.get('opponent_base_model_probability')
        shift=probability-num(base) if _valid_probability(base) else 0.0
        direction_support=_clamp01(0.50+shift/0.16)
        context_support=_clamp01(0.62+coverage*0.25+direction_support*0.13)
    else:
        context_support=0.30

    normalized_edge=_clamp01((edge-0.04)/0.12)
    normalized_ev=_clamp01((expected_value-0.05)/0.25)
    value_support=_clamp01(normalized_ev*0.55+normalized_edge*0.45)

    ranking_score=_clamp01(
        probability*0.60+
        trend_support*0.18+
        reliability*0.10+
        context_support*0.08+
        value_support*0.04
    )
    return (
        ranking_score,
        probability,
        trend_support,
        reliability,
        -abs(odd-2.0),
    )

def final_candidates(db,gid,target=None):
    # UAT Performance/Daily Outcomes must start from the exact same prepared_pattern_candidates
    # generation consumed by the Android probability-first Strong query. The UAT App-Ready bundle
    # already contains the expanded candidate universe; never reconstruct it from prepared_selections.
    if MODE_LABEL=='uat':
        date_clause=" AND c.local_date=?" if target else ""
        args=(gid,target) if target else (gid,)
        src=rows(
            db,
            f"""
            SELECT c.*,
                   s.match_key AS prepared_match_key,
                   s.selection_market,
                   s.selection_name,
                   s.selection_team,
                   s.selection_line,
                   s.bm_market_probability,
                   s.bm_hit_rate,
                   s.bm_sample,
                   s.bm_posterior_probability,
                   s.bm_sample_reliability,
                   s.historical_outcomes_bits,
                   s.score_value,
                   s.score_tier,
                   s.score_trend_adjustment,
                   s.identity_broad_group,
                   s.identity_family,
                   s.identity_sub_market_key,
                   s.identity_team_side,
                   s.identity_line,
                   s.identity_selection_side,
                   s.identity_source_market,
                   s.identity_team,
                   s.identity_selection_token,
                   s.opponent_adjusted_required,
                   s.opponent_model_probability,
                   s.opponent_base_model_probability,
                   s.opponent_without_favorite_probability,
                   s.opponent_without_xg_probability,
                   s.opponent_without_fatigue_probability,
                   s.opponent_without_injuries_probability,
                   s.opponent_without_lineup_probability,
                   s.opponent_without_formation_probability,
                   s.opponent_without_squad_turnover_probability
            FROM prepared_pattern_candidates c
            JOIN prepared_selections s
              ON s.competition_id=c.competition_id
             AND s.snapshot_version=c.snapshot_version
             AND s.selection_key=c.selection_key
            WHERE c.generation_id=?
              {date_clause}
              AND c.selection_odd>=1.50
              AND c.strict_sample>=10
              AND c.strict_hit_rate>=0.70
              AND c.evidence_score>=0.54
              AND c.recommendation_eligible=1
            ORDER BY c.source_order ASC
            """,
            args,
        )
    else:
        # PROD keeps its own canonical source contract. This branch is intentionally separate from
        # UAT so testing a UAT engine never changes the production Performance cohort.
        snapshots=rows(
            db,
            "SELECT competition_id, snapshot_version FROM prepared_snapshot_meta WHERE state='ready'",
        )
        snapshot_map={str(r.get('competition_id') or ''):str(r.get('snapshot_version') or '') for r in snapshots}
        src=[]
        for comp,snap in snapshot_map.items():
            match_payloads={}
            for match_row in rows(
                db,
                """
                SELECT match_key,payload
                FROM prepared_matches
                WHERE competition_id=? AND snapshot_version=?
                """,
                (comp,snap),
            ):
                try:
                    match_payloads[str(match_row.get('match_key') or '')]=json.loads(str(match_row.get('payload') or '{}'))
                except Exception:
                    pass

            date_clause=" AND s.local_date=?" if target else ""
            selection_args=(comp,snap,target) if target else (comp,snap)
            selection_rows=rows(
                db,
                f"""
                SELECT s.*,
                       s.match_key AS prepared_match_key
                FROM prepared_selections s
                WHERE s.competition_id=? AND s.snapshot_version=?
                  {date_clause}
                  AND s.selection_odd>=1.50
                  AND s.bm_sample>=10
                  AND s.bm_hit_rate>=0.70
                  AND s.score_value>=0.54
                ORDER BY s.rowid ASC
                """,
                selection_args,
            )
            for r in selection_rows:
                odd=num(r.get('selection_odd'))
                family=str(r.get('identity_family') or '')
                side=str(r.get('identity_selection_side') or '')
                line=nullable(r.get('identity_line'))
                retired_text=" ".join(
                    str(r.get(k) or "")
                    for k in ("selection_market","identity_family","identity_sub_market_key","identity_source_market")
                ).upper()
                if "ASIAN" in retired_text or "HANDICAP" in retired_text:
                    continue
                if odd>10.0 or not sane_exact_odd(family,side,line,odd):
                    continue
                payload=match_payloads.get(str(r.get('prepared_match_key') or ''))
                if not payload:
                    continue
                runtime_key=runtime_match_key(payload)
                if not runtime_key:
                    continue
                line_text="" if line is None else str(float(line))
                rr=dict(r)
                rr.update({
                    'competition_id':comp,
                    'snapshot_version':snap,
                    'selection_key':str(r.get('selection_key') or ''),
                    'match_key':runtime_key,
                    'local_date':str(r.get('local_date') or payload.get('date') or '')[:10],
                    'league_code':str(payload.get('leagueCode') or payload.get('competition') or ''),
                    'selection_odd':odd,
                    'strict_sample':intval(r.get('bm_sample')),
                    'strict_hit_rate':num(r.get('bm_hit_rate')),
                    'evidence_score':num(r.get('score_value')),
                    'exact_recommendation_key':(
                        f"{r.get('identity_broad_group') or ''}|{r.get('identity_sub_market_key') or ''}|"
                        f"{side}|{line_text}|{r.get('identity_team') or ''}|{r.get('identity_selection_token') or ''}"
                    ),
                })
                src.append(rr)

    three_way={
        'RESULT_1X2','HT_RESULT_1X2','CORNER_RESULT_1X2',
        'SHOTS_RESULT_1X2','SOT_RESULT_1X2'
    }
    favorite_side={}
    contests={
        (
            str(r.get('competition_id') or ''),
            str(r.get('snapshot_version') or ''),
            str(r.get('prepared_match_key') or ''),
            str(r.get('identity_sub_market_key') or ''),
        )
        for r in src
        if str(r.get('identity_sub_market_key') or '') in three_way
    }
    for comp,snap,match_key,sub in contests:
        market_rows=rows(
            db,
            """
            SELECT identity_selection_side, selection_odd
            FROM prepared_selections
            WHERE competition_id=? AND snapshot_version=? AND match_key=?
              AND identity_sub_market_key=?
              AND selection_odd>1.01
            """,
            (comp,snap,match_key,sub),
        )
        market_rows=[
            r for r in market_rows
            if str(r.get('identity_selection_side') or '') in {'HOME','DRAW','AWAY'}
            and num(r.get('selection_odd'))!=float('-inf')
        ]
        if len({str(r.get('identity_selection_side') or '') for r in market_rows})>=3:
            favorite_side[(comp,snap,match_key,sub)]=str(
                min(market_rows,key=lambda r:num(r.get('selection_odd'))).get('identity_selection_side') or ''
            )

    eligible=[]
    for r in src:
        sub=str(r.get('identity_sub_market_key') or '')
        side=str(r.get('identity_selection_side') or '')
        market_probability=num(r.get('bm_market_probability'))
        if not _valid_probability(market_probability):
            continue
        if sub in three_way:
            key=(
                str(r.get('competition_id') or ''),
                str(r.get('snapshot_version') or ''),
                str(r.get('prepared_match_key') or ''),
                sub,
            )
            market_preferred=(favorite_side.get(key)==side)
        else:
            market_preferred=(market_probability>=0.50)
        if not market_preferred:
            continue
        rank=_probability_first_rank(r, market_preferred)
        if rank is None:
            continue
        rr=dict(r)
        rr['_probability_first_rank']=rank
        rr['value_tier']='STRONG_VALUE'
        eligible.append(rr)

    exact={}
    for r in eligible:
        k=(
            str(r.get('competition_id') or ''),
            str(r.get('match_key') or ''),
            str(r.get('exact_recommendation_key') or ''),
        )
        old=exact.get(k)
        if old is None or r['_probability_first_rank']>old['_probability_first_rank']:
            exact[k]=r

    best={}
    for r in exact.values():
        k=(str(r.get('competition_id') or ''),str(r.get('match_key') or ''))
        old=best.get(k)
        if old is None or r['_probability_first_rank']>old['_probability_first_rank']:
            best[k]=r
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
        diagnostic_pre_by_day={}
        diagnostic_after_safety_by_day={}
        diagnostic_after_kickoff_by_day={}
        diagnostic_missing_kickoff_by_day={}
        try:
            g=first(db,"SELECT * FROM prepared_pattern_generation WHERE state='ready' ORDER BY built_at_ms DESC LIMIT 1")
            if not g:return []
            gid=str(g.get('generation_id') or ''); built=intval(g.get('built_at_ms'))
            out=[]
            for c in final_candidates(db,gid,target):
                day=str(c.get('local_date') or '')[:10]
                if target and day!=target:continue
                diagnostic_pre_by_day[day]=diagnostic_pre_by_day.get(day,0)+1
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
                if ko is not None and built>=ko-SAFETY_MS:
                    diagnostic_after_kickoff_by_day[day]=diagnostic_after_kickoff_by_day.get(day,0)+1
                    continue
                if ko is None:
                    diagnostic_missing_kickoff_by_day[day]=diagnostic_missing_kickoff_by_day.get(day,0)+1
                    gd=dt.datetime.fromtimestamp(built/1000,tz=dt.timezone.utc).astimezone(ATHENS).date().isoformat() if built else ''
                    if not day or day<=gd:continue
                diagnostic_after_safety_by_day[day]=diagnostic_after_safety_by_day.get(day,0)+1
                sub=str(s.get('identity_sub_market_key') or '')
                if retired_market(s.get('selection_market'), sub):
                    continue
                hp=list(live._names_from_match_payload(m,'home')); ap=list(live._names_from_match_payload(m,'away'))
                if not hp or not ap:continue
                identity_probe={'homeNames':hp,'awayNames':ap,'homeTeam':str(m.get('homeTeam') or ''),'awayTeam':str(m.get('awayTeam') or '')}
                if not valid_fixture_identity(identity_probe):continue
                mp=nullable(s.get('opponent_model_probability')); post=nullable(s.get('bm_posterior_probability'))
                out.append({
                  'generationId':gid,'generationBuiltAtMs':built,'competitionId':comp,'snapshotVersion':snap,'selectionKey':sk,
                  'matchKey':candidate_match_key,'localDate':day,'leagueCode':str(c.get('league_code') or m.get('leagueCode') or '').upper(),
                  'competition':str(m.get('competition') or ''),'season':str(m.get('season') or ''),'homeTeam':str(m.get('homeTeam') or ''),'awayTeam':str(m.get('awayTeam') or ''),
                  'apiFixtureId':live._fixture_id_from_match_payload(m),'kickoffEpochMillis':ko,'homeNames':hp,'awayNames':ap,
                  'market':str(s.get('selection_market') or ''),'selection':str(s.get('selection_name') or ''),'team':s.get('selection_team'),'line':nullable(s.get('selection_line')),'odd':nullable(s.get('selection_odd')),
                  'broadGroup':s.get('identity_broad_group'),'family':s.get('identity_family'),'subMarketKey':sub,'teamSide':s.get('identity_team_side'),'selectionSide':s.get('identity_selection_side'),'selectionToken':s.get('identity_selection_token'),
                  'marketProbability':nullable(s.get('bm_market_probability')),'modelProbability':mp if mp is not None else post,'reliability':nullable(s.get('bm_sample_reliability')),'valueTier':tier(c.get('value_tier')),
                  'opponentAdjustedRequired':bool(intval(s.get('opponent_adjusted_required'))),'baseModelProbability':nullable(s.get('opponent_base_model_probability')),
                  'withoutFavoriteProbability':nullable(s.get('opponent_without_favorite_probability')),'withoutXgProbability':nullable(s.get('opponent_without_xg_probability')),'withoutFatigueProbability':nullable(s.get('opponent_without_fatigue_probability')),
                  'withoutInjuriesProbability':nullable(s.get('opponent_without_injuries_probability')),'withoutLineupProbability':nullable(s.get('opponent_without_lineup_probability')),'withoutFormationProbability':nullable(s.get('opponent_without_formation_probability')),'withoutSquadTurnoverProbability':nullable(s.get('opponent_without_squad_turnover_probability')),
                  'modifierProfile':s.get('opponent_modifier_profile'),'predictionSource':'OPPONENT_ADJUSTED' if mp is not None else 'BOOKMAKER_POSTERIOR','requiredKind':live.SUBMARKET_REQUIREMENT.get(sub,'unsupported')})
            if rejected_identity:
                print(f"CANONICAL_LEDGER_IDENTITY_REJECTED bundle={bundle.name} rows={rejected_identity}")
            print(
                "CANONICAL_LEDGER_EXTRACT_FUNNEL",
                f"bundle={bundle.name}",
                "pre="+json.dumps(diagnostic_pre_by_day,sort_keys=True),
                "afterSafety="+json.dumps(diagnostic_after_safety_by_day,sort_keys=True),
                "startedAtBuild="+json.dumps(diagnostic_after_kickoff_by_day,sort_keys=True),
                "missingKickoff="+json.dumps(diagnostic_missing_kickoff_by_day,sort_keys=True),
            )
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

def history_from_manifest(day, manifest_rel):
    start=(day-dt.timedelta(days=1)).isoformat()+'T00:00:00Z'; end=day.isoformat()+'T23:59:59Z'
    try:
        commits=subprocess.run(
            ['git','log','--format=%H',f'--since={start}',f'--until={end}','--',manifest_rel],
            cwd=ROOT,check=True,text=True,stdout=subprocess.PIPE
        ).stdout.splitlines()[:24]
    except Exception:
        return [],0
    out=[]; n=0
    for commit in commits:
        raw=git_show(commit,manifest_rel)
        if not raw:continue
        try:
            man=json.loads(raw.decode())
            path=next(str(a.get('path')) for a in man.get('artifacts',[]) if a.get('id')=='app_ready_betting_bundle')
        except Exception:
            continue
        with tempfile.TemporaryDirectory() as td:
            z=Path(td)/'b.zip'
            if git_show(commit,path,z) is None:continue
            n+=1; out.extend(extract(z,day.isoformat()))
    return merge(out),n

def history(day):
    rows_for_day,bundles=history_from_manifest(day,MANIFEST_REL)
    if rows_for_day or MODE_LABEL!='uat':
        return rows_for_day,bundles
    # Before the UAT-specific snapshot existed, reconstruct the same current Strong contract
    # from the historical PROD prepared snapshots rather than inventing or carrying old rows.
    fallback_rows,fallback_bundles=history_from_manifest(
        day,
        'data/statmaker/app_ready/update_manifest.json'
    )
    return fallback_rows,bundles+fallback_bundles

def main():
    global APP,LEDGER,MANIFEST_REL,LEDGER_SOURCE,MODE_LABEL,SCHEMA_VERSION
    ap=argparse.ArgumentParser()
    ap.add_argument('--backfill-dates',type=int,default=30)
    ap.add_argument('--uat',action='store_true')
    a=ap.parse_args()
    if a.uat:
        APP=APP_UAT
        LEDGER=LEDGER_UAT
        MANIFEST_REL='data/statmaker/app_ready_uat/probability-first-v1/update_manifest.json'
        LEDGER_SOURCE='canonical-uat-app-ready-probability-first-strong-singles-ledger-v10'
        MODE_LABEL='uat'
        SCHEMA_VERSION=10
    limit=max(0,min(30,a.backfill_dates))
    today=dt.datetime.now(dt.timezone.utc).astimezone(ATHENS).date(); low=today-dt.timedelta(days=RETENTION); high=today+dt.timedelta(days=14)
    old=load(LEDGER,{})
    invalidated=invalidated_match_keys(low,high)

    # Schema v9 re-materializes the full retained history with the current probability-first
    # Strong Singles contract, including the reliable posterior fallback. Never carry forward
    # v7 identities because the recommendation universe can change under the new contract.
    existing=[]
    if isinstance(old,dict) and intval(old.get('schemaVersion'))>=SCHEMA_VERSION:
        for r in old.get('entries',[]):
            if (
                isinstance(r,dict)
                and r.get('market')
                and r.get('selection')
                and low.isoformat()<=str(r.get('localDate') or '')[:10]<=high.isoformat()
                and valid_fixture_identity(r)
                and not retired_market(r.get('market'), r.get('subMarketKey'))
            ):
                existing.append(dict(r))
    old_schema=intval(old.get('schemaVersion')) if isinstance(old,dict) else 0
    done={str(x)[:10] for x in old.get('backfilledDates',[]) if isinstance(old,dict)} if old_schema>=SCHEMA_VERSION else set()
    cb=current_bundles(); current=[]
    for b in cb:current.extend(extract(b))

    # Today's Daily Outcomes must represent recommendations that were genuinely available
    # before kickoff across the whole day, not only the latest snapshot. UAT snapshots are
    # versioned in git, so replay today's manifest history and let extract() enforce the same
    # anti-leakage cutoff for each historical generation. This recovers valid earlier Strong
    # picks without admitting any recommendation first created after kickoff.
    same_day_rows=[]; same_day_bundles=0
    same_day_rows,same_day_bundles=history_from_manifest(today,MANIFEST_REL)
    current.extend(same_day_rows)

    allr=[r for r in [*existing,*current] if str(r.get('matchKey') or '').strip() not in invalidated]; processed=[]; hb=hr=0

    # One-shot PROD history repair for dates that were previously marked backfilled while
    # carrying zero ledger rows. Replay only their immutable PROD manifest history; do not
    # change recommendation rules, thresholds, current Singles or any already-populated day.
    if MODE_LABEL=='prod':
        for iso in ('2026-09-22','2026-09-23','2026-09-24'):
            day=dt.date.fromisoformat(iso)
            if not (low<=day<today):
                continue
            repaired,n=history(day)
            allr=[x for x in allr if str(x.get('localDate') or '')[:10]!=iso]
            allr.extend(repaired)
            hb+=n; hr+=len(repaired); done.add(iso); processed.append(iso)
            print(f"CANONICAL_LEDGER_TARGETED_HISTORY_REPAIR date={iso} bundles={n} rows={len(repaired)}")

    for off in range(1,RETENTION+1):
        if len(processed)>=limit:break
        day=today-dt.timedelta(days=off); iso=day.isoformat()
        if iso in done:continue
        r,n=history(day)
        # A completed backfill day is an authoritative replacement, not an append. This removes
        # any stale/corrupt identity row from an older ledger generation.
        allr=[x for x in allr if str(x.get('localDate') or '')[:10]!=iso]
        allr.extend(r); hb+=n; hr+=len(r); done.add(iso); processed.append(iso)
    entries=[
        r for r in merge(allr)
        if low.isoformat()<=str(r.get('localDate') or '')[:10]<=high.isoformat()
        and not retired_market(r.get('market'), r.get('subMarketKey'))
    ]
    sem={'schemaVersion':SCHEMA_VERSION,'retentionDays':RETENTION,'source':LEDGER_SOURCE,'backfilledDates':sorted(x for x in done if low.isoformat()<=x<=today.isoformat()),'invalidatedMatchKeys':sorted(invalidated),'entries':sorted(entries,key=lambda r:(str(r.get('localDate') or ''),str(r.get('matchKey') or '')))}
    prior=dict(old) if isinstance(old,dict) else {}; prior.pop('generatedAt',None); changed=prior!=sem
    if changed:
        tmp=LEDGER.with_suffix('.json.tmp'); tmp.write_text(json.dumps({'generatedAt':dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00','Z'),**sem},ensure_ascii=False,indent=2)+'\n',encoding='utf-8'); tmp.replace(LEDGER)
    counts={}
    for r in entries:counts[str(r.get('localDate') or '')[:10]]=counts.get(str(r.get('localDate') or '')[:10],0)+1
    print(f"canonical-ledger-{MODE_LABEL}-v{SCHEMA_VERSION} currentBundles={len(cb)} currentRows={len(merge(current))} sameDayHistoryBundles={same_day_bundles} sameDayHistoryRows={len(same_day_rows)} backfilledDates={','.join(processed) or '-'} historyBundles={hb} historyRows={hr} ledgerRows={len(entries)} changed={changed} dateCounts={json.dumps(counts,sort_keys=True)}")
    return 0
if __name__=='__main__':raise SystemExit(main())
