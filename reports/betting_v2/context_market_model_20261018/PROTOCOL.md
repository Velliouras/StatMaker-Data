# Contextual market-offset count model — fixed development trial

Declare before fitting the new contextual coefficients. All August–October dates have already been inspected in earlier experiments. This is a post-diagnostic development trial, never an untouched holdout. No test-ROI search, threshold grid, winning market/league selection or automatic deployment. No provider calls, LLM calls in jobs, rate refits or bets.

## Research question and inputs

Learn whether strictly prematch ELO, xG, form, venue, count history reliability and offer context add information beyond a contemporaneous complete market partition. This replaces a single global statistical/market blending coefficient with context-dependent deviations from market log odds. It does not claim that bookmakers are always accurate or that same-book benchmarks are independent evidence.

Use the existing hash-bound August–September and October whole-fixture first/last archived quote packets and 17,950-row settlement/update history. Restore each month's own frozen count and FT states and completed coupled-law profiles, with a complete UTC-day feature cutoff at least 24 hours before the observed quote. October's selected-calibrated FT comparator is retained for original favourite-direction checks. Never apply October states backwards. The frozen rate fits precede their statistical calibration/evaluation windows.

Accept only complete, unambiguous same-book binary TOTAL/TEAM partitions for corners, shots, SOT and fouls, at one observation/feature clock and exact provider event. Preserve the verified quote/fixture joins and reject invalid margins. Prefer another bookmaker's complete margin-removed reference; otherwise explicitly flag same-book reference. Canonical OVER defines both conditional sides. Keep push mass from the frozen count law. Other app markets, goals, and unverified card settlements are not priced candidates in this experiment; goal history supplies prematch features and the favourite comparator.

## Fixed feature design and training

Use the 31 features named in `context_market.py`: intercept; statistical-minus-market OVER logit; market logit; metric/scope/venue indicators; ELO context; smoothed xG and goal-form context; recent/venue count residuals against the forecast mean; line distance; count and xG sample quality; missing-xG flag; quote age/time to kickoff; own-book margin; independent-reference flag; and fixed interactions of the statistical disagreement with reliability, ELO, xG, form, scope and metric. Features have fixed clipping/scales rather than test-fitted normalization. Missing xG is explicit; priors do not become invented observed xG.

One pooled regularized binary logistic model with reference market logit as an offset. Fixed coefficient bounds [-2,2], mean proper-loss penalty .01 towards zero, and minimum 100 distinct prior fixtures. Each fixture total weight one, regardless of its number of quotes/metrics/books/lines. Collapse paired sides to one canonical event. Exclude actual pushes from conditional loss. Do not use odds/ROI in the loss; price information defines the offset and prematch offer features.

Fit one point and 12 fixed-seed positive seven-day weighted replicas. These train on fixed historical point feature vectors; input-rate uncertainty during training is not fully propagated. At prediction, combine coefficient replicas with the 12 existing rate/law parameter variants to vary statistical disagreement, mean/line features and push mass. This is sensitivity, not guaranteed lower coverage.

For September use only prior August records with kickoff+24h no later than September 1; for October use prior August–September records with the same lag. August supplies the seed and has no scored learned-arm output. Freeze parameters once per model month, selected from each quote's feature cutoff. Read a current label only after its current candidate offers have been evaluated. A checkpoint binds all code/data/model inputs.

## Predeclared benchmarks and selection

Compare statistical-only, market-only, simple residual (intercept, statistical disagreement and market logit), and all-context models. Simple and contextual share training objective, data, penalty and uncertainty scheme. Evaluate all four on the same universe where both prior learned models are active. Report point and conservative decisions for each; the contextual conservative arm is the primary candidate, with no automatic best-ROI substitution.

Retain odds >1.80/<3, unconditional win probability >=60%, positive EV and all original FT/count mean favourite and raw scenario checks. New learned probabilities may exceed the old statistical probability because this is a new offset model, not a downward calibration. Do not claim the market-conditioned decision probabilities define a coherent joint PMF across all lines/scenarios. Raw favourite scenarios remain separate constraints, not newly market-calibrated probabilities.

For each model choose one point-qualified winner per fixture/count family using the unchanged preference/confidence/probability ranking. Freeze that identity first. The conservative arm caps at its point prediction and the 10th percentile replica win probability; push uses its original capped 10th percentile sensitivity, and original favourite-scenario lower checks remain. If the winner fails, abstain; no runner-up substitution. This common fixed-winner rule also applies to the statistical benchmark and differs from previously reported conservative ranking. Do not compare its ROI against an older different universe as a pure model effect.

## Evidence and limits

Report fixture-weighted Brier/log loss on all common non-push canonical offers, independently of eligibility, by month/metric and on independent-reference subsets. Include paired fixture-resampled intervals for contextual-minus-market log loss (negative favours context), preserving canonical fixture order. These intervals do not account for all temporal dependence or prior model selection. Report all eight strategy arms, selected rows, missing labels, month/metric outcomes and fixture-clustered ROI intervals. Never pool earliest and latest. Archived quotes are not execution fills; first/last slot grids differ across source periods.

Persist input bindings, daily resumable checkpoints, all fitted parameter banks and results. Keep existing prospective freezes/proofs, active UAT, app UI and PROD unchanged during this experiment. Historical development gains alone do not establish future profitability.
