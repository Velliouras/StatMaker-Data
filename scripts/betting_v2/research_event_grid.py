"""Exact fixed-grid scoring from joint marginals; no duplicate raw PMF work.

Validation selection needs calibrated probabilities only. This evaluates the
same masks as JointCount.evaluate by summing their equivalent marginals once.
"""
import math
import numpy as np
from betting_v2.validated_walk_forward import distribution,grid
from betting_v2.joint_count import outcome

def grid_probabilities(joint,specs):
    pmf=joint.pmf;h,a=np.indices(pmf.shape)
    marginals={'HOME':pmf.sum(axis=1),'AWAY':pmf.sum(axis=0),
        'TOTAL':np.bincount((h+a).ravel(),weights=pmf.ravel())}
    home=float(pmf[h>a].sum());away=float(pmf[a>h].sum());draw=float(pmf[h==a].sum())
    yes=float(pmf[(h>0)&(a>0)].sum());no=float(pmf[(h==0)|(a==0)].sum())
    result=[]
    for spec in specs:
        _,scope,side,team,line=spec;push=0.
        if scope=='RESULT':win={'HOME':home,'AWAY':away,'DRAW':draw}[side]
        elif scope=='DC':win={'HOME_OR_DRAW':home+draw,'AWAY_OR_DRAW':away+draw,'HOME_OR_AWAY':home+away}[side]
        elif scope=='DNB':win={'HOME':home,'AWAY':away}[side];push=draw
        elif scope=='BTTS':win={'YES':yes,'NO':no}[side]
        elif scope in ('TOTAL','TEAM'):
            values=marginals['TOTAL' if scope=='TOTAL' else team]
            win=float(values[int(math.floor(line))+1:].sum()) if side=='OVER' else float(values[:int(math.ceil(line))].sum())
            if float(line).is_integer() and int(line)<len(values):push=float(values[int(line)])
        else:raise ValueError('Unsupported fixed-grid market')
        result.append((win,push))
    return result

def validation_events(rows,profile):
    events=[]
    for r in rows:
        specs=grid(r['metric']);probabilities=grid_probabilities(distribution(r,profile),specs)
        for spec,(win,push) in zip(specs,probabilities):
            y,ypush=outcome(*r['actual'],spec)
            if not ypush:events.append(dict(fixture=r['fixture'],metric=r['metric'],regime=r['regime'],
                market='|'.join(map(str,spec[1:])),q=win/(1-push),y=y))
    return events
