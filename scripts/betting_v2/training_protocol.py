"""Fit coherent goal dependence using only an earlier calibration window."""
from collections import defaultdict
import numpy as np
from scipy.optimize import minimize
from .joint_count import JointCount
from .recovery_walk_forward import fit_calibration

def fit_joint_profile(rows):
    grouped=defaultdict(list)
    for r in rows:grouped[r['metric'],r['regime']].append(r)
    result={}
    for (metric,regime),sample in grouped.items():
        if metric in ('GOALS_1H','GOALS_2H'):continue
        params=fit_calibration(sample)
        if metric=='GOALS_FT' and params['fitted']:
            probabilities=[];labels=[]
            for row in sample:
                joint=JointCount.from_means(*row['means'],scale=params['scale'],alpha=params['alpha'])
                h,a=np.indices(joint.pmf.shape)
                categories=(h==a).astype(int)*2+((h>0)&(a>0)).astype(int)
                probabilities.append([joint.pmf[categories==k].sum() for k in range(4)])
                hg,ag=row['actual'];labels.append(int(hg==ag)*2+int(hg>0 and ag>0))
            base=np.maximum(np.asarray(probabilities),1e-15);labels=np.asarray(labels)
            design=np.asarray([[0,0],[0,1],[1,0],[1,1]])
            def loss(theta):
                weighted=base*np.exp(design@theta);p=weighted/weighted.sum(axis=1,keepdims=True)
                value=-np.log(p[np.arange(len(p)),labels]).mean()+.005*float(theta@theta)
                return value,(p@design-design[labels]).mean(axis=0)+.01*theta
            fit=minimize(loss,[0.,0.],jac=True,bounds=[(-2.,2.),(-2.,2.)],method='L-BFGS-B')
            if not fit.success:raise ValueError('Joint calibration failed: '+fit.message)
            params.update(draw_tilt=float(fit.x[0]),btts_tilt=float(fit.x[1]))
        result[metric+'::'+regime]=params
    return result
