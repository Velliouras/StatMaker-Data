"""Venue-specific attacking/defensive GAP ratings with prior MAE fitting.

Independent implementation of the update structure in Wheatcroft (2021),
doi:10.3233/JSA-200462. Daily batching and prefix-only cold priors are our
explicit chronological adaptation. No goal labels, odds or ROI fit the ratings.
"""
from collections import defaultdict
from itertools import groupby
import math
import numpy as np
from scipy.optimize import minimize

METRICS=('SHOTS','SOT','CORNERS')
COLD=dict(SHOTS=12.,SOT=4.,CORNERS=4.5)
INITIAL=(.1,.75,.75)
BOUNDS=((.02,.5),(.05,.95),(.05,.95))
BURN_IN=6

def encode(history,metric,values,cutoff):
    """Initial values for first appearances depend only on previous UTC days."""
    if metric not in METRICS:raise ValueError('Explicit supported GAP metric required')
    rows=sorted((r for r in history if r['_date']<cutoff),key=lambda r:(r['_date'],r['_home'],r['_away']))
    ids={};initial=[];batches=[];pools=defaultdict(lambda:[0.,0.,0]);seen=set()
    def prior(group,venue):
        x=pools.get((group,venue)) or pools.get(('__GLOBAL__',venue))
        return [x[0]/x[2],x[1]/x[2]] if x and x[2] else [COLD[metric],COLD[metric]]
    for day,group in groupby(rows,lambda r:int(r['_date']//86400)):
        batch=[];updates=[]
        for r in group:
            y=values(r,metric)
            if y is None:continue
            if len(y)!=2 or any(not math.isfinite(v) or v<0 for v in y):raise ValueError('Invalid GAP observations')
            identity=(r['_date'],r['_home'],r['_away'])
            if identity in seen:raise ValueError('Duplicate GAP fixture label')
            seen.add(identity)
            for side in ('home','away'):
                team=r['_'+side];g=r.get('_'+side+'_group','__GLOBAL__')
                if team not in ids:ids[team]=len(ids);initial.append(prior(g,True)+prior(g,False))
            batch.append([ids[r['_home']],ids[r['_away']],float(y[0]),float(y[1])])
            updates.extend([(r.get('_home_group','__GLOBAL__'),True,y[0],y[1]),(r.get('_away_group','__GLOBAL__'),False,y[1],y[0])])
        if batch:batches.append((day,np.asarray(batch,dtype=float)))
        for g,v,own,other in updates:
            for key in {(g,v),('__GLOBAL__',v)}:
                p=pools[key];p[0]+=own;p[1]+=other;p[2]+=1
    return dict(ids=ids,initial=np.asarray(initial,dtype=float).reshape(-1,4),batches=batches,
                fixtures=len(seen),maxTimestamp=max((r['_date'] for r in rows if values(r,metric) is not None),default=None))

def roll(encoded,theta,forecasts=False):
    lam,phi_home,phi_away=map(float,theta)
    if not .0<lam<=1 or not 0<phi_home<1 or not 0<phi_away<1:raise ValueError('Invalid GAP update parameters')
    ratings=encoded['initial'].copy();counts=np.zeros(len(ratings),dtype=int);loss=0.;n=0;rows=[]
    for day,b in encoded['batches']:
        h=b[:,0].astype(int);a=b[:,1].astype(int);yh,ya=b[:,2],b[:,3]
        ph=(ratings[h,0]+ratings[a,3])/2;pa=(ratings[a,2]+ratings[h,1])/2
        mature=(counts[h]>=BURN_IN)&(counts[a]>=BURN_IN)
        loss+=float((abs(yh-ph)+abs(ya-pa))[mature].sum());n+=int(mature.sum())
        if forecasts:rows.extend(dict(day=int(day),home=int(i),away=int(j),predicted=[float(x),float(y)],actual=[float(u),float(v)],mature=bool(ok)) for i,j,x,y,u,v,ok in zip(h,a,ph,pa,yh,ya,mature))
        eh=yh-ph;ea=ya-pa;delta=np.zeros_like(ratings)
        for ids,col,error,factor in [(h,0,eh,phi_home),(h,2,eh,1-phi_home),(h,1,ea,phi_home),(h,3,ea,1-phi_home),
                                      (a,2,ea,phi_away),(a,0,ea,1-phi_away),(a,3,eh,phi_away),(a,1,eh,1-phi_away)]:
            np.add.at(delta,(ids,col),lam*factor*error)
        ratings=np.maximum(0.,ratings+delta);np.add.at(counts,h,1);np.add.at(counts,a,1)
    return dict(mae=loss/(2*n) if n else None,scoredFixtures=n,ratings=ratings,counts=counts,forecasts=rows)

def fit(history,metric,values,cutoff):
    # Training labels get a full day to arrive; state construction is separate.
    encoded=encode(history,metric,values,cutoff-86400);meta=dict(metric=metric,trainingBefore=cutoff,labelLagSeconds=86400,
        fixtures=encoded['fixtures'],maxTrainingTimestamp=encoded['maxTimestamp'],objective='Prior sequential per-side MAE after six-match burn-in',
        roiUsed=False,parameters=['learningRate','homeVenueWeight','awayVenueWeight'],initial=list(INITIAL),bounds=list(BOUNDS))
    baseline=roll(encoded,INITIAL)
    if baseline['scoredFixtures']<100:return dict(meta,fitted=False,theta=list(INITIAL),reason='INSUFFICIENT_MATURE_PRIOR_FIXTURES')
    def loss(theta):return roll(encoded,theta)['mae']
    result=minimize(loss,INITIAL,method='Nelder-Mead',bounds=BOUNDS,options=dict(maxiter=250,xatol=.002,fatol=1e-5))
    if not result.success or not np.isfinite(result.fun):raise ValueError('GAP MAE optimization failed '+str(result.message))
    return dict(meta,fitted=True,theta=result.x.tolist(),mae=float(result.fun),initialMae=baseline['mae'],matureFixtures=baseline['scoredFixtures'],iterations=int(result.nit))

def state(history,metric,values,feature_cutoff,parameters):
    """Only labels strictly before the lagged feature day can affect state."""
    encoded=encode(history,metric,values,feature_cutoff);rolled=roll(encoded,parameters['theta'])
    return dict(metric=metric,featureCutoff=feature_cutoff,ids=encoded['ids'],ratings=rolled['ratings'].tolist(),counts=rolled['counts'].tolist(),
                priorFixtures=encoded['fixtures'],maxObservedTimestamp=encoded['maxTimestamp'],theta=parameters['theta'])

def predict(snapshot,home,away):
    if home==away:raise ValueError('Identical GAP teams')
    cold=[COLD[snapshot['metric']]]*4
    def team(name):
        i=snapshot['ids'].get(name)
        return (snapshot['ratings'][i],snapshot['counts'][i]) if i is not None else (cold,0)
    h,hn=team(home);a,an=team(away)
    return dict(home=max(.01,(h[0]+a[3])/2),away=max(.01,(a[2]+h[1])/2),sampleHome=hn,sampleAway=an,
                featureCutoff=snapshot['featureCutoff'],coldStart=min(hn,an)<BURN_IN)
