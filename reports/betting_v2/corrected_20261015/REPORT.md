# Corrected betting research — 2026-10-15

Nine completed chronological folds across full, no_xg and no_goal_stack. Research only; no Android integration or PROD publication.

## Data correction

The recovered audit found 18,764 unique team/day matches. Legacy parsing produced 25,032 rows under Asia/Bangkok but 18,764 under UTC and America/New_York: 6,268 excess rows in the affected Work environment. This does not prove duplicate rows on GitHub UTC runners. Naive timestamps are now interpreted as UTC; the original provider date convention is not independently verified.

8,589 rows contain paired observed xG. Only 12 matches predate July 2025; this is not five complete seasons.

## Method and limitations

Train before July/August/September; calibrate the following month; test August/September/October 1–14. Coefficients are frozen within each fold and rolling features update after an entire UTC day. The original source and data fingerprints matched exactly on recovery; four completed checkpoints were reused and five were recalculated. The final nine checkpoints were validated against the same signature.

657,618 correlated market-event rows per variant are not independent matches. Full-time-goal scoring spans 4,497 fixtures. Brier intervals use paired fixture bootstrap and are marginal descriptive intervals, without correction for multiple comparisons. DNB scoring is conditional on no push.

The periods have already been inspected. They are not an untouched holdout. Historical statistics publication times are not verified. Goal-strength stacking remains in-sample in the full variant. Bookmaker card-points settlement is not inferred from card counts.

## Calibration on clean data

The baseline below is the corrected V3 architecture with day-lagged state, not the earlier corrupted Work probabilities.

| Family | Baseline Brier | Calibrated Brier | Relative reduction | Paired delta 95% |
|---|---:|---:|---:|---|
| GOALS_FT | 0.219755 | 0.217856 | 0.86% | [-0.002289, -0.001463] |
| GOALS_1H | 0.197732 | 0.197232 | 0.25% | [-0.000711, -0.000280] |
| GOALS_2H | 0.209802 | 0.209041 | 0.36% | [-0.001029, -0.000484] |
| CORNERS | 0.232083 | 0.225762 | 2.72% | [-0.007171, -0.005439] |
| SHOTS | 0.192930 | 0.183425 | 4.93% | [-0.010451, -0.008595] |
| SOT | 0.198640 | 0.195967 | 1.35% | [-0.003229, -0.002103] |
| YELLOW_CARDS | 0.191016 | 0.187716 | 1.73% | [-0.004692, -0.002059] |
| CARDS | 0.268801 | 0.235260 | 12.48% | [-0.037332, -0.029319] |
| FOULS | 0.190434 | 0.187779 | 1.39% | [-0.003239, -0.002078] |

26 of 378 market/cohort/direction entries have marginal intervals indicating worse Brier scores. Complementary OVER/UNDER entries are correlated. Aggregate gains are not universal.

## Actual archived-price replay

Only quotes observed on the prediction UTC day after 03:00 UTC and before kickoff are eligible. Join requires unique resolved teams and exact kickoff. One earliest eligible receipt is retained per fixture/bookmaker. Missing prices and unsupported markets are not invented. Cards and ambiguous settlement are excluded.

Retained 4,358 quotes. Receipt audit: `{"invalidReceipt": 1, "noUniqueExactJoin": 44078, "periodOffers": 222582, "quoteNotSameDayAfter03UTC": 152319, "receiptFiles": 1413, "unsupportedOrAmbiguousSettlement": 21535}`.

| Variant / calibration / selection | Bets | Fixtures | Wins | ROI |
|---|---:|---:|---:|---:|
| full::raw::current_preference | 28 | 17 | 14 | -3.35% |
| full::raw::probability_ranking | 28 | 17 | 13 | -10.49% |
| full::calibrated::current_preference | 22 | 15 | 10 | -11.95% |
| full::calibrated::probability_ranking | 22 | 15 | 10 | -11.95% |
| no_xg::raw::current_preference | 26 | 17 | 13 | -2.61% |
| no_xg::raw::probability_ranking | 26 | 17 | 12 | -10.30% |
| no_xg::calibrated::current_preference | 23 | 15 | 11 | -6.65% |
| no_xg::calibrated::probability_ranking | 23 | 15 | 11 | -6.65% |
| no_goal_stack::raw::current_preference | 28 | 17 | 14 | -2.13% |
| no_goal_stack::raw::probability_ranking | 28 | 17 | 13 | -10.49% |
| no_goal_stack::calibrated::current_preference | 22 | 15 | 10 | -11.95% |
| no_goal_stack::calibrated::probability_ranking | 22 | 15 | 10 | -11.95% |

This small, selected sample does not establish profitable deployment. Probability calibration is not automatically an ROI improvement. The replay is a Python port of direct UAT policy, not executed Android parity. No empirical HIGH/CONFIRMED tier certification is claimed.

## Verification and delivery

Nine focused tests passed in this recovered environment: four corrected-training checks, three exact-adapter checks, two quote/selection checks. Workflow YAML was parsed and git diff checked. The manual main-only research workflow has not been dispatched. No provider calls were made.

Automatic approval review blocked the earlier push to StatMaker-Data/main. No push workaround was attempted. The package includes a reviewable patch, model state, prediction/calibration checkpoints, and raw results. See INTEGRATION.md for the remaining UAT work.

Original final local-only commits were lost when the environment reset. This recovered package replaces that incomplete save and contains the rerun evidence; no claim is made that the old final ZIP survived.
