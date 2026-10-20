# Numerical SHOTS/SOT challenger, fixed before fits — 20 October 2026

This experiment changes probabilities, not presentation or admission thresholds.
It uses the frozen count CatBoost architecture plus actual same-receipt, same-book
Over/Under odds. It does not reuse the old pre-CatBoost market residual forecasts.

## Information and chronology

- Frozen 17,950-fixture history and archived earliest/latest exact quotes only.
- Reconstruct historical count features with the existing 24-hour result lag.
- August base predictions: count models trained before July, calibrated in July.
- September base predictions: trained before August, calibrated in August.
- October base predictions: exact deployed bank, trained before September and
  calibrated in September. No in-sample August/September predictions from that bank
  may train a stacking model.
- Select the base bank by feature cutoff, never by future result or kickoff month.
- Train each challenger using earliest quote contracts whose results plus 24 hours
  are available before September 1 / October 1 respectively. Evaluate only quote
  feature cutoffs at or after that boundary and before the next month.
- Earliest and latest archive variants overlap; train on earliest only and report
  the two evaluation variants separately. They are not independent replications.
- The October period has already been inspected in previous development. It is
  a chronological development comparison, not an untouched profitability holdout.
- No historical injuries, lineups or motivation without point-in-time records.

## Models (fixed, no ROI search)

1. Deployed conservative count probabilities (selection baseline), and mean count
   probabilities for proper scoring.
2. Same-book proportional no-vig market probabilities (benchmark).
3. Ridge logistic combination of market logit and count-minus-market logit,
   four SHOTS/SOT x TOTAL/TEAM group intercepts. Gap weight constrained [0,1],
   intercepts [-2,2], L2 penalty .01 against mean fixture-weighted log loss.
4. Market-offset CatBoost probability model using count forecasts and existing
   point-in-time team context. 120 trees, depth 3, learning rate .03, L2 20,
   seeds 17/31/71, two threads, no early stopping or parameter selection.

Training and proper scores weight each fixture equally across all of its lines,
books and metrics. Push observations do not train conditional binary outcomes;
the unchanged count model push mass is explicit in probability/EV reconstruction.
No archive variant is added to the other as extra evidence.

## Fixed selection comparison and checks

Use the existing p >= .55, 1.80 < odds < 3.00, positive model EV, original count
favourite guards, first observed selection and one fixture/metric policy.
Challenger point forecast is replica mean; conservative decision uses the lowest
of three context replica probabilities. Market/linear forecasts are single fits.
Rank with each model's own probability, EV, and the existing contract tie key.
Report selection counts, profit, ROI and fixture bootstrap intervals alongside
full-universe log loss and Brier, by month and market, with paired fixture
comparisons. Do not optimize thresholds, remove losing groups, or rewrite issued
forecasts. Verify deployed baseline replay parity and source hashes.

This experiment does not publish retrospective results to UAT Performance, alter
the existing numerical bank, place bets, or call a paid data/LLM provider.
An improvement claim requires both forecast evidence and candid ROI comparison.
