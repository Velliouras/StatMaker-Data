# Fixed-winner conditional calibration — post-diagnostic development trial

The previous general calibration slightly improved whole-universe proper scores but retained overconfidence among selected winners. This follow-up fixes one specific selection mechanism; the historical dates were already inspected and remain development data, not untouched tests. Declare this protocol before fitting these new offsets. No threshold grid, market/league selection, ROI objective, automatic model promotion, new provider requests or rate refits.

## Inputs and frozen selection

Use the complete pre-label raw point/conservative option lists saved by the probability recalibration trial, generated from each month's own frozen prior rate and coupled-law model. Freeze exactly one existing raw winner per fixture/count family with the unchanged favourite/preference/probability ranking before reading labels. Reproduce all original selected and scored totals. Missing labels never cause a runner-up substitution.

The available count families are corners, shots, SOT and fouls; cards remain blocked by settlement data. Apply to all scopes of their fixed winners. Pool these families for one offset per policy, rather than dropping markets or fitting sparse per-market winner cohorts. Point and conservative raw selectors retain their own distinct training winner cohorts.

## One fixed model

For each policy and evaluation model month, fit one additive log-odds offset on prior frozen winner conditional win probabilities. Slope fixed at one. Offset bounds [-2,2], fixed .01 penalty towards zero, fixture total weight one, minimum 30 distinct prior fixtures. Minimize conditional binary log loss; prices and ROI do not enter the objective. Exclude actual pushes from the conditional objective. Reserve a full 24 hours after kickoff for labels. The kickoff+lag must be at or before the first UTC instant of the model month selected from each winner's feature cutoff.

August has no preceding priced winner seed. September uses prior August winners; October uses prior August and September winners. Earlier forecasts never consume later labels. Twelve fixed-seed positive seven-day block reweights provide parameter sensitivity. Conservative decisions retain the existing raw conservative score and cap it at the fitted winner probability and the 10th percentile calibrated score from those reweights. These are sensitivity measures, not guaranteed lower probability coverage.

Keep the original push mass, odds >1.80 and <3, win >=60%, positive EV, favourite scenario rules and original winner identity. Decisions only decrease the raw score. If the winner fails, abstain for that family: no repeated search for a different line or bookmaker. No fitted coefficient or rule can use current outcomes.

## Evaluation

Compare raw versus corrected fixed winners only where a prior bank is active; report both receipt policies separately, all months/metrics, full selected rows, missing labels and abstentions. Report proper scores on all current matched raw winners before abstention, using full signed adjusted probabilities. These isolate probability correction from selective removal. Do not sum earliest and latest results or present a tiny positive subset as profitability evidence.

Keep all existing forward freezes/proofs, UAT/PROD and thresholds unchanged. Publish reusable parameter banks, source hashes and results. This evaluates the threshold-preserving structural correction; any later threshold change would require a separately declared design and evaluation.
