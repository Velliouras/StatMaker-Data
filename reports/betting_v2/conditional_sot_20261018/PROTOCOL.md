# Conditional SOT under SHOTS — preregistered development experiment

Written before the first real conditional fit/replay. This is a new hypothesis, not an untouched holdout. All August–October 2026 archives have been inspected by earlier experiments. No claim of profit or production promotion.

## Fixed hypothesis and features

Keep the existing monthly frozen coupled SHOTS joint law and its 13 rate/profile variants. Generate each team's SOT conditional on latent SHOTS with a beta-binomial law. The two conversions are conditionally independent given parent counts. The triangular stochastic transform guarantees SOT <= SHOTS per team and preserves the parent's omitted-mass bound; this does not assert the parent SHOTS model is correct.

Conversion offsets average the logits of the team's prior venue attacking conversion and opponent's opposite-venue defensive conversion. Each is shrunk using 120 prior shots toward a prefix-only group/venue prior, which is shrunk by 120 toward a global/venue prior with fixed initial conversion 1/3 and 120 prior shots. Team groups come only from prior observed fixtures. No current fixture actual SHOTS is used as a predictor. Actual SHOTS is the conditioning count only in prior conditional-likelihood training.

The conversion fit has three parameters: shared logit intercept, signed home venue adjustment and log concentration. Bounds [-1,1], [-1,1], [log(.5),log(10000)]; start [0,0,log(30)]. Fixture-weighted paired conditional log likelihood, L2 .01 on the two logit coefficients, minimum 100 prior fixtures, no penalty on concentration, no odds or ROI objective. Twelve seeded exponential weekly-weight sensitivities are paired with frozen parent banks; they do not provide calibrated confidence coverage.

## Time and data

All valid SHOTS/SOT paired history fixtures are eligible, including fixtures without archived odds. Reject noninteger, negative, duplicate or SOT>SHOTS labels. Missing pairs are skipped. For each training fixture, conversion features use only events strictly before floor(kickoff UTC day)-24h. Monthly parameters use only fixtures with kickoff+24h <= month start. Historical result publication times are not independently certified; the 24h assumption is explicit.

Replay uses complete-day featureCutoff from actual quote receipt, at least 24h before observation; market update <= observation < kickoff, max quote age 24h, numeric provider event IDs, same receipt/fixture/clock. Incomplete or ambiguous same-book O/U partitions and mixed fixture clocks are rejected. Earliest/latest packets overlap and are never pooled as independent observations.

## Comparators and decision rules

Same available actual SOT TOTAL/TEAM integer/half-line universe for frozen statistical SOT, normalized market reference and new conditional SOT. Market reference retains statistical SOT push mass and favorite guards; it is a price benchmark, not an independently calibrated full count law. Report reference independence, joint count log loss, fixture-weighted three-state proper scores and paired fixture intervals. No corner, card, result or player market is smuggled into this SOT comparison.

Primary = conditional_conservative. Also publish all statistical/market/conditional point/conservative arms and every month/scope without choosing a retrospective winner. Fixed p>=.60, strict 1.80<odds<3.00, positive push-aware EV. Existing favorite goal-result ordering, higher expected SOT for favorite and conditional SOT favorite-scenario p>=.60 remain. Select the point winner first with existing ranking, one fixture/SOT family; conservative failure abstains without replacement. Conservative values use minimum(point, replica 10th percentile) independently for win, push and favorite support. No EV55 arm in this experiment.

## Execution and stop conditions

Analytic likelihood/gradients checked against scipy and finite differences; transition stochasticity, binomial limit, expected means, tail bound, push partitions and label clocks tested. Stop on optimizer failure, changed SHA256-bound inputs, malformed counts or probability partitions. Resume only with identical input signature. Save sources/protocol/input hashes before actual fits; checkpoint each priced day and fit bank. Zero provider/LLM calls in Python. No app/UAT/PROD or previous frozen prospective changes.
