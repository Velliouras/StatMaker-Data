#!/usr/bin/env python3
"""Read-only point-in-time Value-First versus Hybrid retrospective audit.

Counterfactual replay, NOT evidence that these picks were published historically.
No writes to production ledgers, data, snapshots, or Android.
"""
from __future__ import annotations
import argparse,collections,datetime as dt,json,sqlite3,tempfile
from pathlib import Path
import backtest_elo_betting_ab as hist
import materialize_canonical_recommendation_ledger as ledger

ROOT=Path(__file__).resolve().parents[1]
COMPLETED={'WON','LOST','VOID'}

def summarize(picks):
    decided=[p for p in picks if p.get('outcome') in COMPLETED]
    won=sum(p['outcome']=='WON' for p in decided)
    lost=sum(p['outcome']=='LOST' for p in decided)
    void=len(decided)-won-lost
    returned=sum(float(p.get('grossReturn') or 0) for p in decided)
    equity=peak=drawdown=0.0
    for p in sorted(decided,key=lambda v:(v['date'],v.get('matchKey',''),v.get('selectionKey',''))):
        equity+=float(p.get('grossReturn') or 0)-1.0
        peak=max(peak,equity)
        drawdown=max(drawdown,peak-equity)
    return {'picks':len(picks),'graded':len(decided),'unresolved':len(picks)-len(decided),
            'won':won,'lost':lost,'void':void,'hitRate':won/(won+lost) if won+lost else None,
            'roi':(returned-len(decided))/len(decided) if decided else None,
            'netUnits':round(returned-len(decided),4),
            'maxDrawdownUnits':round(drawdown,4),
            'gradedCoverage':len(decided)/len(picks) if picks else None,
            'averageOdds':sum(float(p['odd']) for p in decided)/len(decided) if decided else None}

def odds_band(odd):
    return '1.50–1.79' if odd<1.80 else '1.80–1.99' if odd<2.00 else '2.00–2.49' if odd<2.50 else '2.50+'

def by_group(picks,selector):
    d=collections.defaultdict(list)
    for p in picks:d[str(selector(p))].append(p)
    return {k:summarize(v) for k,v in sorted(d.items())}

def picks_from_historical_db(db_path,day):
    con=sqlite3.connect(db_path)
    con.row_factory=sqlite3.Row
    try:
        generation=hist.latest_generation(con)
        if generation is None:return None,'NO_PREMATCH_GENERATION'
        gid,built=generation
        result={}
        for mode in ('prod-hybrid','uat-hybrid'):
            ledger.MODE_LABEL=mode
            picks=hist.candidate_records(con,gid,built,day)
            for pick in picks:
                row=con.execute('''SELECT market_family FROM prepared_pattern_candidates
                    WHERE competition_id=? AND snapshot_version=? AND selection_key=? LIMIT 1''',
                    (pick['competitionId'],pick['snapshotVersion'],pick['selectionKey'])).fetchone()
                pick['marketFamily']=str(row[0]) if row and row[0] else pick['subMarketKey']
                # Pre-match 1X2 pricing for directional scoring market analysis.
                # Reading the same historical fixture snapshot never uses future data.
                match=con.execute('''SELECT m.payload FROM prepared_selections s
                    JOIN prepared_matches m ON m.competition_id=s.competition_id
                     AND m.snapshot_version=s.snapshot_version AND m.match_key=s.match_key
                    WHERE s.competition_id=? AND s.snapshot_version=? AND s.selection_key=?
                    LIMIT 1''',
                    (pick['competitionId'],pick['snapshotVersion'],pick['selectionKey'])).fetchone()
                prices={}
                if match:
                    try:
                        payload=json.loads(match[0])
                        for odds in payload.get('markets',[]):
                            if odds.get('market')!='1X2': continue
                            side=str(odds.get('selection') or '').strip().upper()
                            if side not in ('HOME','AWAY'): continue
                            raw=odds.get('odd') if odds.get('odd') is not None else odds.get('odds')
                            if raw is not None and float(raw)>1.01: prices[side]=float(raw)
                    except (TypeError,ValueError,KeyError,json.JSONDecodeError):
                        pass
                favourite=min(prices,key=prices.get) if len(prices)==2 else None
                pick['favorite1X2Odd']=prices.get(favourite) if favourite else None
                pick['isFavoriteTeamMarket']=(
                    str(pick.get('teamSide') or '').upper()==favourite
                ) if favourite else None
            result[mode]=picks
        return result,None
    finally:con.close()

def audit(days,snapshot_hour,end_date,output):
    index=hist.SettlementIndex()
    policies={'prod-hybrid':[],'uat-hybrid':[]}
    skipped=[]; processed=[]
    for target in [end_date-dt.timedelta(days=n) for n in reversed(range(days))]:
        day=target.isoformat()
        commits=hist.manifest_commits_for_local_day(target)
        commit=hist.choose_snapshot_commit(target,commits,snapshot_hour)
        if not commit:
            skipped.append({'date':day,'reason':'NO_DATED_SNAPSHOT'});continue
        cutoff=dt.datetime.combine(target,dt.time(hour=snapshot_hour),hist.ATHENS)
        if dt.datetime.fromtimestamp(hist.commit_epoch(commit),hist.ATHENS)>cutoff:
            skipped.append({'date':day,'reason':'SNAPSHOT_AFTER_FIXED_CUTOFF'});continue
        path=hist.bundle_path_at_commit(commit)
        if not path:
            skipped.append({'date':day,'reason':'NO_BETTING_BUNDLE'});continue
        with tempfile.TemporaryDirectory(prefix='value-first-audit-') as temp:
            db=Path(temp)/'prepared.db'
            if not hist.extract_prepared_db(commit,path,db):
                skipped.append({'date':day,'reason':'BUNDLE_NOT_IN_HISTORY'});continue
            if not hist.has_target_candidates(db,day):
                skipped.append({'date':day,'reason':'NO_TARGET_CANDIDATES'});continue
            try:result,error=picks_from_historical_db(db,day)
            except (sqlite3.Error,ValueError,KeyError) as exc:
                result,error=None,'SELECTION_ERROR:'+str(exc)[:130]
            if result is None:
                skipped.append({'date':day,'reason':error});continue
            for mode,picks in result.items():
                policies[mode].extend(hist.settle_picks(picks,index))
            processed.append(day)
            print('VALUE_FIRST_AUDIT_DAY',day,
                'previousHybrid='+str(len(result['prod-hybrid'])),
                'valueFirst='+str(len(result['uat-hybrid'])),flush=True)
    previous=policies['prod-hybrid']; value=policies['uat-hybrid']
    a=summarize(previous); b=summarize(value)
    report={'schemaVersion':1,'generatedAt':dt.datetime.now(dt.timezone.utc).isoformat(),
      'classification':'EXPLORATORY_COUNTERFACTUAL_PREMATCH_REPLAY_NOT_PROSPECTIVE_VALIDATION',
      'model':'Current UAT Data Value-First Strong selector on historical pre-match candidates',
      'comparator':'Previous Hybrid Strong selector on exactly the same archived snapshots',
      'dateRange':{'from':(end_date-dt.timedelta(days=days-1)).isoformat(),'to':end_date.isoformat(),
                   'fixedSnapshotHourAthens':snapshot_hour},
      'processedDates':processed,'skippedDates':skipped,
      'previousHybrid':a,'valueFirst':b,
      'valueFirstByMarketFamily':by_group(value,lambda p:p.get('marketFamily') or 'UNKNOWN'),
      'valueFirstByOddsBand':by_group(value,lambda p:odds_band(float(p['odd']))),
      'valueFirstByDirection':by_group(
          value,lambda p:(p.get('selectionSide') or 'OTHER').upper()
          if (p.get('selectionSide') or '').upper() in ('OVER','UNDER') else 'OTHER'),
      'valueFirstTeamGoalsByDirection':by_group(
          [p for p in value if p.get('subMarketKey') in ('HOME_TEAM_TOTAL','AWAY_TEAM_TOTAL')],
          lambda p:(p.get('selectionSide') or 'UNKNOWN').upper()),
      'valueFirstTeamGoalsByFavoriteStrengthAndDirection':by_group(
          [p for p in value if p.get('subMarketKey') in ('HOME_TEAM_TOTAL','AWAY_TEAM_TOTAL')],
          lambda p:('FAV_1X2_<=1.20' if p.get('isFavoriteTeamMarket') is True
                    and p.get('favorite1X2Odd') is not None
                    and p['favorite1X2Odd']<=1.20
                   else 'FAV_1X2_<=1.50' if p.get('isFavoriteTeamMarket') is True
                    and p.get('favorite1X2Odd') is not None
                    and p['favorite1X2Odd']<=1.50
                   else 'OTHER_TEAM_OR_MISSING_ODDS')+'_'+
                    (p.get('selectionSide') or 'UNKNOWN').upper()),
      'valueFirstByDate':by_group(value,lambda p:p['date']),
      'assumptions':[
        '1 unit stake per recommendation including independent market families on the same match',
        'historical bookmaker odds only; fixture generation must precede kickoff by >=60 seconds',
        'current selection rules replayed as counterfactual; not actual historical publications',
        'non-settled picks are excluded from ROI but disclosed as unresolved',
        'both policies use exactly the same pre-match snapshot, no rebuild using later football data',
        'historical app bundles may not contain Elo or Simulation for all fixtures',
        'this report cannot reproduce current Android High, Confirmed, Developing classifications',
        'past outcomes were not used to set thresholds during this run; results are exploratory'],
      'status':'INSUFFICIENT_EVIDENCE' if b['graded']<100 else 'EXPLORATORY_ONLY'}
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('VALUE_FIRST_AUDIT_SUMMARY '+json.dumps({
      'daysProcessed':len(processed),'daysSkipped':len(skipped),
      'previousHybrid':a,'valueFirst':b,'status':report['status']},ensure_ascii=False),flush=True)

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--days',type=int,default=14)
    parser.add_argument('--snapshot-local-hour',type=int,default=11)
    parser.add_argument('--end-date',default=None)
    parser.add_argument('--report',default='reports/value_first_retrospective_audit.json')
    args=parser.parse_args()
    today=dt.datetime.now(hist.ATHENS).date()
    end=dt.date.fromisoformat(args.end_date) if args.end_date else today-dt.timedelta(days=1)
    if not(1<=args.days<=30) or not(0<=args.snapshot_local_hour<=23) or end>=today:
        parser.error('days=1..30, hour=0..23, end-date must be before today')
    audit(args.days,args.snapshot_local_hour,end,ROOT/args.report)

if __name__=='__main__':main()
