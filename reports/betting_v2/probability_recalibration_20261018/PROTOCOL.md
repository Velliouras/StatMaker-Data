# Count probability recalibration — locked development trial

Protocol declared before this trial's new fitted coefficients/results. Existing August–October results have been inspected: this is a post-diagnostic development trial, not an untouched holdout. No ROI-based market, league, threshold, hyperparameter, or model winner search. No rate refits, provider requests, LLM calls in jobs, app publication, or bets.

Restore each month's own frozen count and FT rate states. August's rates precede July; September's precede August; October's precede September. Use the already completed point + 12 coupled-law profile banks fitted on the preceding statistical calibration month. October uses the existing selected-calibrated FT favourite comparator, as in its previous archive replay. Completed feature days lag quote receipts by at least 24 hours. Never apply October states backwards.

## Fixed calibrator

Fit a regularized monotone sigmoid of the conditional OVER logit, separately per count metric (corners, shots, SOT, fouls) and scope (TOTAL/TEAM). Two parameters: intercept in [-2,2], positive slope in [.25,2]. Fixed penalty .01 towards identity (0,1), minimum 30 prior fixtures, binary log loss, each fixture total weight one per fitted group. Prices, profits and selection outcomes do not enter the objective. Include every predicted quoted event, irrespective of odd or eligibility; collapse paired OVER/UNDER duplicates per fixture/book/line/receipt and exclude pushes from the conditional calibration objective.

For each forecast month, use only previously generated historical predictions with kickoff + 24 hours no later than that month's start. August seeds the September calibrator; August and September seed October. No July priced seed is available, so August cannot produce calibrated-arm selections. Fit 12 fixed-seed positive UTC-week weighted parameter replicas. Missing prior coverage abstains in calibrated and matched arms; it never silently substitutes an uncalibrated pick.

Preserve original push probability and complementary conditional sides. The calibrated probabilities are a separate decision layer; they are not claimed to define one joint PMF across all lines/scenarios. Keep the base joint count model, raw favourite-direction/scenario checks and conservative scenario floors. For eligibility, cap raw point probability at calibrated point; cap raw conservative probability at calibrated point and the 10th percentile of paired model/calibrator replica win probabilities. Never increase raw statistical win probability or relax probability >=60%, odds >1.80 and <3, positive EV, or one pick per fixture/family. Favourite support remains an original extra constraint, not newly calibrated evidence.

## Comparisons

Report all six arms: base point/conservative; base on the same active calibration universe; calibrated point/conservative. Report all months and metrics. Base includes August; compare the correction with matched arms on September–October, not with a different time/coverage universe. Missing labels remain unscored. Select before reading current labels. Reusable fitted banks and all selected rows are retained.

Evaluate raw versus full signed recalibrated conditional probabilities on every common non-push quoted event, using fixture-weighted Brier and log loss. This assessment is independent of probability/odds eligibility and the downward-only decision cap. Report month-separated scores; improved training loss is not evidence of later calibration. Weekly sensitivity and fixture-resampled ROI intervals do not guarantee risk coverage or account for all temporal/model-selection uncertainty.

## Same-contract prices

Match exact canonical fixture, provider event ID, bookmaker and market spec between first and last whole-fixture quote packets. Reject conflicting price keys. On fixed earliest selected contracts, substitute the later available price without reranking, retain missing matches, and flag prices outside the original band. This is descriptive price attribution, not execution evidence or an executable new policy. Never pool earliest and latest as independent bets. August–September slots are daily 12:00 UTC; October slots are 12:00 and 23:59 UTC.

All code, source histories, quote packets, existing selected rows, monthly states and profiles are hash-bound before execution. Daily replay checkpoints bind that manifest. Keep existing prospective freezes, proofs, active UAT and PROD unchanged during this comparison.
