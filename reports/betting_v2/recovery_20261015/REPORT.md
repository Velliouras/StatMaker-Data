> Superseded for performance conclusions by corrected_20261015. The earlier Work experiment had timezone-dependent duplicate rows. Retained as historical evidence only.

# StatMaker V2 — recovery and measured calibration experiment, 15 October 2026

## Delivery decision

**Recovery succeeded. Full-engine validation and deployment did not.** No Android code, UI, PROD, live forecast table, publisher or eligibility thresholds were changed. Singles maintenance remains untouched. This directory contains executed research, not a claim of a production-ready or profitable model. Do not transplant the fitted calibration onto the newest refit: its coefficients belong to the earlier frozen model used in each fold.

## Recovered work and execution evidence

- Data snapshot: `5b31ab6c63651ff6e4a19cf6d7c96b2d4ce3d32c`, main.
- Android UAT read through the GitHub connector: `ed51717c141db7d195fea157ab351682dd46cea0`, 0.1.52. Direct V3 forecasts still explicitly describe probabilities as uncalibrated. Android repository is private; shell clone was unauthenticated. No Android edit was made.
- Available prior workspace `/workspace/scratch/7f4002e5a05b/review4` contained a fitted pickle, `numeric_review.json`, exact-probability review and complete Python logs. Relevant results were copied to `recovered/`; the executable pickle was not used for the new fits.
- Prior research: two weekly folds, 941 + 239 test fixtures, 22,183 / 23,751 training fixtures. These were diagnostics, not a completed market-wide training/calibration project. A team-goal >=80% bin had mean prediction 85.43%, actual 66.67%, n=39. Nested events and fixture correlation limit interpretation.
- No old independent training process was visible on the accessible host. The previous Work environment cannot be independently enumerated or controlled. Finding old workspace files does not prove that all previous cloud environments were recovered or that no inaccessible job survives.
- GitHub Data Actions read: zero `in_progress`; one queued old statistics-universe run `32213805074`, not training. Latest inspected 20 runs included failed App-Ready publisher `38682841909`; no training run was started here.
- Actual new process: tool session `55333`, exit 0; `run_v1.log` has START, FIT, CALIBRATED, CHECKPOINT and COMPLETE records. Fit/calibration/event-generation elapsed times were 42.89s, 53.31s, 35.69s. Finalizer and priced replay have their own logs. No background execution is claimed after completion.

## Data and temporal limitations

Merged history: **25,032** unique resolved home/away/day fixtures, 2021-08-13 through 2026-10-15 02:30 UTC, cut off at request time 2026-10-15 08:23:22 UTC. Only 12 isolated matches precede July 2025; the date span must not be advertised as five complete seasons.

| Available target | Fixtures |
|---|---:|
| Full-time goals | 25,032 |
| First/second-half goals | 17,198 |
| Corners | 18,938 |
| Shots | 18,935 |
| Shots on target | 18,965 |
| Yellow cards | 18,553 |
| Yellow+red counts | 6,472 |
| Fouls | 13,235 |

The loader rejects invalid goals, missing identities, self-matches and future events, deduplicates resolved fixtures, and uses completed matches only. Features are lagged within each frozen training history. Calibration labels precede all test labels. Identity aliases are descriptive, not performance features. Prices are never model inputs.

**Event-time separation is verified by implementation; original data-publication time is not.** Cached histories can contain later corrections. There is no complete historical xG/injury availability archive. The mixed V3 model uses observed-xG flags; calibration is separated into >=8 xG observations for both teams versus ELO_NO_XG. This is not an independently retrained no-xG-only model. The count-strength feature uses goal-model coefficients fitted on the same earlier training set; this is in-sample stacking, not out-of-fold stacking. It does not read the later test outcomes, but can overfit training. Frozen features are not updated through the calibration/test month: this experiment evaluates a frozen monthly model, not an exact replay of daily refits. Independent Poisson/NB team counts do not establish realistic cross-team dependence.

## Experiment

Baseline is the actual current V3 monotone opponent-adjusted Poisson count model. Candidate fits a positive mean multiplier and negative-binomial dispersion on an earlier calibration window, separately by metric/xG cohort. Optimization uses count likelihood, not hit rate or test ROI. Goal 1X2/DC/BTTS probabilities derive from the same normalized joint count matrix. No artificial market balance, prices, club-specific rule or target hit rate enters fitting.

| Fit uses only dates before | Calibration | Later test |
|---|---|---|
| 2026-07-01 | July | August |
| 2026-08-01 | August | September |
| 2026-09-01 | September | October 1–14 |

**These calendar periods had already been inspected in prior project work. They are out-of-time research, not pristine untouched final holdouts.** No candidate is promoted using these test results. The chronological calibration was frozen before each later test in execution.

`comparison_unique.json` contains per-event/cohort Brier, binary log loss, reliability bins and paired fixture-bootstrap 95% intervals of Brier differences. Fixed diagnostic lines are predeclared; they are not actual bookmaker offers. 14,394 repeated identical team-line rows from the first runner were removed before the authoritative report. Original raw checkpoints retain the original execution fingerprint. Corrected runner and a uniqueness regression contract prevent recurrence.

| Count family, fixed diagnostic grid | Baseline Brier | Candidate Brier | Relative reduction |
|---|---:|---:|---:|
| Goals and derived events | 0.217326 | 0.216723 | 0.28% |
| Corners | 0.242765 | 0.235466 | 3.01% |
| Shots | 0.198036 | 0.188757 | 4.69% |
| SOT | 0.202586 | 0.199790 | 1.38% |
| Yellow cards | 0.189860 | 0.188931 | 0.49% |
| Yellow+red counts | 0.238470 | 0.222310 | 6.78% |
| Fouls | 0.188700 | 0.186317 | 1.26% |

These averages weight the specified correlated event grid. They do not measure profit or prove every line improves. Cards counts do not validate bookmaker card settlement contracts.

A negative result matters: **BTTS without sufficient historical xG worsens**, Brier 0.25989 -> 0.26129 over 4,336 fixtures. Paired fixture-bootstrap delta interval approximately +0.00053 to +0.00221. No-xG home-win Brier improves 0.24005 -> 0.23827. xG away-win Brier slightly worsens. Thus the mean/dispersion transform cannot be enabled globally as an established improvement. Intervals are descriptive marginal intervals, not adjusted for selecting among many markets.

## Archived-price replay

Scanned all **1,413** receipt files; one invalid receipt. Client observation timestamps must precede kickoff. Exact resolved home+away+UTC kickoff joins must be unique. This is not a separately verified cross-provider numeric fixture-ID crosswalk. 222,582 observed offer rows fall in the completed October test period; 143,037 fail the exact join; 73,615 are outside the explicitly supported priced-replay contract; 2,955 have no exact predeclared probability line. **2,204 unique archived quotes** survive. Repeated snapshots use the earliest observation, independent of outcome. One result-family and one goal-total-family opinion per fixture; Totals and Goals Over/Under share the same dedup family.

Strict existing research filter: odds >1.80 and <3.00; p(win)>=.60; p(win)*odds+p(push)-1>0. No threshold tuning. DNB handles draw as a push. Flat one-unit stakes. `priced_audit.json` includes every retained pick and price for the limited supported goal markets.

The initial narrow replay has too few independently settled selections per market to establish EV. Example: Double Chance baseline 3/7 and -17.3% ROI; candidate 2/6 and -38.02% ROI. BTTS baseline 3/6 and -4.85%; candidate 3/4 and +42.72%. These are tiny selected samples, not evidence of profitable or unprofitable populations. No statistically meaningful ROI confidence interval is issued below 30 independent fixtures; a bootstrap with three wins and no losses would give misleading certainty. No slippage/execution evidence exists. No historical prices were invented for corners/shots/cards, and no complete-universe backtest is claimed.

## Tiers and UAT decision

The app's HIGH/CONFIRMED/DEVELOPING labels are raw probability bands, not demonstrated reliability levels. No thresholds were silently relaxed or changed; no new SOLID policy was introduced. A raw 80% is not a verified 80% hit rate. Calibration improvements on some count families do not qualify the whole engine, adverse-scenario policy or EV estimates.

**Live UAT is not promoted from this experiment.** An offline UAT/review package is reproducible, but a complete tested replacement engine is not yet delivered. Remaining substantive work: out-of-fold goal-strength stacking, dependent goal-event calibration (especially BTTS), true no-xG model ablation/retraining, half-time coherent calibration, full raw-market line/settlement replay, exact archived numeric fixture crosswalk, sufficient historical priced coverage, then frozen prospective evaluation. Existing feed/publisher/Android features are preserved.

## Reproduce

From repository root, with numpy/scipy/scikit-learn installed:

```sh
OPENBLAS_NUM_THREADS=2 OMP_NUM_THREADS=2 python scripts/betting_v2/recovery_walk_forward.py --output reports/betting_v2/recovery_20261015/new_run
python -m unittest discover -s tests -p test_recovery_walk_forward.py -v
```

The original three compressed checkpoints, recovered logs, parameters, latest fitted coefficient arrays, feature scales/columns, state checkpoint and source/input fingerprint are retained in the recovery archive. Newest refit used all 25,032 pre-cutoff matches and is a baseline checkpoint only. Binary/data-heavy artifacts should stay in the delivery archive; compact results, code and calibration parameters belong in the repository. No provider API calls, Actions dispatch, Android build or production publish was executed.
