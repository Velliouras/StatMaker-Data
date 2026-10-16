# Prior-window model selection and stronger regularization

Research only. No UAT or PROD change. No provider requests.

Data: the same 18,764 matches and SHA-256 as the corrected baseline. Three chronological test windows: August, September, and October 1–14, 2026. These periods were inspected in earlier development; they are not an untouched holdout.

## Protocol

- Compare full, no-xG, no-goal-stack, chronological goal-stack, and chronological stack with 10× or 50× ridge.
- Hold the full-time goal coefficients identical for the ridge variants; scale the count ridge penalty and half-split logistic penalty.
- Fit validation calibration on the first two thirds of the prior calibration month; use the remaining third to choose a model and joint, half-shrunk, or identity calibration.
- Choose by common-cohort Brier score, equally weighting the metrics in the goal group; keep full-time and both half-time models coupled.
- Freeze choices before the evaluation stage reads test rows, then use the chosen model’s profile refitted on the full prior calibration month.
- Evaluate the identical event cohort across all six models. Prices and historical betting returns do not enter selection.

## Paired development results

ΔBrier = candidate minus the existing full calibrated baseline; negative is better. Intervals resample whole fixtures. They are marginal descriptive intervals, with no multiplicity correction or allowance for serial team/league dependence.

| Family | Baseline Brier | Selected Brier | ΔBrier | 95% interval |
|---|---:|---:|---:|---:|
| GOALS_FT | 0.217856 | 0.218165 | +0.000309 | [+0.000205, +0.000411] |
| GOALS_1H | 0.197232 | 0.194430 | -0.002802 | [-0.003522, -0.002053] |
| GOALS_2H | 0.209041 | 0.206570 | -0.002471 | [-0.003149, -0.001766] |
| CORNERS | 0.225762 | 0.215065 | -0.010697 | [-0.012429, -0.009008] |
| SHOTS | 0.183425 | 0.175245 | -0.008180 | [-0.009461, -0.006973] |
| SOT | 0.195967 | 0.189055 | -0.006912 | [-0.008404, -0.005476] |
| YELLOW_CARDS | 0.187716 | 0.177246 | -0.010470 | [-0.012205, -0.008551] |
| CARDS | 0.235260 | 0.202451 | -0.032809 | [-0.036711, -0.029060] |
| FOULS | 0.187779 | 0.178419 | -0.009360 | [-0.010769, -0.007959] |

Common event rows: 657,618. These are correlated market events, not distinct matches.

## Fixed ridge alternatives with full prior-month calibration

| Family | 10× ridge ΔBrier | 50× ridge ΔBrier |
|---|---:|---:|
| GOALS_FT | +0.000004 | +0.000004 |
| GOALS_1H | -0.002527 | -0.002901 |
| GOALS_2H | -0.002229 | -0.002601 |
| CORNERS | -0.006180 | -0.010697 |
| SHOTS | -0.003880 | -0.008180 |
| SOT | -0.004482 | -0.007068 |
| YELLOW_CARDS | -0.008199 | -0.010409 |
| CARDS | -0.025869 | -0.032809 |
| FOULS | -0.004506 | -0.009360 |

## Frozen prior-window choices

| Test window | Group | Model | Calibration |
|---|---|---|---|
| 2026-08-01 | GOALS | oof_ridge_50 | half_shrink |
| 2026-08-01 | CORNERS | oof_ridge_50 | joint |
| 2026-08-01 | SHOTS | oof_ridge_50 | joint |
| 2026-08-01 | SOT | oof_ridge_50 | joint |
| 2026-08-01 | YELLOW_CARDS | oof_ridge_50 | joint |
| 2026-08-01 | CARDS | oof_ridge_50 | joint |
| 2026-08-01 | FOULS | oof_ridge_50 | joint |
| 2026-09-01 | GOALS | oof_ridge_50 | joint |
| 2026-09-01 | CORNERS | oof_ridge_50 | joint |
| 2026-09-01 | SHOTS | oof_ridge_50 | joint |
| 2026-09-01 | SOT | oof_ridge_50 | identity |
| 2026-09-01 | YELLOW_CARDS | oof_ridge_50 | joint |
| 2026-09-01 | CARDS | oof_ridge_50 | joint |
| 2026-09-01 | FOULS | oof_ridge_50 | joint |
| 2026-10-01 | GOALS | oof_ridge_50 | joint |
| 2026-10-01 | CORNERS | oof_ridge_50 | joint |
| 2026-10-01 | SHOTS | oof_ridge_50 | joint |
| 2026-10-01 | SOT | oof_ridge_50 | joint |
| 2026-10-01 | YELLOW_CARDS | oof_ridge_50 | half_shrink |
| 2026-10-01 | CARDS | oof_ridge_50 | joint |
| 2026-10-01 | FOULS | oof_ridge_50 | joint |

## High-probability diagnostic grid

These bins cover unpriced events. They do not certify HIGH/CONFIRMED priced picks.

| Family | 80–100% events | Fixtures | Predicted | Observed |
|---|---:|---:|---:|---:|
| CARDS | 4418 | 1857 | 87.54% | 81.96% |
| CORNERS | 2355 | 1420 | 83.88% | 80.98% |
| FOULS | 9629 | 2804 | 88.33% | 86.79% |
| GOALS_1H | 16068 | 4440 | 87.58% | 84.58% |
| GOALS_2H | 12099 | 4440 | 86.12% | 81.79% |
| GOALS_FT | 9725 | 3459 | 85.83% | 78.96% |
| SHOTS | 10217 | 3241 | 88.11% | 88.56% |
| SOT | 9699 | 3243 | 86.61% | 84.75% |
| YELLOW_CARDS | 10156 | 3189 | 88.17% | 88.47% |

## Interpretation and limits

The selected rule lowers Brier in eight of nine families. Full-time goals worsen slightly; this negative result is retained. The full-time goal regression weights were held fixed: the larger FT difference comes from the selected half-strength calibration in the August test, while fixed-ridge joint calibration has only the previously documented numerical refit difference. No claim of universal improvement is made.

The candidate menu was designed after prior development inspection. Chronological selection prevents each test window’s labels from selecting its own model, but does not make the overall research an independent final test.

No new ROI estimate is produced here. The earlier 59-bet replay, including HIGH 0/2 and CONFIRMED 8/19, is retained unchanged. Card markets remain count diagnostics without a verified bookmaker settlement contract. Historical statistic publication timestamps remain unverified. No application tier thresholds were changed.

## Separate prospective batch

Frozen at 2026-10-16T07:25:39.178664+00:00, SHA-256 `8326b2c8e651a7b3343325e18fbc0982a9ea21fdc361587b4635754ab8b2254d`. The first prospective file remains unchanged.

| Model | Forecasts | Fixtures | Research selections |
|---|---:|---:|---:|
| full | 22581 | 152 | 133 |
| prior_window_selected | 22581 | 152 | 72 |

This batch overlaps the first prospective batch; it is not an additional independent sample.

All kickoffs follow this new freeze. Inputs are the same archived quote packets, with at least 24 hours of feature lag before the quote. Quote age is recorded; current execution and slippage are unverified. No future results were read and no automatic future scoring job was created.
