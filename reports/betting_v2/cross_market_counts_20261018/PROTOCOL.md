# Cross-market corner-count experiment, 18 October 2026

Primary candidate: **cross_dynamic_conservative**. Development study, no app promotion. The existing contextual forward cohort and its 122 bound inputs remain untouched.

## Hypothesis and fixed design

Goal O/U and 1X2 odds can add information to corner counts beyond frozen statistical rate offsets. Interpret complete contemporaneous same-book goal partitions with independent-Poisson total/winner-share inversion; preserve the draw mismatch as a covariate. Consolidate aliases, choose the most balanced valid total line, aggregate median log goal intensities across real bookmakers. All markets must have real timestamps within the preceding 24 hours, observed before kickoff, from one whole-fixture receipt. These are price-implied intensities, not measured xG.

Model total corners using NB with log mean offset from the frozen corner model. Eight fixed covariates: intercept, log implied total goals, goal imbalance, draw residual, lagged recent/venue corner ratios, lagged SOT level and sample quality. Log dispersion depends on intercept and goal imbalance. Home allocation uses beta-binomial with baseline home-share offset, intercept and signed goal supremacy. The constant-dispersion ablation fixes only its dispersion slope at zero. Fit fixture-weighted joint count log likelihood with fixed L2 0.01 and numerical bounds declared in source; no corner odds or ROI enter the fit. Minimum 100 prior unique fixtures. Twelve fixed-seed weekly weighted parameter fits measure sensitivity, without calibrated coverage claims.

Models freeze monthly: August provides prior priced-feature training for September; August/September provide training for October. Training fixture kickoff +24h must precede month start. Features use existing complete-day lagged monthly model states. Historical periods already inspected in earlier work remain DEVELOPMENT, even though each model's labels are strictly prior. Earliest/latest receipt policies are separate and cannot be pooled. Latest is not necessarily closing.

## Comparators and decisions

On the identical complete CORNERS TOTAL/TEAM partition universe compare frozen statistical, prior contextual and no-vig market probabilities, plus the new constant/dynamic count models. Existing contextual coefficients are frozen artifacts; no refit of that candidate. Each arm first selects one point winner per fixture/family with unchanged odds >1.80/<3.00, probability >=60%, positive push-aware EV, favorite mean/scenario rules and existing ranking. Conservative sensitivity may reject its winner, never replace it. No threshold, league or market selected using observed evaluation returns.

Score all pre-decision forecasts (conditional binary and full win/push/loss scores), paired fixture-resampled loss differences and one joint count score per available fixture. Report selected ROI/unit profit and fixture-resampled intervals by month and scope, sample sizes and exclusions. Point results are diagnostic; constant dispersion is an ablation, not an alternative promoted from test ROI. Counts can train on anchored fixtures without quoted corner contracts when prior labels and baseline features exist; price comparisons require actual offered corner partitions.

## Evidence and resumption

Bind source/config, frozen monthly states/profiles, goal/context banks, history and quote packets with SHA256 before running new real count fits. Each packet checkpoint must match the manifest signature. Save training banks, selected rows, complete proper scores and coverage. Provider API calls and LLM calls within the computations: zero. No bets, UAT edits, production changes or automatic promotion. New fitted results require assessment; they are not execution evidence.

References: Yip et al., https://arxiv.org/html/2112.13001v3 (cross-market count framework). This is our explicit adaptation with a conditional allocation model, fixed existing offsets and current selection constraints, not reproduction of the paper's reported ROI.
