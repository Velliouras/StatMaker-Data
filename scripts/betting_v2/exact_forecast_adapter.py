"""Offline integration seam. Not wired to the Android or published feeds.

Inputs must be prematch predictions and a profile fitted before their test
window. The caller owns that provenance; this adapter does not certify tiers.
"""
from betting_v2.joint_count import VERSION
from betting_v2.validated_walk_forward import distribution


def forecast(row, profile, spec):
    if row['metric'] != spec[0]:
        raise ValueError('Prediction metric does not match market')
    joint = distribution(row, profile)
    win, push = joint.evaluate(spec)
    scenarios = {}
    for side in ('HOME', 'AWAY'):
        if row['metric'].startswith('GOALS') and 'shares' in row:
            fullrow = dict(row, metric='GOALS_FT', means=row['fullMeans'])
            fullrow.pop('shares')
            conditional, mass = distribution(fullrow, profile).condition(side)
            if conditional is not None:
                conditional = conditional.thin(*row['shares'])
        else:
            conditional, mass = joint.condition(side)
        scenarios[side] = dict(conditionMass=mass,
            winProbability=conditional.evaluate(spec)[0] if conditional is not None else None)
    home, away = joint.means
    return dict(model=VERSION, actualRuns=0, probabilityMethod='exact_joint_pmf',
        winProbability=win, pushProbability=push, lossProbability=max(0., 1-win-push),
        homeMean=home, awayMean=away, scenarios=scenarios,
        omittedMassBound=joint.omitted_mass, validatedTier=None)
