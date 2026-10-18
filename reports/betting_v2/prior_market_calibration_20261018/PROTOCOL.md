# Prior count-price calibration: August–September 2026

## Fixed corpus and chronology

Use the last path-changing Git commit before each 12:00 UTC daily slot from 2 August through 30 September. Sixty slots yield 58 distinct provider payloads (1,856,948,795 bytes). Every exact payload is verified against independently retrieved Git tree blob SHA-1 and length, plus SHA-256. No odds-provider calls are made. Preserve only approved canonical identity with unique exact kickoff, pre-kickoff commit observation, generation before commit, and a market update no more than 24 hours before commit. Conflicting quote keys remain rejected.

The universe contains 1,972 unique fixtures: at least one snapshot has corners for 1,919, shots for 313, SOT for 552 and fouls for 131. Whole-fixture packet availability differs: earliest 1,267/41/167/20 and latest 1,835/278/492/131 respectively. Coverage is not a bet count. All whole-fixture books/lines use one observation; earliest and latest overlap and are not independent samples. The original 17,950-row historical input is pinned by compressed and content hashes.

## Frozen monthly models

Never apply the October model to August or September. Choose the most recent monthly pretest state whose clock is at or before the quote's feature cutoff, with an additional complete UTC day of feature lag. August rates were fitted before July; September rates before August. Prior statistical calibration precedes each deployment month. Restore frozen FT and four count models, advance observations only up to each cutoff, and retain all 13 parameter replicas. No rate coefficients are refitted.

For the coupled total/allocation law, fit point and 12 sensitivity profiles separately on the preceding month's already frozen calibration predictions, never on target-period outcomes. Existing independent-law profile banks remain the control.

## Prior price correction

Fit one statistical/market pooling weight per metric to prior fixture-weighted binary log loss, with the existing fixed penalty 0.002 and minimum 30 distinct prior fixtures. Use complete, unambiguous two-sided partitions after removing bookmaker margin. Prefer another bookmaker; otherwise explicitly label same-book benchmark. Bet365 no-latency is the same bookmaker identity.

Price calibration includes only fixtures whose kickoff plus 24 hours is at or before the model-month cutoff. It cannot consume the current month's labels. August has no eligible earlier priced calibration in this experiment, so the new price-cap arms are inactive then. September learns from August only. Twelve independent positive weekly reweights quantify price-weight sensitivity, not calibrated risk protection.

Cap the statistical win probability at its prior-fitted pooled value; never increase it. Keep original push accounting, odds >1.80 and <3.00, probability >=60%, positive EV, favourite-direction/scenario preferences and one pick per fixture/family. Conservative caps also use the 10th-percentile replica sensitivity capped at the point estimate. No empirical ROI, market removal, threshold relaxation or league selection is permitted.

## Predeclared comparisons

Report all eight arms: independent control point/conservative; coupled point/conservative; coupled point/conservative on the same eligible reference universe; and prior-price-capped point/conservative. The matched reference arms isolate the effect of the price cap from missing reference data or insufficient prior sample. Selection precedes label lookup. Missing target labels remain unscored; integer pushes are stake returns.

Keep results by calendar month and metric, expected versus actual profit, fixture-clustered intervals, full selected rows and price-fit metadata. Export weights trained strictly before 1 October for research only; do not deploy them or alter the existing forward candidate. The dates have already been inspected in development, historical result publication is unverified and archived prices are not execution fills.

## Durability and checks

All model states, profiles, code, corpus and history are hash-bound. Per-day replay checkpoints bind code, data and model inputs. Tests cover monthly cutoff isolation, 24-hour label lag, immunity of prior weights to future labels, push accounting, unchanged selection thresholds, favourite support, and equivalence of incremental versus single-jump state advancement with future rows excluded.

App/PROD changes: 0. Existing prospective snapshots changed: 0. Provider calls: 0. LLM calls inside Python jobs: 0.
