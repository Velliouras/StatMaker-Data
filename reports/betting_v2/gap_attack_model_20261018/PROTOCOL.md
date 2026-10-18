# Prior GAP statistics and goal/BTTS offsets

Primary: **gap_strong60_conservative**. The cross-market corner experiment failed to establish a profitable candidate and remains recorded separately. No frozen forward forecasts, app/UI, or production settings change in this experiment.

## Fixed hypothesis

Implement the four attacking/defensive home/away GAP ratings for SHOTS, SOT and CORNERS following the update structure in Wheatcroft (2021), https://doi.org/10.3233/JSA-200462 . This is an independent adaptation: daily batch updates prevent same-day order leakage; new teams initialize using prefix-only venue/group priors. Monthly learning rate and venue weights minimize prior per-side MAE after six observed matches per club, with bounds and one fixed initial point in source. They use no goals, odds or betting ROI. Labels require kickoff +24h before each monthly parameter boundary.

Materialize strictly prior GAP predictions and frozen existing goal-rate banks at each lagged feature day. Train the goal correction on **all available historical fixtures in August/September**, not only those having goal odds; require a valid frozen baseline and actual goal counts. Fit shared, symmetric five-feature Poisson log-rate offsets (intercept, forecast shots/SOT/corner logs, quality) with fixed L2 0.01, minimum 100 prior unique fixtures and no ROI objective. Preserve the existing prior goal-law calibration in all arms. GAP feature generation and goal correction are chronological: prior monthly GAP parameters generate earlier training features, never parameters fitted on the later goal test period.

Compare statistical, no-vig market and GAP probabilities on identical complete pre-match GOALS_FT TOTAL/TEAM/BTTS partitions. Use real receipt/market timestamps, one receipt per fixture, no mixed numeric event IDs, and treat bookmaker aliases as the same source. Existing favorite preference/scenario guards and one GOALS-family winner per fixture remain.

## Two explicit policies, no hidden threshold changes

1. **strong60**, primary: probability >=60%, odds >1.80/<3.00, push-aware EV >0. Freeze a point winner before conservative sensitivity can abstain, without replacement.
2. **ev55_research**, predeclared secondary: probability >=55%, same odds/EV/ranking/favorite rules; favorite scenario support remains >=60%. This is a research comparison of the previously discussed modest-edge policy. It is **not STRONG, not SOLID, not an app change and not a proposed winner selected from the observed ROI**.

For both, twelve weekly-weight goal-coefficient sensitivities pair with the frozen goal-rate bank. GAP update parameters remain fixed during these replicas, so sensitivity is partial and has no calibrated confidence coverage claim. Point and secondary results remain diagnostic; no automatic promotion.

## Reporting and evidence

Bind source, tests, workflow, frozen baseline states/profiles, history and archived quote packets with SHA256 before real fits. Cache monthly GAP parameters and daily feature checkpoints under the manifest signature. Report count prediction MAE, priced coverage, all-contract win/push/loss proper scores, paired fixture loss differences, selected unit profit/ROI with fixture intervals, and month/scope breakdowns. Earliest/latest receipts are separate, not independent samples or guaranteed closing prices. Dates already inspected are DEVELOPMENT, even when individual fits are prior-only.

Scope extensions to BTTS and our goal-rate stack are hypotheses, not reproductions of the published GAP paper's ROI. Zero provider API calls, zero LLM calls inside calculations, no wagers and no deploy. If the study fails, retain and explain the negative result.
