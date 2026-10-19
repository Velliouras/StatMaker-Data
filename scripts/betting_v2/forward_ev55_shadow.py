"""Pre-published EV55 research projections of the frozen raw point winners.

Reads its parent's immutable forecast cohorts, never refits or calls providers.
STRONG60 remains unchanged in the parent and app; these are research-only rows.
"""
import argparse,fcntl,json,tempfile,sys
from pathlib import Path
from datetime import datetime,timezone
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from betting_v2.research_probability_recalibration import ROOT
from betting_v2.research_model_revision import read
from betting_v2.research_csv_prices import write,digest
from betting_v2.forward_candidate import record_publication,public_rows
from betting_v2.forward_market_residual import score as parent_score
from betting_v2.audit_ev55_sensitivity import adjusted,conservative,key
from betting_v2.research_git_archive import absolute_time

CONFIG=ROOT/'reports/betting_v2/forward_ev55_shadow_20261019/candidate.json'
PARENT=ROOT/'reports/betting_v2/forward_market_residual_20261019'
KINDS=('stat','market','residual')


def freeze(out,index,config):
    now=datetime.now(timezone.utc).timestamp()
    if now>=absolute_time(config['freezeBeforeUTC']):return [],dict(expired=True)
    source_index=read(PARENT/'index.json');proof=read(PARENT/'publication_evidence.json');bankpath=ROOT/config['parameters']['path']
    if digest(bankpath.read_bytes())!=config['parameters']['sha256']:raise ValueError('EV55 source parameter bank changed')
    banks=read(bankpath)['fits']['earliest']['2026-10-19'];known=set(index.get('sourceCohorts',[]));created=[]
    for item in source_index['cohorts']:
        if item['path'] in known:continue
        raw=(PARENT/item['path']).read_bytes()
        if digest(raw)!=item['sha256']:raise ValueError('Immutable source forecast changed')
        evidence=proof.get(item['path'])
        if not evidence:continue
        if evidence['sha256']!=item['sha256']:raise ValueError('Parent publication evidence changed')
        verified=datetime.fromisoformat(evidence['publiclyVerifiedAtUTC']).timestamp();source=read(PARENT/item['path']);when=datetime.now(timezone.utc).timestamp()
        if not datetime.fromisoformat(source['frozenAtUTC']).timestamp()<=verified<=when:raise ValueError('Invalid source publication timing')
        if source['parameters']!=config['parameters']:raise ValueError('Source parameter selection changed')
        results={};bykey={key(q):q for q in source['models']['residual_point']['forecasts']}
        winners=[q for q in public_rows(source['models']['stat_point']['selected'],verified) if q['kickoff']>when+60 and q['observed']>=when-86400]
        for kind in KINDS:
            point=[];lower=[]
            for q in winners:
                ref=bykey[key(q)]['marketReference'];p=banks[q['spec'][0]]
                if kind=='stat':candidate=q;minimum=.6
                else:
                    if kind=='market':p=dict(p,point=dict(p['point'],theta=[0.,0.]),replicas=[dict(f,theta=[0.,0.]) for f in p['replicas']])
                    candidate=adjusted(q,ref,p,.55);minimum=.55
                if candidate is None:continue
                candidate=dict(candidate,certifiedStrong=False,strategyClass='RAW60_COMPARATOR' if kind=='stat' else 'RESEARCH_EV55');point.append(candidate);c=conservative(candidate,minimum)
                if c is not None:lower.append(c)
            rows=[q for q in public_rows(source['models'][kind+'_point']['forecasts'],verified) if q['kickoff']>when+60 and q['observed']>=when-86400]
            for mode,picks in [('point',point),('conservative',lower)]:results[kind+'_'+mode]=dict(forecasts=rows,selected=picks)
        keys=sorted({(q['fixture'],q['spec'][0]) for d in results.values() for q in d['forecasts']})
        known.add(item['path'])
        if not keys:continue
        stamp=datetime.fromtimestamp(when,timezone.utc).strftime('%Y%m%dT%H%M%S%fZ');target=out/f'frozen_{stamp}.json.gz'
        if target.exists():raise ValueError('Refusing to replace immutable EV55 projection')
        doc=dict(schema=1,candidateId=config['candidateId'],frozenAtUTC=datetime.fromtimestamp(when,timezone.utc).isoformat(),sourceCohort=item['path'],sourceSha256=item['sha256'],sourcePublicationEvidence=evidence,sourceCandidateSha256=source['candidateSha256'],parameters=config['parameters'],models=results,marketKeys=keys,candidateSha256=digest(CONFIG.read_bytes()),minimumProbabilityByModel={'stat':.6,'market':.55,'residual':.55},researchOnly=True,certifiedStrong=False,providerCalls=0,llmCalls=0,refits=0,executionEvidence=False)
        write(target,doc,zipped=True);index.setdefault('cohorts',[]).append(dict(path=target.name,sha256=digest(target.read_bytes()),frozenAtUTC=doc['frozenAtUTC'],sourceCohort=item['path'],marketKeys=keys,models={k:dict(forecasts=len(v['forecasts']),selected=len(v['selected'])) for k,v in results.items()}));created.append(target.name)
    index['sourceCohorts']=sorted(known);index['marketKeys']=sorted({tuple(k) for c in index.get('cohorts',[]) for k in c['marketKeys']});return created,{}


def score(out,index):
    parent_score(out,index);p=out/'summary.json';doc=read(p);doc.update(researchOnly=True,certifiedStrong=False,minimumProbabilityByModel={'stat':.6,'market':.55,'residual':.55});write(p,doc)
    f=out/'RESULTS_GR.md';f.write_text('ΕΡΕΥΝΗΤΙΚΗ EV55 ΠΟΛΙΤΙΚΗ — Δεν είναι STRONG και δεν εμφανίζεται στο app. Σταθερός raw point winner πριν από market correction· χωρίς replacement.\n\n'+f.read_text())


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--record-publication',action='store_true');p.add_argument('--score-only',action='store_true');a=p.parse_args();out=a.output;out.mkdir(parents=True,exist_ok=True)
    lockpath=Path(tempfile.gettempdir())/('ev55-shadow-'+digest(str(out.resolve()).encode())[:16]+'.lock')
    with lockpath.open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);index=read(out/'index.json') if (out/'index.json').exists() else dict(schema=1,cohorts=[],marketKeys=[],sourceCohorts=[])
        if a.record_publication:record_publication(out,index);score(out,index);return
        changed=[];created=[];audit={}
        if not a.score_only:
            config=read(CONFIG);changed=[p for p,h in config['files'].items() if not (ROOT/p).exists() or digest((ROOT/p).read_bytes())!=h]
            if not changed:created,audit=freeze(out,index,config)
            write(out/'index.json',index)
        score(out,index);write(out/'status.json',dict(status='COMPLETE',newCohorts=created,freezeSkippedForChangedInputs=changed,audit=audit,researchOnly=True,certifiedStrong=False,providerCalls=0,llmCalls=0,refits=0));print('EV55_SHADOW',created,flush=True)
if __name__=='__main__':main()
