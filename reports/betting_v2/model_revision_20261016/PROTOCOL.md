# Model revision protocol — 2026-10-16

Requested changes: reliability of sparse inputs, time-sensitive team strength, calibration on priced selections, and uncertainty-aware selection. These are implemented as separate ablations. This is development research on already inspected periods, not an untouched holdout or a promise of profit.

## Immutable inputs

Restore `Velliouras/StatMaker-Data` commit `edb62bdc5b939ba5dac56f10e4a027db420bc7d9` into a separate data directory. The corrected chronological loader must yield exactly 18,764 matches and history SHA256 `1653f6779eece3b7fef7dd5265db1978ffe566ec61e312f5866374739e14e9c0`. No active raw data is overwritten. Existing frozen `full` and `separate_ft_selected_period` predictions are reused as controls.

Use the 38 saved official CSV files and the 2,257 joined fixtures from the preceding CSV study. Every source hash is validated. No new odds download, provider request or LLM call. Bet365 closing prices only; exact quote timestamps remain unknown. The existing odds range, probability floor, EV formula, favourite preference and one-pick-per-family policy remain fixed.

## Changes fixed before this experiment's ROI

1. Reliability model: recent, venue and xG features are shrunk toward prior observed league rates using eight pseudo-observations. Include reliability ratios. Team attack/defence residual penalties increase when weighted club support is small, with 20 prior matches. Add a regularized league-specific home effect. The baseline ridge factor remains 50. This is a penalized GLM with partial pooling, not a claimed full Bayesian posterior.
2. Recency: the same design uses an exponential 180-day training half-life. Coefficients remain frozen inside each next-month evaluation; chronological actual observations update features only after the complete UTC day.
3. Selected calibration: after the prior-month joint profile, fit four regularized tilts on the six FT result × total-2.5 categories, using fixture-weighted Brier on a fixed prior selected cohort. Penalty 10; at least 30 prior selected fixtures, otherwise explicit identity fallback. The optimizer never reads price or ROI. All markets derive from the same adjusted joint PMF; goal periods thin that same PMF.
4. Uncertainty: 12 positive exponential-weight bootstrap refits on whole UTC-week training blocks, plus prior-week weighted selected calibrations. The decision uses the smaller of point probability and the 10th replica percentile, and likewise for favourite-conditional support. Both conservative probability ≥60% and conservative EV >0 are required. The point forecast remains separately recorded. This quantile is an approximate sensitivity measure, not a certified 90% confidence bound. Fixed prior scales and base-profile uncertainty are not fully propagated.

## Chronology and comparisons

Three folds: fit before July/calibrate July/evaluate August; fit before August/calibrate August/evaluate September; fit before September/calibrate September/evaluate October 1–14. Prior calibration aliases are inferred only from that prior window, anchored by existing explicit names, with two dates and two distinct existing opponents. No future result, price or model probability is used for identity inference.

Report `full`, `current`, `reliability`, `recency`, `calibrated` and `uncertainty`, without choosing an arm from test ROI. Compare on the common predicted subset of the original 2,257 priced fixtures and record every coverage loss. Report all-priced Brier by market, ROI, fixture-cluster intervals, months and tiers; retain negative and zero-pick results. Do not convert zero bets into a profitability claim.

This experiment changes the FT goal core first, which also supplies coherent goal-market and period distributions. It does not claim that corner/shot/foul/card coefficients have been retrained. No Android/PROD publication or new certification gate is part of this research workflow. A runtime integration must preserve point probabilities, uncertainty and source provenance as distinct quantities.
